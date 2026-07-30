"""Shared pytest fixtures for the Aurora server test suite.

Aurora's import root is ``server/``, so tests import as
``from connectors.github_connector.vault_keys import ...`` — NOT
``from server.connectors.github_connector...``. All fixtures are
function-scope to keep tests fully isolated.
"""

from __future__ import annotations

import importlib.machinery as _importlib_machinery
import importlib.util as _importlib_util
import os
import sys
from collections.abc import Iterator
from types import ModuleType, SimpleNamespace
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

# routes/gcp/auth.py does `os.getenv("FRONTEND_URL") + "/chat"` at import time,
# which raises TypeError when the var is unset. Any test that transitively
# imports a routes package (routes.datadog -> celery_config -> routes.gcp) hits
# it. Inert placeholder; nothing here dials the frontend.
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")

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
    "langchain_aws", "langchain_ollama",
    "kubernetes", "kubernetes.client", "kubernetes.client.rest",
    "kubernetes.client.exceptions", "kubernetes.config", "kubernetes.stream",
    # Submodules imported directly by source modules. Each needs its own entry:
    # `from langgraph.types import ...` cannot resolve against a stub of the
    # parent alone, since a stub package has no real search path.
    "langchain.agents", "langchain.agents.middleware",
    "langchain.agents.middleware.types",
    "langchain_core.callbacks", "langchain_core.messages",
    "langchain_core.messages.utils", "langchain_core.runnables",
    "langchain_core.runnables.config",
    "langgraph.checkpoint", "langgraph.checkpoint.memory",
    "langgraph.graph", "langgraph.graph.state", "langgraph.types",
    "celery.exceptions", "celery.signals",
)

# Namespace packages that must be stubbed *prefix-wise*, not module-by-module.
# Reached transitively by anything importing a routes package (routes.datadog ->
# routes.datadog.tasks -> chat.background -> google_chat_connector and
# agent.providers.google_provider). Enumerating submodules does not work here:
# langchain_google_genai is installed in CI and walks arbitrary google.genai.*
# submodules at import time, so a fixed list always misses one.
_STUBBED_NAMESPACES = ("google", "googleapiclient")


class _StubModule(ModuleType):
    """A real module object that mocks any attribute on demand.

    A bare ``MagicMock`` will not do here: as a stand-in for a *package* it needs
    ``__path__`` (or Python raises "'google' is not a package") and a real
    ``__spec__`` for pytest's assertion-rewriting machinery -- but setting those
    on a MagicMock stops it auto-creating the *other* attributes callers want,
    e.g. ``service_account.Credentials``. Subclassing ModuleType and mocking via
    ``__getattr__`` satisfies both at once.
    """

    def __init__(self, fullname: str) -> None:
        super().__init__(fullname)
        self.__path__: list[str] = []
        self.__spec__ = _importlib_machinery.ModuleSpec(fullname, None, is_package=True)

    def __getattr__(self, item: str) -> Any:
        if item.startswith("__") and item.endswith("__"):
            raise AttributeError(item)
        value = MagicMock(name=f"{self.__name__}.{item}")
        setattr(self, item, value)
        return value


# Submodules of the google* namespace packages that source modules import.
# Registered explicitly, parents before children: every one must be present in
# sys.modules before the import machinery sees it, because an empty __path__
# lets Python's namespace-package resolution return a real, attribute-less
# module instead of these stubs.
_STUBBED_SUBMODULES = (
    "google", "google.auth", "google.auth.transport",
    "google.auth.transport.requests", "google.oauth2",
    "google.oauth2.credentials", "google.oauth2.service_account",
    "google.cloud", "google.cloud.bigquery",
    "google.api_core", "google.api_core.exceptions",
    "google.genai", "google.genai.types", "google.genai.client",
    "googleapiclient", "googleapiclient.discovery", "googleapiclient.errors",
)

# langchain_google_genai is installed in CI but imports google.genai.* at module
# scope, so it cannot load against stubs. When google is absent, stub the wrapper
# too rather than chasing whichever google.genai submodule it reaches next.
_STUB_WHEN_GOOGLE_ABSENT = ("langchain_google_genai",)

def _is_installed(name: str) -> bool:
    try:
        return _importlib_util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _stub(name: str) -> None:
    if name not in sys.modules:
        sys.modules[name] = _StubModule(name)


# Stub each optional package (and any listed submodule) only when the real one
# is absent, so a real installation always wins. _StubModule rather than a bare
# MagicMock because several of these are imported as packages -- e.g.
# `from langgraph.types import ...` fails against a MagicMock with
# "'langgraph' is not a package".
for _pkg in _OPTIONAL_PACKAGES:
    # Check the exact module, not just its root: CI installs langchain-core,
    # which brings a real `langgraph` package that has no `langgraph.types`.
    # Gating on the root would skip the submodule and leave the import failing.
    if not _is_installed(_pkg):
        _stub(_pkg)

for _ns in _STUBBED_NAMESPACES:
    # Per-submodule, not per-namespace: `google` is a namespace package that any
    # installed google-* distribution provides, so gating the whole loop on the
    # root would leave a missing google.oauth2 unstubbed whenever some unrelated
    # google package happens to be present. _stub() and _is_installed() both
    # no-op on real modules, so nothing is ever masked.
    for _mod in _STUBBED_SUBMODULES:
        if _mod.split(".")[0] == _ns and not _is_installed(_mod):
            _stub(_mod)
    if _ns == "google" and not _is_installed("google.oauth2"):
        # These wrappers import google.* at module scope, so they cannot load
        # against stubs even when they are themselves installed.
        for _wrapper in _STUB_WHEN_GOOGLE_ABSENT:
            sys.modules.pop(_wrapper, None)
            sys.modules[_wrapper] = _StubModule(_wrapper)

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

    try:
        return _psycopg2.connect(**params)
    except _psycopg2.OperationalError:
        # The host is configured but unreachable (refused, timeout, DNS).
        # Treat this as "Postgres not available for tests" → skip.
        return None
    # Anything else (auth failure, bad TLS config, invalid params) is a
    # real misconfiguration we want CI to see; let it propagate.


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
