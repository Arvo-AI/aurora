"""Pre-RCA check (services/rca_routing.py): classify, tier choice and persistence."""

import os
import sys
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from services import rca_routing as rr  # noqa: E402

MAIN = "anthropic/claude-opus-5.5"
CHEAP = "anthropic/claude-haiku-5.5"

_ENV_KEYS = (
    "RCA_MODEL_ROUTING_MODE", "RCA_ROUTING_CHEAP_MODEL", "RCA_ROUTING_SIMPLE_SEVERITIES",
    "RCA_ROUTING_MAIN_SEVERITIES", "RCA_ROUTING_REPEAT_LOOKBACK_HOURS",
    "RCA_ROUTING_REPEAT_MIN_SCORE", "ORCHESTRATOR_ENABLED",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def _live(monkeypatch, cheap=CHEAP):
    monkeypatch.setenv("RCA_MODEL_ROUTING_MODE", "live")
    if cheap is not None:
        monkeypatch.setenv("RCA_ROUTING_CHEAP_MODEL", cheap)


def _incident(**kw):
    base = {
        "id": "11111111-1111-1111-1111-111111111111",
        "source_type": "datadog",
        "severity": "high",
        "alert_title": "High CPU on api",
        "alert_service": "api",
        "alert_metadata": {},
        "started_at": datetime(2026, 11, 10, 12, 0, 0),
    }
    base.update(kw)
    return base


_PRIOR = {"id": "22222222-2222-2222-2222-222222222222", "title": "High CPU on api",
          "summary": "Runaway cron job.", "evidence": "same alert title and service"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_mode_defaults_off(self):
        assert rr.get_routing_mode() == "off"

    @pytest.mark.parametrize("raw,expected", [("LIVE", "live"), (" shadow ", "shadow"), ("off", "off")])
    def test_mode_parsing(self, monkeypatch, raw, expected):
        monkeypatch.setenv("RCA_MODEL_ROUTING_MODE", raw)
        assert rr.get_routing_mode() == expected

    def test_unknown_mode_degrades_to_off(self, monkeypatch):
        monkeypatch.setenv("RCA_MODEL_ROUTING_MODE", "lvie")
        assert rr.get_routing_mode() == "off"

    def test_severity_defaults(self):
        assert rr.get_simple_severities() == frozenset({"low"})
        assert rr.get_main_severities() == frozenset({"critical"})

    def test_severity_lists_parse_and_can_be_emptied(self, monkeypatch):
        monkeypatch.setenv("RCA_ROUTING_SIMPLE_SEVERITIES", " Low, MEDIUM ,")
        monkeypatch.setenv("RCA_ROUTING_MAIN_SEVERITIES", "")
        assert rr.get_simple_severities() == frozenset({"low", "medium"})
        assert rr.get_main_severities() == frozenset()

    @pytest.mark.parametrize("raw", ["abc", "-1", "nan", "1e9"])
    def test_bad_lookback_uses_default(self, monkeypatch, raw):
        monkeypatch.setenv("RCA_ROUTING_REPEAT_LOOKBACK_HOURS", raw)
        assert rr.get_repeat_lookback_hours() == 168.0

    def test_bad_score_uses_default(self, monkeypatch):
        monkeypatch.setenv("RCA_ROUTING_REPEAT_MIN_SCORE", "1.5")
        assert rr.get_repeat_min_score() == 0.75

    @pytest.mark.parametrize("source", ["action", "chat", "slack", "teams", "google_chat",
                                        "postmortem_generation", "change_gating", "SLACK"])
    def test_follow_up_sources_never_routed(self, source):
        assert rr.should_route(source, "inc") is False

    def test_alert_sources_routed_only_with_incident(self):
        assert rr.should_route("datadog", "inc") is True
        assert rr.should_route("datadog", None) is False


# ---------------------------------------------------------------------------
# classify + tier
# ---------------------------------------------------------------------------

class TestClassify:
    def test_critical_is_hard_even_when_repeat(self):
        route, reason = rr.classify(_incident(severity="critical"), _PRIOR)
        assert route == rr.ROUTE_HARD
        assert "critical" in reason

    def test_repeat_wins_over_severity(self):
        route, reason = rr.classify(_incident(severity="high"), _PRIOR)
        assert route == rr.ROUTE_REPEAT
        assert _PRIOR["id"] in reason

    def test_low_without_prior_is_simple(self):
        assert rr.classify(_incident(severity="low"), None)[0] == rr.ROUTE_SIMPLE

    def test_high_without_prior_is_hard(self):
        assert rr.classify(_incident(severity="high"), None)[0] == rr.ROUTE_HARD

    @pytest.mark.parametrize("sev", [None, "", "  "])
    def test_unknown_severity_is_hard(self, sev):
        route, reason = rr.classify(_incident(severity=sev), None)
        assert route == rr.ROUTE_HARD
        assert "unknown" in reason


class TestResolveTier:
    def test_hard_always_main(self, monkeypatch):
        _live(monkeypatch)
        assert rr._resolve_tier(rr.ROUTE_HARD, "live", MAIN)[:2] == (rr.TIER_MAIN, MAIN)

    def test_live_routes_cheap(self, monkeypatch):
        _live(monkeypatch)
        assert rr._resolve_tier(rr.ROUTE_SIMPLE, "live", MAIN) == (rr.TIER_CHEAP, CHEAP, "")

    def test_shadow_keeps_main(self, monkeypatch):
        monkeypatch.setenv("RCA_ROUTING_CHEAP_MODEL", CHEAP)
        tier, model, note = rr._resolve_tier(rr.ROUTE_REPEAT, "shadow", MAIN)
        assert (tier, model) == (rr.TIER_MAIN, MAIN)
        assert "shadow" in note

    def test_live_without_cheap_model_keeps_main(self, monkeypatch):
        _live(monkeypatch, cheap=None)
        tier, model, note = rr._resolve_tier(rr.ROUTE_SIMPLE, "live", MAIN)
        assert (tier, model) == (rr.TIER_MAIN, MAIN)
        assert "RCA_ROUTING_CHEAP_MODEL" in note

    def test_orchestrator_keeps_main(self, monkeypatch):
        _live(monkeypatch)
        monkeypatch.setenv("ORCHESTRATOR_ENABLED", "true")
        assert rr._resolve_tier(rr.ROUTE_SIMPLE, "live", MAIN)[0] == rr.TIER_MAIN


# ---------------------------------------------------------------------------
# Prior-RCA lookup
# ---------------------------------------------------------------------------

class _Cursor:
    """Returns queued fetchone() results in order and records executed SQL."""

    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestFindPriorRca:
    def test_high_score_hint_matches(self):
        cur = _Cursor([("p1", "t", "s")])
        inc = _incident(alert_metadata={"correlation_hint": {"incident_id": "p1", "score": 0.9}})
        prior = rr._find_prior_rca(cur, inc)
        assert prior["id"] == "p1"
        assert "correlator" in prior["evidence"]
        assert len(cur.executed) == 1
        assert "rca_model_tier IS DISTINCT FROM 'cheap'" in cur.executed[0][0]

    def test_low_score_hint_falls_through_to_fingerprint(self):
        cur = _Cursor([("p2", "t", "s")])
        inc = _incident(alert_metadata={"correlation_hint": {"incident_id": "p1", "score": 0.6}})
        prior = rr._find_prior_rca(cur, inc)
        assert prior["id"] == "p2"
        assert prior["evidence"] == "same alert title and service"
        assert len(cur.executed) == 1
        assert "alert_title = %s" in cur.executed[0][0]

    def test_unqualified_hint_falls_through_to_fingerprint(self):
        cur = _Cursor([None, None])
        inc = _incident(alert_metadata={"correlation_hint": {"incident_id": "p1", "score": 0.95}})
        assert rr._find_prior_rca(cur, inc) is None
        assert len(cur.executed) == 2

    def test_garbage_hint_score_ignored(self):
        cur = _Cursor([None])
        inc = _incident(alert_metadata={"correlation_hint": {"incident_id": "p1", "score": "x"}})
        assert rr._find_prior_rca(cur, inc) is None
        assert len(cur.executed) == 1

    def test_no_title_skips_fingerprint(self):
        cur = _Cursor([])
        assert rr._find_prior_rca(cur, _incident(alert_title="")) is None
        assert cur.executed == []

    def test_zero_lookback_disables_fingerprint(self, monkeypatch):
        monkeypatch.setenv("RCA_ROUTING_REPEAT_LOOKBACK_HOURS", "0")
        cur = _Cursor([])
        assert rr._find_prior_rca(cur, _incident()) is None
        assert cur.executed == []

    def test_fingerprint_params(self):
        cur = _Cursor([None])
        rr._find_prior_rca(cur, _incident())
        params = cur.executed[0][1]
        assert params[1:4] == ("datadog", "High CPU on api", "api")
        assert params[5] == 168.0 * 3600


# ---------------------------------------------------------------------------
# route_rca end to end (DB mocked)
# ---------------------------------------------------------------------------

def _incident_row(severity="low", meta=None):
    return ("11111111-1111-1111-1111-111111111111", "datadog", severity,
            "High CPU on api", "api", meta or {}, datetime(2026, 11, 10, 12, 0))


@contextmanager
def _db(cursor, org="org-1"):
    conn = MagicMock()
    conn.cursor.return_value = cursor

    @contextmanager
    def _get_conn():
        yield conn

    pool = MagicMock()
    pool.get_admin_connection = _get_conn
    with patch("utils.db.connection_pool.db_pool", pool), \
            patch("utils.auth.stateless_auth.set_rls_context", return_value=org) as rls:
        yield conn, rls


def _update(cursor):
    updates = [p for sql, p in cursor.executed if "UPDATE incidents" in sql]
    assert len(updates) == 1
    return updates[0]


class TestRouteRca:
    def test_off_does_nothing(self):
        with patch("utils.db.connection_pool.db_pool") as pool:
            assert rr.route_rca("u1", "inc", MAIN) is None
            pool.get_admin_connection.assert_not_called()

    def test_live_simple_routes_cheap_and_persists(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([_incident_row("low"), None])
        with _db(cur) as (conn, rls):
            d = rr.route_rca("u1", "inc", MAIN)
        rls.assert_called_once()
        conn.commit.assert_called_once()
        assert (d.route, d.tier, d.model) == ("simple", "cheap", CHEAP)
        params = _update(cur)
        assert params[:3] == ("simple", "cheap", CHEAP)
        assert params[-1] == "inc"
        assert rr.prior_rca_context(d) is None  # simple: no prior hint

    def test_live_repeat_gets_prior_context(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([_incident_row("high"), ("p9", "High CPU on api", "Cron job. " * 400)])
        with _db(cur):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.route, d.tier) == ("repeat", "cheap")
        ctx = rr.prior_rca_context(d)
        assert ctx["incident_id"] == "p9"
        assert len(ctx["summary"]) <= rr._PRIOR_SUMMARY_MAX_CHARS + 1

    def test_shadow_records_route_but_uses_main(self, monkeypatch):
        monkeypatch.setenv("RCA_MODEL_ROUTING_MODE", "shadow")
        monkeypatch.setenv("RCA_ROUTING_CHEAP_MODEL", CHEAP)
        cur = _Cursor([_incident_row("low"), None])
        with _db(cur):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.route, d.tier, d.model) == ("simple", "main", MAIN)
        assert _update(cur)[:3] == ("simple", "main", MAIN)
        assert rr.prior_rca_context(d) is None

    def test_critical_stays_main(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([_incident_row("critical"), None])
        with _db(cur):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.route, d.tier, d.model) == ("hard", "main", MAIN)

    def test_missing_org_falls_back_to_main(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([_incident_row("low")])
        with _db(cur, org=None):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.tier, d.model) == ("main", MAIN)
        assert cur.executed == []

    def test_db_error_falls_back_to_main(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([])
        cur.execute = MagicMock(side_effect=RuntimeError("boom"))
        with _db(cur):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.route, d.tier, d.model) == ("hard", "main", MAIN)

    def test_missing_incident_uses_main(self, monkeypatch):
        _live(monkeypatch)
        cur = _Cursor([None])
        with _db(cur):
            d = rr.route_rca("u1", "inc", MAIN)
        assert (d.tier, d.model) == ("main", MAIN)


class TestPriorPromptBlock:
    def test_block_wraps_summary(self):
        from chat.backend.agent.prompt.background import _format_prior_rca
        text = _format_prior_rca({"incident_id": "p9", "title": "High CPU", "summary": "Cron."})
        assert "p9" in text and "High CPU" in text
        assert "<<PRIOR_RCA_SUMMARY>>\nCron.\n<<END_PRIOR_RCA_SUMMARY>>" in text
        assert "Verify it against current data" in text

    def test_block_without_summary(self):
        from chat.backend.agent.prompt.background import _format_prior_rca
        assert "<<PRIOR_RCA_SUMMARY>>" not in _format_prior_rca({"incident_id": "p9"})
