"""What Aurora says in a channel without being asked.

A look that ends (services/detection/chat_intake.py) may leave a reply, and one
that reaches here has already been held against what the look ran
(services/detection/chat_proof.py). Whether it is posted is decided here, from
the channel's level and nothing else:

    listen  never
    sure    only for a problem Aurora confirmed
    free    whenever the look left something to say

Every reply goes in the thread of the message it is about, says that nobody
asked for it, and asks whether it was too much or not enough. The answer is
given the way everything else is told to Aurora: by saying so, or with one of
the two buttons under the reply, which move that channel one step and write it
to Aurora's Slack memory (services/detection/chat_memory.py).

When a look finds that two public channels are on the same thing, each thread
gets a pointer to the other. A private channel is never pointed at or from.

The reply text is written by a model that read untrusted chat, so it is cleaned
of anything Slack would turn into a notification before it is posted.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from services.detection import chat_memory, chat_signals
from services.detection.chat_signals import (
    LEVEL_FREE, LEVEL_LISTEN, LEVEL_OFF, LEVEL_SURE, STATUS_CONFIRMED,
)
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_LOG = "[DetectionChat]"
REPLY_MAX_CHARS = 1200
# A pointer in a thread that has been quiet longer than this helps nobody.
POINTER_WITHIN_HOURS = 6

ACTION_QUIETER = "aurora_speak_quieter"
ACTION_LOUDER = "aurora_speak_louder"

LEVEL_NAMES = {
    LEVEL_OFF: "Off",
    LEVEL_LISTEN: "Listening",
    LEVEL_SURE: "Speaks when sure",
    LEVEL_FREE: "Speaks freely",
}
# One line each, as Aurora would say it in the channel.
LEVEL_MEANS = {
    LEVEL_OFF: "I don't read this channel.",
    LEVEL_LISTEN: "I read this channel and look into problems, and I never post here.",
    LEVEL_SURE: "I reply here only once I have confirmed that something is broken.",
    LEVEL_FREE: "I reply here when I can help: problems, questions, and what I checked.",
}

_MENTION = re.compile(r"<[!@][^>]*>")
_BROADCAST = re.compile(r"@(channel|here|everyone)\b", re.IGNORECASE)


def clean_reply(value: Any) -> str:
    """The reply as it may be posted: no mention, no broadcast, capped."""
    if not isinstance(value, str):
        return ""
    text = _MENTION.sub("", value.replace("\x00", ""))
    text = _BROADCAST.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:REPLY_MAX_CHARS]


def thread_link(channel_id: str, ts: str) -> str:
    return "https://slack.com/archives/%s/p%s" % (channel_id, ts.replace(".", ""))


def _mrkdwn(text: str) -> Dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _context(text: str) -> Dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def stepped(level: str, louder: bool) -> Optional[str]:
    """The level one step from *level*, or None when there is no further step that way."""
    position = chat_signals.LEVELS.index(level) + (1 if louder else -1)
    return chat_signals.LEVELS[position] if 0 <= position < len(chat_signals.LEVELS) else None


def reply_blocks(text: str, incident_url: Optional[str] = None, pointer: str = "") -> List[Dict[str, Any]]:
    blocks = []
    if text:
        blocks.append(_mrkdwn(text))
    if pointer:
        blocks.append(_mrkdwn(pointer))
    if incident_url:
        blocks.append(_mrkdwn("I opened an incident and I am investigating: <%s|follow it in Aurora>." % incident_url))
    blocks.append(_context(
        "Nobody asked me: I noticed this while reading the channel. Too much, or not enough? "
        "Use a button, or tell me how you want it, like `@Aurora only speak up here for outages`."
    ))
    blocks.append({
        "type": "actions",
        "block_id": "aurora_speak_feedback",
        "elements": [
            {"type": "button", "action_id": ACTION_QUIETER, "value": "quieter",
             "text": {"type": "plain_text", "text": "Be quieter here"}},
            {"type": "button", "action_id": ACTION_LOUDER, "value": "louder",
             "text": {"type": "plain_text", "text": "Speak up more here"}},
        ],
    })
    return blocks


def level_set_note(level: str, slack_user_id: str, louder: bool) -> str:
    note = "<@%s> asked me to %s here. I am now set to *%s* in this channel. %s" % (
        slack_user_id, "speak up more" if louder else "be quieter", LEVEL_NAMES[level], LEVEL_MEANS[level],
    )
    if level in (LEVEL_OFF, LEVEL_LISTEN):
        # No message of mine will carry the buttons here again.
        note += " To change it later, mention me and say so."
    return note


# ---- posting --------------------------------------------------------------

def _workspace(user_id: str):
    """(client, team_id) for the org's Slack, or (None, None)."""
    from connectors.slack_connector.client import get_slack_client_for_user
    from utils.auth.stateless_auth import get_credentials_from_db

    client = get_slack_client_for_user(user_id)
    team_id = (get_credentials_from_db(user_id, "slack") or {}).get("team_id")
    return (client, team_id) if client and team_id else (None, None)


def _incident_url(incident_id: Optional[str]) -> Optional[str]:
    base = (os.getenv("FRONTEND_URL") or "").rstrip("/")
    return "%s/incidents/%s" % (base, incident_id) if base and incident_id else None


def _plan(cur, org_id: str, team_id: str, company: str, report_id: str) -> Optional[Dict[str, Any]]:
    """Decide what to post for a concluded report, and claim it. None when nothing is to be posted here.

    The claim (replied_at) is taken before Slack is called, so a look that
    concludes twice, or a retry, cannot post twice.
    """
    report = chat_signals.to_say(cur, org_id, report_id)
    if report is None:
        return None
    # What the team last told Aurora about its channels applies to this reply too.
    chat_memory.sync(cur, org_id)
    channel = chat_signals.channel_level(cur, org_id, team_id, report["channel_id"], company)
    if channel is None:
        return None
    real = report["status"] == STATUS_CONFIRMED
    other = None
    if report["related_to"]:
        other = chat_signals.other_public_report(cur, org_id, team_id, report_id, report["related_to"])
    plan = {"report": report, "channel": channel, "real": real, "other": other, "here": None}

    text = clean_reply(report["reply"])
    pointer = ""
    if other:
        pointer = "This looks like what <#%s> is already on: <%s|open that thread>." % (
            other["channel_id"], thread_link(other["channel_id"], other["thread_ts"]),
        )
    may_speak = (
        chat_signals.speaks(channel["level"], report["kind"], real)
        and report["fresh"] and not report["reply_ts"] and (text or pointer)
        and chat_signals.replies_left_today(cur, org_id, report["channel_id"]) > 0
    )
    if may_speak and chat_signals.claim_reply(cur, report_id):
        plan["here"] = {"text": text, "pointer": pointer}
    return plan


def _post_here(client, plan: Dict[str, Any]) -> Optional[str]:
    report, here = plan["report"], plan["here"]
    blocks = reply_blocks(here["text"], _incident_url(report["incident_id"]), here["pointer"])
    sent = client.send_message(
        channel=report["channel_id"], text=here["text"] or here["pointer"],
        thread_ts=report["thread_ts"], blocks=blocks,
    ) or {}
    return sent.get("ts")


def _post_other_side(client, cur, org_id: str, team_id: str, company: str, plan: Dict[str, Any]) -> None:
    """Tell the other channel's thread that this one is on the same thing, once."""
    report, other = plan["report"], plan["other"]
    if plan["channel"]["is_private"]:
        return
    there = chat_signals.channel_level(cur, org_id, team_id, other["channel_id"], company)
    if there is None or not chat_signals.speaks(there["level"], report["kind"], plan["real"]):
        return
    if chat_signals.replies_left_today(cur, org_id, other["channel_id"]) <= 0:
        return
    if not chat_signals.claim_pointer(cur, org_id, other["id"], report["id"], POINTER_WITHIN_HOURS):
        return
    cur.connection.commit()
    pointer = "<#%s> is on what looks like the same thing: <%s|open that thread>." % (
        report["channel_id"], thread_link(report["channel_id"], report["thread_ts"]),
    )
    client.send_message(
        channel=other["channel_id"], text=pointer, thread_ts=other["thread_ts"],
        blocks=reply_blocks("", None, pointer),
    )


def _reply_here(client, user_id: str, org_id: str, report_id: str, plan: Dict[str, Any]) -> bool:
    """Post the claimed reply and record it, or give the claim back when the post fails."""
    from services.detection.runner import _org_cursor

    posted_ts = None
    try:
        posted_ts = _post_here(client, plan)
    except Exception:
        logger.exception("%s Could not reply in channel %s", _LOG, sanitize(plan["report"]["channel_id"]))
    with _org_cursor(user_id, org_id) as (cur, conn):
        if cur is None:
            return bool(posted_ts)
        if posted_ts:
            chat_signals.record_reply(cur, report_id, posted_ts)
        else:
            chat_signals.release_reply(cur, report_id)
        conn.commit()
    return bool(posted_ts)


def _point_there(client, workspace: tuple, report_id: str, plan: Dict[str, Any]) -> None:
    """Tell the other channel. A failure here leaves the report's own reply as it is."""
    from services.detection.runner import _org_cursor

    user_id, org_id, team_id, company = workspace
    try:
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is not None:
                _post_other_side(client, cur, org_id, team_id, company, plan)
                conn.commit()
    except Exception:
        logger.exception("%s Could not point the other channel at report %s", _LOG, sanitize(report_id))


def say(user_id: str, org_id: str, report_id: str) -> bool:
    """Post what a concluded report's look left to say, where the channel's level allows it.

    Returns whether a reply was posted in the report's own thread. Never raises:
    a failed post must not undo what the look recorded.
    """
    from services.detection.runner import _org_cursor

    try:
        client, team_id = _workspace(user_id)
        if client is None:
            return False
        company = chat_signals.company_level(org_id)
        with _org_cursor(user_id, org_id) as (cur, conn):
            if cur is None:
                return False
            plan = _plan(cur, org_id, team_id, company, report_id)
            conn.commit()
        if plan is None:
            return False
        posted = bool(plan["here"]) and _reply_here(client, user_id, org_id, report_id, plan)
        if plan["other"]:
            _point_there(client, (user_id, org_id, team_id, company), report_id, plan)
        return posted
    except Exception:
        logger.exception("%s Speaking for report %s failed", _LOG, sanitize(report_id))
        return False
