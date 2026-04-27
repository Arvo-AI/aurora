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
for _pkg in ("neo4j", "casbin", "casbin_sqlalchemy_adapter", "sqlalchemy",
              "hvac", "redis", "celery", "weaviate", "flask_socketio",
              "flask_cors", "langchain", "langgraph", "requests"):
    if _pkg not in sys.modules:
        sys.modules[_pkg] = MagicMock()
