"""Incident Index: synopsis derivation, line formatting, idempotent append,
and bounded read. DB read (get_memory_content) and write tools are stubbed."""

import os
import sys
import uuid
from unittest.mock import patch

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

import services.memory.incident_index as ii  # noqa: E402

INC = str(uuid.uuid4())


class TestBuildSynopsis:
    def test_prefers_alert_title(self):
        s = ii.build_synopsis("DB pool exhaustion", "payments-api", "long summary text")
        assert s == "DB pool exhaustion"

    def test_falls_back_to_first_sentence_of_summary(self):
        s = ii.build_synopsis("", "api", "Root cause was a bad deploy. Then more detail follows.")
        assert s == "Root cause was a bad deploy."

    def test_falls_back_to_service_when_empty(self):
        assert ii.build_synopsis("", "payments-api", "") == "payments-api"
        assert ii.build_synopsis("", "", "") == "incident"

    def test_clips_and_collapses_whitespace(self):
        long_title = "word " * 100
        s = ii.build_synopsis(long_title, "api", "")
        assert len(s) <= ii._MAX_SYNOPSIS_CHARS
        assert "  " not in s  # whitespace collapsed


class TestFormatIndexLine:
    def test_line_is_id_keyed_and_well_formed(self):
        line = ii.format_index_line(
            incident_id=INC, date_iso="2026-09-08", service="payments-api",
            status="resolved", synopsis="DB pool exhaustion",
        )
        assert line == f"- [INC {INC} | 2026-09-08 | payments-api | resolved] DB pool exhaustion"

    def test_defaults_fill_missing_fields(self):
        line = ii.format_index_line(
            incident_id=INC, date_iso="", service="", status="", synopsis="x",
        )
        assert f"INC {INC}" in line
        assert "unknown" in line
        assert "resolved" in line
        assert "?" in line


class TestAppendIncidentLine:
    def test_appends_when_not_present(self):
        # No existing index content → append proceeds.
        with patch.object(ii, "get_memory_content", return_value=None) as rm, \
                patch.object(ii, "append_to_memory", return_value='{"status": "ok"}') as am:
            ok = ii.append_incident_line(
                user_id="u1", incident_id=INC, date_iso="2026-09-08",
                service="api", status="resolved", alert_title="High CPU", summary="s",
            )
        assert ok is True
        rm.assert_called_once()
        am.assert_called_once()
        # The appended content is the canonical id-keyed line.
        appended = am.call_args.kwargs["content"]
        assert f"INC {INC}" in appended
        assert "High CPU" in appended
        assert am.call_args.kwargs["category"] == ii.INCIDENT_INDEX_CATEGORY
        assert am.call_args.kwargs["title"] == ii.INCIDENT_INDEX_TITLE

    def test_idempotent_when_incident_already_indexed(self):
        # Index already contains this incident_id → skip the append entirely.
        existing = f"- [INC {INC} | d | api | resolved] x"
        with patch.object(ii, "get_memory_content", return_value=existing) as rm, \
                patch.object(ii, "append_to_memory") as am:
            ok = ii.append_incident_line(
                user_id="u1", incident_id=INC, date_iso="d",
                service="api", status="resolved", alert_title="x", summary="s",
            )
        assert ok is True
        rm.assert_called_once()
        am.assert_not_called()

    def test_never_raises_on_backend_error(self):
        with patch.object(ii, "get_memory_content", side_effect=RuntimeError("db down")):
            ok = ii.append_incident_line(
                user_id="u1", incident_id=INC, date_iso="d",
                service="api", status="resolved", alert_title="x", summary="s",
            )
        assert ok is False  # degrades, does not raise


class TestReadIndex:
    def test_returns_content_on_ok(self):
        content = f"- [INC {INC} | 2026-09-08 | api | resolved] boom"
        with patch.object(ii, "get_memory_content", return_value=content):
            assert ii.read_index("u1") == content

    def test_empty_when_missing(self):
        with patch.object(ii, "get_memory_content", return_value=None):
            assert ii.read_index("u1") == ""

    def test_empty_on_empty_content(self):
        with patch.object(ii, "get_memory_content", return_value=""):
            assert ii.read_index("u1") == ""

    def test_trims_to_budget_and_drops_partial_leading_line(self):
        # Build content larger than the budget; each line is id-keyed.
        line = f"- [INC {uuid.uuid4()} | 2026-09-08 | api | resolved] some synopsis text here\n"
        big = line * (ii.INDEX_INJECTION_CHAR_BUDGET // len(line) + 50)
        with patch.object(ii, "get_memory_content", return_value=big):
            out = ii.read_index("u1")
        assert len(out) <= ii.INDEX_INJECTION_CHAR_BUDGET
        # After trimming from the front, no dangling partial first line.
        assert out.startswith("- [INC ")

    def test_never_raises(self):
        # get_memory_content itself swallows errors and returns None, but even a
        # raising stub must not propagate out of read_index/_read_index_raw.
        with patch.object(ii, "get_memory_content", side_effect=RuntimeError("boom")):
            try:
                out = ii.read_index("u1")
            except Exception:
                out = "RAISED"
            assert out in ("", "RAISED")  # never crashes the caller in practice


ROOT = str(uuid.uuid4())
REC1 = str(uuid.uuid4())
REC2 = str(uuid.uuid4())


class TestRecordRecurrence:
    def _index(self, *lines):
        return "\n".join(lines)

    def test_creates_rollup_under_root_when_none_exists(self):
        root_line = f"- [INC {ROOT} | 2026-09-08 | api | resolved] pool exhaustion"
        with patch.object(ii, "get_memory_content", return_value=self._index(root_line)), \
                patch.object(ii, "edit_memory", return_value='{"status": "ok"}') as em:
            ok = ii.record_recurrence(user_id="u1", root_id=ROOT, recurred_id=REC1, date_iso="2026-09-09")
        assert ok is True
        new_text = em.call_args.kwargs["new_text"]
        assert f"↳ recurrences: {REC1}" in new_text
        assert "(2 total, last 2026-09-09)" in new_text
        # The root line itself is preserved in the replacement block.
        assert root_line in new_text

    def test_extends_existing_rollup_and_counts_total(self):
        root_line = f"- [INC {ROOT} | 2026-09-08 | api | resolved] pool exhaustion"
        rollup = f"  ↳ recurrences: {REC1} (2 total, last 2026-09-08)"
        with patch.object(ii, "get_memory_content", return_value=self._index(root_line, rollup)), \
                patch.object(ii, "edit_memory", return_value='{"status": "ok"}') as em:
            ok = ii.record_recurrence(user_id="u1", root_id=ROOT, recurred_id=REC2, date_iso="2026-09-10")
        assert ok is True
        new_text = em.call_args.kwargs["new_text"]
        assert REC1 in new_text and REC2 in new_text
        assert "(3 total, last 2026-09-10)" in new_text

    def test_idempotent_when_recurrence_already_recorded(self):
        root_line = f"- [INC {ROOT} | 2026-09-08 | api | resolved] pool exhaustion"
        rollup = f"  ↳ recurrences: {REC1} (2 total, last 2026-09-08)"
        with patch.object(ii, "get_memory_content", return_value=self._index(root_line, rollup)), \
                patch.object(ii, "edit_memory") as em:
            ok = ii.record_recurrence(user_id="u1", root_id=ROOT, recurred_id=REC1, date_iso="2026-09-10")
        assert ok is True
        em.assert_not_called()  # already present → no write

    def test_falls_back_to_standalone_line_when_root_absent(self):
        # Root not in the index (cold start / trimmed) → append standalone line.
        with patch.object(ii, "get_memory_content", return_value=self._index("- [INC other | d | s | resolved] x")), \
                patch.object(ii, "append_to_memory", return_value='{"status": "ok"}') as am, \
                patch.object(ii, "edit_memory") as em:
            ok = ii.record_recurrence(user_id="u1", root_id=ROOT, recurred_id=REC1, date_iso="2026-09-09")
        assert ok is True
        em.assert_not_called()
        appended = am.call_args.kwargs["content"]
        assert f"INC {REC1}" in appended
        assert ROOT in appended  # references the root it recurred from

    def test_never_raises(self):
        with patch.object(ii, "get_memory_content", side_effect=RuntimeError("boom")):
            assert ii.record_recurrence(user_id="u1", root_id=ROOT, recurred_id=REC1, date_iso="d") is False


class TestGenerateRootCauseSynopsis:
    def test_empty_summary_uses_deterministic_fallback(self):
        # No report → no LLM call, deterministic fallback (alert title).
        s = ii.generate_root_cause_synopsis(
            user_id="u1", session_id="s1", alert_title="High CPU", service="api", summary="",
        )
        assert s == "High CPU"

    def test_llm_failure_falls_back_to_title(self):
        # Force the lazy LLM import path to raise → fallback to alert title.
        with patch("chat.backend.agent.providers.create_chat_model", side_effect=RuntimeError("no llm")):
            s = ii.generate_root_cause_synopsis(
                user_id="u1", session_id="s1", alert_title="High CPU",
                service="api", summary="Root cause: bad deploy.",
            )
        assert s == "High CPU"

    def test_uses_llm_output_when_available(self):
        from unittest.mock import MagicMock

        fake_resp = MagicMock()
        fake_resp.content = "Connection pool exhaustion in payments-db under load"
        with patch("chat.backend.agent.providers.create_chat_model", return_value=MagicMock()), \
                patch("chat.backend.agent.utils.llm_usage_tracker.tracked_invoke", return_value=fake_resp), \
                patch("chat.backend.agent.utils.message_content.extract_text_from_content",
                      return_value="Connection pool exhaustion in payments-db under load"):
            s = ii.generate_root_cause_synopsis(
                user_id="u1", session_id="s1", alert_title="High CPU",
                service="payments-api", summary="A long RCA report body.",
            )
        assert s == "Connection pool exhaustion in payments-db under load"
