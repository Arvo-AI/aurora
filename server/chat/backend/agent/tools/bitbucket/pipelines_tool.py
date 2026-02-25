"""Bitbucket Pipelines CI/CD operations tool."""

import json
import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .utils import (
    get_bb_client_for_user,
    resolve_workspace_repo,
    require_repo,
    forward_if_error,
    truncate_text,
    build_error_response,
    build_success_response,
    confirm_or_cancel,
)

logger = logging.getLogger(__name__)

LOG_TRUNCATE_LIMIT = 100_000


def _get_state_name(state_value) -> str:
    """Extract pipeline/step state name from the API's state field."""
    if isinstance(state_value, dict):
        return state_value.get("name", "")
    return str(state_value) if state_value else ""


class BitbucketPipelinesArgs(BaseModel):
    action: Literal[
        "list_pipelines",
        "get_pipeline",
        "trigger_pipeline",
        "stop_pipeline",
        "list_pipeline_steps",
        "get_step_log",
        "get_pipeline_step",
    ] = Field(description="The operation to perform.")
    workspace: Optional[str] = Field(None, description="Workspace slug. Auto-resolves from saved selection if omitted.")
    repo_slug: Optional[str] = Field(None, description="Repository slug. Auto-resolves from saved selection if omitted.")
    pipeline_uuid: Optional[str] = Field(None, description="Pipeline UUID (required for single-pipeline operations).")
    step_uuid: Optional[str] = Field(None, description="Step UUID (for get_step_log, get_pipeline_step).")
    target_branch: Optional[str] = Field(None, description="Branch to run pipeline on (for trigger_pipeline).")
    pattern: Optional[str] = Field(None, description="Custom pipeline pattern name (for trigger_pipeline).")
    variables: Optional[dict[str, str]] = Field(None, description="Pipeline variables as key-value pairs (for trigger_pipeline).")


def _require_pipeline(ws, repo, pipeline_uuid) -> Optional[str]:
    """Validate workspace, repo, and pipeline_uuid are present."""
    err = require_repo(ws, repo)
    if err:
        return err
    if not pipeline_uuid:
        return "pipeline_uuid is required"
    return None


def bitbucket_pipelines(
    action: str,
    workspace: Optional[str] = None,
    repo_slug: Optional[str] = None,
    pipeline_uuid: Optional[str] = None,
    step_uuid: Optional[str] = None,
    target_branch: Optional[str] = None,
    pattern: Optional[str] = None,
    variables: Optional[dict[str, str]] = None,
    user_id: Optional[str] = None,
    **kwargs,
) -> str:
    if not user_id:
        return build_error_response("User context not available")

    client = get_bb_client_for_user(user_id)
    if not client:
        return build_error_response("Bitbucket not connected. Please connect Bitbucket first.")

    ws, repo, saved_branch, source = resolve_workspace_repo(user_id, workspace, repo_slug)

    try:
        if action == "list_pipelines":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            result = client.list_pipelines(ws, repo)
            if isinstance(result, list):
                pipelines = [{
                    "uuid": p.get("uuid", "").strip("{}"),
                    "state": _get_state_name(p.get("state")),
                    "target_branch": p.get("target", {}).get("ref_name", ""),
                    "creator": p.get("creator", {}).get("display_name", "") if p.get("creator") else "",
                    "created_on": p.get("created_on"),
                    "completed_on": p.get("completed_on"),
                    "build_number": p.get("build_number"),
                } for p in result]
                return build_success_response(pipelines=pipelines, count=len(pipelines))
            return json.dumps(result, default=str)

        if action == "get_pipeline":
            if err := _require_pipeline(ws, repo, pipeline_uuid):
                return build_error_response(err)
            return json.dumps(client.get_pipeline(ws, repo, pipeline_uuid), default=str)

        if action == "trigger_pipeline":
            if err := require_repo(ws, repo):
                return build_error_response(err)
            branch = target_branch or saved_branch
            if not branch:
                return build_error_response("target_branch is required")
            if cancelled := confirm_or_cancel(user_id,
                    f"Trigger pipeline on branch '{branch}' in {ws}/{repo}",
                    "bitbucket_pipelines"):
                return cancelled
            result = client.trigger_pipeline(ws, repo, branch, pattern=pattern, variables=variables)
            if err := forward_if_error(result):
                return err
            return build_success_response(
                message=f"Pipeline triggered on branch '{branch}'",
                uuid=result.get("uuid", "").strip("{}"),
                build_number=result.get("build_number"),
            )

        if action == "stop_pipeline":
            if err := _require_pipeline(ws, repo, pipeline_uuid):
                return build_error_response(err)
            if cancelled := confirm_or_cancel(user_id,
                    f"Stop pipeline {pipeline_uuid} in {ws}/{repo}",
                    "bitbucket_pipelines"):
                return cancelled
            result = client.stop_pipeline(ws, repo, pipeline_uuid)
            if err := forward_if_error(result):
                return err
            return build_success_response(message=f"Pipeline {pipeline_uuid} stop requested")

        if action == "list_pipeline_steps":
            if err := _require_pipeline(ws, repo, pipeline_uuid):
                return build_error_response(err)
            result = client.list_pipeline_steps(ws, repo, pipeline_uuid)
            if isinstance(result, list):
                steps = [{
                    "uuid": s.get("uuid", "").strip("{}"),
                    "name": s.get("name", ""),
                    "state": _get_state_name(s.get("state")),
                    "started_on": s.get("started_on"),
                    "completed_on": s.get("completed_on"),
                    "duration_in_seconds": s.get("duration_in_seconds"),
                } for s in result]
                return build_success_response(steps=steps, count=len(steps))
            return json.dumps(result, default=str)

        if action == "get_pipeline_step":
            if err := _require_pipeline(ws, repo, pipeline_uuid):
                return build_error_response(err)
            if not step_uuid:
                return build_error_response("step_uuid is required")
            return json.dumps(client.get_pipeline_step(ws, repo, pipeline_uuid, step_uuid), default=str)

        if action == "get_step_log":
            if err := _require_pipeline(ws, repo, pipeline_uuid):
                return build_error_response(err)
            if not step_uuid:
                return build_error_response("step_uuid is required")
            result = client.get_pipeline_step_log(ws, repo, pipeline_uuid, step_uuid)
            if err := forward_if_error(result):
                return err
            if isinstance(result, str):
                result = truncate_text(result, LOG_TRUNCATE_LIMIT, label="log")
            return build_success_response(log=result, pipeline_uuid=pipeline_uuid, step_uuid=step_uuid)

        return build_error_response(f"Unknown action: {action}")

    except Exception as e:
        logger.error(f"Bitbucket pipelines tool error: {e}", exc_info=True)
        return build_error_response(f"Bitbucket API error: {str(e)}")
