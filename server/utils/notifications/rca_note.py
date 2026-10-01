"""
Turn an RCA report (incidents.aurora_summary) into the short plain-text note
Aurora posts back onto the paging system's incident: the root-cause paragraph,
the impact paragraph when the report has one, a link to the full investigation
and a disclaimer. Shared by the PagerDuty note and incident.io update services.
"""

import re
from typing import Optional, Tuple

NOTE_MAX_CHARS = 700
ROOT_CAUSE_MAX_CHARS = 1200
IMPACT_MAX_CHARS = 500
MIN_SUMMARY_CHARS = 80

_CITATION_RE = re.compile(r"\[\d+(?:,\s*\d+)*\]")
# The space a removed citation leaves before punctuation ("spiked [2].": "spiked .")
_SPACE_BEFORE_PUNCT_RE = re.compile(r" ([.,;:!?])(?=\s|$)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*((?:[^*\n]|\*(?!\*))+)\*\*|__([^_\n]+)__")
# A star glued to a word char is not emphasis (p95*2, 3*4 nodes); only a delimiter-bounded pair is
_STAR_ITALIC_RE = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+)(?<!\s)\*(?![\w*])")
_UNDERSCORE_ITALIC_RE = re.compile(r"(?<!\w)_(?!\s)([^_\n]+)(?<!\s)_(?!\w)")
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.MULTILINE)
_BULLET_RE = re.compile(r"^[ \t]*[*+][ \t]+", re.MULTILINE)
_RULE_RE = re.compile(r"^[-*_]{3,}$")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s")
# Section headings. The summarizer is asked for three paragraphs (what happened,
# root cause, impact & timeline) followed by "## Ruled Out" / "## Not Checked";
# models render the paragraph labels as "## X", "**X**" or bare "X" lines, or not at all.
_MD_HEADING_RE = re.compile(r"^#{1,6}[ \t]+(.+?)[ \t#]*$")
_BOLD_LINE_RE = re.compile(r"^(?:\*\*|__)([^*_.|]{1,80}?)(?:\*\*|__):?$")
_KNOWN_TITLE_RE = re.compile(
    r"^(?:summary|what happened|root cause|impact|timeline|incident report|ruled out|not checked|"
    r"suggested next steps|next steps|recommendations|action items|proposed actions|remediation steps)\b",
    re.IGNORECASE,
)
_END_SECTION_RE = re.compile(
    r"^(?:ruled out|not checked|suggested next steps|next steps|recommendations|action items|"
    r"proposed actions|remediation steps)\b",
    re.IGNORECASE,
)
_ROOT_HEADING_RE = re.compile(r"root cause", re.IGNORECASE)
_IMPACT_HEADING_RE = re.compile(r"impact|timeline", re.IGNORECASE)
# Opening phrases the summarizer is told to use for the root-cause paragraph
_ROOT_CAUSE_RE = re.compile(r"^(?:the )?(?:root cause|most likely cause)\b|^evidence suggests\b", re.IGNORECASE)
MIN_PROSE_CHARS = 40
# The summarizer sometimes writes the section label inline ("Root Cause: the pool..."); the note
# adds its own label, so the inline one is dropped once the paragraph has been identified. Only a
# label followed by a separator matches: "Root cause undetermined ..." is prose and stays.
_INLINE_LABEL_RE = re.compile(
    r"^(?:summary|what happened|root cause|most likely cause|impact(?:\s*(?:&|and)\s*timeline)?|timeline)\s*[:\u2014\u2013-]\s+",
    re.IGNORECASE,
)


def truncate(text: str, limit: int = NOTE_MAX_CHARS) -> str:
    """Cut at the last space before limit; hard cut if that space is too early."""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    if cut < 200:
        cut = limit
    return text[:cut].rstrip() + "..."


def to_plain_text(text: str, max_chars: Optional[int] = NOTE_MAX_CHARS) -> str:
    """Strip markdown/citations to plain text; paragraphs stay separated by one blank line."""
    if not text:
        return ""
    text = text.replace("```", "").replace("`", "")
    text = _LINK_RE.sub(r"\1 (\2)", text)
    text = _CITATION_RE.sub("", text)
    text = _HEADING_RE.sub("", text)
    text = _BULLET_RE.sub("- ", text)
    text = _BOLD_RE.sub(lambda m: m.group(1) or m.group(2), text)
    text = _STAR_ITALIC_RE.sub(r"\1", text)
    text = _UNDERSCORE_ITALIC_RE.sub(r"\1", text)
    paragraphs = [_SPACE_BEFORE_PUNCT_RE.sub(r"\1", " ".join(p.split())) for p in re.split(r"\n\s*\n", text)]
    text = "\n\n".join(p for p in paragraphs if p)
    return truncate(text, max_chars) if max_chars else text


def heading_text(line: str) -> Optional[str]:
    """Lower-case title if the line is a section heading ("## X", "**X**", or a bare known title)."""
    m = _MD_HEADING_RE.match(line)
    if m:
        return m.group(1).strip("*_ :").lower()
    m = _BOLD_LINE_RE.match(line)
    if m:
        return m.group(1).strip(" :").lower()
    if len(line) <= 60 and "." not in line and _KNOWN_TITLE_RE.match(line):
        return line.strip(" :").lower()
    return None


def sections(summary: str) -> list:
    """[(heading, [prose paragraphs])] in document order; the first entry has heading "".

    Line-based so a heading directly followed by its paragraph (no blank line)
    still splits. Rules, list items, pipe-metadata lines and stubs shorter than
    MIN_PROSE_CHARS are dropped.
    """
    sections = [["", []]]
    buffer: list = []

    def flush() -> None:
        if not buffer:
            return
        first = buffer[0]
        raw = " ".join(buffer)
        buffer.clear()
        if _LIST_ITEM_RE.match(first) or raw.count(" | ") >= 2:
            return
        text = to_plain_text(raw, max_chars=None)
        if len(text) >= MIN_PROSE_CHARS:
            sections[-1][1].append(text)

    for line in summary.splitlines():
        line = line.strip()
        if not line or _RULE_RE.match(line):
            flush()
            continue
        heading = heading_text(line)
        if heading is not None:
            flush()
            sections.append([heading, []])
            continue
        buffer.append(line)
    flush()
    return [(heading, paragraphs) for heading, paragraphs in sections]


def extract_note_body(summary: Optional[str]) -> Tuple[str, str]:
    """(root cause, impact) paragraphs as plain text; impact may be "".

    Headed reports are read by section title. Unheaded ones fall back to the
    summarizer's paragraph order: the root-cause paragraph is the one opening
    with its prescribed phrase (else the 2nd), and impact is the paragraph
    after it. Nothing after "Ruled Out" / "Not Checked" / next-steps is used.
    """
    root: Optional[str] = None
    impact: Optional[str] = None
    narrative: list = []
    for heading, paragraphs in sections(summary or ""):
        if _END_SECTION_RE.match(heading):
            break
        if root is None and _ROOT_HEADING_RE.search(heading):
            root = paragraphs[0] if paragraphs else None
        elif impact is None and _IMPACT_HEADING_RE.search(heading):
            impact = paragraphs[0] if paragraphs else None
        else:
            narrative.extend(paragraphs)

    if root is None and narrative:
        index = next((i for i, p in enumerate(narrative) if _ROOT_CAUSE_RE.match(p)), None)
        if index is None:
            index = 1 if len(narrative) >= 2 else 0
        root = narrative[index]
        if impact is None and index + 1 < len(narrative):
            impact = narrative[index + 1]

    # Identification above relies on the opening phrase; only now drop an inline label.
    root = _INLINE_LABEL_RE.sub("", root or "")
    impact = _INLINE_LABEL_RE.sub("", impact or "")
    return truncate(root, ROOT_CAUSE_MAX_CHARS), truncate(impact, IMPACT_MAX_CHARS)


def _note_sections(root_cause: str, impact: str) -> list:
    sections = [("Root cause", root_cause)]
    if impact:
        sections.append(("Impact", impact))
    return sections


def _investigation_url(incident_id: str, base_url: Optional[str]) -> str:
    base_url = (base_url or "").rstrip("/")
    return f"{base_url}/incidents/{incident_id}" if base_url else ""


def compose_note(root_cause: str, incident_id: str, impact: str = "", base_url: Optional[str] = None) -> str:
    """Plain-text note body (PagerDuty notes render no markup); base_url links the full investigation."""
    lines = ["Aurora RCA", ""]
    for label, body in _note_sections(root_cause, impact):
        lines += [label, body, ""]
    url = _investigation_url(incident_id, base_url)
    if url:
        lines += [f"Full investigation: {url}", ""]
    lines.append("Generated automatically by Aurora. Verify before acting.")
    return "\n".join(lines)


# A bare asterisk opens emphasis in markdown ("p95*2 nodes and 3*4 workers" italicises the
# middle); the look-alike operator renders the same and is inert. Backslash escapes are shown
# literally by incident.io, so they cannot be used; underscores inside identifiers render as-is.
_MD_INERT_STAR = "\u2217"


def _markdown_safe(text: str) -> str:
    return text.replace("*", _MD_INERT_STAR)


def compose_note_markdown(root_cause: str, incident_id: str, impact: str = "", base_url: Optional[str] = None) -> str:
    """Markdown note body for renderers that understand it (incident.io incident updates).

    Blocks are separated by blank lines because single newlines collapse into one paragraph.
    """
    lines = ["**Aurora RCA**", ""]
    for label, body in _note_sections(root_cause, impact):
        lines += [f"**{label}**", "", _markdown_safe(body), ""]
    url = _investigation_url(incident_id, base_url)
    if url:
        lines += [f"- [Open the full investigation]({url})", ""]
    lines.append("_Generated automatically by Aurora. Verify before acting._")
    return "\n".join(lines)
