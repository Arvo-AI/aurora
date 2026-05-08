"""Factory for the write_findings StructuredTool injected into each sub-agent."""

import json
import logging
from datetime import datetime, timezone

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from chat.backend.agent.orchestrator.findings_schema import (
    FindingsValidationError, make_stub, parse_findings,
)
from utils.auth.stateless_auth import set_rls_context
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)


class WriteFindingsArgs(BaseModel):
    body: str = Field(description=(
        "Complete findings.md text including YAML frontmatter and all required sections "
        "(## Summary, ## Evidence, ## Reasoning, ## What I ruled out)."
    ))


_SCHEMA_RETRY_LIMIT = 2


def _mirror_capture_tool_end(output: str, is_error: bool) -> None:
    """Best-effort mirror of capture_tool_end so the execution_steps row created
    by capture_tool_start (in agent.py's sub-agent stream loop) flips from
    running -> success/error. Never raises — tracking failure must not break
    the actual write_findings return value.

    Needed because make_write_findings_tool builds the StructuredTool directly
    (bypassing wrap_func_with_capture in cloud_tools.py), so nothing else flips
    the execution_steps row to a terminal state.
    """
    try:
        from chat.backend.agent.tools.cloud_tools import get_current_tool_call_id
        from utils.cloud.cloud_utils import get_tool_capture
        _tc = get_tool_capture()
        if _tc is None:
            return
        _tcid = get_current_tool_call_id(tool_name="write_findings")
        if _tcid:
            _tc.capture_tool_end(_tcid, output, is_error=is_error)
    except Exception:
        logging.debug("write_findings: capture_tool_end mirror failed", exc_info=True)


def make_write_findings_tool(agent_id: str, role_name: str, incident_id: str,
                              user_id: str,
                              child_session_id: str) -> StructuredTool:
    schema_failures = {"n": 0}  # closure-bound; bounded by _SCHEMA_RETRY_LIMIT

    def write_findings(body: str) -> str:
        try:
            return _write_findings_impl(
                body=body, agent_id=agent_id, role_name=role_name,
                incident_id=incident_id, user_id=user_id,
                child_session_id=child_session_id, failures=schema_failures,
            )
        except Exception as _exc:
            # Safety net: any uncaught exception inside _write_findings_impl
            # must still flip the execution_steps row to error before
            # propagating, otherwise the write_findings step stays stuck at
            # status=running forever. Mirrors wrap_func_with_capture's
            # error-path behavior in cloud_tools.py.
            _mirror_capture_tool_end(f"ERROR: {_exc}", is_error=True)
            raise

    return StructuredTool.from_function(
        func=write_findings,
        name="write_findings",
        description=(
            "Call this tool ONCE at the end of your investigation to persist your findings. "
            "Provide the full findings.md text with YAML frontmatter and required H2 sections."
        ),
        args_schema=WriteFindingsArgs,
    )


def _write_findings_impl(*, body: str, agent_id: str, role_name: str,
                         incident_id: str, user_id: str,
                         child_session_id: str, failures: dict) -> str:
    inc_hash = hash_for_log(incident_id)
    logger.info(
        "write_findings: agent=%s incident=%s session=%s",
        agent_id, inc_hash, hash_for_log(child_session_id),
    )

    try:
        meta = parse_findings(body)
        if str(meta.get("agent_id")) != agent_id:
            raise FindingsValidationError(
                f"findings.md agent_id must be {agent_id!r}, got {meta.get('agent_id')!r}"
            )
    except FindingsValidationError as exc:
        failures["n"] += 1
        logger.warning(
            "write_findings: schema validation failed for %s (attempt %d/%d): %s",
            agent_id, failures["n"], _SCHEMA_RETRY_LIMIT, exc,
        )
        if failures["n"] >= _SCHEMA_RETRY_LIMIT:
            # Hard cap: force a stub so the agent stops retrying broken markdown
            # and synthesis sees status=failed deterministically.
            stub_result = _force_stub_after_retry_exhaustion(
                agent_id=agent_id, role_name=role_name, incident_id=incident_id,
                user_id=user_id, child_session_id=child_session_id,
                error_message=str(exc),
            )
            _mirror_capture_tool_end(stub_result, is_error=True)
            return stub_result
        retry_msg = f"ERROR (attempt {failures['n']}/{_SCHEMA_RETRY_LIMIT}): findings.md schema validation failed: {exc}. Please fix and call write_findings again."
        _mirror_capture_tool_end(retry_msg, is_error=True)
        return retry_msg

    storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
    try:
        from utils.storage.storage import get_storage_manager
        mgr = get_storage_manager(user_id)
        mgr.upload_bytes(body.encode("utf-8"), storage_uri, user_id, content_type="text/markdown")
        logger.info("write_findings: uploaded findings for agent %s", agent_id)
    except Exception:
        logger.exception("write_findings: storage upload failed for agent %s", agent_id)
        upload_err = "ERROR: storage upload failed. Please retry."
        _mirror_capture_tool_end(upload_err, is_error=True)
        return upload_err

    db_ok = _update_finding_row(
        agent_id=agent_id, incident_id=incident_id, user_id=user_id,
        meta=meta, storage_uri=storage_uri, child_session_id=child_session_id,
    )
    if not db_ok:
        # DB update failed after a successful upload — try to delete the now-orphaned
        # object so a retry doesn't leave stale content behind. Best-effort: log on
        # cleanup failure but still surface the original DB error to the LLM.
        try:
            from utils.storage.storage import get_storage_manager
            get_storage_manager(user_id).delete_file(storage_uri, user_id)
        except Exception:
            logger.warning(
                "write_findings: orphaned storage object %s for agent %s "
                "(DB update failed and cleanup also failed)",
                storage_uri, agent_id,
            )
        db_err = "ERROR: failed to persist findings row. Please retry."
        _mirror_capture_tool_end(db_err, is_error=True)
        return db_err

    # Reset on confirmed success: only consecutive validation failures should
    # count toward the retry cap. Without this, a successful write followed by
    # one bad call would force-stub and overwrite the just-saved findings.md.
    failures["n"] = 0

    success_msg = f"findings.md saved successfully. strength={meta.get('self_assessed_strength')} status={meta.get('status')}"
    _mirror_capture_tool_end(success_msg, is_error=False)
    return success_msg


def _update_finding_row(*, agent_id: str, incident_id: str, user_id: str,
                        meta: dict, storage_uri: str,
                        child_session_id: str) -> bool:
    try:
        now = datetime.now(timezone.utc)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(cur, conn, user_id, log_prefix="[FindingsWriter]"):
                    logger.warning(
                        "write_findings: failed to set RLS context for agent %s", agent_id
                    )
                    return False
                cur.execute(
                    """
                    UPDATE rca_findings SET
                        status = %s,
                        storage_uri = %s,
                        self_assessed_strength = %s,
                        tools_used = %s,
                        citations = %s,
                        follow_ups_suggested = %s,
                        completed_at = %s,
                        child_session_id = %s
                    WHERE incident_id = %s AND agent_id = %s
                    """,
                    (
                        str(meta.get("status", "succeeded")),
                        storage_uri,
                        str(meta.get("self_assessed_strength", "inconclusive")),
                        json.dumps(meta.get("tools_used", [])),
                        json.dumps(meta.get("citations", [])),
                        json.dumps(meta.get("follow_ups_suggested", [])),
                        now,
                        child_session_id,
                        incident_id,
                        agent_id,
                    ),
                )
                if cur.rowcount == 0:
                    logger.warning(
                        "write_findings: no rca_findings row matched incident=%s agent=%s — "
                        "dispatcher may not have pre-inserted the row",
                        hash_for_log(incident_id), agent_id,
                    )
                    return False
            conn.commit()
        return True
    except Exception:
        logger.exception(
            "write_findings: failed to update rca_findings row for agent %s", agent_id
        )
        return False


def _row_already_succeeded(*, incident_id: str, agent_id: str, user_id: str) -> bool:
    """Return True if the rca_findings row for this agent is already at status='succeeded'.

    Used as a pre-check before _force_stub_after_retry_exhaustion to avoid
    clobbering a previously-good body with a stub.
    """
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                if not set_rls_context(cur, conn, user_id, log_prefix="[FindingsWriter]"):
                    return False
                cur.execute(
                    "SELECT status FROM rca_findings WHERE incident_id = %s AND agent_id = %s",
                    (incident_id, agent_id),
                )
                row = cur.fetchone()
                if not row:
                    return False
                return str(row[0]) == "succeeded"
    except Exception:
        logger.exception(
            "write_findings: status pre-check failed for agent %s", agent_id,
        )
        return False


def _force_stub_after_retry_exhaustion(*, agent_id: str, role_name: str,
                                        incident_id: str, user_id: str,
                                        child_session_id: str,
                                        error_message: str) -> str:
    """Persist a synthetic stub when the LLM exhausts its schema-retry budget.

    Returns a success-shaped acknowledgment so the LLM stops retrying broken
    markdown. Synthesis sees status=failed deterministically.
    """
    logger.warning(
        "write_findings: agent %s exhausted schema retries — writing stub", agent_id,
    )
    # Refuse to overwrite a row already at terminal/succeeded status. Without
    # this check, a successful write followed by two consecutive bad-schema
    # calls would force-stub and clobber the just-saved findings.md body.
    if _row_already_succeeded(incident_id=incident_id, agent_id=agent_id, user_id=user_id):
        logger.warning(
            "write_findings: agent %s already at status=succeeded — skipping stub overwrite",
            agent_id,
        )
        return (
            f"findings.md already persisted with status=succeeded; ignoring schema "
            f"retry exhaustion. Stop calling write_findings."
        )
    storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
    stub_body = make_stub(
        agent_id=agent_id, role_name=role_name, incident_id=incident_id,
        purpose="schema validation retries exhausted", status="failed",
        error_message=error_message,
    )
    upload_ok = False
    db_ok = False
    try:
        from utils.storage.storage import get_storage_manager
        get_storage_manager(user_id).upload_bytes(
            stub_body.encode("utf-8"), storage_uri, user_id, content_type="text/markdown",
        )
        upload_ok = True
    except Exception:
        logger.exception("write_findings: failed to upload stub for agent %s", agent_id)

    if upload_ok:
        try:
            meta = parse_findings(stub_body)
            db_ok = _update_finding_row(
                agent_id=agent_id, incident_id=incident_id, user_id=user_id,
                meta=meta, storage_uri=storage_uri, child_session_id=child_session_id,
            )
        except Exception:
            logger.exception("write_findings: failed to persist stub row for agent %s", agent_id)

    if upload_ok and db_ok:
        return (
            f"findings.md persisted with status=failed after {_SCHEMA_RETRY_LIMIT} "
            f"schema-validation failures. Stop calling write_findings."
        )
    return (
        f"findings.md could NOT be persisted after {_SCHEMA_RETRY_LIMIT} "
        f"schema-validation failures (upload_ok={upload_ok}, db_ok={db_ok}). "
        f"Stop calling write_findings."
    )
