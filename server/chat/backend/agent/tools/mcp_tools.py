"""
MCP (Model Context Protocol) integration for the Aurora platform.

This module handles all MCP server connections, tool discovery, and execution.
Extracted from cloud_tools.py to improve code organization.

Supported MCP servers:
- GitHub (github-mcp-server binary) - Active
- Context7 (npx @upstash/context7-mcp) - Active when OVH credentials present
- AWS - DISABLED (dependency conflicts with boto3/rsa)
- Azure - DISABLED (commented out throughout)
"""

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from langchain.tools import StructuredTool
from pydantic import BaseModel, Field

from server.chat.backend.agent.tools.mcp_schema_extractor import extract_mcp_tool_schema
from server.chat.backend.agent.tools.tool_permissions import is_tool_allowed_for_org
from server.chat.backend.agent.tools.tool_registry import gate_action
from server.chat.backend.agent.tools.tool_utils import cap_tool_output
from server.database.db_utils import get_db_connection
from server.utils.github_auth_router import get_github_installation_token

logger = logging.getLogger(__name__)

# ─── MCP server paths ────────────────────────────────────────────────────────

REAL_MCP_SERVER_PATHS = {
    "github": os.path.join(
        os.path.dirname(__file__), "../../../../mcp_servers/github-mcp-server"
    ),
    "context7": "context7",  # npx package
    # "aws": ...,   # DISABLED
    # "azure": ..., # DISABLED
}

# ─── Credential cache ─────────────────────────────────────────────────────────

_credentials_cache: Dict[str, Any] = {}
_credentials_cache_time: Dict[str, float] = {}
CREDENTIALS_CACHE_TTL = 300  # 5 minutes

# ─── MCP tools cache ──────────────────────────────────────────────────────────

_mcp_tools_cache: Dict[str, Any] = {}
_mcp_tools_cache_time: Dict[str, float] = {}
MCP_TOOLS_CACHE_TTL = 600  # 10 minutes

# ─── LangChain tools cache ────────────────────────────────────────────────────

_langchain_tools_cache: Dict[str, Any] = {}
_langchain_tools_cache_time: Dict[str, float] = {}
LANGCHAIN_TOOLS_CACHE_TTL = 600  # 10 minutes

# ─── GitHub tool allowlist ────────────────────────────────────────────────────

ALLOWED_GITHUB_TOOLS = {
    "mcp_add_comment_to_pending_review",
    "mcp_add_issue_comment",
    "mcp_assign_copilot_to_issue",
    "mcp_create_branch",
    "mcp_create_gist",
    "mcp_create_or_update_file",
    "mcp_create_pull_request",
    "mcp_create_repository",
    "mcp_delete_file",
    "mcp_dismiss_notification",
    "mcp_fork_repository",
    "mcp_get_code_scanning_alert",
    "mcp_get_commit",
    "mcp_get_dependabot_alert",
    "mcp_get_discussion",
    "mcp_get_discussion_comments",
    "mcp_get_file_contents",
    "mcp_get_gist",
    "mcp_get_global_security_advisory",
    "mcp_get_job_logs",
    "mcp_get_label",
    "mcp_get_latest_release",
    "mcp_get_me",
    "mcp_get_notification_details",
    "mcp_get_release_by_tag",
    "mcp_get_repository_tree",
    "mcp_get_secret_scanning_alert",
    "mcp_get_tag",
    "mcp_get_team_members",
    "mcp_get_teams",
    "mcp_issue_read",
    "mcp_issue_write",
    "mcp_label_write",
    "mcp_list_branches",
    "mcp_list_code_scanning_alerts",
    "mcp_list_commits",
    "mcp_list_dependabot_alerts",
    "mcp_list_discussion_categories",
    "mcp_list_discussions",
    "mcp_list_gists",
    "mcp_list_global_security_advisories",
    "mcp_list_issue_types",
    "mcp_list_issues",
    "mcp_list_label",
    "mcp_list_notifications",
    "mcp_list_org_repository_security_advisories",
    "mcp_list_pull_requests",
    "mcp_list_releases",
    "mcp_list_repository_security_advisories",
    "mcp_list_secret_scanning_alerts",
    "mcp_list_starred_repositories",
    "mcp_list_tags",
    "mcp_manage_notification_subscription",
    "mcp_manage_repository_notification_subscription",
    "mcp_mark_all_notifications_read",
    "mcp_merge_pull_request",
    "mcp_pull_request_read",
    "mcp_pull_request_review_write",
    "mcp_push_files",
    "mcp_request_copilot_review",
    "mcp_search_code",
    "mcp_search_issues",
    "mcp_search_orgs",
    "mcp_search_pull_requests",
    "mcp_search_repositories",
    "mcp_search_users",
    "mcp_star_repository",
    "mcp_sub_issue_write",
    "mcp_unstar_repository",
    "mcp_update_gist",
    "mcp_update_pull_request",
    "mcp_update_pull_request_branch",
}

# ─── Destructive tool detection ───────────────────────────────────────────────

DESTRUCTIVE_PREFIXES = (
    "create_",
    "delete_",
    "update_",
    "write_",
    "push_",
    "merge_",
    "fork_",
    "star_",
    "unstar_",
    "dismiss_",
    "manage_",
    "mark_",
    "assign_",
    "label_",
    "sub_issue_",
    "add_",
    "request_",
)

DESTRUCTIVE_TOOLS_EXPLICIT = {
    "mcp_create_or_update_file",
    "mcp_delete_file",
    "mcp_merge_pull_request",
    "mcp_push_files",
    "mcp_create_pull_request",
    "mcp_create_branch",
    "mcp_create_repository",
    "mcp_fork_repository",
    "mcp_issue_write",
    "mcp_pull_request_review_write",
}


def is_destructive_mcp_tool(tool_name: str) -> bool:
    """Return True if the MCP tool performs a write/delete operation."""
    if tool_name in DESTRUCTIVE_TOOLS_EXPLICIT:
        return True
    # Strip mcp_ prefix for prefix matching
    bare = tool_name[4:] if tool_name.startswith("mcp_") else tool_name
    return any(bare.startswith(p) for p in DESTRUCTIVE_PREFIXES)


def summarize_mcp_tool_action(tool_name: str, tool_input: Dict) -> str:
    """Generate a human-readable summary of a destructive MCP tool action."""
    bare = tool_name[4:] if tool_name.startswith("mcp_") else tool_name
    parts = [f"GitHub MCP: `{bare}`"]
    for key in ("owner", "repo", "path", "branch", "pullNumber", "issue_number"):
        if key in tool_input:
            parts.append(f"{key}={tool_input[key]}")
    return " | ".join(parts)


# ─── Credential helpers ───────────────────────────────────────────────────────


async def get_user_cloud_credentials(user_id: str, org_id: str) -> Dict[str, Any]:
    """Fetch cloud credentials for a user from the DB (cached 5 min)."""
    cache_key = f"{user_id}:{org_id}"
    now = time.time()
    if (
        cache_key in _credentials_cache
        and now - _credentials_cache_time.get(cache_key, 0) < CREDENTIALS_CACHE_TTL
    ):
        return _credentials_cache[cache_key]

    try:
        conn = await asyncio.to_thread(get_db_connection)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT provider, credentials
                    FROM connected_accounts
                    WHERE org_id = %s AND is_active = TRUE
                    """,
                    (org_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        creds: Dict[str, Any] = {}
        for provider, credentials in rows:
            creds[provider] = credentials

        _credentials_cache[cache_key] = creds
        _credentials_cache_time[cache_key] = now
        return creds

    except Exception as e:
        logging.error(f"Error fetching credentials for user {user_id}: {e}")
        return {}


def get_credentials_for_server(
    server_type: str, credentials: Dict[str, Any]
) -> Dict[str, str]:
    """Map provider credentials to environment variables for an MCP server."""
    env_vars: Dict[str, str] = {}

    if server_type == "github":
        github_creds = credentials.get("github", {})
        token = github_creds.get("access_token") or github_creds.get("token")
        if token:
            env_vars["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
            env_vars["GITHUB_TOKEN"] = token

    elif server_type == "aws":
        aws_creds = credentials.get("aws", {})
        if aws_creds.get("access_key_id"):
            env_vars["AWS_ACCESS_KEY_ID"] = aws_creds["access_key_id"]
            env_vars["AWS_SECRET_ACCESS_KEY"] = aws_creds.get("secret_access_key", "")
            if aws_creds.get("region"):
                env_vars["AWS_DEFAULT_REGION"] = aws_creds["region"]

    return env_vars


# ─── RealMCPServerManager ─────────────────────────────────────────────────────


class RealMCPServerManager:
    """Manages MCP server subprocess lifecycle and JSON-RPC communication."""

    def __init__(self):
        self.server_processes: Dict[str, Any] = {}
        self._server_locks: Dict[str, threading.Lock] = {}
        self._message_id = 0
        self._message_id_lock = threading.Lock()

    def get_server_lock(self, server_type: str) -> threading.Lock:
        if server_type not in self._server_locks:
            self._server_locks[server_type] = threading.Lock()
        return self._server_locks[server_type]

    def get_next_message_id(self) -> int:
        with self._message_id_lock:
            self._message_id += 1
            return self._message_id

    async def start_mcp_server(
        self,
        server_type: str,
        credentials: Optional[Dict[str, Any]] = None,
        github_token: Optional[str] = None,
    ) -> Optional[subprocess.Popen]:
        """Start an MCP server subprocess and return the Popen object."""
        try:
            # ── Check if already running ──────────────────────────────────────
            if server_type in self.server_processes:
                process_info = self.server_processes[server_type]
                if isinstance(process_info, dict):
                    process = process_info.get("process")
                    pid = process_info.get("pid")
                else:
                    # Handle legacy format where process_info is directly a subprocess.Popen object
                    process = process_info
                    pid = process.pid if process else None

                if process and process.poll() is None:  # Still running
                    logging.info(f" MCP server {server_type} already running with PID {pid}")
                    # For AWS and GitHub, force restart to ensure updated credentials
                    if server_type in ["aws", "github"]:
                        logging.info(f" Force restarting {server_type.upper()} MCP server for updated credentials")
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        # Close pipe handles explicitly to prevent ResourceWarning on GC.
                        # Python emits ResourceWarning on stderr for each unclosed pipe,
                        # which GCP Cloud Logging classifies as ERROR severity.
                        for _pipe in (process.stdin, process.stdout, process.stderr):
                            if _pipe is not None:
                                try:
                                    _pipe.close()
                                except Exception:
                                    pass
                        del self.server_processes[server_type]
                        logging.info(f" {server_type.upper()} MCP server restarted successfully")
                    else:
                        return process
                else:
                    # Process died, remove it
                    logging.info(f"DEBUG: {server_type} process is dead, removing")
                    del self.server_processes[server_type]

            # For AWS, always restart to ensure config files are created
            if server_type == "aws":
                if server_type in self.server_processes:
                    process_info = self.server_processes[server_type]
                    if isinstance(process_info, dict):
                        process = process_info.get("process")
                    else:
                        process = process_info

                    if process and process.poll() is None:
                        logging.info(f" Terminating existing AWS MCP server process {process.pid}")
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                        # Close pipe handles explicitly to prevent ResourceWarning on GC.
                        for _pipe in (process.stdin, process.stdout, process.stderr):
                            if _pipe is not None:
                                try:
                                    _pipe.close()
                                except Exception:
                                    pass
                    del self.server_processes[server_type]
                    logging.info(f" AWS MCP server process cleared")


            server_path = REAL_MCP_SERVER_PATHS.get(server_type)
            if not server_path:
                logging.warning(f"Unknown MCP server type: {server_type}")
                return None

            # Check if server file exists (skip for AWS, Azure, GitHub, and Context7, which use on-demand installation)
            if server_type not in ["aws", "azure", "github", "context7"]:
                PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
                full_path = os.path.join(PROJECT_ROOT, server_path) if not os.path.isabs(server_path) else server_path
                if not os.path.exists(full_path):
                    logging.warning(f"MCP server binary not found: {full_path}")
                    return None

            # ── Build command ─────────────────────────────────────────────────
            env = os.environ.copy()

            if server_type == "github":
                # Resolve GitHub token: explicit arg > OAuth creds > App installation token
                token = github_token
                if not token and credentials:
                    github_creds = credentials.get("github", {})
                    token = github_creds.get("access_token") or github_creds.get("token")
                if not token:
                    try:
                        token = await get_github_installation_token()
                    except Exception as e:
                        logging.warning(f"Could not get GitHub App token: {e}")

                if token:
                    env["GITHUB_PERSONAL_ACCESS_TOKEN"] = token
                    env["GITHUB_TOKEN"] = token

                server_binary = REAL_MCP_SERVER_PATHS["github"]
                if not os.path.isabs(server_binary):
                    PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
                    server_binary = os.path.join(PROJECT_ROOT, server_binary)

                if not os.path.exists(server_binary):
                    logging.error(f"GitHub MCP server binary not found: {server_binary}")
                    return None

                cmd = [server_binary, "stdio"]

            elif server_type == "context7":
                cmd = ["npx", "-y", "@upstash/context7-mcp"]

            elif server_type == "aws":
                # AWS MCP is disabled due to dependency conflicts
                logging.info("AWS MCP server is disabled")
                return None

            else:
                logging.warning(f"Unsupported MCP server type: {server_type}")
                return None

            # ── Inject credentials into env ───────────────────────────────────
            if credentials:
                cred_env = get_credentials_for_server(server_type, credentials)
                env.update(cred_env)

            # ── Spawn subprocess ──────────────────────────────────────────────
            logging.info(f" Starting MCP server {server_type}: {' '.join(cmd[:3])}")
            process = await asyncio.to_thread(
                subprocess.Popen,
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1,
            )

            # Give the process a moment to start
            startup_wait = 0.5
            await asyncio.sleep(startup_wait)

            # Check if process started successfully
            if process.poll() is not None:
                # Process has already exited
                stdout, stderr = process.communicate()
                logging.error(f" MCP server {server_type} failed to start. Exit code: {process.returncode}")
                logging.error(f"Stdout: {stdout}")
                logging.error(f"Stderr: {stderr}")
                return None

            # Store the process
            self.server_processes[server_type] = process

            return process

        except Exception as e:
            logging.error(f" Failed to start MCP server {server_type}: {str(e)}")
            return None

    async def send_mcp_message(self, server_type: str, message: Dict, timeout: int = None, _lock_held: bool = False) -> Optional[Dict]:
        """Send a message to an MCP server and get response.

        IMPORTANT: MCP uses stdio which is sequential - only one request can be
        processed at a time. We use a threading lock per server to serialize requests.

        Args:
            server_type: The MCP server to send to
            message: The JSON-RPC message dict
            timeout: Optional timeout in seconds
            _lock_held: Internal flag - set True if caller already holds the server lock
        """
        if timeout is None:
            timeout = 30 if server_type == "github" else 15

        def _send_sync():
            lock = self.get_server_lock(server_type)

            def _do_send():
                process_info = self.server_processes.get(server_type)
                if not process_info:
                    return None

                if isinstance(process_info, dict):
                    proc = process_info.get("process")
                else:
                    proc = process_info

                if not proc or proc.poll() is not None:
                    return None

                try:
                    proc.stdin.write(json.dumps(message) + "\n")
                    proc.stdin.flush()

                    start = time.time()
                    while time.time() - start < timeout:
                        line = proc.stdout.readline()
                        if not line:
                            if proc.poll() is not None:
                                return None
                            time.sleep(0.01)
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            response = json.loads(line)
                            # Skip notifications
                            if "method" in response and "id" not in response:
                                continue
                            return response
                        except json.JSONDecodeError:
                            continue
                    logging.warning(f"MCP {server_type} timeout after {timeout}s")
                    return None
                except (BrokenPipeError, OSError) as e:
                    logging.error(f"MCP {server_type} pipe error: {e}")
                    return None

            if _lock_held:
                return _do_send()
            else:
                with lock:
                    return _do_send()

        return await asyncio.to_thread(_send_sync)

    async def initialize_mcp_server(self, server_type: str) -> bool:
        """Send MCP initialize handshake."""
        init_msg = {
            "jsonrpc": "2.0",
            "id": self.get_next_message_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "clientInfo": {"name": "aurora", "version": "1.0.0"},
            },
        }
        response = await self.send_mcp_message(server_type, init_msg)
        if not response or "error" in response:
            return False

        # Send initialized notification
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        await self.send_mcp_message(server_type, notif)
        return True

    async def _initialize_mcp_server_with_timeout(self, server_type: str, timeout: int = 8) -> bool:
        """Initialize MCP server with timeout.

        Note: This is always called with the server lock held.
        """
        try:
            # Send initialization message according to MCP protocol
            init_message = {
                "jsonrpc": "2.0",
                "id": self.get_next_message_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "aurora", "version": "1.0.0"},
                },
            }

            response = await asyncio.wait_for(
                self.send_mcp_message(server_type, init_message, timeout=timeout, _lock_held=True),
                timeout=timeout,
            )

            if not response:
                logging.error(f" No response to MCP initialize for {server_type}")
                return False

            if "error" in response:
                logging.error(f" MCP initialize error for {server_type}: {response['error']}")
                return False

            # Send initialized notification (no response expected)
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await self.send_mcp_message(
                server_type, initialized_notification, timeout=5, _lock_held=True
            )

            logging.info(f" MCP server {server_type} initialized successfully")
            return True

        except asyncio.TimeoutError:
            logging.error(f" MCP initialize timed out for {server_type} after {timeout}s")
            return False
        except Exception as e:
            logging.error(f" MCP initialize exception for {server_type}: {e}")
            return False

    async def start_and_initialize_mcp_server(
        self,
        server_type: str,
        credentials: Optional[Dict[str, Any]] = None,
        github_token: Optional[str] = None,
    ) -> bool:
        """Start an MCP server and run the initialization handshake."""
        lock = self.get_server_lock(server_type)

        async def _start_init():
            process = await self.start_mcp_server(
                server_type, credentials=credentials, github_token=github_token
            )
            if not process:
                return False

            # Initialize with longer timeout for GitHub (Docker container) and Azure
            timeout = 30 if server_type == "github" else 8
            initialize_success = await self._initialize_mcp_server_with_timeout(server_type, timeout=timeout)
            if not initialize_success:
                logging.error(f" Failed to initialize MCP server {server_type}")
                # Clean up the process
                try:
                    process.terminate()
                    process.wait(timeout=2)
                except:
                    process.kill()
                # communicate() drains the pipes but does not close the underlying
                # file handles — close them explicitly to suppress ResourceWarning.
                for _pipe in (process.stdin, process.stdout, process.stderr):
                    if _pipe is not None:
                        try:
                            _pipe.close()
                        except Exception:
                            pass
                return False
            return True

        with lock:
            return await asyncio.to_thread(lambda: asyncio.run(_start_init()))

    async def list_mcp_tools(self, server_type: str) -> List[Dict]:
        """Get list of tools from a real MCP server."""
        try:
            # For all MCP servers, try standard methods
            list_tools_message = {
                "jsonrpc": "2.0",
                "id": self.get_next_message_id(),
                "method": "tools/list",
                "params": {}
            }

            response = await self.send_mcp_message(server_type, list_tools_message)

            if not response:
                logging.warning(f"No response from MCP server {server_type} for tools/list")
                return []

            if "error" in response:
                logging.error(f"Error listing tools from {server_type}: {response['error']}")
                return []

            tools = response.get("result", {}).get("tools", [])
            logging.info(f" Got {len(tools)} tools from {server_type} MCP server")
            return tools

        except Exception as e:
            logging.error(f" Error listing MCP tools from {server_type}: {str(e)}")
            return []

    async def call_mcp_tool(
        self, server_type: str, tool_name: str, tool_input: Dict
    ) -> Dict:
        """Call a tool on an MCP server."""
        try:
            # Filter out None values for GitHub tools to prevent API errors
            if server_type == "github":
                tool_input = {k: v for k, v in tool_input.items() if v is not None}

            call_message = {
                "jsonrpc": "2.0",
                "id": self.get_next_message_id(),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": tool_input},
            }

            response = await self.send_mcp_message(server_type, call_message, timeout=60)

            if not response:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"No response from MCP server {server_type} for tool {tool_name}",
                        }
                    ]
                }

            if "error" in response:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"MCP tool error: {response['error']}",
                        }
                    ]
                }

            result = response.get("result", {})

            # Handle resource-type responses (text in resource.text, base64 in resource.blob)
            content_list = result.get("content", [])
            normalized = []
            for item in content_list:
                if item.get("type") == "resource":
                    resource = item.get("resource", {})
                    if "text" in resource:
                        normalized.append({"type": "text", "text": resource["text"]})
                    elif "blob" in resource:
                        try:
                            decoded = base64.b64decode(resource["blob"]).decode("utf-8")
                            normalized.append({"type": "text", "text": decoded})
                        except Exception:
                            normalized.append({"type": "text", "text": f"[binary blob, {len(resource['blob'])} chars base64]"})
                    else:
                        normalized.append({"type": "text", "text": str(resource)})
                else:
                    normalized.append(item)

            if normalized:
                result = dict(result)
                result["content"] = normalized

            return result

        except Exception as e:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"Error calling MCP tool {tool_name} on {server_type}: {str(e)}"
                    }
                ]
            }

    def cleanup(self):
        """Clean up MCP server processes."""
        for server_type, process in self.server_processes.items():
            try:
                if isinstance(process, dict):
                    process_info = process
                    process = process_info.get("process")
                    pid = process_info.get("pid")
                else:
                    pid = process.pid if process else None

                if process and process.poll() is None:  # Process is still running
                    logging.info(f" Terminating MCP server {server_type}")
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logging.warning(f"Force killing MCP server {server_type}")
                        process.kill()
                        process.wait()
                    # Close pipe handles explicitly to prevent ResourceWarning on GC.
                    for _pipe in (process.stdin, process.stdout, process.stderr):
                        if _pipe is not None:
                            try:
                                _pipe.close()
                            except Exception:
                                pass
            except Exception as e:
                logging.error(f"Error terminating MCP server {server_type}: {str(e)}")

        self.server_processes.clear()

    async def _initialize_mcp_server_with_timeout(self, server_type: str, timeout: int = 8) -> bool:
        """Initialize MCP server with timeout.

        Note: This is always called with the server lock held.
        """
        try:
            # Send initialization message according to MCP protocol
            init_message = {
                "jsonrpc": "2.0",
                "id": self.get_next_message_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "aurora", "version": "1.0.0"},
                },
            }

            response = await asyncio.wait_for(
                self.send_mcp_message(server_type, init_message, timeout=timeout, _lock_held=True),
                timeout=timeout,
            )

            if not response:
                logging.error(f" No response to MCP initialize for {server_type}")
                return False

            if "error" in response:
                logging.error(f" MCP initialize error for {server_type}: {response['error']}")
                return False

            # Send initialized notification (no response expected)
            initialized_notification = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await self.send_mcp_message(
                server_type, initialized_notification, timeout=5, _lock_held=True
            )

            logging.info(f" MCP server {server_type} initialized successfully")
            return True

        except asyncio.TimeoutError:
            logging.error(f" MCP initialize timed out for {server_type} after {timeout}s")
            return False
        except Exception as e:
            logging.error(f" MCP initialize exception for {server_type}: {e}")
            return False


# ─── Module-level manager singleton ──────────────────────────────────────────

_mcp_manager: Optional[RealMCPServerManager] = None
_mcp_manager_lock = threading.Lock()


def get_mcp_manager() -> RealMCPServerManager:
    global _mcp_manager
    if _mcp_manager is None:
        with _mcp_manager_lock:
            if _mcp_manager is None:
                _mcp_manager = RealMCPServerManager()
                import atexit
                atexit.register(_mcp_manager.cleanup)
    return _mcp_manager


# ─── Public API ───────────────────────────────────────────────────────────────


def run_async_in_thread(coro):
    """Run an async coroutine in a new event loop in the current thread."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def get_real_mcp_tools_for_user(
    user_id: str,
    org_id: str,
    session_id: Optional[str] = None,
) -> List[StructuredTool]:
    """Return LangChain tools backed by real MCP servers for this user."""
    cache_key = f"{user_id}:{org_id}"
    now = time.time()

    if (
        cache_key in _langchain_tools_cache
        and now - _langchain_tools_cache_time.get(cache_key, 0) < LANGCHAIN_TOOLS_CACHE_TTL
    ):
        cached = _langchain_tools_cache[cache_key]
        if cached:
            return cached

    credentials = await get_user_cloud_credentials(user_id, org_id)

    # Determine which servers to start
    servers_to_start = []

    github_token = None
    has_github = bool(credentials.get("github"))
    if not has_github:
        try:
            github_token = await get_github_installation_token()
            has_github = bool(github_token)
        except Exception:
            pass
    if has_github:
        servers_to_start.append("github")

    has_ovh = bool(credentials.get("ovh"))
    if has_ovh:
        servers_to_start.append("context7")

    if not servers_to_start:
        return []

    manager = get_mcp_manager()

    # Start all servers in parallel
    start_tasks = [
        manager.start_and_initialize_mcp_server(
            s, credentials=credentials, github_token=github_token
        )
        for s in servers_to_start
    ]
    results = await asyncio.gather(*start_tasks, return_exceptions=True)

    active_servers = [
        s for s, ok in zip(servers_to_start, results) if ok is True
    ]

    if not active_servers:
        return []

    # Collect raw MCP tool definitions
    raw_tools_key = cache_key
    if (
        raw_tools_key in _mcp_tools_cache
        and now - _mcp_tools_cache_time.get(raw_tools_key, 0) < MCP_TOOLS_CACHE_TTL
    ):
        all_raw_tools = _mcp_tools_cache[raw_tools_key]
    else:
        tool_lists = await asyncio.gather(
            *[manager.list_mcp_tools(s) for s in active_servers]
        )
        all_raw_tools = []
        for server, tools in zip(active_servers, tool_lists):
            for t in tools:
                t["_server"] = server
            all_raw_tools.extend(tools)
        _mcp_tools_cache[raw_tools_key] = all_raw_tools
        _mcp_tools_cache_time[raw_tools_key] = now

    langchain_tools = create_mcp_langchain_tools(
        all_raw_tools, manager, user_id, org_id, session_id
    )

    _langchain_tools_cache[cache_key] = langchain_tools
    _langchain_tools_cache_time[cache_key] = now

    return langchain_tools


def create_mcp_langchain_tools(
    raw_tools: List[Dict],
    manager: RealMCPServerManager,
    user_id: str,
    org_id: str,
    session_id: Optional[str] = None,
) -> List[StructuredTool]:
    """Convert raw MCP tool definitions to LangChain StructuredTool objects."""
    langchain_tools = []

    for raw_tool in raw_tools:
        server_type = raw_tool.get("_server", "github")
        tool_name_raw = raw_tool.get("name", "")
        tool_name = f"mcp_{tool_name_raw}"
        description = raw_tool.get("description", "")

        # Filter GitHub tools to allowlist
        if server_type == "github" and tool_name not in ALLOWED_GITHUB_TOOLS:
            continue

        # Check org-level tool permissions
        if not is_tool_allowed_for_org(org_id, tool_name):
            continue

        # Build Pydantic input schema
        input_schema = extract_mcp_tool_schema(tool_name_raw, raw_tool, server_type)

        destructive = is_destructive_mcp_tool(tool_name)

        def make_tool_fn(
            _server=server_type,
            _raw_name=tool_name_raw,
            _tool_name=tool_name,
            _destructive=destructive,
        ):
            def tool_fn(**kwargs):
                async def _async_call():
                    if _destructive:
                        summary = summarize_mcp_tool_action(_tool_name, kwargs)
                        approved = await gate_action(
                            user_id=user_id,
                            org_id=org_id,
                            session_id=session_id,
                            tool_name=_tool_name,
                            action_summary=summary,
                        )
                        if not approved:
                            return f"Action declined by user: {summary}"

                    result = await manager.call_mcp_tool(_server, _raw_name, kwargs)
                    content = result.get("content", [])
                    texts = [
                        item.get("text", "")
                        for item in content
                        if item.get("type") == "text"
                    ]
                    output = "\n".join(texts)
                    return cap_tool_output(output, _tool_name)

                return run_async_in_thread(_async_call())

            return tool_fn

        try:
            lc_tool = StructuredTool(
                name=tool_name,
                description=description,
                args_schema=input_schema,
                func=make_tool_fn(),
                return_direct=False,
            )
            langchain_tools.append(lc_tool)
        except Exception as e:
            logging.warning(f"Could not create LangChain tool for {tool_name}: {e}")

    return langchain_tools


logging.info("REAL MCP INTEGRATION ACTIVE: Connected to official MCP servers using real protocol implementation")
