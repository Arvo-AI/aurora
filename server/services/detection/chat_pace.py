"""Reading chat no faster than Slack allows.

What Aurora knows of Slack's limit on a workspace, and who gets the one call a
minute it leaves, is in connectors/slack_connector/read_limit.py. This is the
reader's side of it.

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

from connectors.slack_connector import read_limit
from connectors.slack_connector.client import SlackAPIError, SlackRateLimited
from connectors.slack_connector.read_limit import HISTORY, REPLIES, limited  # noqa: F401  (the reader's names)
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_LOG = "[DetectionChat]"
# How long what Slack already gave for an unfinished reading is kept, counted
# from when the reading began. A reading not finished by then starts over.
PROGRESS_SECONDS = 30 * 60


class Busy(Exception):
    """Slack is not to be asked this now."""


class Unreachable(Exception):
    """The request did not get an answer from Slack."""


def _key(kind: str, *parts: str) -> str:
    return "detection:chat:%s:%s" % (kind, ":".join(parts))


def ask(client, team_id: str, endpoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """One GET to Slack for the reader. Raises Busy when it may not be made now,
    or when Slack turned it away as too many, and Unreachable when no answer
    came. An answer that refuses (SlackAPIError) is passed on. Never waits."""
    if not read_limit.take(team_id, endpoint):
        raise Busy(endpoint)
    try:
        return client._make_request("GET", endpoint, params, max_retries=0) or {}
    except SlackRateLimited as limit:
        read_limit.was_limited(team_id, endpoint, limit.retry_after)
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
    redis = read_limit.redis()
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
    redis = read_limit.redis()
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
    redis = read_limit.redis()
    try:
        if redis:
            redis.delete(_key("progress", org_id, channel_id))
    except Exception:
        # It expires by itself, and is not used once the cursor has moved.
        logger.warning("%s Could not drop the finished reading of channel %s: Redis did not answer",
                       _LOG, sanitize(channel_id))
