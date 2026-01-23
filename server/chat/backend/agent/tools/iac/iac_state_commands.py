"""
Terraform state inspection and query commands.
Handles outputs, state list, show, and pull operations.
These are read-only operations for querying infrastructure state.
"""

import json
import logging
import shlex
from typing import Any, List, Optional

from .iac_execution_core import (
    collect_terraform_context,
    run_terraform_command,
)

logger = logging.getLogger(__name__)


def iac_outputs(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    output_name: Optional[str] = None,
) -> str:
    """Retrieve Terraform outputs from the state."""
    if not user_id:
        logger.error("iac_outputs: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "outputs"})
    if not session_id:
        logger.error("iac_outputs: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "outputs"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "outputs"})

        command = "terraform output -json"
        if output_name:
            command = f"terraform output -json {shlex.quote(output_name)}"

        output_result = run_terraform_command(
            command, str(terraform_dir), user_id, session_id
        )

        outputs: Any = {}
        chat_output = output_result.get("stderr", "")
        if output_result.get("success"):
            raw = output_result.get("stdout", "")
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                logger.warning("Failed to parse terraform output JSON; returning raw output")
                parsed = raw

            if output_name:
                outputs = parsed
                chat_output = json.dumps(parsed, indent=2) if not isinstance(parsed, str) else parsed
            else:
                outputs = {}
                if isinstance(parsed, dict):
                    for key, value in parsed.items():
                        if isinstance(value, dict) and "value" in value:
                            outputs[key] = value["value"]
                        else:
                            outputs[key] = value
                else:
                    outputs = parsed
                chat_output = json.dumps(outputs, indent=2) if not isinstance(outputs, str) else outputs

        final_result = {
            "status": "success" if output_result.get("success") else "failed",
            "action": "outputs",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "output_name": output_name,
            "outputs": outputs,
            "result": output_result,
            "chat_output": chat_output,
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_outputs: {e}")
        return json.dumps({"error": f"IaC outputs failed: {str(e)}", "action": "outputs"})


def iac_state_list(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """List all resources in the Terraform state."""
    if not user_id:
        logger.error("iac_state_list: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "state_list"})
    if not session_id:
        logger.error("iac_state_list: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "state_list"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "state_list"})

        state_result = run_terraform_command(
            "terraform state list", str(terraform_dir), user_id, session_id
        )

        resources: List[str] = []
        if state_result.get("success"):
            resources = [line.strip() for line in state_result.get("stdout", "").splitlines() if line.strip()]

        final_result = {
            "status": "success" if state_result.get("success") else "failed",
            "action": "state_list",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "resources": resources,
            "result": state_result,
            "chat_output": "\n".join(resources)
            if state_result.get("success")
            else state_result.get("stderr", ""),
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_state_list: {e}")
        return json.dumps({"error": f"IaC state list failed: {str(e)}", "action": "state_list"})


def iac_state_show(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    state_address: Optional[str] = None,
) -> str:
    """Show details of a specific resource in the Terraform state."""
    if not user_id:
        logger.error("iac_state_show: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "state_show"})
    if not session_id:
        logger.error("iac_state_show: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "state_show"})
    if not state_address:
        logger.error("iac_state_show: state_address is required but not provided")
        return json.dumps({"error": "Terraform state address is required", "action": "state_show"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "state_show"})

        command = f"terraform state show {shlex.quote(state_address)}"
        state_result = run_terraform_command(
            command, str(terraform_dir), user_id, session_id, timeout=600
        )

        final_result = {
            "status": "success" if state_result.get("success") else "failed",
            "action": "state_show",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "state_address": state_address,
            "result": state_result,
            "chat_output": state_result.get("stdout", "")
            if state_result.get("success")
            else state_result.get("stderr", ""),
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_state_show: {e}")
        return json.dumps({"error": f"IaC state show failed: {str(e)}", "action": "state_show"})


def iac_state_pull(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Pull and display the entire Terraform state."""
    if not user_id:
        logger.error("iac_state_pull: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "state_pull"})
    if not session_id:
        logger.error("iac_state_pull: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "state_pull"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "state_pull"})

        state_result = run_terraform_command(
            "terraform state pull", str(terraform_dir), user_id, session_id, timeout=600
        )

        state_data: Any = {}
        chat_output = state_result.get("stderr", "")
        if state_result.get("success"):
            raw = state_result.get("stdout", "")
            try:
                state_data = json.loads(raw) if raw else {}
                chat_output = json.dumps(state_data, indent=2)
            except json.JSONDecodeError:
                logger.warning("Failed to parse terraform state pull JSON; returning raw output")
                state_data = raw
                chat_output = raw

        final_result = {
            "status": "success" if state_result.get("success") else "failed",
            "action": "state_pull",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "state": state_data,
            "result": state_result,
            "chat_output": chat_output,
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_state_pull: {e}")
        return json.dumps({"error": f"IaC state pull failed: {str(e)}", "action": "state_pull"})


__all__ = [
    "iac_outputs",
    "iac_state_list",
    "iac_state_show",
    "iac_state_pull",
]
