"""When store_tokens_in_db schedules an infrastructure prediscovery run.

Token refreshes (Jira OAuth, MCP server re-saves, the generic refresh helper)
all write through store_tokens_in_db. Each write used to schedule prediscovery,
so a busy org re-ran discovery every hour. Now:
- schedule_prediscovery=True (user-initiated connect routes) always schedules,
  since re-connecting with different credentials changes what can be discovered;
- schedule_prediscovery=False (token refresh) never schedules;
- the default schedules only for a provider the org has no active row for.
"""

from contextlib import contextmanager

import pytest

import utils.auth.token_management as tm

_LOOKUP = "SELECT 1 FROM user_tokens"


class _Cursor:
    """Fake cursor that records SQL and answers the existing-row lookup."""

    def __init__(self, existing_row: bool, fail_lookup: bool = False):
        """Configure whether an active row exists and whether the lookup raises."""
        self.existing_row = existing_row
        self.fail_lookup = fail_lookup
        self.statements = []
        self._last_select = False

    def execute(self, sql, params=None):
        """Record the statement; raise on the lookup when configured to."""
        self.statements.append(sql)
        self._last_select = sql.startswith(_LOOKUP)
        if self._last_select and self.fail_lookup:
            raise RuntimeError("lookup failed")

    def fetchone(self):
        """Return a row for the lookup only when an active row exists."""
        return (1,) if self._last_select and self.existing_row else None


class _Conn:
    """Fake connection that hands out one cursor and records the commit."""

    def __init__(self, cursor):
        """Wrap the given fake cursor."""
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        """Return the wrapped fake cursor."""
        return self._cursor

    def commit(self):
        """Mark the transaction as committed."""
        self.committed = True


def _index(statements, prefix):
    """Index of the first statement starting with prefix."""
    return next(i for i, s in enumerate(statements) if s.startswith(prefix))


@pytest.fixture
def store(monkeypatch):
    """Run store_tokens_in_db against a fake DB; return the scheduled user ids."""

    def _run(existing_row, fail_lookup=False, **kwargs):
        """Store a token and return (scheduled user ids, fake cursor)."""
        cursor = _Cursor(existing_row, fail_lookup)
        conn = _Conn(cursor)
        scheduled = []

        @contextmanager
        def _admin_connection():
            """Yield the fake connection."""
            yield conn

        class _Secrets:
            """Fake Vault secret manager."""

            def store_secret(self, name, value):
                """Return a Vault reference without storing anything."""
                return f"vault:kv/data/aurora/users/{name}"

        monkeypatch.setattr(tm.db_pool, "get_admin_connection", _admin_connection)
        monkeypatch.setattr(tm, "set_rls_context", lambda *a, **k: "org-1")
        monkeypatch.setattr("utils.secrets.secret_ref_utils.SecretRefManager", _Secrets)
        monkeypatch.setattr("utils.secrets.secret_cache.clear_secret_cache", lambda ref: None)
        monkeypatch.setattr(tm, "_schedule_prediscovery", scheduled.append)

        tm.store_tokens_in_db("user-1", {"access_token": "t"}, kwargs.pop("provider", "jira"), **kwargs)
        assert conn.committed
        return scheduled, cursor

    return _run


def test_new_connector_schedules_prediscovery(store):
    """A provider with no active row schedules prediscovery by default."""
    scheduled, _ = store(existing_row=False)
    assert scheduled == ["user-1"]


@pytest.mark.parametrize("provider", ["jira", "mcp", "bitbucket", "gcp"])
def test_refresh_of_existing_connector_does_not_schedule(store, provider):
    """A default write over an existing active row does not schedule."""
    scheduled, _ = store(existing_row=True, provider=provider)
    assert scheduled == []


@pytest.mark.parametrize("provider", ["gcp", "scaleway", "kubeconfig_org-1_prod"])
def test_connect_route_reconnect_still_schedules(store, provider):
    """Re-connecting with different credentials over an active row schedules."""
    scheduled, cursor = store(existing_row=True, provider=provider, schedule_prediscovery=True)
    assert scheduled == ["user-1"]
    assert not any(s.startswith(_LOOKUP) for s in cursor.statements)


def test_opt_out_never_schedules_even_for_new_connector(store):
    """schedule_prediscovery=False never schedules, and skips the lookup."""
    scheduled, cursor = store(existing_row=False, schedule_prediscovery=False)
    assert scheduled == []
    assert not any(s.startswith(_LOOKUP) for s in cursor.statements)


def test_failed_lookup_falls_back_to_scheduling_and_still_upserts(store):
    """A failed lookup rolls back to its savepoint, then upserts and schedules."""
    scheduled, cursor = store(existing_row=False, fail_lookup=True)
    assert scheduled == ["user-1"]
    statements = cursor.statements
    savepoint = _index(statements, "SAVEPOINT sp_token_row_check")
    lookup = _index(statements, _LOOKUP)
    rollback = _index(statements, "ROLLBACK TO SAVEPOINT sp_token_row_check")
    upsert = _index(statements, "INSERT INTO user_tokens")
    assert savepoint < lookup < rollback < upsert
