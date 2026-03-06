"""
Spinnaker RCA Tool — Query Spinnaker CD platform for root cause analysis.

Provides actions to check recent deployments, pipeline execution details,
application health, cluster status, and trigger pipelines (e.g., rollback).
"""

import json
import logging
from typing import Dict, Optional

from pydantic import BaseModel, Field
from typing import Literal

logger = logging.getLogger(__name__)


class SpinnakerRCAArgs(BaseModel):
    action: Literal[
        "recent_pipelines",
        "pipeline_detail",
        "application_health",
        "list_pipeline_configs",
        "trigger_pipeline",
        "execution_logs",
    ] = Field(description="The action to perform")
    application: Optional[str] = Field(default=None, description="Spinnaker application name")
    execution_id: Optional[str] = Field(default=None, description="Pipeline execution ID")
    pipeline_name: Optional[str] = Field(default=None, description="Pipeline name to trigger")
    parameters: Optional[Dict[str, str]] = Field(default=None, description="Pipeline trigger parameters")
    limit: Optional[int] = Field(default=25, description="Max results for listing")


def _get_client_for_user(user_id: str):
    """Build a SpinnakerClient from stored credentials."""
    from connectors.spinnaker_connector.client import get_spinnaker_client_for_user
    return get_spinnaker_client_for_user(user_id)


def is_spinnaker_connected(user_id: str) -> bool:
    """Check if Spinnaker is connected for a user."""
    from utils.auth.token_management import get_token_data
    creds = get_token_data(user_id, "spinnaker")
    return bool(creds and creds.get("base_url"))


def _action_recent_pipelines(client, application: Optional[str], limit: int) -> str:
    """Fetch recent pipeline executions."""
    try:
        if application:
            executions = client.list_pipeline_executions(application, limit=limit)
        else:
            # If no app specified, list applications and get recent executions across all
            apps = client.list_applications()
            executions = []
            for app in apps[:10]:  # Limit to first 10 apps to avoid timeout
                app_name = app.get("name", "")
                if app_name:
                    try:
                        app_execs = client.list_pipeline_executions(app_name, limit=5)
                        for ex in app_execs:
                            ex["_application"] = app_name
                        executions.extend(app_execs)
                    except Exception:
                        continue
            # Sort by start time descending
            executions.sort(key=lambda x: x.get("startTime", 0), reverse=True)
            executions = executions[:limit]

        # Summarize for the agent
        summary = []
        for ex in executions:
            summary.append({
                "application": ex.get("_application") or ex.get("application", ""),
                "name": ex.get("name", ""),
                "id": ex.get("id", ""),
                "status": ex.get("status", ""),
                "startTime": ex.get("startTime"),
                "endTime": ex.get("endTime"),
                "trigger": {
                    "type": ex.get("trigger", {}).get("type", ""),
                    "user": ex.get("trigger", {}).get("user", ""),
                },
                "stages_summary": [
                    {"name": s.get("name", ""), "status": s.get("status", "")}
                    for s in ex.get("stages", [])
                ],
            })
        return json.dumps({"executions": summary, "count": len(summary)})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch recent pipelines: {str(e)}"})


def _action_pipeline_detail(client, execution_id: Optional[str]) -> str:
    """Get full execution details with stage-by-stage status."""
    if not execution_id:
        return json.dumps({"error": "execution_id is required for pipeline_detail"})
    try:
        execution = client.get_pipeline_execution(execution_id)
        # Extract key fields
        detail = {
            "id": execution.get("id", ""),
            "application": execution.get("application", ""),
            "name": execution.get("name", ""),
            "status": execution.get("status", ""),
            "startTime": execution.get("startTime"),
            "endTime": execution.get("endTime"),
            "trigger": execution.get("trigger", {}),
            "parameters": execution.get("trigger", {}).get("parameters", {}),
            "stages": [
                {
                    "name": s.get("name", ""),
                    "type": s.get("type", ""),
                    "status": s.get("status", ""),
                    "startTime": s.get("startTime"),
                    "endTime": s.get("endTime"),
                    "context": {
                        k: v for k, v in s.get("context", {}).items()
                        if k in ("deploy.server.groups", "exception", "kato.last.task.id", "failureMessage",
                                 "deploymentDetails", "capacity", "targetHealthyDeployPercentage")
                    },
                    "outputs": s.get("outputs", {}),
                }
                for s in execution.get("stages", [])
            ],
        }
        return json.dumps(detail)
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch pipeline detail: {str(e)}"})


def _action_application_health(client, application: Optional[str]) -> str:
    """Get cluster + server group health for an application."""
    if not application:
        return json.dumps({"error": "application is required for application_health"})
    try:
        clusters = client.list_clusters(application)
        health = {"application": application, "clusters": clusters}
        return json.dumps(health)
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch application health: {str(e)}"})


def _action_list_pipeline_configs(client, application: Optional[str]) -> str:
    """List available pipeline definitions for an application."""
    if not application:
        return json.dumps({"error": "application is required for list_pipeline_configs"})
    try:
        configs = client.list_pipeline_configs(application)
        summary = [
            {
                "name": c.get("name", ""),
                "id": c.get("id", ""),
                "stages": [s.get("type", "") for s in c.get("stages", [])],
                "triggers": [t.get("type", "") for t in c.get("triggers", [])],
                "parameterDefinitions": c.get("parameterDefinitions", []),
            }
            for c in configs
        ]
        return json.dumps({"pipelines": summary, "count": len(summary)})
    except Exception as e:
        return json.dumps({"error": f"Failed to list pipeline configs: {str(e)}"})


def _action_execution_logs(client, execution_id: Optional[str]) -> str:
    """Get detailed logs/context for a failed execution."""
    if not execution_id:
        return json.dumps({"error": "execution_id is required for execution_logs"})
    try:
        execution = client.get_pipeline_execution(execution_id)
        failed_stages = []
        for stage in execution.get("stages", []):
            if stage.get("status") in ("TERMINAL", "FAILED_CONTINUE", "STOPPED"):
                failed_stages.append({
                    "name": stage.get("name", ""),
                    "type": stage.get("type", ""),
                    "status": stage.get("status", ""),
                    "context": stage.get("context", {}),
                    "outputs": stage.get("outputs", {}),
                    "tasks": [
                        {
                            "name": t.get("name", ""),
                            "status": t.get("status", ""),
                            "stageStart": t.get("stageStart"),
                            "stageEnd": t.get("stageEnd"),
                        }
                        for t in stage.get("tasks", [])
                    ],
                })
        return json.dumps({"execution_id": execution_id, "failed_stages": failed_stages})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch execution logs: {str(e)}"})


def spinnaker_rca(
    action: str,
    application: Optional[str] = None,
    execution_id: Optional[str] = None,
    pipeline_name: Optional[str] = None,
    parameters: Optional[Dict[str, str]] = None,
    limit: int = 25,
    **kwargs,
) -> str:
    """Unified Spinnaker investigation tool for RCA and interactive chat."""
    user_id = kwargs.get("user_id", "")

    if not user_id:
        return json.dumps({"error": "No user context. Run this from an authenticated session."})

    # Mutating action: trigger_pipeline requires human-in-the-loop confirmation
    if action == "trigger_pipeline":
        # Block in background/ask mode — no user to confirm
        try:
            from chat.backend.agent.tools.cloud_tools import get_state_context
            state = get_state_context()
            if state and getattr(state, "is_background", False):
                return json.dumps({"error": "trigger_pipeline is not available in background mode. Only read-only actions can run automatically."})
        except Exception:
            pass

        if not application or not pipeline_name:
            return json.dumps({"error": "application and pipeline_name are required for trigger_pipeline"})

        client = _get_client_for_user(user_id)
        if not client:
            return json.dumps({"error": "Spinnaker is not connected. Configure credentials in Settings > Connectors > Spinnaker."})

        try:
            from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation
            from chat.backend.agent.tools.cloud_tools import get_state_context

            session_id = getattr(get_state_context(), "session_id", None)

            summary = f"Trigger pipeline '{pipeline_name}' for application '{application}'"
            if parameters:
                summary += f"\nParameters: {json.dumps(parameters)}"

            confirmed = wait_for_user_confirmation(
                user_id=user_id,
                message=summary,
                tool_name="spinnaker_rca",
                session_id=session_id,
            )
            if not confirmed:
                return json.dumps({"status": "cancelled", "message": "Pipeline trigger cancelled by user"})
        except Exception as e:
            logger.error("[SPINNAKER_RCA] Confirmation flow failed, aborting trigger: %s", e)
            return json.dumps({"error": f"Failed to get user confirmation: {str(e)}"})

        try:
            result = client.trigger_pipeline(application, pipeline_name, parameters)
            return json.dumps({"status": "triggered", "result": result})
        except Exception as e:
            return json.dumps({"error": f"Failed to trigger pipeline: {str(e)}"})

    # Read-only actions
    client = _get_client_for_user(user_id)
    if not client:
        return json.dumps({"error": "Spinnaker is not connected. Configure credentials in Settings > Connectors > Spinnaker."})

    if action == "recent_pipelines":
        return _action_recent_pipelines(client, application, limit)
    elif action == "pipeline_detail":
        return _action_pipeline_detail(client, execution_id)
    elif action == "application_health":
        return _action_application_health(client, application)
    elif action == "list_pipeline_configs":
        return _action_list_pipeline_configs(client, application)
    elif action == "execution_logs":
        return _action_execution_logs(client, execution_id)
    else:
        return json.dumps({"error": f"Unknown action: {action}"})
