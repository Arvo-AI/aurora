"""
IaC (Infrastructure as Code) module.
Contains core execution, command implementations, and user flows.
"""

from .iac_execution_core import (
    analyze_terraform_error,
    collect_terraform_context,
    initialize_terraform,
    parse_fmt_changes,
    parse_terraform_outputs,
    run_terraform_command,
    summarize_plan,
)
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
from .iac_user_flows import (
    check_github_connection,
    prepare_github_commit_suggestion,
    send_github_connection_toast,
)
from .iac_commands_tool import (
    iac_plan,
    iac_apply,
    iac_destroy,
)
from .iac_write_tool import (
    iac_write,
    get_terraform_directory,
)

__all__ = [
    # Execution core
    "analyze_terraform_error",
    "collect_terraform_context",
    "initialize_terraform",
    "parse_fmt_changes",
    "parse_terraform_outputs",
    "run_terraform_command",
    "summarize_plan",
    # Simple commands
    "iac_fmt",
    "iac_refresh",
    "iac_validate",
    # State commands
    "iac_outputs",
    "iac_state_list",
    "iac_state_pull",
    "iac_state_show",
    # Complex commands
    "iac_plan",
    "iac_apply",
    "iac_destroy",
    # Write operations
    "iac_write",
    "get_terraform_directory",
    # User flows
    "check_github_connection",
    "prepare_github_commit_suggestion",
    "send_github_connection_toast",
]
