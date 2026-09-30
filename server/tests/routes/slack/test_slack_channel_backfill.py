"""Tests for the periodic Slack channel-description backfill.

Agent routing only offers channels with ``metadata_status = 'ready'``, and
descriptions were previously only enqueued by a user-triggered reconcile pass
that is capped per pass — so in a large workspace the tail of Aurora's
memberships never got described. These tests cover the beat task that closes
that gap (see issue #673).

These exercise ``_backfill_channel_descriptions`` (the implementation) rather
than the ``@celery_app.task``-decorated wrapper: conftest stubs ``celery``, so
the decorator yields a MagicMock and the real logic would never run.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from routes.slack import slack_channel_metadata as mod

ORG_A = "00000000-0000-0000-0000-00000000000a"
ORG_B = "00000000-0000-0000-0000-00000000000b"
USER_A = "11111111-1111-1111-1111-11111111111a"
USER_A2 = "11111111-1111-1111-1111-11111111112a"
USER_B = "11111111-1111-1111-1111-11111111111b"


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


def _run(users_by_org, rows_per_org, connected_users=None, rls_ok=True):
    """Run the backfill task, returning (result, enqueued, mark_pending_calls).

    ``rows_per_org`` is a list of row batches, consumed in org iteration order.
    """
    enqueued = []
    marked = []
    batches = list(rows_per_org)

    def fake_select(cur, limit):
        batch = batches.pop(0) if batches else []
        return batch[:limit]

    def fake_client(uid):
        if connected_users is None:
            return MagicMock()
        return MagicMock() if uid in connected_users else None

    dbcm, _conn, _cur = _db()
    with patch.object(mod, "_users_by_org", return_value=users_by_org), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context",
               return_value=(ORG_A if rls_ok else None)), \
         patch("connectors.slack_connector.client.get_slack_client_for_user",
               side_effect=fake_client), \
         patch.object(mod, "_select_undescribed_channels", side_effect=fake_select), \
         patch("routes.slack.slack_channels._mark_pending",
               side_effect=lambda cur, cids: marked.append(list(cids))), \
         patch("routes.slack.slack_channels._enqueue_metadata",
               side_effect=lambda uid, cid: enqueued.append((uid, cid))):
        result = mod._backfill_channel_descriptions()
    return result, enqueued, marked


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
    connected member so the channel isn't stranded."""
    _result, enqueued, _marked = _run(
        {ORG_A: [USER_A, USER_A2]},
        [[("C1", USER_A)]],
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

    def flaky_select(cur, limit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient DB error")
        return [("C2", USER_B)]

    dbcm, _conn, _cur = _db()
    with patch.object(mod, "_users_by_org", return_value={ORG_A: [USER_A], ORG_B: [USER_B]}), \
         patch("utils.db.connection_pool.db_pool.get_admin_connection", return_value=dbcm), \
         patch("utils.auth.stateless_auth.set_rls_context", return_value=ORG_A), \
         patch("connectors.slack_connector.client.get_slack_client_for_user",
               return_value=MagicMock()), \
         patch.object(mod, "_select_undescribed_channels", side_effect=flaky_select), \
         patch("routes.slack.slack_channels._mark_pending"), \
         patch("routes.slack.slack_channels._enqueue_metadata",
               side_effect=lambda uid, cid: enqueued.append((uid, cid))):
        result = mod._backfill_channel_descriptions()

    assert enqueued == [(USER_B, "C2")]
    assert result == {"orgs": 1, "enqueued": 1}


def test_backfill_returns_empty_when_org_enumeration_fails():
    with patch.object(mod, "_users_by_org", side_effect=RuntimeError("db down")):
        assert mod._backfill_channel_descriptions() == {"orgs": 0, "enqueued": 0}


# --- the selection query itself ---------------------------------------------

def test_select_only_targets_member_channels_awaiting_description():
    """'error'/'limit_reached' must NOT be swept: retrying a hard failure or a
    cost-capped org on a timer would burn quota in a loop."""
    cur = MagicMock()
    cur.fetchall.return_value = [("C1", USER_A)]
    assert mod._select_undescribed_channels(cur, 25) == [("C1", USER_A)]

    sql, params = cur.execute.call_args.args
    assert "is_member" in sql
    assert "'skipped'" in sql and "'pending'" in sql
    assert "error" not in sql and "limit_reached" not in sql
    # Longest-waiting first so repeated bounded runs drain the backlog fairly.
    assert "ORDER BY d.updated_at ASC" in sql
    # One LLM call per channel even when several org members each hold a row.
    assert "DISTINCT ON (channel_id)" in sql
    assert params == (mod.BACKFILL_STALE_MINUTES, 25)


def test_select_excludes_freshly_queued_pending_rows():
    """A young 'pending' row has a task in flight; re-enqueueing duplicates the
    LLM call, so the query filters on the staleness window."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    mod._select_undescribed_channels(cur, 10)
    sql = cur.execute.call_args.args[0]
    assert "updated_at <" in sql and "make_interval" in sql


# --- beat registration ------------------------------------------------------

def test_implementation_is_a_plain_function():
    """Regression guard for the tests above.

    conftest stubs ``celery``, so ``@celery_app.task`` yields a MagicMock. If
    these tests targeted the decorated wrapper they would exercise a mock and
    pass while asserting nothing — which is exactly what happened before the
    implementation was split out.
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
