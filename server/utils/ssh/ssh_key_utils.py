import json
import logging
import uuid
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from utils.db.connection_pool import db_pool
from utils.secrets.secret_ref_utils import get_user_token_data

logger = logging.getLogger(__name__)


def generate_ssh_key_pair(key_size: int = 4096, comment: str = "aurora-ssh-key") -> Tuple[str, str]:
    """Generate an RSA SSH keypair for Aurora-managed access.

    Returns:
        Tuple of (private_key_pem, public_key_openssh)
    """
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend(),
    )

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.OpenSSH,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    public_key_openssh = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.OpenSSH,
        format=serialization.PublicFormat.OpenSSH,
    ).decode("utf-8")

    public_key_with_comment = f"{public_key_openssh} {comment}"
    logger.debug("Generated SSH key pair with comment '%s'", comment)
    return private_key_pem, public_key_with_comment


def build_ssh_provider_name() -> str:
    """Return a unique provider key for Aurora-managed SSH credentials."""
    return f"aurora_ssh_{uuid.uuid4()}"


def _fetch_public_key(user_id: str, provider: str, token_data: Any) -> Optional[str]:
    """Return the stored public key from DB or Vault without exposing private material.

    This function first attempts to extract the public key from the provided token_data parameter
    (DB cache), and only performs an actual fetch from Vault if the DB copy is absent.
    """
    parsed_data = token_data
    if isinstance(token_data, str):
        try:
            parsed_data = json.loads(token_data)
        except Exception:
            parsed_data = None

    # Prefer the DB copy (safe to store)
    if isinstance(parsed_data, dict):
        for key_name in ("public_key", "ssh_public_key"):
            if parsed_data.get(key_name):
                return parsed_data.get(key_name)

    # Fallback to Vault if DB token_data is absent
    try:
        secret_payload = get_user_token_data(user_id, provider) or {}
        if isinstance(secret_payload, dict):
            for key_name in ("public_key", "ssh_public_key"):
                if secret_payload.get(key_name):
                    return secret_payload.get(key_name)
    except Exception as exc:
        logger.warning("Failed to fetch SSH key secret for user %s provider %s: %s", user_id, provider, exc)

    return None


def _parse_token_data(raw_token_data: Any) -> Dict[str, Any]:
    token_body: Dict[str, Any] = {}
    if isinstance(raw_token_data, str):
        try:
            token_body = json.loads(raw_token_data)
        except Exception:
            token_body = {}
    elif isinstance(raw_token_data, dict):
        token_body = raw_token_data
    return token_body


def _serialize_key_row(user_id: str, row: tuple) -> Dict[str, Any]:
    key_id, provider, token_data, secret_ref, created_at = row
    parsed_data = _parse_token_data(token_data)
    label = parsed_data.get("label") or provider
    return {
        "id": key_id,
        "provider": provider,
        "label": label,
        "publicKey": _fetch_public_key(user_id, provider, token_data),
        "createdAt": created_at.isoformat() if created_at else None,
        "hasSecret": bool(secret_ref),
    }


def _get_single_key(user_id: str, key_id: int) -> Optional[Dict[str, Any]]:
    with db_pool.get_user_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SET myapp.current_user_id = %s;", (user_id,))
            conn.commit()
            cur.execute(
                """
                SELECT id, provider, token_data, secret_ref, timestamp
                FROM user_tokens
                WHERE user_id = %s
                  AND id = %s
                  AND provider LIKE 'aurora_ssh%%'
                  AND is_active = TRUE
                LIMIT 1;
                """,
                (user_id, key_id),
            )
            row = cur.fetchone()
    return _serialize_key_row(user_id, row) if row else None
