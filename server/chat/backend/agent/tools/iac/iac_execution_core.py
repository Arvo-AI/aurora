"""
Core execution engine for Infrastructure as Code operations.
Provides low-level command execution, parsing, and analysis utilities.
Designed to be tool-agnostic for future Pulumi support.
"""

import json
import logging
import re
import subprocess
from utils.terminal.terminal_run import terminal_run
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .iac_write_tool import get_terraform_directory
from ..output_sanitizer import sanitize_terraform_output
from routes.setup_terraform_environment import setup_terraform_environment_isolated

logger = logging.getLogger(__name__)


def run_terraform_command(
    command: str,
    working_dir: str,
    user_id: str,
    session_id: Optional[str] = None,
    timeout: int = 300,
) -> Dict[str, Any]:
    """Execute a Terraform command in the specified directory."""
    try:
        success, resource_id, isolated_env = setup_terraform_environment_isolated(user_id)
        # Only require success and isolated_env - resource_id can be None for OVH 
        # where project is discovered dynamically via CLI
        if not success or isolated_env is None:
            return {"error": "Failed to setup Terraform environment"}

        result = terminal_run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            env=isolated_env,
        )

        # For terraform plan -detailed-exitcode, exit code 2 means "changes detected" (success)
        # Exit code 0 = no changes, 1 = error, 2 = changes detected
        # However, sometimes terraform returns exit code 1 even with successful plans due to warnings
        # So we also check if the stdout contains a plan summary
        is_plan_with_detailed_exitcode = "terraform plan" in command and "-detailed-exitcode" in command
        has_plan_output = "Plan:" in result.stdout and ("to add" in result.stdout or "to change" in result.stdout or "to destroy" in result.stdout)
        
        is_successful = (
            result.returncode == 0 or
            (is_plan_with_detailed_exitcode and result.returncode == 2) or
            (is_plan_with_detailed_exitcode and has_plan_output)  # Plan succeeded even if exit code is 1
        )

        return {
            "command": command,
            "working_directory": working_dir,
            "return_code": result.returncode,
            "stdout": sanitize_terraform_output(result.stdout),
            "stderr": sanitize_terraform_output(result.stderr),
            "success": is_successful,
            "resource_id": resource_id,
        }

    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        logger.error(f"Error executing Terraform command: {e}")
        return {"error": f"Command execution failed: {str(e)}"}


def initialize_terraform(
    directory: str,
    user_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Initialize Terraform in the specified directory."""
    return run_terraform_command("terraform init", directory, user_id, session_id)


def collect_terraform_context(
    directory: Optional[str],
    user_id: Optional[str],
    session_id: Optional[str],
) -> Tuple[Optional[Path], List[Path], Optional[str]]:
    """Resolve terraform directory, ensure files exist, and detect required artifacts."""

    terraform_dir: Optional[Path] = None
    if directory and directory not in {".", ""}:
        terraform_dir = Path(directory).resolve()

    if terraform_dir is None:
        terraform_dir = get_terraform_directory(user_id, session_id)

    if terraform_dir is None:
        return None, [], "Terraform directory could not be determined"

    # Check if directory exists in terminal pod (where files are actually stored)
    check_dir_cmd = f"test -d {terraform_dir} && echo 'exists' || echo 'missing'"
    dir_check = terminal_run(check_dir_cmd, shell=True, capture_output=True, text=True, timeout=10)
    if dir_check.returncode != 0 or 'missing' in dir_check.stdout:
        return None, [], f"Terraform directory does not exist: {terraform_dir}"

    # List .tf files in terminal pod
    list_tf_cmd = f"ls {terraform_dir}/*.tf 2>/dev/null || true"
    tf_list = terminal_run(list_tf_cmd, shell=True, capture_output=True, text=True, timeout=10)
    if tf_list.returncode != 0 or not tf_list.stdout.strip():
        return terraform_dir, [], f"No Terraform files found in directory: {terraform_dir}"
    
    # Parse the list of .tf files
    tf_files = [Path(f.strip()) for f in tf_list.stdout.strip().split('\n') if f.strip()]
    
    if not tf_files:
        return terraform_dir, [], f"No Terraform files found in directory: {terraform_dir}"

    # Check for required files (like lambda.zip) by reading files in terminal pod
    required_files: List[str] = []
    for tf_file in tf_files:
        read_cmd = f"cat {tf_file}"
        read_result = terminal_run(read_cmd, shell=True, capture_output=True, text=True, timeout=10)
        if read_result.returncode == 0:
            content = read_result.stdout
            if "aws_lambda_function" in content:
                required_files.append("lambda.zip")

    # Check if required files exist in terminal pod
    missing_files = []
    for required_file in required_files:
        check_cmd = f"test -f {terraform_dir}/{required_file} && echo 'exists' || echo 'missing'"
        check_result = terminal_run(check_cmd, shell=True, capture_output=True, text=True, timeout=10)
        if check_result.returncode != 0 or 'missing' in check_result.stdout:
            missing_files.append(required_file)
    
    if missing_files:
        return (
            terraform_dir,
            tf_files,
            f"Missing required files in {terraform_dir}: {', '.join(missing_files)}",
        )

    return terraform_dir, tf_files, None


def parse_fmt_changes(fmt_stdout: str) -> List[str]:
    """Parse Terraform fmt output to extract list of formatted files."""
    return [line.strip() for line in fmt_stdout.splitlines() if line.strip()]


def parse_terraform_outputs(stdout: str) -> Dict[str, Any]:
    """Parse Terraform outputs from apply stdout."""
    outputs: Dict[str, Any] = {}
    lines = stdout.split("\n")

    for line in lines:
        if "=" in line and ("Apply complete!" in stdout or "Creation complete" in line):
            if line.strip().startswith("Outputs:"):
                continue
            if " = " in line:
                parts = line.split(" = ", 1)
                if len(parts) == 2:
                    key = parts[0].strip()
                    value = parts[1].strip().strip('"')
                    outputs[key] = value

    return outputs


def analyze_terraform_error(stderr: str, stdout: str) -> Dict[str, str]:
    """
    Analyze Terraform errors and provide suggested fixes.
    Pattern-based analysis that can be extended for other IaC tools.
    """
    error_analysis = {
        "error_type": "unknown",
        "suggested_fix": "Review the error and try again",
        "auto_fixable": False,
    }

    error_text = (stderr + stdout).lower()

    if "could not find image" in error_text or "image not found" in error_text:
        error_analysis.update(
            {
                "error_type": "invalid_image",
                "suggested_fix": "Update image to a valid format like 'debian-cloud/debian-11' or 'ubuntu-os-cloud/ubuntu-2204-lts'",
                "auto_fixable": True,
            }
        )

    elif "already exists" in error_text or "resource already exists" in error_text:
        error_analysis.update(
            {
                "error_type": "resource_conflict",
                "suggested_fix": "Add a random suffix to resource names or use unique naming",
                "auto_fixable": True,
            }
        )

    elif "permission denied" in error_text or "api not enabled" in error_text:
        error_analysis.update(
            {
                "error_type": "permission_error",
                "suggested_fix": "Ensure required GCP APIs are enabled and service account has proper permissions",
                "auto_fixable": False,
            }
        )

    elif "quota exceeded" in error_text or "insufficient quota" in error_text:
        error_analysis.update(
            {
                "error_type": "quota_error",
                "suggested_fix": "Request quota increase or try a different region/resource type",
                "auto_fixable": False,
            }
        )

    elif "invalid zone" in error_text or "zone does not exist" in error_text:
        error_analysis.update(
            {
                "error_type": "invalid_zone",
                "suggested_fix": "Use a valid zone like 'us-central1-a' or 'us-east1-b'",
                "auto_fixable": True,
            }
        )

    return error_analysis


def summarize_plan(plan_stdout: str) -> str:
    """
    Generate a human-friendly summary of Terraform plan output.
    Extracts resource changes and formats them for user confirmation.
    """
    default_summary = (
        "Terraform is ready to apply the planned infrastructure changes. Do you want to proceed?"
    )

    if not plan_stdout:
        return default_summary

    counts_match = re.search(
        r"Plan:\s+(?P<add>\d+)\s+to\s+add,\s+(?P<change>\d+)\s+to\s+change,\s+(?P<destroy>\d+)\s+to\s+destroy",
        plan_stdout,
    )

    add = change = destroy = None
    if counts_match:
        add = int(counts_match.group("add"))
        change = int(counts_match.group("change"))
        destroy = int(counts_match.group("destroy"))

    add_list: list[str] = []
    change_list: list[str] = []
    destroy_list: list[str] = []

    for line in plan_stdout.splitlines():
        l = line.strip()
        if not l.startswith("# "):
            continue
        if "will be created" in l or "will be added" in l:
            res = l.lstrip("# ").split(" will ")[0].strip()
            add_list.append(res)
        elif "will be destroyed" in l:
            res = l.lstrip("# ").split(" will ")[0].strip()
            destroy_list.append(res)
        elif (
            "will be updated" in l
            or "will be changed" in l
            or "will be replaced" in l
        ):
            res = l.lstrip("# ").split(" will ")[0].strip()
            change_list.append(res)

    parts: list[str] = []

    if add_list:
        add_preview = ", ".join(add_list[:5]) + (" and others" if len(add_list) > 5 else "")
        parts.append(f"Resources to be created: {add_preview}.")
    if change_list:
        change_preview = ", ".join(change_list[:5]) + (" and others" if len(change_list) > 5 else "")
        parts.append(f"Resources to be updated: {change_preview}.")
    if destroy_list:
        destroy_preview = ", ".join(destroy_list[:5]) + (" and others" if len(destroy_list) > 5 else "")
        parts.append(f"Resources to be destroyed: {destroy_preview}.")

    if not parts:
        count_segments = []
        if add and add > 0:
            count_segments.append(f"{add} to add")
        if change and change > 0:
            count_segments.append(f"{change} to change")
        if destroy and destroy > 0:
            count_segments.append(f"{destroy} to destroy")

        if count_segments:
            if len(count_segments) == 1:
                count_str = count_segments[0]
            else:
                count_str = ", ".join(count_segments[:-1]) + f" and {count_segments[-1]}"

            parts.append(f"Terraform plan detected {count_str}.")

    return "\n".join(parts) if parts else default_summary


__all__ = [
    "run_terraform_command",
    "initialize_terraform",
    "collect_terraform_context",
    "parse_fmt_changes",
    "parse_terraform_outputs",
    "analyze_terraform_error",
    "summarize_plan",
]
