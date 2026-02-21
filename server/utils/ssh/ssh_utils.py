import io
import logging
import paramiko
import socket
import requests
from typing import Any, Optional, Tuple

from utils.db.connection_pool import db_pool
from utils.secrets.secret_ref_utils import delete_user_secret
from utils.auth.token_management import get_token_data
from utils.ssh.ssh_jump_parser import SSHJumpConfig, parse_ssh_jump_command

logger = logging.getLogger(__name__)


def delete_ssh_credentials(
    user_id: str, vm_id: str, provider: str
) -> Tuple[bool, int, str]:
    """
    Delete SSH credentials for a VM from Vault.

    Args:
        user_id: The user ID
        vm_id: The VM/instance/server ID
        provider: The provider name ('ovh' or 'scaleway')

    Returns:
        Tuple of (success: bool, status_code: int, message: str)
    """
    secret_name = f"{provider}_ssh_{vm_id}"
    logger.info(f"Deleting SSH key for {provider} VM {vm_id}, user: {user_id}")

    # delete_user_secret returns (success: bool, rows_deleted: int)
    success, rows_deleted = delete_user_secret(user_id, secret_name)

    if success:
        if rows_deleted > 0:
            logger.info(f"Successfully deleted SSH key for {provider} VM {vm_id}")
            return True, 200, "SSH credentials removed successfully"
        else:
            # No rows deleted means no credentials were found
            logger.info(f"No SSH credentials found for {provider} VM {vm_id}")
            return False, 404, "No credentials found to delete"
    else:
        logger.error(f"Failed to delete SSH key for {provider} VM {vm_id}")
        return False, 500, "Failed to delete credentials"


def check_if_user_has_vms(user_id: str, provider: str) -> bool:
    """
    Check if a user has any VMs on a provider (OVH or Scaleway).

    Args:
        user_id: The user ID
        provider: 'ovh' or 'scaleway'

    Returns:
        True if user has at least one VM, False otherwise
    """
    try:
        if provider == "ovh":
            from routes.ovh.oauth2_auth_code_flow import get_valid_access_token
            from routes.ovh.ovh_api_routes import OVH_API_ENDPOINTS

            token_data = get_valid_access_token(user_id)
            if not token_data:
                return False

            access_token = token_data.get("access_token")
            endpoint = token_data.get("endpoint")
            api_base_url = OVH_API_ENDPOINTS.get(endpoint)
            headers = {"Authorization": f"Bearer {access_token}"}

            # Get projects and check for instances
            projects_resp = requests.get(
                f"{api_base_url}/cloud/project", headers=headers, timeout=5
            )
            if projects_resp.ok:
                for project_id in projects_resp.json():
                    try:
                        instances_resp = requests.get(
                            f"{api_base_url}/cloud/project/{project_id}/instance",
                            headers=headers,
                            timeout=3,
                        )
                        if instances_resp.ok and len(instances_resp.json()) > 0:
                            return True
                    except (requests.RequestException, ValueError, KeyError) as e:
                        logger.debug(
                            f"Error checking instances in project {project_id}: {e}"
                        )
                        continue

        elif provider == "scaleway":
            token_data = get_token_data(user_id, "scaleway")
            if not token_data:
                return False

            secret_key = token_data.get("secret_key")
            headers = {"X-Auth-Token": secret_key}

            # Import zone list from scaleway routes to stay consistent
            from routes.scaleway.scaleway_routes import SCALEWAY_ZONES

            # Check zones for instances
            for zone in SCALEWAY_ZONES:
                try:
                    resp = requests.get(
                        f"https://api.scaleway.com/instance/v1/zones/{zone}/servers",
                        headers=headers,
                        timeout=2,
                    )
                    if resp.ok and len(resp.json().get("servers", [])) > 0:
                        return True
                except (requests.RequestException, ValueError, KeyError) as e:
                    logger.debug(f"Error checking servers in zone {zone}: {e}")
                    continue

        return False
    except Exception as e:
        logger.warning(f"Error checking VMs for {provider}: {e}")
        return False


def load_user_private_key(user_id: str, ssh_key_id: int) -> str:
    """
    Load a managed SSH private key for a user by key ID.

    Raises:
        ValueError: if the key does not exist for the user or no private key is stored.
    """
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(
                """
                SELECT provider
                FROM user_tokens
                WHERE id = %s AND user_id = %s AND is_active = TRUE
                """,
                (ssh_key_id, user_id),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("SSH key not found for this user")
            provider = row[0]

    token_data = get_token_data(user_id, provider)
    if not token_data or "private_key" not in token_data:
        raise ValueError("SSH private key not available for the selected key")
    return token_data["private_key"]


def parse_ssh_key_id(raw_ssh_key_id: Any) -> Tuple[Optional[int], Optional[str]]:
    try:
        return int(raw_ssh_key_id), None
    except (TypeError, ValueError):
        return None, "sshKeyId must be an integer"


def load_user_private_key_safe(
    user_id: str, ssh_key_id: int
) -> Tuple[Optional[str], Optional[str]]:
    try:
        return load_user_private_key(user_id, ssh_key_id), None
    except ValueError as exc:
        logger.warning(
            "Failed to load SSH key %s for user %s: %s",
            ssh_key_id,
            user_id,
            exc,
            exc_info=True,
        )
        return None, "Invalid sshKeyId"


def get_key_fingerprint(pkey: paramiko.PKey) -> str:
    """Get the MD5 fingerprint of a key for comparison."""
    import hashlib

    key_bytes = pkey.get_fingerprint()
    return ":".join(format(b, "02x") for b in key_bytes)


def parse_private_key(private_key: str) -> paramiko.PKey:
    """
    Parse a private key in various formats (OpenSSH, PEM).

    Args:
        private_key: The private key content as a string

    Returns:
        A paramiko key object (RSAKey, Ed25519Key, or ECDSAKey)

    Raises:
        ValueError: If the key format is invalid or cannot be parsed
    """
    try:
        key_io = io.StringIO(private_key)
        pkey = None

        # OpenSSH format (new format used by ssh-keygen)
        if "OPENSSH PRIVATE KEY" in private_key:
            logger.info("Detected OpenSSH format key")
            try:
                pkey = paramiko.RSAKey.from_private_key(key_io)
            except (paramiko.SSHException, ValueError) as e:
                logger.debug(f"Not an RSA key: {e}")
                key_io.seek(0)
                try:
                    pkey = paramiko.Ed25519Key.from_private_key(key_io)
                except (paramiko.SSHException, ValueError) as e:
                    logger.debug(f"Not an Ed25519 key: {e}")
                    key_io.seek(0)
                    pkey = paramiko.ECDSAKey.from_private_key(key_io)
        # PEM format (traditional format)
        elif "RSA" in private_key:
            logger.info("Detected RSA PEM format key")
            pkey = paramiko.RSAKey.from_private_key(key_io)
        elif "ECDSA" in private_key:
            logger.info("Detected ECDSA PEM format key")
            pkey = paramiko.ECDSAKey.from_private_key(key_io)
        elif "ED25519" in private_key:
            logger.info("Detected ED25519 PEM format key")
            pkey = paramiko.Ed25519Key.from_private_key(key_io)
        else:
            logger.info("Unknown key format, trying RSA as default")
            pkey = paramiko.RSAKey.from_private_key(key_io)

        fingerprint = get_key_fingerprint(pkey)
        logger.info(
            f"Parsed private key successfully: {type(pkey).__name__}, fingerprint: {fingerprint}"
        )
        return pkey
    except Exception as e:
        logger.error(f"Invalid private key format: {e}", exc_info=True)
        raise ValueError(
            f"Invalid private key format: {str(e)}. Please ensure the key is in valid OpenSSH or PEM format."
        )


def validate_and_test_ssh(
    ip_address: str,
    username: str,
    private_key: str,
    timeout: int = 10,
    port: int = 22,
    jump_command: Optional[str] = None,
):
    """
    Validate SSH credentials by attempting to connect to a server (direct or via jump host).

    Args:
        ip_address: The IP address of the target server (used when no jump host is provided)
        username: The SSH username
        private_key: The private key content as a string
        timeout: Connection timeout in seconds (default: 10)
        port: SSH port for direct connections (default: 22)
        jump_command: Optional SSH command string with -J/ProxyJump syntax for bastion access

    Returns:
        tuple: (success: bool, error_message: str or None, connected_as: str or None)

    Example:
        success, error, user = validate_and_test_ssh("1.2.3.4", "root", private_key_str)
        if success:
            print(f"Connected as {user}")
        else:
            print(f"Error: {error}")
    """
    try:
        # Parse the private key
        pkey = parse_private_key(private_key)
    except ValueError as e:
        logger.error(f"SSH key validation error: {e}")
        return False, "Invalid private key format", None

    bastion_client = None
    ssh = None
    channel = None

    target_host = ip_address
    target_user = username
    target_port = port

    try:
        if jump_command:
            jump_cfg: SSHJumpConfig = parse_ssh_jump_command(jump_command)
            target_host = jump_cfg.target_host or target_host
            if jump_cfg.target_port is not None:
                target_port = jump_cfg.target_port
            target_user = jump_cfg.target_user or target_user
            bastion_user = jump_cfg.bastion_user or username

            if not target_host:
                return False, "Target host missing for SSH jump connection", None

            logger.info(
                "Attempting SSH via bastion %s:%s to %s:%s as %s",
                jump_cfg.bastion_host,
                jump_cfg.bastion_port,
                target_host,
                target_port,
                target_user,
            )

            bastion_client = paramiko.SSHClient()
            bastion_client.set_missing_host_key_policy(paramiko.WarningPolicy())
            bastion_client.connect(
                hostname=jump_cfg.bastion_host,
                port=jump_cfg.bastion_port,
                username=bastion_user,
                pkey=pkey,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )

            transport = bastion_client.get_transport()
            if not transport:
                raise paramiko.SSHException(
                    "Failed to establish transport to jump host"
                )

            channel = transport.open_channel(
                kind="direct-tcpip",
                dest_addr=(target_host, target_port),
                src_addr=("", 0),
            )

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
            ssh.connect(
                hostname=target_host,
                username=target_user,
                pkey=pkey,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
                sock=channel,
                look_for_keys=False,
                allow_agent=False,
            )
        else:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
            logger.info(
                f"Attempting SSH connection to {target_host}:{target_port} as {target_user}..."
            )
            ssh.connect(
                hostname=target_host,
                port=target_port,
                username=target_user,
                pkey=pkey,
                timeout=timeout,
                auth_timeout=timeout,
                banner_timeout=timeout,
                look_for_keys=False,
                allow_agent=False,
            )

        # Verify connection by running a command
        stdin, stdout, stderr = ssh.exec_command("whoami", timeout=5)
        result = stdout.read().decode().strip()
        error_output = stderr.read().decode().strip()

        logger.info(f"SSH command result: '{result}', error: '{error_output}'")

        # Compare against target_user which may differ from username when using jump commands
        if result == target_user:
            logger.info(f"SSH authentication successful! Connected as: {result}")
            return True, None, result
        else:
            error_msg = f"SSH connection succeeded but unexpected user: {result} (expected {target_user})"
            logger.warning(error_msg)
            return False, error_msg, None

    except socket.timeout:
        error_msg = "SSH connection timeout. The server may not be accessible or SSH port may be blocked by firewall."
        logger.error(f"SSH timeout to {target_host}: {error_msg}")
        return False, error_msg, None
    except paramiko.AuthenticationException as e:
        error_msg = "SSH authentication failed. The private key is incorrect or not authorized for this server."
        logger.error(f"SSH auth failed to {target_host}: {e}")
        return False, error_msg, None
    except socket.error as e:
        error_msg = f"Unable to connect to {target_host}:{target_port}. The server may be offline, unreachable, or SSH is not running."
        logger.error(f"Socket error to {target_host}: {e}")
        return False, error_msg, None
    except paramiko.SSHException as e:
        error_msg = f"SSH connection error: {str(e)}. The server may not be ready or SSH service may not be running."
        logger.error(f"SSH protocol error to {target_host}: {e}", exc_info=True)
        return False, error_msg, None
    except Exception as e:
        error_msg = f"An unexpected error occurred: {str(e)}"
        logger.error(f"Unexpected SSH error to {target_host}: {e}", exc_info=True)
        return False, error_msg, None
    finally:
        if channel:
            try:
                channel.close()
            except Exception:
                pass
        if ssh:
            ssh.close()
        if bastion_client:
            bastion_client.close()


def normalize_private_key(private_key: str) -> str:
    """
    Normalize a private key string by cleaning up whitespace and newlines.

    Args:
        private_key: The raw private key string

    Returns:
        The normalized private key string
    """
    if not private_key or not isinstance(private_key, str):
        raise ValueError("Private key must be a non-empty string")

    # Clean up the private key (remove extra whitespace, normalize newlines)
    private_key = private_key.strip()

    # Handle escaped newlines from JSON
    if "\\n" in private_key:
        private_key = private_key.replace("\\n", "\n")

    return private_key


def validate_private_key_format(private_key: str) -> Tuple[bool, Optional[str]]:
    """
    Validate that a string looks like a private key.

    Args:
        private_key: The private key string to validate

    Returns:
        tuple: (is_valid: bool, error_message: str or None)
    """
    if not private_key or not isinstance(private_key, str):
        return False, "Private key must be a non-empty string"

    # Check for basic PEM structure
    if not (
        "BEGIN" in private_key and "PRIVATE KEY" in private_key and "END" in private_key
    ):
        return (
            False,
            "Private key must be in valid PEM format (-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----)",
        )

    return True, None
