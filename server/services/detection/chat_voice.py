"""What Aurora says in a channel without being asked.

A look that ends (services/detection/chat_intake.py) may leave a reply. Whether
it is posted is decided here, from the channel's level and nothing else:

    listen  never
    sure    only for a problem Aurora confirmed
    free    whenever the look left something to say

Every reply goes in the thread of the message it is about, says that nobody
asked for it, and carries the dial: anyone in the channel can turn Aurora down,
or off, from the message itself. The first reply in a channel also explains
what Aurora is doing there.

When a look finds that two public channels are on the same thing, each thread
gets a pointer to the other. A private channel is never pointed at or from.

The reply text is written by a model that read untrusted chat, so it is cleaned
of anything Slack would turn into a notification before it is posted.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from services.detection import chat_signals
from services.detection.chat_signals import (
    LEVEL_FREE, LEVEL_LISTEN, LEVEL_OFF, LEVEL_SURE, STATUS_CONFIRMED,
)
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_LOG = "[DetectionChat]"
REPLY_MAX_CHARS = 1200
# A pointer in a thread that has been quiet longer than this helps nobody.
POINTER_WITHIN_HOURS = 6

ACTION_HELPFUL = "aurora_speak_helpful"
ACTION_UNHELPFUL = "aurora_speak_unhelpful"
ACTION_SETTINGS = "aurora_speak_settings"
ACTION_LEVEL_PREFIX = "aurora_speak_level_"

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
# Loudest first: the order of the buttons.
_BUTTON_ORDER = (LEVEL_FREE, LEVEL_SURE, LEVEL_LISTEN, LEVEL_OFF)
_BUTTON_LABELS = {
    LEVEL_FREE: "Speak freely",
    LEVEL_SURE: "Only when sure",
    LEVEL_LISTEN: "Just listen",
    LEVEL_OFF: "Stay out",
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


def level_buttons(current: str) -> Dict[str, Any]:
    """The dial, as buttons. The level in force is the highlighted one."""
    elements = []
    for level in _BUTTON_ORDER:
        button = {
            "type": "button",
            "action_id": ACTION_LEVEL_PREFIX + level,
            "value": level,
            "text": {"type": "plain_text", "text": _BUTTON_LABELS[level]},
        }
        if level == current:
            button["style"] = "primary"
        elements.append(button)
    return {"type": "actions", "block_id": "aurora_speak_dial", "elements": elements}


def settings_blocks(current: str, lead: str = "") -> List[Dict[str, Any]]:
    """The dial with what each stop means. Shown only to the person who asked for it."""
    lines = "\n".join(
        "*%s*: %s" % (_BUTTON_LABELS[level], LEVEL_MEANS[level]) for level in _BUTTON_ORDER
    )
    head = (lead + "\n\n" if lead else "") + "How much I do in this channel. It applies to everyone here."
    return [_mrkdwn(head), _mrkdwn(lines), level_buttons(current)]


def introduction_blocks(level: str) -> List[Dict[str, Any]]:
    return [
        {"type": "divider"},
        _mrkdwn(
            "*This is the first time I've spoken up here without being asked.* "
            "I read this channel and reply in a thread when I can help. "
            "If that is too much, or not wanted here, anyone in the channel can change it:"
        ),
        level_buttons(level),
    ]


def reply_blocks(report_id: str, text: str, level: str, incident_url: Optional[str] = None,
                 pointer: str = "", first_time: bool = False) -> List[Dict[str, Any]]:
    blocks = []
    if text:
        blocks.append(_mrkdwn(text))
    if pointer:
        blocks.append(_mrkdwn(pointer))
    if incident_url:
        blocks.append(_mrkdwn("I opened an incident and I am investigating: <%s|follow it in Aurora>." % incident_url))
    blocks.append(_context(
        "Nobody asked me. I am set to *%s* in this channel." % LEVEL_NAMES.get(level, level)
    ))
    blocks.append({
        "type": "actions",
        "block_id": "aurora_speak_feedback",
        "elements": [
            {"type": "button", "action_id": ACTION_HELPFUL, "value": report_id,
             "text": {"type": "plain_text", "text": "Helpful"}},
            {"type": "button", "action_id": ACTION_UNHELPFUL, "value": report_id,
             "text": {"type": "plain_text", "text": "Not useful"}},
            {"type": "button", "action_id": ACTION_SETTINGS, "value": report_id,
             "text": {"type": "plain_text", "text": "Change how much I speak"}},
        ],
    })
    if first_time:
        blocks.extend(introduction_blocks(level))
    return blocks


def level_set_note(level: str, slack_user_id: str) -> str:
    note = "<@%s> set me to *%s* in this channel. %s" % (
        slack_user_id, LEVEL_NAMES[level], LEVEL_MEANS[level],
    )
    if level in (LEVEL_OFF, LEVEL_LISTEN):
        # No message of mine will carry the dial here again.
        note += " To change it later, open Checks in Aurora."
    return note


def without_introduction(blocks: List[Dict[str, Any]], note: str) -> List[Dict[str, Any]]:
    """A reply's blocks once someone has answered its introduction: the dial is replaced by who chose."""
    kept = []
    for block in blocks or []:
        if block.get("type") == "divider":
            break
        kept.append(block)
    return kept + [_context(note)]


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
        plan["here"] = {
            "text": text, "pointer": pointer,
            "first_time": chat_signals.introduce(cur, org_id, report["channel_id"]),
        }
    return plan


def _post_here(client, plan: Dict[str, Any]) -> Optional[str]:
    report, here, level = plan["report"], plan["here"], plan["channel"]["level"]
    blocks = reply_blocks(
        report["id"], here["text"], level, _incident_url(report["incident_id"]),
        here["pointer"], here["first_time"],
    )
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
    first_time = chat_signals.introduce(cur, org_id, other["channel_id"])
    cur.connection.commit()
    pointer = "<#%s> is on what looks like the same thing: <%s|open that thread>." % (
        report["channel_id"], thread_link(report["channel_id"], report["thread_ts"]),
    )
    client.send_message(
        channel=other["channel_id"], text=pointer, thread_ts=other["thread_ts"],
        blocks=reply_blocks(other["id"], "", there["level"], None, pointer, first_time),
    )


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

        posted_ts = None
        if plan["here"]:
            try:
                posted_ts = _post_here(client, plan)
            except Exception:
                logger.exception("%s Could not reply in channel %s", _LOG, sanitize(plan["report"]["channel_id"]))
            with _org_cursor(user_id, org_id) as (cur, conn):
                if cur is not None:
                    if posted_ts:
                        chat_signals.record_reply(cur, report_id, posted_ts)
                    else:
                        chat_signals.release_reply(cur, org_id, report_id, plan["here"]["first_time"])
                    conn.commit()

        if plan["other"]:
            try:
                with _org_cursor(user_id, org_id) as (cur, conn):
                    if cur is not None:
                        _post_other_side(client, cur, org_id, team_id, company, plan)
                        conn.commit()
            except Exception:
                logger.exception("%s Could not point the other channel at report %s", _LOG, sanitize(report_id))
        return bool(posted_ts)
    except Exception:
        logger.exception("%s Speaking for report %s failed", _LOG, sanitize(report_id))
        return False
