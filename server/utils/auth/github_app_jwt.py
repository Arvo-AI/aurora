"""
JWT mint utility for GitHub App authentication.

Mints short-lived RS256 JWTs used to obtain installation access tokens from
GitHub. Per GitHub's October 2024 change, the `iss` claim MUST be the App's
client_id (e.g. ``Iv1.abc123...``), NOT the numeric ``app_id``. Using
``app_id`` works today but GitHub will reject it in 2026.

References:
    https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app

Security notes:
    - JWTs are minted fresh on demand (cheap, never cached).
    - The PEM and the JWT itself are never logged at any level.
    - Only RS256 is supported; HS256/none are not used.
"""

import logging
import time
from typing import TYPE_CHECKING

import jwt as pyjwt

if TYPE_CHECKING:
    # Type-only import to avoid a hard runtime dependency on the parallel
    # Wave 1 config module. mint_app_jwt_with_config accepts any object that
    # exposes a `.client_id` attribute.
    from connectors.github_connector.config import GitHubAppConfig

logger = logging.getLogger(__name__)

# 9 minutes (540s) leaves a 1-minute buffer below GitHub's 10-minute hard cap.
_JWT_EXPIRY_SECONDS = 540
# 60s clock-skew backdating per GitHub's recommendation.
_JWT_IAT_BACKDATE_SECONDS = 60
# RS256 is the only algorithm GitHub accepts for App JWTs.
_JWT_ALGORITHM = "RS256"


class GitHubAppJWTError(Exception):
    pass


def mint_app_jwt_with_config(config: "GitHubAppConfig", private_key: str) -> str:
    """Mint a GitHub App JWT from explicit config + private key.

    Pure function with no Vault, environment, or filesystem dependencies, so
    it can be exercised directly in unit tests with a generated keypair.

    Args:
        config: Object exposing ``client_id`` (str). MUST be the App's
            ``client_id`` (e.g. ``Iv1.abc...``), NOT the numeric ``app_id``.
        private_key: PEM-encoded RSA private key as a UTF-8 string.

    Returns:
        Encoded JWT (three base64url-encoded segments separated by ``.``).

    Raises:
        GitHubAppJWTError: If ``client_id`` is missing/empty, the private key
            is empty, or the signing operation fails.
    """
    client_id = getattr(config, "client_id", None)
    if not client_id:
        raise GitHubAppJWTError(
            "GitHub App config is missing client_id; cannot mint JWT (iss must be client_id, not app_id)."
        )
    if not private_key:
        raise GitHubAppJWTError("GitHub App private key is empty; cannot mint JWT.")

    now = int(time.time())
    payload = {
        "iat": now - _JWT_IAT_BACKDATE_SECONDS,
        "exp": now + _JWT_EXPIRY_SECONDS,
        "iss": client_id,
    }

    try:
        token = pyjwt.encode(payload, private_key, algorithm=_JWT_ALGORITHM)
    except Exception as exc:
        # Wrap PyJWT/cryptography errors. Never include the key or token text.
        raise GitHubAppJWTError(
            f"Failed to mint GitHub App JWT (RS256): {type(exc).__name__}: {exc}"
        ) from exc

    # Intentionally do NOT log the token at any level.
    logger.debug("[GITHUB-APP-JWT] minted RS256 JWT for client_id=%s (exp=%ds)", client_id, _JWT_EXPIRY_SECONDS)
    return token


def mint_app_jwt() -> str:
    """Mint a GitHub App JWT using process config + Vault-stored private key.

    Composes :func:`mint_app_jwt_with_config` with the real config loader and
    the Vault private-key helper. Intended for production runtime use.

    Returns:
        Encoded JWT (string).

    Raises:
        GitHubAppJWTError: If config or Vault helpers are unavailable, or if
            JWT minting fails for any reason. The original error is chained.
    """
    # Lazy imports keep this module importable while the parallel Wave 1
    # tasks (config + vault_keys) are still being implemented. Once they
    # land, real production callers get the full integration without code
    # changes here.
    try:
        from connectors.github_connector.config import load_github_app_config
        from connectors.github_connector.vault_keys import get_app_private_key
    except ImportError as exc:
        raise GitHubAppJWTError(
            f"GitHub App config or vault helpers are unavailable: {exc}"
        ) from exc

    try:
        config = load_github_app_config()
        private_key = get_app_private_key()
    except Exception as exc:
        raise GitHubAppJWTError(
            f"Failed to load GitHub App config or private key: {type(exc).__name__}: {exc}"
        ) from exc

    return mint_app_jwt_with_config(config, private_key)
