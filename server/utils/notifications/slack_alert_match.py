"""Locate a provider's original Slack post using its identity and payload evidence."""
from datetime import datetime, timezone
import hashlib
import html
import json
import logging
import time
import re
from urllib.parse import urlsplit

# Slack Marketplace application IDs. Bot IDs and display names vary by installation.
_APP_SOURCES = {
    "A6L22LZNH": "cloudwatch",
    "A011MFBJEUU": "sentry",
    "A1FKYAUUX": "pagerduty",
    "A0238CWMJDB": "pagerduty",
    "A01DEGPUHHC": "incidentio",
}
_WINDOW = 180
logger = logging.getLogger(__name__)


def uses_source_thread(source_type):
    return source_type in _APP_SOURCES.values()


def _strings(value):
    if isinstance(value, str):
        yield html.unescape(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _normal(text):
    return " ".join(re.findall(r"\w+", text.casefold()))


def _incident_link(url, source):
    """Identify a native provider object, ignoring tracking parameters and Slack link labels."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme != "https":
        return None
    if source == "pagerduty" and (host == "pagerduty.com" or host.endswith(".pagerduty.com")):
        match = re.fullmatch(r"/(incidents)/([A-Za-z0-9_-]+)/?", parts.path)
    elif source == "incidentio" and host == "app.incident.io":
        match = re.fullmatch(r"/(?:[\w-]+/)?(incidents|alerts)/([\w-]+)/?", parts.path)
    else:
        return None
    if match:
        return host, parts.path.rstrip("/"), match[1], match[2]
    return None


def _matches(message, data, team):
    source = data["source_type"]
    profile = message.get("bot_profile") or {}
    if _APP_SOURCES.get(message.get("app_id") or profile.get("app_id")) != source:
        return False
    if (profile.get("team_id") or message.get("team") or team) != team:
        return False
    text = "\n".join(_strings([message.get(k) for k in ("text", "attachments", "blocks")]))
    meta = data.get("alert_metadata") or {}
    if source == "sentry":
        issue = str(meta.get("issueId") or "")
        if not issue.isdigit():
            return False
        link = re.compile(r"/issues/" + re.escape(issue) + r"(?:/events/([a-fA-F0-9]{32}))?(?=[/?#|>\s]|$)")
        event = str(meta.get("eventId") or "").lower()
        return any(not match[1] or not event or match[1].lower() == event for match in link.finditer(text))
    if source in ("incidentio", "pagerduty"):
        expected = _incident_link(meta.get("incidentUrl") or meta.get("permalink") or "", source)
        native_id = meta.get("incidentId")
        kind = "alerts" if str(meta.get("event_type", "")).startswith(("public_alert.", "private_alert.")) else "incidents"
        for url in re.findall(r"https://[^\s<>|]+", text):
            link = _incident_link(url, source)
            if link and link[2] == kind:
                if link == expected or (expected is None and link[3] == native_id):
                    return True
        return False
    arn = meta.get("alarm_arn")
    if arn and re.search(re.escape(arn) + r"(?=[|>\s]|$)", text):
        return True
    title = _normal(data.get("alert_title") or "")
    body = meta.get("reason") or ""
    normalized = " " + _normal(text) + " "
    if not title or " " + title + " " not in normalized or not body:
        return False
    # Timestamp in a custom notification identifies a particular occurrence.
    timestamp = re.search(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\b", body)
    if timestamp and timestamp[0] in text:
        return True
    detail = _normal(body)
    return bool(detail and detail != title and " " + detail + " " in normalized)


def _published_at(data):
    value = (data.get("alert_metadata") or {}).get("source_post_at") or data["created_at"]
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        value = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, AttributeError, TypeError):
        return None


def source_thread_window_end(data):
    when = _published_at(data)
    return when + _WINDOW if when is not None else None


def _channel_page(client, params, team, cache, org):
    # Bursts share history pages for the same minute. Only closed windows
    # are cached so a late post is visible on the very next retry.
    key = None
    if cache is not None and float(params["latest"]) < time.time():
        identity = json.dumps([org, team, params], sort_keys=True).encode()
        key = "rca:slack-app-history:v2:" + hashlib.sha256(identity).hexdigest()
        try:
            saved = cache.get(key)
            if saved:
                return json.loads(saved)
        except Exception:
            logger.warning("Slack history cache unavailable")
    page = client._make_request("GET", "conversations.history", params, timeout=10, max_retries=0)
    if key and (not page.get("has_more") or (page.get("response_metadata") or {}).get("next_cursor")):
        keep = [msg for msg in page.get("messages", []) if
                (msg.get("app_id") or (msg.get("bot_profile") or {}).get("app_id")) in _APP_SOURCES]
        try:
            cache.setex(key, 900, json.dumps({**page, "messages": keep}))
        except Exception:
            logger.warning("Could not cache Slack alert history")
    return page


def find_source_thread(client, channels, team, data, *, competing=(), cache=None, org=None):
    """Only return a unique match after every registered channel has been checked."""
    when = _published_at(data)
    if when is None:
        return None
    from connectors.slack_connector.client import SlackAPIError

    matches = set()
    bucket = int(when // 60) * 60
    for channel in channels:
        params = dict(channel=channel, oldest=str(bucket - _WINDOW), latest=str(bucket + 60 + _WINDOW), limit=100)
        while True:
            try:
                page = _channel_page(client, params, team, cache, org)
            except SlackAPIError as exc:
                if exc.error in {"channel_not_found", "not_in_channel", "is_archived"}:
                    break
                raise
            for msg in page.get("messages", []):
                if not msg.get("ts") or abs(float(msg["ts"]) - when) > _WINDOW:
                    continue
                if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
                    continue
                if _matches(msg, data, team):
                    if any(_matches(msg, other, team) for other in competing):
                        return None
                    matches.add((channel, msg["ts"]))
                    if len(matches) > 1:
                        return None
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                # A truncated search cannot establish uniqueness.
                if page.get("has_more"):
                    raise ValueError("Incomplete Slack history page")
                break
            params["cursor"] = cursor
    return next(iter(matches), None)
