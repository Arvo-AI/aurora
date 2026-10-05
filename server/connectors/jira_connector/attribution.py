"""Attribution banner for anything Aurora writes into Jira.

Atlassian 3LO issues tokens *as the user*, so a comment Aurora posts shows the
Atlassian account that connected the integration as its author — there is no
bot principal to post as. The body is therefore the only place attribution can
live, and it has to be unmissable so a teammate doesn't read an Aurora RCA as
their colleague's own words.
"""

from __future__ import annotations

from typing import Any, Dict
import json

HEADER = "Aurora — automated root cause analysis"
FOOTER = (
    "Posted automatically by Aurora through the connected Atlassian account. "
    "The account shown as the author did not write this. Verify before acting."
)


def _paragraph(text: str, mark: str) -> Dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": text, "marks": [{"type": mark}]}],
    }


def _carries_banner(doc: Dict[str, Any]) -> bool:
    """True if this document already has the banner (a retried post)."""
    try:
        # ensure_ascii=False so the header's em dash isn't escaped past the match
        return HEADER in json.dumps(doc, ensure_ascii=False)
    except (TypeError, ValueError):
        return False


def attribute_adf(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Sandwich an ADF document between Aurora's attribution paragraphs.

    Idempotent, so a retry doesn't stack banners. Jira Data Center flattens
    this to plain text in ``JiraClient``, which keeps the wording.
    """
    if not isinstance(doc, dict) or doc.get("type") != "doc":
        return doc
    if _carries_banner(doc):
        return doc
    content = list(doc.get("content") or [])
    return {
        **doc,
        "content": [_paragraph(HEADER, "strong"), *content, _paragraph(FOOTER, "em")],
    }
