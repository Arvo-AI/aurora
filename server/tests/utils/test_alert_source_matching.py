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
