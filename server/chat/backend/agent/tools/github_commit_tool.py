import logging
import json
from typing import Dict, Any, Optional
from pydantic import BaseModel, Field
from pathlib import Path

logger = logging.getLogger(__name__)

class GitHubCommitArgs(BaseModel):
    repo: str = Field(description="The GitHub repository (e.g., 'owner/repo')")
    commit_message: str = Field(description="The commit message")
    branch: str = Field(default="main", description="The branch to commit to")
    push: bool = Field(default=True, description="Whether to push the commit to the remote repository")
    # Note: user_id and session_id are injected automatically by the with_forced_context decorator

def github_commit(
    repo: str,
    commit_message: str,
    branch: str = "main",
    push: bool = True,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None
) -> str:
    """
    Commits Terraform files to a GitHub repository using MCP tools.
    """
    
    
    try:
        # Get the Terraform directory for this session
        from .iac.iac_write_tool import get_terraform_directory
        
        # If session_id is 'current', find the most recent session with .tf files
        if session_id == 'current':
            base_dir = Path("/app/terraform_workdir")
            if user_id:
                user_dir = base_dir / user_id
                if user_dir.exists():
                    # Find session directories with .tf files, sorted by modification time
                    session_dirs_with_tf = []
                    for d in user_dir.iterdir():
                        if d.is_dir() and d.name.startswith('session_'):
                            # Check if this session has .tf files
                            tf_files = list(d.glob("*.tf"))
                            if tf_files:
                                session_dirs_with_tf.append(d)
                    
                    if session_dirs_with_tf:
                        # Sort by modification time, get most recent
                        latest_session = max(session_dirs_with_tf, key=lambda x: x.stat().st_mtime)
                        terraform_dir = str(latest_session)
                    else:
                        terraform_dir = None
                else:
                    terraform_dir = None
            else:
                terraform_dir = None
        else:
            terraform_dir = get_terraform_directory(user_id, session_id)
        
        # If no specific session directory, try to find the most recent one
        if not terraform_dir or terraform_dir == "terraform_workdir":
            base_dir = Path("/app/terraform_workdir")
            if user_id:
                user_dir = base_dir / user_id
                if user_dir.exists():
                    # Find the most recent session directory
                    session_dirs = [d for d in user_dir.iterdir() if d.is_dir() and d.name.startswith('session_')]
                    if session_dirs:
                        # Sort by modification time, get most recent
                        latest_session = max(session_dirs, key=lambda x: x.stat().st_mtime)
                        terraform_dir = str(latest_session)
        
        if not terraform_dir or not Path(terraform_dir).exists():
            return json.dumps({
                "status": "error",
                "error": "No Terraform files found to commit",
                "details": f"Terraform directory not found: {terraform_dir}"
            })
        
        # Get list of Terraform files
        terraform_files = []
        for tf_file in Path(terraform_dir).glob("*.tf"):
            with open(tf_file, 'r') as f:
                content = f.read()
                terraform_files.append({
                    "path": f"terraform/{tf_file.name}",
                    "content": content
                })
        
        if not terraform_files:
            return json.dumps({
                "status": "error", 
                "error": "No Terraform files found to commit",
                "details": f"No .tf files in directory: {terraform_dir}"
            })
        
        # Parse repo to get owner and repo name
        try:
            owner, repo_name = repo.split('/', 1)
        except ValueError:
            return json.dumps({
                "status": "error",
                "error": "Invalid repository format. Expected 'owner/repo'",
                "details": f"Received: {repo}"
            })
        
        # Use MCP push_files tool to commit all files at once
        try:
            import asyncio
            import concurrent.futures
            from .mcp_tools import get_real_mcp_tools_for_user
            
            # Use a thread pool to run the async function
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, get_real_mcp_tools_for_user(user_id))
                mcp_tools = future.result()
            push_files_tool = None
            
            for tool in mcp_tools:
                if tool.get('name') == 'push_files':
                    push_files_tool = tool
                    break
            
            if not push_files_tool:
                return json.dumps({
                    "status": "error",
                    "error": "GitHub push_files tool not available",
                    "details": "MCP GitHub tools not properly initialized"
                })
            
            # Call the MCP push_files tool
            from .mcp_tools import RealMCPServerManager
            mcp_manager = RealMCPServerManager()
            
            # Define async function to handle MCP operations
            async def run_mcp_operations():
                # Initialize GitHub MCP server for the user
                await mcp_manager.initialize_mcp_server("github", user_id)
                
                # Call the tool
                return await mcp_manager.call_mcp_tool(
                    server_type="github",
                    tool_name="push_files",
                    arguments={
                        "owner": owner,
                        "repo": repo_name,
                        "branch": branch,
                        "files": terraform_files,
                        "message": commit_message
                    }
                )
            
            # Run the async operations in a separate thread
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, run_mcp_operations())
                result = future.result()
            

            
            # Parse the MCP result
            if isinstance(result, dict) and result.get("content"):
                content = result["content"][0] if result["content"] else {}
                if content.get("type") == "text":
                    response_text = content.get("text", "")
                    
                    # Try to parse as JSON to check for commit SHA
                    try:
                        response_data = json.loads(response_text)
                        # If we have a SHA in the response, it's a success
                        if response_data.get("object", {}).get("sha") or response_data.get("sha"):
                            commit_sha = response_data.get("object", {}).get("sha") or response_data.get("sha")
                            commit_url = response_data.get("object", {}).get("url") or response_data.get("url", "")
                            return json.dumps({
                                "status": "success",
                                "message": f"Successfully committed {len(terraform_files)} Terraform files to {repo}/{branch} in terraform/ folder",
                                "commit_sha": commit_sha,
                                "commit_url": f"https://github.com/{repo}/commit/{commit_sha}",
                                "files_committed": [f["path"] for f in terraform_files],
                                "commit_message": commit_message,
                                "repository": repo,
                                "branch": branch
                            })
                    except (json.JSONDecodeError, TypeError):
                        # Not JSON or can't parse, check for success keywords
                        if "successfully" in response_text.lower() or "pushed" in response_text.lower() or "commit" in response_text.lower():
                            return json.dumps({
                                "status": "success",
                                "message": f"Successfully committed {len(terraform_files)} Terraform files to {repo}/{branch} in terraform/ folder",
                                "details": response_text,
                                "files_committed": [f["path"] for f in terraform_files],
                                "commit_message": commit_message,
                                "repository": repo,
                                "branch": branch
                            })
                    
                    # If we get here, return the raw response
                    return json.dumps({
                        "status": "error",
                        "error": "Commit may have failed",
                        "details": response_text
                    })
            
            return json.dumps({
                "status": "error",
                "error": "Unexpected response from GitHub",
                "details": str(result)
            })
            
        except Exception as mcp_error:
            logger.error(f"MCP tool execution failed: {mcp_error}")
            return json.dumps({
                "status": "error",
                "error": f"Failed to execute GitHub commit: {str(mcp_error)}",
                "details": f"MCP error for repo {repo}"
            })
        
    except Exception as e:
        logger.error(f"Error in github_commit: {e}")
        return json.dumps({
            "status": "error",
            "error": f"GitHub commit failed: {str(e)}",
            "details": f"Unexpected error for repo {repo}"
        })
