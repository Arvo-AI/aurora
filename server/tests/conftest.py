"""Shared test fixtures for the Aurora test suite."""

import sys
import os
from unittest.mock import MagicMock

# Ensure server/ is on sys.path so ``services.*`` imports resolve.
_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# ---------------------------------------------------------------------------
# Stub out heavy third-party packages that aren't installed in test env
# ---------------------------------------------------------------------------
# neo4j is required by services.graph.memgraph_client but may not be present
# in a lightweight test environment.  Provide a minimal stub so the module
# can be imported and patched normally.
for _pkg in (
    "neo4j", "casbin", "casbin_sqlalchemy_adapter", "sqlalchemy",
    "hvac", "redis", "celery", "weaviate", "flask_socketio",
    "flask_cors", "langchain", "langgraph", "requests", "tiktoken",
    # DB / web framework layer (connection_pool, stateless_auth, db_utils)
    "psycopg2", "psycopg2.pool", "psycopg2.extras",
    "dotenv",
    "flask",
    # LangChain sub-packages (cloud_exec_tool, iac_tool, providers)
    "langchain_core", "langchain_core.tools", "langchain_core.language_models",
    "langchain_core.language_models.chat_models",
    "langchain_anthropic", "langchain_openai", "langchain_google_genai",
    # Kubernetes SDK (terminal_run → tool_executor, terminal_pod_manager)
    "kubernetes", "kubernetes.client", "kubernetes.client.rest",
    "kubernetes.config", "kubernetes.stream",
):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()
