"""Agent tools + shared helpers for exporting Aurora postmortems to Notion."""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field

from connectors.notion_connector.client import NotionAuthExpiredError, NotionClient
from connectors.notion_connector.postmortem_parser import parse_action_items
from routes.audit_routes import record_audit_event
from utils.auth.stateless_auth import resolve_org_id
from utils.db.connection_pool import db_pool

from .common import build_rich_text, notion_tool_error, notion_tool_success

logger = logging.getLogger(__name__)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _looks_like_email(value: Optional[str]) -> bool:
    return bool(value and _EMAIL_RE.match(value.strip()))


def _looks_like_iso_date(value: Optional[str]) -> bool:
    return bool(value and _ISO_DATE_RE.match(value.strip()))


def _validate_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, TypeError):
        return False


def _find_property_by_type(
    db_schema: Dict[str, Any], target_type: str
) -> Optional[str]:
    """Return the first property key whose declared ``type`` matches."""
    props = (db_schema or {}).get("properties") or {}
    for key, meta in props.items():
        if isinstance(meta, dict) and meta.get("type") == target_type:
            return key
    return None


def _coerce_property_value(prop_meta: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    """Best-effort conversion of a raw value into a Notion property payload.

    Unknown/unsupported types return ``None`` so the caller can skip them.
    """
    prop_type = prop_meta.get("type") if isinstance(prop_meta, dict) else None
    if prop_type is None or value is None:
        return None

    try:
        if prop_type == "date":
            iso = value if isinstance(value, str) else str(value)
            return {"date": {"start": iso}}
        if prop_type == "rich_text":
            return {"rich_text": build_rich_text(str(value))}
        if prop_type == "title":
            return {"title": build_rich_text(str(value))}
        if prop_type == "select":
            return {"select": {"name": str(value)}}
        if prop_type == "multi_select":
            if isinstance(value, (list, tuple)):
                return {
                    "multi_select": [
                        {"name": str(v)} for v in value if v is not None
                    ]
                }
            return {"multi_select": [{"name": str(value)}]}
        if prop_type == "number":
            try:
                return {"number": float(value)}
            except (TypeError, ValueError):
                return None
        if prop_type == "checkbox":
            return {"checkbox": bool(value)}
        if prop_type == "url":
            return {"url": str(value)}
        if prop_type == "email":
            return {"email": str(value)}
        if prop_type == "phone_number":
            return {"phone_number": str(value)}
    except Exception as exc:
        logger.warning(
            "Failed to coerce property value for type %s: %s", prop_type, exc
        )
        return None

    logger.info(
        "Skipping unsupported Notion property type '%s' in mapping", prop_type
    )
    return None


def _merge_property_mapping(
    properties: Dict[str, Any],
    db_schema: Dict[str, Any],
    mapping: Dict[str, Any],
) -> None:
    """Mutate ``properties`` in place with coerced values from ``mapping``."""
    if not mapping or not isinstance(mapping, dict):
        return
    schema_props = (db_schema or {}).get("properties") or {}
    for key, raw_value in mapping.items():
        prop_meta = schema_props.get(key)
        if not isinstance(prop_meta, dict):
            logger.info(
                "Skipping mapping key '%s' — not present in target Notion DB", key
            )
            continue
        payload = _coerce_property_value(prop_meta, raw_value)
        if payload is not None:
            properties[key] = payload


def _fetch_postmortem(
    user_id: str, org_id: str, incident_id: str
) -> Optional[Dict[str, Any]]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET myapp.current_user_id = %s", (user_id,))
            cursor.execute("SET myapp.current_org_id = %s", (org_id,))
            conn.commit()
            cursor.execute(
                """SELECT id, content FROM postmortems
                   WHERE incident_id = %s AND org_id = %s""",
                (incident_id, org_id),
            )
            row = cursor.fetchone()
    if not row:
        return None
    return {"id": row[0], "content": row[1]}


def _update_postmortem_notion_metadata(
    user_id: str,
    org_id: str,
    postmortem_id: Any,
    *,
    page_id: str,
    page_url: Optional[str],
    database_id: str,
) -> None:
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_user_id = %s", (user_id,))
                cursor.execute("SET myapp.current_org_id = %s", (org_id,))
                conn.commit()
                cursor.execute(
                    """UPDATE postmortems
                       SET notion_page_id = %s,
                           notion_page_url = %s,
                           notion_exported_at = CURRENT_TIMESTAMP,
                           notion_database_id = %s
                       WHERE id = %s AND org_id = %s""",
                    (
                        str(page_id),
                        page_url,
                        str(database_id),
                        str(postmortem_id),
                        org_id,
                    ),
                )
                conn.commit()
    except Exception as exc:
        logger.warning(
            "[NOTION] Failed to update notion metadata for postmortem %s: %s",
            postmortem_id,
            exc,
        )


def _create_action_items(
    client: NotionClient,
    postmortem_md: str,
    action_items_database_id: str,
) -> int:
    """Create one Notion page per unchecked action item in the target DB.

    Returns the number of pages successfully created.
    """
    items = parse_action_items(postmortem_md or "")
    if not items:
        return 0

    try:
        db_schema = client.get_database(action_items_database_id)
    except Exception as exc:
        logger.warning(
            "[NOTION] Failed to fetch action-items DB schema %s: %s",
            action_items_database_id,
            exc,
        )
        return 0

    title_key = _find_property_by_type(db_schema, "title")
    if not title_key:
        logger.warning(
            "[NOTION] Action-items DB %s has no title property",
            action_items_database_id,
        )
        return 0

    people_key = _find_property_by_type(db_schema, "people")
    date_key = _find_property_by_type(db_schema, "date")

    # Prime the client's email→user cache once so `find_user_by_email` below
    # is a dict lookup instead of a fresh /users pagination per item.
    if people_key and any(_looks_like_email(i.get("assignee_hint")) for i in items):
        try:
            client.prime_user_cache()
        except Exception as exc:
            logger.info("[NOTION] Workspace user prefetch failed: %s", exc)

    created = 0
    for item in items:
        if item.get("checked"):
            continue
        text = (item.get("text") or "").strip()
        if not text:
            continue

        properties: Dict[str, Any] = {
            title_key: {"title": build_rich_text(text)}
        }

        assignee_hint = item.get("assignee_hint")
        if people_key and _looks_like_email(assignee_hint):
            try:
                user = client.find_user_by_email(assignee_hint.strip())
                if user and user.get("id"):
                    properties[people_key] = {
                        "people": [{"id": user["id"]}]
                    }
            except Exception as exc:
                logger.info(
                    "[NOTION] Assignee lookup failed for '%s': %s",
                    assignee_hint,
                    exc,
                )

        due_hint = item.get("due_hint")
        if date_key and _looks_like_iso_date(due_hint):
            properties[date_key] = {"date": {"start": due_hint.strip()}}

        try:
            client.create_page(
                parent={"database_id": action_items_database_id},
                properties=properties,
            )
            created += 1
        except Exception as exc:
            logger.warning(
                "[NOTION] Failed to create action-item page for '%s': %s",
                text[:80],
                exc,
            )
    return created


def _export_postmortem_to_notion(
    user_id: str,
    incident_id: str,
    database_id: str,
    *,
    title_property: Optional[str] = None,
    property_mapping: Optional[Dict[str, Any]] = None,
    action_items_database_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a Notion page from a postmortem and persist the link back.

    Raises:
        ValueError: on invalid input or missing postmortem content.
        NotionAuthExpiredError: when stored Notion credentials cannot refresh.
    """
    if not _validate_uuid(incident_id):
        raise ValueError("Invalid incident ID")
    if not database_id:
        raise ValueError("database_id is required")

    org_id = resolve_org_id(user_id)
    if not org_id:
        raise ValueError("Could not resolve org for user")

    postmortem = _fetch_postmortem(user_id, org_id, incident_id)
    if not postmortem:
        raise ValueError("Postmortem not found")
    content = postmortem.get("content")
    if not content:
        raise ValueError("Postmortem has no content to export")

    client = NotionClient(user_id)

    db_schema = client.get_database(database_id)
    if title_property:
        title_key = title_property
    else:
        title_key = _find_property_by_type(db_schema, "title")
        if not title_key:
            raise ValueError(
                f"Notion database {database_id} has no title property"
            )

    page_title = f"Postmortem – Incident {incident_id[:8]}"
    properties: Dict[str, Any] = {
        title_key: {"title": build_rich_text(page_title)}
    }

    if property_mapping:
        _merge_property_mapping(properties, db_schema, property_mapping)

    page = client.create_page(
        parent={"database_id": database_id}, properties=properties
    )
    page_id = page.get("id")
    page_url = page.get("url")
    if not page_id:
        raise ValueError("Notion create_page returned no page id")

    try:
        client.update_page_markdown(page_id, content, mode="replace")
    except Exception as exc:
        logger.warning(
            "[NOTION] update_page_markdown failed for page %s: %s",
            page_id,
            exc,
        )

    _update_postmortem_notion_metadata(
        user_id,
        org_id,
        postmortem["id"],
        page_id=page_id,
        page_url=page_url,
        database_id=database_id,
    )

    action_item_count = 0
    if action_items_database_id:
        try:
            action_item_count = _create_action_items(
                client,
                content,
                action_items_database_id,
            )
        except Exception as exc:
            logger.warning(
                "[NOTION] Action-item creation failed for incident %s: %s",
                incident_id,
                exc,
            )

    # Best-effort audit log — never fail the export if audit write breaks.
    try:
        record_audit_event(
            org_id,
            user_id,
            "export_postmortem_notion",
            "postmortem",
            incident_id,
            {
                "page_url": page_url,
                "page_id": page_id,
                "database_id": database_id,
                "action_item_count": action_item_count,
            },
            None,
        )
    except Exception as exc:
        logger.warning("[NOTION] Audit event failed (non-fatal): %s", exc)

    return {
        "success": True,
        "pageId": page_id,
        "pageUrl": page_url,
        "actionItemCount": action_item_count,
    }


class NotionExportPostmortemArgs(BaseModel):
    incident_id: str = Field(
        description="Incident UUID whose postmortem to export"
    )
    database_id: str = Field(
        description="Target Notion database ID (where the postmortem page is created)"
    )
    action_items_database_id: Optional[str] = Field(
        default=None,
        description="Optional Notion database ID for action-item rows",
    )


class NotionCreateActionItemsArgs(BaseModel):
    incident_id: str = Field(
        description="Incident UUID whose postmortem has the Action Items section"
    )
    action_items_database_id: str = Field(
        description="Target Notion database for the action-item rows"
    )
    assignee_hints: Optional[Dict[str, str]] = Field(
        default=None,
        description="Optional override: map {item_text_prefix: user_email} to force assignees",
    )


def notion_export_postmortem(
    incident_id: str,
    database_id: str,
    action_items_database_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Export an incident's postmortem to a Notion database."""
    _ = session_id
    if not user_id:
        return notion_tool_error("user_id is required", code="missing_user")

    try:
        result = _export_postmortem_to_notion(
            user_id=user_id,
            incident_id=incident_id,
            database_id=database_id,
            action_items_database_id=action_items_database_id,
        )
        return notion_tool_success(result)
    except NotionAuthExpiredError:
        return notion_tool_error(
            "Notion credentials expired — reconnect and retry.",
            code="reauth_required",
        )
    except ValueError as exc:
        return notion_tool_error(str(exc), code="bad_input")
    except Exception as exc:
        logger.exception("Postmortem export to Notion failed: %s", exc)
        return notion_tool_error(f"Export failed: {exc}", code="tool_failure")


def _apply_assignee_overrides(
    content: str, overrides: Optional[Dict[str, str]]
) -> str:
    """Inject ``(owner: email)`` hints into action-item lines that match a prefix.

    The parser picks up ``owner: ...`` as the assignee_hint so this is a cheap
    way to force assignees without re-implementing the parser.
    """
    if not overrides or not content:
        return content
    lines = content.splitlines()
    checkbox_re = re.compile(r"^(\s*[-*]\s+\[( |x|X)\]\s+)(.*)$")
    for i, line in enumerate(lines):
        m = checkbox_re.match(line)
        if not m:
            continue
        prefix, _, body = m.group(1), m.group(2), m.group(3)
        body_lower = body.lower()
        for key, email in overrides.items():
            if not key or not email:
                continue
            if body_lower.startswith(key.lower()):
                if "owner:" in body_lower:
                    break
                body = f"{body.rstrip()} (owner: {email})"
                lines[i] = f"{prefix}{body}"
                break
    return "\n".join(lines)


def notion_create_action_items(
    incident_id: str,
    action_items_database_id: str,
    assignee_hints: Optional[Dict[str, str]] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Create Notion action-item pages from a postmortem's Action Items section."""
    _ = session_id
    if not user_id:
        return notion_tool_error("user_id is required", code="missing_user")
    if not _validate_uuid(incident_id):
        return notion_tool_error("Invalid incident ID", code="bad_input")
    if not action_items_database_id:
        return notion_tool_error(
            "action_items_database_id is required", code="bad_input"
        )

    try:
        org_id = resolve_org_id(user_id)
        if not org_id:
            return notion_tool_error(
                "Could not resolve org for user", code="bad_input"
            )

        postmortem = _fetch_postmortem(user_id, org_id, incident_id)
        if not postmortem:
            return notion_tool_error(
                "Postmortem not found", code="bad_input"
            )
        content = postmortem.get("content") or ""
        if not content:
            return notion_tool_error(
                "Postmortem has no content to parse", code="bad_input"
            )

        content = _apply_assignee_overrides(content, assignee_hints)

        client = NotionClient(user_id)
        count = _create_action_items(
            client,
            content,
            action_items_database_id,
        )
        return notion_tool_success({"actionItemCount": count})
    except NotionAuthExpiredError:
        return notion_tool_error(
            "Notion credentials expired — reconnect and retry.",
            code="reauth_required",
        )
    except ValueError as exc:
        return notion_tool_error(str(exc), code="bad_input")
    except Exception as exc:
        logger.exception("Notion action-item creation failed: %s", exc)
        return notion_tool_error(
            f"Action-item creation failed: {exc}", code="tool_failure"
        )
