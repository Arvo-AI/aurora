"""The words, and the look, of each setup email.

They read like a note from the guide, not a newsletter: short paragraphs,
one button that matters, the same four-part stack the setup page shows, and
a real signature. Plain text carries exactly the same content.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from urllib.parse import urlsplit

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


# ── Building blocks ──


@dataclass(frozen=True)
class P:
    text: str


@dataclass(frozen=True)
class Button:
    label: str
    url: str
    quiet: bool = False  # outlined, for the second choice


@dataclass(frozen=True)
class Stack:
    done: tuple[str, ...]


Block = P | Button | Stack

CONTINUE_SETUP = "Continue setup"
OPEN_SETUP = "Open setup"


def _app_url(ctx: EmailContext) -> str:
    parts = urlsplit(ctx.setup_url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ctx.setup_url


def _who(guide: Guide) -> str:
    return "one of the founders of Aurora" if "founder" in guide.title.lower() else "on the Aurora team"


def _booking(ctx: EmailContext, quiet: bool = True) -> list[Block]:
    return [Button("Pick a time", ctx.guide.booking_url, quiet=quiet)] if ctx.guide.booking_url else []


def _next_phrase(ctx: EmailContext) -> str | None:
    nxt = next_part(ctx.parts_done)
    return f"{nxt.noun} ({nxt.examples})" if nxt else None


# ── The emails ──


def _welcome(ctx: EmailContext) -> tuple[str, list[Block]]:
    g = ctx.guide
    blocks: list[Block] = [P(
        f"I'm {g.first_name}, {_who(g)}. I'll be your contact while you get set up, "
        "so if you reply to this, it comes straight to me."
    )]
    if ctx.via == "github" and "github" in ctx.providers:
        blocks.append(P("You've connected GitHub, so Aurora is already reviewing pull requests on the repos you picked."))
    elif ctx.via == "slack" and "slack" in ctx.providers:
        blocks.append(P("Aurora is in your Slack now. Mention @Aurora in any channel to try it."))
    if len(ctx.parts_done) < len(STACK):
        blocks += [
            P("Aurora gets really useful once it can see four parts of your stack. Here's where you are:"),
            Stack(ctx.parts_done),
            Button(CONTINUE_SETUP, ctx.setup_url),
        ]
    if g.booking_url:
        blocks += [
            P("If you'd rather do it together, pick 15 minutes and I'll walk you through it. "
              "Or reply with what your stack looks like and I'll tell you the fastest way in."),
            *_booking(ctx),
        ]
    else:
        blocks.append(P("Or reply with what your stack looks like and I'll tell you the fastest way in."))
    return "Getting you set up on Aurora", blocks


def _connected(ctx: EmailContext, parts: tuple[str, ...]) -> tuple[str, list[Block]]:
    names = [label(p) for p in sorted(ctx.providers) if any(p in PARTS[x].providers for x in parts)]
    names = names or [PARTS[x].noun for x in parts]
    joined = join_words(names)
    verb = "is" if len(names) == 1 else "are"
    blocks: list[Block] = [P(f"{joined} {verb} connected. " + " ".join(PARTS[p].payoff for p in parts)), Stack(ctx.parts_done)]
    nxt = _next_phrase(ctx)
    if nxt:
        blocks += [
            P(f"Next up is {nxt}. It takes a few minutes."),
            Button(CONTINUE_SETUP, ctx.setup_url),
            P("If anything in setup was confusing, reply and tell me. I read everything."),
        ]
    else:
        blocks += [
            P("That's your whole stack. The next alert that fires gets a full investigation. "
              "If the first one looks off in any way, reply and tell me what you expected."),
            Button("Open Aurora", _app_url(ctx)),
        ]
    return f"{joined} {verb} connected", blocks


def _checkin(ctx: EmailContext) -> tuple[str, list[Block]]:
    if not ctx.parts_done:
        return "Anything get in the way?", [
            P("You signed up for Aurora yesterday, but nothing is connected yet. Usually that means "
              "something got in the way: a permission you don't have, a tool we don't support yet, or just no time."),
            P("Which was it? Reply and I'll help, or grab 15 minutes and we'll connect it together."),
            *_booking(ctx, quiet=False),
            Button(OPEN_SETUP, ctx.setup_url, quiet=bool(ctx.guide.booking_url)),
        ]
    done = join_words([PARTS[p].noun for p in ctx.parts_done])
    return "How's the setup going?", [
        P(f"Aurora can see {done} so far."),
        Stack(ctx.parts_done),
        P(f"The piece that makes the biggest difference next is {_next_phrase(ctx)}. Is anything blocking you there?"),
        Button(CONTINUE_SETUP, ctx.setup_url),
        *([P("Happy to do it with you on a call."), *_booking(ctx)] if ctx.guide.booking_url else []),
    ]


def _nudge3(ctx: EmailContext) -> tuple[str, list[Block]]:
    return "Quick question", [
        P("What does your team use for alerts and for hosting? Tell me and I'll send the exact steps for your setup."),
    ]


def _nudge7(ctx: EmailContext) -> tuple[str, list[Block]]:
    return "Should I stop emailing?", [
        P("Nothing is connected yet, so this is the last time I'll nudge you about it. If you come back to it, "
          "everything is where you left it."),
        Button(OPEN_SETUP, ctx.setup_url),
        *([P("A call works too, or just reply."), *_booking(ctx)] if ctx.guide.booking_url else [P("A reply works too.")]),
        P("Thanks for trying Aurora."),
    ]


_KINDS = {"welcome": _welcome, "checkin": _checkin, "nudge_day3": _nudge3, "nudge_day7": _nudge7}


# ── Rendering ──

_INK = "#1f2328"
_MUTED = "#6a737d"
_LINE = "#e6e8eb"
_GREEN = "#1f9d61"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif"


def _e(s: str) -> str:
    return html.escape(s, quote=True)


def _text(blocks: list[Block]) -> list[str]:
    out = []
    for b in blocks:
        if isinstance(b, P):
            out.append(b.text)
        elif isinstance(b, Button):
            out.append(f"{b.label}: {b.url}")
        elif isinstance(b, Stack):
            out.append("   ".join(f"[{'x' if p.id in b.done else ' '}] {p.short}" for p in STACK))
    return out


def _html_block(b: Block) -> str:
    if isinstance(b, P):
        return f'<p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:{_INK}">{_e(b.text)}</p>'
    if isinstance(b, Button):
        bg, fg, border = ("#ffffff", _INK, "#d0d4d9") if b.quiet else ("#0b0d0e", "#ffffff", "#0b0d0e")
        return (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 0 22px"><tr>'
            f'<td style="border-radius:8px;background:{bg};border:1px solid {border}">'
            f'<a href="{_e(b.url)}" style="display:inline-block;padding:11px 20px;font-family:{_FONT};'
            f'font-size:14px;font-weight:600;color:{fg};text-decoration:none;border-radius:8px">{_e(b.label)}</a>'
            "</td></tr></table>"
        )
    cells = []
    for p in STACK:
        on = p.id in b.done
        dot = (
            f'<span style="display:inline-block;width:16px;height:16px;line-height:16px;border-radius:8px;vertical-align:-3px;'
            f'background:{_GREEN};color:#fff;font-size:11px;text-align:center;font-weight:700">&#10003;</span>'
            if on else
            '<span style="display:inline-block;width:14px;height:14px;border-radius:8px;border:1px solid #c4c9cf;vertical-align:-3px"></span>'
        )
        cells.append(
            f'<td style="padding:10px 12px;font-family:{_FONT};font-size:13px;color:{_INK if on else _MUTED};'
            f'white-space:nowrap">{dot}&nbsp;&nbsp;{_e(p.short)}</td>'
        )
    return (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="margin:0 0 22px;border:1px solid {_LINE};border-radius:10px;background:#fafbfc">'
        f'<tr>{"".join(cells)}</tr></table>'
    )


def _html(greeting: str, blocks: list[Block], guide: Guide, unsubscribe_url: str) -> str:
    initials = "".join(w[0].upper() for w in guide.name.split()[:2] if w)
    if guide.avatar_url.startswith("https://"):
        face = (f'<img src="{_e(guide.avatar_url)}" width="40" height="40" alt="" '
                'style="display:block;width:40px;height:40px;border-radius:20px">')
    else:
        face = (f'<div style="width:40px;height:40px;line-height:40px;border-radius:20px;background:#0b0d0e;'
                f'color:#fff;font-family:{_FONT};font-size:14px;font-weight:600;text-align:center">{_e(initials)}</div>')
    title = f'<div style="font-size:13px;color:{_MUTED}">{_e(guide.title)}, Aurora</div>' if guide.title else \
        f'<div style="font-size:13px;color:{_MUTED}">Aurora</div>'
    body = "".join(_html_block(b) for b in blocks)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="light"></head>
<body style="margin:0;padding:0;background:#f4f5f6">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f4f5f6"><tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#ffffff;border:1px solid {_LINE};border-radius:14px">
<tr><td style="height:3px;line-height:3px;font-size:0;border-radius:14px 14px 0 0;background:#1a99cc;background-image:linear-gradient(90deg,#26e680,#1a99cc 55%,#9926b3)">&nbsp;</td></tr>
<tr><td style="padding:32px 36px 8px;font-family:{_FONT}">
<p style="margin:0 0 18px;font-size:15px;line-height:1.6;color:{_INK}">{_e(greeting)}</p>
{body}
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:8px 0 28px"><tr>
<td style="padding-right:12px;vertical-align:middle">{face}</td>
<td style="vertical-align:middle;font-family:{_FONT}"><div style="font-size:14px;font-weight:600;color:{_INK}">{_e(guide.name)}</div>{title}</td>
</tr></table>
</td></tr></table>
<p style="margin:18px 0 0;font-family:{_FONT};font-size:12px;line-height:1.5;color:#8b9198">You're getting this because you signed up for Aurora.
<a href="{_e(unsubscribe_url)}" style="color:#8b9198">Stop setup emails</a></p>
</td></tr></table></body></html>"""


def compose(kind: str, ctx: EmailContext, parts: tuple[str, ...] = ()) -> Composed:
    if kind == "connected":
        subject, blocks = _connected(ctx, parts)
    elif kind in _KINDS:
        subject, blocks = _KINDS[kind](ctx)
    else:
        raise ValueError(f"unknown onboarding email kind: {kind}")

    g = ctx.guide
    greeting = f"Hi {ctx.first_name}," if ctx.first_name else "Hi,"
    sign = g.first_name if not g.title else f"{g.first_name}\n{g.title}, Aurora"
    text = "\n\n".join([greeting, *_text(blocks), sign])
    text += f"\n\n--\nYou're getting this because you signed up for Aurora. Stop setup emails: {ctx.unsubscribe_url}\n"
    return Composed(subject=subject, text=text, html=_html(greeting, blocks, g, ctx.unsubscribe_url))
