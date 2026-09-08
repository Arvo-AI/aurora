"""_search_similar_rcas_impl: DB candidate fetch + similarity ranking.

The embedding-backed SimilarityStrategy is patched to a deterministic scorer so
these tests exercise the query construction, RLS handling, filtering, sorting
and limiting without needing a real DB or embedding provider.
"""

import os
import sys
import uuid
from unittest.mock import MagicMock

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

import services.correlation.recurrence_agent as ra  # noqa: E402


def _mk_row(title, service="api", source="datadog", severity="high", summary="rca"):
    return (str(uuid.uuid4()), title, service, source, severity, summary)


@pytest.fixture
def db(monkeypatch):
    """Patch db_pool + set_rls_context; return the cursor mock for assertions."""
    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    pool = MagicMock()
    pool.get_admin_connection.return_value.__enter__.return_value = conn
    pool.get_admin_connection.return_value.__exit__.return_value = False
    monkeypatch.setattr(ra, "db_pool", pool)
    monkeypatch.setattr(ra, "set_rls_context", lambda c, cn, u, log_prefix="": "org-1")
    return cursor


def _patch_strategy(monkeypatch, score_by_title):
    """Make SimilarityStrategy.score return a lookup by candidate title."""
    from services.correlation.strategies import similarity as sim

    def _score(self, alert_title, alert_service, incident_title, incident_services):
        return score_by_title.get(incident_title, 0.0)

    monkeypatch.setattr(sim.SimilarityStrategy, "score", _score)


class TestSearchSimilarRcas:
    def test_rls_miss_returns_empty(self, db, monkeypatch):
        monkeypatch.setattr(ra, "set_rls_context", lambda c, cn, u, log_prefix="": None)
        out = ra._search_similar_rcas_impl("u1", "High CPU")
        assert out == []
        db.execute.assert_not_called()

    def test_filters_below_min_score(self, db, monkeypatch):
        db.fetchall.return_value = [_mk_row("High CPU on api"), _mk_row("Unrelated disk alert")]
        _patch_strategy(monkeypatch, {"High CPU on api": 0.9, "Unrelated disk alert": 0.1})
        out = ra._search_similar_rcas_impl("u1", "High CPU", min_score=0.5)
        assert [r["alert_title"] for r in out] == ["High CPU on api"]
        assert out[0]["similarity"] == 0.9

    def test_sorts_desc_and_limits(self, db, monkeypatch):
        rows = [_mk_row("a"), _mk_row("b"), _mk_row("c")]
        db.fetchall.return_value = rows
        _patch_strategy(monkeypatch, {"a": 0.6, "b": 0.95, "c": 0.7})
        out = ra._search_similar_rcas_impl("u1", "q", min_score=0.5, limit=2)
        assert [r["alert_title"] for r in out] == ["b", "c"]

    def test_source_type_narrows_query(self, db, monkeypatch):
        db.fetchall.return_value = []
        _patch_strategy(monkeypatch, {})
        ra._search_similar_rcas_impl("u1", "q", source_type="datadog")
        sql, params = db.execute.call_args.args
        assert "source_type = %s" in sql
        assert params == ["datadog"]

    def test_no_source_type_omits_filter(self, db, monkeypatch):
        db.fetchall.return_value = []
        _patch_strategy(monkeypatch, {})
        ra._search_similar_rcas_impl("u1", "q")
        sql, params = db.execute.call_args.args
        assert "source_type = %s" not in sql
        assert params == []

    def test_skips_candidates_without_title(self, db, monkeypatch):
        db.fetchall.return_value = [_mk_row(""), _mk_row("Real incident")]
        _patch_strategy(monkeypatch, {"Real incident": 0.8})
        out = ra._search_similar_rcas_impl("u1", "q", min_score=0.5)
        assert [r["alert_title"] for r in out] == ["Real incident"]

    def test_db_exception_degrades_to_empty(self, db, monkeypatch):
        db.execute.side_effect = RuntimeError("boom")
        _patch_strategy(monkeypatch, {})
        assert ra._search_similar_rcas_impl("u1", "q") == []
