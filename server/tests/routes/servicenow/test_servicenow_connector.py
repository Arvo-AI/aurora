"""ServiceNow connector: instance URL validation, webhook secret check, payload
mapping and the RCA priority gate (no DB, no network).

``routes.servicenow.tasks`` imports the Celery app and the LangGraph-backed
RCA prompt builder at module load; both are stubbed here the same way the
incident.io tests do, so the pure helpers can be tested hermetically.
"""

import sys
from datetime import timezone
from types import ModuleType

import pytest


def _install_import_stubs() -> None:
    rca_pb = ModuleType("chat.background.rca_prompt_builder")
    rca_pb.build_rca_prompt = lambda *a, **k: ("prompt", "rail")  # type: ignore[attr-defined]
    sys.modules.setdefault("chat.background.rca_prompt_builder", rca_pb)

    celery_cfg = ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                return fn
            if dargs and callable(dargs[0]) and not dkwargs:
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()  # type: ignore[attr-defined]
    sys.modules.setdefault("celery_config", celery_cfg)


_install_import_stubs()

from routes.servicenow import servicenow_client as client  # noqa: E402
from routes.servicenow import tasks  # noqa: E402
from routes.servicenow import webhook_auth  # noqa: E402
from chat.backend.agent.tools import servicenow_tool as tool  # noqa: E402


# ── instance URL ─────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("https://acme.service-now.com", "https://acme.service-now.com"),
    ("https://ACME.service-now.com/", "https://acme.service-now.com"),
    ("acme.service-now.com", "https://acme.service-now.com"),
    ("https://acme.servicenowservices.com", "https://acme.servicenowservices.com"),
    ("https://acme.service-now.com:443", "https://acme.service-now.com"),
])
def test_normalize_instance_url_accepts_servicenow_hosts(raw, expected):
    assert client.normalize_instance_url(raw) == expected


@pytest.mark.parametrize("raw", [
    "",
    "http://acme.service-now.com",
    "https://service-now.com",
    "https://acme.service-now.com.evil.example",
    "https://evil.example/?x=acme.service-now.com",
    "https://acme.service-now.com/api/now/table/incident",
    "https://acme.service-now.com?sysparm_query=1",
    "https://user:pw@acme.service-now.com",
    "https://acme.service-now.com:8443",
    "https://169.254.169.254",
    "https://localhost",
])
def test_normalize_instance_url_rejects_everything_else(raw):
    with pytest.raises(ValueError):
        client.normalize_instance_url(raw)


def test_custom_domain_suffix_via_env(monkeypatch):
    monkeypatch.setenv("SERVICENOW_ALLOWED_HOSTS", "itsm.example.com")
    assert client.normalize_instance_url("https://prod.itsm.example.com") == "https://prod.itsm.example.com"
    with pytest.raises(ValueError):
        client.normalize_instance_url("https://itsm.example.com.evil.example")


def test_incident_url_is_a_classic_nav_link():
    sys_id = "a" * 32
    assert client.incident_url("https://acme.service-now.com", sys_id) == (
        f"https://acme.service-now.com/nav_to.do?uri=incident.do?sys_id={sys_id}"
    )


def test_sys_id_and_number_shapes():
    assert client.is_sys_id("0123456789abcdef0123456789ABCDEF")
    assert not client.is_sys_id("0123456789abcdef0123456789abcde")
    assert not client.is_sys_id("../incident")
    assert client.is_incident_number("INC0010001")
    assert not client.is_incident_number("INC")
    assert not client.is_incident_number("INC0010001^ORDERBY")


def test_client_from_credentials_refuses_a_bad_stored_url():
    with pytest.raises(ValueError):
        client.ServiceNowClient.from_credentials({"instance_url": "https://evil.example"})


# ── webhook secret ───────────────────────────────────────────────────

def test_secret_matches_header_or_bearer_only():
    secret = webhook_auth.new_webhook_secret()
    assert len(secret) >= 32
    assert webhook_auth.secret_matches(secret, secret, None)
    assert webhook_auth.secret_matches(secret, f"  {secret} ", None)
    assert webhook_auth.secret_matches(secret, None, f"Bearer {secret}")
    assert webhook_auth.secret_matches(secret, None, f"bearer {secret}")
    assert not webhook_auth.secret_matches(secret, None, None)
    assert not webhook_auth.secret_matches(secret, "", "")
    assert not webhook_auth.secret_matches(secret, secret[:-1], None)
    assert not webhook_auth.secret_matches(secret, None, secret)  # bare value in Authorization
    assert not webhook_auth.secret_matches(None, secret, None)
    assert not webhook_auth.secret_matches("", "", None)


# ── payload mapping ──────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("1", 1), ("2 - High", 2), (3, 3), ("5 - Planning", 5), ("", None), (None, None),
    ("critical", None), (0, None), (6, None), (True, None),
])
def test_priority_number(value, expected):
    assert tasks.priority_number(value) == expected


@pytest.mark.parametrize("payload, severity", [
    ({"priority": "1"}, "critical"),
    ({"priority": "2", "priority_label": "2 - High"}, "high"),
    ({"priority_label": "3 - Moderate"}, "medium"),
    ({"priority": "4"}, "low"),
    ({"priority": "5"}, "low"),
    ({}, "medium"),
])
def test_extract_severity(payload, severity):
    assert tasks.extract_severity(payload) == severity


@pytest.mark.parametrize("payload, resolved", [
    ({"state": "6"}, True), ({"state": "7"}, True), ({"state": "8"}, True),
    ({"state": "2", "state_label": "In Progress"}, False),
    ({"state_label": "Resolved"}, True), ({"state_label": "Closed"}, True),
    ({}, False),
])
def test_is_resolved(payload, resolved):
    assert tasks.is_resolved(payload) is resolved




def test_alert_title_and_service():
    payload = {"number": "INC0010001", "short_description": "DB down", "assignment_group": "DBA"}
    assert tasks.alert_title(payload) == "[INC0010001] DB down"
    assert tasks.alert_service(payload) == "DBA"
    assert tasks.alert_service({"cmdb_ci": "pg-prod-1", "assignment_group": "DBA"}) == "pg-prod-1"
    assert tasks.alert_service({}) == "servicenow"
    assert tasks.alert_title({}) == "ServiceNow incident"


def test_build_alert_metadata_keeps_sys_id_and_deep_link_and_drops_empties():
    sys_id = "b" * 32
    payload = {
        "sys_id": sys_id, "number": "INC0010001", "priority": "1", "priority_label": "1 - Critical",
        "state": "2", "state_label": "In Progress", "assignment_group": "", "description": "x" * 3000,
        "event": "Inserted",
    }
    meta = tasks.build_alert_metadata(payload, "https://acme.service-now.com")
    assert meta["sysId"] == sys_id
    assert meta["priority"] == "1 - Critical"
    assert meta["state"] == "In Progress"
    assert meta["event"] == "inserted"
    assert meta["incidentUrl"].endswith(f"sys_id={sys_id}")
    assert len(meta["description"]) == 2000
    assert "assignmentGroup" not in meta
    assert "incidentUrl" not in tasks.build_alert_metadata(payload, None)


def test_parse_servicenow_datetime():
    parsed = tasks.parse_servicenow_datetime("2026-10-08 21:15:03")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert parsed.hour == 21
    assert tasks.parse_servicenow_datetime("2026-10-08T21:15:03Z").minute == 15
    assert tasks.parse_servicenow_datetime("not a date") is None
    assert tasks.parse_servicenow_datetime("") is None


def test_source_alert_id_is_stable_and_scoped_to_the_org():
    sys_id = "c" * 32
    first = tasks.source_alert_id_for("org-1", sys_id)
    assert first == tasks.source_alert_id_for("org-1", sys_id)
    assert first != tasks.source_alert_id_for("org-2", sys_id)
    assert 0 <= first < 2**31


# ── agent tool query building ────────────────────────────────────────

def test_build_query_maps_state_and_priority_and_orders_newest_first():
    q = tool.build_query(state="active", priority=1, assignment_group="DBA", query="disk full")
    assert q == "active=true^priority=1^assignment_group.nameLIKEDBA^short_descriptionLIKEdisk full^ORdescriptionLIKEdisk full^ORDERBYDESCopened_at"
    assert tool.build_query(state="in progress") == "state=2^ORDERBYDESCopened_at"
    assert tool.build_query(state="7") == "state=7^ORDERBYDESCopened_at"
    assert tool.build_query(state="bogus", priority=9) == "ORDERBYDESCopened_at"


def test_build_query_strips_query_separators_from_free_text():
    q = tool.build_query(query="x^active=false", assignment_group="a^b\nc")
    # The separator is neutralised, so the injected text stays inside the LIKE
    # value instead of becoming its own clause.
    assert "^active=false" not in q
    assert q.split("^") == [
        "assignment_group.nameLIKEa b c",
        "short_descriptionLIKEx active=false",
        "ORdescriptionLIKEx active=false",
        "ORDERBYDESCopened_at",
    ]
