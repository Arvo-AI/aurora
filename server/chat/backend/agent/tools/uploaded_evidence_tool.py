"""
Uploaded Evidence Tool

Lets the RCA agent list, read and grep the files a user uploaded when they
created an incident by hand (source_type='upload'). Those incidents may have
nothing connected, so these files can be the agent's only view of the system.
"""

import json
import logging
import re
from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_READ_LINES = 400
MAX_GREP_MATCHES = 100
MAX_PATTERN_LENGTH = 200
MAX_LINE_CHARS = 300

class ReadUploadedFileArgs(BaseModel):
    operation: str = Field(
        description="'list' to see the uploaded files, 'read' to read lines from one file, 'grep' to search"
    )
    filename: Optional[str] = Field(
        default=None,
        description="File name from 'list'. Required for 'read'; for 'grep', omit to search every file.",
    )
    pattern: Optional[str] = Field(
        default=None,
        description="Case-insensitive regex for 'grep', e.g. 'error|exception|timeout|oom'",
    )
    start_line: int = Field(default=1, description="First line to return for 'read' (1-indexed)")
    num_lines: int = Field(default=200, description=f"Number of lines for 'read' (max {MAX_READ_LINES})")


READ_UPLOADED_FILE_DESCRIPTION = (
    "Read the evidence files the user uploaded for this incident (logs, alert payloads, CI output, "
    "stack traces). Start with operation='list', then 'grep' for errors across all files, then "
    "'read' around the matching line numbers. Cite file name and line number in your findings."
)


def _fetch_uploaded_files(cursor, conn, incident_id: str, user_id: str) -> Tuple[List[dict], Optional[str]]:
    """Return the uploaded file entries stored on the incident, or an error string."""
    from utils.auth.stateless_auth import set_rls_context

    set_rls_context(cursor, conn, user_id, log_prefix="[UploadedEvidence]")
    cursor.execute("SELECT alert_metadata FROM incidents WHERE id = %s", (incident_id,))
    row = cursor.fetchone()
    if not row:
        return [], f"Error: Incident {incident_id} not found."

    metadata = row[0] or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    files = metadata.get("uploaded_files") or []
    if not files:
        return [], "No files were uploaded for this incident."
    return files, None


def _load_lines(entry: dict, user_id: str) -> List[str]:
    from utils.storage.storage import get_storage_manager

    data = get_storage_manager(user_id).download_bytes(entry["path"], user_id=user_id)
    return data.decode("utf-8", errors="replace").splitlines()


def _read_lines(lines: List[str], start_line: int, num_lines: int) -> str:
    start = max(start_line, 1)
    count = min(max(num_lines, 1), MAX_READ_LINES)
    chunk = lines[start - 1:start - 1 + count]
    if not chunk:
        return f"No lines at {start}; the file has {len(lines)} lines."
    body = "\n".join(f"{start + i}: {line[:MAX_LINE_CHARS]}" for i, line in enumerate(chunk))
    return f"Lines {start}-{start + len(chunk) - 1} of {len(lines)}:\n{body}"


def _grep_lines(filename: str, lines: List[str], regex, limit: int) -> List[str]:
    hits = []
    for number, line in enumerate(lines, start=1):
        if regex.search(line):
            hits.append(f"{filename}:{number}: {line[:MAX_LINE_CHARS]}")
            if len(hits) >= limit:
                break
    return hits


def read_uploaded_file(
    operation: str,
    filename: Optional[str] = None,
    pattern: Optional[str] = None,
    start_line: int = 1,
    num_lines: int = 200,
    user_id: Optional[str] = None,
    incident_id: Optional[str] = None,
    **kwargs,
) -> str:
    """List, read or grep uploaded evidence files (from the incident, or attached to the current message)."""
    if not user_id:
        return "Error: User authentication required."

    from utils.db.connection_pool import db_pool

    try:
        files, err = [], "No uploaded files are available."
        if incident_id:
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    files, err = _fetch_uploaded_files(cursor, conn, incident_id, user_id)
        if not files:
            # Interactive chat: no incident yet, so read the files attached to this message.
            from chat.backend.agent.tools.trigger_rca_tool import _evidence_from_state
            chat_files = _evidence_from_state()
            if chat_files:
                files, err = chat_files, None
        if err:
            return err

        if operation == "list":
            return "\n".join(f"- {f['filename']} ({f['size_bytes']} bytes)" for f in files)

        by_name = {f["filename"]: f for f in files}
        if filename and filename not in by_name:
            return f"Error: unknown file '{filename}'. Uploaded files: {', '.join(by_name)}"

        if operation == "read":
            if not filename:
                return "Error: filename is required for 'read'."
            return _read_lines(_load_lines(by_name[filename], user_id), start_line, num_lines)

        if operation == "grep":
            if not pattern or len(pattern) > MAX_PATTERN_LENGTH:
                return f"Error: pattern is required for 'grep' (max {MAX_PATTERN_LENGTH} chars)."
            try:
                regex = re.compile(pattern, re.IGNORECASE)
            except re.error as e:
                return f"Error: invalid regex: {e}"
            targets = [by_name[filename]] if filename else files
            hits: List[str] = []
            for entry in targets:
                hits.extend(_grep_lines(entry["filename"], _load_lines(entry, user_id), regex, MAX_GREP_MATCHES - len(hits)))
                if len(hits) >= MAX_GREP_MATCHES:
                    break
            if not hits:
                return f"No lines match '{pattern}'."
            suffix = f"\n(stopped at {MAX_GREP_MATCHES} matches; narrow the pattern)" if len(hits) >= MAX_GREP_MATCHES else ""
            return "\n".join(hits) + suffix

        return "Error: operation must be 'list', 'read' or 'grep'."
    except Exception as e:
        logger.exception("[UploadedEvidence] Error reading uploaded evidence: %s", e)
        return f"Error reading uploaded evidence: {e}"
