"""Tool Executor - Abstracts kubectl exec for running commands in terminal pods

This module provides command execution in isolated terminal pods (untrusted namespace)
instead of running commands directly in the orchestrator or chatbot conversation pods.
"""

import json
import logging
import os
import time
from typing import Optional, Tuple, Dict
from kubernetes import client, config
from kubernetes import stream as k8s_stream
from kubernetes.client.rest import ApiException
from utils.terminal.terminal_pod_manager import TerminalPodManager

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Executes tools/commands in isolated terminal pods via kubectl exec."""
    
    def __init__(self):
        """Initialize Kubernetes client and terminal pod manager."""
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config for ToolExecutor")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded kubeconfig from local environment for ToolExecutor")
            except config.ConfigException as e:
                logger.warning(f"Could not load K8s config: {e}")
        
        self.core_v1 = client.CoreV1Api()
        self.pod_manager = TerminalPodManager()
        self.namespace = self.pod_manager.namespace  # Use untrusted namespace from TerminalPodManager
        logger.info(f"ToolExecutor initialized - will execute commands in namespace: {self.namespace}")
    
    def execute_command(
        self,
        user_id: str,
        session_id: str,
        command: str,
        timeout: int = 300,
        working_dir: Optional[str] = None,
        env_vars: Optional[Dict[str, str]] = None
    ) -> Tuple[int, str, str]:
        """
        Execute a command in the user's terminal pod (untrusted namespace).
        
        This replaces subprocess.run() calls in tools.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            command: Shell command to execute
            timeout: Command timeout in seconds
            working_dir: Working directory (default: /home/appuser)
            env_vars: Additional environment variables
        
        Returns:
            Tuple of (returncode, stdout, stderr)
        """
        pod_name = self.pod_manager.generate_pod_name(user_id, session_id)
        
        # Ensure terminal pod exists and is ready
        try:
            pod = self.core_v1.read_namespaced_pod(pod_name, self.namespace)
            if pod.status.phase not in ["Running"]:
                logger.info(f"Terminal pod {pod_name} in phase {pod.status.phase}, creating new one")
                raise ApiException(status=404)
        except ApiException as e:
            if e.status == 404:
                logger.info(f"Terminal pod doesn't exist, creating: {pod_name}")
                success, pod_info = self.pod_manager.create_terminal_pod(
                    user_id, session_id, env_vars
                )
                if not success:
                    error_msg = f"Failed to create terminal pod: {pod_info.get('error', 'Unknown error')}"
                    logger.error(error_msg)
                    return 127, "", error_msg
                
                if not pod_info.get("ready"):
                    logger.warning(f"Terminal pod {pod_name} created but not ready yet")
                    return 127, "", "Terminal pod created but not ready. Please retry in a few seconds."
            else:
                logger.error(f"Error checking terminal pod: {e}")
                return 127, "", str(e)
        
        # Prepare command with working directory
        work_dir = working_dir or "/home/appuser"
        full_command = f"cd {work_dir} && {command}"
        
        # Execute via kubectl exec
        try:
            logger.info(f"Executing in terminal pod {pod_name}: {command[:100]}...")
            
            exec_command = ["/bin/bash", "-c", full_command]
            
            # Use _preload_content=False to get WSClient for proper stdout/stderr separation and exit code
            resp = k8s_stream.stream(
                self.core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                self.namespace,
                container="terminal",  # Terminal pod container name
                command=exec_command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
                _request_timeout=timeout
            )
            
            # Read stdout and stderr from separate channels
            stdout_data = []
            stderr_data = []
            start_time = time.time()
            
            while resp.is_open():
                # Check timeout and kill process if exceeded
                if time.time() - start_time > timeout:
                    try:
                        # Send SIGTERM to cleanup hanging process
                        self.core_v1.connect_get_namespaced_pod_exec(
                            pod_name, self.namespace, container="terminal",
                            command=["/bin/sh", "-c", f"pkill -TERM -f '{command[:50]}'"],
                            stderr=False, stdin=False, stdout=False, tty=False
                        )
                    except Exception:
                        pass  # Best effort cleanup
                    resp.close()
                    return 124, ''.join(stdout_data), f"Command timeout after {timeout}s\n" + ''.join(stderr_data)
                
                resp.update(timeout=1)
                if resp.peek_stdout():
                    stdout_data.append(resp.read_stdout())
                if resp.peek_stderr():
                    stderr_data.append(resp.read_stderr())
            
            # Get exit code from ERROR_CHANNEL
            exit_code = 0
            error_channel = resp.read_channel(k8s_stream.ws_client.ERROR_CHANNEL)
            if error_channel:
                try:
                    error_data = json.loads(error_channel)
                    if 'status' in error_data and error_data['status'] == 'Success':
                        exit_code = 0
                    elif 'causes' in error_data:
                        # Extract exit code from causes
                        for cause in error_data.get('causes', []):
                            if cause.get('reason') == 'ExitCode':
                                exit_code = int(cause.get('message', '1'))
                                break
                    elif 'code' in error_data:
                        exit_code = error_data['code']
                    else:
                        exit_code = 1
                except (json.JSONDecodeError, ValueError, KeyError):
                    exit_code = 1
            
            resp.close()
            
            stdout = ''.join(stdout_data)
            stderr = ''.join(stderr_data)
            
            logger.info(f"Command completed in terminal pod {pod_name} with exit code {exit_code}")
            
            # Update last activity timestamp (fire-and-forget, failures are safe)
            try:
                self.core_v1.patch_namespaced_pod(
                    name=pod_name,
                    namespace=self.namespace,
                    body={"metadata": {"annotations": {"last-activity-at": str(int(time.time()))}}}
                )
            except Exception as e:
                # Log failures to help debug why pods aren't being cleaned up properly
                logger.warning(f"Failed to update last-activity-at annotation for pod {pod_name}: {e}")
            
            return exit_code, stdout, stderr
            
        except ApiException as e:
            logger.error(f"Failed to execute command in terminal pod: {e}")
            return 126, "", str(e)
        except Exception as e:
            logger.error(f"Unexpected error during command execution: {e}")
            return 126, "", str(e)


# Global singleton
_tool_executor = None

def get_tool_executor() -> ToolExecutor:
    """Get the global tool executor instance."""
    global _tool_executor
    if _tool_executor is None:
        _tool_executor = ToolExecutor()
    return _tool_executor
