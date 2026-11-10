"""store_tokens_in_db schedules prediscovery only for a newly connected provider.

Token refreshes (Jira OAuth, MCP server re-saves, the generic refresh helper)
all write through store_tokens_in_db. Each write used to schedule an
infrastructure prediscovery run, so a busy org re-ran discovery every hour.
"""

from contextlib import contextmanager

import pytest

import utils.auth.token_management as tm


class _Cursor:
    def __init__(self, existing_row: bool, fail_lookup: bool = False):
        self.existing_row = existing_row
        self.fail_lookup = fail_lookup
        self.statements = []
        self._last_select = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self._last_select = sql.startswith("SELECT 1 FROM user_tokens")
        if self._last_select and self.fail_lookup:
            raise RuntimeError("lookup failed")

    def fetchone(self):
        return (1,) if self._last_select and self.existing_row else None


class _Conn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


@pytest.fixture
def store(monkeypatch):
    """Run store_tokens_in_db against a fake DB; return the scheduled user ids."""

    def _run(existing_row, fail_lookup=False, **kwargs):
        cursor = _Cursor(existing_row, fail_lookup)
        conn = _Conn(cursor)
        scheduled = []

        @contextmanager
        def _admin_connection():
            yield conn

        class _Secrets:
            def store_secret(self, name, value):
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
    scheduled, _ = store(existing_row=False)
    assert scheduled == ["user-1"]


@pytest.mark.parametrize("provider", ["jira", "mcp", "bitbucket", "gcp"])
def test_refresh_of_existing_connector_does_not_schedule(store, provider):
    scheduled, _ = store(existing_row=True, provider=provider)
    assert scheduled == []


def test_opt_out_never_schedules_even_for_new_connector(store):
    scheduled, _ = store(existing_row=False, schedule_prediscovery=False)
    assert scheduled == []


def test_failed_lookup_falls_back_to_scheduling_and_still_upserts(store):
    scheduled, cursor = store(existing_row=False, fail_lookup=True)
    assert scheduled == ["user-1"]
    assert "ROLLBACK TO SAVEPOINT sp_token_row_check" in cursor.statements
    assert any(s.startswith("INSERT INTO user_tokens") for s in cursor.statements)


def test_lookup_uses_the_upsert_conflict_key(store):
    _, cursor = store(existing_row=False)
    lookup = next(s for s in cursor.statements if s.startswith("SELECT 1 FROM user_tokens"))
    assert "org_id IS NOT DISTINCT FROM" in lookup
    assert "provider = %s" in lookup
