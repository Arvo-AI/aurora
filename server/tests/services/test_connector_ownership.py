"""Connectors whose owner is gone get moved to another admin."""

from services.connectors.ownership import reassign_connectors_to_admin


class _Cursor:
    def __init__(self, token_ids, conn_ids, leftover=0, dropped_tokens=0, dropped_conns=0):
        self.token_ids = token_ids
        self.conn_ids = conn_ids
        self.leftover = leftover
        self.dropped_tokens = dropped_tokens
        self.dropped_conns = dropped_conns
        self.calls = []
        self._fetch = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        # RETURNING updates are the only statements that fetch rows.
        if "UPDATE user_tokens" in sql:
            self._fetch = [(row_id,) for row_id in self.token_ids]
            self.rowcount = len(self.token_ids)
        elif "UPDATE user_connections" in sql:
            self._fetch = [(row_id,) for row_id in self.conn_ids]
            self.rowcount = len(self.conn_ids)
        elif "DELETE FROM user_tokens" in sql:
            self._fetch = []
            self.rowcount = self.dropped_tokens
        elif "DELETE FROM user_connections" in sql:
            self._fetch = []
            self.rowcount = self.dropped_conns
        else:
            self._fetch = []
            self.rowcount = 0

    def fetchall(self):
        return self._fetch

    def fetchone(self):
        return (self.leftover,)


def test_reassign_moves_departing_user_connectors_to_another_admin():
    cursor = _Cursor(token_ids=["token-1"], conn_ids=["conn-1"])

    moved = reassign_connectors_to_admin(cursor, "org-1", "user-leaving")

    assert moved == 2
    token_sql, token_params = cursor.calls[0]
    conn_sql, conn_params = cursor.calls[1]
    assert "UPDATE user_tokens" in token_sql
    assert "UPDATE user_connections" in conn_sql
    assert "role = %s" in token_sql
    assert "IS DISTINCT FROM" in token_sql
    assert "NOT EXISTS" in token_sql
    # org, admin role, excluded user, then ownership: org, user, user
    expected = ("org-1", "admin", "user-leaving", "org-1", "user-leaving", "user-leaving")
    assert token_params == expected
    assert conn_params == expected
    # Moved rows mean we don't need the leftover count. Drops still run.
    assert len(cursor.calls) == 4
    assert "DELETE FROM user_tokens" in cursor.calls[2][0]
    assert "DELETE FROM user_connections" in cursor.calls[3][0]


def test_reassign_counts_dropped_duplicates_so_the_scheduler_commits():
    cursor = _Cursor(token_ids=[], conn_ids=[], dropped_tokens=1, dropped_conns=2)

    assert reassign_connectors_to_admin(cursor, "org-1") == 3
    # Drops are the change. No leftover count, which would hide the commit signal.
    assert len(cursor.calls) == 4


def test_reassign_noop_when_another_admin_owns_them():
    cursor = _Cursor(token_ids=[], conn_ids=[], leftover=0)

    assert reassign_connectors_to_admin(cursor, "org-1") == 0
    # Two updates, two drops, then the leftover count.
    assert len(cursor.calls) == 5
    # Healing a missing owner passes NULL so only the orphan branch matches.
    assert cursor.calls[0][1][2] is None
    assert cursor.calls[0][1][5] is None


def test_reassign_warns_when_no_other_admin():
    cursor = _Cursor(token_ids=[], conn_ids=[], leftover=3)

    assert reassign_connectors_to_admin(cursor, "org-1", "last-admin") == 0
    assert "COUNT" in cursor.calls[4][0]


def test_reassign_skips_blank_org():
    cursor = _Cursor(token_ids=["token-1"], conn_ids=[])

    assert reassign_connectors_to_admin(cursor, "", "user-leaving") == 0
    assert cursor.calls == []
