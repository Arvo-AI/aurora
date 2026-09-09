"""Incident Index: synopsis derivation, line formatting, idempotent append,
and bounded read. DB layer (append_to_memory/read_memory) is stubbed."""

import json
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
        # read_memory returns "not found" → append proceeds.
        read_ret = json.dumps({"status": "not_found"})
        with patch.object(ii, "read_memory", return_value=read_ret) as rm, \
                patch.object(ii, "append_to_memory", return_value=json.dumps({"status": "ok"})) as am:
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
        existing = json.dumps({"status": "ok", "content": f"- [INC {INC} | d | api | resolved] x"})
        with patch.object(ii, "read_memory", return_value=existing) as rm, \
                patch.object(ii, "append_to_memory") as am:
            ok = ii.append_incident_line(
                user_id="u1", incident_id=INC, date_iso="d",
                service="api", status="resolved", alert_title="x", summary="s",
            )
        assert ok is True
        rm.assert_called_once()
        am.assert_not_called()

    def test_never_raises_on_backend_error(self):
        with patch.object(ii, "read_memory", side_effect=RuntimeError("db down")):
            ok = ii.append_incident_line(
                user_id="u1", incident_id=INC, date_iso="d",
                service="api", status="resolved", alert_title="x", summary="s",
            )
        assert ok is False  # degrades, does not raise


class TestReadIndex:
    def test_returns_content_on_ok(self):
        content = f"- [INC {INC} | 2026-09-08 | api | resolved] boom"
        with patch.object(ii, "read_memory", return_value=json.dumps({"status": "ok", "content": content})):
            assert ii.read_index("u1") == content

    def test_empty_when_missing(self):
        with patch.object(ii, "read_memory", return_value=json.dumps({"status": "not_found"})):
            assert ii.read_index("u1") == ""

    def test_empty_on_none(self):
        with patch.object(ii, "read_memory", return_value=None):
            assert ii.read_index("u1") == ""

    def test_trims_to_budget_and_drops_partial_leading_line(self):
        # Build content larger than the budget; each line is id-keyed.
        line = f"- [INC {uuid.uuid4()} | 2026-09-08 | api | resolved] some synopsis text here\n"
        big = line * (ii.INDEX_INJECTION_CHAR_BUDGET // len(line) + 50)
        with patch.object(ii, "read_memory", return_value=json.dumps({"status": "ok", "content": big})):
            out = ii.read_index("u1")
        assert len(out) <= ii.INDEX_INJECTION_CHAR_BUDGET
        # After trimming from the front, no dangling partial first line.
        assert out.startswith("- [INC ")

    def test_never_raises(self):
        with patch.object(ii, "read_memory", side_effect=RuntimeError("boom")):
            assert ii.read_index("u1") == ""
