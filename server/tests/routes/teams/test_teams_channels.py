"""Teams channel sync: the prune step must only remove the caller's own, truly stale rows."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from routes.teams import teams_channels as tch


class FakeCursor:
    """Records SQL; owned rows answer the 'WHERE ... user_id' lookup."""

    def __init__(self, owned=()):
        self.owned = list(owned)
        self.executed = []
        self._last = ""

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self._last = self.executed[-1][0]

    def fetchall(self):
        if "AND user_id = %s" in self._last and self._last.startswith("SELECT channel_id"):
            return [(cid,) for cid in self.owned]
        return []

    @property
    def deletes(self):
        return [e for e in self.executed if e[0].startswith("DELETE")]


# --- _prune_stale_channels ------------------------------------------------------------

def test_prune_removes_only_own_channels_that_disappeared(monkeypatch):
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: None)
    cur = FakeCursor(owned=["keep", "gone-1", "gone-2"])
    tch._prune_stale_channels(cur, "uid-self", {"keep"})
    (sql, params), = cur.deletes
    # Scoped to the caller's own rows, not every row in the org
    assert "user_id = %s" in sql
    assert params[1] == "uid-self"
    assert sorted(params[2]) == ["gone-1", "gone-2"]


def test_prune_never_touches_the_incidents_card_channel(monkeypatch):
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: "card")
    cur = FakeCursor(owned=["card", "gone"])
    tch._prune_stale_channels(cur, "uid-self", set())
    (_sql, params), = cur.deletes
    assert params[2] == ["gone"]


def test_prune_is_a_noop_when_nothing_is_stale(monkeypatch):
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: None)
    cur = FakeCursor(owned=["a", "b"])
    tch._prune_stale_channels(cur, "uid-self", {"a", "b"})
    assert cur.deletes == []


# --- auto_register_channels -----------------------------------------------------------------

@pytest.fixture
def sync_env(monkeypatch):
    """Everything auto_register touches, faked; returns the cursor and call log."""
    env = {"cursor": FakeCursor(owned=["old-channel"]), "enqueued": []}

    @contextmanager
    def admin_conn():
        conn = MagicMock()

        @contextmanager
        def cursor_ctx():
            yield env["cursor"]

        conn.cursor = cursor_ctx
        # `with a as conn, conn.cursor() as cur` needs a real context manager on cursor()
        yield conn

    pool = MagicMock()
    pool.get_admin_connection = admin_conn
    monkeypatch.setattr(tch, "db_pool", pool)
    monkeypatch.setattr(tch, "set_rls_context", lambda *a, **k: "org-1")
    monkeypatch.setattr(tch, "resolve_org", lambda user_id: "org-1")
    monkeypatch.setattr(tch, "_upsert_channel", lambda cur, uid, org, ch, existing, **k: (ch["channel_id"], False))
    monkeypatch.setattr(tch, "_mark_pending", lambda cur, ids, *a, **k: [])
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: None)
    monkeypatch.setattr(tch, "_enqueue_metadata", lambda *a, **k: env["enqueued"].append(a))
    # Default: every team has the bot; tests override `env["installed"]`/`env["unknown"]`
    env["installed"] = {"t1"}
    env["unknown"] = set()
    monkeypatch.setattr(
        tch.teams_installs, "resolve_installed_teams",
        lambda user_id, client, team_ids: (set(env["installed"]), set(env["unknown"])),
    )
    return env


def _channels(n):
    return [{"channel_id": f"c{i}", "team_id": "t1", "channel_name": f"n{i}"} for i in range(n)]


def test_missing_client_returns_before_touching_the_database(monkeypatch, sync_env):
    monkeypatch.setattr(tch, "get_teams_client_for_user", lambda user_id: None)
    assert tch.auto_register_channels("uid-self") == 0
    assert sync_env["cursor"].executed == []


def test_full_listing_prunes_the_callers_stale_rows(monkeypatch, sync_env):
    monkeypatch.setattr(tch, "get_teams_client_for_user", lambda user_id: MagicMock())
    monkeypatch.setattr(tch, "_enumerate_member_channels", lambda user_id, client: _channels(2))
    tch.auto_register_channels("uid-self")
    (_sql, params), = sync_env["cursor"].deletes
    assert params[1] == "uid-self" and params[2] == ["old-channel"]


def test_truncated_listing_skips_pruning(monkeypatch, sync_env):
    monkeypatch.setattr(tch, "LIST_CHANNELS_CAP", 2)
    monkeypatch.setattr(tch, "get_teams_client_for_user", lambda user_id: MagicMock())
    # Exactly at the cap: channels past it were never listed, so nothing can be called stale
    monkeypatch.setattr(tch, "_enumerate_member_channels", lambda user_id, client: _channels(2))
    tch.auto_register_channels("uid-self")
    assert sync_env["cursor"].deletes == []


# --- bot-install gating -----------------------------------------------------------------------

def _two_team_channels():
    return [
        {"channel_id": "in-1", "team_id": "t1", "channel_name": "a"},
        {"channel_id": "out-1", "team_id": "t2", "channel_name": "b"},
        {"channel_id": "unk-1", "team_id": "t3", "channel_name": "c"},
    ]


def _capture_upserts(monkeypatch):
    upserted = []

    def fake_upsert(cur, uid, org, ch, existing, **k):
        upserted.append(ch["channel_id"])
        return ch["channel_id"], True

    monkeypatch.setattr(tch, "_upsert_channel", fake_upsert)
    return upserted


def test_only_teams_with_the_bot_get_active_channels(monkeypatch, sync_env):
    monkeypatch.setattr(tch, "get_teams_client_for_user", lambda user_id: MagicMock())
    monkeypatch.setattr(tch, "_enumerate_member_channels", lambda user_id, client: _two_team_channels())
    upserted = _capture_upserts(monkeypatch)
    tch.auto_register_channels("uid-self")
    # t2 has no bot and t3 is unverified: neither is activated (or LLM-described)
    assert upserted == ["in-1"]


def test_channels_of_a_team_without_the_bot_are_pruned_but_unverified_ones_kept(monkeypatch, sync_env):
    monkeypatch.setattr(tch, "get_teams_client_for_user", lambda user_id: MagicMock())
    monkeypatch.setattr(tch, "_enumerate_member_channels", lambda user_id, client: _two_team_channels())
    sync_env["unknown"] = {"t3"}
    sync_env["cursor"].owned = ["in-1", "out-1", "unk-1"]
    _capture_upserts(monkeypatch)
    tch.auto_register_channels("uid-self")
    (_sql, params), = sync_env["cursor"].deletes
    # Verified-absent bot -> row dropped; outage-unknown team -> row survives
    assert params[2] == ["out-1"]


def test_restore_is_refused_when_the_bot_is_not_in_the_team(monkeypatch):
    from flask import Flask

    app = Flask(__name__)
    monkeypatch.setattr(tch, "_team_has_bot", lambda user_id, team_id: False)
    registered = []
    monkeypatch.setattr(tch, "register_single_channel", lambda *a, **k: registered.append(a))
    with app.test_request_context("/", method="POST", json={"team_id": "t2"}):
        # __wrapped__ skips the RBAC decorator, which needs a real request context
        resp, status = tch.restore_teams_channel.__wrapped__("uid-self", "out-1")
    assert status == 409
    assert resp.get_json()["code"] == "bot_not_installed"
    assert registered == []


def test_bulk_activate_skips_teams_without_the_bot(monkeypatch):
    monkeypatch.setattr(tch, "_team_id_for_channel", lambda user_id, cid: "t1" if cid == "a" else "t2")
    monkeypatch.setattr(tch, "_team_has_bot", lambda user_id, team_id: team_id == "t1")
    registered = []
    monkeypatch.setattr(tch, "register_single_channel", lambda user_id, cid, **k: registered.append(cid) or True)
    assert tch._activate_channels("uid-self", ["a", "b"]) == 1
    assert registered == ["a"]


def test_dismissed_channels_stay_listable_and_flag_missing_bot(monkeypatch):
    monkeypatch.setattr(tch, "_fetch_all_workspace_channels", lambda user_id: [
        {"channel_id": "x", "team_id": "t1", "channel_name": "x"},
        {"channel_id": "y", "team_id": "t2", "channel_name": "y"},
    ])
    monkeypatch.setattr(tch.teams_installs, "get_installed_team_ids", lambda user_id: {"t1"})
    out = {c["channel_id"]: c["bot_installed"] for c in tch._list_available_channels("uid-self", set())}
    assert out == {"x": True, "y": False}


# --- Teams-side lifecycle cleanup -----------------------------------------------------------------

@pytest.fixture
def lifecycle_db(monkeypatch):
    cur = FakeCursor()
    cur.fetchall = lambda: [("c1",), ("c2",)]

    @contextmanager
    def admin_conn():
        conn = MagicMock()

        @contextmanager
        def cursor_ctx():
            yield cur

        conn.cursor = cursor_ctx
        yield conn

    pool = MagicMock()
    pool.get_admin_connection = admin_conn
    monkeypatch.setattr(tch, "db_pool", pool)
    monkeypatch.setattr(tch, "set_rls_context", lambda *a, **k: "org-1")
    monkeypatch.setattr(tch, "_invalidate_available_cache", lambda user_id: None)
    cleared = []
    monkeypatch.setattr(tch, "_clear_card_channel", lambda user_id: cleared.append(user_id))
    return cur, cleared


def test_removing_a_team_deletes_its_rows_and_clears_a_card_inside_it(monkeypatch, lifecycle_db):
    cur, cleared = lifecycle_db
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: "c2")
    tch.remove_team_channels("uid-self", "t1")
    assert any("team_id = %s" in sql for sql, _ in cur.deletes)
    assert cleared == ["uid-self"]


def test_removing_a_team_leaves_a_card_elsewhere_alone(monkeypatch, lifecycle_db):
    _cur, cleared = lifecycle_db
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: "other")
    tch.remove_team_channels("uid-self", "t1")
    assert cleared == []


def test_deleted_channel_clears_the_card_when_it_was_the_card(monkeypatch, lifecycle_db):
    _cur, cleared = lifecycle_db
    monkeypatch.setattr(tch, "_get_card_channel_id", lambda user_id: "c9")
    tch.remove_channel("uid-self", "c9")
    assert cleared == ["uid-self"]


def test_rename_updates_only_with_a_name(lifecycle_db):
    cur, _ = lifecycle_db
    tch.rename_channel("uid-self", "c1", "")
    assert not cur.executed
    tch.rename_channel("uid-self", "c1", "new-name")
    assert cur.executed[0][1][0] == "new-name"
