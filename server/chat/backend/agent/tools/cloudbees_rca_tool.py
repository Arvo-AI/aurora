"""
CloudBees CI RCA Tool - Thin wrapper around the Jenkins RCA tool.

CloudBees CI exposes the same REST API as Jenkins, so all investigation
actions (build details, pipeline stages, logs, Blue Ocean, etc.) are
identical.  The only difference is that credentials are stored under the
``cloudbees`` provider name instead of ``jenkins``.
"""

import json
import logging
from typing import Optional

from pydantic import BaseModel, Field
from typing import Literal

from .jenkins_rca_tool import (
    _action_recent_deployments,
)


logger = logging.getLogger(__name__)


class CloudBeesRCAArgs(BaseModel):
    action: Literal[
        "recent_deployments",
        "build_detail",
        "pipeline_stages",
        "stage_log",
        "build_logs",
        "test_results",
        "blue_ocean_run",
        "blue_ocean_steps",
        "flag_changes",
        "cross_controller_deployments",
        "controller_list",
    ] = Field(description="Investigation action to perform")
    job_path: Optional[str] = Field(default=None, description="Job path (e.g. 'folder/job-name')")
    build_number: Optional[int] = Field(default=None, description="Build number to investigate")
    pipeline_name: Optional[str] = Field(default=None, description="Pipeline name for Blue Ocean API")
    run_number: Optional[int] = Field(default=None, description="Run number for Blue Ocean API")
    branch: Optional[str] = Field(default=None, description="Branch name (Blue Ocean)")
    node_id: Optional[str] = Field(default=None, description="Node/stage ID for stage-level log or steps")
    service: Optional[str] = Field(default=None, description="Service name filter for recent_deployments")
    time_window_hours: Optional[int] = Field(default=24, description="Lookback window in hours for recent_deployments")
    app_id: Optional[str] = Field(default=None, description="Feature Management application ID for flag_changes")
    controller_url: Optional[str] = Field(default=None, description="Controller URL for OC mode (from controller_list or cross_controller_deployments results). Required for build introspection when connected via Operations Center.")


def is_cloudbees_connected(user_id: str) -> bool:
    """Check if CloudBees CI is connected for a user (legacy single-controller OR OC/PAT)."""
    from utils.auth.token_management import get_token_data
    # Legacy single-controller credentials
    creds = get_token_data(user_id, "cloudbees")
    if creds and creds.get("base_url") and creds.get("username") and creds.get("api_token"):
        return True
    # OC/PAT enterprise credentials
    oc_creds = get_token_data(user_id, "cloudbees_oc")
    if oc_creds and oc_creds.get("base_url") and oc_creds.get("api_token"):
        return True
    return False


def _get_client_for_cloudbees_user(user_id: str):
    """Build a JenkinsClient from the user's stored CloudBees credentials."""
    from utils.auth.token_management import get_token_data
    from connectors.jenkins_connector.api_client import JenkinsClient

    creds = get_token_data(user_id, "cloudbees")
    if not creds:
        logger.warning("[CLOUDBEES_RCA] No stored credentials for user %s", user_id)
        return None
    base_url = creds.get("base_url")
    username = creds.get("username")
    api_token = creds.get("api_token")
    if not base_url or not username or not api_token:
        logger.warning("[CLOUDBEES_RCA] Incomplete credentials for user %s (missing %s)", user_id,
                       ", ".join(k for k in ("base_url", "username", "api_token") if not creds.get(k)))
        return None
    return JenkinsClient(base_url=base_url, username=username, api_token=api_token)


def _get_oc_client_for_user(user_id: str):
    """Build a CloudBeesOCClient from the user's stored OC credentials."""
    from utils.auth.token_management import get_token_data
    from connectors.cloudbees_connector.oc_client import CloudBeesOCClient

    creds = get_token_data(user_id, "cloudbees_oc")
    if not creds:
        logger.debug("[CLOUDBEES_RCA] No OC credentials for user %s", user_id)
        return None
    base_url = creds.get("base_url")
    username = creds.get("username", "")
    api_token = creds.get("api_token")
    auth_mode = creds.get("auth_mode", "basic")
    if not base_url or not api_token:
        logger.debug("[CLOUDBEES_RCA] Incomplete OC credentials for user %s (missing %s)", user_id,
                     "base_url" if not base_url else "api_token")
        return None
    if auth_mode == "basic" and not username:
        logger.debug("[CLOUDBEES_RCA] Basic auth requires username for user %s", user_id)
        return None
    return CloudBeesOCClient(base_url=base_url, username=username, api_token=api_token, auth_mode=auth_mode)


def _get_fm_client_for_user(user_id: str):
    """Build a CloudBeesFMClient from the user's stored FM credentials."""
    from utils.auth.token_management import get_token_data
    from connectors.cloudbees_connector.fm_client import CloudBeesFMClient

    creds = get_token_data(user_id, "cloudbees_fm")
    if not creds:
        return None
    api_token = creds.get("api_token")
    if not api_token:
        return None
    return CloudBeesFMClient(api_token=api_token)


def cloudbees_rca(
    action: str,
    job_path: Optional[str] = None,
    build_number: Optional[int] = None,
    pipeline_name: Optional[str] = None,
    run_number: Optional[int] = None,
    branch: Optional[str] = None,
    node_id: Optional[str] = None,
    service: Optional[str] = None,
    time_window_hours: int = 24,
    app_id: Optional[str] = None,
    controller_url: Optional[str] = None,
    **kwargs,
) -> str:
    """Unified CloudBees CI investigation tool for RCA.

    Delegates to the same action implementations as jenkins_rca but resolves
    credentials from the ``cloudbees`` provider.
    """
    user_id = kwargs.get("user_id", "")

    if not user_id:
        return json.dumps({"error": "No user context. Run this from an authenticated session."})

    # --- Enterprise actions (OC / FM) ---
    if action == "flag_changes":
        if not app_id:
            return json.dumps({"error": "app_id is required for flag_changes action."})
        fm_client = _get_fm_client_for_user(user_id)
        if not fm_client:
            return json.dumps({
                "error": "Feature Management is not connected. Connect it in Connectors → CloudBees to enable flag change queries."
            })
        with fm_client:
            success, changes, error = fm_client.get_recent_flag_changes(
                app_id, since_hours=time_window_hours
            )
            if not success:
                return json.dumps({"error": error or "Failed to query Feature Management."})
            return json.dumps({"flag_changes": changes, "count": len(changes), "time_window_hours": time_window_hours})

    elif action == "cross_controller_deployments":
        oc_client = _get_oc_client_for_user(user_id)
        if not oc_client:
            return json.dumps({
                "error": "Operations Center is not connected. Connect it in Connectors → CloudBees to enable cross-controller queries."
            })
        with oc_client:
            success, builds, error = oc_client.query_recent_builds_across_controllers(
                service=service, time_window_hours=time_window_hours
            )
            if not success:
                return json.dumps({"error": error or "Failed to query Operations Center."})
            return json.dumps({"builds": builds, "count": len(builds), "time_window_hours": time_window_hours, "warnings": error})

    elif action == "controller_list":
        oc_client = _get_oc_client_for_user(user_id)
        if not oc_client:
            return json.dumps({
                "error": "Operations Center is not connected. Connect it in Connectors → CloudBees to enable cross-controller queries."
            })
        with oc_client:
            success, controllers, error = oc_client.discover_controllers()
            if not success:
                return json.dumps({"error": error or "Failed to discover controllers."})
            return json.dumps({"controllers": controllers, "count": len(controllers)})

    # --- Existing single-controller actions ---
    elif action == "recent_deployments":
        return _action_recent_deployments(user_id, service, time_window_hours, provider="cloudbees")

    # Resolve client: prefer legacy single-controller, then OC per-controller
    client = _get_client_for_cloudbees_user(user_id)
    if not client:
        oc_client = _get_oc_client_for_user(user_id)
        if not oc_client:
            return json.dumps({"error": "CloudBees CI is not connected. Configure credentials in Settings > Connectors > CloudBees CI."})
        # OC mode: route build introspection through a per-controller JenkinsClient
        if not controller_url:
            return json.dumps({
                "error": "controller_url is required for build introspection in Operations Center mode. "
                "Use controller_list or cross_controller_deployments first to discover controller URLs, "
                "then pass the relevant controller_url for this action."
            })
        try:
            client = oc_client.get_controller_client(controller_url)
        except ValueError as e:
            return json.dumps({"error": str(e)})

    from .jenkins_rca_tool import (
        _action_build_detail,
        _action_pipeline_stages,
        _action_stage_log,
        _action_build_logs,
        _action_test_results,
        _action_blue_ocean_run,
        _action_blue_ocean_steps,
    )

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
