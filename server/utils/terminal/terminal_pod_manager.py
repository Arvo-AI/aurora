"""Terminal Pod Manager for Isolated Tool Execution

Manages lightweight terminal pods in the untrusted namespace.
Creates a new pod for each chat session to ensure complete isolation.
"""

import logging
import os
import hashlib
import time
from typing import Optional, Dict, Any, Tuple
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from datetime import datetime, timezone

from utils.terminal.terminal_ssh_setup import setup_ssh_keys_in_pod
from utils.terminal.terminal_storage_sync import restore_terraform_files_from_storage

logger = logging.getLogger(__name__)


class TerminalPodManager:
    """Manages terminal pods for isolated tool execution."""
    
    def __init__(self):
        """Initialize Kubernetes client."""
        try:
            config.load_incluster_config()
            logger.info("Loaded in-cluster Kubernetes config")
        except config.ConfigException:
            try:
                config.load_kube_config()
                logger.info("Loaded kubeconfig from local environment")
            except config.ConfigException as e:
                logger.error(f"Failed to load Kubernetes config: {e}")
                raise
        
        self.core_v1 = client.CoreV1Api()
        
        # Required environment variables - fail fast if not set
        self.namespace = os.environ.get("TERMINAL_NAMESPACE")
        if not self.namespace:
            raise ValueError("TERMINAL_NAMESPACE environment variable must be set")
        
        self.image = os.environ.get("TERMINAL_IMAGE")
        if not self.image:
            raise ValueError("TERMINAL_IMAGE environment variable must be set")
        
        pod_ttl_str = os.environ.get("TERMINAL_POD_TTL")
        if not pod_ttl_str:
            raise ValueError("TERMINAL_POD_TTL environment variable must be set")
        try:
            self.pod_ttl_seconds = int(pod_ttl_str)
        except ValueError:
            raise ValueError(f"TERMINAL_POD_TTL must be a valid integer, got: {pod_ttl_str}")
        
        logger.info(f"TerminalPodManager initialized - namespace: {self.namespace}, image: {self.image}")
    
    def generate_pod_name(self, user_id: str, session_id: str) -> str:
        """Generate pod name from user_id and session_id.

        Each chat session gets a unique pod for complete isolation.
        """
        combined = f"{user_id}-{session_id}"
        name_hash = hashlib.sha256(combined.encode()).hexdigest()[:8]
        return f"terminal-conv-{name_hash}"

    def _generate_legacy_pod_name(self, user_id: str, session_id: str) -> str:
        """Generate legacy pod name using md5 (for migration from md5 to sha256)."""
        combined = f"{user_id}-{session_id}"
        name_hash = hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()[:8]
        return f"terminal-conv-{name_hash}"

    def _find_running_pod(self, user_id: str, session_id: str) -> Optional[str]:
        """Find a running/pending pod, checking both sha256 (current) and md5 (legacy) names."""
        for pod_name in [self.generate_pod_name(user_id, session_id),
                         self._generate_legacy_pod_name(user_id, session_id)]:
            try:
                pod = self.core_v1.read_namespaced_pod(pod_name, self.namespace)
                if pod.status.phase in ["Running", "Pending"]:
                    return pod_name
            except ApiException as e:
                if e.status != 404:
                    logger.warning(f"Error checking pod {pod_name}: {e}")
        return None

    def create_terminal_pod(
        self,
        user_id: str,
        session_id: str,
        env_vars: Optional[Dict[str, str]] = None,
        enable_tailscale: bool = False
    ) -> Tuple[bool, Dict[str, Any]]:
        """Create a new terminal pod for this chat session.

        Args:
            user_id: User ID for isolation
            session_id: Chat session ID
            env_vars: Optional environment variables to inject
            enable_tailscale: If True, runs tailscale-init.sh on pod startup

        Returns:
            Tuple of (success, pod_info_dict)
        """
        # Check for existing running pod (sha256 first, then legacy md5)
        existing_name = self._find_running_pod(user_id, session_id)
        if existing_name:
            logger.info(f"Terminal pod {existing_name} already exists and is running")
            return True, {
                "pod_name": existing_name,
                "status": "Running",
                "namespace": self.namespace,
                "already_exists": True
            }

        # New pods always use sha256
        pod_name = self.generate_pod_name(user_id, session_id)

        # Check if a non-running pod exists under the new name and clean it up
        try:
            existing_pod = self.core_v1.read_namespaced_pod(pod_name, self.namespace)
            # Delete failed/completed pod and create new one
            logger.info(f"Deleting old terminal pod {pod_name} with status {existing_pod.status.phase}")
            self.core_v1.delete_namespaced_pod(pod_name, self.namespace)
            time.sleep(2)

        except ApiException as e:
            if e.status != 404:
                logger.error(f"Error checking existing pod: {e}")
                return False, {"error": str(e)}
        
        # Build environment variables
        pod_env = {
            "USER_ID": user_id,
            "SESSION_ID": session_id,
            "POD_CREATED_AT": datetime.now(timezone.utc).isoformat(),
            "HOME": "/home/appuser",  # Ensure cloud CLIs write to correct home directory
        }
        if env_vars:
            pod_env.update(env_vars)
        
        env_list = [client.V1EnvVar(name=k, value=v) for k, v in pod_env.items()]

        # Create pod specification
        pod_spec = self._create_pod_spec(
            pod_name, user_id, session_id, env_list
        )
        
        # Create the pod
        try:
            logger.info(f"Creating terminal pod: {pod_name} for session {session_id}")
            
            # Send "setting up environment" status to frontend
            try:
                from chat.backend.agent.tools.cloud_tools import send_websocket_message
                send_websocket_message({
                    "type": "tool_status",
                    "data": {
                        "status": "setting_up_environment",
                        "message": "Setting up environment..."
                    }
                }, "terminal_pod_manager")
            except Exception as e:
                logger.debug(f"Failed to send setup status: {e}")
            
            self.core_v1.create_namespaced_pod(self.namespace, pod_spec)
            
            # Wait for pod to be ready (3 min timeout for image pulling on new nodes)
            ready = self._wait_for_pod_ready(pod_name, timeout_seconds=180)
            
            # Restore terraform files from storage if they exist
            if ready:
                restore_terraform_files_from_storage(
                    self.core_v1, pod_name, self.namespace, user_id, session_id
                )
                # Setup SSH keys inside the pod (if user has any)
                try:
                    setup_ssh_keys_in_pod(self.core_v1, pod_name, self.namespace, user_id)
                except Exception as e:
                    logger.warning(f"SSH key setup skipped: {e}")
            
            return True, {
                "pod_name": pod_name,
                "status": "Running" if ready else "Pending",
                "namespace": self.namespace,
                "ready": ready
            }
        
        except ApiException as e:
            logger.error(f"Failed to create terminal pod {pod_name}: {e}")
            return False, {"error": str(e)}
    
    def _create_pod_spec(
        self,
        pod_name: str,
        user_id: str,
        session_id: str,
        env_vars: list
    ) -> client.V1Pod:
        """Create pod specification with maximum security."""

        # Base command - just keep container alive
        # Tailscale setup is handled by tailscale_ssh_tool.py which:
        # 1. Restores state from storage (to reuse existing device identity)
        # 2. Starts tailscaled and joins tailnet
        # 3. Saves state back to storage after successful connection
        # This approach ensures we can restore state BEFORE joining.
        container_command = ["/bin/bash", "-c", "tail -f /dev/null"]

        terminal_container = client.V1Container(
            name="terminal",
            image=self.image,
            image_pull_policy="Never",  # Use local images (rebuild to update)
            command=container_command,
            env=env_vars,
            security_context=client.V1SecurityContext(
                run_as_non_root=True,
                run_as_user=10000,
                allow_privilege_escalation=False,
                read_only_root_filesystem=True,
                capabilities=client.V1Capabilities(drop=["ALL"])
            ),
            resources=client.V1ResourceRequirements(
                requests={"memory": "256Mi", "cpu": "100m"},
                limits={"memory": "512Mi", "cpu": "1", "ephemeral-storage": "2Gi"}
            ),
            volume_mounts=[
                client.V1VolumeMount(name="terminal-home", mount_path="/home/appuser"),
                client.V1VolumeMount(name="terminal-tmp", mount_path="/tmp")
            ]
        )
        
        pod_spec = client.V1PodSpec(
            automount_service_account_token=False,
            enable_service_links=False,
            restart_policy="Never",
            security_context=client.V1PodSecurityContext(
                run_as_non_root=True,
                seccomp_profile=client.V1SeccompProfile(type="RuntimeDefault")
            ),
            runtime_class_name=os.environ.get("TERMINAL_RUNTIME_CLASS"),
            containers=[terminal_container],
            volumes=[
                client.V1Volume(
                    name="terminal-home",
                    empty_dir=client.V1EmptyDirVolumeSource(size_limit="1Gi")
                ),
                client.V1Volume(
                    name="terminal-tmp",
                    empty_dir=client.V1EmptyDirVolumeSource(size_limit="1Gi")
                )
            ]
        )
        
        if os.environ.get("USE_UNTRUSTED_NODES", "false").lower() == "true":
            pod_spec.node_selector = {"workload": "untrusted"}
            pod_spec.tolerations = [
                client.V1Toleration(
                    key="workload",
                    operator="Equal",
                    value="untrusted",
                    effect="NoSchedule"
                )
            ]
        
        current_timestamp = str(int(time.time()))
        metadata = client.V1ObjectMeta(
            name=pod_name,
            labels={
                "app": "user-terminal",
                "user-id": hashlib.sha256(user_id.encode()).hexdigest()[:16],
                "session-id": hashlib.sha256(session_id.encode()).hexdigest()[:16],
                "managed-by": "terminal-pod-manager",
                "created-at": current_timestamp
            },
            annotations={
                "user-id-original": user_id,
                "session-id-original": session_id,
                "inactivity-timeout-seconds": str(self.pod_ttl_seconds),
                "last-activity-at": current_timestamp
            }
        )
        
        return client.V1Pod(api_version="v1", kind="Pod", metadata=metadata, spec=pod_spec)
    
    def _wait_for_pod_ready(self, pod_name: str, timeout_seconds: int = 60) -> bool:
        """Wait for pod to be ready."""
        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                pod = self.core_v1.read_namespaced_pod(pod_name, self.namespace)
                if pod.status.phase == "Running":
                    for container_status in (pod.status.container_statuses or []):
                        if container_status.name == "terminal" and container_status.ready:
                            logger.info(f"Terminal pod {pod_name} is ready")
                            return True
                elif pod.status.phase == "Failed":
                    logger.error(f"Terminal pod {pod_name} failed to start")
                    return False
                time.sleep(2)
            except ApiException as e:
                logger.warning(f"Error checking pod status: {e}")
                time.sleep(2)
        
        logger.warning(f"Terminal pod {pod_name} not ready after {timeout_seconds}s")
        return False

    def wait_for_pod_ready(self, pod_name: str, timeout_seconds: int = 60) -> bool:
        """Public method to wait for pod to be ready.

        Args:
            pod_name: Name of the pod to wait for
            timeout_seconds: Maximum time to wait

        Returns:
            True if pod is ready, False otherwise
        """
        return self._wait_for_pod_ready(pod_name, timeout_seconds)

    def create_tailscale_terminal_pod(
        self,
        user_id: str,
        session_id: str,
        tailscale_auth_key: str
    ) -> Tuple[bool, Dict[str, Any]]:
        """Create terminal pod with Tailscale connectivity.

        Convenience method that creates a terminal pod configured to join
        a Tailscale tailnet. Uses persistent auth key (one device per user).

        The pod joins tailnet with hostname based on user_id hash, ensuring
        only ONE Aurora device appears in user's Tailscale admin regardless
        of how many sessions/pods are created.

        Args:
            user_id: User ID for isolation (used for Tailscale hostname)
            session_id: Chat session ID
            tailscale_auth_key: Reusable Tailscale auth key

        Returns:
            Tuple of (success, pod_info_dict)
        """
        # Pass USER_ID so tailscale-init.sh can compute consistent hostname
        env_vars = {
            "TS_AUTH_KEY": tailscale_auth_key,
            "TAILSCALE_USERSPACE": "1",
            "USER_ID": user_id  # For consistent one-device-per-user hostname
        }
        return self.create_terminal_pod(
            user_id=user_id,
            session_id=session_id,
            env_vars=env_vars,
            enable_tailscale=True
        )

    def wait_for_tailscale_ready(
        self,
        pod_name: str,
        timeout_seconds: int = 30
    ) -> Tuple[bool, Optional[str]]:
        """Wait for Tailscale to connect in a pod.

        Args:
            pod_name: Name of the terminal pod
            timeout_seconds: Maximum time to wait

        Returns:
            Tuple of (success, error_message)
        """
        from kubernetes.stream import stream

        start_time = time.time()
        while time.time() - start_time < timeout_seconds:
            try:
                # Check if tailscale status shows connected
                result = stream(
                    self.core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    self.namespace,
                    command=[
                        "/bin/bash", "-c",
                        "tailscale --socket=/tmp/tailscaled.sock status 2>/dev/null | head -1"
                    ],
                    container="terminal",
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False
                )

                if result and "100." in result:
                    logger.info(f"Tailscale connected in pod {pod_name}")
                    return True, None

            except Exception as e:
                logger.debug(f"Tailscale not ready yet in {pod_name}: {e}")

            time.sleep(2)

        return False, f"Tailscale not ready after {timeout_seconds}s"

