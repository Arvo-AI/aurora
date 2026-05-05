"""Shared test fixtures for the Aurora test suite."""

import importlib.util
import os
import sys
from unittest.mock import MagicMock

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

# Stub heavy third-party packages so source modules import in a lightweight
# test env. Only stub when the real package isn't installed -- some tests
# (e.g. test_input_rail.py) need real classes like BaseChatModel / AIMessage.
_OPTIONAL_PACKAGES = (
    "neo4j", "casbin", "casbin_sqlalchemy_adapter", "sqlalchemy",
    "hvac", "redis", "celery", "weaviate", "flask_socketio",
    "flask_cors", "langchain", "langgraph", "requests", "tiktoken",
    "psycopg2", "psycopg2.pool", "psycopg2.extras",
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
        spec = importlib.util.find_spec(_pkg)
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        sys.modules[_pkg] = MagicMock()
