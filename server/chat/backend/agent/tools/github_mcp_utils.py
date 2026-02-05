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

    GitHub MCP server returns file content in this format:
    - content[0]: status message with type='text'
    - content[1]: resource with type='resource' containing the actual file in resource.text

    Args:
        result: MCP response from get_file_contents

    Returns:
        Decoded file content string or None if parsing fails
    """
    import base64

    if "error" in result:
        logger.warning(f"Failed to get file content: {result['error']}")
        return None

    content_list = result.get("content", [])
    if not content_list or not isinstance(content_list, list):
        logger.warning(f"No content array in result: {result}")
        return None

    for idx, content_item in enumerate(content_list):
        if not isinstance(content_item, dict):
            continue
            
        content_type = content_item.get("type", "")
        
        # Handle 'resource' type - file content is in resource.text
        if content_type == "resource":
            resource = content_item.get("resource", {})
            if isinstance(resource, dict) and resource.get("text"):
                file_content = resource["text"]
                logger.info(f"[parse_file_content] Got file content from resource.text ({len(file_content)} chars)")
                return file_content
        
        # Handle 'text' type - might be JSON with base64 content or raw text
        elif content_type == "text":
            text = content_item.get("text", "")
            
            # Skip status messages
            if text.startswith("successfully") or text.startswith("Successfully"):
                continue
            
            # Try to parse as JSON (GitHub API format with base64)
            try:
                data = json.loads(text)
                if data.get("encoding") == "base64" and data.get("content"):
                    decoded = base64.b64decode(data["content"]).decode("utf-8")
                    logger.info(f"[parse_file_content] Decoded base64 content ({len(decoded)} chars)")
                    return decoded
                if data.get("content"):
                    return data["content"]
            except json.JSONDecodeError:
                # Raw text that looks like code
                if '\n' in text:
                    return text

    logger.warning(f"[parse_file_content] Could not extract file content from {len(content_list)} items")
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
