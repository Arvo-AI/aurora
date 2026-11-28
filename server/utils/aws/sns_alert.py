"""Normalize authenticated SNS Notifications without changing their stored payload."""
import re
from dataclasses import dataclass

_FIELDS = re.compile(
    r"^[ \t]*(Terminal|Device|RPT|Client|Service|Resource|Severity|Connectivity|Terminal Alert|Low cash)"
    r"[ \t]*:[ \t]*(.+?)[ \t]*$", re.I | re.M,
)
_SEVERITY = re.compile(r"\b(critical|high|medium|low|warning|info)\b", re.I)
_TERMINAL_STATUS = re.compile(r"\bterminal\s+([\w.-]+)\s+(?:went|is)\s+(?:down|up|offline|online)\b", re.I)
_DEVICE_STATUS = re.compile(
    r"\b(?:terminal|device)\s+([\w.-]+)(?:\s+\([^()\n]+\))?\s+"
    r"(?:is|went)\s+(down|offline|up|online)\b", re.I,
)
_TRANSITION = re.compile(r"(online|offline|stale)\s*(?:→|->)\s*(online|offline)", re.I)
# A test notification is different from an actual outage in a [TEST] environment.
_TEST_MESSAGE = re.compile(
    r"^(?:this is (?:an? )?)?(?:(?:amazon )?sns (?:console )?)?test(?: (?:message|notification))?"
    r"(?: from (?:the )?(?:amazon )?sns console)?[.!]?$", re.I,
)
_CONNECTIVITY_DOWN = {"down", "offline"}
_CONNECTIVITY_UP = {"up", "online"}
_RECOVERY_TITLE = re.compile(r"^(?:\[(?:resolved|recovered|ok)\]|(?:resolved|recovered)(?:\s*[:\-]|$))", re.I)


@dataclass(frozen=True)
class SnsAlert:
    title: str
    description: str
    service: str
    severity: str
    state: str = "ALARM"
    condition: str = ""
    continued: bool = False


def _plain_state(title, body, fields, statuses):
    """Return the current state and condition from known status reports."""
    headline = body.splitlines()[0].strip()
    if _TEST_MESSAGE.fullmatch(headline) or _TEST_MESSAGE.fullmatch(title) or _RECOVERY_TITLE.match(title):
        return "RECORD_ONLY", "", False

    low_cash = "low cash" in fields or "low cash" in title.lower()
    offline_condition = "connectivity_and_low_cash" if low_cash else "connectivity"
    transition = _TRANSITION.fullmatch(fields.get("connectivity", ""))
    if transition:
        previous, current = (value.lower() for value in transition.groups())
        if current == "offline" and not statuses.intersection(_CONNECTIVITY_UP):
            return "ALARM", offline_condition, previous == current
        if current != "online" or low_cash or statuses.intersection(_CONNECTIVITY_DOWN):
            return "RECORD_ONLY", "", False
        return "OK", "connectivity", False

    if (fields.get("terminal alert", "").upper() == "FREQUENT_NETWORK_OUTAGES"
            or re.match(r"^(?:\[\w+\]\s*)?FREQUENT_NETWORK_OUTAGES\b", title, re.I)):
        return "ALARM", "network_outages", False

    if statuses and statuses <= _CONNECTIVITY_DOWN:
        return "ALARM", offline_condition, False
    if statuses and statuses <= _CONNECTIVITY_UP and not low_cash:
        return "OK", "connectivity", False

    # Other direct failure reports have no corresponding automatic recovery rule.
    report = next((line.strip() for line in body.splitlines() if line.strip() and not _FIELDS.fullmatch(line)), "")
    if any(line.casefold().rstrip(".!") in {"power lost", "battery offline", "signal unavailable"}
           for line in (title, report)):
        return "ALARM", "", False
    return "RECORD_ONLY", "", False


def parse_sns_alert(payload: dict) -> SnsAlert | None:
    content = payload.get("content")
    structured = payload.get("source") == "custom" and payload.get("version") == "1.0" and isinstance(content, dict)
    if structured:
        title, body = content.get("title"), content.get("description")
    elif payload.get("Type") == "Notification":
        title, body = payload.get("Subject"), payload.get("Message")
    else:
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    body = body.strip()
    title = title.strip() if isinstance(title, str) else ""
    title = title or body.splitlines()[0]
    fields = {k.lower(): v.strip() for k, v in _FIELDS.findall(body)}
    resource = next((fields[k] for k in ("terminal", "client", "service", "resource") if fields.get(k)), "")
    if not resource:
        terminals = set(_TERMINAL_STATUS.findall(f"{title}\n{body}"))
        if len(terminals) == 1:
            resource = terminals.pop()
    # An explicit body field or severity prefix wins. "low cash" is a symptom.
    match = _SEVERITY.fullmatch(fields.get("severity", "")) or re.match(
        r"^(?:\[v\d+\]\s*)?\[?(critical|high|medium|low|warning|info)\b", title, re.I
    )
    level = match[1].lower() if match else "unknown"
    severity = {"warning": "medium", "info": "low"}.get(level, level)
    if structured:
        return SnsAlert(title, body, resource[:255], severity)

    # Match recoveries by an explicit device identity. A site/client may share many devices.
    identities = [fields[k] for k in ("terminal", "device", "rpt") if fields.get(k)]
    statuses = set()
    for line in (title, body.splitlines()[0]):
        if _DEVICE_STATUS.match(line):
            for device, status in _DEVICE_STATUS.findall(line):
                identities.append(device)
                statuses.add(status.lower())
    state, condition, continued = _plain_state(title, body, fields, statuses)
    if identities and len({name.casefold() for name in identities}) == 1 and len(identities[0]) <= 255:
        return SnsAlert(title, body, identities[0], severity, state, condition, continued)
    return SnsAlert(title, body, resource[:255], severity, state)
