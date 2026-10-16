"""Actions whose creator is gone get moved to another admin."""

from services.actions.ownership import reassign_actions_to_admin


class _Cursor:
    def __init__(self, moved_ids, leftover=0):
        self.moved_ids = moved_ids
        self.leftover = leftover
        self.calls = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return [(action_id,) for action_id in self.moved_ids]

    def fetchone(self):
        return (self.leftover,)


def test_reassign_moves_departing_user_and_orphans_to_another_admin():
    cursor = _Cursor(moved_ids=["action-1", "action-2"])

    moved = reassign_actions_to_admin(cursor, "org-1", "user-leaving")

    assert moved == 2
    sql, params = cursor.calls[0]
    assert "role = %s" in sql
    assert "IS DISTINCT FROM" in sql
    assert "NOT EXISTS" in sql
    # org, admin role, excluded user, org again, departing user
    assert params == ("org-1", "admin", "user-leaving", "org-1", "user-leaving")
    # Moved rows mean we don't need the leftover count.
    assert len(cursor.calls) == 1


def test_reassign_noop_when_another_admin_owns_them():
    cursor = _Cursor(moved_ids=[], leftover=0)

    assert reassign_actions_to_admin(cursor, "org-1") == 0
    assert len(cursor.calls) == 2
    # Healing a missing creator passes NULL so only the orphan branch matches.
    assert cursor.calls[0][1][2] is None
    assert cursor.calls[0][1][4] is None


def test_reassign_warns_when_no_other_admin():
    cursor = _Cursor(moved_ids=[], leftover=3)

    assert reassign_actions_to_admin(cursor, "org-1", "last-admin") == 0
    assert "COUNT" in cursor.calls[1][0]


def test_reassign_skips_blank_org():
    cursor = _Cursor(moved_ids=["action-1"])

    assert reassign_actions_to_admin(cursor, "", "user-leaving") == 0
    assert cursor.calls == []
