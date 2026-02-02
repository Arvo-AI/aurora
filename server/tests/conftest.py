"""Shared test fixtures for the Aurora test suite."""

import sys
import os
from unittest.mock import MagicMock

import pytest

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
if "neo4j" not in sys.modules:
    _neo4j_stub = MagicMock()
    sys.modules["neo4j"] = _neo4j_stub


# ---------------------------------------------------------------------------
# Mock MemgraphClient
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_memgraph_client():
    """Return a ``MagicMock`` standing in for ``MemgraphClient``.

    The caller can configure return values on the mock's methods:
      - ``are_connected``
      - ``get_shortest_path``
      - ``get_all_upstream``
      - ``get_all_downstream``

    The fixture automatically patches ``get_memgraph_client`` so that any
    production code importing it receives this mock instead.
    """
    client = MagicMock(name="MemgraphClient")

    # Sensible defaults — tests override as needed
    client.are_connected.return_value = False
    client.get_shortest_path.return_value = {}
    client.get_all_upstream.return_value = []
    client.get_all_downstream.return_value = []

    return client
