"""The words, and the look, of each setup email.

They read like a letter from the guide: a narrow column of short paragraphs, the four-part stack as the same slim bars the
setup page shows, one button, and a plain signature. Plain text carries
exactly the same content.
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
    # One phrase inside the text that becomes a link: (phrase, url).
    link: tuple[str, str] | None = None


@dataclass(frozen=True)
class Button:
    label: str
    url: str


@dataclass(frozen=True)
class Stack:
    done: tuple[str, ...]


Block = P | Button | Stack

CONTINUE_SETUP = "Continue setup"
OPEN_SETUP = "Open setup"
BOOK_PHRASE = "grab 15 minutes with me"


def _app_url(ctx: EmailContext) -> str:
    parts = urlsplit(ctx.setup_url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ctx.setup_url


def _who(guide: Guide) -> str:
    return "one of the founders of Aurora" if "founder" in guide.title.lower() else "on the Aurora team"


def _book(ctx: EmailContext, lead: str, tail: str = "") -> P:
    """A sentence offering a call, with the booking link inline; or a reply, without one."""
    if ctx.guide.booking_url:
        return P(f"{lead} {BOOK_PHRASE}{tail}", link=(BOOK_PHRASE, ctx.guide.booking_url))
    return P("Or just reply to this email and I'll help.")


def _remaining(ctx: EmailContext) -> int:
    return len(STACK) - len(ctx.parts_done)


# ── The emails ──


def _welcome(ctx: EmailContext) -> tuple[str, list[Block]]:
    g = ctx.guide
    blocks: list[Block] = [P(
        f"I'm {g.first_name}, {_who(g)}. I'll be the one helping you get set up, so replies to this email come straight to me."
    )]
    left = _remaining(ctx)
    if ctx.via == "github" and "github" in ctx.providers:
        blocks.append(P(f"GitHub is connected, so Aurora is already reviewing pull requests on the repos you picked. "
                        f"Connect the other {left} and it can investigate an incident end to end:"))
    elif ctx.via == "slack" and "slack" in ctx.providers:
        blocks.append(P(f"Aurora is in your Slack. Connect the other {left} parts of your stack and it can investigate "
                        "an incident end to end:"))
    elif left:
        blocks.append(P("Aurora needs to see four parts of your stack to investigate an incident end to end:"))
    if left:
        blocks += [Stack(ctx.parts_done), Button(CONTINUE_SETUP, ctx.setup_url)]
    blocks.append(_book(ctx, "Rather do it together? Reply with what your stack looks like, or", "."))
    return "Getting you set up on Aurora", blocks


def _connected(ctx: EmailContext, parts: tuple[str, ...]) -> tuple[str, list[Block]]:
    names = [label(p) for p in sorted(ctx.providers) if any(p in PARTS[x].providers for x in parts)]
    names = names or [PARTS[x].noun for x in parts]
    joined = join_words(names)
    verb = "is" if len(names) == 1 else "are"
    blocks: list[Block] = [P(f"{joined} {verb} connected. " + " ".join(PARTS[p].payoff for p in parts)), Stack(ctx.parts_done)]
    nxt = next_part(ctx.parts_done)
    if nxt:
        blocks += [
            P(f"Next up is {nxt.noun} ({nxt.examples}). It takes a few minutes."),
            Button(CONTINUE_SETUP, ctx.setup_url),
            P("If anything in setup was confusing, reply and tell me. I read everything."),
        ]
    else:
        blocks += [
            P("That's your whole stack. The next alert that fires gets a full investigation. "
              "If the first one looks off, reply and tell me what you expected."),
            Button("Open Aurora", _app_url(ctx)),
        ]
    return f"{joined} {verb} connected", blocks


def _checkin(ctx: EmailContext) -> tuple[str, list[Block]]:
    if not ctx.parts_done:
        return "Anything get in the way?", [
            P("You signed up for Aurora yesterday, but nothing is connected yet. Usually something got in the way: "
              "a permission you don't have, a tool we don't support yet, or just no time."),
            _book(ctx, "Which was it? Reply and I'll help, or", " and we'll connect it together."),
            Button(OPEN_SETUP, ctx.setup_url),
        ]
    nxt = next_part(ctx.parts_done)
    done = join_words([PARTS[p].noun for p in ctx.parts_done])
    return "How's the setup going?", [
        P(f"Aurora can see {done} so far."),
        Stack(ctx.parts_done),
        P(f"The piece that makes the biggest difference next is {nxt.noun} ({nxt.examples}). "
          "Is anything blocking you there?" if nxt else "Is anything blocking you?"),
        Button(CONTINUE_SETUP, ctx.setup_url),
        _book(ctx, "Happy to do it with you, just", "."),
    ]


def _nudge3(ctx: EmailContext) -> tuple[str, list[Block]]:
    return "Quick question", [
        P("What does your team use for alerts and for hosting? Tell me and I'll send the exact steps for your setup."),
    ]


def _nudge7(ctx: EmailContext) -> tuple[str, list[Block]]:
    return "Should I stop emailing?", [
        P("Nothing is connected yet, so this is the last time I'll nudge you about it. "
          "If you come back to it, everything is where you left it."),
        Button(OPEN_SETUP, ctx.setup_url),
        _book(ctx, "A reply works too, or", "."),
        P("Thanks for trying Aurora."),
    ]


_KINDS = {"welcome": _welcome, "checkin": _checkin, "nudge_day3": _nudge3, "nudge_day7": _nudge7}


# ── Rendering ──

_INK = "#1d2126"
_MUTED = "#6b737c"
_FAINT = "#9aa1a9"
_TRACK = "#e7e9ec"
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
# The aurora, left to right across the four parts.
_PART_COLORS = {"alerts": "#22c97a", "cloud": "#17a8a6", "code": "#1a87cc", "chat": "#8a3fc0"}


def _e(s: str) -> str:
    return html.escape(s, quote=True)


def _text(blocks: list[Block]) -> list[str]:
    out = []
    for b in blocks:
        if isinstance(b, P):
            out.append(b.text.replace(b.link[0], f"{b.link[0]} ({b.link[1]})") if b.link else b.text)
        elif isinstance(b, Button):
            out.append(f"{b.label}: {b.url}")
        else:
            out.append("  ".join(f"[{'x' if p.id in b.done else ' '}] {p.short}" for p in STACK))
    return out


def _html_p(b: P) -> str:
    body = _e(b.text)
    if b.link:
        phrase, url = b.link
        body = body.replace(_e(phrase), f'<a href="{_e(url)}" style="color:{_INK};text-decoration:underline">{_e(phrase)}</a>', 1)
    return f'<p style="margin:0 0 20px;font-size:15px;line-height:1.65;color:{_INK}">{body}</p>'


def _html_button(b: Button) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:4px 0 26px"><tr>'
        '<td style="border-radius:8px;background:#111417">'
        f'<a href="{_e(b.url)}" style="display:inline-block;padding:12px 22px;font-family:{_FONT};font-size:14px;'
        f'font-weight:600;line-height:1;color:#ffffff;text-decoration:none;border-radius:8px">{_e(b.label)}</a>'
        "</td></tr></table>"
    )


def _html_stack(b: Stack) -> str:
    cells = []
    for i, p in enumerate(STACK):
        on = p.id in b.done
        bar = _PART_COLORS[p.id] if on else _TRACK
        pad = "0" if i == len(STACK) - 1 else "0 8px 0 0"
        name = f"&#10003;&nbsp;{_e(p.short)}" if on else _e(p.short)
        cells.append(
            f'<td width="25%" valign="top" style="padding:{pad}">'
            f'<div style="height:4px;line-height:4px;font-size:0;border-radius:2px;background:{bar}">&nbsp;</div>'
            f'<div style="padding-top:9px;font-family:{_FONT};font-size:12px;line-height:1.2;'
            f'color:{_INK if on else _FAINT};font-weight:{600 if on else 400}">{name}</div></td>'
        )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:2px 0 26px">'
        f'<tr>{"".join(cells)}</tr></table>'
    )


def _html_block(b: Block) -> str:
    if isinstance(b, P):
        return _html_p(b)
    if isinstance(b, Button):
        return _html_button(b)
    return _html_stack(b)


def _signature(guide: Guide, app_url: str) -> str:
    """Name and role beside a small square: the guide's photo if there is
    one, otherwise the Aurora icon. If images are blocked, the text stands alone."""
    role = f"{guide.title}, Aurora" if guide.title else "Aurora"
    if guide.avatar_url.startswith("https://"):
        img, radius = guide.avatar_url, 16
    elif app_url.startswith("https://"):
        img, radius = f"{app_url}/arvologo.png", 8
    else:
        img, radius = "", 0
    face = (
        f'<td width="32" style="width:32px;padding-right:12px;vertical-align:middle"><img src="{_e(img)}" width="32" height="32" '
        f'alt="Aurora" style="display:block;width:32px;height:32px;border-radius:{radius}px;border:0"></td>'
        if img else ""
    )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:6px 0 0"><tr>'
        f'{face}<td style="vertical-align:middle;font-family:{_FONT}">'
        f'<div style="font-size:15px;line-height:1.35;color:{_INK}">{_e(guide.name)}</div>'
        f'<div style="font-size:13px;line-height:1.35;color:{_MUTED}">{_e(role)}</div>'
        "</td></tr></table>"
    )


def _html(greeting: str, blocks: list[Block], guide: Guide, app_url: str, unsubscribe_url: str) -> str:
    body = "".join(_html_block(b) for b in blocks)
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta name="color-scheme" content="light only"><meta name="supported-color-schemes" content="light"></head>
<body style="margin:0;padding:0;background:#ffffff">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#ffffff"><tr><td align="center" style="padding:40px 20px 32px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:520px;font-family:{_FONT}">
<tr><td>
<p style="margin:0 0 20px;font-size:15px;line-height:1.65;color:{_INK}">{_e(greeting)}</p>
{body}
{_signature(guide, app_url)}
</td></tr>
<tr><td style="padding-top:36px"><div style="border-top:1px solid #eef0f2;padding-top:16px;font-size:12px;line-height:1.5;color:{_FAINT}">
You're getting this because you signed up for Aurora. <a href="{_e(unsubscribe_url)}" style="color:{_FAINT};text-decoration:underline">Stop setup emails</a>
</div></td></tr>
</table></td></tr></table></body></html>"""


def compose(kind: str, ctx: EmailContext, parts: tuple[str, ...] = ()) -> Composed:
    if kind == "connected":
        subject, blocks = _connected(ctx, parts)
    elif kind in _KINDS:
        subject, blocks = _KINDS[kind](ctx)
    else:
        raise ValueError(f"unknown onboarding email kind: {kind}")

    g = ctx.guide
    greeting = f"Hi {ctx.first_name}," if ctx.first_name else "Hi,"
    sign = f"{g.name}\n{g.title + ', ' if g.title else ''}Aurora"
    text = "\n\n".join([greeting, *_text(blocks), sign])
    text += f"\n\n--\nYou're getting this because you signed up for Aurora. Stop setup emails: {ctx.unsubscribe_url}\n"
    return Composed(subject=subject, text=text, html=_html(greeting, blocks, g, _app_url(ctx), ctx.unsubscribe_url))
