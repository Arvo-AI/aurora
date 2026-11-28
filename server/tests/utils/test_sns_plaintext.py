import pytest

from utils.aws.sns_alert import parse_sns_alert


@pytest.mark.parametrize(("subject", "body", "state", "condition", "continued"), [
    ("Terminal kiosk-7 is DOWN [TEST]", "Terminal kiosk-7 (lab) went DOWN.\nPrevious Status: ACTIVE",
     "ALARM", "connectivity", False),
    ("", "Terminal kiosk-7 went up.", "OK", "connectivity", False),
    ("", "Device kiosk-7 is ONLINE.\nPrevious Status: DOWN", "OK", "connectivity", False),
    ("[CRITICAL] FREQUENT_NETWORK_OUTAGES - kiosk-7", "Terminal: kiosk-7\nLast outage ended at 12:05",
     "ALARM", "network_outages", False),
    ("[V2] CRITICAL kiosk-7 OFFLINE + low cash", "RPT: kiosk-7\nClient: lab\nConnectivity: STALE → OFFLINE",
     "ALARM", "connectivity_and_low_cash", False),
    ("", "Device: kiosk-7\nConnectivity: OFFLINE -> OFFLINE\nLow cash: $10",
     "ALARM", "connectivity_and_low_cash", True),
    ("", "Device: kiosk-7\nConnectivity: OFFLINE → ONLINE", "OK", "connectivity", False),
    ("", "Device: kiosk-7\nConnectivity: OFFLINE → ONLINE\nLow cash: $10", "RECORD_ONLY", "", False),
    ("Test", "Test", "RECORD_ONLY", "", False),
    ("Test notification", "This is a test message from the Amazon SNS console.", "RECORD_ONLY", "", False),
    ("Test message", "Terminal kiosk-7 went DOWN", "RECORD_ONLY", "", False),
    ("Resolved: outage", "Terminal kiosk-7 went down.", "RECORD_ONLY", "", False),
    ("FYI", "Previous Status: Terminal kiosk-7 went down", "RECORD_ONLY", "", False),
    ("critical", "Routine status update", "RECORD_ONLY", "", False),
    ("", "Terminal kiosk-7 is not offline", "RECORD_ONLY", "", False),
    ("Terminal kiosk-7 is down", "Terminal kiosk-7 is up", "RECORD_ONLY", "", False),
    ("", "Device: kiosk-7\nConnectivity: UNKNOWN", "RECORD_ONLY", "", False),
    ("Signal unavailable", "Service: uplink", "ALARM", "", False),
])
def test_classify_plain_sns(subject, body, state, condition, continued):
    alert = parse_sns_alert({"Type": "Notification", "Subject": subject, "Message": body})
    assert (alert.state, alert.condition, alert.continued) == (state, condition, continued)
    if condition:
        assert alert.service == "kiosk-7"


@pytest.mark.parametrize("body", [
    "Client: shared site\nConnectivity: OFFLINE → ONLINE",
    "Device: kiosk-8\nConnectivity: OFFLINE → ONLINE",
])
def test_recovery_requires_one_identifiable_device(body):
    title = "Terminal kiosk-7 is online" if "kiosk-8" in body else "Connectivity update"
    alert = parse_sns_alert({"Type": "Notification", "Subject": title, "Message": body})
    assert alert.state == "OK"
    assert not alert.condition


def test_structured_sns_uses_existing_policy():
    alert = parse_sns_alert({"source": "custom", "version": "1.0", "content": {
        "title": "Test", "description": "Device: kube-2\nClient: lab",
    }})
    assert (alert.state, alert.service, alert.condition) == ("ALARM", "lab", "")
