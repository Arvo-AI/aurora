"""
MCP (Model Context Protocol) integration for the Aurora platform.

This module handles all MCP server connections, tool discovery, and execution.
Extracted from cloud_tools.py to improve code organization and maintainability.
"""

from typing import Any, Dict, Optional, List, Callable
import threading
import json
import asyncio
import logging
import subprocess
import tempfile
import os
import time
import shutil
import concurrent.futures
from functools import wraps
from datetime import datetime
from pydantic import BaseModel, Field

# Import required classes
from langchain_core.tools import StructuredTool

# Real MCP client integration
REAL_MCP_ENABLED = True
REAL_MCP_SERVER_PATHS = {
    "aws": "aws-api-mcp-server",  # Not used for path, just for type
    # "azure": "npx-azure-mcp",  # Not used for path, just for type - DISABLED
    "github": "github-mcp-server",  # Not used for path, just for type
    "context7": "context7-mcp",  # Uses npx, not a local path
}

# --------------------------------------------------------------------------------------
# Destructive MCP tool detection - tools that create, modify, or delete resources
# --------------------------------------------------------------------------------------
_DESTRUCTIVE_MCP_PREFIXES = {
    "create_", "delete_", "update_", "push_", "merge_", "close_",
    "add_", "remove_", "cancel_", "rerun_", "fork_", "assign_",
    "request_", "submit_", "approve_", "dismiss_", "resolve_",
}

_DESTRUCTIVE_MCP_TOOLS = {
    # GitHub destructive operations
    "create_or_update_file", "push_files", "create_branch", "create_repository",
    "create_issue", "create_pull_request", "create_pull_request_review",
    "merge_pull_request", "update_pull_request_branch", "fork_repository",
    "add_issue_comment", "add_comment_to_pending_review", "add_project_item",
    "delete_file", "delete_pending_review", "cancel_workflow_run",
    "rerun_workflow", "rerun_workflow_failed_jobs", "assign_copilot_to_issue",
    "request_copilot_review", "update_issue", "update_project_item_field_value",
    "close_pull_request_review", "manage_pull_request_review",
}


def is_destructive_mcp_tool(tool_name: str) -> bool:
    """Check if an MCP tool is destructive (creates, modifies, or deletes resources)."""
    # Check exact match first
    if tool_name in _DESTRUCTIVE_MCP_TOOLS:
        return True
    # Check prefix patterns
    for prefix in _DESTRUCTIVE_MCP_PREFIXES:
        if tool_name.startswith(prefix):
            return True
    return False


def summarize_mcp_tool_action(tool_name: str, kwargs: dict) -> str:
    """Generate a human-readable summary of what the MCP tool will do."""
    action = tool_name.replace("_", " ")
    
    # Extract key identifiers from kwargs
    parts = [f"The tool will {action}"]
    
    if "owner" in kwargs and "repo" in kwargs:
        parts.append(f"in repository {kwargs['owner']}/{kwargs['repo']}")
    elif "repo" in kwargs:
        parts.append(f"in repository {kwargs['repo']}")
    
    if "branch" in kwargs:
        parts.append(f"on branch '{kwargs['branch']}'")
    if "path" in kwargs:
        parts.append(f"at path '{kwargs['path']}'")
    if "title" in kwargs:
        parts.append(f"with title '{kwargs['title']}'")
    if "pullNumber" in kwargs or "pull_number" in kwargs:
        pr_num = kwargs.get("pullNumber") or kwargs.get("pull_number")
        parts.append(f"for PR #{pr_num}")
    if "issue_number" in kwargs or "issueNumber" in kwargs:
        issue_num = kwargs.get("issue_number") or kwargs.get("issueNumber")
        parts.append(f"for issue #{issue_num}")
    
    return " ".join(parts) + ".\n\n"

# Real MCP server connection manager
class RealMCPServerManager:
    """Manages connections to real MCP servers via stdio transport."""
    
    def __init__(self):
        self.server_processes = {}
        self.server_sessions = {}
        self.message_id = 1
        self.lock = threading.Lock()
        # Threading locks per server type to serialize MCP requests (stdio is sequential)
        # Using threading.Lock instead of asyncio.Lock because calls come from different event loops
        self._server_locks: Dict[str, threading.Lock] = {}
        
        # Server configurations
        self.server_configs = {
            "aws": {
                "command": ["python", "-m", "awslabs.aws_api_mcp_server.server"],
                "description": "AWS API MCP Server"
            },
            # "azure": {
            #     "command": ["npx", "-y", "@azure/mcp@latest", "server", "start"],
            #     "description": "Azure MCP Server"
            # },
            "github": {
                "command": ["docker", "run", "-i", "--rm"],
                "description": "GitHub Official MCP Server (Docker)"
            },
            "context7": {
                "command": ["npx", "-y", "@upstash/context7-mcp"],
                "description": "Context7 MCP Server - Up-to-date docs for OVH CLI/Terraform"
            }
        }
        
    def get_next_message_id(self):
        """Get next message ID thread-safely."""
        with self.lock:
            self.message_id += 1
            return self.message_id
    
    def _get_server_lock(self, server_type: str) -> threading.Lock:
        """Get or create a threading lock for a server type to serialize MCP requests.
        
        Using threading.Lock instead of asyncio.Lock because MCP tool calls can come
        from different event loops/threads, and asyncio.Lock is bound to a single loop.
        """
        with self.lock:  # Thread-safe creation
            if server_type not in self._server_locks:
                self._server_locks[server_type] = threading.Lock()
            return self._server_locks[server_type]
            
    async def start_mcp_server(self, server_type: str, user_credentials: Dict = None) -> Optional[subprocess.Popen]:
        """Start a real MCP server process."""
        # AWS MCP server disabled - dependency conflicts with boto3/rsa versions
        if server_type == "aws":
            logging.info("AWS MCP server is disabled")
            return None
        try:
            # Check if server is already running
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
                    del self.server_processes[server_type]
                    logging.info(f" AWS MCP server process cleared")
            
            
            server_path = REAL_MCP_SERVER_PATHS.get(server_type)
            if not server_path:
                logging.warning(f"Unknown MCP server type: {server_type}")
                return None
                
            # Check if server file exists (skip for AWS, Azure, GitHub, and Context7, which use on-demand installation)
            if server_type not in ["aws", "azure", "github", "context7"]:
                PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
                full_path = os.path.abspath(os.path.join(PROJECT_ROOT, server_path))
                if not os.path.exists(full_path):
                    logging.warning(f"MCP server not found at: {full_path}")
                    return None
            # Start the real MCP server process with stdio transport
            # if server_type == "azure":
            #     # Azure MCP server uses npx command
            #     npx_cmd = shutil.which("npx") or "/usr/bin/npx"
            #     cmd = [npx_cmd, "-y", "@azure/mcp@latest", "server", "start"]
            if server_type == "aws":
                # AWS MCP server uses Python module
                python_cmd = shutil.which("python") or "/usr/local/bin/python"
                cmd = [python_cmd, "-m", "awslabs.aws_api_mcp_server.server"]
            elif server_type == "github":
                # Official GitHub MCP Server via Docker
                # Command is built dynamically below to include the token
                docker_cmd = shutil.which("docker") or "/usr/bin/docker"
                # Get GitHub token from credentials
                github_token = ""
                if user_credentials and "github" in user_credentials:
                    github_token = str(user_credentials["github"].get("access_token", ""))
                
                if not github_token:
                    logging.error(" GitHub token is required for GitHub MCP server")
                    return None
                
                # Build Docker command with token passed via -e flag
                # Using GITHUB_TOOLSETS=all to enable all 60+ tools
                cmd = [
                    docker_cmd, "run", "-i", "--rm",
                    "-e", f"GITHUB_PERSONAL_ACCESS_TOKEN={github_token}",
                    "-e", "GITHUB_TOOLSETS=all",
                    "ghcr.io/github/github-mcp-server"
                ]
                
                logging.info(f" GitHub MCP server command prepared (Docker with all toolsets)")
                logging.info(f" GitHub token configured (length: {len(github_token)}, starts with: {github_token[:10]}...)")
            elif server_type == "context7":
                # Context7 MCP server via npx - provides up-to-date docs for OVH CLI/Terraform
                npx_cmd = shutil.which("npx") or "/usr/bin/npx"
                cmd = [npx_cmd, "-y", "@upstash/context7-mcp"]
                logging.info(f" Context7 MCP server command prepared (npx)")
            else:
                # Fallback to node for other servers
                node_cmd = shutil.which("node") or "/usr/bin/node"
                cmd = [node_cmd, full_path]
            # Set up environment with cloud provider credentials
            env = os.environ.copy()
            # Add cloud provider credentials to environment
            if user_credentials:
                if server_type == "aws" and "aws" in user_credentials:
                    
                    aws_creds = user_credentials["aws"]
                    env["AWS_ACCESS_KEY_ID"] = str(aws_creds.get("access_key_id", ""))
                    env["AWS_SECRET_ACCESS_KEY"] = str(aws_creds.get("secret_access_key", ""))
                    env["AWS_DEFAULT_REGION"] = str(aws_creds.get("region", "us-east-1"))
                    env["AWS_REGION"] = str(aws_creds.get("region", "us-east-1")) 
                    
                    # Create AWS config directory in /tmp to ensure it's writable
                    aws_dir = "/tmp/.aws"
                    os.makedirs(aws_dir, exist_ok=True)
                    
                    # Create credentials file
                    credentials_content = f"""[default]
aws_access_key_id = {aws_creds.get("access_key_id", "")}
aws_secret_access_key = {aws_creds.get("secret_access_key", "")}
region = {aws_creds.get("region", "us-east-1")}
"""
                    if aws_creds.get("session_token"):
                        credentials_content += f"aws_session_token = {aws_creds.get('session_token', '')}\n"
                    
                    credentials_file = os.path.join(aws_dir, "credentials")
                    with open(credentials_file, "w") as f:
                        f.write(credentials_content)
                    
                    # Create config file
                    config_content = f"""[default]
region = {aws_creds.get("region", "us-east-1")}
output = json
"""
                    config_file = os.path.join(aws_dir, "config")
                    with open(config_file, "w") as f:
                        f.write(config_content)
                    
                    # Set environment variables to point to our custom AWS config location
                    env["AWS_SHARED_CREDENTIALS_FILE"] = credentials_file
                    env["AWS_CONFIG_FILE"] = config_file
                    
                # GitHub credentials are passed via Docker -e flag in the command itself
                # (handled above when building the docker command)
                elif server_type == "github":
                    # Token already passed in Docker command args, nothing to add to env
                    pass

            
            process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                bufsize=1
            )
            
            # Give the process a moment to start
            # Docker containers need more time, especially on first run (image pull)
            startup_wait = 2.0 if server_type == "github" else 0.5
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
            _lock_held: Internal flag - if True, skip acquiring lock (caller already holds it)
        """
        # Use longer timeout for GitHub (Docker) and Azure which can be slow
        if timeout is None:
            timeout = 15 if server_type == "github" else 5
        
        # Get the lock for this server type to serialize requests
        if _lock_held:
            # Caller already holds the lock, execute directly
            return await self._send_mcp_message_impl(server_type, message, timeout)
        
        server_lock = self._get_server_lock(server_type)
        with server_lock:
            return await self._send_mcp_message_impl(server_type, message, timeout)
    
    async def _send_mcp_message_impl(self, server_type: str, message: Dict, timeout: int) -> Optional[Dict]:
        """Internal implementation of send_mcp_message (assumes lock is held)."""
        try:
            server_info = self.server_processes.get(server_type)
            if not server_info or not server_info.get("process"):
                logging.error(f" MCP server {server_type} not running")
                return None
            
            proc = server_info["process"]
            message_id = message.get("id")
            
            # Check if process is still alive
            if proc.poll() is not None:
                logging.warning(f" MCP server {server_type} process has died, restarting...")
                # Remove the dead process
                del self.server_processes[server_type]
                # Try to restart the server (use impl directly since we already hold the lock)
                await self._initialize_mcp_server_impl(server_type)
                # Get the new process
                server_info = self.server_processes.get(server_type)
                if not server_info or not server_info.get("process"):
                    logging.error(f" Failed to restart MCP server {server_type}")
                    return None
                proc = server_info["process"]
            
            # Send message
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            
            if message_id:
                # Wait for response with timeout
                start_time = time.time()
                while time.time() - start_time < timeout:
                    line = proc.stdout.readline()
                    if not line:
                        continue
                    
                    try:
                        response = json.loads(line)
                        # Handle notifications (messages without id)
                        if "id" not in response:
                            logging.debug(f" Received notification from {server_type}: {response}")
                            continue
                        
                        if response.get("id") == message_id:
                            return response
                    except json.JSONDecodeError:
                        continue
                
                logging.warning(f"Timeout waiting for response to message ID {message_id} from {server_type}")
                return None
            else:
                # For notifications, don't wait for response
                return None
                
        except BrokenPipeError as e:
            logging.error(f" Broken pipe error with MCP server {server_type}: {str(e)}")
            # Remove the dead process and try to restart
            if server_type in self.server_processes:
                del self.server_processes[server_type]
            logging.info(f" Attempting to restart MCP server {server_type}...")
            await self._initialize_mcp_server_impl(server_type)
            return None
        except Exception as e:
            logging.error(f" Error sending message to MCP server {server_type}: {str(e)}")
            return None
    
    async def initialize_mcp_server(self, server_type: str, user_id: str = None) -> bool:
        """Initialize MCP server with handshake.
        
        Uses the same threading lock as send_mcp_message to prevent restarting
        the server while requests are in flight.
        """
        # AWS MCP server disabled - dependency conflicts with boto3/rsa versions
        if server_type == "aws":
            logging.info("AWS MCP server is disabled")
            return False
        
        # Acquire lock to prevent conflicts with in-flight requests
        server_lock = self._get_server_lock(server_type)
        
        with server_lock:
            return await self._initialize_mcp_server_impl(server_type, user_id)
    
    async def _initialize_mcp_server_impl(self, server_type: str, user_id: str = None) -> bool:
        """Internal implementation of initialize_mcp_server (called with lock held)."""
        try:
            # Get server configuration
            server_config = self.server_configs.get(server_type)
            if not server_config:
                logging.error(f" No server configuration found for {server_type}")
                return False
            
            # Set up credentials for the server
            if user_id:
                flat_credentials = self.get_credentials_for_server(server_type, user_id)
                if flat_credentials:
                    logging.info(f" {server_type.capitalize()} credentials configured for MCP server")
                    
                    # Convert flat credentials to nested format for start_mcp_server
                    if server_type == "aws":
                        credentials = {
                            "aws": {
                                "access_key_id": flat_credentials.get("AWS_ACCESS_KEY_ID"),
                                "secret_access_key": flat_credentials.get("AWS_SECRET_ACCESS_KEY"),
                                "session_token": flat_credentials.get("AWS_SESSION_TOKEN"),
                                "region": flat_credentials.get("AWS_DEFAULT_REGION", "us-east-1")
                            }
                        }
                    # elif server_type == "azure":
                    #     credentials = {
                    #         "azure": {
                    #             "client_id": flat_credentials.get("AZURE_CLIENT_ID"),
                    #             "client_secret": flat_credentials.get("AZURE_CLIENT_SECRET"),
                    #             "tenant_id": flat_credentials.get("AZURE_TENANT_ID"),
                    #             "subscription_id": flat_credentials.get("AZURE_SUBSCRIPTION_ID")
                    #         }
                    #     }
                    elif server_type == "github":
                        credentials = {
                            "github": {
                                "access_token": flat_credentials.get("GITHUB_PERSONAL_ACCESS_TOKEN"),
                                "api_url": flat_credentials.get("GITHUB_API_URL", "https://api.github.com")
                            }
                        }
                    else:
                        credentials = {}
                else:
                    credentials = {}
            else:
                credentials = {}
            
            # Use our start_mcp_server method instead of direct subprocess.Popen
            process = await self.start_mcp_server(server_type, credentials)
            if not process:
                logging.error(f" Failed to start MCP server {server_type}")
                return False
            
            # Store process info
            self.server_processes[server_type] = {
                "process": process,
                "pid": process.pid,
                "command": server_config["command"]
            }
            
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
                return False
            return True
            
        except Exception as e:
            logging.error(f" Error starting MCP server {server_type}: {str(e)}")
            return False
    
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
            if response and response.get("result"):
                tools = response["result"].get("tools", [])
                return tools
            else:
                # If tools/list fails, try alternative method names
                logging.warning(f"tools/list failed for {server_type}, trying alternative methods...")
                
                # Try alternative method names
                alternative_methods = ["tools/list", "list_tools", "tools.list"]
                for method in alternative_methods:
                    try:
                        alt_message = {
                            "jsonrpc": "2.0",
                            "id": self.get_next_message_id(),
                            "method": method,
                            "params": {}
                        }
                        alt_response = await self.send_mcp_message(server_type, alt_message)
                        if alt_response and alt_response.get("result"):
                            tools = alt_response["result"].get("tools", [])
                            logging.info(f" Found {len(tools)} tools using method {method}")
                            return tools
                    except Exception as e:
                        logging.debug(f"Method {method} failed: {e}")
                        continue
                
                logging.warning(f"No tools found in MCP server {server_type}")
                return []
                
        except Exception as e:
            logging.error(f" Error listing MCP tools from {server_type}: {str(e)}")
            return []
    
    async def call_mcp_tool(self, server_type: str, tool_name: str, arguments: Dict) -> Dict:
        """Call a tool on a real MCP server."""
        try:
            # Standard MCP method for all servers
            call_tool_message = {
                "jsonrpc": "2.0",
                "id": self.get_next_message_id(),
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": arguments
                }
            }
            
            response = await self.send_mcp_message(server_type, call_tool_message)
            if response and response.get("result"):
                result = response["result"]
                return result
            else:
                # For all servers, try alternative method names
                logging.warning(f"tools/call failed for {server_type}, trying alternative methods...")
                
                # Try alternative method names
                alternative_methods = ["tools/call", "call_tool", "tools.call"]
                for method in alternative_methods:
                    try:
                        alt_message = {
                            "jsonrpc": "2.0",
                            "id": self.get_next_message_id(),
                            "method": method,
                            "params": {
                                "name": tool_name,
                                "arguments": arguments
                            }
                        }
                        
                        alt_response = await self.send_mcp_message(server_type, alt_message)
                        if alt_response and alt_response.get("result"):
                            result = alt_response["result"]
                            return result
                    except Exception as e:
                        logging.debug(f"Method {method} failed: {e}")
                        continue
                
                # If all methods fail, return error
                error_msg = response.get("error", {}).get("message", "Unknown error") if response else "No response"
                logging.error(f" All MCP tool call methods failed for {server_type}: {error_msg}")
                return {
                    "content": [
                        {
                            "type": "text", 
                            "text": f"MCP server {server_type} error: {error_msg}"
                        }
                    ]
                }
                
        except Exception as e:
            logging.error(f" Error calling MCP tool {tool_name} on {server_type}: {str(e)}")
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
                    "capabilities": {
                        "roots": {
                            "listChanged": True
                        },
                        "sampling": {}
                    },
                    "clientInfo": {
                        "name": "Aurora Platform",
                        "version": "1.0.0"
                    }
                }
            }
            
            response = await self.send_mcp_message(server_type, init_message, timeout=timeout, _lock_held=True)
            if response and response.get("result"):
                logging.info(f" Successfully initialized MCP server {server_type}")
                
                # Send initialized notification
                initialized_message = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                }
                await self.send_mcp_message(server_type, initialized_message, timeout=timeout, _lock_held=True)
                
                return True
            else:
                logging.error(f" Failed to initialize MCP server {server_type}: {response}")
                return False
                
        except Exception as e:
            logging.error(f" Error initializing MCP server {server_type}: {str(e)}")
            return False

    def get_credentials_for_server(self, server_type: str, user_id: str = None) -> Dict[str, str]:
        """Get credentials for a specific MCP server from Aurora's database."""
        try:
            if not user_id:
                # Try to get user_id from context if not provided
                try:
                    from utils.cloud.cloud_utils import get_user_context
                    user_context = get_user_context()
                    user_id = user_context.get('user_id') if isinstance(user_context, dict) else user_context
                except ImportError:
                    pass
            
            if not user_id:
                logging.warning(f" No user ID found for {server_type} MCP server")
                return {}
            
            # Get user credentials from Aurora's database
            credentials = get_user_cloud_credentials(user_id)
            if not credentials:
                logging.warning(f" No credentials found for user {user_id}")
                return {}
            
            # Map credentials to environment variables for each server type
            if server_type == "aws":
                aws_creds = credentials.get("aws", {})
                return {
                    "AWS_ACCESS_KEY_ID": aws_creds.get("aws_access_key_id"),
                    "AWS_SECRET_ACCESS_KEY": aws_creds.get("aws_secret_access_key"),
                    "AWS_SESSION_TOKEN": aws_creds.get("aws_session_token"),
                    "AWS_DEFAULT_REGION": aws_creds.get("aws_region", "us-east-1"),
                    "AWS_REGION": aws_creds.get("aws_region", "us-east-1"),
                    "AWS_API_MCP_WORKING_DIR": "/tmp/aws-api-mcp-workdir",
                    "AWS_API_MCP_PROFILE_NAME": "default",
                    "READ_OPERATIONS_ONLY": "false",  # Allow write operations
                    "AWS_API_MCP_TELEMETRY": "false"
                }
            # elif server_type == "azure":
            #     azure_creds = credentials.get("azure", {})
            #     return {
            #         "AZURE_CLIENT_ID": azure_creds.get("client_id"),
            #         "AZURE_CLIENT_SECRET": azure_creds.get("client_secret"),
            #         "AZURE_TENANT_ID": azure_creds.get("tenant_id"),
            #         "AZURE_SUBSCRIPTION_ID": azure_creds.get("subscription_id")
            #     }
            elif server_type == "github":
                github_creds = credentials.get("github", {})
                token = github_creds.get("access_token", "")
                if token:
                    logging.info(f" Found GitHub token for MCP server (length: {len(token)})")
                else:
                    logging.warning(" No GitHub token found in credentials")
                return {
                    # @modelcontextprotocol/server-github uses GITHUB_PERSONAL_ACCESS_TOKEN
                    "GITHUB_PERSONAL_ACCESS_TOKEN": token,
                    "GITHUB_API_URL": github_creds.get("api_url", "https://api.github.com"),
                    "GITHUB_USERNAME": github_creds.get("username", "")
                }
            else:
                logging.warning(f" Unknown server type: {server_type}")
                return {}
                
        except Exception as e:
            logging.error(f" Error getting credentials for {server_type} MCP server: {str(e)}")
            return {}

# Global MCP server manager
_mcp_manager = RealMCPServerManager()

# Cache for user credentials to avoid repeated database calls
_user_credentials_cache = {}
_cache_expiry = {}
CACHE_DURATION = 300  # 5 minutes

# Cache for MCP tools to avoid repeated initialization
_mcp_tools_cache = {}
_mcp_tools_cache_expiry = {}
MCP_TOOLS_CACHE_DURATION = 600  # 10 minutes - longer cache for tools

# Cache for fully processed LangChain tools (including schemas)
_langchain_tools_cache = {}
_langchain_tools_cache_expiry = {}
LANGCHAIN_TOOLS_CACHE_DURATION = 600  # 10 minutes

def clear_credentials_cache(user_id: str = None):
    """Clear credentials cache for a specific user or all users."""
    global _user_credentials_cache, _cache_expiry, _mcp_tools_cache, _mcp_tools_cache_expiry
    global _langchain_tools_cache, _langchain_tools_cache_expiry
    if user_id:
        _user_credentials_cache.pop(user_id, None)
        _cache_expiry.pop(user_id, None)
        _mcp_tools_cache.pop(user_id, None)
        _mcp_tools_cache_expiry.pop(user_id, None)
        # Clear all variations of langchain tools cache keys for this user
        keys_to_remove = [k for k in _langchain_tools_cache.keys() if k.startswith(f"{user_id}:")]
        for key in keys_to_remove:
            _langchain_tools_cache.pop(key, None)
            _langchain_tools_cache_expiry.pop(key, None)
        logging.info(f"Cleared credentials and MCP tools cache for user {user_id}")
    else:
        _user_credentials_cache.clear()
        _cache_expiry.clear()
        _mcp_tools_cache.clear()
        _mcp_tools_cache_expiry.clear()
        _langchain_tools_cache.clear()
        _langchain_tools_cache_expiry.clear()
        logging.info("Cleared all credentials and MCP tools cache")

def get_user_cloud_credentials(user_id: str) -> Dict[str, Dict]:
    """Get user's cloud credentials from Aurora's authentication system with caching."""
    current_time = time.time()
    
    # Check cache first
    if user_id in _user_credentials_cache:
        if user_id in _cache_expiry and current_time < _cache_expiry[user_id]:
            logging.debug(f"Using cached credentials for user {user_id}")
            return _user_credentials_cache[user_id]
        else:
            # Cache expired
            _user_credentials_cache.pop(user_id, None)
            _cache_expiry.pop(user_id, None)
    
    credentials = {}
    
    try:
        # Try different import paths for database utilities
        try:
            from ...utils.db.db_utils import get_user_db_connection
        except ImportError:
            try:
                from utils.db.db_utils import get_user_db_connection
            except ImportError:
                # Fallback to stateless auth
                from utils.auth.stateless_auth import get_credentials_from_db
                
                # Use the fallback approach
                for provider in ["aws", "azure", "gcp", "github", "github_repo_selection", "ovh"]:
                    try:
                        provider_creds = get_credentials_from_db(user_id, provider)
                        if provider_creds:
                            credentials[provider] = provider_creds
                            logging.debug(f"Found {provider} credentials for user {user_id}")
                    except Exception as e:
                        logging.warning(f"Error fetching {provider} credentials: {str(e)}")
                        continue
                
                # Cache the results
                if credentials:
                    _user_credentials_cache[user_id] = credentials
                    _cache_expiry[user_id] = current_time + CACHE_DURATION
                    logging.debug(f"Cached credentials for user {user_id}: {list(credentials.keys())}")
                
                return credentials
        
        # Get database connection for this user
        conn = get_user_db_connection(user_id)
        if not conn:
            logging.debug(f"No database connection for user {user_id}")
            return {}
        
        def get_credentials_from_db_conn(user_id: str, provider: str) -> Optional[Dict]:
            """Get credentials for a specific provider from database."""
            try:
                with conn.cursor() as cursor:
                    # Cloud providers store credentials in connected_accounts table
                    cursor.execute("""
                        SELECT provider_data FROM connected_accounts 
                        WHERE user_id = %s AND provider = %s AND status = 'active'
                    """, (user_id, provider))
                    result = cursor.fetchone()
                    
                    if result and result[0]:
                        provider_data = json.loads(result[0]) if isinstance(result[0], str) else result[0]
                        logging.debug(f"Found {provider} credentials for user {user_id}")
                        return provider_data
                        
                return None
            except Exception as e:
                logging.warning(f"Error fetching {provider} credentials for user {user_id}: {str(e)}")
                return None
        
        # Check all providers
        for provider in ["aws", "azure", "gcp", "github", "ovh"]:
            try:
                provider_creds = get_credentials_from_db_conn(user_id, provider)
                if provider_creds:
                    credentials[provider] = provider_creds
                    logging.debug(f"Found {provider} credentials for user {user_id}")
            except Exception as e:
                logging.warning(f"Error fetching {provider} credentials: {str(e)}")
                continue
        
        # Cache the results
        if credentials:
            _user_credentials_cache[user_id] = credentials
            _cache_expiry[user_id] = current_time + CACHE_DURATION
            logging.debug(f"Cached credentials for user {user_id}: {list(credentials.keys())}")
        
        return credentials
        
    except Exception as e:
        logging.error(f"Error fetching cloud credentials for user {user_id}: {str(e)}")
        return {}

def run_async_in_thread(coro, timeout=60):
    """Run an async coroutine in a thread with configurable timeout."""
    
    def run_coro():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(run_coro)
        try:
            return future.result(timeout=timeout)  # Configurable timeout
        except concurrent.futures.TimeoutError:
            logging.error(f" MCP operation timed out after {timeout} seconds")
            return []
        except Exception as e:
            logging.error(f" Error in MCP operation: {str(e)}")
            return []

async def get_real_mcp_tools_for_user(user_id: str) -> List:
    """Get real MCP tools for a user based on their cloud credentials with caching."""
    import time
    
    try:
        # Check cache first
        current_time = time.time()
        
        # Get user's cloud credentials FIRST to check for cache invalidation
        credentials = get_user_cloud_credentials(user_id)
        
        # Check if we have cached tools
        if (user_id in _mcp_tools_cache and 
            user_id in _mcp_tools_cache_expiry and 
            current_time < _mcp_tools_cache_expiry[user_id]):
            cached_tools = _mcp_tools_cache[user_id]
            
            # IMPORTANT: If cache is empty but user now has credentials,
            # invalidate cache and reload (handles cross-container cache issue)
            has_github_creds = credentials and credentials.get("github", {}).get("access_token")
            has_aws_creds = credentials and (credentials.get("aws", {}).get("aws_access_key_id") or credentials.get("aws", {}).get("aws_secret_access_key"))
            
            # Check if OVH is connected but Context7 is not in cache
            has_ovh_creds = False
            has_context7_in_cache = any(t.get('server_type') == 'context7' or 'context7' in t.get('name', '').lower() for t in cached_tools)
            try:
                from utils.secrets.secret_ref_utils import get_user_token_data
                ovh_token_data = get_user_token_data(user_id, 'ovh')
                has_ovh_creds = bool(ovh_token_data)
            except Exception:
                pass
            
            should_invalidate = False
            if len(cached_tools) == 0 and (has_github_creds or has_aws_creds or has_ovh_creds):
                logging.info(f"Cache has 0 tools but user {user_id} now has credentials - invalidating stale cache")
                should_invalidate = True
            elif has_ovh_creds and not has_context7_in_cache:
                logging.info(f"OVH connected but Context7 not in cache for user {user_id} - invalidating cache")
                should_invalidate = True
            
            if should_invalidate:
                del _mcp_tools_cache[user_id]
                del _mcp_tools_cache_expiry[user_id]
            else:
                logging.info(f"Using cached MCP tools for user {user_id} ({len(cached_tools)} tools)")
                return cached_tools
        
        # Determine which MCP servers to start based on available credentials
        available_servers = []
        
        # Context7 MCP is only needed when OVH is connected (provides OVH CLI/Terraform docs)
        try:
            from utils.secrets.secret_ref_utils import get_user_token_data
            ovh_token_data = get_user_token_data(user_id, 'ovh')
            if ovh_token_data:
                available_servers.append("context7")
                logging.info(f"OVH connected for user {user_id}, enabling Context7 MCP")
        except Exception as e:
            logging.warning(f"Could not check OVH credentials: {e}")
        
        if credentials:
            # AWS MCP server disabled - dependency conflicts with boto3/rsa versions
            # if credentials.get("aws", {}).get("aws_access_key_id") or credentials.get("aws", {}).get("aws_secret_access_key"):
            #     available_servers.append("aws")
            # if credentials.get("azure", {}).get("client_id") or credentials.get("azure", {}).get("client_secret"):
            #     available_servers.append("azure")
            if credentials.get("github", {}).get("access_token"):
                available_servers.append("github")
        
        if not available_servers:
            logging.info(f"No cloud credentials found for user {user_id}, no MCP servers to load")
        
        logging.info(f" Found credentials for servers: {available_servers}")
        
        # Try to initialize MCP servers in parallel for better performance
        import asyncio
        
        async def init_server(server_type):
            """Initialize a single MCP server and return its tools."""
            try:
                logging.info(f"Initializing {server_type} MCP server...")
                success = await _mcp_manager.initialize_mcp_server(server_type, user_id)
                if success:
                    tools = await _mcp_manager.list_mcp_tools(server_type)
                    if tools:
                        # Add server_type to each tool for later identification
                        for tool in tools:
                            tool['server_type'] = server_type
                        logging.info(f" Added {len(tools)} tools from {server_type} MCP server")
                        return tools
                    else:
                        logging.warning(f" No tools found from {server_type} MCP server")
                else:
                    logging.warning(f" Failed to initialize {server_type} MCP server, continuing with native tools")
            except Exception as e:
                logging.error(f" Error with {server_type} MCP server: {str(e)}")
            return []
        
        # Initialize all servers in parallel
        tasks = [init_server(server_type) for server_type in available_servers]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Collect all tools from successful initializations
        all_tools = []
        for result in results:
            if isinstance(result, list):
                all_tools.extend(result)
            elif isinstance(result, Exception):
                logging.error(f" MCP server initialization exception: {str(result)}")
        
        # Cache the result
        _mcp_tools_cache[user_id] = all_tools
        _mcp_tools_cache_expiry[user_id] = current_time + MCP_TOOLS_CACHE_DURATION
        logging.info(f"Cached {len(all_tools)} MCP tools for user {user_id} (expires in {MCP_TOOLS_CACHE_DURATION}s)")
        
        return all_tools
        
    except Exception as e:
        logging.error(f" Error getting real MCP tools for user {user_id}: {str(e)}")
        return []

def create_mcp_langchain_tools(real_mcp_tools: List, tool_capture=None, send_tool_start=None, send_tool_completion=None, send_tool_error=None, run_async_in_thread=None) -> List:
    """Convert MCP tool definitions to LangChain tools."""
    tools = []
    
    if not real_mcp_tools:
        return tools
    
    # Define the allowed GitHub MCP tools
    # COMPLETE list of all Official GitHub MCP Server tools (70+ tools)
    # Updated to match https://github.com/github/github-mcp-server
    allowed_github_mcp_tools = {
        # ===== REPOSITORY TOOLS =====
        'create_or_update_file',        # Create or update a single file
        'create_repository',            # Create a new repository
        'create_branch',                # Create a new branch
        'delete_file',                  # Delete a file from repository
        'fork_repository',              # Fork a repository
        'get_file_contents',            # Get file or directory contents
        'get_commit',                   # Get commit details with diff
        'get_latest_release',           # Get latest release
        'get_release_by_tag',           # Get release by tag name
        'get_tag',                      # Get tag details
        'get_repository_tree',          # Get repository file tree
        'list_branches',                # List branches
        'list_commits',                 # List commits
        'list_releases',                # List releases
        'list_tags',                    # List tags
        'push_files',                   # Push multiple files in single commit
        'search_code',                  # Search code across repositories
        'search_repositories',          # Search repositories
        
        # ===== ISSUES TOOLS =====
        'add_issue_comment',            # Add comment to issue
        'assign_copilot_to_issue',      # Assign Copilot to issue
        'create_issue',                 # Create new issue (legacy)
        'get_issue',                    # Get issue details (legacy)
        'get_label',                    # Get a label
        'issue_read',                   # Get issue details (consolidated)
        'issue_write',                  # Create or update issue (consolidated)
        'list_issue_types',             # List available issue types
        'list_issues',                  # List issues
        'search_issues',                # Search issues
        'sub_issue_write',              # Add/remove/reprioritize sub-issues
        'update_issue',                 # Update issue (legacy)
        
        # ===== LABELS TOOLS =====
        'label_write',                  # Create/update/delete labels
        'list_label',                   # List labels
        
        # ===== PULL REQUEST TOOLS =====
        'add_comment_to_pending_review',           # Add comment to pending review
        'add_pull_request_review_comment_to_pending_review',  # Add review comment
        'create_and_submit_pull_request_review',   # Create and submit review
        'create_pending_pull_request_review',      # Create pending review
        'create_pull_request',                     # Create new PR
        'create_pull_request_review',              # Create PR review (legacy)
        'get_pull_request',                        # Get PR details (legacy)
        'get_pull_request_comments',               # Get PR comments (legacy)
        'get_pull_request_files',                  # Get PR files (legacy)
        'get_pull_request_reviews',                # Get PR reviews (legacy)
        'get_pull_request_status',                 # Get PR status (legacy)
        'list_pull_requests',                      # List PRs
        'merge_pull_request',                      # Merge PR
        'pull_request_read',                       # Get PR details (consolidated)
        'pull_request_review_write',               # Write PR review operations
        'request_copilot_review',                  # Request Copilot review
        'search_pull_requests',                    # Search PRs
        'update_pull_request',                     # Edit PR
        'update_pull_request_branch',              # Update PR branch
        
        # ===== GITHUB ACTIONS / WORKFLOWS TOOLS =====
        'cancel_workflow_run',          # Cancel workflow run
        'delete_workflow_run_logs',     # Delete workflow logs
        'download_workflow_run_artifact',# Download workflow artifact
        'get_job_logs',                 # Get job logs
        'get_workflow_run',             # Get workflow run details
        'get_workflow_run_logs',        # Get workflow run logs
        'get_workflow_run_usage',       # Get workflow usage/billing
        'list_workflow_jobs',           # List workflow jobs
        'list_workflow_run_artifacts',  # List workflow artifacts
        'list_workflow_runs',           # List workflow runs
        'list_workflows',               # List workflows
        'rerun_failed_jobs',            # Rerun failed jobs
        'rerun_workflow_run',           # Rerun workflow
        'run_workflow',                 # Trigger workflow dispatch
        
        # ===== SECURITY / SCANNING TOOLS =====
        'get_code_scanning_alert',      # Get code scanning alert
        'list_code_scanning_alerts',    # List code scanning alerts
        'get_dependabot_alert',         # Get Dependabot alert
        'list_dependabot_alerts',       # List Dependabot alerts
        'get_secret_scanning_alert',    # Get secret scanning alert
        'list_secret_scanning_alerts',  # List secret scanning alerts
        'get_global_security_advisory', # Get GHSA advisory
        'list_global_security_advisories',        # List global advisories
        'list_org_repository_security_advisories',# List org security advisories
        'list_repository_security_advisories',    # List repo security advisories
        
        # ===== DISCUSSIONS TOOLS =====
        'get_discussion',               # Get discussion
        'get_discussion_comments',      # Get discussion comments
        'list_discussion_categories',   # List discussion categories
        'list_discussions',             # List discussions
        
        # ===== GISTS TOOLS =====
        'create_gist',                  # Create gist
        'get_gist',                     # Get gist content
        'list_gists',                   # List gists
        'update_gist',                  # Update gist
        
        # ===== PROJECTS TOOLS =====
        'add_project_item',             # Add item to project
        'delete_project_item',          # Delete item from project
        'get_project',                  # Get project details
        'get_project_field',            # Get project field
        'get_project_item',             # Get project item
        'list_project_fields',          # List project fields
        'list_project_items',           # List project items
        'list_projects',                # List projects
        'update_project_item',          # Update project item
        
        # ===== NOTIFICATIONS TOOLS =====
        'dismiss_notification',         # Mark notification read/done
        'get_notification_details',     # Get notification details
        'list_notifications',           # List notifications
        'manage_notification_subscription',              # Manage thread subscription
        'manage_repository_notification_subscription',   # Manage repo subscription
        'mark_all_notifications_read',  # Mark all as read
        
        # ===== USERS / TEAMS / ORGS TOOLS =====
        'get_me',                       # Get authenticated user profile
        'get_team_members',             # Get team members
        'get_teams',                    # Get user's teams
        'search_orgs',                  # Search organizations
        'search_users',                 # Search users
        
        # ===== STARS TOOLS =====
        'list_starred_repositories',    # List starred repos
        'star_repository',              # Star a repo
        'unstar_repository',            # Unstar a repo
        
        # ===== COPILOT TOOLS (Remote MCP Server only) =====
        'create_pull_request_with_copilot',  # Create PR with Copilot agent
        'get_copilot_space',            # Get Copilot Space
        'list_copilot_spaces',          # List Copilot Spaces
        'github_support_docs_search',   # Search GitHub docs
    }
    
    # Convert MCP tool definitions to LangChain tools
    for tool_def in real_mcp_tools:
        tool_name = tool_def.get('name', 'unknown')
        tool_description = tool_def.get('description', 'MCP tool')
        
        # Filter tools based on provider and allowed tool lists
        if tool_name.startswith('extension_az'):
            # Skip Azure CLI extension tools - use cloud_exec instead
            continue
        elif tool_name in ['call_aws', 'suggest_aws_commands']:
            # Keep AWS MCP tools - they're useful for AWS operations
            pass  # Don't skip these
        elif 'azure' in tool_name.lower():
            # Skip ALL Azure MCP tools - Azure MCP disabled
            continue
        elif 'github' in tool_name.lower() and tool_name not in allowed_github_mcp_tools:
            # Skip GitHub MCP tools not in our allowed list
            continue
        # Create wrapper function for the MCP tool
        def create_mcp_tool_wrapper(server_type, original_tool_name):
            def mcp_tool_wrapper(**kwargs):
                """Wrapper that calls MCP server tools."""
                # Generate consistent tool_call_id for start/completion matching
                import hashlib
                import json
                tool_name = f"mcp_{original_tool_name}"
                # Use JSON serialization with sorted keys for deterministic hashing
                signature = f"{tool_name}_{json.dumps(kwargs, sort_keys=True, default=str)}"
                # Use longer hash (16 chars) to reduce collision risk
                signature_hash = hashlib.sha256(signature.encode()).hexdigest()[:16]
                tool_call_id = f"{tool_name}_{signature_hash}"
                
                # Send tool start notification with prefixed name for MCP tools
                try:
                    if send_tool_start:
                        send_tool_start(tool_name, kwargs, tool_call_id)
                except Exception as start_notify_err:
                    logging.warning(f"Failed to send start notification for {tool_name}: {start_notify_err}")
                
                # Check if this is a destructive MCP tool and ask for confirmation
                if is_destructive_mcp_tool(original_tool_name):
                    try:
                        from utils.cloud.infrastructure_confirmation import wait_for_user_confirmation
                        from utils.cloud.cloud_utils import get_user_context
                        from chat.backend.agent.tools.cloud_tools import get_state_context
                        
                        # Get user context
                        context = get_user_context()
                        user_id = context.get('user_id') if isinstance(context, dict) else context
                        state_context = get_state_context()
                        session_id = state_context.session_id if state_context and hasattr(state_context, 'session_id') else None
                        
                        if user_id:
                            summary_msg = summarize_mcp_tool_action(original_tool_name, kwargs)
                            if not wait_for_user_confirmation(
                                user_id=user_id,
                                message=summary_msg,
                                tool_name=tool_name,
                                session_id=session_id
                            ):
                                # User cancelled the action
                                cancellation_result = f"MCP tool {original_tool_name} cancelled by user."
                                try:
                                    if send_tool_completion:
                                        send_tool_completion(tool_name, cancellation_result, "cancelled", tool_call_id)
                                except Exception:
                                    pass
                                return cancellation_result
                    except Exception as confirm_err:
                        logging.warning(f"Failed to get confirmation for {tool_name}: {confirm_err}")
                        # Continue without confirmation if there's an error
                
                try:
                    # Handle both old 'command' and new 'cli_command' parameters
                    if 'kwargs' in kwargs:
                        # Extract the actual arguments from the nested kwargs
                        actual_kwargs = kwargs['kwargs']
                    else:
                        actual_kwargs = kwargs
                    
                    # Azure MCP server expects 'command' parameter for CLI tools - DISABLED
                    # if server_type == "azure" and original_tool_name.startswith("extension_az"):
                    #     # For Azure CLI extension tools, ensure command is passed correctly
                    #     if 'command' in actual_kwargs:
                    #         # Keep the command parameter as is for Azure
                    #         pass
                    #     elif 'cli_command' in actual_kwargs:
                    #         # Convert cli_command back to command for Azure
                    #         actual_kwargs['command'] = actual_kwargs.pop('cli_command')
                    
                    # Convert old 'command' parameter to 'cli_command' if needed (for AWS)
                    if server_type == "aws" and 'command' in actual_kwargs and 'cli_command' not in actual_kwargs:
                        actual_kwargs['cli_command'] = actual_kwargs.pop('command')
                    
                    # Filter out None values for GitHub - GitHub API rejects None/null parameters
                    # This prevents errors like "parameter sort is not of type string, is <nil>"
                    # We only do this for GitHub since other MCP servers may handle None differently
                    if server_type == "github":
                        actual_kwargs = {k: v for k, v in actual_kwargs.items() if v is not None}
                    
                    # Run the async call in a separate thread
                    result = run_async_in_thread(
                        _mcp_manager.call_mcp_tool(
                            server_type, 
                            original_tool_name, 
                            actual_kwargs
                        )
                    ) if run_async_in_thread else None
                    
                    # If AWS model is still initializing, retry with short backoff
                    if server_type == "aws" and original_tool_name == "suggest_aws_commands":
                        try:
                            def _extract_detail_text(res: Any) -> str:
                                if isinstance(res, dict):
                                    if res.get("error") is True:
                                        return str(res.get("detail", ""))
                                    if "content" in res:
                                        items = res.get("content") or []
                                        if isinstance(items, list) and items and isinstance(items[0], dict):
                                            return str(items[0].get("text", ""))
                                return str(res)

                            detail_text = _extract_detail_text(result)
                            if "initializing" in detail_text.lower():
                                for delay in [1, 2, 4]:
                                    logging.info(f"AWS MCP model not ready; retrying suggest_aws_commands in {delay}s")
                                    time.sleep(delay)
                                    result = run_async_in_thread(
                                        _mcp_manager.call_mcp_tool(
                                            server_type,
                                            original_tool_name,
                                            actual_kwargs
                                        )
                                    ) if run_async_in_thread else None
                                    detail_text = _extract_detail_text(result)
                                    if "initializing" not in detail_text.lower():
                                        break
                        except Exception as retry_err:
                            logging.warning(f"Retry/backoff failed for {original_tool_name}: {retry_err}")

                    # Add debug logging for MCP server results
                    if isinstance(result, dict) and "error" in result:
                        logging.error(f"{server_type.upper()} MCP server error: {result['error']}")
                    
                    # Handle server restart scenarios
                    if result is None:
                        logging.warning(f" MCP server {server_type} returned None, server may have restarted")
                        error_msg = f"MCP server {server_type} is restarting, please try again in a moment."
                        try:
                            if send_tool_error:
                                send_tool_error(original_tool_name, error_msg)
                        except Exception as notification_error:
                            logging.warning(f"Failed to send error notification for {original_tool_name}: {notification_error}")
                        return error_msg
                    
                    # Convert MCP result to string format expected by LangChain
                    final_result = ""
                    if isinstance(result, dict) and "content" in result:
                        content_items = result["content"]
                        if content_items and len(content_items) > 0:
                            first_content = content_items[0]
                            if isinstance(first_content, dict) and "text" in first_content:
                                final_result = first_content["text"]
                            else:
                                final_result = str(result)
                        else:
                            final_result = str(result)
                    else:
                        final_result = str(result)
                    
                    # Send tool completion notification with prefixed name
                    try:
                        if send_tool_completion:
                            send_tool_completion(tool_name, final_result, "completed", tool_call_id)
                    except Exception as notification_error:
                        logging.warning(f"Failed to send completion notification for {tool_name}: {notification_error}")
                    
                    return final_result
                        
                except Exception as e:
                    error_msg = f"Error calling MCP tool {original_tool_name}: {str(e)}"
                    logging.error(error_msg)
                    
                    # Enhanced error handling for GitHub MCP tools
                    if server_type == "github" and "not found" in str(e).lower():
                        enhanced_error = (
                            f"GitHub MCP Error: {str(e)}\n"
                            f"This might be due to:\n"
                            f"1. Repository access permissions - ensure your GitHub token has 'repo' scope\n"
                            f"2. Repository doesn't exist or is private\n"
                            f"3. Branch protection rules preventing direct commits\n"
                            f"4. Token has expired or been revoked\n"
                            f"\nTry re-authenticating with GitHub or using a Personal Access Token."
                        )
                        error_msg = enhanced_error
                    
                    # Send tool error notification with prefixed name
                    try:
                        if send_tool_error:
                            send_tool_error(f"mcp_{original_tool_name}", str(e))
                    except Exception as notification_error:
                        logging.warning(f"Failed to send error notification for mcp_{original_tool_name}: {notification_error}")
                    
                    return f"Error: {error_msg}"
            
            return mcp_tool_wrapper
        
        # Determine server type from tool name or tool_def
        tool_server_type = tool_def.get('server_type', '')
        if "aws" in tool_name.lower():
            server_type = "aws"
        elif tool_name in allowed_github_mcp_tools:
            server_type = "github"
        elif "github" in tool_name.lower():
            server_type = "github"
        elif tool_server_type == "context7" or "context7" in tool_name.lower() or tool_name in ['get-library-docs']:
            server_type = "context7"
        else:
            # server_type = "azure"  # Azure disabled
            continue  # Skip tools that would default to Azure
        
        # Create the wrapper function
        wrapper = create_mcp_tool_wrapper(server_type, tool_name)
        
        # Create LangChain StructuredTool from MCP tool
        
        # Create the StructuredTool with explicit schema for AWS tools
        if tool_name == "call_aws":
            # For AWS call_aws tool, explicitly define the schema
            class CallAwsArgs(BaseModel):
                command: str = Field(description="The AWS CLI command to execute (e.g., 'aws s3 ls')")
            
            structured_tool = StructuredTool.from_function(
                func=wrapper,
                name=f"mcp_{tool_name}",
                description=f"[MCP] {tool_description}",
                args_schema=CallAwsArgs
            )
        elif tool_name == "suggest_aws_commands":
            # For AWS suggest_aws_commands tool, explicitly define the schema
            class SuggestAwsCommandsArgs(BaseModel):
                query: str = Field(description="The query to suggest AWS commands for (e.g., 'list S3 buckets')")
            
            structured_tool = StructuredTool.from_function(
                func=wrapper,
                name=f"mcp_{tool_name}",
                description=f"[MCP] {tool_description}",
                args_schema=SuggestAwsCommandsArgs
            )
        elif tool_name == "get-library-docs":
            # Context7 get-library-docs tool - fetches documentation
            class GetLibraryDocsArgs(BaseModel):
                context7CompatibleLibraryID: str = Field(description="Context7 library ID (e.g., '/ovh/ovhcloud-cli' or '/ovh/terraform-provider-ovh')")
                topic: Optional[str] = Field(default=None, description="Topic to focus on (e.g., 'cloud kube create' or 'ovh_cloud_project_kube')")
                tokens: Optional[int] = Field(default=10000, description="Maximum tokens of documentation to return")
            
            structured_tool = StructuredTool.from_function(
                func=wrapper,
                name=f"mcp_context7_get_library_docs",
                description=f"[Context7] Fetches up-to-date documentation for OVH CLI or Terraform provider. Use when OVH commands fail or syntax is unclear. Use '/ovh/ovhcloud-cli' for CLI docs or '/ovh/terraform-provider-ovh' for Terraform docs.",
                args_schema=GetLibraryDocsArgs
            )
        else:
            # For other tools, extract schema from MCP server definition
            try:
                from chat.backend.agent.tools.mcp_schema_extractor import (
                    extract_mcp_tool_schema, 
                    get_github_tool_schemas,
                    log_tool_schema
                )
                
                # Log the tool schema for debugging
                log_tool_schema(tool_def)
                
                # Try to extract schema from MCP definition
                schema_model = extract_mcp_tool_schema(tool_def)
                
                # Fallback to hardcoded schemas for GitHub tools if extraction fails
                if not schema_model and server_type == "github":
                    github_schemas = get_github_tool_schemas()
                    schema_model = github_schemas.get(tool_name)
                    if schema_model:
                        logging.info(f" Using hardcoded schema for GitHub tool: {tool_name}")
                
                # Create the structured tool with or without schema
                if schema_model:
                    structured_tool = StructuredTool.from_function(
                        func=wrapper,
                        name=f"mcp_{tool_name}",
                        description=f"[MCP] {tool_description}",
                        args_schema=schema_model
                    )
                else:
                    # Fallback to no schema (will use function signature)
                    logging.warning(f" No schema available for tool {tool_name}, using generic wrapper")
                    structured_tool = StructuredTool.from_function(
                        func=wrapper,
                        name=f"mcp_{tool_name}",
                        description=f"[MCP] {tool_description}"
                    )
            except ImportError as ie:
                logging.warning(f"Could not import MCP schema extractor: {ie}")
                # Fallback to no schema
                structured_tool = StructuredTool.from_function(
                    func=wrapper,
                    name=f"mcp_{tool_name}",
                    description=f"[MCP] {tool_description}"
                )
        
        tools.append(structured_tool)
    
    return tools

# Register cleanup handler
import atexit
atexit.register(_mcp_manager.cleanup)

# Log MCP configuration status  
logging.info("REAL MCP INTEGRATION ACTIVE: Connected to official MCP servers using real protocol implementation")
