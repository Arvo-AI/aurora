"""Shared helpers for Notion chat tools."""

import json
import logging
from typing import Any, Callable, Dict, Optional

from connectors.notion_connector.client import (
    NotionAuthExpiredError,
    NotionClient,
    extract_title,
    rich_text_to_plain,
)
from utils.auth.token_management import get_token_data

__all__ = [
    "NotionClient",
    "NotionAuthExpiredError",
    "build_icon",
    "build_cover",
    "build_rich_text",
    "build_title_property",
    "extract_title",
    "is_notion_connected",
    "notion_tool_error",
    "notion_tool_success",
    "rich_text_to_plain",
    "run_notion_tool",
    "wrap_optional_feature",
]

# Notion caps each rich_text segment at ~2000 characters.
_RICH_TEXT_CONTENT_CAP = 2000

logger = logging.getLogger(__name__)


def is_notion_connected(user_id: Optional[str]) -> bool:
    """Cheap DB check — does this user have stored Notion creds?"""
    if not user_id:
        return False
    try:
        return bool(get_token_data(user_id, "notion"))
    except Exception:
        return False


def notion_tool_error(
    message: str,
    code: str = "error",
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    payload: Dict[str, Any] = {"status": "error", "code": code, "error": message}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def notion_tool_success(data: Dict[str, Any]) -> str:
    return json.dumps({"status": "success", **data}, ensure_ascii=False)


def run_notion_tool(
    user_id: Optional[str], fn: Callable[[NotionClient], Dict[str, Any]]
) -> str:
    """Boilerplate: validate user, build client, run fn, translate errors to JSON."""
    if not user_id:
        return notion_tool_error("user_id is required", code="missing_user")
    try:
        client = NotionClient(user_id)
    except ValueError:
        return notion_tool_error(
            "Notion is not connected for this user — ask the user to connect at /notion/connect.",
            code="not_connected",
        )
    try:
        return notion_tool_success(fn(client))
    except NotionAuthExpiredError:
        return notion_tool_error(
            "Notion credentials expired. Ask the user to reconnect Notion, then retry.",
            code="reauth_required",
        )
    except Exception as exc:
        logger.exception("Notion tool failed for user %s: %s", user_id, exc)
        return notion_tool_error(
            f"Notion operation failed: {exc}", code="tool_failure"
        )


def wrap_optional_feature(raw: Any, unsupported_marker: str) -> Dict[str, Any]:
    """Shape Notion responses that may 404 on plan-gated features.

    If ``NotionClient`` surfaced the 404 as ``{"error": unsupported_marker}``,
    returns ``{supported: False, error: ...}``. Otherwise wraps the result with
    ``supported: True``.
    """
    if isinstance(raw, dict) and raw.get("error") == unsupported_marker:
        return {"supported": False, "error": unsupported_marker}
    if isinstance(raw, dict):
        return {"supported": True, **raw}
    return {"supported": True, "result": raw}


def build_rich_text(text: str, *, cap: int = _RICH_TEXT_CONTENT_CAP) -> list[Dict[str, Any]]:
    """Build a single-segment Notion rich_text array, capped to Notion's limit."""
    content = (text or "")[:cap]
    return [{"type": "text", "text": {"content": content}}]


def build_title_property(title: str, for_database: bool = False) -> Dict[str, Any]:
    """Build a Notion title property payload.

    For page-parented pages, Notion expects key "title".
    For database-parented pages, most DBs use "Name" (the default title property key).
    """
    key = "Name" if for_database else "title"
    return {key: {"title": build_rich_text(title or "Untitled")}}


def build_icon(icon: Optional[Any]) -> Optional[Dict[str, Any]]:
    """Accept either an emoji string, a URL string, or an already-built dict."""
    if icon is None:
        return None
    if isinstance(icon, dict):
        return icon
    if not isinstance(icon, str) or not icon.strip():
        return None
    value = icon.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return {"type": "external", "external": {"url": value}}
    return {"type": "emoji", "emoji": value}


def build_cover(cover: Optional[Any]) -> Optional[Dict[str, Any]]:
    if cover is None:
        return None
    if isinstance(cover, dict):
        return cover
    if not isinstance(cover, str) or not cover.strip():
        return None
    return {"type": "external", "external": {"url": cover.strip()}}
