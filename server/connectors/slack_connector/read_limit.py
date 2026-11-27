"""What Aurora knows of Slack's limit on reading a workspace's messages.

Slack limits `conversations.history` and `conversations.replies` per method and
per workspace. For an app installed from outside its Marketplace that is about
one call a minute; an app a workspace built for itself gets far more. Aurora
cannot tell which it is until Slack says so, so it learns: the first 429 on a
method marks that workspace as limited for a day.

Two kinds of caller share that one call a minute. The chat reader works in the
background and can always come back later: it takes a turn (take), and stays
out of the way of the others. Everything else, a person's question the agent
answers or an alert being threaded, has someone waiting: it says it is about to
call (waiting), and the reader leaves the method alone until it has.

Without Redis nothing is known and nothing is held back.
"""

import logging
from typing import Optional

from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

_LOG = "[SlackReadLimit]"
HISTORY = "conversations.history"
REPLIES = "conversations.replies"
METHODS = (HISTORY, REPLIES)
# Between two of the reader's calls to one method in a workspace known to be
# limited. A little under the minute the reader's own turns are apart, so that
# a turn does not find the last one's call still in the way. Slack says so if
# it is too soon.
SPACING_SECONDS = 50
# How long a workspace stays known as limited after its last 429.
LIMITED_SECONDS = 24 * 3600
# Slack is taken at its word on how long to wait. Up to a day: a header that
# says more is not believed.
MAX_WAIT_SECONDS = LIMITED_SECONDS
# How long the reader stays off a method for a caller that said it was about
# to use it and never said it was done.
WAITING_SECONDS = 120

# Hold KEYS[1] for ARGV[1] seconds, unless it is already held for longer.
_HOLD = """
if redis.call('TTL', KEYS[1]) < tonumber(ARGV[1]) then
    redis.call('SET', KEYS[1], '1', 'EX', ARGV[1])
end
return 1
"""


def _key(kind: str, team_id: str, endpoint: str) -> str:
    return "detection:chat:%s:%s:%s" % (kind, team_id, endpoint)


def redis():
    try:
        return get_redis_client()
    except Exception:
        return None


def limited(team_id: str, endpoint: str = HISTORY) -> bool:
    """Whether Slack has rate limited this workspace on *endpoint* in the last day."""
    client = redis()
    try:
        return bool(client and client.exists(_key("limited", team_id, endpoint)))
    except Exception:
        logger.warning("%s Could not tell whether the workspace is rate limited on %s: Redis did not answer",
                       _LOG, endpoint)
        return False


def take(team_id: str, endpoint: str) -> bool:
    """The reader asks for the turn to call *endpoint* now. False when it may not:
    its last call is too recent, Slack said to wait, or someone else is waiting
    to make theirs."""
    client = redis()
    if client is None:
        return True
    wait = _key("wait", team_id, endpoint)
    try:
        if client.exists(_key("waiting", team_id, endpoint)):
            return False
        if client.exists(_key("limited", team_id, endpoint)):
            return bool(client.set(wait, "1", nx=True, ex=SPACING_SECONDS))
        return not client.exists(wait)
    except Exception:
        logger.warning("%s Could not pace Slack calls: Redis did not answer", _LOG)
        return True


def was_limited(team_id: str, endpoint: str, retry_after: Optional[int]) -> None:
    """Slack answered 429 on *endpoint*: the workspace is limited, and the reader
    does not ask again before Slack said, and never sooner than its own spacing."""
    wait = min(max(retry_after or 0, SPACING_SECONDS), MAX_WAIT_SECONDS)
    logger.info("%s Slack rate limited %s; the reader does not ask this workspace again for %ds",
                _LOG, endpoint, wait)
    client = redis()
    if client is None:
        return
    try:
        client.set(_key("limited", team_id, endpoint), "1", ex=LIMITED_SECONDS)
        # Two calls can be turned away at once: the longer wait is the one kept.
        client.eval(_HOLD, 1, _key("wait", team_id, endpoint), wait)
    except Exception:
        logger.warning("%s Could not record the rate limit: Redis did not answer", _LOG)


def waiting(team_id: str, endpoint: str) -> None:
    """A caller with someone waiting is about to call *endpoint*: the reader
    leaves it alone until answered() or WAITING_SECONDS, whichever is first.
    Only matters where the workspace is limited, and costs nothing elsewhere."""
    client = redis()
    try:
        if client and client.exists(_key("limited", team_id, endpoint)):
            client.set(_key("waiting", team_id, endpoint), "1", ex=WAITING_SECONDS)
    except Exception:
        logger.warning("%s Could not hold the reader back: Redis did not answer", _LOG)


def answered(team_id: str, endpoint: str, used: bool = True) -> None:
    """The caller that was waiting() is done. With *used*, it got its answer: in
    a limited workspace that was this minute's call, so the reader waits its
    spacing from here."""
    client = redis()
    try:
        if client and client.exists(_key("limited", team_id, endpoint)):
            if used:
                client.eval(_HOLD, 1, _key("wait", team_id, endpoint), SPACING_SECONDS)
            client.delete(_key("waiting", team_id, endpoint))
    except Exception:
        logger.warning("%s Could not let the reader back in: Redis did not answer", _LOG)
