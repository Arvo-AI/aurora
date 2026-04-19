"""Parse Notion URLs/IDs into typed references.

Supported shapes:
  - Raw UUID (with or without dashes).
  - ``https://www.notion.so/<workspace>/Some-Title-<32hex>``
  - ``https://www.notion.so/<workspace>/<32hex>?v=<view_uuid>`` (database view)
  - ``notion.so/<32hex>``
  - URLs with a ``#<block_id>`` fragment (returns a block_id reference).
"""

from __future__ import annotations

import logging
import re
from typing import Dict
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_HEX32_RE = re.compile(r"([0-9a-fA-F]{32})")
_UUID_DASHED_RE = re.compile(
    r"^([0-9a-fA-F]{8})-([0-9a-fA-F]{4})-([0-9a-fA-F]{4})-"
    r"([0-9a-fA-F]{4})-([0-9a-fA-F]{12})$"
)


def _to_dashed_uuid(raw: str) -> str:
    """Normalize a 32-char hex (or already dashed) UUID to dashed form."""
    if not raw:
        return raw
    raw = raw.strip()
    if _UUID_DASHED_RE.match(raw):
        return raw.lower()
    compact = raw.replace("-", "")
    if len(compact) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        c = compact.lower()
        return f"{c[0:8]}-{c[8:12]}-{c[12:16]}-{c[16:20]}-{c[20:32]}"
    return raw


def parse_notion_url(url_or_id: str) -> Dict[str, str]:
    """Return ``{"page_id": ...}``, ``{"database_id": ...}`` or ``{"block_id": ...}``."""
    if not url_or_id:
        raise ValueError("url_or_id is required")

    value = url_or_id.strip()

    # Raw UUID (dashed or 32-hex compact)
    if _UUID_DASHED_RE.match(value) or (
        len(value) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", value)
    ):
        return {"page_id": _to_dashed_uuid(value)}

    # Add scheme if missing so urlparse works consistently
    working = value
    if not re.match(r"^https?://", working):
        # Accept schemeless notion.so, www.notion.so, and workspace-hosted
        # custom domains like ``<team>.notion.site``.
        host_head = working.split("/", 1)[0].lower()
        if (
            host_head == "notion.so"
            or host_head == "www.notion.so"
            or host_head.endswith(".notion.so")
            or host_head.endswith(".notion.site")
        ):
            working = "https://" + working
        else:
            # Not a recognised Notion domain and no https:// scheme —
            # reject instead of blindly matching hex in arbitrary strings
            # (e.g. "evil.com/something/abc123def456…").
            raise ValueError(f"Unrecognised Notion URL or ID: {url_or_id!r}")

    parsed = urlparse(working)
    fragment = parsed.fragment or ""
    query = parsed.query or ""
    path = parsed.path or ""

    # Block anchor in fragment takes highest priority
    if fragment:
        frag_match = _HEX32_RE.search(fragment)
        if frag_match:
            return {"block_id": _to_dashed_uuid(frag_match.group(1))}

    # The last path segment usually ends in a 32-hex id
    segments = [seg for seg in path.split("/") if seg]
    last_id_match = None
    for seg in reversed(segments):
        m = _HEX32_RE.search(seg)
        if m:
            last_id_match = m.group(1)
            break

    if not last_id_match:
        raise ValueError(f"Could not extract Notion ID from URL: {url_or_id!r}")

    # Presence of ?v=<uuid> indicates a database view URL
    if "v=" in query:
        return {"database_id": _to_dashed_uuid(last_id_match)}

    return {"page_id": _to_dashed_uuid(last_id_match)}
