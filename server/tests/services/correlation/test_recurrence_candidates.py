"""Recent-incident candidate list handed to the recurrence agent (layer-1 fix:
the agent no longer depends on picking the right list_incidents status filter,
and only sees incidents whose group is still open)."""

import os
import sys
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

import services.correlation.recurrence_agent as ra  # noqa: E402
from services.correlation.recurrence_config import GROUP_IDLE_HOURS  # noqa: E402

ME = str(uuid.uuid4())
ROOT = str(uuid.uuid4())
CHILD = str(uuid.uuid4())
_T0 = datetime(2026, 9, 8, 13, 49, 2)


def _recent():
    return [
        {"id": CHILD, "title": "High memory", "service": "payments-api", "status": "analyzed",
         "fired_at_iso": "2026-09-08T14:01:00Z", "recurrence_of": ROOT},
        {"id": ROOT, "title": "High memory", "service": "payments-api", "status": "analyzed",
         "fired_at_iso": "2026-09-08T13:49:02Z", "recurrence_of": None},
    ]


class TestRecentIncidentsLines:
    def test_lists_candidates_with_root_pointer(self):
        text = "\n".join(ra._recent_incidents_lines(_recent()))
        assert f"last {GROUP_IDLE_HOURS}h" in text
        assert f"- {CHILD} | fired 2026-09-08T14:01:00Z | analyzed | payments-api | High memory | recurrence of {ROOT}" in text
        assert f"- {ROOT} | fired 2026-09-08T13:49:02Z | analyzed | payments-api | High memory" in text
        assert "closed" in text

    def test_empty_list_says_answer_new(self):
        text = "\n".join(ra._recent_incidents_lines([]))
        assert "(none)" in text
        assert "answer new" in text

    def test_truncated_list_is_not_presented_as_complete(self):
        text = "\n".join(ra._recent_incidents_lines(_recent(), truncated=True))
        assert "more incidents fired" in text
        assert "list_incidents" in text
        assert "Only these" not in text
        assert "more incidents" not in "\n".join(ra._recent_incidents_lines(_recent()))

    def test_input_block_carries_the_section(self):
        ctx = {"title": "t", "service": "s", "source_type": "datadog", "severity": "critical",
               "fired_at_iso": "x", "summary": "conclusion", "recent": _recent()}
        block = ra._build_input_block(ctx, ME, "after")
        assert "### Completed investigation conclusion" in block
        assert "### Recent incidents in this org" in block
        assert block.index("### Recent incidents") > block.index("conclusion")
        assert CHILD in block
        # Incident Index section is present (empty here) and precedes recent.
        assert "### Incident Index" in block
        assert block.index("### Incident Index") < block.index("### Recent incidents")


class TestFetchRecentIncidents:
    def test_query_excludes_self_and_merged_within_idle_window(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (uuid.UUID(CHILD), "High memory", "payments-api", "analyzed", _T0, uuid.UUID(ROOT)),
            (uuid.UUID(ROOT), None, None, "investigating", _T0, None),
        ]
        recent, truncated = ra._fetch_recent_incidents(cursor, ME)
        sql = " ".join(cursor.execute.call_args.args[0].split())
        params = cursor.execute.call_args.args[1]
        assert "id <> %s" in sql
        assert "status <> 'merged'" in sql
        assert "make_interval(hours => %s)" in sql
        assert "ORDER BY COALESCE(alert_fired_at, started_at) DESC" in sql
        # one extra row is fetched only to learn whether the window overflowed
        assert params == (ME, GROUP_IDLE_HOURS, ra.RECENT_CANDIDATES_LIMIT + 1)
        assert truncated is False
        assert recent[0]["id"] == CHILD
        assert recent[0]["recurrence_of"] == ROOT
        assert recent[1] == {"id": ROOT, "title": "", "service": "", "status": "investigating",
                             "fired_at_iso": recent[1]["fired_at_iso"], "recurrence_of": None}

    def test_overflow_is_capped_and_flagged(self):
        cursor = MagicMock()
        cursor.fetchall.return_value = [
            (uuid.uuid4(), "t", "svc", "analyzed", _T0, None) for _ in range(ra.RECENT_CANDIDATES_LIMIT + 1)
        ]
        recent, truncated = ra._fetch_recent_incidents(cursor, ME)
        assert len(recent) == ra.RECENT_CANDIDATES_LIMIT
        assert truncated is True

    def test_context_includes_recent(self):
        cursor = MagicMock()
        cursor.fetchone.return_value = ("title", "svc", "datadog", "critical", _T0, _T0, "summary", {})
        cursor.fetchall.return_value = [(uuid.UUID(ROOT), "t", "svc", "analyzed", _T0, None)]
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cursor
        pool = MagicMock()
        pool.get_admin_connection.return_value.__enter__.return_value = conn
        # read_index is exercised separately; stub it so this test asserts only
        # the incident-context query behavior (execute count below).
        with patch.object(ra, "db_pool", pool), \
                patch.object(ra, "set_rls_context", return_value="org-1"), \
                patch("services.memory.incident_index.read_index", return_value="- [INC x | d | s | resolved] syn"):
            ctx = ra._fetch_incident_context(ME, "u1")
        assert ctx["recent"] == [{"id": ROOT, "title": "t", "service": "svc", "status": "analyzed",
                                  "fired_at_iso": ctx["recent"][0]["fired_at_iso"], "recurrence_of": None}]
        assert ctx["recent_truncated"] is False
        assert ctx["incident_index"] == "- [INC x | d | s | resolved] syn"
        assert cursor.execute.call_count == 2
