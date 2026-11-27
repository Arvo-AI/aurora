from unittest.mock import Mock

import pytest

from utils.aws.sns_alert import parse_sns_alert
from utils.notifications.slack_alert_match import find_source_thread


def test_custom_description_without_title_and_plain_subject():
    assert parse_sns_alert({"source": "custom", "version": "1.0", "content": {
        "description": "Disk full\nService: checkout\nSeverity: HIGH"
    }}).title == "Disk full"
    plain = parse_sns_alert({"Type": "Notification", "Subject": "[V2] CRITICAL offline",
                             "Message": "Client: terminal-14\nLow cash: $10"})
    assert (plain.title, plain.service, plain.severity) == ("[V2] CRITICAL offline", "terminal-14", "critical")
    assert parse_sns_alert({"source": "other", "content": {"description": "x"}}) is None
    assert parse_sns_alert({"Type": "Notification", "Message": ""}) is None


def test_missing_severity_is_not_inferred_from_a_symptom():
    assert parse_sns_alert({"Type": "Notification", "Subject": "Cash balance low", "Message": "No bills"}).severity == "unknown"


@pytest.mark.parametrize("custom", [False, True])
def test_terminal_status_identifies_resource_without_inventing_severity(custom):
    title = "Terminal kiosk-17 is DOWN"
    body = "Terminal kiosk-17 went DOWN during the status check."
    if custom:
        payload = {"source": "custom", "version": "1.0", "content": {"title": title, "description": body}}
    else:
        payload = {"Type": "Notification", "Subject": title, "Message": body}
    alert = parse_sns_alert(payload)
    assert (alert.service, alert.severity) == ("kiosk-17", "unknown")


def test_multiple_terminal_statuses_do_not_identify_a_single_resource():
    payload = {"Type": "Notification", "Message": "Terminal kiosk-17 went down. Terminal kiosk-18 is offline."}
    assert parse_sns_alert(payload).service == ""


def page(*messages, cursor=""):
    return {"messages": list(messages), "response_metadata": {"next_cursor": cursor}}


SENTRY = {"source_type": "sentry", "created_at": 1700000000, "alert_metadata": {"issueId": "55"}}
POST = {"ts": "1700000001.1", "app_id": "A011MFBJEUU", "team": "T1",
        "attachments": [{"title_link": "https://example.sentry.io/issues/55/"}]}


@pytest.mark.parametrize("changes", [
    {"app_id": "another-app"}, {"team": "other"}, {"attachments": [{"title_link": "/issues/555/"}]}
])
def test_sentry_rejects_other_apps_workspaces_and_issue_numbers(changes):
    client = Mock()
    client._make_request.return_value = page({**POST, **changes})
    assert find_source_thread(client, ["C"], "T1", SENTRY) is None


def test_non_unique_matches_on_later_pages_or_in_other_channels():
    client = Mock()
    client._make_request.side_effect = [page(POST, cursor="next"), page(), page(POST)]
    assert find_source_thread(client, ["C1", "C2"], "T1", SENTRY) is None


@pytest.mark.parametrize("preferred,main_posts,expected", [
    ("C-main", 1, ("C-main", POST["ts"])),
    ("C-main", 0, ("C-service", POST["ts"])),
    ("C-main", 2, None),
    ("C-elsewhere", 1, None),
    (None, 1, None),
])
def test_pagerduty_announcements_in_multiple_channels(preferred, main_posts, expected):
    url = "https://acme.pagerduty.com/incidents/P123"
    data = {"source_type": "pagerduty", "created_at": 1700000000, "alert_metadata": {"incidentUrl": url}}
    post = {"ts": POST["ts"], "app_id": "A1FKYAUUX", "team": "T1", "text": f"<{url}|QA alert>"}
    main = [post, {**post, "ts": "1700000002.2"}][:main_posts]
    client = Mock()
    client._make_request.side_effect = [page(post), page(*main)]
    assert find_source_thread(client, ["C-service", "C-main"], "T1", data, preferred_channel=preferred) == expected


def test_title_and_arrival_time_alone_never_identify_an_amazon_post():
    client = Mock()
    title = "[CRITICAL] Power lost - terminal 42"
    data = {"source_type": "cloudwatch", "created_at": 1700000000, "alert_title": title,
            "alert_metadata": {"reason": "Terminal: 42\nTimestamp: 2026-01-03T10:01:00Z"}}
    post = {"ts": "1700000001.1", "bot_profile": {"app_id": "A6L22LZNH", "team_id": "T1"},
            "text": title}
    client._make_request.return_value = page(post)
    assert find_source_thread(client, ["C"], "T1", data) is None
    client._make_request.return_value = page({**post, "blocks": [{"text": {"text": "2026-01-03T10:01:00Z"}}]})
    assert find_source_thread(client, ["C"], "T1", data) == ("C", post["ts"])


def test_an_issue_post_matching_two_webhooks_has_no_unique_owner():
    client = Mock()
    client._make_request.return_value = page(POST)
    assert find_source_thread(client, ["C"], "T1", SENTRY, competing=[SENTRY]) is None


def test_event_permalink_distinguishes_two_occurrences_of_a_sentry_issue():
    client = Mock()
    event_a = dict(SENTRY, alert_metadata={"issueId": "55", "eventId": "a"*32})
    event_b = dict(SENTRY, alert_metadata={"issueId": "55", "eventId": "b"*32})
    client._make_request.return_value = page({**POST, "attachments": [{"title_link": "/issues/55/events/" + "a"*32 + "/"}]})
    assert find_source_thread(client, ["C"], "T1", event_a, competing=[event_b]) == ("C", POST["ts"])


@pytest.mark.parametrize("source,app_id,metadata,url", [
    ("pagerduty", "A1FKYAUUX", {"incidentId": "PI123"}, "https://acme.pagerduty.com/incidents/PI123"),
    ("pagerduty", "A0238CWMJDB", {"incidentId": "PI123"}, "https://acme.eu.pagerduty.com/incidents/PI123"),
    ("incidentio", "A01DEGPUHHC", {"permalink": "https://app.incident.io/acme/incidents/13"},
     "https://app.incident.io/acme/incidents/13/?utm_source=slack"),
    ("incidentio", "A01DEGPUHHC", {"event_type": "private_alert.alert_created_v1", "incidentId": "A123",
                                    "permalink": "https://monitor.example.com/dashboard"},
     "https://app.incident.io/acme/alerts/A123"),
])
@pytest.mark.parametrize("channel", ["C-main", "C-service"])
def test_original_provider_post_can_be_in_any_channel(source, app_id, metadata, url, channel):
    data = {"source_type": source, "created_at": 1700000000, "alert_metadata": metadata}
    post = {"ts": POST["ts"], "bot_profile": {"app_id": app_id, "team_id": "T1"},
            "blocks": [{"type": "section", "text": {"text": f"<{url}|View incident>"}}]}
    client = Mock()
    client._make_request.side_effect = lambda method, endpoint, params, **kwargs: (
        page(post) if params["channel"] == channel else page()
    )
    assert find_source_thread(client, ["C-main", "C-service"], "T1", data) == (channel, POST["ts"])


@pytest.mark.parametrize("reference,subscribe_id,path,matches", [
    ("INC-3", None, "acme/incidents/3", True),
    (None, "01INC", "acme/incidents/3", True),  # Previously stored metadata has only a permalink.
    ("INC-3", "01INC", "acme/incidents/3", True),
    ("INC-3", "01OTHER", "acme/incidents/3", False),
    ("INC-3", "01INC", "acme/incidents/30", False),
    (None, None, "acme/incidents/3", False),
    ("INC-3", "01INC", "other/incidents/3", False),
    ("INC-3", None, "incidents/3", False),
    ("INC-3", None, "acme/alerts/3", False),
])
def test_incidentio_numbered_announcement(reference, subscribe_id, path, matches):
    metadata = {"permalink": "https://app.incident.io/acme/incidents/01INC"}
    if reference:
        metadata["reference"] = reference
    buttons = [{"type": "button", "url": f"https://app.incident.io/{path}"}]
    if subscribe_id:
        buttons.append({"type": "button", "action_id": "action_subscribe", "value": subscribe_id})
    message = {"app_id": "A01DEGPUHHC", "team": "T1", "ts": POST["ts"],
               "attachments": [{"blocks": [{"type": "actions", "elements": buttons}]}]}
    client = Mock()
    client._make_request.return_value = page(message)
    data = {"source_type": "incidentio", "alert_metadata": metadata, "created_at": 1700000000}
    assert find_source_thread(client, ["C"], "T1", data) == (("C", POST["ts"]) if matches else None)


def test_incidentio_reference_in_a_related_incident_link_does_not_own_the_card():
    base = "https://app.incident.io/acme/incidents"
    client = Mock()
    client._make_request.return_value = page({
        "app_id": "A01DEGPUHHC", "ts": POST["ts"], "text": f"Related to <{base}/3|INC-3>",
        "blocks": [{"type": "actions", "elements": [{"type": "button", "url": f"{base}/4"}]}],
    })
    data = {"source_type": "incidentio", "created_at": 1700000000,
            "alert_metadata": {"permalink": f"{base}/01INC", "reference": "INC-3"}}
    assert find_source_thread(client, ["C"], "T1", data) is None


@pytest.mark.parametrize("url,app_id,parent", [
    ("https://acme.pagerduty.com/incidents/PI1234", "A1FKYAUUX", None),
    ("https://acme.pagerduty.com.invalid/incidents/PI123", "A1FKYAUUX", None),
    ("https://other.pagerduty.com/incidents/PI123", "A1FKYAUUX", None),
    ("https://acme.pagerduty.com/incidents/PI123", "unrelated-app", None),
    ("https://acme.pagerduty.com/incidents/PI123", "A1FKYAUUX", "1699999999.0"),
])
def test_shared_title_wrong_provider_or_reply_is_not_an_original_alert(url, app_id, parent):
    data = {"source_type": "pagerduty", "created_at": 1700000000, "alert_title": "API down",
            "alert_metadata": {"incidentId": "PI123", "incidentUrl": "https://acme.pagerduty.com/incidents/PI123"}}
    client = Mock()
    client._make_request.return_value = page({"ts": POST["ts"], "app_id": app_id, "text": f"API down {url}",
                                             "thread_ts": parent})
    assert find_source_thread(client, ["C-main"], "T1", data) is None
