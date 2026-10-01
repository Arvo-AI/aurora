"""Tests for the periodic Slack channel-description backfill (issue #673).

Routing only offers 'ready' channels, and descriptions were previously only
enqueued by capped, user-triggered reconcile passes. These target
``_backfill_channel_descriptions``, not the wrapper: conftest stubs ``celery``.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from routes.slack import slack_channel_metadata as mod

ORG_A = "00000000-0000-0000-0000-00000000000a"
ORG_B = "00000000-0000-0000-0000-00000000000b"
USER_A = "11111111-1111-1111-1111-11111111111a"
USER_A2 = "11111111-1111-1111-1111-11111111112a"
USER_B = "11111111-1111-1111-1111-11111111111b"
TEAM_1 = "T1000000"
TEAM_2 = "T2000000"


def _db(fetchall_rows=None):
    """Build a (dbcm, conn, cur) context-manager-shaped DB stub."""
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchall.return_value = fetchall_rows if fetchall_rows is not None else []
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, conn, cur


def _run(users_by_org, rows_per_org, connected_users=None, rls_ok=True,
         claimed=None, teams=None):
    """Run the backfill, returning (result, enqueued, mark_pending_calls).

    ``rows_per_org`` is consumed in org iteration order; rows are
    (channel_id, owner_user_id[, team_id]) — team_id defaults to TEAM_1 so the
    common case stays terse. ``teams`` maps user_id -> the workspace their Slack
    connection belongs to (default: everyone on TEAM_1). ``claimed`` restricts
    which channel_ids the UPDATE reports as won (default: all). Rotation is
    pinned to dict order here and covered by its own tests below.

    The read pass can re-query to page past unreachable workspaces, so the stub
    applies the exclusion filters itself and only advances to the next org's batch
    once a batch is exhausted.
    """
    enqueued = []
    marked = []
    batches = [[_row(r) for r in batch] for batch in rows_per_org]
    current = {"rows": None}

    def fake_select(cur, limit, exclude_teams=None, exclude_untagged=False):
        # A fresh org always reads with no exclusions; a re-read always carries at
        # least one (it only happens after a workspace is proven unreachable), so
        # the exclusion state is what distinguishes "next org" from "next page".
        if not exclude_teams and not exclude_untagged:
            current["rows"] = batches.pop(0) if batches else []
        excluded = set(exclude_teams or [])
        rows = [r for r in current["rows"] or []
                if r[2] not in excluded and not (exclude_untagged and r[2] is None)]
        return rows[:limit]

    def fake_creds(uid, provider):
        if connected_users is not None and uid not in connected_users:
            return None
        return {"access_token": "xoxb-test",
                "team_id": (teams or {}).get(uid, TEAM_1)}

    def fake_mark_pending(cur, cids, stale_minutes=None):
        marked.append(list(cids))
        return [c for c in cids if claimed is None or c in claimed]

    dbcm, _conn, _cur = _db()
    with patch("utils.auth.stateless_auth.users_by_org", return_value=users_by_org), \
         patch.object(mod, "_rotate_orgs", side_effect=lambda ids, now=None: list(ids)), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context",
               return_value=(ORG_A if rls_ok else None)), \
         patch("utils.auth.stateless_auth.get_credentials_from_db",
               side_effect=fake_creds), \
         patch.object(mod, "_select_undescribed_channels", side_effect=fake_select), \
         patch("routes.slack.slack_channels._mark_pending",
               side_effect=fake_mark_pending), \
         patch("routes.slack.slack_channels._enqueue_metadata",
               side_effect=lambda uid, cid: enqueued.append((uid, cid))):
        result = mod._backfill_channel_descriptions()
    return result, enqueued, marked


def _row(row):
    """Pad a (channel_id, owner) test row out to the query's 3-tuple shape."""
    return row if len(row) == 3 else (row[0], row[1], TEAM_1)


# --- happy path -------------------------------------------------------------

def test_backfill_enqueues_undescribed_member_channels():
    """The whole point: channels left 'pending' past the per-pass cap get
    descriptions enqueued without any user action."""
    result, enqueued, marked = _run(
        {ORG_A: [USER_A]},
        [[("C1", USER_A), ("C2", USER_A)]],
    )
    assert enqueued == [(USER_A, "C1"), (USER_A, "C2")]
    assert result == {"orgs": 1, "enqueued": 2}
    # Rows are flipped to 'pending' + restamped before the tasks are queued.
    assert marked == [["C1", "C2"]]


def test_backfill_sweeps_every_org():
    result, enqueued, _marked = _run(
        {ORG_A: [USER_A], ORG_B: [USER_B]},
        [[("C1", USER_A)], [("C2", USER_B)]],
    )
    assert enqueued == [(USER_A, "C1"), (USER_B, "C2")]
    assert result == {"orgs": 2, "enqueued": 2}


def test_backfill_noop_when_nothing_pending():
    result, enqueued, marked = _run({ORG_A: [USER_A]}, [[]])
    assert enqueued == []
    assert marked == []          # no pointless status churn
    assert result == {"orgs": 0, "enqueued": 0}


# --- bounds -----------------------------------------------------------------

def test_backfill_caps_per_org():
    """Per-org cap bounds LLM spend; the rest waits for the next run."""
    rows = [(f"C{i}", USER_A) for i in range(100)]
    with patch.object(mod, "BACKFILL_MAX_PER_ORG", 5):
        result, enqueued, _marked = _run({ORG_A: [USER_A]}, [rows])
    assert len(enqueued) == 5
    assert result["enqueued"] == 5


def test_backfill_caps_total_across_orgs():
    """Once the global cap is hit, later orgs are skipped entirely this run."""
    with patch.object(mod, "BACKFILL_MAX_PER_ORG", 10), \
         patch.object(mod, "BACKFILL_MAX_TOTAL", 3):
        result, enqueued, _marked = _run(
            {ORG_A: [USER_A], ORG_B: [USER_B]},
            [[(f"A{i}", USER_A) for i in range(10)],
             [(f"B{i}", USER_B) for i in range(10)]],
        )
    assert len(enqueued) == 3
    assert all(uid == USER_A for uid, _cid in enqueued)  # org B never reached
    assert result == {"orgs": 1, "enqueued": 3}


# --- credential handling ----------------------------------------------------

def test_backfill_falls_back_to_another_connected_org_member():
    """The row owner disconnected Slack, but creds are org-shared — use another
    member connected to the SAME workspace so the channel isn't stranded."""
    _result, enqueued, _marked = _run(
        {ORG_A: [USER_A, USER_A2]},
        [[("C1", USER_A, TEAM_1)]],
        connected_users={USER_A2},  # owner USER_A has no Slack creds
    )
    assert enqueued == [(USER_A2, "C1")]


def test_backfill_skips_org_with_no_slack_connection():
    """Nobody in the org can reach Slack — leave rows pending rather than queue
    tasks that would only mark them 'error' (never auto-retried)."""
    _result, enqueued, marked = _run(
        {ORG_A: [USER_A]},
        [[("C1", USER_A)]],
        connected_users=set(),
    )
    assert enqueued == []
    assert marked == []


def test_backfill_skips_a_channel_whose_only_standin_is_another_workspace():
    """Slack creds are org-shared, but an org can connect several workspaces. A
    token for the wrong workspace can't read the channel: conversations.info
    returns nothing and the task would persist an invented description as
    'ready'. Leave the row pending instead."""
    _result, enqueued, marked = _run(
        {ORG_A: [USER_A, USER_A2]},
        [[("C1", USER_A, TEAM_1)]],
        connected_users={USER_A2},          # owner disconnected
        teams={USER_A2: TEAM_2},            # ...and the stand-in is on another team
    )
    assert enqueued == []
    assert marked == []


def test_backfill_skips_org_without_resolvable_rls_context():
    _result, enqueued, _marked = _run(
        {ORG_A: [USER_A]}, [[("C1", USER_A)]], rls_ok=False,
    )
    assert enqueued == []


# --- resilience -------------------------------------------------------------

def test_backfill_one_failing_org_does_not_stop_the_sweep():
    """A DB error on one org must not starve the others."""
    enqueued = []
    calls = {"n": 0}

    def flaky_select(cur, limit, exclude_teams=None, exclude_untagged=False):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        return [("C2", USER_B, TEAM_1)]

    dbcm, _conn, _cur = _db()
    # now=0 pins the rotation so the failing org is the one visited first.
    with patch("utils.auth.stateless_auth.users_by_org", return_value={ORG_A: [USER_A], ORG_B: [USER_B]}), \
         patch.object(mod, "_rotate_orgs", return_value=[ORG_A, ORG_B]), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A), \
         patch("utils.auth.stateless_auth.get_credentials_from_db",
               return_value={"access_token": "xoxb-test", "team_id": TEAM_1}), \
         patch.object(mod, "_select_undescribed_channels", side_effect=flaky_select), \
         patch("routes.slack.slack_channels._mark_pending",
               side_effect=lambda cur, cids, stale_minutes=None: list(cids)), \
         patch("routes.slack.slack_channels._enqueue_metadata",
               side_effect=lambda uid, cid: enqueued.append((uid, cid))):
        result = mod._backfill_channel_descriptions()

    assert enqueued == [(USER_B, "C2")]
    assert result == {"orgs": 1, "enqueued": 1}


def test_backfill_returns_empty_when_org_enumeration_fails():
    with patch("utils.auth.stateless_auth.users_by_org", side_effect=RuntimeError("db down")):
        assert mod._backfill_channel_descriptions() == {"orgs": 0, "enqueued": 0}


# --- concurrency: the claim decides what gets enqueued ----------------------

def test_backfill_enqueues_only_the_rows_it_actually_claimed():
    """A row whose status moved on between SELECT and the claiming UPDATE (a
    racing sweep, or a worker that started describing it) must NOT be enqueued —
    otherwise we pay for an LLM call whose result _update_metadata discards."""
    result, enqueued, _marked = _run(
        {ORG_A: [USER_A]},
        [[("C1", USER_A), ("C2", USER_A)]],
        claimed={"C1"},  # C2 was won by someone else
    )
    assert enqueued == [(USER_A, "C1")]
    assert result == {"orgs": 1, "enqueued": 1}


def test_backfill_skips_org_when_it_claims_nothing():
    """Lost every row to a concurrent sweep — nothing to enqueue, and the org
    shouldn't be counted as touched."""
    result, enqueued, _marked = _run(
        {ORG_A: [USER_A]}, [[("C1", USER_A)]], claimed=set(),
    )
    assert enqueued == []
    assert result == {"orgs": 0, "enqueued": 0}


def test_backfill_claim_reasserts_the_staleness_window():
    """The UPDATE must re-check the predicate the SELECT observed, so a racing
    sweep's restamp locks this one out instead of both enqueueing."""
    seen = {}

    def capture(cur, cids, stale_minutes=None):
        seen["stale_minutes"] = stale_minutes
        return list(cids)

    dbcm, _conn, _cur = _db()
    with patch("utils.auth.stateless_auth.users_by_org", return_value={ORG_A: [USER_A]}), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A), \
         patch("utils.auth.stateless_auth.get_credentials_from_db",
               return_value={"access_token": "xoxb-test", "team_id": TEAM_1}), \
         patch.object(mod, "_select_undescribed_channels",
                      return_value=[("C1", USER_A, TEAM_1)]), \
         patch("routes.slack.slack_channels._mark_pending", side_effect=capture), \
         patch("routes.slack.slack_channels._enqueue_metadata"):
        mod._backfill_channel_descriptions()

    assert seen["stale_minutes"] == mod.BACKFILL_STALE_MINUTES


# --- generate_channel_metadata: claim before spending -----------------------

def _claim_db(rowcount=1):
    conn = MagicMock()
    cur = MagicMock()
    cur.rowcount = rowcount
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: False
    dbcm = MagicMock()
    dbcm.__enter__ = lambda s: conn
    dbcm.__exit__ = lambda s, *a: False
    return dbcm, cur


def test_claim_for_generation_flips_pending_to_generating():
    dbcm, cur = _claim_db(rowcount=1)
    with patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A):
        assert mod._claim_for_generation(USER_A, "C1") is True

    sql, params = cur.execute.call_args.args
    assert "metadata_status = 'generating'" in sql
    # Only an unclaimed row may be taken; a row already 'generating' belongs to
    # another task, so its status is what makes the claim exclusive.
    assert params == ("C1", ["pending", "skipped"])


def test_claim_for_generation_loses_to_a_concurrent_task():
    """No rows matched — someone else already flipped the row, so this task must
    bail out instead of paying for an LLM call _update_metadata would discard."""
    dbcm, _cur = _claim_db(rowcount=0)
    with patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A):
        assert mod._claim_for_generation(USER_A, "C1") is False


def test_claim_for_generation_accepts_generating_on_a_retry():
    """The task's own retry re-enters with the row already flipped by attempt 1 —
    it must not lock itself out."""
    dbcm, cur = _claim_db(rowcount=1)
    with patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A):
        mod._claim_for_generation(USER_A, "C1", allow_generating=True)
    assert cur.execute.call_args.args[1][1] == ["pending", "skipped", "generating"]


def test_claim_for_generation_without_an_org_context_claims_nothing():
    """slack_channels is RLS-protected, so an unset org context makes the UPDATE
    match zero rows — treat it as a lost claim, not a win."""
    dbcm, cur = _claim_db(rowcount=1)
    with patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=None):
        assert mod._claim_for_generation(USER_A, "C1") is False
    cur.execute.assert_not_called()


def test_build_context_refuses_a_channel_it_could_not_read():
    """conversations.info returning nothing means the token can't see the channel
    (wrong workspace, archived, scope revoked). Describing it from an empty
    context would persist a fabricated summary as 'ready'."""
    import pytest

    client = MagicMock()
    client.get_channel_info.return_value = None
    with pytest.raises(mod.ChannelUnreadable):
        mod._build_context(client, "C1")


# --- cross-org fairness -----------------------------------------------------

def test_rotate_orgs_advances_with_the_beat_interval():
    """A fixed order would let the same few orgs consume the global cap every run
    and starve the rest, so the leading org rotates each interval."""
    orgs = ["a", "b", "c"]
    interval = mod.BACKFILL_INTERVAL_SECONDS
    assert mod._rotate_orgs(orgs, now=0) == ["a", "b", "c"]
    assert mod._rotate_orgs(orgs, now=interval) == ["b", "c", "a"]
    assert mod._rotate_orgs(orgs, now=2 * interval) == ["c", "a", "b"]
    # Wraps cleanly back to the start.
    assert mod._rotate_orgs(orgs, now=3 * interval) == ["a", "b", "c"]


def test_rotate_orgs_is_stable_within_one_interval():
    """Concurrent beat workers in the same window must agree on the order."""
    orgs = ["a", "b", "c"]
    base = 10 * mod.BACKFILL_INTERVAL_SECONDS
    assert mod._rotate_orgs(orgs, now=base) == mod._rotate_orgs(orgs, now=base + 1)


def test_rotate_orgs_handles_empty_and_single():
    assert mod._rotate_orgs([]) == []
    assert mod._rotate_orgs(["only"], now=12345) == ["only"]


def test_rotate_orgs_preserves_every_org():
    """A rotation must never drop or duplicate an org."""
    orgs = [f"org{i}" for i in range(7)]
    for step in range(10):
        rotated = mod._rotate_orgs(orgs, now=step * mod.BACKFILL_INTERVAL_SECONDS)
        assert sorted(rotated) == sorted(orgs)


# --- the selection query itself ---------------------------------------------

def test_select_only_targets_member_channels_awaiting_description():
    """'error'/'limit_reached' must NOT be swept: retrying a hard failure or a
    cost-capped org on a timer would burn quota in a loop."""
    cur = MagicMock()
    cur.fetchall.return_value = [("C1", USER_A, TEAM_1)]
    assert mod._select_undescribed_channels(cur, 25) == [("C1", USER_A, TEAM_1)]

    sql, params = cur.execute.call_args.args
    assert "is_member" in sql
    assert "'skipped'" in sql
    assert "'pending'" in sql
    assert "error" not in sql
    assert "limit_reached" not in sql
    # Longest-waiting first so repeated bounded runs drain the backlog fairly.
    assert "ORDER BY d.updated_at ASC" in sql
    # One LLM call per channel even when several org members each hold a row.
    assert "DISTINCT ON (channel_id)" in sql
    # The workspace comes back with the row: the actor picker needs it to reject a
    # stand-in whose token belongs to a different Slack workspace.
    assert "team_id" in sql
    assert params == (mod.BACKFILL_STALE_MINUTES, [], False, 25)


def test_select_can_exclude_unreachable_workspaces():
    """The caller pages past workspaces nobody can describe, so the query must be
    able to skip them — otherwise the same dead rows fill the window every run."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    mod._select_undescribed_channels(cur, 25, exclude_teams=[TEAM_2],
                                     exclude_untagged=True)
    sql, params = cur.execute.call_args.args
    # NULL-guarded: `NULL = ANY(...)` is unknown, not false, so an unguarded
    # exclusion would silently drop every untagged row the moment one team is dead.
    assert "team_id IS NULL OR team_id <> ALL" in sql
    assert params == (mod.BACKFILL_STALE_MINUTES, [TEAM_2], True, 25)


def test_select_excludes_freshly_queued_pending_rows():
    """A young 'pending' row has a task in flight; re-enqueueing duplicates the
    LLM call, so the query filters on the staleness window."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    mod._select_undescribed_channels(cur, 10)
    sql = cur.execute.call_args.args[0]
    assert "updated_at <" in sql
    assert "make_interval" in sql


def test_select_recovers_a_generating_row_abandoned_by_a_dead_worker():
    """'generating' is set by the task itself, so no retry fires if the worker is
    killed (eviction/OOM) — nothing else resets it. Without this the row is
    stranded for good: a spinner in the UI, and invisible to the agent, which
    lists only 'ready'."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    mod._select_undescribed_channels(cur, 10)
    sql = cur.execute.call_args.args[0]
    assert "metadata_status IN ('pending', 'generating')" in sql
    # Only a STALE one: a 'generating' row inside the window is a live task, and
    # re-enqueueing it would duplicate the LLM call the claim exists to prevent.
    # Normalised so the assertion survives reformatting of the SQL literal.
    flat = " ".join(sql.split())
    assert "metadata_status IN ('pending', 'generating') AND updated_at <" in flat, (
        "a 'generating' row must be gated on the staleness window, not reclaimed outright"
    )


# --- beat registration ------------------------------------------------------

def test_implementation_is_a_plain_function():
    """Regression guard for the tests above.

    conftest stubs ``celery``, so ``@celery_app.task`` yields a MagicMock —
    tests aimed at the wrapper would exercise a mock and pass vacuously.
    """
    import types

    assert isinstance(mod._backfill_channel_descriptions, types.FunctionType)


def test_backfill_is_registered_on_the_beat_schedule():
    """Without a beat entry the tail never converges — that IS the bug.

    Asserted against the source text rather than an imported ``celery_config``:
    importing it for real needs Redis + the whole chat stack, which this
    hermetic unit test deliberately stubs out.
    """
    source = (Path(__file__).resolve().parents[3] / "celery_config.py").read_text()
    assert "backfill-slack-channel-descriptions" in source
    assert "routes.slack.slack_channel_metadata.backfill_channel_descriptions" in source


def test_stale_window_covers_the_observed_queue_wait():
    """The claim restamps ``updated_at``, so the window decides when a queued row
    is presumed lost. Descriptions sit in the default queue (observed ~85 min
    deep), so a short window re-claims rows that are merely waiting. The task's
    own claim makes a duplicate free, but churning statuses isn't.
    """
    assert mod.BACKFILL_STALE_MINUTES >= 120, (
        f"stale window {mod.BACKFILL_STALE_MINUTES}min is inside the observed "
        f"queue wait — rows still queued would be re-claimed every sweep"
    )
    assert mod.BACKFILL_STALE_MINUTES * 60 >= mod.BACKFILL_INTERVAL_SECONDS


def test_beat_schedule_uses_the_shared_interval_constant():
    """A literal here would drift from _rotate_orgs' interval and skip orgs."""
    source = (Path(__file__).resolve().parents[3] / "celery_config.py").read_text()
    entry = source.split("'backfill-slack-channel-descriptions'", 1)[1].split("},", 1)[0]
    assert "'schedule': BACKFILL_INTERVAL_SECONDS" in entry, (
        f"beat schedule must reference the shared constant, got: {entry!r}"
    )


def test_stale_window_is_shared_with_the_registration_path():
    """slack_channels backdates un-described rows past this window. If the two
    modules held separate copies, a change here would strand those rows for the
    difference — the dead zone this constant's sharing exists to prevent."""
    from routes.slack import slack_channels

    assert slack_channels.BACKFILL_STALE_MINUTES is mod.BACKFILL_STALE_MINUTES


# --- _assign_actors (credential selection in isolation) ---------------------

class _CountingProbe(mod._SlackCredProbe):
    """_SlackCredProbe with the credential fetch stubbed, counting round trips.

    ``teams`` maps connected user_id -> the workspace their Slack token belongs
    to; a user absent from it has no usable credentials.
    """

    def __init__(self, teams: dict):
        super().__init__()
        self._teams = teams
        self.lookups = 0

    def _creds(self, user_id: str):
        if user_id not in self._cache:
            self.lookups += 1
            self._cache[user_id] = (
                {"access_token": "xoxb-test", "team_id": self._teams[user_id]}
                if user_id in self._teams else None
            )
        return self._cache[user_id]


def _assign_actors_with(rows, org_users, teams: dict):
    """Return just the assignments; the unreachable-workspace set has its own tests."""
    assigned, _unreachable = mod._assign_actors(rows, org_users, _CountingProbe(teams))
    return assigned


def test_assign_actors_prefers_the_row_owner():
    """No extra credential lookup when the owner is still connected."""
    assigned = _assign_actors_with([("C1", USER_A, TEAM_1)], [USER_A, USER_A2],
                                   {USER_A: TEAM_1, USER_A2: TEAM_1})
    assert assigned == [(USER_A, "C1")]


def test_assign_actors_drops_channels_nobody_can_reach():
    assigned = _assign_actors_with([("C1", USER_A, TEAM_1)], [USER_A], {})
    assert assigned == []


def test_assign_actors_reports_the_workspaces_nobody_can_reach():
    """The caller needs this to page past dead rows: without it the oldest 25
    channels of a disconnected workspace fill the window every single sweep and
    the describable ones behind them are never reached."""
    _assigned, unreachable = mod._assign_actors(
        [("C1", USER_A, TEAM_1), ("C2", USER_B, TEAM_2)],
        [USER_A, USER_B], _CountingProbe({USER_B: TEAM_2}),
    )
    assert unreachable == {TEAM_1}


def test_assign_actors_rejects_an_owner_who_moved_workspace():
    """The owner registered the row, but a disconnect/reconnect can point their
    token at a different workspace — where this channel doesn't exist."""
    assigned = _assign_actors_with([("C1", USER_A, TEAM_1)], [USER_A],
                                   {USER_A: TEAM_2})
    assert assigned == []


def test_assign_actors_requires_the_fallback_to_match_the_workspace():
    """An org can connect several Slack workspaces. A token from the wrong one
    can't read the channel: conversations.info comes back empty and we'd save an
    invented description as 'ready'. Leave the row pending instead."""
    assigned = _assign_actors_with(
        [("C1", USER_A, TEAM_1)], [USER_A, USER_A2],
        {USER_A2: TEAM_2},  # owner disconnected; only stand-in is on another team
    )
    assert assigned == []


def test_assign_actors_picks_the_fallback_on_the_channels_workspace():
    """With both workspaces connected, the stand-in must be the one whose token
    belongs to the channel's workspace — not merely the first connected member."""
    assigned = _assign_actors_with(
        [("C1", USER_A, TEAM_2)], [USER_A, USER_A2, USER_B],
        {USER_A2: TEAM_1, USER_B: TEAM_2},
    )
    assert assigned == [(USER_B, "C1")]


def test_assign_actors_allows_an_untagged_row_when_the_org_has_one_workspace():
    """Rows registered before workspace tagging have team_id NULL. A single
    connected workspace makes the stand-in unambiguous, so don't strand them."""
    assigned = _assign_actors_with(
        [("C1", USER_A, None)], [USER_A, USER_A2], {USER_A2: TEAM_1},
    )
    assert assigned == [(USER_A2, "C1")]


def test_assign_actors_verifies_the_owner_of_an_untagged_row_too():
    """An untagged row can't be matched against a token, so the owner being
    connected proves nothing about which workspace they're on now. With two
    workspaces connected, assigning them would be a guess — and a wrong guess
    saves an invented description as 'ready'."""
    assigned = _assign_actors_with(
        [("C1", USER_A, None)], [USER_A, USER_B],
        {USER_A: TEAM_1, USER_B: TEAM_2},
    )
    assert assigned == []


def test_assign_actors_keeps_the_owner_on_an_untagged_single_workspace_row():
    """One connected workspace makes the owner's token provably the right one, so
    prefer them — the actor stays stable across runs."""
    assigned = _assign_actors_with(
        [("C1", USER_A, None)], [USER_A2, USER_A],
        {USER_A: TEAM_1, USER_A2: TEAM_1},
    )
    assert assigned == [(USER_A, "C1")]


def test_assign_actors_skips_an_untagged_row_in_a_multi_workspace_org():
    """Untagged row + more than one connected workspace = a guess. Don't take it."""
    assigned = _assign_actors_with(
        [("C1", USER_A, None)], [USER_A, USER_A2, USER_B],
        {USER_A2: TEAM_1, USER_B: TEAM_2},
    )
    assert assigned == []


def test_assign_actors_resolves_each_workspace_fallback_once():
    """Probing a stand-in is a DB + Vault round trip, so the per-workspace answer
    must be memoized across the batch."""
    probe = _CountingProbe({USER_A2: TEAM_1})
    rows = [(f"C{i}", USER_A, TEAM_1) for i in range(20)]
    assigned, _unreachable = mod._assign_actors(rows, [USER_A, USER_A2], probe)
    assert len(assigned) == 20
    # One miss for the owner + one hit for the stand-in, not one pair per row.
    assert probe.lookups == 2


# --- paging past unreachable workspaces -------------------------------------

def test_backfill_looks_past_a_workspace_nobody_can_describe():
    """The regression: the oldest rows belong to a disconnected workspace, so a
    single capped read hands back only dead rows and the describable channels
    behind them are never reached — every sweep, forever."""
    rows = ([("DEAD1", USER_B, TEAM_2), ("DEAD2", USER_B, TEAM_2)]
            + [("C1", USER_A, TEAM_1)])
    with patch.object(mod, "BACKFILL_MAX_PER_ORG", 2):
        _result, enqueued, _marked = _run(
            {ORG_A: [USER_A, USER_B]}, [rows],
            connected_users={USER_A},   # nobody is connected to TEAM_2
        )
    assert enqueued == [(USER_A, "C1")]


def test_backfill_paging_stops_once_the_budget_is_full():
    """A full page of describable rows must not trigger another read."""
    reads = {"n": 0}
    real_select = mod._select_undescribed_channels

    def counting(cur, limit, exclude_teams=None, exclude_untagged=False):
        reads["n"] += 1
        return real_select(cur, limit, exclude_teams, exclude_untagged)

    dbcm, _conn, _cur = _db(fetchall_rows=[("C1", USER_A, TEAM_1)])
    with patch("utils.auth.stateless_auth.users_by_org", return_value={ORG_A: [USER_A]}), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A), \
         patch("utils.auth.stateless_auth.get_credentials_from_db",
               return_value={"access_token": "xoxb-test", "team_id": TEAM_1}), \
         patch.object(mod, "_select_undescribed_channels", side_effect=counting), \
         patch.object(mod, "BACKFILL_MAX_PER_ORG", 1), \
         patch("routes.slack.slack_channels._mark_pending",
               side_effect=lambda cur, cids, stale_minutes=None: list(cids)), \
         patch("routes.slack.slack_channels._enqueue_metadata"):
        mod._backfill_channel_descriptions()
    assert reads["n"] == 1


def test_backfill_paging_is_bounded_per_org():
    """Re-reading past dead workspaces must not page through an unbounded backlog
    every 15 minutes."""
    dead_teams = [f"TDEAD{i}" for i in range(50)]
    rows = [(f"D{i}", USER_B, t) for i, t in enumerate(dead_teams)]
    reads = {"n": 0}

    def counting(cur, limit, exclude_teams=None, exclude_untagged=False):
        reads["n"] += 1
        excluded = set(exclude_teams or [])
        return [r for r in rows if r[2] not in excluded][:limit]

    dbcm, _conn, _cur = _db()
    with patch("utils.auth.stateless_auth.users_by_org", return_value={ORG_A: [USER_A]}), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A), \
         patch("utils.auth.stateless_auth.get_credentials_from_db",
               return_value={"access_token": "xoxb-test", "team_id": TEAM_1}), \
         patch.object(mod, "_select_undescribed_channels", side_effect=counting), \
         patch.object(mod, "BACKFILL_MAX_SCAN_PER_ORG", 5), \
         patch.object(mod, "BACKFILL_MAX_PER_ORG", 1), \
         patch("routes.slack.slack_channels._mark_pending",
               side_effect=lambda cur, cids, stale_minutes=None: list(cids)), \
         patch("routes.slack.slack_channels._enqueue_metadata"):
        mod._backfill_channel_descriptions()

    # 1 row scanned per read, so the scan ceiling caps the reads.
    assert reads["n"] == 5
