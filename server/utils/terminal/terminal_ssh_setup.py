import logging
from typing import Dict

from kubernetes import client
from kubernetes.stream import stream
from kubernetes.client.exceptions import ApiException

from utils.db.connection_pool import db_pool
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

# SQL pattern to match SSH key providers (e.g., "scaleway_ssh_<vmId>", "ovh_ssh_<vmId>")
# Using unescaped pattern for PostgreSQL compatibility across versions
SSH_PROVIDER_PATTERN = '%_ssh_%'


def _fetch_user_ssh_keys(user_id: str) -> Dict[str, str]:
    """
    Return all SSH private keys for a user, keyed by a readable name:
    e.g., {"scaleway_4b9511a5": "<private key>", "ovh_abc123": "<private key>"}
    """
    ssh_keys: Dict[str, str] = {}
    with db_pool.get_admin_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT provider FROM user_tokens WHERE user_id = %s AND provider LIKE %s",
            (user_id, SSH_PROVIDER_PATTERN)
        )
        for row in cursor.fetchall():
            provider = row[0] if isinstance(row, tuple) else row
            try:
                token_data = get_token_data(user_id, provider)
                if token_data and "private_key" in token_data:
                    # provider is like "scaleway_ssh_<vmId>" or "ovh_ssh_<vmId>"
                    vm_key = provider.replace("_ssh_", "_")  # scaleway_<vmId>
                    ssh_keys[vm_key] = token_data["private_key"]
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Failed to load SSH key for {provider}: {e}")
    return ssh_keys


def setup_ssh_keys_in_pod(
    core_v1: client.CoreV1Api, pod_name: str, namespace: str, user_id: str
) -> bool:
    """
    Write all user SSH keys into the terminal pod filesystem at ~/.ssh/.
    Keys are named id_<provider>_<vmId> so the agent can pick the right key.
    """
    import base64
    
    try:
        ssh_keys = _fetch_user_ssh_keys(user_id)
        if not ssh_keys:
            logger.info(f"No SSH keys to setup for user {user_id}")
            return True

        setup_cmds = [
            "set -e",
            "mkdir -p ~/.ssh",
            "chmod 700 ~/.ssh",
            # safe default ssh config
            "cat > ~/.ssh/config << 'EOF'",
            "Host *",
            "    StrictHostKeyChecking no",
            "    UserKnownHostsFile=/dev/null",
            "    LogLevel ERROR",
            "EOF",
            "chmod 600 ~/.ssh/config",
        ]

        for vm_key, private_key in ssh_keys.items():
            # Normalize the private key to fix line ending issues and ensure trailing newline
            private_key_normalized = private_key.strip().replace('\r\n', '\n').replace('\r', '\n')
            if not private_key_normalized.endswith('\n'):
                private_key_normalized += '\n'
            
            key_path = f"~/.ssh/id_{vm_key}"
            # Use base64 encoding to safely transfer the key without shell escaping issues
            encoded_key = base64.b64encode(private_key_normalized.encode()).decode()
            setup_cmds.extend(
                [
                    f"echo '{encoded_key}' | base64 -d > {key_path}",
                    f"chmod 600 {key_path}",
                ]
            )

        exec_command = ["/bin/bash", "-c", "\n".join(setup_cmds)]

        resp = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=exec_command,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False,
        )
        resp.run_forever(timeout=60)

        logger.info(
            f"Setup {len(ssh_keys)} SSH key(s) in pod {pod_name} for user {user_id}"
        )
        return True
    except (ApiException, IOError, ValueError, OSError) as e:
        logger.error(
            f"Failed to setup SSH keys in pod {pod_name} for user {user_id}: {e}",
            exc_info=True,
        )
        return False

