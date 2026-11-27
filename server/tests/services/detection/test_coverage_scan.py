"""The Monitoring Coverage Scan, and the check an RCA leaves behind.

The scan writes checks for assets nothing watches; only its own session may
make such a check open cases from the start. Every RCA on an incident now ends
with the instruction to save one check, instead of waiting for someone to
click Resolve.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chat.backend.agent.prompt import background
from chat.backend.agent.tools import detection_check_tool as tool
from services.actions import coverage_scan_action as scan
from services.actions import system_actions
from services.detection import store


def _in_session(monkeypatch, state):
    import utils.cloud.cloud_utils as cloud_utils

    monkeypatch.setattr(cloud_utils, "get_state_context", lambda: state)


def _args(unmonitored=True):
    return SimpleNamespace(unmonitored=unmonitored)


_SCAN = SimpleNamespace(is_background=True, trigger_source="action", is_coverage_scan_action=True)


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
    SimpleNamespace(is_background=True, trigger_source="action"),    # another action
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


# --- passing now before opening cases --------------------------------------

def _save_capturing(monkeypatch, healthy):
    """save_detection_check with the store and gate stubbed; returns what _store_check got."""
    seen = {}
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    monkeypatch.setattr(tool, "_may_write", lambda _user: True)
    monkeypatch.setattr(tool, "gate_command", lambda **_kw: SimpleNamespace(allowed=True))
    monkeypatch.setattr(tool, "_try_replay", lambda *_a: (None, ""))
    monkeypatch.setattr(tool, "_starts_active", lambda *_a: True)
    monkeypatch.setattr(tool, "_healthy_now", lambda *_a: healthy)

    def _store(user_id, args, definition, origin, replayed, activate=False):
        seen["activate"] = activate
        return '{"status": "saved", "state": "silent"}'

    monkeypatch.setattr(tool, "_store_check", _store)
    answer = tool.save_detection_check(
        user_id="u1", name="db-up", prediction="db down", asset="orders-db", pattern="replicas",
        provider="onprem_kubectl", target="c1", command="get statefulset orders-db",
        expect="text", op="not_empty", origin_kind="graph", unmonitored=True,
    )
    return seen, answer


def test_a_check_that_passes_now_starts_active(monkeypatch):
    seen, _ = _save_capturing(monkeypatch, healthy=True)
    assert seen["activate"] is True


def test_a_check_that_does_not_pass_now_is_saved_silent(monkeypatch):
    seen, _ = _save_capturing(monkeypatch, healthy=False)
    assert seen["activate"] is False


def test_the_agent_is_told_why_it_was_saved_silent(monkeypatch):
    import json

    _, answer = _save_capturing(monkeypatch, healthy=False)
    assert "did not pass" in json.loads(answer)["active_note"]


# --- the action ------------------------------------------------------------

def _registered():
    return next(a for a in system_actions.SYSTEM_ACTIONS if a["system_key"] == scan.SYSTEM_KEY)


def test_the_scan_is_its_own_system_action():
    assert _registered()["name"] == "Monitoring Coverage Scan"


def test_the_scan_runs_weekly():
    assert _registered()["trigger_config"] == {"interval_seconds": 604800}


def test_the_scan_runs_in_ask_mode():
    assert _registered()["mode"] == "ask"


def test_the_scan_has_default_instructions():
    assert system_actions._get_default_instructions(scan.SYSTEM_KEY) == scan.DEFAULT_COVERAGE_SCAN_INSTRUCTIONS


def test_the_scan_is_told_not_to_duplicate_working_alerts():
    assert "Do NOT save a check for something an existing working alert" in (
        scan.DEFAULT_COVERAGE_SCAN_INSTRUCTIONS
    )


def test_the_scan_caps_new_checks_per_run():
    assert "at most %d new checks" % scan.MAX_NEW_CHECKS_PER_RUN in scan.DEFAULT_COVERAGE_SCAN_INSTRUCTIONS


# --- started after prediscovery --------------------------------------------

def test_a_routine_prediscovery_leaves_the_scan_to_its_schedule(monkeypatch):
    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    assert not scan.start_after_prediscovery("u1", "scheduled")


def test_no_scan_with_the_feature_off(monkeypatch):
    monkeypatch.setenv("DETECTION_CHECKS_ENABLED", "false")
    assert not scan.start_after_prediscovery("u1", "new_connector")


def _dispatch_setup(monkeypatch, owner_can_save):
    """start_after_prediscovery with the database and seeding stubbed; returns the dispatch mock."""
    from contextlib import contextmanager

    import services.actions.executor as executor
    import utils.auth.stateless_auth as auth
    import utils.db.connection_pool as pool

    monkeypatch.delenv("DETECTION_CHECKS_ENABLED", raising=False)
    monkeypatch.setattr(auth, "get_org_id_for_user", lambda _user: "org-1")
    monkeypatch.setattr(auth, "set_rls_context", lambda *_a, **_kw: "org-1")
    monkeypatch.setattr(system_actions, "seed_system_actions", lambda *_a: 0)
    monkeypatch.setattr(scan, "owner_to_run_as",
                        lambda _action, _org, owner: owner if owner_can_save else None)
    cursor = MagicMock()
    cursor.fetchone.return_value = ("act-1", "owner-1")
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    @contextmanager
    def _admin():
        yield conn

    monkeypatch.setattr(pool.db_pool, "get_admin_connection", _admin)
    dispatch = MagicMock()
    monkeypatch.setattr(executor, "dispatch_action", dispatch)
    return dispatch


def test_a_new_connection_dispatches_the_orgs_scan(monkeypatch):
    dispatch = _dispatch_setup(monkeypatch, owner_can_save=True)
    assert scan.start_after_prediscovery("u1", "new_connector")
    dispatch.assert_called_once_with(
        action_id="act-1", user_id="owner-1",
        trigger_context={"source": "prediscovery", "trigger": "new_connector"},
    )


def test_no_scan_when_no_one_can_save_checks(monkeypatch):
    dispatch = _dispatch_setup(monkeypatch, owner_can_save=False)
    assert not scan.start_after_prediscovery("u1", "new_connector")
    dispatch.assert_not_called()


def _owner_setup(monkeypatch, can_save, admin_row):
    from contextlib import contextmanager

    import utils.db.connection_pool as pool

    monkeypatch.setattr(scan, "_owner_can_save_checks", lambda user, _org: user in can_save)
    cursor = MagicMock()
    cursor.fetchone.return_value = admin_row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    @contextmanager
    def _admin():
        yield conn

    monkeypatch.setattr(pool.db_pool, "get_admin_connection", _admin)
    return cursor


def test_an_owner_who_can_save_checks_runs_it(monkeypatch):
    _owner_setup(monkeypatch, {"owner-1"}, None)
    assert scan.owner_to_run_as("act-1", "org-1", "owner-1") == "owner-1"


def test_a_viewer_owned_scan_is_handed_to_an_admin(monkeypatch):
    cursor = _owner_setup(monkeypatch, {"admin-1"}, ("admin-1",))
    assert scan.owner_to_run_as("act-1", "org-1", "viewer-1") == "admin-1"
    assert cursor.execute.call_args[0][1] == ("admin-1", "act-1", "org-1")


def test_no_admin_means_no_scan(monkeypatch):
    _owner_setup(monkeypatch, set(), None)
    assert scan.owner_to_run_as("act-1", "org-1", "viewer-1") is None
