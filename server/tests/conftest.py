"""Shared pytest fixtures for the Aurora server test suite.

Aurora's import root is ``server/``, so tests import as
``from connectors.github_connector.vault_keys import ...`` — NOT
``from server.connectors.github_connector...``. All fixtures are
function-scope to keep tests fully isolated.
"""

from __future__ import annotations

import importlib.util as _importlib_util
import os
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# POSTGRES_* must be set before any test module imports utils.db.db_utils
# (directly or transitively via utils.auth.*, utils.secrets.*, etc.) --
# db_utils reads these env vars eagerly at import time. Values are inert
# placeholders; tests stub the connection pool and never dial Postgres.
os.environ.setdefault("POSTGRES_DB", "aurora_test")
os.environ.setdefault("POSTGRES_USER", "test_user")
os.environ.setdefault("POSTGRES_PASSWORD", "test_pw")  # noqa: S105
os.environ.setdefault("POSTGRES_HOST", "localhost")
os.environ.setdefault("POSTGRES_PORT", "5432")

# Stub heavy third-party packages so source modules import in a lightweight
# test env. Only stub when the real package isn't installed — some tests
# (e.g. test_input_rail.py) need real classes like BaseChatModel / AIMessage.
# psycopg2 and cryptography are intentionally NOT stubbed: db_session and
# the GitHub App fixtures below skip cleanly when those are absent, but
# stubbing them with MagicMock would silently mask real failures.
_OPTIONAL_PACKAGES = (
    "neo4j", "casbin", "casbin_sqlalchemy_adapter", "sqlalchemy",
    "hvac", "redis", "celery", "weaviate", "flask_socketio",
    "flask_cors", "langchain", "langgraph", "requests", "tiktoken",
    "dotenv", "flask",
    "langchain_core", "langchain_core.tools", "langchain_core.language_models",
    "langchain_core.language_models.chat_models",
    "langchain_anthropic", "langchain_openai", "langchain_google_genai",
    "kubernetes", "kubernetes.client", "kubernetes.client.rest",
    "kubernetes.config", "kubernetes.stream",
)

for _pkg in _OPTIONAL_PACKAGES:
    if _pkg in sys.modules:
        continue
    try:
        spec = _importlib_util.find_spec(_pkg)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        sys.modules[_pkg] = MagicMock()

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    _cryptography_available = True
except ImportError:  # pragma: no cover — host envs without cryptography
    serialization = None  # type: ignore[assignment]
    rsa = None  # type: ignore[assignment]
    _cryptography_available = False

try:
    import psycopg2 as _psycopg2
except ImportError:  # pragma: no cover — host envs without postgres driver
    _psycopg2 = None  # type: ignore[assignment]

try:
    import responses as _responses
except ImportError:  # pragma: no cover — host envs without responses installed
    _responses = None  # type: ignore[assignment]

try:
    from connectors.github_connector.config import GitHubAppConfig as _GitHubAppConfig
except ImportError:  # pragma: no cover
    _GitHubAppConfig = None  # type: ignore[assignment]


@pytest.fixture(scope="function")
def app_private_key() -> tuple[str, str]:
    """Yield ``(private_pem, public_pem)`` from a fresh RSA-2048 keypair."""
    if not _cryptography_available:
        pytest.skip("cryptography library is not installed")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


@pytest.fixture(scope="function")
def app_config() -> Any:
    """Yield a GitHub App config: real :class:`GitHubAppConfig` if importable, else a SimpleNamespace duck-type."""
    if _GitHubAppConfig is not None:
        return _GitHubAppConfig(
            app_id=1,
            client_id="Iv1.test",
            enabled=True,
            webhook_url="https://example.test/github/webhook",
            setup_url="https://example.test/github/app/install/callback",
        )
    return SimpleNamespace(
        app_id=1,
        client_id="Iv1.test",
        enabled=True,
        webhook_url="https://example.test/github/webhook",
        setup_url="https://example.test/github/app/install/callback",
    )


@pytest.fixture(scope="function")
def webhook_secret() -> str:
    return "test-webhook-secret"


@pytest.fixture(scope="function")
def mock_vault(
    monkeypatch: pytest.MonkeyPatch,
    app_private_key: tuple[str, str],
    webhook_secret: str,
) -> Iterator[dict[str, str]]:
    """Patch GitHub App vault helpers to return deterministic test secrets.

    ``connectors.github_connector.vault_keys`` caches both secrets in module
    globals; this fixture clears that cache on entry AND on exit so each
    test starts clean and never poisons later runs.
    """
    from connectors.github_connector import vault_keys

    private_pem, _ = app_private_key
    vault_keys.clear_cache()
    monkeypatch.setattr(vault_keys, "get_app_private_key", lambda: private_pem)
    monkeypatch.setattr(vault_keys, "get_app_webhook_secret", lambda: webhook_secret)

    try:
        yield {"private_key": private_pem, "webhook_secret": webhook_secret}
    finally:
        vault_keys.clear_cache()


@pytest.fixture(scope="function")
def responses_mock() -> Iterator[Any]:
    if _responses is None:
        pytest.skip("responses library is not installed")
    with _responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        yield mocked


def _try_postgres_connection() -> Any:
    if _psycopg2 is None:
        return None
    host = os.getenv("POSTGRES_HOST")
    port = os.getenv("POSTGRES_PORT")
    if not host or not port:
        return None

    try:
        params: dict[str, Any] = {
            "host": host,
            "port": int(port),
            "dbname": os.getenv("POSTGRES_DB", "aurora_db"),
            "user": os.getenv("POSTGRES_USER", "aurora"),
            "password": os.getenv("POSTGRES_PASSWORD", ""),
            "sslmode": os.getenv("POSTGRES_SSLMODE", "prefer"),
        }
        sslrootcert = os.getenv("POSTGRES_SSLROOTCERT")
        if sslrootcert:
            params["sslrootcert"] = sslrootcert
        return _psycopg2.connect(**params)
    except Exception:
        return None


@pytest.fixture(scope="function")
def db_session() -> Iterator[Any]:
    """Yield a Postgres connection wrapped in a rollback-only transaction.

    Skips cleanly via ``pytest.skip()`` when Postgres is unreachable —
    Aurora uses Postgres-specific types (JSONB, etc.) so an in-memory SQLite
    stand-in would silently mask real schema bugs.
    """
    connection = _try_postgres_connection()
    if connection is None:
        pytest.skip(
            "PostgreSQL is not reachable (POSTGRES_HOST/PORT unset or unreachable)"
        )

    connection.autocommit = False
    try:
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()
