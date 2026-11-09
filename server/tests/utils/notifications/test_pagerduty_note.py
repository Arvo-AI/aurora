"""pagerduty_notification_service + PagerDuty write-capability detection (no DB, no network)."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.pagerduty.pagerduty_helpers import PD_READ_ONLY_ROLES, PagerDutyAPIError, PagerDutyClient, validate_token
from utils.notifications import pagerduty_notification_service as svc
from utils.notifications import postback_claim, rca_note

ROOT_CAUSE = (
    "Root cause: the connection pool was exhausted after a deploy doubled the worker count "
    "without raising the Postgres max_connections limit."
)
SUMMARY = "What happened: the API tier fell over during the 14:00 deploy.\n\n" + ROOT_CAUSE
# What the note shows: the summarizer's inline "Root cause:" label is dropped
ROOT_CAUSE_BODY = ROOT_CAUSE[len("Root cause: "):]

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
    assert rca_note.to_plain_text(text) == "Root cause: the pod was OOMKilled after memory spiked. See runbook (https://x.y/z)."


def test_plain_text_keeps_stars_glued_to_words():
    text = "p95*2 latency across 3*4 nodes; **a*b** is bold and *.log* is a glob"
    assert rca_note.to_plain_text(text) == "p95*2 latency across 3*4 nodes; a*b is bold and .log is a glob"


def test_plain_text_keeps_paragraph_breaks_and_drops_heading_marks():
    assert rca_note.to_plain_text("## Summary\n\nA b\nc.\n\n\n* item\n") == "Summary\n\nA b c.\n\n- item"


def test_plain_text_truncates_on_a_word_boundary():
    out = rca_note.to_plain_text("word " * 600)
    assert out.endswith("word...")
    assert len(out) <= rca_note.NOTE_MAX_CHARS + 3


def test_truncate_hard_cuts_when_no_early_space():
    out = rca_note.truncate("x" * (rca_note.NOTE_MAX_CHARS + 200))
    assert out == "x" * rca_note.NOTE_MAX_CHARS + "..."


def test_truncate_keeps_word_boundary_for_short_limits():
    out = rca_note.truncate("word " * 60, rca_note.NEXT_STEP_MAX_CHARS)
    assert out.endswith("word...")


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
    assert rca_note.extract_note_body(SUMMARY)[0] == ROOT_CAUSE_BODY
    assert rca_note.extract_note_body(ROOT_CAUSE)[0] == ROOT_CAUSE_BODY


def test_unheaded_report_yields_root_cause_and_the_paragraph_after_it_as_impact():
    assert rca_note.extract_note_body(REPORT) == (REPORT_ROOT_CAUSE, REPORT_IMPACT)
    assert rca_note.extract_note_body(REPORT)[0] == REPORT_ROOT_CAUSE


def test_bold_headed_report_is_read_by_section_title():
    assert rca_note.extract_note_body(HEADED_BOLD) == (REPORT_ROOT_CAUSE, REPORT_IMPACT)


def test_markdown_headed_report_is_read_by_section_title():
    root, impact = rca_note.extract_note_body(HEADED_MD)
    assert root.startswith("Root cause confirmed: This is a test or validation alert with no production impact. The alert references")
    assert root.endswith("does not exist in the infrastructure inventory.")
    assert impact == "No operational impact. The alert was triggered at 20:44:11 UTC and remains in triggered status."


def test_two_paragraph_summary_has_no_impact():
    assert rca_note.extract_note_body(SUMMARY) == (ROOT_CAUSE_BODY, "")


def test_bold_metadata_line_is_not_a_heading():
    assert rca_note.heading_text("**2026-09-08 13:49:02 UTC | Critical | payments-api**") is None
    assert rca_note.heading_text("**Root cause confirmed: nothing was deployed.**") is None
    assert rca_note.heading_text("**Impact & Timeline**") == "impact & timeline"
    assert rca_note.heading_text("### Root Cause") == "root cause"
    assert rca_note.heading_text("Summary") == "summary"


def test_root_cause_undetermined_paragraph_is_preferred_wherever_it_sits():
    text = (
        "What happened first, described at some length so the paragraph is prose.\n\n"
        "Impact paragraph, also long enough to count as narrative prose here.\n\n"
        "Root cause undetermined. No checkout API service exists in any observable infrastructure.\n\n"
        "**Next Steps**\n\n- do something"
    )
    assert rca_note.extract_note_body(text)[0].startswith("Root cause undetermined.")


def test_inline_section_labels_are_dropped_but_prose_openers_stay():
    text = (
        "Root Cause: the pool was exhausted after the deploy doubled the worker count without raising the limit.\n\n"
        "Impact & Timeline: no production systems, users or services were affected during the window.\n\n"
    )
    root, impact = rca_note.extract_note_body(text)
    assert root.startswith("the pool was exhausted")
    assert impact.startswith("no production systems")
    undetermined = "Root cause undetermined — no production incident occurred and nothing was deployed in the window."
    assert rca_note.extract_note_body(undetermined)[0] == undetermined


def test_bullet_sections_never_leak_when_no_prose_precedes_them():
    assert rca_note.extract_note_body("## Ruled Out\n\n- **Hypothesis** — killed by evidence [1].\n") == ("", "")


# --- extract_next_steps ------------------------------------------------------

NEXT_STEPS_REPORT = REPORT + (
    "\n## Suggested Next Steps\n\n"
    "- **Redeploy** the checkout frontend so clients drop stale Server Action IDs [7] [S:4]\n"
    "- Add a 5xx alert on `checkout-api` scoped to the deploy window\n"
    "2. Confirm the Fleet Server agent count recovered [28]\n"
    "- A fourth step the note leaves out\n"
)
NEXT_STEPS = [
    "Redeploy the checkout frontend so clients drop stale Server Action IDs",
    "Add a 5xx alert on checkout-api scoped to the deploy window",
    "Confirm the Fleet Server agent count recovered",
]


def test_next_steps_are_read_as_plain_one_liners_capped_at_three():
    assert rca_note.extract_next_steps(NEXT_STEPS_REPORT) == NEXT_STEPS


def test_next_steps_do_not_change_root_cause_or_impact():
    assert rca_note.extract_note_body(NEXT_STEPS_REPORT) == (REPORT_ROOT_CAUSE, REPORT_IMPACT)


@pytest.mark.parametrize("heading", ["## Next steps", "**Next Steps**", "### Recommendations"])
def test_next_steps_heading_variants(heading):
    summary = SUMMARY + f"\n\n{heading}\n- Raise max_connections on orders-db to 400\n\n## Not Checked\n- DNS"
    assert rca_note.extract_next_steps(summary) == ["Raise max_connections on orders-db to 400"]


def test_no_next_steps_section_gives_none():
    assert rca_note.extract_next_steps(REPORT) == []
    assert rca_note.extract_next_steps(None) == []


def test_long_next_step_is_cut_on_a_word():
    step = rca_note.extract_next_steps("## Next steps\n- " + "word " * 60)[0]
    assert step.endswith("word...")
    assert len(step) <= rca_note.NEXT_STEP_MAX_CHARS + 3


def test_typical_next_step_is_not_cut():
    """A real RCA step (~185 chars) used to lose its tail at the old 160 cap."""
    real = (
        "Identify where checkout-api runs (cluster, namespace, or resource group) and query "
        "its 5xx error-rate metric for the window around 2026-11-09T21:37:13 in that metrics source."
    )
    assert rca_note.extract_next_steps("## Suggested Next Steps\n- " + real) == [real]


# --- _compose_note ------------------------------------------------------------

def test_note_ends_with_next_steps_block_before_link_and_disclaimer():
    root, impact = rca_note.extract_note_body(NEXT_STEPS_REPORT)
    steps = rca_note.extract_next_steps(NEXT_STEPS_REPORT)
    note = rca_note.compose_note(root, "i1", impact, "https://aurora.example.com", steps)
    assert note.endswith(
        "Next steps\n- " + "\n- ".join(NEXT_STEPS) + "\n\n"
        "Full investigation: https://aurora.example.com/incidents/i1\n\n"
        "Generated automatically by Aurora. Verify before acting."
    )
    assert "[S:" not in note
    assert len(note) <= rca_note.NOTE_MAX_CHARS


def test_largest_possible_note_fits_the_cap():
    root = rca_note.truncate("word " * 400, rca_note.ROOT_CAUSE_MAX_CHARS)
    impact = rca_note.truncate("word " * 200, rca_note.IMPACT_MAX_CHARS)
    steps = [rca_note.truncate("word " * 60, rca_note.NEXT_STEP_MAX_CHARS)] * rca_note.NEXT_STEPS_MAX
    base_url = "https://aurora." + "x" * 80 + ".example.com"
    note = rca_note.compose_note(root, "0" * 36, impact, base_url, steps)
    assert len(note) <= rca_note.NOTE_MAX_CHARS


def test_note_has_root_cause_impact_link_and_disclaimer():
    note = rca_note.compose_note("Because X.", "i1", "Nobody noticed.", "https://aurora.example.com/")
    assert note == (
        "Aurora RCA\n\nRoot cause\nBecause X.\n\nImpact\nNobody noticed.\n\n"
        "Full investigation: https://aurora.example.com/incidents/i1\n\n"
        "Generated automatically by Aurora. Verify before acting."
    )


def test_note_omits_impact_block_when_none_was_found():
    note = rca_note.compose_note("Because X.", "i1", base_url="https://aurora.example.com")
    assert "Impact" not in note
    assert note.startswith("Aurora RCA\n\nRoot cause\nBecause X.\n\nFull investigation:")


def test_note_omits_link_when_frontend_url_unset():
    note = rca_note.compose_note("Because X.", "i1")
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
    assert content.startswith("Aurora RCA\n\nRoot cause\n" + ROOT_CAUSE_BODY + "\n\n")
    assert "Full investigation: http://localhost:3000/incidents/i1" in content
    assert content.endswith("Generated automatically by Aurora. Verify before acting.")
    assert "**" not in content
    assert wired.built == [{"from_email": "bot@acme.com", "api_token": "tok"}]
    wired.stored.assert_not_called()


def test_posted_note_includes_next_steps(wired):
    assert svc.send_pagerduty_incident_note("u1", _anchor(aurora_summary=NEXT_STEPS_REPORT)) is True
    _, content = wired.client.create_note.call_args.args
    assert "\n\nNext steps\n- " + "\n- ".join(NEXT_STEPS) + "\n\n" in content


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
    monkeypatch.setattr(postback_claim, "set_rls_context", lambda *a, **k: None)
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
    assert svc.send_pagerduty_incident_note("u1", _anchor(source_type="datadog")) is False
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


# --- recurrence note (folded incidents) ---------------------------------------

ANCHOR_ID = "a0"
ANCHOR_FIRED = datetime(2026, 11, 3, 14, 5, 9)
ANCHOR_SUMMARY = (
    "What happened: checkout 5xx paged the on-call at 14:05.\n\n"
    "Root cause: the payments-db connection pool was exhausted after a deploy doubled the worker count [3]. "
    "Postgres max_connections was never raised. A third sentence the note leaves out."
)
FOLD_CLAIM = (
    "UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s AND pagerduty_note_id IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM incidents o WHERE o.id <> %s AND o.source_type = 'pagerduty' "
    "AND o.pagerduty_note_id IS NOT NULL AND o.alert_metadata->>'incidentId' = %s)",
    ("pending", "c1", "c1", "PREP456"),
)
FOLD_RECORD = ("UPDATE incidents SET pagerduty_note_id = %s WHERE id = %s", ("PWL7QXS", "c1"))


def _folded(**over):
    data = {
        "incident_id": "c1", "source_type": "pagerduty", "recurrence_of": ANCHOR_ID, "pagerduty_note_id": None,
        "alert_metadata": {"incidentId": "PREP456"}, "aurora_summary": SUMMARY,
        "anchor_alert_title": "Checkout 5xx spike",
    }
    data.update(over)
    return data


@pytest.fixture
def anchor_row(wired, monkeypatch):
    """Rows _fetch_anchor reads: anchor title, summary, first fire, and whether the PD incident has a note."""
    row = ["Checkout 5xx spike", ANCHOR_SUMMARY, ANCHOR_FIRED, False]
    monkeypatch.setattr(svc, "db_pool", wired.pool.pool)
    monkeypatch.setattr(svc, "set_rls_context", lambda *a, **k: "org-1")
    wired.pool.cursor.fetchone.side_effect = lambda: tuple(row)
    return row


def test_folded_incident_posts_one_recurrence_note(wired, anchor_row, monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "https://aurora.acme.com/")
    assert svc.send_pagerduty_incident_note("u1", _folded()) is True
    assert wired.pool.updates == [FOLD_CLAIM, FOLD_RECORD]
    wired.client.create_note.assert_called_once()
    pd_incident_id, content = wired.client.create_note.call_args.args
    assert pd_incident_id == "PREP456"  # the folded incident's own PagerDuty incident, not the anchor's
    assert content == (
        "Aurora RCA: recurrence\n\n"
        'Recurrence of "Checkout 5xx spike" (2026-11-03 14:05 UTC).\n'
        "Root cause then: the payments-db connection pool was exhausted after a deploy doubled the worker count. "
        "Postgres max_connections was never raised.\n\n"
        "Earlier investigation: https://aurora.acme.com/incidents/a0\n\n"
        "Generated automatically by Aurora. Verify before acting."
    )


def test_anchor_lookup_is_scoped_to_the_anchor_and_this_pd_incident(wired, anchor_row):
    svc.send_pagerduty_incident_note("u1", _folded())
    selects = [(sql, params) for sql, params in wired.pool.executes if sql.startswith("SELECT a.alert_title")]
    assert len(selects) == 1
    sql, params = selects[0]
    assert params == ("c1", "PREP456", ANCHOR_ID)
    assert "o.pagerduty_note_id IS NOT NULL" in sql and "o.id <> %s" in sql


def test_retried_completion_posts_no_second_recurrence_note(wired, anchor_row):
    assert svc.send_pagerduty_incident_note("u1", _folded()) is True
    # The retry re-reads the incident: the claim column now holds the note id
    assert svc.send_pagerduty_incident_note("u1", _folded(pagerduty_note_id="PWL7QXS")) is False
    wired.client.create_note.assert_called_once()


def test_concurrent_recurrence_note_loses_the_claim(wired, anchor_row):
    wired.pool.cursor.rowcount = 0
    assert svc.send_pagerduty_incident_note("u1", _folded()) is False
    assert wired.pool.updates == [FOLD_CLAIM]
    wired.client.create_note.assert_not_called()


def test_pd_incident_already_noted_by_another_aurora_incident_is_skipped(wired, anchor_row):
    # A re-triggered page folded onto its own anchor shares the anchor's PagerDuty incident
    anchor_row[3] = True
    assert svc.send_pagerduty_incident_note("u1", _folded()) is False
    assert wired.pool.updates == []
    wired.client.create_note.assert_not_called()


def test_missing_anchor_posts_nothing(wired, anchor_row):
    wired.pool.cursor.fetchone.side_effect = lambda: None
    assert svc.send_pagerduty_incident_note("u1", _folded()) is False
    wired.client.create_note.assert_not_called()


@pytest.mark.parametrize("over", [
    {"source_type": "datadog"},
    {"pagerduty_note_id": "pending"},
    {"alert_metadata": {}},
    {"alert_metadata": {"incidentId": "../users/PX"}},
])
def test_ineligible_folded_incident_never_touches_db_or_pagerduty(wired, anchor_row, over):
    assert svc.send_pagerduty_incident_note("u1", _folded(**over)) is False
    assert wired.pool.executes == []
    wired.client.create_note.assert_not_called()


def test_read_only_credentials_post_no_recurrence_note(wired, anchor_row, monkeypatch):
    creds = {**wired.creds, "capabilities": {"can_write_incidents": False}}
    monkeypatch.setattr(svc, "get_token_data", lambda user_id, provider: creds)
    assert svc.send_pagerduty_incident_note("u1", _folded()) is False
    assert wired.pool.executes == []
    wired.client.create_note.assert_not_called()


def test_rejected_recurrence_note_releases_the_claim(wired, anchor_row):
    wired.client.create_note.side_effect = PagerDutyAPIError("Rate limited", status_code=429)
    assert svc.send_pagerduty_incident_note("u1", _folded()) is False
    assert wired.pool.updates == [
        FOLD_CLAIM,
        ("UPDATE incidents SET pagerduty_note_id = NULL WHERE id = %s AND pagerduty_note_id = %s", ("c1", "pending")),
    ]


def test_anchor_without_a_usable_root_cause_still_points_at_it(wired, anchor_row, monkeypatch):
    monkeypatch.setattr(svc, "FRONTEND_URL", "")
    anchor_row[1] = "Too short."
    anchor_row[2] = None
    assert svc.send_pagerduty_incident_note("u1", _folded()) is True
    content = wired.client.create_note.call_args.args[1]
    assert 'Recurrence of "Checkout 5xx spike".\nRoot cause then: not determined.' in content
    assert "/incidents/" not in content


def test_recurrence_root_cause_is_capped():
    long_sentence = "The pool was exhausted " + "because of load " * 40 + "today."
    root = svc._short_root_cause("Intro paragraph that is long enough to count as prose.\n\nRoot cause: " + long_sentence)
    assert len(root) <= svc.RECURRENCE_ROOT_CAUSE_MAX_CHARS + 3
    assert root.endswith("...")


def test_recurrence_note_title_is_plain_single_line():
    content = svc.compose_recurrence_note("a0", "**CPU** high\non `api`", None, "x.", None)
    assert 'Recurrence of "CPU high on api".' in content


def test_anchor_note_path_unchanged_by_recurrence_support(wired, monkeypatch):
    # Non-fold path: full RCA note, no anchor lookup
    monkeypatch.setattr(svc, "FRONTEND_URL", "http://localhost:3000")
    assert svc.send_pagerduty_incident_note("u1", _anchor()) is True
    assert not any(sql.startswith("SELECT a.alert_title") for sql, _ in wired.pool.executes)
    content = wired.client.create_note.call_args.args[1]
    assert content.startswith("Aurora RCA\n\nRoot cause\n")
    assert "recurrence" not in content.lower()


# --- review follow-ups ----------------------------------------------------------

def test_recurrence_claim_locks_the_shared_pd_incident_first(wired, anchor_row, monkeypatch):
    """Two folded incidents on one PagerDuty incident serialise on (org, PD incident)."""
    from utils.notifications import postback_claim
    monkeypatch.setattr(postback_claim, "db_pool", wired.pool.pool)
    monkeypatch.setattr(postback_claim, "set_rls_context", lambda *a, **k: "org-1")
    assert svc.send_pagerduty_incident_note("u1", _folded()) is True
    executes = wired.pool.executes
    lock = executes.index(("SELECT pg_advisory_xact_lock(hashtext(%s))", ("pagerduty_note_id:org-1:PREP456",)))
    assert executes[lock + 1] == FOLD_CLAIM


def test_second_folded_incident_on_the_same_pd_incident_loses(wired, anchor_row, monkeypatch):
    """The second claimer's NOT EXISTS sees the first claim, so it posts nothing."""
    from utils.notifications import postback_claim
    monkeypatch.setattr(postback_claim, "db_pool", wired.pool.pool)
    monkeypatch.setattr(postback_claim, "set_rls_context", lambda *a, **k: "org-1")
    assert svc.send_pagerduty_incident_note("u1", _folded()) is True
    wired.pool.cursor.rowcount = 0
    assert svc.send_pagerduty_incident_note("u1", _folded(incident_id="c2")) is False
    wired.client.create_note.assert_called_once()


def test_short_factual_root_cause_is_kept_in_the_recurrence_note():
    summary = "Root cause: a bad deploy of payments-api v2.3 broke auth."
    assert len(summary) < rca_note.MIN_SUMMARY_CHARS + 20
    assert svc._short_root_cause(summary) == "a bad deploy of payments-api v2.3 broke auth."


def test_aware_fired_at_is_shown_in_utc():
    from datetime import timedelta, timezone
    fired = datetime(2026, 11, 3, 14, 5, tzinfo=timezone(timedelta(hours=2)))
    note = svc.compose_recurrence_note("a0", "Checkout 5xx", fired, "")
    assert "(2026-11-03 12:05 UTC)" in note


def test_wrapped_next_step_lines_join_their_item():
    summary = (
        "## Suggested Next Steps\n"
        "- Raise max_connections on orders-db to 400 and redeploy\n"
        "  the orders-api workers\n"
        "- Add a pool-exhaustion alert on orders-api\n"
        "- Roll back deploy 2.3\n"
    )
    assert rca_note.extract_next_steps(summary) == [
        "Raise max_connections on orders-db to 400 and redeploy the orders-api workers",
        "Add a pool-exhaustion alert on orders-api",
        "Roll back deploy 2.3",
    ]


def test_bare_next_step_lines_are_still_one_step_each():
    summary = "## Next steps\nRaise max_connections on orders-db\nRoll back deploy 2.3\n"
    assert rca_note.extract_next_steps(summary) == ["Raise max_connections on orders-db", "Roll back deploy 2.3"]


def test_compose_note_caps_caller_supplied_steps():
    steps = ["word " * 100] * 5
    note = rca_note.compose_note("Because X happened.", "i1", "", None, steps)
    bullets = [line for line in note.splitlines() if line.startswith("- ")]
    assert len(bullets) == rca_note.NEXT_STEPS_MAX
    assert all(len(b) <= rca_note.NEXT_STEP_MAX_CHARS + 5 for b in bullets)
    huge = rca_note.compose_note("x " * 5000, "i1", "y " * 5000, "https://a.example.com", steps)
    assert len(huge) <= rca_note.NOTE_MAX_CHARS
