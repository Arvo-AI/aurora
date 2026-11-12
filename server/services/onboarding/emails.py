"""The words of each setup email.

They read like a note from the guide, not a newsletter: plain text first, a
light HTML copy of the same words, no banners, one link that matters.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from services.onboarding.guides import Guide
from services.onboarding.stack import PARTS, STACK, join_words, label, next_part


@dataclass(frozen=True)
class EmailContext:
    guide: Guide
    first_name: str | None
    via: str | None
    providers: frozenset[str]  # connected right now
    parts_done: tuple[str, ...]
    setup_url: str
    unsubscribe_url: str


@dataclass(frozen=True)
class Composed:
    subject: str
    text: str
    html: str


def _hi(ctx: EmailContext) -> str:
    return f"Hi {ctx.first_name}," if ctx.first_name else "Hi,"


def _call_line(ctx: EmailContext, lead: str) -> str:
    if ctx.guide.booking_url:
        return f"{lead} {ctx.guide.booking_url}"
    return "Or just reply to this email and I'll help."


def _who(guide: Guide) -> str:
    title = guide.title.lower()
    if "founder" in title:
        return "one of the founders of Aurora"
    return "on the Aurora team"


def _welcome(ctx: EmailContext) -> tuple[str, list[str]]:
    g = ctx.guide
    paras = [f"I'm {g.first_name}, {_who(g)}. I'll be your contact while you get set up."]
    if ctx.via == "github" and "github" in ctx.providers:
        paras.append("You've connected GitHub, so Aurora is already reviewing pull requests on the repos you picked.")
    elif ctx.via == "slack" and "slack" in ctx.providers:
        paras.append("Aurora is in your Slack now. Mention @Aurora in any channel to try it.")
    missing = [p for p in STACK if p.id not in ctx.parts_done]
    if missing:
        paras.append(
            f"Aurora gets really useful once it can see {join_words([p.noun for p in missing])}. "
            f"Your setup page shows what's left, and each tool takes a few minutes:\n{ctx.setup_url}"
        )
    paras.append(_call_line(ctx, "If you'd rather do it together, pick a time and I'll walk you through it:"))
    if ctx.guide.booking_url:
        paras.append("Or reply with what your stack looks like and I'll tell you the fastest way in.")
    return "Getting you set up on Aurora", paras


def _connected(ctx: EmailContext, parts: tuple[str, ...]) -> tuple[str, list[str]]:
    names = [label(p) for p in sorted(ctx.providers) if any(p in PARTS[x].providers for x in parts)]
    names = names or [PARTS[x].noun for x in parts]
    joined = join_words(names)
    verb = "is" if len(names) == 1 else "are"
    paras = [f"{joined} {verb} connected. " + " ".join(PARTS[p].payoff for p in parts)]
    nxt = next_part(ctx.parts_done)
    if nxt:
        paras.append(f"Next up is {nxt.noun} ({nxt.examples}):\n{ctx.setup_url}")
        paras.append("If anything in setup was confusing, reply and tell me. I read everything.")
    else:
        paras.append(
            "That's your whole stack. The next alert that fires gets a full investigation. "
            "If the first one looks off in any way, reply and tell me what you expected."
        )
    return f"{joined} {verb} connected", paras


def _checkin(ctx: EmailContext) -> tuple[str, list[str]]:
    if not ctx.parts_done:
        return "Anything get in the way?", [
            "You signed up for Aurora yesterday, but nothing is connected yet. Usually that means "
            "something got in the way: a permission you don't have, a tool we don't support yet, or no time.",
            "Which was it? Reply and I'll help.",
            _call_line(ctx, "Or grab 15 minutes and we'll connect it together:"),
        ]
    done = [PARTS[p].noun for p in ctx.parts_done]
    nxt = next_part(ctx.parts_done)
    assert nxt is not None  # schedule.py skips the check-in once everything is connected
    return "How's the setup going?", [
        f"Aurora can see {join_words(done)} so far. The piece that makes the biggest difference next "
        f"is {nxt.noun} ({nxt.examples}):\n{ctx.setup_url}",
        "Is anything blocking you there?",
        _call_line(ctx, "Happy to do it with you on a call:"),
    ]


def _nudge3(ctx: EmailContext) -> tuple[str, list[str]]:
    return "Quick question", [
        "What does your team use for alerts and for hosting? Tell me and I'll send the exact steps for your setup.",
    ]


def _nudge7(ctx: EmailContext) -> tuple[str, list[str]]:
    paras = [
        "Nothing is connected yet, so this is my last setup email.",
        f"If you come back to it, your setup page is here:\n{ctx.setup_url}",
    ]
    if ctx.guide.booking_url:
        paras.append(f"My calendar is here if a call is easier:\n{ctx.guide.booking_url}")
    paras.append("A reply works too. Thanks for trying Aurora.")
    return "Should I stop emailing?", paras


_URL = re.compile(r"https?://[^\s<>\"]+")


def _to_html(paragraphs: list[str]) -> str:
    def para(p: str) -> str:
        out, pos = [], 0
        for m in _URL.finditer(p):
            out.append(html.escape(p[pos:m.start()]))
            url = m.group(0)
            out.append(f'<a href="{html.escape(url, quote=True)}" style="color:#0b6bcb">{html.escape(url)}</a>')
            pos = m.end()
        out.append(html.escape(p[pos:]))
        return "<p style=\"margin:0 0 16px\">" + "".join(out).replace("\n", "<br>") + "</p>"

    body = "".join(para(p) for p in paragraphs)
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.55;color:#1a1a1a;max-width:560px">'
        f"{body}</div>"
    )


def compose(kind: str, ctx: EmailContext, parts: tuple[str, ...] = ()) -> Composed:
    if kind == "welcome":
        subject, paras = _welcome(ctx)
    elif kind == "connected":
        subject, paras = _connected(ctx, parts)
    elif kind == "checkin":
        subject, paras = _checkin(ctx)
    elif kind == "nudge_day3":
        subject, paras = _nudge3(ctx)
    elif kind == "nudge_day7":
        subject, paras = _nudge7(ctx)
    else:
        raise ValueError(f"unknown onboarding email kind: {kind}")

    g = ctx.guide
    sign = g.first_name if not g.title else f"{g.first_name}\n{g.title}, Aurora"
    paragraphs = [_hi(ctx), *paras, sign]
    footer = f"You're getting this because you signed up for Aurora. Stop setup emails: {ctx.unsubscribe_url}"

    text = "\n\n".join(paragraphs) + f"\n\n--\n{footer}\n"
    html_body = _to_html(paragraphs) + (
        '<p style="margin:28px 0 0;font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:12px;color:#8a8a8a">'
        "You're getting this because you signed up for Aurora. "
        f'<a href="{html.escape(ctx.unsubscribe_url, quote=True)}" style="color:#8a8a8a">Stop setup emails</a></p>'
    )
    return Composed(subject=subject, text=text, html=html_body)
