"""
Jenkins RCA Tool - Unified Jenkins investigation tool for Root Cause Analysis.

Provides on-demand enrichment via all three Jenkins REST APIs:
- Core REST API: build details, changeSets, SCM revision
- Pipeline REST API (wfapi): stage-level breakdown and per-stage logs
- Blue Ocean REST API: HAL-compliant run data with changeSet
Plus OTel W3C Trace Context extraction for end-to-end correlation.
"""

import json
import logging
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class JenkinsRCAArgs(BaseModel):
    action: Literal[
        "recent_deployments",
        "build_detail",
        "pipeline_stages",
        "stage_log",
        "build_logs",
        "test_results",
        "blue_ocean_run",
        "blue_ocean_steps",
        "trace_context",
    ] = Field(description="Investigation action to perform")
    job_path: Optional[str] = Field(default=None, description="Jenkins job path (e.g. 'folder/job-name')")
    build_number: Optional[int] = Field(default=None, description="Build number to investigate")
    pipeline_name: Optional[str] = Field(default=None, description="Pipeline name for Blue Ocean API")
    run_number: Optional[int] = Field(default=None, description="Run number for Blue Ocean API")
    branch: Optional[str] = Field(default=None, description="Branch name (Blue Ocean)")
    node_id: Optional[str] = Field(default=None, description="Node/stage ID for stage-level log or steps")
    service: Optional[str] = Field(default=None, description="Service name filter for recent_deployments")
    time_window_hours: Optional[int] = Field(default=24, description="Lookback window in hours for recent_deployments")
    deployment_event_id: Optional[int] = Field(default=None, description="Deployment event ID for trace_context lookup")


def _get_client_for_user(user_id: str):
    """Build a JenkinsClient from the user's stored credentials."""
    from utils.auth.token_management import get_token_data
    from connectors.jenkins_connector.api_client import JenkinsClient

    creds = get_token_data(user_id, "jenkins")
    if not creds:
        return None
    base_url = creds.get("base_url")
    username = creds.get("username")
    api_token = creds.get("api_token")
    if not base_url or not username or not api_token:
        return None
    return JenkinsClient(base_url=base_url, username=username, api_token=api_token)


def jenkins_rca(
    action: str,
    job_path: Optional[str] = None,
    build_number: Optional[int] = None,
    pipeline_name: Optional[str] = None,
    run_number: Optional[int] = None,
    branch: Optional[str] = None,
    node_id: Optional[str] = None,
    service: Optional[str] = None,
    time_window_hours: int = 24,
    deployment_event_id: Optional[int] = None,
    **kwargs,
) -> str:
    """Unified Jenkins investigation tool for RCA."""
    user_id = kwargs.get("user_id", "")

    if not user_id:
        return json.dumps({"error": "No user context. Run this from an authenticated session."})

    if action == "recent_deployments":
        return _action_recent_deployments(user_id, service, time_window_hours)
    elif action == "trace_context":
        return _action_trace_context(user_id, deployment_event_id, job_path, build_number)

    client = _get_client_for_user(user_id)
    if not client:
        return json.dumps({"error": "Jenkins is not connected. Configure credentials in Settings > Connectors > Jenkins."})

    if action == "build_detail":
        return _action_build_detail(client, job_path, build_number)
    elif action == "pipeline_stages":
        return _action_pipeline_stages(client, job_path, build_number)
    elif action == "stage_log":
        return _action_stage_log(client, job_path, build_number, node_id)
    elif action == "build_logs":
        return _action_build_logs(client, job_path, build_number)
    elif action == "test_results":
        return _action_test_results(client, job_path, build_number)
    elif action == "blue_ocean_run":
        return _action_blue_ocean_run(client, pipeline_name or job_path, run_number or build_number, branch)
    elif action == "blue_ocean_steps":
        return _action_blue_ocean_steps(client, pipeline_name or job_path, run_number or build_number, node_id, branch)
    else:
        return json.dumps({"error": f"Unknown action: {action}"})


# ------------------------------------------------------------------
# Action implementations
# ------------------------------------------------------------------

def _action_recent_deployments(user_id: str, service: Optional[str], hours: int) -> str:
    """Query stored deployment events for temporal correlation."""
    try:
        from utils.db.connection_pool import db_pool
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if service:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM jenkins_deployment_events
                           WHERE user_id = %s AND service = %s
                                 AND received_at >= NOW() - make_interval(hours => %s)
                           ORDER BY received_at DESC LIMIT 20""",
                        (user_id, service, hours),
                    )
                else:
                    cursor.execute(
                        """SELECT id, service, environment, result, build_number, build_url,
                                  commit_sha, branch, repository, deployer, duration_ms,
                                  job_name, trace_id, received_at
                           FROM jenkins_deployment_events
                           WHERE user_id = %s
                                 AND received_at >= NOW() - make_interval(hours => %s)
                           ORDER BY received_at DESC LIMIT 20""",
                        (user_id, hours),
                    )
                rows = cursor.fetchall()

        if not rows:
            return json.dumps({"deployments": [], "message": f"No deployments found in last {hours}h" + (f" for service '{service}'" if service else "")})

        deployments = []
        for r in rows:
            deployments.append({
                "id": r[0], "service": r[1], "environment": r[2], "result": r[3],
                "build_number": r[4], "build_url": r[5], "commit_sha": r[6],
                "branch": r[7], "repository": r[8], "deployer": r[9],
                "duration_ms": r[10], "job_name": r[11], "trace_id": r[12],
                "received_at": r[13].isoformat() if r[13] else None,
            })
        return json.dumps({"deployments": deployments, "count": len(deployments)}, default=str)
    except Exception:
        logger.exception("jenkins_rca recent_deployments query failed")
        return json.dumps({"error": "Failed to fetch recent deployments. Please try again."})


def _action_build_detail(client, job_path: Optional[str], build_number: Optional[int]) -> str:
    """Fetch RCA-focused build details via Core REST API tree parameter."""
    if not job_path or not build_number:
        return json.dumps({"error": "job_path and build_number are required"})
    success, data, error = client.get_build_detail(job_path, build_number)
    if not success:
        return json.dumps({"error": error or "Failed to fetch build detail"})

    # Extract and format key RCA fields
    result = {
        "result": data.get("result"),
        "timestamp": data.get("timestamp"),
        "duration": data.get("duration"),
        "building": data.get("building"),
        "displayName": data.get("displayName"),
    }

    # Extract changeSets
    change_sets = []
    for cs in data.get("changeSets", []):
        for item in cs.get("items", []):
            change_sets.append({
                "commitId": item.get("commitId"),
                "author": item.get("author", {}).get("fullName"),
                "msg": item.get("msg"),
                "timestamp": item.get("timestamp"),
                "paths": item.get("paths", [])[:20],
            })
    result["changeSets"] = change_sets

    # Extract SCM info and build causes from actions
    for action in data.get("actions", []):
        if not isinstance(action, dict):
            continue
        if "lastBuiltRevision" in action:
            rev = action["lastBuiltRevision"]
            result["scm"] = {
                "sha": rev.get("SHA1"),
                "branch": [b.get("name") for b in rev.get("branch", [])],
                "remoteUrls": action.get("remoteUrls", []),
            }
        if "causes" in action:
            result["causes"] = action["causes"]
        if "parameters" in action:
            result["parameters"] = action["parameters"]

    # Extract OTel trace context if available
    from connectors.jenkins_connector.api_client import JenkinsClient
    trace_ctx = JenkinsClient.extract_trace_context(data)
    if trace_ctx:
        result["traceContext"] = trace_ctx

    return json.dumps(result, default=str)


def _action_pipeline_stages(client, job_path: Optional[str], build_number: Optional[int]) -> str:
    """Fetch stage-level breakdown via Pipeline REST API (wfapi)."""
    if not job_path or not build_number:
        return json.dumps({"error": "job_path and build_number are required"})
    success, data, error = client.get_pipeline_stages(job_path, build_number)
    if not success:
        return json.dumps({"error": error or "Failed to fetch pipeline stages (may not be a Pipeline job)"})
    return json.dumps(data, default=str)


def _action_stage_log(client, job_path: Optional[str], build_number: Optional[int], node_id: Optional[str]) -> str:
    """Fetch per-stage log segment via Pipeline REST API."""
    if not job_path or not build_number or not node_id:
        return json.dumps({"error": "job_path, build_number, and node_id are required"})
    success, text, error = client.get_pipeline_stage_log(job_path, build_number, node_id)
    if not success:
        return json.dumps({"error": error or "Failed to fetch stage log"})
    return json.dumps({"log": text, "node_id": node_id})


def _action_build_logs(client, job_path: Optional[str], build_number: Optional[int]) -> str:
    """Fetch console output via Core REST API."""
    if not job_path or not build_number:
        return json.dumps({"error": "job_path and build_number are required"})
    success, text, error = client.get_build_console(job_path, build_number)
    if not success:
        return json.dumps({"error": error or "Failed to fetch build logs"})
    return json.dumps({"console": text})


def _action_test_results(client, job_path: Optional[str], build_number: Optional[int]) -> str:
    """Fetch test report via Core REST API."""
    if not job_path or not build_number:
        return json.dumps({"error": "job_path and build_number are required"})
    success, data, error = client.get_build_test_results(job_path, build_number)
    if not success:
        return json.dumps({"error": error or "No test results found (testReport may not exist)"})
    return json.dumps(data, default=str)


def _action_blue_ocean_run(client, pipeline_name: Optional[str], run_number: Optional[int], branch: Optional[str]) -> str:
    """Fetch run data via Blue Ocean REST API."""
    if not pipeline_name or not run_number:
        return json.dumps({"error": "pipeline_name and run_number are required"})
    success, data, error = client.get_blue_ocean_run(pipeline_name, run_number, branch=branch)
    if not success:
        return json.dumps({"error": error or "Failed to fetch Blue Ocean run data (Blue Ocean plugin may not be installed)"})
    return json.dumps(data, default=str)


def _action_blue_ocean_steps(client, pipeline_name: Optional[str], run_number: Optional[int], node_id: Optional[str], branch: Optional[str] = None) -> str:
    """Fetch step-level detail via Blue Ocean REST API."""
    if not pipeline_name or not run_number or not node_id:
        return json.dumps({"error": "pipeline_name, run_number, and node_id are required"})
    success, data, error = client.get_blue_ocean_steps(pipeline_name, run_number, node_id, branch=branch)
    if not success:
        return json.dumps({"error": error or "Failed to fetch Blue Ocean steps"})
    return json.dumps(data, default=str)


def _action_trace_context(user_id: str, event_id: Optional[int], job_path: Optional[str] = None, build_number: Optional[int] = None) -> str:
    """Extract or look up OTel W3C Trace Context for a deployment."""
    # First try: look up from stored deployment event
    if event_id and user_id:
        try:
            from utils.db.connection_pool import db_pool
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """SELECT trace_id, span_id, build_url, service, commit_sha
                           FROM jenkins_deployment_events
                           WHERE id = %s AND user_id = %s""",
                        (event_id, user_id),
                    )
                    row = cursor.fetchone()
                    if row and row[0]:
                        return json.dumps({
                            "trace_id": row[0],
                            "span_id": row[1],
                            "build_url": row[2],
                            "service": row[3],
                            "commit_sha": row[4],
                            "source": "stored_event",
                        })
        except Exception as e:
            logger.warning("trace_context DB lookup error: %s", e)

    # Second try: fetch from Jenkins build data via Core API
    if job_path and build_number and user_id:
        client = _get_client_for_user(user_id)
        if client:
            success, data, _ = client.get_build(job_path, build_number)
            if success and data:
                from connectors.jenkins_connector.api_client import JenkinsClient
                trace_ctx = JenkinsClient.extract_trace_context(data)
                if trace_ctx:
                    trace_ctx["source"] = "build_api"
                    return json.dumps(trace_ctx)

    return json.dumps({"error": "No trace context found. The OTel Jenkins plugin may not be installed or trace context was not forwarded."})
