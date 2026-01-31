import json
import logging
from typing import Optional

from pydantic import BaseModel, Field

from connectors.confluence_connector.runbook_utils import fetch_confluence_runbook

logger = logging.getLogger(__name__)


class ConfluenceRunbookArgs(BaseModel):
    page_url: str = Field(description="Full Confluence page URL to parse")
    # user_id/session_id are injected by with_user_context


def confluence_runbook_parse(
    page_url: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch and clean a Confluence runbook into markdown + steps for LLM use."""
    _ = session_id
    if not user_id:
        raise ValueError("user_id is required to parse Confluence runbooks")

    try:
        result = fetch_confluence_runbook(page_url, user_id)
    except Exception as exc:
        logger.exception("Confluence runbook fetch failed for user %s: %s", user_id, exc)
        raise ValueError("Failed to fetch Confluence runbook; check connection and URL access") from exc
    if not result:
        raise ValueError("Failed to fetch Confluence runbook; check connection and URL access")

    page_payload = result.get("page") or {}
    response = {
        "status": "success",
        "title": page_payload.get("title"),
        "pageId": page_payload.get("id") or page_payload.get("pageId"),
        "pageUrl": page_url,
        "markdown": result.get("markdown"),
        "sections": result.get("sections") or {},
        "steps": result.get("steps") or [],
    }

    return json.dumps(response, ensure_ascii=False)
