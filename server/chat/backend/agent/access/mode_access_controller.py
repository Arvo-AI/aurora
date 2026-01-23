"""Centralized access-control helpers for Ask/Agent modes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

from langchain_core.tools import StructuredTool  # type: ignore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReadOnlyPolicy:
    """Configuration container for read-only behavior."""

    safe_tool_names: Tuple[str, ...]
    blocked_tool_prefixes: Tuple[str, ...]
    iac_safe_actions: Tuple[str, ...]


class ModeAccessController:
    """Enforces tooling restrictions based on chat mode."""

    READ_ONLY_MODE = "ask"
    
    # Read-only GitHub MCP tools that are safe in ask mode
    # These tools only read data and don't modify anything
    SAFE_GITHUB_MCP_TOOLS = {
        "mcp_list_commits",
        "mcp_get_commit",
        "mcp_get_file_contents",
        "mcp_search_code",
        "mcp_search_repositories",
        "mcp_list_branches",
        "mcp_get_repository_tree",
        "mcp_list_issues",
        "mcp_get_issue",
        "mcp_search_issues",
        "mcp_list_pull_requests",
        "mcp_get_pull_request",
    }
    
    _POLICY = ReadOnlyPolicy(
        safe_tool_names=(
            "web_search",
            "analyze_zip_file",
            "rag_index_zip",
        ),
        blocked_tool_prefixes=("mcp_",),  # Block MCP tools by default
        iac_safe_actions=(
            "plan",
            "state_list",
            "state_show",
            "state_pull",
            "outputs",
            "refresh",
        ),
    )

    @classmethod
    def is_read_only_mode(cls, mode: Optional[str]) -> bool:
        return (mode or "").strip().lower() == cls.READ_ONLY_MODE

    @classmethod
    def filter_tools(cls, mode: Optional[str], tools: Sequence[StructuredTool]) -> List[StructuredTool]:
        if not cls.is_read_only_mode(mode):
            return list(tools)

        filtered: List[StructuredTool] = []
        for tool in tools:
            name = getattr(tool, "name", "") or ""
            if name in cls._POLICY.safe_tool_names:
                filtered.append(tool)
                continue

            # Allow safe read-only GitHub MCP tools even in ask mode
            if name in cls.SAFE_GITHUB_MCP_TOOLS:
                LOGGER.info("ModeAccessController allowing read-only GitHub MCP tool %s in ask mode", name)
                filtered.append(tool)
                continue

            if any(name.startswith(prefix) for prefix in cls._POLICY.blocked_tool_prefixes):
                LOGGER.info("ModeAccessController dropped tool %s due to read-only mode prefix match", name)
                continue

            if name in {"iac_tool", "github_commit"}:
                LOGGER.info("ModeAccessController dropped tool %s for read-only mode", name)
                continue

            filtered.append(tool)

        return filtered

    @classmethod
    def ensure_iac_action_allowed(cls, mode: Optional[str], action: str) -> Tuple[bool, str]:
        if not cls.is_read_only_mode(mode):
            return True, ""

        normalized = (action or "").strip().lower()
        if normalized in cls._POLICY.iac_safe_actions:
            return True, ""

        message = (
            "IaC action '%s' is blocked in Ask mode. Switch to Agent mode to modify infrastructure." % normalized
        )
        return False, message

    @classmethod
    def ensure_cloud_command_allowed(cls, mode: Optional[str], is_read_only_command: bool, command: str) -> Tuple[bool, str]:
        if not cls.is_read_only_mode(mode) or is_read_only_command:
            return True, ""

        message = (
            "Command '%s' modifies infrastructure and is blocked in Ask mode. "
            "Send the request in Agent mode to proceed." % command
        )
        return False, message

    @classmethod
    def is_tool_allowed(cls, mode: Optional[str], tool_name: str) -> bool:
        if not cls.is_read_only_mode(mode):
            return True

        name = (tool_name or "").strip().lower()
        if name in cls._POLICY.safe_tool_names:
            return True

        # Allow safe read-only GitHub MCP tools
        if name in cls.SAFE_GITHUB_MCP_TOOLS:
            return True

        if any(name.startswith(prefix) for prefix in cls._POLICY.blocked_tool_prefixes):
            return False

        return name not in {"iac_tool", "github_commit"}


__all__ = ["ModeAccessController"]
