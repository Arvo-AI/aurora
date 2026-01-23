"""Tailscale state persistence for terminal pods.

Stores and retrieves Tailscale device identity in PostgreSQL so that
each user has only ONE Tailscale device across all their chat sessions.
"""

import base64
import logging
from typing import Optional

from utils.db.db_utils import connect_to_db_as_user

logger = logging.getLogger(__name__)

_TAILSCALE_STATE_PATH = "/home/appuser/.local/share/tailscale/tailscaled.state"


def _store_state_in_postgres(user_id: str, state_data: bytes) -> bool:
    """Store Tailscale state in PostgreSQL."""
    conn = None
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()

        # Create table if not exists
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tailscale_state (
                user_id VARCHAR(255) PRIMARY KEY,
                state_data TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Upsert state data (base64 encoded)
        encoded = base64.b64encode(state_data).decode('ascii')
        cursor.execute("""
            INSERT INTO tailscale_state (user_id, state_data, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id)
            DO UPDATE SET state_data = EXCLUDED.state_data, updated_at = CURRENT_TIMESTAMP
        """, (user_id, encoded))

        conn.commit()
        cursor.close()
        logger.info(f"Stored Tailscale state in PostgreSQL for user {user_id}")
        return True
    except Exception as e:
        logger.warning(f"Failed to store Tailscale state in PostgreSQL: {e}")
        return False
    finally:
        if conn:
            conn.close()


def _get_state_from_postgres(user_id: str) -> Optional[bytes]:
    """Retrieve Tailscale state from PostgreSQL."""
    conn = None
    try:
        conn = connect_to_db_as_user()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT state_data FROM tailscale_state WHERE user_id = %s",
            (user_id,)
        )
        row = cursor.fetchone()
        cursor.close()

        if row:
            return base64.b64decode(row[0])
        return None
    except Exception as e:
        logger.warning(f"Failed to get Tailscale state from PostgreSQL: {e}")
        return None
    finally:
        if conn:
            conn.close()


def restore_tailscale_state(
    core_v1,
    pod_name: str,
    namespace: str,
    user_id: str
) -> bool:
    """
    Restore Tailscale state from PostgreSQL to terminal pod.

    This allows the pod to reuse an existing Tailscale device identity,
    avoiding duplicate devices in the user's tailnet.

    State is stored per-USER (not per-session) so all sessions share
    the same Tailscale device.

    Args:
        core_v1: Kubernetes CoreV1Api client
        pod_name: Name of the terminal pod
        namespace: Kubernetes namespace
        user_id: User ID

    Returns:
        True if state was restored, False otherwise
    """
    try:
        from kubernetes.stream import stream

        # Get state from PostgreSQL
        state_content = _get_state_from_postgres(user_id)
        if not state_content:
            logger.info(f"No Tailscale state found for user {user_id}")
            return False

        logger.info(f"Retrieved Tailscale state ({len(state_content)} bytes) from PostgreSQL")

        # Create state directory in pod
        mkdir_cmd = ["/bin/sh", "-c", "mkdir -p /home/appuser/.local/share/tailscale"]
        stream(core_v1.connect_get_namespaced_pod_exec,
               pod_name, namespace, command=mkdir_cmd,
               container="terminal",
               stderr=True, stdin=False, stdout=True, tty=False)

        # Write state file to pod using base64 to handle binary content
        encoded_state = base64.b64encode(state_content).decode('ascii')
        write_cmd = [
            "/bin/sh", "-c",
            f"echo '{encoded_state}' | base64 -d > {_TAILSCALE_STATE_PATH}"
        ]
        stream(core_v1.connect_get_namespaced_pod_exec,
               pod_name, namespace, command=write_cmd,
               container="terminal",
               stderr=True, stdin=False, stdout=True, tty=False)

        logger.info(f"Restored Tailscale state to pod {pod_name}")
        return True

    except Exception as e:
        logger.warning(f"Failed to restore Tailscale state: {e}")
        return False


def save_tailscale_state(
    core_v1,
    pod_name: str,
    namespace: str,
    user_id: str
) -> bool:
    """
    Save Tailscale state from terminal pod to PostgreSQL.

    This preserves the Tailscale device identity so future pods can
    reuse the same device instead of creating new ones.

    Args:
        core_v1: Kubernetes CoreV1Api client
        pod_name: Name of the terminal pod
        namespace: Kubernetes namespace
        user_id: User ID

    Returns:
        True if state was saved, False otherwise
    """
    try:
        from kubernetes.stream import stream

        # Read state file from pod (base64 encoded to handle binary)
        read_cmd = [
            "/bin/sh", "-c",
            f"if [ -f {_TAILSCALE_STATE_PATH} ]; then base64 {_TAILSCALE_STATE_PATH}; fi"
        ]
        result = stream(
            core_v1.connect_get_namespaced_pod_exec,
            pod_name, namespace, command=read_cmd,
            container="terminal",
            stderr=True, stdin=False, stdout=True, tty=False
        )

        if not result or not result.strip():
            logger.warning(f"No Tailscale state file found in pod {pod_name}")
            return False

        # Decode state content
        state_content = base64.b64decode(result.strip())
        logger.info(f"Read Tailscale state ({len(state_content)} bytes) from pod {pod_name}")

        # Save to PostgreSQL
        return _store_state_in_postgres(user_id, state_content)

    except Exception as e:
        logger.warning(f"Failed to save Tailscale state: {e}")
        return False
