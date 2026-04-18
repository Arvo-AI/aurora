"""Notion workspace operations: users, comments, emojis, file uploads."""

from __future__ import annotations

import ipaddress
import logging
import mimetypes
import os
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests
from pydantic import BaseModel, Field

from .common import (
    build_rich_text,
    notion_tool_error,
    rich_text_to_plain,
    run_notion_tool,
    wrap_optional_feature,
)

logger = logging.getLogger(__name__)


# ===========================================================================
# Users / people
#
# NOTE: The parameter name ``user_id`` is reserved by the chat framework
# (``with_user_context`` injects the Aurora caller). The Notion target user
# is therefore exposed as ``target_user_id`` to the LLM.
# ===========================================================================


class NotionListUsersArgs(BaseModel):
    max_results: int = Field(
        default=100, ge=1, le=100, description="Page size (1-100)."
    )
    start_cursor: Optional[str] = Field(
        default=None, description="Pagination cursor."
    )


class NotionGetUserArgs(BaseModel):
    target_user_id: str = Field(
        description="Notion user ID to fetch (the user you want details for)."
    )


class NotionGetSelfArgs(BaseModel):
    pass


class NotionFindPersonArgs(BaseModel):
    name_or_email: str = Field(
        description="Email (exact match) or name substring (case-insensitive) of a Notion user."
    )


class NotionListTeamspacesArgs(BaseModel):
    pass


def _shape_user(u: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not u:
        return None
    person = u.get("person") or {}
    bot = u.get("bot") or {}
    return {
        "id": u.get("id"),
        "type": u.get("type"),
        "name": u.get("name"),
        "avatar_url": u.get("avatar_url"),
        "email": person.get("email") if u.get("type") == "person" else None,
        "workspace_name": bot.get("workspace_name") if u.get("type") == "bot" else None,
    }


def notion_list_users(
    max_results: int = 100,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List Notion workspace users (people + bots)."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        page = client.list_users(page_size=max_results, start_cursor=start_cursor)
        users = [_shape_user(u) for u in page.get("results") or []]
        return {
            "count": len(users),
            "users": users,
            "has_more": page.get("has_more", False),
            "next_cursor": page.get("next_cursor"),
        }

    return run_notion_tool(user_id, _do)


def notion_get_user(
    target_user_id: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Fetch a single Notion user by ID."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        return _shape_user(client.get_user(target_user_id)) or {}

    return run_notion_tool(user_id, _do)


def notion_get_self(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Get the Notion bot user (self) for the current integration."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        return _shape_user(client.get_self()) or {}

    return run_notion_tool(user_id, _do)


def notion_find_person(
    name_or_email: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Find a Notion person by email (exact) or name (case-insensitive substring)."""
    _ = session_id

    needle = (name_or_email or "").strip()

    def _do(client: Any) -> Dict[str, Any]:
        if not needle:
            return {"found": False, "user": None}

        if "@" in needle:
            hit = client.find_user_by_email(needle)
            return {"found": bool(hit), "user": _shape_user(hit)}

        lower = needle.lower()
        cursor: Optional[str] = None
        max_pages = 20
        matches: List[Dict[str, Any]] = []
        for _ in range(max_pages):
            page = client.list_users(start_cursor=cursor)
            for u in page.get("results") or []:
                name = (u.get("name") or "").lower()
                if lower in name:
                    shaped = _shape_user(u)
                    if shaped:
                        matches.append(shaped)
            if not page.get("has_more"):
                break
            cursor = page.get("next_cursor")
            if not cursor:
                break
        return {"found": bool(matches), "count": len(matches), "users": matches}

    return run_notion_tool(user_id, _do)


def notion_list_teamspaces(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Best-effort teamspace listing via workspace search. Empty on Free plan."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        raw = client.search(query="", filter_types=["team_space"])
        results = []
        for item in raw.get("results") or []:
            if item.get("object") != "team_space":
                continue
            results.append(
                {
                    "id": item.get("id"),
                    "object": item.get("object"),
                    "name": item.get("name") or "",
                }
            )
        return {
            "count": len(results),
            "teamspaces": results,
            "note": "Notion has no dedicated teamspace API — results may be empty on Free/Plus plans.",
        }

    return run_notion_tool(user_id, _do)


# ===========================================================================
# Comments
# ===========================================================================


class NotionCreateCommentArgs(BaseModel):
    text: str = Field(description="Plain-text comment body.")
    page_id: Optional[str] = Field(
        default=None,
        description="Parent page ID (exactly one of page_id / block_id / discussion_id must be set).",
    )
    block_id: Optional[str] = Field(
        default=None,
        description="Parent block ID (e.g. for inline comments on a specific block).",
    )
    discussion_id: Optional[str] = Field(
        default=None,
        description="Discussion ID to reply in (for threaded comments).",
    )


class NotionGetCommentsArgs(BaseModel):
    page_id_or_block_id: str = Field(
        description="ID of the page or block whose comments to list."
    )
    include_resolved: bool = Field(
        default=False,
        description="If true, include resolved discussions; otherwise open comments only.",
    )


def notion_create_comment(
    text: str,
    page_id: Optional[str] = None,
    block_id: Optional[str] = None,
    discussion_id: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Add a new comment on a Notion page, block, or existing discussion."""
    _ = session_id

    if not text or not text.strip():
        return notion_tool_error("text is required", code="bad_input")
    if sum(bool(x) for x in (page_id, block_id, discussion_id)) != 1:
        return notion_tool_error(
            "exactly one of page_id / block_id / discussion_id must be set",
            code="bad_input",
        )

    rich_text = build_rich_text(text)

    def _do(client: Any) -> Dict[str, Any]:
        parent: Optional[Dict[str, Any]] = None
        did = discussion_id
        if page_id:
            parent = {"page_id": page_id}
        elif block_id:
            parent = {"block_id": block_id}
        result = client.create_comment(
            parent=parent, discussion_id=did, rich_text=rich_text
        )
        return {
            "id": result.get("id"),
            "discussion_id": result.get("discussion_id"),
            "created_time": result.get("created_time"),
        }

    return run_notion_tool(user_id, _do)


def notion_get_comments(
    page_id_or_block_id: str,
    include_resolved: bool = False,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List comments on a Notion page or block."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        raw = client.list_comments(page_id_or_block_id)
        comments: List[Dict[str, Any]] = []
        for c in raw.get("results") or []:
            resolved = bool(c.get("resolved_time"))
            if resolved and not include_resolved:
                continue
            author = (c.get("created_by") or {}).get("id")
            comments.append(
                {
                    "id": c.get("id"),
                    "discussion_id": c.get("discussion_id"),
                    "author_id": author,
                    "text": rich_text_to_plain(c.get("rich_text")),
                    "created_time": c.get("created_time"),
                    "resolved": resolved,
                }
            )
        return {"count": len(comments), "comments": comments}

    return run_notion_tool(user_id, _do)


# ===========================================================================
# Emojis
# ===========================================================================


class NotionListCustomEmojisArgs(BaseModel):
    pass


def notion_list_custom_emojis(
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List the workspace's custom emojis. Returns supported=False if the API is unavailable."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        shaped = wrap_optional_feature(client.list_emojis(), "emojis_not_available")
        if not shaped.get("supported"):
            return {**shaped, "emojis": []}
        emojis = [
            {
                "id": e.get("id"),
                "name": e.get("name"),
                "url": e.get("url") or (e.get("file") or {}).get("url"),
            }
            for e in shaped.get("results") or []
        ]
        return {"supported": True, "count": len(emojis), "emojis": emojis}

    return run_notion_tool(user_id, _do)


# ===========================================================================
# File uploads
# ===========================================================================

_SINGLE_PART_LIMIT = 20 * 1024 * 1024  # 20 MB
_MULTI_PART_CHUNK = 10 * 1024 * 1024  # 10 MB per part
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MB hard cap


class NotionUploadFileArgs(BaseModel):
    file_path_or_url: str = Field(
        description="Publicly reachable http(s) URL to the file."
    )
    filename: Optional[str] = Field(
        default=None,
        description="Override the filename attached to the upload (defaults to the source's basename).",
    )
    content_type: Optional[str] = Field(
        default=None,
        description="Override the MIME type (defaults to auto-detect from the filename).",
    )


class NotionListFileUploadsArgs(BaseModel):
    max_results: int = Field(default=25, ge=1, le=100, description="Page size (1-100).")
    start_cursor: Optional[str] = Field(default=None, description="Pagination cursor.")


def _looks_like_url(s: str) -> bool:
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False


def _guess_filename(source: str) -> str:
    if _looks_like_url(source):
        path = urlparse(source).path or "upload.bin"
        return os.path.basename(path) or "upload.bin"
    return os.path.basename(source) or "upload.bin"


def _guess_content_type(filename: str) -> str:
    mt, _ = mimetypes.guess_type(filename)
    return mt or "application/octet-stream"


def _assert_public_url(url: str) -> None:
    """Reject URLs whose host resolves to loopback/private/link-local IPs.

    The file-upload tool fetches arbitrary URLs server-side on behalf of a
    chat user, so without this check it's an SSRF vector into the Aurora
    deployment's internal network (e.g. cloud metadata endpoints at
    ``169.254.169.254``).
    """
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise ValueError(f"URL has no hostname: {url!r}")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed for {host!r}: {exc}") from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Refusing to fetch URL pointing at non-public address {addr}"
            )


def _fetch_bytes(source: str) -> tuple[bytes, str]:
    """Return (bytes, effective_filename) for a URL or local path."""
    if _looks_like_url(source):
        # SSRF guard: reject URLs that resolve to internal/private ranges.
        _assert_public_url(source)
        # Best-effort HEAD: many CDNs reject HEAD, so only trust the
        # Content-Length on a 2xx response. Streaming cap below is the
        # authoritative guard. ``allow_redirects=False`` prevents a 3xx
        # away from the DNS-validated host to an internal one.
        try:
            head = requests.head(source, allow_redirects=False, timeout=30)
            if head.ok:
                length_header = head.headers.get("Content-Length")
                if length_header and length_header.isdigit():
                    if int(length_header) > _MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            f"File exceeds 500 MB cap ({int(length_header)} bytes)"
                        )
        except requests.RequestException as exc:
            # HEAD is advisory; the streaming cap below is the real guard.
            logger.debug("HEAD pre-check failed for %s: %s", source, exc)
        resp = requests.get(
            source, stream=True, timeout=120, allow_redirects=False
        )
        resp.raise_for_status()
        buf = bytearray()
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if not chunk:
                continue
            buf.extend(chunk)
            if len(buf) > _MAX_DOWNLOAD_BYTES:
                raise ValueError("Download exceeded 500 MB cap during streaming")
        return bytes(buf), _guess_filename(source)

    raise ValueError(
        "Only public http(s) URLs are accepted — local file paths are not allowed."
    )


def notion_upload_file(
    file_path_or_url: str,
    filename: Optional[str] = None,
    content_type: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Upload a file to Notion via the file_uploads API (single- or multi-part)."""
    _ = session_id

    if not file_path_or_url or not file_path_or_url.strip():
        return notion_tool_error("file_path_or_url is required", code="bad_input")

    src_url = file_path_or_url.strip()

    def _do(client: Any) -> Dict[str, Any]:
        try:
            data, src_filename = _fetch_bytes(src_url)
        except Exception as exc:
            return {"error": f"Failed to read source: {exc}", "code": "fetch_failed"}

        effective_filename = filename or src_filename
        effective_ct = content_type or _guess_content_type(effective_filename)
        total_size = len(data)

        if total_size <= _SINGLE_PART_LIMIT:
            created = client.create_file_upload(
                mode="single_part",
                filename=effective_filename,
                content_type=effective_ct,
            )
            upload_id = created.get("id")
            if not upload_id:
                return {"error": "No upload id returned by Notion"}
            client.send_file_upload(
                upload_id,
                data,
                filename=effective_filename,
                content_type=effective_ct,
            )
            completed = client.complete_file_upload(upload_id)
            return {
                "upload_id": upload_id,
                "filename": effective_filename,
                "content_type": effective_ct,
                "size_bytes": total_size,
                "mode": "single_part",
                "status": completed.get("status"),
            }

        parts = max(1, -(-total_size // _MULTI_PART_CHUNK))  # ceil division
        created = client.create_file_upload(
            mode="multi_part",
            filename=effective_filename,
            content_type=effective_ct,
            number_of_parts=parts,
        )
        upload_id = created.get("id")
        if not upload_id:
            return {"error": "No upload id returned by Notion"}

        for i in range(parts):
            start = i * _MULTI_PART_CHUNK
            end = min(start + _MULTI_PART_CHUNK, total_size)
            chunk = data[start:end]
            client.send_file_upload(
                upload_id,
                chunk,
                part_number=i + 1,
                filename=effective_filename,
                content_type=effective_ct,
            )

        completed = client.complete_file_upload(upload_id)
        return {
            "upload_id": upload_id,
            "filename": effective_filename,
            "content_type": effective_ct,
            "size_bytes": total_size,
            "mode": "multi_part",
            "parts": parts,
            "status": completed.get("status"),
        }

    return run_notion_tool(user_id, _do)


def notion_list_file_uploads(
    max_results: int = 25,
    start_cursor: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List recent Notion file uploads for this integration."""
    _ = session_id

    def _do(client: Any) -> Dict[str, Any]:
        page = client.list_file_uploads(
            page_size=max_results, start_cursor=start_cursor
        )
        uploads = []
        for u in page.get("results") or []:
            uploads.append(
                {
                    "id": u.get("id"),
                    "filename": u.get("filename"),
                    "content_type": u.get("content_type"),
                    "status": u.get("status"),
                    "created_time": u.get("created_time"),
                }
            )
        return {
            "count": len(uploads),
            "uploads": uploads,
            "has_more": page.get("has_more", False),
            "next_cursor": page.get("next_cursor"),
        }

    return run_notion_tool(user_id, _do)
