"""Shared pytest fixtures for the Aurora server test suite.

Aurora's import root is ``server/``, so tests import as
``from connectors.github_connector.vault_keys import ...`` — NOT
``from server.connectors.github_connector...``. All fixtures are
function-scope to keep tests fully isolated.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

# Compatibility shim for pre-existing tests/services/correlation/ tests that
# import Aurora services whose third-party deps may be absent on host envs.
# No-op when the real modules are installed (e.g. inside aurora-server).
for _stub_pkg in (
    "neo4j",
    "casbin",
    "casbin_sqlalchemy_adapter",
    "sqlalchemy",
    "hvac",
    "redis",
    "celery",
    "weaviate",
    "flask_socketio",
    "flask_cors",
    "langchain",
    "langgraph",
):
    sys.modules.setdefault(_stub_pkg, MagicMock())

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
except ImportError:  # pragma: no cover — Task 3 not yet landed
    _GitHubAppConfig = None  # type: ignore[assignment]


@pytest.fixture(scope="function")
def app_private_key() -> tuple[str, str]:
    """Yield ``(private_pem, public_pem)`` from a fresh RSA-2048 keypair."""
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
    return SimpleNamespace(app_id=1, client_id="Iv1.test")


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
        return _psycopg2.connect(
            host=host,
            port=int(port),
            dbname=os.getenv("POSTGRES_DB", "aurora_db"),
            user=os.getenv("POSTGRES_USER", "aurora"),
            password=os.getenv("POSTGRES_PASSWORD", ""),
        )
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
