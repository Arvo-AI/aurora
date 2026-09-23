"""pagerduty_notification_service + PagerDuty write-capability detection (no DB, no network)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.pagerduty.pagerduty_helpers import PD_READ_ONLY_ROLES, PagerDutyAPIError, PagerDutyClient, validate_token
from utils.notifications import pagerduty_notification_service as svc

ROOT_CAUSE = (
    "Root cause: the connection pool was exhausted after a deploy doubled the worker count "
    "without raising the Postgres max_connections limit."
)
SUMMARY = "What happened: the API tier fell over during the 14:00 deploy.\n\n" + ROOT_CAUSE

# The shape summarization.py produces: heading, metadata line, rule, 3 prose paragraphs, bullet sections.
REPORT = (
    "## Incident Report — Checkout API 5xx Spike\n\n"
    "**Incident ID:** 7f8d0e55 | **Severity:** High | **Source:** PagerDuty | **Triggered:** 2026-09-22T05:35:35Z\n\n"
    "---\n\n"
    "A high-severity PagerDuty alert titled \"checkout API 5xx spike\" fired at 05:35:35 UTC, "
    "attributed to the Default Service [33]. No application logs or traces were present [8, 11].\n\n"
    "The **most likely cause** is a stale client bundle sending requests to deprecated Server Action IDs "
    "after the 12:59 UTC deployment [7, 11], though direct confirmation was not obtained.\n\n"
    "No production impact was identified. The Fleet Server reported one healthy agent through 05:37:59 UTC [28].\n\n"
    "---\n\n"
    "## Ruled Out\n\n"
    "- **Real checkout API failure** — No checkout service is deployed anywhere [16, 17].\n"
    "- **Deployment-induced regression** — Only three seed commits exist [24].\n\n"
    "## Not Checked\n\n"
    "- **GitHub history** — authentication returned 401 [12].\n"
)
REPORT_ROOT_CAUSE = (
    "The most likely cause is a stale client bundle sending requests to deprecated Server Action IDs "
    "after the 12:59 UTC deployment, though direct confirmation was not obtained."
)
REPORT_IMPACT = "No production impact was identified. The Fleet Server reported one healthy agent through 05:37:59 UTC."

# The same report with the paragraph labels rendered as bold-only heading lines and no blank
# line before each paragraph (seen in the dev DB), and as markdown headings (a teammate's run).
HEADED_BOLD = (
    "## Incident Report — Checkout API 5xx Spike\n"
    "**2026-09-22 05:35:35 UTC | High | Default Service**\n\n---\n\n"
    "**What Happened**\n"
    "A high-severity PagerDuty alert titled \"checkout API 5xx spike\" fired at 05:35:35 UTC, "
    "attributed to the Default Service [33].\n\n"
    "**Root Cause**\n"
    "The **most likely cause** is a stale client bundle sending requests to deprecated Server Action IDs "
    "after the 12:59 UTC deployment [7, 11], though direct confirmation was not obtained.\n\n"
    "**Impact & Timeline**\n"
    "No production impact was identified. The Fleet Server reported one healthy agent through 05:37:59 UTC [28].\n\n"
    "---\n\n## Ruled Out\n\n- **Real checkout API failure** — nothing deployed [16].\n"
)
HEADED_MD = (
    "## Incident Report: Test Incident – Default Service\n\n"
    "### Summary\n"
    "A PagerDuty alert titled \"Test incident\" was triggered on 2026-09-22 at 20:44:11 UTC with medium severity, "
    "assigned to the Default Service [1].\n\n"
    "### Root Cause\n"
    "**Root cause confirmed: This is a test or validation alert with no production impact.** The alert references "
    "\"Default Service,\" which does not exist in the infrastructure inventory [7].\n\n"
    "### Impact & Timeline\n"
    "No operational impact. The alert was triggered at 20:44:11 UTC and remains in triggered status.\n\n"
    "## Ruled Out\n- **Real outage** — nothing deployed [7].\n"
)


def _anchor(**over):
    data = {
        "incident_id": "i1", "source_type": "pagerduty", "recurrence_of": None, "pagerduty_note_id": None,
        "alert_metadata": {"incidentId": "PABC123"}, "aurora_summary": SUMMARY,
    }
    data.update(over)
    return data


# --- _to_plain_text -----------------------------------------------------------

def test_plain_text_strips_markdown_citations_and_links():
    text = "**Root cause**: the *pod* was OOMKilled [1] after `memory` spiked [2, 3]. See [runbook](https://x.y/z)."
    assert svc._to_plain_text(text) == "Root cause: the pod was OOMKilled after memory spiked. See runbook (https://x.y/z)."


def test_plain_text_keeps_stars_glued_to_words():
    text = "p95*2 latency across 3*4 nodes; **a*b** is bold and *.log* is a glob"
    assert svc._to_plain_text(text) == "p95*2 latency across 3*4 nodes; a*b is bold and .log is a glob"


def test_plain_text_keeps_paragraph_breaks_and_drops_heading_marks():
    assert svc._to_plain_text("## Summary\n\nA b\nc.\n\n\n* item\n") == "Summary\n\nA b c.\n\n- item"


def test_plain_text_truncates_on_a_word_boundary():
    out = svc._to_plain_text("word " * 200)
    assert out.endswith("word...")
    assert len(out) <= svc.NOTE_MAX_CHARS + 3


def test_truncate_hard_cuts_when_no_early_space():
    out = svc._truncate("x" * 900)
    assert out == "x" * svc.NOTE_MAX_CHARS + "..."


# --- _should_post -------------------------------------------------------------

@pytest.mark.parametrize("over, reason", [
    ({"source_type": "datadog"}, "not a PagerDuty"),
    ({"recurrence_of": "root-1"}, "recurrence of root-1"),
    ({"pagerduty_note_id": "PWL7QXS"}, "already posted"),
    ({"pagerduty_note_id": "pending"}, "already posted"),
    ({"alert_metadata": {"incidentUrl": "https://x"}}, "no PagerDuty incident id"),
    ({"alert_metadata": None}, "no PagerDuty incident id"),
    ({"alert_metadata": {"incidentId": "../users/PX"}}, "no PagerDuty incident id"),
    ({"alert_metadata": {"incidentId": "PABC123/notes?x=1"}}, "no PagerDuty incident id"),
    ({"alert_metadata": {"incidentId": "P" * 33}}, "no PagerDuty incident id"),
    ({"aurora_summary": "Short summary."}, "too short"),
    ({"aurora_summary": None}, "too short"),
])
def test_should_post_rejects(over, reason):
    ok, why = svc._should_post(_anchor(**over))
    assert ok is False
    assert reason in why


def test_should_post_accepts_a_well_formed_anchor():
    assert svc._should_post(_anchor()) == (True, "")


def test_should_post_decodes_string_alert_metadata():
    assert svc._should_post(_anchor(alert_metadata='{"incidentId": "PABC123"}'))[0] is True


def test_root_cause_is_the_second_paragraph():
    assert svc._pick_root_cause_paragraph(SUMMARY) == ROOT_CAUSE
    assert svc._pick_root_cause_paragraph(ROOT_CAUSE) == ROOT_CAUSE


def test_unheaded_report_yields_root_cause_and_the_paragraph_after_it_as_impact():
    assert svc._extract_note_body(REPORT) == (REPORT_ROOT_CAUSE, REPORT_IMPACT)
    assert svc._pick_root_cause_paragraph(REPORT) == REPORT_ROOT_CAUSE


def test_bold_headed_report_is_read_by_section_title():
    assert svc._extract_note_body(HEADED_BOLD) == (REPORT_ROOT_CAUSE, REPORT_IMPACT)


def test_markdown_headed_report_is_read_by_section_title():
    root, impact = svc._extract_note_body(HEADED_MD)
    assert root.startswith("Root cause confirmed: This is a test or validation alert with no production impact. The alert references")
    assert root.endswith("does not exist in the infrastructure inventory.")
    assert impact == "No operational impact. The alert was triggered at 20:44:11 UTC and remains in triggered status."


def test_two_paragraph_summary_has_no_impact():
    assert svc._extract_note_body(SUMMARY) == (ROOT_CAUSE, "")


def test_bold_metadata_line_is_not_a_heading():
    assert svc._heading_text("**2026-09-08 13:49:02 UTC | Critical | payments-api**") is None
    assert svc._heading_text("**Root cause confirmed: nothing was deployed.**") is None
    assert svc._heading_text("**Impact & Timeline**") == "impact & timeline"
    assert svc._heading_text("### Root Cause") == "root cause"
    assert svc._heading_text("Summary") == "summary"


def test_root_cause_undetermined_paragraph_is_preferred_wherever_it_sits():
    text = (
        "What happened first, described at some length so the paragraph is prose.\n\n"
        "Impact paragraph, also long enough to count as narrative prose here.\n\n"
        "Root cause undetermined. No checkout API service exists in any observable infrastructure.\n\n"
        "**Next Steps**\n\n- do something"
    )
    assert svc._pick_root_cause_paragraph(text).startswith("Root cause undetermined.")


def test_bullet_sections_never_leak_when_no_prose_precedes_them():
    assert svc._extract_note_body("## Ruled Out\n\n- **Hypothesis** — killed by evidence [1].\n") == ("", "")


# --- _compose_note ------------------------------------------------------------

def test_note_has_root_cause_impact_link_and_disclaimer(monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "https://aurora.example.com/")
    note = svc._compose_note("Because X.", "i1", "Nobody noticed.")
    assert note == (
        "Aurora RCA\n\nRoot cause\nBecause X.\n\nImpact\nNobody noticed.\n\n"
        "Full investigation: https://aurora.example.com/incidents/i1\n\n"
        "Generated automatically by Aurora. Verify before acting."
    )


def test_note_omits_impact_block_when_none_was_found(monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "https://aurora.example.com")
    note = svc._compose_note("Because X.", "i1")
    assert "Impact" not in note
    assert note.startswith("Aurora RCA\n\nRoot cause\nBecause X.\n\nFull investigation:")


def test_note_omits_link_when_frontend_url_unset(monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "")
    note = svc._compose_note("Because X.", "i1")
    assert "Full investigation" not in note
    assert "/incidents/" not in note
    assert note.endswith("Generated automatically by Aurora. Verify before acting.")


# --- validate_token -----------------------------------------------------------

class _StubClient:
    def __init__(self, role=None, *, is_oauth=False, account=False):
        self.is_oauth = is_oauth
        self._role = role
        self._account = account

    def get_current_user(self):
        if self._account:
            raise PagerDutyAPIError("Access Denied: this endpoint requires the user's identity; account-level tokens cannot")
        user = {"email": "bot@acme.com", "name": "Aurora Bot", "html_url": "https://acme.pagerduty.com/users/P1"}
        if self._role:
            user["role"] = self._role
        return {"user": user}

    def get_subdomain(self):
        return "acme"


@pytest.mark.parametrize("role", sorted(PD_READ_ONLY_ROLES))
def test_read_only_roles_cannot_write(role):
    info = validate_token(_StubClient(role))
    assert info["capabilities"]["can_write_incidents"] is False
    assert info["capabilities"]["api_key_access"] == "user"
    assert info["external_user_role"] == role


@pytest.mark.parametrize("role", ["user", "limited_user", "admin", "owner"])
def test_write_roles_can_write_with_a_user_key(role):
    info = validate_token(_StubClient(role))
    assert info["capabilities"] == {"can_read_incidents": True, "can_write_incidents": True, "api_key_access": "user"}
    assert info["external_user_email"] == "bot@acme.com"
    assert info["account_subdomain"] == "acme"


def test_missing_role_denies_write():
    info = validate_token(_StubClient(None))
    assert info["capabilities"]["can_write_incidents"] is False
    assert "external_user_role" not in info


def test_account_level_key_is_reported_and_denied():
    info = validate_token(_StubClient(account=True))
    assert info["capabilities"] == {"can_read_incidents": True, "can_write_incidents": False, "api_key_access": "account"}
    assert info["account_subdomain"] == "acme"
    assert "external_user_email" not in info


def test_other_errors_still_raise():
    client = _StubClient("user")
    client.get_current_user = MagicMock(side_effect=PagerDutyAPIError("Unauthorized: Invalid or expired API token"))
    with pytest.raises(PagerDutyAPIError):
        validate_token(client)


@pytest.mark.parametrize("scopes", [None, "", "openid users.read incidents.read services.read"])
def test_oauth_without_incidents_write_scope_denies(scopes):
    info = validate_token(_StubClient("admin", is_oauth=True), granted_scopes=scopes)
    assert info["capabilities"]["can_write_incidents"] is False
    assert info["capabilities"]["api_key_access"] == "oauth"


def test_oauth_with_scope_and_write_role_allows():
    scopes = "openid users.read incidents.read incidents.write services.read"
    assert validate_token(_StubClient("user", is_oauth=True), granted_scopes=scopes)["capabilities"]["can_write_incidents"] is True
    assert validate_token(_StubClient("observer", is_oauth=True), granted_scopes=scopes)["capabilities"]["can_write_incidents"] is False


# --- PagerDutyClient ----------------------------------------------------------

def test_from_header_only_when_email_known():
    assert "From" not in PagerDutyClient(api_token="t").headers
    assert "From" not in PagerDutyClient(api_token="t", from_email="").headers
    headers = PagerDutyClient(oauth_token="t", from_email="bot@acme.com").headers
    assert headers["From"] == "bot@acme.com"
    assert headers["Authorization"] == "Bearer t"


def test_create_note_posts_the_note_body(monkeypatch):
    client = PagerDutyClient(api_token="t", from_email="bot@acme.com")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        response = MagicMock()
        response.json.return_value = {"note": {"id": "PWL7QXS"}}
        return response

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.create_note("PABC123", "hello") == {"note": {"id": "PWL7QXS"}}
    assert calls == [("POST", "/incidents/PABC123/notes", {"json": {"note": {"content": "hello"}}})]


# --- send_pagerduty_incident_note --------------------------------------------

CLAIM = ("UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s AND pagerduty_note_id IS NULL", ("pending", "i1"))
RELEASE = ("UPDATE incidents SET pagerduty_note_id = NULL WHERE id = %s AND pagerduty_note_id = %s", ("i1", "pending"))
RECORD = ("UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s", ("PWL7QXS", "i1"))


@pytest.fixture
def wired(monkeypatch, patched_db):
    creds = {
        "auth_type": "api_token", "api_token": "tok", "external_user_email": "bot@acme.com",
        "capabilities": {"can_read_incidents": True, "can_write_incidents": True, "api_key_access": "user"},
    }
    client = MagicMock(name="client")
    client.create_note.return_value = {"note": {"id": "PWL7QXS"}}
    built = []

    def fake_client(**kwargs):
        built.append(kwargs)
        return client

    monkeypatch.setattr(svc, "PagerDutyClient", fake_client)
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: dict(creds))
    stored = MagicMock(name="store_tokens_in_db")
    monkeypatch.setattr(svc, "store_tokens_in_db", stored)
    return SimpleNamespace(pool=patched_db, client=client, built=built, stored=stored, creds=creds)


def test_happy_path_claims_posts_then_records(wired, monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "http://localhost:3000")
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is True
    assert wired.pool.updates == [CLAIM, RECORD]
    wired.client.create_note.assert_called_once()
    pd_incident_id, content = wired.client.create_note.call_args.args
    assert pd_incident_id == "PABC123"
    assert content.startswith("Aurora RCA\n\nRoot cause\n" + ROOT_CAUSE + "\n\n")
    assert "Full investigation: http://localhost:3000/incidents/i1" in content
    assert content.endswith("Generated automatically by Aurora. Verify before acting.")
    assert "**" not in content
    assert wired.built == [{"from_email": "bot@acme.com", "api_token": "tok"}]
    wired.stored.assert_not_called()


def test_lost_claim_posts_nothing(wired):
    wired.pool.cursor.rowcount = 0
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]
    wired.client.create_note.assert_not_called()


def test_rejected_post_releases_the_claim(wired):
    wired.client.create_note.side_effect = PagerDutyAPIError("Rate limited", status_code=429)
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM, RELEASE]
    wired.stored.assert_not_called()


def test_no_response_keeps_the_claim(wired):
    # Timeout / connection reset: PagerDuty may have stored the note, so never retry
    wired.client.create_note.side_effect = PagerDutyAPIError("read timeout")
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]
    wired.stored.assert_not_called()


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_server_error_keeps_the_claim(wired, status):
    wired.client.create_note.side_effect = PagerDutyAPIError(f"HTTP {status}", status)
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]
    wired.stored.assert_not_called()


def test_unexpected_error_keeps_the_claim_and_does_not_raise(wired):
    wired.client.create_note.side_effect = ValueError("boom")
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM]


def test_unresolvable_org_never_posts(wired, monkeypatch):
    monkeypatch.setattr(svc, "set_rls_context", lambda *a, **k: None)
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    wired.client.create_note.assert_not_called()


def test_forbidden_flips_write_capability_off(wired):
    wired.client.create_note.side_effect = PagerDutyAPIError("Forbidden: Token lacks required permissions", status_code=403)
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == [CLAIM, RELEASE]
    wired.stored.assert_called_once()
    user_id, payload, provider = wired.stored.call_args.args
    assert (user_id, provider) == ("u1", "pagerduty")
    assert payload["capabilities"]["can_write_incidents"] is False
    assert payload["capabilities"]["can_read_incidents"] is True
    assert payload["api_token"] == "tok"  # the rest of the Vault payload survives the overwrite


def test_ineligible_incident_never_touches_db_or_pagerduty(wired):
    assert svc.send_pagerduty_incident_note("u1", _anchor(recurrence_of="root-1")) is False
    assert wired.pool.updates == []
    wired.client.create_note.assert_not_called()


def test_read_only_credentials_post_nothing(wired, monkeypatch):
    creds = {**wired.creds, "capabilities": {"can_read_incidents": True, "can_write_incidents": False}}
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: creds)
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is False
    assert wired.pool.updates == []
    wired.client.create_note.assert_not_called()


def test_empty_frontend_url_omits_the_link_line(wired, monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "")
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is True
    content = wired.client.create_note.call_args.args[1]
    assert "Full investigation" not in content
    assert "/incidents/" not in content
