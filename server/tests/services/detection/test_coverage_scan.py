"""The Monitoring Coverage Scan, and the check an RCA leaves behind.

The scan writes checks for assets nothing watches; only its own session may
make such a check open cases from the start. Every RCA on an incident now ends
with the instruction to save one check, instead of waiting for someone to
click Resolve.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.backend.agent.prompt import background
from chat.backend.agent.tools import detection_check_tool as tool
from chat.background import coverage_scan_task as scan
from services.detection import COVERAGE_SCAN_SOURCE, store


def _in_session(monkeypatch, state):
    import utils.cloud.cloud_utils as cloud_utils

    monkeypatch.setattr(cloud_utils, "get_state_context", lambda: state)


def _args(unmonitored=True):
    return SimpleNamespace(unmonitored=unmonitored)


_SCAN = SimpleNamespace(is_background=True, trigger_source=COVERAGE_SCAN_SOURCE)


# --- starting active -------------------------------------------------------

@pytest.mark.parametrize("kind", ["graph", "explore"])
def test_the_scan_can_start_a_check_on_an_unwatched_asset_active(monkeypatch, kind):
    _in_session(monkeypatch, _SCAN)
    assert tool._starts_active(_args(), {"kind": kind, "ref": "no monitor"})


def test_a_check_is_not_active_unless_the_scan_says_nothing_watches_it(monkeypatch):
    _in_session(monkeypatch, _SCAN)
    assert not tool._starts_active(_args(unmonitored=False), {"kind": "graph", "ref": ""})


@pytest.mark.parametrize("kind", ["incident", "miss", "gap", "person"])
def test_other_origins_cannot_claim_unmonitored(monkeypatch, kind):
    _in_session(monkeypatch, _SCAN)
    assert not tool._starts_active(_args(), {"kind": kind, "ref": ""})


@pytest.mark.parametrize("state", [
    SimpleNamespace(is_background=True, trigger_source="grafana"),   # an RCA
    SimpleNamespace(is_background=True, trigger_source="action"),    # a scheduled action
    SimpleNamespace(is_background=False),                            # a chat
    None,
])
def test_only_the_scan_session_can_start_a_check_active(monkeypatch, state):
    _in_session(monkeypatch, state)
    assert not tool._starts_active(_args(), {"kind": "graph", "ref": ""})


def test_starting_active_moves_only_a_silent_check_and_names_no_voucher():
    cursor = MagicMock()
    cursor.rowcount = 1
    assert store.start_active(cursor, "org-1", "chk-1")
    sql, params = cursor.execute.call_args[0]
    assert "vouched_by" not in sql
    assert params == (store.STATE_PROVEN, "chk-1", "org-1", store.STATE_SILENT)


def test_the_unmonitored_note_explains_why_it_opens_cases():
    import json

    response = json.loads(tool._saved_response(("chk-1", 1, True), "proven", False, None, active=True))
    assert response["note"] == tool._UNMONITORED_NOTE


# --- the check an RCA leaves behind ----------------------------------------

def test_every_rca_on_an_incident_ends_with_the_check_step(monkeypatch):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    step = background._after_rca_check_step("inc-42")
    assert 'origin_ref "INC inc-42"' in step


def test_the_check_step_says_what_to_do_when_no_check_is_clear(monkeypatch):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    assert "No clear check for this incident." in background._after_rca_check_step("inc-42")


def test_no_check_step_without_an_incident(monkeypatch):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    assert background._after_rca_check_step(None) == ""


def test_no_check_step_with_the_feature_off(monkeypatch):
    monkeypatch.setenv("DETECTION_CHECKS_ENABLED", "false")
    assert background._after_rca_check_step("inc-42") == ""


def test_the_postmortem_no_longer_asks_for_the_check(monkeypatch):
    from collections import defaultdict
    from services.actions import postmortem_action

    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    incident = defaultdict(str, incident_id="inc-42", title="t", summary="s")
    assert "save_detection_check" not in postmortem_action._build_action_prompt("Write it.", incident)


# --- when the scan runs ----------------------------------------------------

@pytest.mark.parametrize("trigger", ["new_connector", "manual"])
def test_a_new_connection_or_a_manual_run_always_scans(trigger):
    assert scan.scan_is_due("u1", trigger)


def test_a_scheduled_run_scans_when_there_was_no_scan(monkeypatch):
    monkeypatch.setattr(scan, "_last_scan_at", lambda _user: None)
    assert scan.scan_is_due("u1", "scheduled")


def test_a_scheduled_run_skips_a_scan_from_this_week(monkeypatch):
    monkeypatch.setattr(scan, "_last_scan_at", lambda _user: datetime.now() - timedelta(days=2))
    assert not scan.scan_is_due("u1", "scheduled")


def test_a_scheduled_run_rescans_after_a_week(monkeypatch):
    monkeypatch.setattr(scan, "_last_scan_at", lambda _user: datetime.now() - timedelta(days=8))
    assert scan.scan_is_due("u1", "scheduled")


def test_an_unreadable_last_scan_skips_rather_than_piling_up_runs(monkeypatch):
    def _broken(_user):
        raise RuntimeError("db down")

    monkeypatch.setattr(scan, "_last_scan_at", _broken)
    assert not scan.scan_is_due("u1", "scheduled")


def test_prediscovery_starts_a_due_scan(monkeypatch):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    delay = MagicMock()
    monkeypatch.setattr(scan.run_coverage_scan, "delay", delay)
    assert scan.schedule_after_prediscovery("u1", "new_connector")
    delay.assert_called_once_with(user_id="u1", trigger="new_connector")


def test_no_scan_with_the_feature_off(monkeypatch):
    monkeypatch.setenv("DETECTION_CHECKS_ENABLED", "false")
    delay = MagicMock()
    monkeypatch.setattr(scan.run_coverage_scan, "delay", delay)
    assert not scan.schedule_after_prediscovery("u1", "new_connector")
    delay.assert_not_called()


# --- what the scan is told -------------------------------------------------

def test_the_scan_is_told_not_to_duplicate_working_alerts():
    assert "Do NOT save a check for something an existing working alert" in (
        scan.build_coverage_scan_prompt(["aws"], {"datadog": True})
    )


def test_the_scan_caps_new_checks_per_run():
    assert "at most %d new checks" % scan.MAX_NEW_CHECKS_PER_RUN in (
        scan.build_coverage_scan_prompt([], {})
    )
