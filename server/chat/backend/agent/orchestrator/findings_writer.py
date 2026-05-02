"""Factory for the write_findings StructuredTool injected into each sub-agent."""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

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


def make_write_findings_tool(agent_id: str, role_name: str, incident_id: str,
                              user_id: str, org_id: Optional[str],
                              child_session_id: str) -> StructuredTool:
    schema_failures = {"n": 0}  # closure-bound; bounded by _SCHEMA_RETRY_LIMIT

    def write_findings(body: str) -> str:
        return _write_findings_impl(
            body=body, agent_id=agent_id, role_name=role_name,
            incident_id=incident_id, user_id=user_id, org_id=org_id,
            child_session_id=child_session_id, failures=schema_failures,
        )

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
                         incident_id: str, user_id: str, org_id: Optional[str],
                         child_session_id: str, failures: dict) -> str:
    inc_hash = hash_for_log(incident_id)
    logger.info(
        "write_findings: agent=%s incident=%s session=%s",
        agent_id, inc_hash, hash_for_log(child_session_id),
    )

    try:
        meta = parse_findings(body)
    except FindingsValidationError as exc:
        failures["n"] += 1
        logger.warning(
            "write_findings: schema validation failed for %s (attempt %d/%d): %s",
            agent_id, failures["n"], _SCHEMA_RETRY_LIMIT, exc,
        )
        if failures["n"] >= _SCHEMA_RETRY_LIMIT:
            # Hard cap: force a stub so the agent stops retrying broken markdown
            # and synthesis sees status=failed deterministically.
            return _force_stub_after_retry_exhaustion(
                agent_id=agent_id, role_name=role_name, incident_id=incident_id,
                user_id=user_id, org_id=org_id, child_session_id=child_session_id,
                error_message=str(exc),
            )
        return f"ERROR (attempt {failures['n']}/{_SCHEMA_RETRY_LIMIT}): findings.md schema validation failed: {exc}. Please fix and call write_findings again."

    storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
    try:
        from utils.storage.storage import get_storage_manager
        mgr = get_storage_manager(user_id)
        mgr.upload_bytes(body.encode("utf-8"), storage_uri, user_id, content_type="text/markdown")
        logger.info("write_findings: uploaded findings for agent %s", agent_id)
    except Exception:
        logger.exception("write_findings: storage upload failed for agent %s", agent_id)
        return "ERROR: storage upload failed. Please retry."

    _update_finding_row(
        agent_id=agent_id, incident_id=incident_id, user_id=user_id, org_id=org_id,
        meta=meta, storage_uri=storage_uri, child_session_id=child_session_id,
    )

    return f"findings.md saved successfully. strength={meta.get('self_assessed_strength')} status={meta.get('status')}"


def _update_finding_row(*, agent_id: str, incident_id: str, user_id: str,
                        org_id: Optional[str], meta: dict, storage_uri: str,
                        child_session_id: str) -> None:
    try:
        now = datetime.now(timezone.utc)
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                set_rls_context(cur, conn, user_id, log_prefix="[FindingsWriter]")
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
            conn.commit()
    except Exception:
        logger.exception(
            "write_findings: failed to update rca_findings row for agent %s", agent_id
        )


def _force_stub_after_retry_exhaustion(*, agent_id: str, role_name: str,
                                        incident_id: str, user_id: str,
                                        org_id: Optional[str], child_session_id: str,
                                        error_message: str) -> str:
    """Persist a synthetic stub when the LLM exhausts its schema-retry budget.

    Returns a success-shaped acknowledgment so the LLM stops retrying broken
    markdown. Synthesis sees status=failed deterministically.
    """
    logger.warning(
        "write_findings: agent %s exhausted schema retries — writing stub", agent_id,
    )
    storage_uri = f"rca/{incident_id}/findings/{agent_id}.md"
    stub_body = make_stub(
        agent_id=agent_id, role_name=role_name, incident_id=incident_id,
        purpose="schema validation retries exhausted", status="failed",
        error_message=error_message,
    )
    try:
        from utils.storage.storage import get_storage_manager
        get_storage_manager(user_id).upload_bytes(
            stub_body.encode("utf-8"), storage_uri, user_id, content_type="text/markdown",
        )
    except Exception:
        logger.exception("write_findings: failed to upload stub for agent %s", agent_id)

    try:
        meta = parse_findings(stub_body)
        _update_finding_row(
            agent_id=agent_id, incident_id=incident_id, user_id=user_id, org_id=org_id,
            meta=meta, storage_uri=storage_uri, child_session_id=child_session_id,
        )
    except Exception:
        logger.exception("write_findings: failed to persist stub row for agent %s", agent_id)

    return (
        f"findings.md persisted with status=failed after {_SCHEMA_RETRY_LIMIT} "
        f"schema-validation failures. Stop calling write_findings."
    )
