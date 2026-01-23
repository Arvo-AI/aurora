"""
Simple IaC commands for Terraform operations.
Includes formatting, validation, and state refresh operations.
These are non-destructive, read-mostly commands.
"""

import json
import logging
from typing import Optional

from .iac_execution_core import (
    collect_terraform_context,
    initialize_terraform,
    parse_fmt_changes,
    run_terraform_command,
)

logger = logging.getLogger(__name__)


def iac_fmt(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Format Terraform configuration files in the specified directory."""
    if not user_id:
        logger.error("iac_fmt: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "fmt"})
    if not session_id:
        logger.error("iac_fmt: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "fmt"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(directory, user_id, session_id)

        if dir_error:
            return json.dumps({"error": dir_error, "action": "fmt"})

        fmt_result = run_terraform_command(
            "terraform fmt -recursive", str(terraform_dir), user_id, session_id
        )
        formatted_files = parse_fmt_changes(fmt_result.get("stdout", ""))

        final_result = {
            "status": "success" if fmt_result.get("success") else "failed",
            "action": "fmt",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "formatted_files": formatted_files,
            "result": fmt_result,
            "chat_output": "Terraform files formatted successfully"
            if fmt_result.get("success")
            else fmt_result.get("stderr", fmt_result.get("stdout", "")),
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_fmt: {e}")
        return json.dumps({"error": f"IaC fmt failed: {str(e)}", "action": "fmt"})


def iac_validate(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Validate Terraform configuration files."""
    if not user_id:
        logger.error("iac_validate: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "validate"})
    if not session_id:
        logger.error("iac_validate: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "validate"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(directory, user_id, session_id)

        if dir_error:
            return json.dumps({"error": dir_error, "action": "validate"})

        results = []

        logger.info(f"Initializing Terraform in {terraform_dir}")
        init_result = initialize_terraform(str(terraform_dir), user_id, session_id)
        results.append({"step": "terraform_init", "result": init_result})

        if not init_result.get("success"):
            return json.dumps(
                {
                    "status": "failed",
                    "action": "validate",
                    "message": "Terraform initialization failed",
                    "results": results,
                }
            )

        logger.info("Validating Terraform configuration")
        validate_result = run_terraform_command(
            "terraform validate", str(terraform_dir), user_id, session_id
        )
        results.append({"step": "terraform_validate", "result": validate_result})

        final_result = {
            "status": "success" if validate_result.get("success") else "failed",
            "action": "validate",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "results": results,
            "chat_output": validate_result.get("stdout", "")
            if validate_result.get("success")
            else validate_result.get("stderr", ""),
            "summary": {
                "initialization": "success" if init_result.get("success") else "failed",
                "validation": "success" if validate_result.get("success") else "failed",
            },
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_validate: {e}")
        return json.dumps({"error": f"IaC validate failed: {str(e)}", "action": "validate"})


def iac_refresh(
    directory: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Refresh Terraform state from actual infrastructure."""
    if not user_id:
        logger.error("iac_refresh: user_id is required but not provided")
        return json.dumps({"error": "User context is required but not available", "action": "refresh"})
    if not session_id:
        logger.error("iac_refresh: session_id is required but not provided")
        return json.dumps({"error": "Session context is required but not available", "action": "refresh"})

    try:
        terraform_dir, tf_files, dir_error = collect_terraform_context(
            directory, user_id, session_id
        )

        if dir_error:
            return json.dumps({"error": dir_error, "action": "refresh"})

        results = []

        logger.info(f"Initializing Terraform in {terraform_dir}")
        init_result = initialize_terraform(str(terraform_dir), user_id, session_id)
        results.append({"step": "terraform_init", "result": init_result})

        if not init_result.get("success"):
            return json.dumps(
                {
                    "status": "failed",
                    "action": "refresh",
                    "message": "Terraform initialization failed",
                    "results": results,
                }
            )

        logger.info("Refreshing Terraform state")
        refresh_result = run_terraform_command(
            "terraform refresh", str(terraform_dir), user_id, session_id, timeout=600
        )
        results.append({"step": "terraform_refresh", "result": refresh_result})

        final_result = {
            "status": "success" if refresh_result.get("success") else "failed",
            "action": "refresh",
            "directory": str(terraform_dir),
            "terraform_files": [str(f.name) for f in tf_files],
            "results": results,
            "chat_output": refresh_result.get("stdout", "")
            if refresh_result.get("success")
            else refresh_result.get("stderr", ""),
            "summary": {
                "initialization": "success" if init_result.get("success") else "failed",
                "refresh": "success" if refresh_result.get("success") else "failed",
            },
        }

        return json.dumps(final_result, indent=2)

    except Exception as e:
        logger.error(f"Error in iac_refresh: {e}")
        return json.dumps({"error": f"IaC refresh failed: {str(e)}", "action": "refresh"})


__all__ = [
    "iac_fmt",
    "iac_validate",
    "iac_refresh",
]
