"""Reading chat no faster than Slack allows.

Slack limits how often an app may read a workspace's messages, per method and
per workspace. For an app installed from outside its Marketplace that is about
one `conversations.history` and one `conversations.replies` a minute; an app a
workspace built for itself gets far more. Aurora cannot tell which it is until
Slack says so, so it learns: the first 429 on a method marks that workspace as
limited for a day, and from then on calls to that method are spaced out.

A call that may not be made now raises Busy. Nothing went wrong and nothing is
lost: the reading stops where it is and goes on from there on a later turn.
What it had already been given by Slack is kept for that, for a few minutes
(see progress below), so a channel that needs several calls is read one call
at a time and not started over each minute. That is message text, held in
Redis for at most PROGRESS_SECONDS from when the reading began, and dropped as
soon as the reading is done.

Without Redis there is no pacing and no progress kept. A 429 still only stops
the turn.
"""

import json
import logging
import time
from typing import Any, Dict, Optional

from connectors.slack_connector.client import SlackAPIError, SlackRateLimited
from utils.cache.redis_client import get_redis_client
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_LOG = "[DetectionChat]"
HISTORY = "conversations.history"
REPLIES = "conversations.replies"
# Between two calls to one method in a workspace known to be limited. A little
# under the minute the reader's own turns are apart, so that a turn does not
# find the last one's call still in the way. Slack says so if it is too soon.
SPACING_SECONDS = 50
# How long a workspace stays known as limited after its last 429.
LIMITED_SECONDS = 24 * 3600
# How long what Slack already gave for an unfinished reading is kept, counted
# from when the reading began. A reading not finished by then starts over.
PROGRESS_SECONDS = 30 * 60
# Slack is taken at its word on how long to wait, up to this.
MAX_WAIT_SECONDS = 15 * 60


class Busy(Exception):
    """Slack is not to be asked this now."""


class Unreachable(Exception):
    """The request did not get an answer from Slack."""


def _key(kind: str, *parts: str) -> str:
    return "detection:chat:%s:%s" % (kind, ":".join(parts))


def _redis():
    try:
        return get_redis_client()
    except Exception:
        return None


def limited(team_id: str, endpoint: str = HISTORY) -> bool:
    """Whether Slack has rate limited this workspace on *endpoint* in the last day."""
    redis = _redis()
    try:
        return bool(redis and redis.exists(_key("limited", team_id, endpoint)))
    except Exception:
        logger.warning("%s Could not tell whether the workspace is rate limited on %s: Redis did not answer",
                       _LOG, endpoint)
        return False


# Hold KEYS[1] for ARGV[1] seconds, unless it is already held for longer.
_HOLD = """
if redis.call('TTL', KEYS[1]) < tonumber(ARGV[1]) then
    redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
end
return 1
"""


def _take(team_id: str, endpoint: str) -> None:
    """Take the turn to call *endpoint* now, or raise Busy."""
    redis = _redis()
    if redis is None:
        return
    wait = _key("wait", team_id, endpoint)
    try:
        if redis.exists(_key("limited", team_id, endpoint)):
            free = redis.set(wait, "1", nx=True, ex=SPACING_SECONDS)
        else:
            free = not redis.exists(wait)
    except Exception:
        logger.warning("%s Could not pace Slack calls: Redis did not answer", _LOG)
        return
    if not free:
        raise Busy(endpoint)


def _was_limited(team_id: str, endpoint: str, retry_after: Optional[int]) -> None:
    wait = SPACING_SECONDS if retry_after is None else min(max(retry_after, 1), MAX_WAIT_SECONDS)
    logger.info("%s Slack rate limited %s; not asking this workspace again for %ds", _LOG, endpoint, wait)
    redis = _redis()
    if redis is None:
        return
    try:
        redis.set(_key("limited", team_id, endpoint), "1", ex=LIMITED_SECONDS)
        # Two calls can be turned away at once: the longer wait is the one kept.
        redis.eval(_HOLD, 1, _key("wait", team_id, endpoint), wait)
    except Exception:
        logger.warning("%s Could not record the rate limit: Redis did not answer", _LOG)


def ask(client, team_id: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One GET to Slack for the reader. Raises Busy when it may not be made now,
    or when Slack turned it away as too many, and Unreachable when no answer
    came. An answer that refuses (SlackAPIError) is passed on. Never waits."""
    _take(team_id, endpoint)
    try:
        return client._make_request("GET", endpoint, params, max_retries=0) or {}
    except SlackRateLimited as limit:
        _was_limited(team_id, endpoint, limit.retry_after)
        raise Busy(endpoint) from limit
    except SlackAPIError:
        raise
    except ValueError as failure:
        raise Unreachable(endpoint) from failure


# ---- an unfinished reading --------------------------------------------------

class Progress:
    """What Slack has answered so far for one reading of one channel.

    A reading is the channel from one cursor up to one moment. Asked the same
    thing twice within it, Slack is only called once: the second time, and any
    later turn that takes the reading up again, gets the first answer.
    """

    def __init__(self, last_ts: str, upto: str, answers: Optional[Dict[str, Any]] = None,
                 began: Optional[float] = None):
        self.last_ts, self.upto = last_ts, upto
        self.answers: Dict[str, Any] = answers or {}
        self.began = time.time() if began is None else began

    def ask(self, client, team_id: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        key = endpoint + " " + json.dumps(params, sort_keys=True)
        if key not in self.answers:
            try:
                result = ask(client, team_id, endpoint, params)
            except SlackAPIError as gone:
                # A thread that was deleted has no replies. Kept as an answer
                # like any other, so a reading taken up again does not spend
                # its next call asking for it once more.
                if endpoint != REPLIES or gone.error != "thread_not_found":
                    raise
                result = {}
            self.answers[key] = {
                "messages": result.get("messages", []),
                "has_more": bool(result.get("has_more")),
                "response_metadata": {
                    "next_cursor": (result.get("response_metadata") or {}).get("next_cursor"),
                },
            }
        return self.answers[key]


def resume(org_id: str, channel_id: str, last_ts: str, upto: str) -> Progress:
    """The unfinished reading of this channel from *last_ts*, or a new one up to *upto*."""
    redis = _redis()
    try:
        kept = json.loads(redis.get(_key("progress", org_id, channel_id)) or "null") if redis else None
    except Exception:
        logger.warning("%s Could not take up the reading of channel %s again: Redis did not answer",
                       _LOG, sanitize(channel_id))
        kept = None
    if isinstance(kept, dict) and kept.get("last_ts") == last_ts and kept.get("upto"):
        return Progress(last_ts, kept["upto"], kept.get("answers") or {}, kept.get("began"))
    return Progress(last_ts, upto)


def keep(org_id: str, channel_id: str, progress: Progress) -> None:
    """Keep an unfinished reading for the turn that takes it up again.

    Until PROGRESS_SECONDS after it began, however many times it is kept. Past
    that it is dropped, and the channel is read from the start.
    """
    redis = _redis()
    if redis is None or not progress.answers:
        return
    left = int(progress.began + PROGRESS_SECONDS - time.time())
    try:
        if left <= 0:
            logger.warning("%s The reading of channel %s was not finished in %d minutes and starts over",
                           _LOG, sanitize(channel_id), PROGRESS_SECONDS // 60)
            redis.delete(_key("progress", org_id, channel_id))
            return
        redis.set(
            _key("progress", org_id, channel_id),
            json.dumps({"last_ts": progress.last_ts, "upto": progress.upto, "began": progress.began,
                        "answers": progress.answers}),
            ex=left,
        )
    except Exception:
        logger.warning("%s Could not keep the unfinished reading of channel %s: Redis did not answer",
                       _LOG, sanitize(channel_id))


def done(org_id: str, channel_id: str) -> None:
    """The reading is finished: forget what was kept for it."""
    redis = _redis()
    try:
        if redis:
            redis.delete(_key("progress", org_id, channel_id))
    except Exception:
        # It expires by itself, and is not used once the cursor has moved.
        logger.warning("%s Could not drop the finished reading of channel %s: Redis did not answer",
                       _LOG, sanitize(channel_id))
