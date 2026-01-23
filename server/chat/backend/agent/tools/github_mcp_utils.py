"""
Shared utilities for GitHub MCP tool integrations.

This module provides common functionality used by github_fix_tool and
github_apply_fix_tool to reduce code duplication.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def call_github_mcp_sync(
    tool_name: str,
    arguments: dict,
    user_id: str,
    timeout: int = 60
) -> dict:
    """
    Synchronous wrapper to call GitHub MCP tools.

    Uses existing RealMCPServerManager infrastructure to execute MCP tools
    in a synchronous context.

    Args:
        tool_name: Name of the MCP tool to call
        arguments: Arguments to pass to the tool
        user_id: User ID for authentication
        timeout: Timeout in seconds (default: 60)

    Returns:
        Dict containing either the result or an error key
    """
    from .mcp_tools import _mcp_manager, run_async_in_thread

    async def _async_call():
        await _mcp_manager.initialize_mcp_server("github", user_id)
        return await _mcp_manager.call_mcp_tool(
            server_type="github",
            tool_name=tool_name,
            arguments=arguments
        )

    try:
        result = run_async_in_thread(_async_call(), timeout=timeout)
        if result is not None:
            return result
        return {"error": "No response from MCP"}
    except Exception as e:
        logger.error(f"MCP call failed for {tool_name}: {e}")
        return {"error": str(e)}


def parse_mcp_response(result: dict) -> dict:
    """
    Parse MCP response content to extract data.

    Handles the nested content structure from MCP responses and
    extracts the actual JSON data.

    Args:
        result: Raw MCP response dict

    Returns:
        Parsed data dict or error dict
    """
    if not result:
        return {}

    if "error" in result:
        return {"error": result["error"]}

    content = result.get("content", [])
    if not content or not isinstance(content, list):
        return result

    first_content = content[0]
    if not isinstance(first_content, dict) or first_content.get("type") != "text":
        return result

    text = first_content.get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"text": text}


def parse_file_content_response(result: dict) -> Optional[str]:
    """
    Parse MCP file content response and decode base64 if needed.

    GitHub returns file content as base64 encoded, this function
    handles the decoding.

    Args:
        result: MCP response from get_file_contents

    Returns:
        Decoded file content string or None if parsing fails
    """
    import base64

    if "error" in result:
        logger.warning(f"Failed to get file content: {result['error']}")
        return None

    content = result.get("content", [])
    if not content or not isinstance(content, list):
        return None

    first_content = content[0]
    if not isinstance(first_content, dict) or first_content.get("type") != "text":
        return None

    text = first_content.get("text", "")
    try:
        data = json.loads(text)
        # GitHub returns base64 encoded content
        if data.get("encoding") == "base64" and data.get("content"):
            return base64.b64decode(data["content"]).decode("utf-8")
        if data.get("content"):
            return data["content"]
    except (json.JSONDecodeError, Exception) as e:
        logger.warning(f"Failed to parse file content: {e}")

    return None


def build_error_response(error: str, **kwargs) -> str:
    """
    Build a consistent JSON error response.

    Args:
        error: Error message
        **kwargs: Additional fields to include

    Returns:
        JSON string with error and success=False
    """
    response = {"error": error, "success": False}
    response.update(kwargs)
    return json.dumps(response)


def build_success_response(**kwargs) -> str:
    """
    Build a consistent JSON success response.

    Args:
        **kwargs: Fields to include in the response

    Returns:
        JSON string with success=True
    """
    response = {"success": True}
    response.update(kwargs)
    return json.dumps(response)
