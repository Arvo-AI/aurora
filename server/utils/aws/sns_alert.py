"""Normalize authenticated SNS Notifications without changing their stored payload."""
import re
from dataclasses import dataclass

_FIELDS = re.compile(r"^\s*(Terminal|Client|Service|Resource|Severity)\s*:\s*(.+?)\s*$", re.I | re.M)
_SEVERITY = re.compile(r"\b(critical|high|medium|low|warning|info)\b", re.I)
_TERMINAL_STATUS = re.compile(r"\bterminal\s+([\w.-]+)\s+(?:went|is)\s+(?:down|up|offline|online)\b", re.I)


@dataclass(frozen=True)
class SnsAlert:
    title: str
    description: str
    service: str
    severity: str


def parse_sns_alert(payload: dict) -> SnsAlert | None:
    content = payload.get("content")
    if payload.get("source") == "custom" and payload.get("version") == "1.0" and isinstance(content, dict):
        title, body = content.get("title"), content.get("description")
    elif payload.get("Type") == "Notification":
        title, body = payload.get("Subject"), payload.get("Message")
    else:
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    title = title.strip() if isinstance(title, str) else ""
    title = title or body.strip().splitlines()[0]
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
    return SnsAlert(title, body.strip(), resource[:255], {"warning": "medium", "info": "low"}.get(level, level))
