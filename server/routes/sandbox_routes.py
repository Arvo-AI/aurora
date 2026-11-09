"""
Sandboxes: A sandbox holds evidence like pasted logs, postmortems,
alert payloads, documents, screenshots, links, public GitHub repo 
link, and incident time.

Storage: text lives in ``sandbox_artifacts.content``; image bytes 
live in object storage at ``sandboxes/{org_id}/{sandbox_id}/{artifact_id}``.

Links are stored as text and never fetched server-side (no SSRF surface).
"""

import io
import logging
import re
from functools import wraps

from flask import Blueprint, jsonify, request, send_file

from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request, set_rls_context
from utils.db.connection_pool import db_pool
from utils.query_helpers import iso_utc
from utils.validation import is_valid_uuid, strip_nul

logger = logging.getLogger(__name__)

sandbox_bp = Blueprint("sandbox", __name__)
_LOG_PREFIX = "[Sandbox]"

MAX_SANDBOX_NAME = 200
MAX_ARTIFACTS_PER_SANDBOX = 50
MAX_ARTIFACT_NAME = 255
MAX_TEXT_CHARS = 200_000          # per pasted/extracted text artifact
MAX_FILE_BYTES = 10 * 1024 * 1024  # per uploaded file
PREVIEW_CHARS = 300

TEXT_KINDS = {"log", "text", "link"}
DOC_EXTENSIONS = {"pdf", "txt", "md", "log", "json", "yaml", "yml", "csv"}
# No SVG: it can carry script, and these bytes are served back to the browser.
IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_URL_RE = re.compile(r"https?://[^\s<>\"']{1,2000}")


def _err(message: str, status: int):
    return jsonify({"error": message}), status


def _storage_path(org_id: str, sandbox_id: str, artifact_id: str) -> str:
    return f"sandboxes/{org_id}/{sandbox_id}/{artifact_id}"


def _serialize_sandbox(row) -> dict:
    """Row: (id, name, public_repo, incident_time, created_at, updated_at, artifact_count)."""
    return {
        "id": str(row[0]),
        "name": row[1],
        "publicRepo": row[2],
        "incidentTime": iso_utc(row[3]) if row[3] else None,
        "createdAt": iso_utc(row[4]),
        "updatedAt": iso_utc(row[5]),
        "artifactCount": row[6],
    }


_ARTIFACT_COLS = "id, kind, name, content, mime_type, size_bytes, created_at"


def _serialize_artifact(row, *, full: bool) -> dict:
    """Row: (id, kind, name, content, mime_type, size_bytes, created_at)."""
    content = row[3] or ""
    data = {
        "id": str(row[0]),
        "kind": row[1],
        "name": row[2],
        "mimeType": row[4],
        "sizeBytes": row[5],
        "createdAt": iso_utc(row[6]),
        "preview": content[:PREVIEW_CHARS],
        "truncated": len(content) > PREVIEW_CHARS,
    }
    if full:
        data["content"] = content
    return data


def with_sandbox(fn):
    """Validate sandbox_id, open DB, set RLS, confirm the sandbox exists.

    Injects org_id, conn, cursor as keyword args. 404 if absent.
    """
    @wraps(fn)
    def wrapper(user_id, sandbox_id, *args, **kwargs):
        if not is_valid_uuid(sandbox_id):
            return _err("Invalid sandbox ID", 400)
        org_id = get_org_id_from_request()
        try:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    if set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX) is None:
                        return _err("Unable to resolve organization context", 500)
                    cursor.execute(
                        "SELECT id FROM sandboxes WHERE id = %s AND org_id = %s",
                        (sandbox_id, org_id),
                    )
                    if not cursor.fetchone():
                        return _err("Sandbox not found", 404)
                    return fn(user_id, sandbox_id, *args, org_id=org_id, conn=conn, cursor=cursor, **kwargs)
        except Exception:
            logger.exception("%s %s failed for sandbox %s", _LOG_PREFIX, fn.__name__, sandbox_id)
            return _err(f"Failed to {fn.__name__.replace('_', ' ')}", 500)

    return wrapper


def _touch(cursor, sandbox_id: str) -> None:
    cursor.execute("UPDATE sandboxes SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (sandbox_id,))


# ── Sandboxes ───────────────────────────────────────────────────────


@sandbox_bp.route("/api/sandboxes", methods=["GET"])
@require_permission("incidents", "read")
def list_sandboxes(user_id):
    org_id = get_org_id_from_request()
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX)
                cursor.execute(
                    """SELECT s.id, s.name, s.public_repo, s.incident_time, s.created_at, s.updated_at,
                              (SELECT COUNT(*) FROM sandbox_artifacts a WHERE a.sandbox_id = s.id)
                       FROM sandboxes s
                       WHERE s.org_id = %s
                       ORDER BY s.updated_at DESC""",
                    (org_id,),
                )
                rows = cursor.fetchall()
        return jsonify({"sandboxes": [_serialize_sandbox(r) for r in rows]})
    except Exception:
        logger.exception("%s Failed to list sandboxes", _LOG_PREFIX)
        return _err("Failed to list sandboxes", 500)


@sandbox_bp.route("/api/sandboxes", methods=["POST"])
@require_permission("incidents", "write")
def create_sandbox(user_id):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()[:MAX_SANDBOX_NAME] or "Untitled sandbox"
    org_id = get_org_id_from_request()
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if set_rls_context(cursor, conn, user_id, log_prefix=_LOG_PREFIX) is None:
                    return _err("Unable to resolve organization context", 500)
                cursor.execute(
                    """INSERT INTO sandboxes (org_id, user_id, name) VALUES (%s, %s, %s)
                       RETURNING id, name, public_repo, incident_time, created_at, updated_at, 0""",
                    (org_id, user_id, name),
                )
                row = cursor.fetchone()
            conn.commit()
        return jsonify({"sandbox": _serialize_sandbox(row)}), 201
    except Exception:
        logger.exception("%s Failed to create sandbox", _LOG_PREFIX)
        return _err("Failed to create sandbox", 500)


def _load_sandbox(cursor, sandbox_id: str) -> dict:
    """Sandbox plus artifact list (previews only). Caller has set RLS."""
    cursor.execute(
        """SELECT s.id, s.name, s.public_repo, s.incident_time, s.created_at, s.updated_at,
                  (SELECT COUNT(*) FROM sandbox_artifacts a WHERE a.sandbox_id = s.id)
           FROM sandboxes s WHERE s.id = %s""",
        (sandbox_id,),
    )
    sandbox = _serialize_sandbox(cursor.fetchone())
    cursor.execute(
        f"SELECT {_ARTIFACT_COLS} FROM sandbox_artifacts WHERE sandbox_id = %s ORDER BY created_at",
        (sandbox_id,),
    )
    sandbox["artifacts"] = [_serialize_artifact(r, full=False) for r in cursor.fetchall()]
    return sandbox


@sandbox_bp.route("/api/sandboxes/<sandbox_id>", methods=["GET"])
@require_permission("incidents", "read")
@with_sandbox
def get_sandbox(user_id, sandbox_id, *, org_id, conn, cursor):
    return jsonify({"sandbox": _load_sandbox(cursor, sandbox_id)})


@sandbox_bp.route("/api/sandboxes/<sandbox_id>", methods=["PATCH"])
@require_permission("incidents", "write")
@with_sandbox
def update_sandbox(user_id, sandbox_id, *, org_id, conn, cursor):
    """Rename, set/clear the public repo, set/clear the incident time."""
    data = request.get_json(silent=True) or {}
    sets, params = [], []

    if "name" in data:
        name = str(data.get("name") or "").strip()[:MAX_SANDBOX_NAME]
        if not name:
            return _err("name cannot be empty", 400)
        sets.append("name = %s")
        params.append(name)

    if "publicRepo" in data:
        repo = str(data.get("publicRepo") or "").strip()
        if repo:
            from chat.backend.agent.tools.github_rca_tool import check_public_repo
            repo = check_public_repo(repo)
            if not repo:
                return _err("publicRepo must be a public GitHub repository as owner/repo", 400)
        sets.append("public_repo = %s")
        params.append(repo or None)

    if "incidentTime" in data:
        from utils.incidents import parse_incident_time
        incident_time, time_error = parse_incident_time(data.get("incidentTime"))
        if time_error:
            return _err(time_error, 400)
        sets.append("incident_time = %s")
        params.append(incident_time)

    if not sets:
        return _err("Nothing to update", 400)

    cursor.execute(
        f"UPDATE sandboxes SET {', '.join(sets)}, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
        (*params, sandbox_id),
    )
    conn.commit()
    return jsonify({"sandbox": _load_sandbox(cursor, sandbox_id)})


@sandbox_bp.route("/api/sandboxes/<sandbox_id>", methods=["DELETE"])
@require_permission("incidents", "write")
@with_sandbox
def delete_sandbox(user_id, sandbox_id, *, org_id, conn, cursor):
    cursor.execute("DELETE FROM sandboxes WHERE id = %s", (sandbox_id,))  # artifacts cascade
    conn.commit()
    try:
        from utils.storage.storage import get_storage_manager
        get_storage_manager().delete_files_with_prefix(f"sandboxes/{org_id}/{sandbox_id}/")
    except Exception:
        # Rows are gone; orphaned image bytes are unreachable (path is unguessable
        # and the raw route checks the row). Log and move on.
        logger.warning("%s Could not delete stored files for sandbox %s", _LOG_PREFIX, sandbox_id)
    return jsonify({"deleted": True})


# ── Artifacts ───────────────────────────────────────────────────────


def _read_upload():
    """Return (kind, name, content, mime, raw_bytes) for a multipart upload, or an error response."""
    file = request.files.get("file")
    if not file or not file.filename:
        return None, _err("No file provided", 400)

    raw = file.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        return None, _err("File too large (max 10MB)", 400)

    name = file.filename[:MAX_ARTIFACT_NAME]
    mime = (file.mimetype or "").lower()

    if mime in IMAGE_TYPES:
        return ("image", name, None, mime, raw), None

    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if ext not in DOC_EXTENSIONS:
        allowed = ", ".join(sorted(DOC_EXTENSIONS)) + ", png, jpg, gif, webp"
        return None, _err(f"Unsupported file type. Supported: {allowed}", 400)

    if ext == "pdf":
        from routes.memory.routes import _extract_pdf_text
        try:
            content = _extract_pdf_text(raw)
        except Exception:
            return None, _err("Could not read PDF", 400)
    else:
        content = raw.decode("utf-8", errors="replace")

    content = strip_nul(content)
    if not content.strip():
        return None, _err("No text could be extracted from the file", 400)
    if len(content) > MAX_TEXT_CHARS:
        return None, _err(f"Extracted text exceeds {MAX_TEXT_CHARS:,} characters", 400)
    kind = "log" if ext == "log" else "document"
    return (kind, name, content, mime or "text/plain", None), None


def _read_json_artifact():
    """Return (kind, name, content, mime, None) for a pasted log/text/link, or an error response."""
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "").strip()
    if kind not in TEXT_KINDS:
        return None, _err("kind must be one of: log, text, link", 400)

    content = strip_nul(str(data.get("content") or "")).strip()
    if not content:
        return None, _err("content is required", 400)
    if len(content) > MAX_TEXT_CHARS:
        return None, _err(f"content exceeds {MAX_TEXT_CHARS:,} characters", 400)
    if kind == "link" and not _URL_RE.fullmatch(content):
        return None, _err("link must be a single http(s) URL", 400)

    default_name = content if kind == "link" else f"Pasted {kind}"
    name = (str(data.get("name") or "").strip() or default_name)[:MAX_ARTIFACT_NAME]
    return (kind, name, content, "text/plain", None), None


@sandbox_bp.route("/api/sandboxes/<sandbox_id>/artifacts", methods=["POST"])
@require_permission("incidents", "write")
@with_sandbox
def add_artifact(user_id, sandbox_id, *, org_id, conn, cursor):
    """Add a pasted log/text/link (JSON) or an uploaded document/image (multipart)."""
    cursor.execute("SELECT COUNT(*) FROM sandbox_artifacts WHERE sandbox_id = %s", (sandbox_id,))
    if cursor.fetchone()[0] >= MAX_ARTIFACTS_PER_SANDBOX:
        return _err(f"A sandbox holds at most {MAX_ARTIFACTS_PER_SANDBOX} artifacts", 400)

    parsed, error = _read_upload() if request.files else _read_json_artifact()
    if error:
        return error
    kind, name, content, mime, raw = parsed
    size = len(raw) if raw is not None else len(content.encode("utf-8"))

    cursor.execute(
        f"""INSERT INTO sandbox_artifacts (sandbox_id, org_id, user_id, kind, name, content, mime_type, size_bytes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING {_ARTIFACT_COLS}""",
        (sandbox_id, org_id, user_id, kind, name, content, mime, size),
    )
    row = cursor.fetchone()

    if raw is not None:
        # Image: bytes go to object storage; the row records where.
        path = _storage_path(org_id, sandbox_id, str(row[0]))
        try:
            from utils.storage.storage import get_storage_manager
            get_storage_manager().upload_bytes(raw, path, content_type=mime)
        except Exception:
            conn.rollback()
            logger.exception("%s Image upload failed for sandbox %s", _LOG_PREFIX, sandbox_id)
            return _err("Failed to store image", 500)
        cursor.execute("UPDATE sandbox_artifacts SET storage_path = %s WHERE id = %s", (path, row[0]))

    _touch(cursor, sandbox_id)
    conn.commit()
    return jsonify({"artifact": _serialize_artifact(row, full=False)}), 201


@sandbox_bp.route("/api/sandboxes/<sandbox_id>/artifacts/<artifact_id>", methods=["GET"])
@require_permission("incidents", "read")
@with_sandbox
def get_artifact(user_id, sandbox_id, artifact_id, *, org_id, conn, cursor):
    if not is_valid_uuid(artifact_id):
        return _err("Invalid artifact ID", 400)
    cursor.execute(
        f"SELECT {_ARTIFACT_COLS} FROM sandbox_artifacts WHERE id = %s AND sandbox_id = %s",
        (artifact_id, sandbox_id),
    )
    row = cursor.fetchone()
    if not row:
        return _err("Artifact not found", 404)
    return jsonify({"artifact": _serialize_artifact(row, full=True)})


@sandbox_bp.route("/api/sandboxes/<sandbox_id>/artifacts/<artifact_id>/raw", methods=["GET"])
@require_permission("incidents", "read")
@with_sandbox
def get_artifact_raw(user_id, sandbox_id, artifact_id, *, org_id, conn, cursor):
    """Serve an image artifact's bytes (for thumbnails)."""
    if not is_valid_uuid(artifact_id):
        return _err("Invalid artifact ID", 400)
    cursor.execute(
        "SELECT storage_path, mime_type FROM sandbox_artifacts WHERE id = %s AND sandbox_id = %s AND kind = 'image'",
        (artifact_id, sandbox_id),
    )
    row = cursor.fetchone()
    if not row or not row[0] or row[1] not in IMAGE_TYPES:
        return _err("Image not found", 404)
    from utils.storage.storage import get_storage_manager
    data = get_storage_manager().download_bytes(row[0])
    resp = send_file(io.BytesIO(data), mimetype=row[1])
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


@sandbox_bp.route("/api/sandboxes/<sandbox_id>/artifacts/<artifact_id>", methods=["DELETE"])
@require_permission("incidents", "write")
@with_sandbox
def delete_artifact(user_id, sandbox_id, artifact_id, *, org_id, conn, cursor):
    if not is_valid_uuid(artifact_id):
        return _err("Invalid artifact ID", 400)
    cursor.execute(
        "DELETE FROM sandbox_artifacts WHERE id = %s AND sandbox_id = %s RETURNING storage_path",
        (artifact_id, sandbox_id),
    )
    row = cursor.fetchone()
    if not row:
        return _err("Artifact not found", 404)
    _touch(cursor, sandbox_id)
    conn.commit()
    if row[0]:
        try:
            from utils.storage.storage import get_storage_manager
            get_storage_manager().delete_file(row[0])
        except Exception:
            logger.warning("%s Could not delete stored file for artifact %s", _LOG_PREFIX, artifact_id)
    return jsonify({"deleted": True})
