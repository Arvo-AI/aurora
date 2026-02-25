"""Tailscale SSH tool for executing commands on Tailscale devices.

Provides SSH access to devices on a user's Tailscale tailnet using stored SSH keys.

Flow:
1. User connects Tailscale account → SSH keys + auth key generated and stored
2. User adds Aurora's public SSH key to their devices' ~/.ssh/authorized_keys
3. On SSH request: Aurora uses stored private key to SSH into the device

In K8s mode: Uses existing terminal pod, writes SSH key to pod, joins tailnet if needed
In local dev: Uses direct SSH execution with stored keys
"""

import hashlib
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)


def _is_pod_isolation_enabled() -> bool:
    """Check if pod isolation is enabled (K8s mode vs local dev)."""
    return os.getenv('ENABLE_POD_ISOLATION', 'true').lower() == 'true'


def _execute_ssh_local(
    device_hostname: str,
    command: str,
    ssh_user: str,
    timeout: int,
    ssh_private_key: Optional[str] = None
) -> str:
    """Execute SSH command in local development mode.

    Uses regular SSH with key-based authentication to Tailscale IP addresses.
    The container must be on the tailnet (tailscale up) to reach Tailscale IPs.

    This approach works with any device that has:
    1. Regular SSH server enabled (sshd)
    2. SSH key authentication configured

    Args:
        device_hostname: Target device hostname or IP
        command: Command to execute
        ssh_user: SSH username
        timeout: Command timeout in seconds
        ssh_private_key: Optional SSH private key content (if not provided, uses default keys)
    """
    import subprocess
    import tempfile

    # Write SSH key to temp file if provided
    key_file = None
    if ssh_private_key:
        try:
            key_file = tempfile.NamedTemporaryFile(mode='w', suffix='_aurora_key', delete=False)
            key_file.write(ssh_private_key)
            key_file.close()
            os.chmod(key_file.name, 0o600)
        except Exception as e:
            logger.warning(f"Failed to write SSH key to temp file: {e}")
            key_file = None

    # Build SSH command
    ssh_cmd = ["ssh"]

    if key_file:
        ssh_cmd.extend(["-i", key_file.name])

    ssh_cmd.extend([
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "BatchMode=yes",  # Fail immediately if password is required
        "-o", "ConnectTimeout=10",
        f"{ssh_user}@{device_hostname}",
        command
    ])

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )

        output = result.stdout if result.returncode == 0 else (result.stderr or result.stdout)

        # Check for common SSH errors
        if result.returncode != 0:
            if "Permission denied" in output or "Host key verification failed" in output:
                error_hint = (
                    f"SSH key authentication failed for {ssh_user}@{device_hostname}. "
                    f"Please add Aurora's SSH public key to the device's ~/.ssh/authorized_keys. "
                    f"Get the key from Settings > Cloud Providers > Tailscale."
                )
                return json.dumps({
                    "success": False,
                    "error": error_hint,
                    "device": device_hostname,
                    "provider": "tailscale_ssh"
                }, indent=2)

            if "No route to host" in output or "Network is unreachable" in output or "Could not resolve hostname" in output:
                error_hint = (
                    f"Cannot reach {device_hostname}. "
                    f"The device may be offline or disconnected from Tailscale."
                )
                return json.dumps({
                    "success": False,
                    "error": error_hint,
                    "device": device_hostname,
                    "provider": "tailscale_ssh"
                }, indent=2)

            if "Connection refused" in output:
                error_hint = (
                    f"SSH connection refused by {device_hostname}. "
                    f"Ensure the SSH server is running on the device."
                )
                return json.dumps({
                    "success": False,
                    "error": error_hint,
                    "device": device_hostname,
                    "provider": "tailscale_ssh"
                }, indent=2)

        return json.dumps({
            "success": result.returncode == 0,
            "command": command,
            "device": device_hostname,
            "user": ssh_user,
            "output": output,
            "chat_output": output,
            "return_code": result.returncode,
            "provider": "tailscale_ssh"
        }, indent=2)

    except subprocess.TimeoutExpired:
        return json.dumps({
            "success": False,
            "error": f"SSH command timed out after {timeout} seconds",
            "device": device_hostname,
            "provider": "tailscale_ssh"
        })
    except FileNotFoundError:
        return json.dumps({
            "success": False,
            "error": "SSH connection failed. Please try again.",
            "device": device_hostname,
            "provider": "tailscale_ssh"
        })
    except Exception as e:
        return json.dumps({
            "success": False,
            "error": f"SSH command failed: {str(e)}",
            "device": device_hostname,
            "provider": "tailscale_ssh"
        })
    finally:
        # Clean up temp key file
        if key_file:
            try:
                os.unlink(key_file.name)
            except Exception:
                pass


def tailscale_ssh(
    device_hostname: str,
    command: str,
    ssh_user: str = "root",
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    timeout: int = 60
) -> str:
    """
    Execute a command on a Tailscale device via SSH.

    In K8s mode: Creates a terminal pod that joins the user's Tailscale tailnet.
    In local dev mode: Uses direct SSH execution (requires Tailscale on host or direct network access).

    Args:
        device_hostname: Hostname or Tailscale IP of the target device
        command: Shell command to execute on the remote device
        ssh_user: SSH username (default: root)
        user_id: User context (auto-injected by framework)
        session_id: Session context (auto-injected by framework)
        timeout: Command timeout in seconds (default: 60)

    Returns:
        JSON string with execution results

    Example:
        tailscale_ssh("myserver", "uptime", "root")
        tailscale_ssh("web-prod", "docker ps", "admin")
    """
    if not user_id or not session_id:
        logger.error("tailscale_ssh: user_id and session_id are required")
        return json.dumps({
            "success": False,
            "error": "User context is required but not available"
        })

    if not device_hostname or not device_hostname.strip():
        return json.dumps({
            "success": False,
            "error": "Device hostname is required"
        })

    if not command or not command.strip():
        return json.dumps({
            "success": False,
            "error": "Command cannot be empty"
        })

    # Validate SSH user (basic sanitization)
    if not ssh_user or (not ssh_user.isalnum() and ssh_user not in ["root"]):
        ssh_user = "root"

    # Get stored SSH key for authentication
    from utils.auth.token_management import get_token_data, store_tokens_in_db
    token_data = get_token_data(user_id, "tailscale")
    ssh_private_key = token_data.get("ssh_private_key") if token_data else None

    # If SSH keys are missing but we have valid Tailscale credentials, generate them
    if token_data and not ssh_private_key:
        try:
            from routes.tailscale.tailscale_routes import generate_ssh_key_pair
            new_private_key, new_public_key = generate_ssh_key_pair()

            token_data["ssh_private_key"] = new_private_key
            token_data["ssh_public_key"] = new_public_key

            # Store updated token data (handles secret storage and cache clearing internally)
            store_tokens_in_db(user_id, token_data, "tailscale")

            # Return immediately with instructions to add the new key
            return json.dumps({
                "success": False,
                "error": "SSH keys were just generated. You need to add Aurora's public key to your device.",
                "action_required": "Go to Settings > Cloud Providers > Tailscale > SSH Setup to get your new SSH public key, then add it to ~/.ssh/authorized_keys on your device.",
                "device": device_hostname,
                "provider": "tailscale_ssh"
            })
        except Exception as key_gen_error:
            logger.error(f"Failed to generate SSH keys: {key_gen_error}")

    # LOCAL DEV MODE: Use direct SSH execution
    if not _is_pod_isolation_enabled():
        if not ssh_private_key:
            return json.dumps({
                "success": False,
                "error": "Tailscale not connected or SSH keys not found. Please connect your Tailscale account first.",
                "hint": "Go to Settings > Cloud Providers > Tailscale > SSH Setup to get your SSH key."
            })

        # Try to join tailnet if targeting a Tailscale hostname
        is_tailscale_hostname = device_hostname.endswith(".ts.net") or device_hostname.startswith("100.")
        if is_tailscale_hostname:
            import subprocess as sp
            try:
                ts_status = sp.run(
                    ["tailscale", "--socket=/tmp/tailscaled.sock", "status", "--json"],
                    capture_output=True, text=True, timeout=5
                )
                if ts_status.returncode != 0:
                    tailscale_auth_key = token_data.get("tailscale_auth_key") if token_data else None
                    if tailscale_auth_key:
                        user_hash = hashlib.sha256(user_id.encode()).hexdigest()[:8]
                        aurora_hostname = f"aurora-{user_hash}"
                        sp.run(
                            ["tailscale", "--socket=/tmp/tailscaled.sock", "up",
                             f"--authkey={tailscale_auth_key}", f"--hostname={aurora_hostname}"],
                            capture_output=True, text=True, timeout=30
                        )
            except (FileNotFoundError, sp.TimeoutExpired, Exception) as e:
                logger.warning(f"Tailscale join failed in local mode: {e}. Continuing with SSH via host network.")

        return _execute_ssh_local(device_hostname, command, ssh_user, timeout, ssh_private_key)

    # K8S MODE: Use existing terminal pod with stored SSH keys
    try:
        # Check SSH key is available
        if not token_data:
            return json.dumps({
                "success": False,
                "error": "Tailscale not connected. Please connect your Tailscale account first."
            })

        tailscale_auth_key = token_data.get("tailscale_auth_key")

        if not ssh_private_key:
            return json.dumps({
                "success": False,
                "error": "SSH keys not found. Please reconnect your Tailscale account to generate new keys."
            })

        # Get terminal pod for this session
        try:
            from utils.terminal.terminal_pod_manager import TerminalPodManager
            from kubernetes.client.rest import ApiException
            manager = TerminalPodManager()
        except ValueError:
            return _execute_ssh_local(device_hostname, command, ssh_user, timeout, ssh_private_key)

        pod_name = manager.generate_pod_name(user_id, session_id)

        # Check if pod exists, create if not
        try:
            pod = manager.core_v1.read_namespaced_pod(pod_name, manager.namespace)
            if pod.status.phase not in ["Running", "Pending"]:
                logger.info(f"Terminal pod {pod_name} in phase {pod.status.phase}, recreating")
                raise ApiException(status=404)
        except ApiException as e:
            if e.status == 404:
                logger.info(f"Creating Tailscale terminal pod: {pod_name}")
                success, pod_info = manager.create_tailscale_terminal_pod(
                    user_id, session_id, tailscale_auth_key
                )
                if not success:
                    error_msg = pod_info.get('error', 'Failed to create SSH environment')
                    logger.error(f"Failed to create terminal pod: {error_msg}")
                    return json.dumps({
                        "success": False,
                        "error": f"Failed to create SSH environment: {error_msg}",
                        "provider": "tailscale_ssh"
                    })
            else:
                logger.error(f"Error checking terminal pod: {e}")
                return json.dumps({
                    "success": False,
                    "error": f"Error checking SSH environment: {str(e)}",
                    "provider": "tailscale_ssh"
                })

        if not manager.wait_for_pod_ready(pod_name):
            return json.dumps({
                "success": False,
                "error": "SSH environment is starting up. Please try again in a moment.",
                "provider": "tailscale_ssh"
            })

        from kubernetes.stream import stream

        # Write SSH private key to pod
        key_write_cmd = f"""
mkdir -p /tmp/.ssh &&
cat > /tmp/.ssh/aurora_key << 'AURORA_SSH_KEY_EOF'
{ssh_private_key}
AURORA_SSH_KEY_EOF
chmod 600 /tmp/.ssh/aurora_key
"""
        try:
            stream(
                manager.core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                manager.namespace,
                command=["/bin/bash", "-c", key_write_cmd],
                container="terminal",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False
            )
        except Exception as key_error:
            logger.error(f"Failed to write SSH key to pod: {key_error}")
            return json.dumps({
                "success": False,
                "error": "Failed to setup SSH connection. Please try again.",
                "provider": "tailscale_ssh"
            })

        # Ensure pod is on tailnet (join if not already)
        # First, restore Tailscale state from DB to reuse existing device identity
        tailscale_state_restored = False
        if tailscale_auth_key:
            try:
                from utils.terminal.terminal_tailscale_state import restore_tailscale_state
                tailscale_state_restored = restore_tailscale_state(
                    manager.core_v1, pod_name, manager.namespace, user_id
                )
                if tailscale_state_restored:
                    logger.info(f"Restored Tailscale state for user {user_id}")
            except Exception as restore_err:
                logger.warning(f"Failed to restore Tailscale state: {restore_err}")

            user_hash = hashlib.sha256(user_id.encode()).hexdigest()[:8]
            aurora_hostname = f"aurora-{user_hash}"
            state_path = "/home/appuser/.local/share/tailscale/tailscaled.state"

            # Use restored state path if state was restored, otherwise use temp path
            tailscale_join_cmd = f"""
if ! timeout 5 tailscale --socket=/tmp/tailscaled.sock status 2>/dev/null | grep -q "100\\."; then
    if ! pgrep -x tailscaled > /dev/null; then
        mkdir -p /home/appuser/.local/share/tailscale
        tailscaled --state={state_path} --socket=/tmp/tailscaled.sock --tun=userspace-networking --statedir=/home/appuser/.local/share/tailscale > /dev/null 2>&1 &
        sleep 2
    fi
    timeout 30 tailscale --socket=/tmp/tailscaled.sock up --authkey={tailscale_auth_key} --hostname={aurora_hostname} --accept-routes 2>&1 || true
    sleep 1
fi
"""
            try:
                stream(
                    manager.core_v1.connect_get_namespaced_pod_exec,
                    pod_name,
                    manager.namespace,
                    command=["/bin/bash", "-c", tailscale_join_cmd],
                    container="terminal",
                    stderr=True,
                    stdin=False,
                    stdout=True,
                    tty=False
                )
            except Exception as e:
                logger.warning(f"Tailscale join failed in pod: {e}. Continuing - device may be reachable if already on tailnet.")

        # Execute SSH command
        # Use ProxyCommand with tailscale nc for userspace networking mode
        # In K8s, Tailscale runs with --tun=userspace-networking which doesn't create
        # a kernel tun device. Direct SSH to 100.x.x.x IPs fails because the kernel
        # can't route to them. ProxyCommand routes traffic through Tailscale's tunnel.
        escaped_command = command.replace("'", "'\\''")
        ssh_cmd = (
            f"ssh -i /tmp/.ssh/aurora_key "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o BatchMode=yes "
            f"-o ConnectTimeout=30 "
            f"-o ProxyCommand='tailscale --socket=/tmp/tailscaled.sock nc %h %p' "
            f"{ssh_user}@{device_hostname} '{escaped_command}'"
        )

        try:
            result = stream(
                manager.core_v1.connect_get_namespaced_pod_exec,
                pod_name,
                manager.namespace,
                command=["/bin/bash", "-c", ssh_cmd],
                container="terminal",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=True
            )

            output = result if result else ""

            # Check for SSH errors in output
            if "Permission denied" in output:
                return json.dumps({
                    "success": False,
                    "error": (
                        f"SSH key authentication failed for {ssh_user}@{device_hostname}. "
                        f"Please add Aurora's SSH public key to the device's ~/.ssh/authorized_keys. "
                        f"Get the key from Settings > Cloud Providers > Tailscale."
                    ),
                    "device": device_hostname,
                    "provider": "tailscale_ssh"
                })

            # Save Tailscale state after successful connection
            # This preserves device identity for future sessions
            if tailscale_auth_key:
                try:
                    from utils.terminal.terminal_tailscale_state import save_tailscale_state
                    save_tailscale_state(
                        manager.core_v1, pod_name, manager.namespace, user_id
                    )
                except Exception as save_err:
                    logger.debug(f"Failed to save Tailscale state: {save_err}")

            return json.dumps({
                "success": True,
                "command": command,
                "device": device_hostname,
                "user": ssh_user,
                "output": output,
                "chat_output": output,
                "return_code": 0,
                "provider": "tailscale_ssh"
            }, indent=2)

        except Exception as ssh_error:
            error_str = str(ssh_error)

            if "Connection refused" in error_str:
                error_msg = (
                    f"SSH connection refused by {device_hostname}. "
                    "Ensure SSH server is running on the device."
                )
            elif "No route to host" in error_str or "Network is unreachable" in error_str:
                error_msg = (
                    f"Cannot reach {device_hostname}. "
                    "The device may be offline or not on the tailnet."
                )
            elif "Permission denied" in error_str:
                error_msg = (
                    f"SSH key authentication failed for {ssh_user}@{device_hostname}. "
                    f"Please add Aurora's SSH public key to the device's ~/.ssh/authorized_keys. "
                    f"Get the key from Settings > Cloud Providers > Tailscale."
                )
            else:
                error_msg = f"SSH command failed: {error_str}"

            return json.dumps({
                "success": False,
                "error": error_msg,
                "command": command,
                "device": device_hostname,
                "provider": "tailscale_ssh"
            })

    except Exception as e:
        logger.error(f"tailscale_ssh error: {e}", exc_info=True)
        return json.dumps({
            "success": False,
            "error": f"Tailscale SSH failed: {str(e)}"
        })
