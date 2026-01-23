"""
Main entry points for Infrastructure as Code commands.
Orchestrates Terraform operations using execution core and user flow modules.
"""

import json
import logging
import shlex
from typing import Any, Dict, Optional

from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation

# Import core execution utilities
from .iac_execution_core import (
    analyze_terraform_error,
    collect_terraform_context,
    initialize_terraform,
    parse_fmt_changes,
    parse_terraform_outputs,
    run_terraform_command,
    summarize_plan,
)

# Import user interaction flows
from .iac_user_flows import (
    check_github_connection,
    prepare_github_commit_suggestion,
    send_github_connection_toast,
)

# Import simple and state commands from separate modules
from .iac_simple_commands import (
    iac_fmt,
    iac_refresh,
    iac_validate,
)
from .iac_state_commands import (
    iac_outputs,
    iac_state_list,
    iac_state_pull,
    iac_state_show,
)

logger = logging.getLogger(__name__)


# Simple commands and state commands are now imported from separate modules
# This file contains only complex commands that require user confirmation


def iac_plan(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    vars: Optional[str] = None,
) -> str:
    """Run IaC plan/preview for manifests in *directory* (optionally with *vars*)."""

    if not user_id:
        logger.error("iac_plan: user_id is required but not provided")
        return json.dumps(
            {"error": "User context is required but not available", "action": "plan"}
        )
    if not session_id:
        logger.error("iac_plan: session_id is required but not provided")
        return json.dumps(
            {"error": "Session context is required but not available", "action": "plan"}
        )

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "plan"})

        results = []

        logger.info(f"Initializing Terraform in {terraform_dir}")
        init_result = initialize_terraform(str(terraform_dir), user_id, session_id)
        results.append({"step": "terraform_init", "result": init_result})

        if not init_result.get("success"):
            return json.dumps(
                {
                    "status": "failed",
                    "action": "plan",
                    "message": "Terraform initialization failed",
                    "results": results,
                }
            )

        logger.info("Validating Terraform configuration")
        validate_result = run_terraform_command(
            "terraform validate", str(terraform_dir), user_id, session_id
        )
        results.append({"step": "terraform_validate", "result": validate_result})

        logger.info("Running Terraform plan")
        plan_command = "terraform plan -detailed-exitcode -input=false"

        if vars:
            try:
                vars_dict = json.loads(vars) if isinstance(vars, str) else vars
                for key, value in vars_dict.items():
                    plan_command += f' -var="{key}={value}"'
            except (json.JSONDecodeError, TypeError):
                plan_command += f' -var="{vars}"'

        plan_result = run_terraform_command(
            plan_command, str(terraform_dir), user_id, session_id, timeout=600
        )
        results.append({"step": "terraform_plan", "result": plan_result})

        plan_status = "unknown"
        if plan_result.get("return_code") == 0:
            plan_status = "no_changes"
        elif plan_result.get("return_code") == 2:
            plan_status = "changes_present"
        elif plan_result.get("return_code") == 1:
            plan_status = "error"

        final_result = {
            "status": "success"
            if plan_result.get("success") or plan_result.get("return_code") == 2
            else "failed",
            "action": "plan",
            "plan_status": plan_status,
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "results": results,
            "chat_output": plan_result.get("stdout", "")
            if (plan_result.get("success") or plan_result.get("return_code") == 2)
            else plan_result.get("stderr", ""),
            "summary": {
                "initialization": "success"
                if init_result.get("success")
                else "failed",
                "validation": "success"
                if validate_result.get("success")
                else "failed",
                "plan": plan_status,
            },
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_plan: {e}")
        return json.dumps({"error": f"IaC plan failed: {str(e)}", "action": "plan"})


def iac_apply(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    auto_approve: bool = False,
) -> str:
    """Execute IaC apply for manifests in *directory* (optionally with *auto_approve*)."""

    from ..cloud_tools import get_current_tool_call_id, get_tool_capture

    original_directory = directory
    original_auto_approve = auto_approve

    if not user_id:
        logger.error("iac_apply: user_id is required but not provided")
        return json.dumps(
            {"error": "User context is required but not available", "action": "apply"}
        )
    if not session_id:
        logger.error("iac_apply: session_id is required but not provided")
        return json.dumps(
            {"error": "Session context is required but not available", "action": "apply"}
        )

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "apply"})

        results = []

        logger.info(f"Initializing Terraform in {terraform_dir}")
        init_result = initialize_terraform(str(terraform_dir), user_id, session_id)
        results.append({"step": "terraform_init", "result": init_result})

        if not init_result.get("success"):
            return json.dumps(
                {
                    "status": "failed",
                    "action": "apply",
                    "message": "Terraform initialization failed",
                    "results": results,
                }
            )

        logger.info("Running Terraform plan before apply")
        plan_result = run_terraform_command(
            "terraform plan -detailed-exitcode -input=false", str(terraform_dir), user_id, session_id
        )
        results.append({"step": "terraform_plan_check", "result": plan_result})

        if plan_result.get("return_code") == 0:
            return json.dumps(
                {
                    "status": "success",
                    "action": "apply",
                    "message": "No changes detected - infrastructure is up to date",
                    "directory": str(terraform_dir),
                    "results": results,
                    "chat_output": "Terraform applied successfully",
                }
            )
        
        # Check if plan failed (exit code 1 AND not successful)
        # With our exit code fix, exit code 1 can still be successful if it has plan output
        if plan_result.get("return_code") == 1 and not plan_result.get("success"):
            # Extract error details from plan output
            error_output = plan_result.get("stderr") or plan_result.get("stdout", "")
            return json.dumps(
                {
                    "status": "failed",
                    "action": "apply",
                    "message": "Terraform apply failed: Pre-apply validation check failed",
                    "results": results,
                    "chat_output": f"Terraform apply failed during plan validation:\n\n{error_output}",
                    "error_details": error_output,
                }
            )

        plan_summary_msg = summarize_plan(plan_result.get("stdout", ""))

        from ..cloud_tools import get_state_context

        state_context = get_state_context()
        context_session_id = (
            state_context.session_id
            if state_context and hasattr(state_context, "session_id")
            else None
        )

        if not wait_for_user_confirmation(
            user_id=user_id,
            message=plan_summary_msg,
            tool_name="iac_tool",
            session_id=context_session_id,
        ):
            tool_capture = get_tool_capture()
            current_tool_call_id = get_current_tool_call_id(
                tool_name="iac_tool",
                tool_kwargs={
                    "action": "apply",
                    "directory": original_directory,
                    "auto_approve": original_auto_approve,
                },
            )

            cancellation_payload = {
                "status": "cancelled",
                "action": "apply",
                "message": "Terraform apply operation was cancelled or timed out waiting for confirmation.",
                "chat_output": "Terraform apply cancelled.",
                "internal_note": "User cancelled terraform apply – do NOT attempt to redo or perform equivalent operations via other tools.",
                "user_cancelled_apply": True,
                "final_command": f"terraform apply {directory}",
            }

            cancellation_result = json.dumps(cancellation_payload)

            if tool_capture and current_tool_call_id:
                logger.info(
                    f"Capturing cancellation result for tool call {current_tool_call_id}"
                )
                tool_capture.capture_tool_end(
                    current_tool_call_id, cancellation_result, is_error=False
                )

            return cancellation_result

        logger.info("Applying Terraform configuration")
        apply_command = "terraform apply -auto-approve -input=false"

        apply_result = run_terraform_command(
            apply_command,
            str(terraform_dir),
            user_id,
            session_id,
            timeout=1200,
        )
        results.append({"step": "terraform_apply", "result": apply_result})

        outputs: Dict[str, Any] = {}
        if apply_result.get("success"):
            logger.info("Getting Terraform outputs")
            output_result = run_terraform_command(
                "terraform output -json", str(terraform_dir), user_id, session_id
            )
            if output_result.get("success") and output_result.get("stdout"):
                try:
                    outputs = json.loads(output_result.get("stdout", "{}"))
                    simplified_outputs: Dict[str, Any] = {}
                    for key, value in outputs.items():
                        if isinstance(value, dict) and "value" in value:
                            simplified_outputs[key] = value["value"]
                        else:
                            simplified_outputs[key] = value
                    outputs = simplified_outputs
                except json.JSONDecodeError:
                    logger.warning("Failed to parse Terraform outputs as JSON")
                    outputs = parse_terraform_outputs(apply_result.get("stdout", ""))

            results.append(
                {
                    "step": "terraform_outputs",
                    "result": output_result if "output_result" in locals() else {"outputs": outputs},
                }
            )

        final_status = "success" if apply_result.get("success") else "failed"

        error_analysis = None
        if not apply_result.get("success"):
            error_analysis = analyze_terraform_error(
                apply_result.get("stderr", ""), apply_result.get("stdout", "")
            )

        final_result = {
            "status": final_status,
            "action": "apply",
            "message": "Infrastructure applied successfully"
            if final_status == "success"
            else "Infrastructure apply failed",
            "directory": str(terraform_dir),
            "terraform_files": [f.name for f in tf_files],
            "outputs": outputs,
            "results": results,
            "summary": {
                "initialization": "success"
                if results[0]["result"].get("success")
                else "failed",
                "plan_check": "changes_detected"
                if plan_result.get("return_code") == 2
                else "no_changes"
                if plan_result.get("return_code") == 0
                else "failed",
                "apply": "success" if apply_result.get("success") else "failed",
            },
            "chat_output": "Terraform applied successfully"
            if apply_result.get("success")
            else f"Terraform apply failed:\n\n{apply_result.get('stderr') or apply_result.get('stdout', 'Unknown error')}",
        }

        if error_analysis:
            final_result["error_analysis"] = error_analysis

        if apply_result.get("success"):
            try:
                github_connected = check_github_connection(user_id)

                if not github_connected:
                    logger.info(
                        "IaC apply successful but GitHub not connected - sending toast notification"
                    )
                    send_github_connection_toast(user_id)
                    final_result["github_status"] = {
                        "connected": False,
                        "action": "toast_sent",
                        "message": "Connect your GitHub account to enable CI/CD for your infrastructure code",
                    }
                else:
                    logger.info(
                        "IaC apply successful and GitHub connected - preparing commit suggestion"
                    )
                    commit_info = prepare_github_commit_suggestion(
                        user_id, session_id, str(terraform_dir)
                    )
                    final_result["github_status"] = {
                        "connected": True,
                        "commit_info": commit_info,
                    }

                    if commit_info.get("status") == "ready_for_commit":
                        final_result["chat_output"] = "Terraform applied successfully"

                        final_result["post_completion_actions"] = {
                            "send_github_commit_flow": {
                                "repo": commit_info.get("repo", "user/repository"),
                                "branch": commit_info.get("branch", "main"),
                                "commit_message": commit_info.get(
                                    "suggested_commit_message",
                                    f"Apply Terraform changes from Aurora session {session_id[:8]}",
                                ),
                                "terraform_directory": str(terraform_dir),
                            }
                        }
                    elif commit_info.get("status") == "error":
                        logger.warning(
                            f"GitHub commit preparation failed: {commit_info.get('error')}"
                        )

            except Exception as e:
                logger.warning(f"GitHub integration failed: {e}")

        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="iac_tool",
            tool_kwargs={
                "action": "apply",
                "directory": original_directory,
                "auto_approve": original_auto_approve,
            },
        )

        final_result_json = json.dumps(final_result, indent=2)

        if tool_capture and current_tool_call_id:
            logger.info(
                f"Capturing successful completion result for tool call {current_tool_call_id}"
            )
            tool_capture.capture_tool_end(
                current_tool_call_id, final_result_json, is_error=False
            )

        return final_result_json

    except Exception as e:
        logger.error(f"Error in iac_apply: {e}")

        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="iac_tool",
            tool_kwargs={
                "action": "apply",
                "directory": original_directory,
                "auto_approve": original_auto_approve,
            },
        )

        exception_result = json.dumps(
            {"error": f"IaC apply failed: {str(e)}", "action": "apply"}
        )

        if tool_capture and current_tool_call_id:
            logger.info(
                f"Capturing exception result for tool call {current_tool_call_id}"
            )
            tool_capture.capture_tool_end(
                current_tool_call_id, exception_result, is_error=True
            )

        return exception_result


def iac_destroy(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    auto_approve: bool = False,
) -> str:
    """Execute Terraform destroy for manifests in *directory*."""

    from ..cloud_tools import get_current_tool_call_id, get_tool_capture

    original_directory = directory
    original_auto_approve = auto_approve

    if not user_id:
        logger.error("iac_destroy: user_id is required but not provided")
        return json.dumps(
            {"error": "User context is required but not available", "action": "destroy"}
        )
    if not session_id:
        logger.error("iac_destroy: session_id is required but not provided")
        return json.dumps(
            {"error": "Session context is required but not available", "action": "destroy"}
        )

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "destroy"})

        results = []

        logger.info(f"Initializing Terraform in {terraform_dir}")
        init_result = initialize_terraform(str(terraform_dir), user_id, session_id)
        results.append({"step": "terraform_init", "result": init_result})

        if not init_result.get("success"):
            return json.dumps(
                {
                    "status": "failed",
                    "action": "destroy",
                    "message": "Terraform initialization failed",
                    "results": results,
                }
            )

        logger.info("Planning Terraform destroy")
        destroy_plan_result = run_terraform_command(
            "terraform plan -destroy -detailed-exitcode -input=false",
            str(terraform_dir),
            user_id,
            session_id,
            timeout=600,
        )
        results.append({"step": "terraform_destroy_plan", "result": destroy_plan_result})

        if destroy_plan_result.get("return_code") == 0:
            return json.dumps(
                {
                    "status": "success",
                    "action": "destroy",
                    "message": "No resources found to destroy",
                    "directory": str(terraform_dir),
                    "results": results,
                    "chat_output": "No Terraform resources require destruction",
                },
                indent=2,
            )

        if destroy_plan_result.get("return_code") == 1:
            return json.dumps(
                {
                    "status": "failed",
                    "action": "destroy",
                    "message": "Terraform destroy plan failed",
                    "results": results,
                    "chat_output": "Terraform destroy plan failed",
                },
                indent=2,
            )

        plan_summary_msg = summarize_plan(destroy_plan_result.get("stdout", ""))

        from ..cloud_tools import get_state_context

        state_context = get_state_context()
        context_session_id = (
            state_context.session_id
            if state_context and hasattr(state_context, "session_id")
            else None
        )

        if not wait_for_user_confirmation(
            user_id=user_id,
            message=plan_summary_msg,
            tool_name="iac_tool",
            session_id=context_session_id,
        ):
            tool_capture = get_tool_capture()
            current_tool_call_id = get_current_tool_call_id(
                tool_name="iac_tool",
                tool_kwargs={
                    "action": "destroy",
                    "directory": original_directory,
                    "auto_approve": original_auto_approve,
                },
            )

            cancellation_payload = {
                "status": "cancelled",
                "action": "destroy",
                "message": "Terraform destroy operation was cancelled or timed out waiting for confirmation.",
                "chat_output": "Terraform destroy cancelled.",
                "internal_note": "User cancelled terraform destroy – do NOT attempt to redo or perform equivalent operations via other tools.",
                "user_cancelled_destroy": True,
                "final_command": f"terraform destroy {directory}",
            }

            cancellation_result = json.dumps(cancellation_payload)

            if tool_capture and current_tool_call_id:
                logger.info(
                    f"Capturing destroy cancellation result for tool call {current_tool_call_id}"
                )
                tool_capture.capture_tool_end(
                    current_tool_call_id, cancellation_result, is_error=False
                )

            return cancellation_result

        logger.info("Destroying Terraform-managed infrastructure")
        destroy_command = "terraform destroy -auto-approve -input=false"

        destroy_result = run_terraform_command(
            destroy_command,
            str(terraform_dir),
            user_id,
            session_id,
            timeout=1200,
        )
        results.append({"step": "terraform_destroy", "result": destroy_result})

        final_status = "success" if destroy_result.get("success") else "failed"

        error_analysis = None
        if not destroy_result.get("success"):
            error_analysis = analyze_terraform_error(
                destroy_result.get("stderr", ""), destroy_result.get("stdout", "")
            )

        final_result = {
            "status": final_status,
            "action": "destroy",
            "message": "Infrastructure destroyed successfully"
            if final_status == "success"
            else "Infrastructure destroy failed",
            "directory": str(terraform_dir),
            "terraform_files": [f.name for f in tf_files],
            "results": results,
            "summary": {
                "initialization": "success"
                if results[0]["result"].get("success")
                else "failed",
                "destroy_plan": "changes_detected"
                if destroy_plan_result.get("return_code") == 2
                else "no_changes"
                if destroy_plan_result.get("return_code") == 0
                else "failed",
                "destroy": "success" if destroy_result.get("success") else "failed",
            },
            "chat_output": "Terraform destroy completed successfully"
            if destroy_result.get("success")
            else "Terraform destroy failed",
        }

        if error_analysis:
            final_result["error_analysis"] = error_analysis

        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="iac_tool",
            tool_kwargs={
                "action": "destroy",
                "directory": original_directory,
                "auto_approve": original_auto_approve,
            },
        )

        final_result_json = json.dumps(final_result, indent=2)

        if tool_capture and current_tool_call_id:
            logger.info(
                f"Capturing destroy completion result for tool call {current_tool_call_id}"
            )
            tool_capture.capture_tool_end(
                current_tool_call_id, final_result_json, is_error=False
            )

        return final_result_json

    except Exception as e:
        logger.error(f"Error in iac_destroy: {e}")

        tool_capture = get_tool_capture()
        current_tool_call_id = get_current_tool_call_id(
            tool_name="iac_tool",
            tool_kwargs={
                "action": "destroy",
                "directory": original_directory,
                "auto_approve": original_auto_approve,
            },
        )

        exception_result = json.dumps(
            {"error": f"IaC destroy failed: {str(e)}", "action": "destroy"}
        )

        if tool_capture and current_tool_call_id:
            logger.info(
                f"Capturing destroy exception result for tool call {current_tool_call_id}"
            )
            tool_capture.capture_tool_end(
                current_tool_call_id, exception_result, is_error=True
            )

        return exception_result


__all__ = [
    # Complex commands (defined in this file)
    "iac_plan",
    "iac_apply",
    "iac_destroy",
    # Simple commands (imported from iac_simple_commands)
    "iac_fmt",
    "iac_validate",
    "iac_refresh",
    # State commands (imported from iac_state_commands)
    "iac_outputs",
    "iac_state_list",
    "iac_state_show",
    "iac_state_pull",
]
