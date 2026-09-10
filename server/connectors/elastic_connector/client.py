"""
Elastic Cloud (Elasticsearch + Kibana) REST client.

Read-only client used by the Elastic connector routes, the RCA agent tools and
the connector-status checker. Supports Elastic Cloud Hosted (Cloud ID),
Elastic Cloud Serverless (endpoint URLs) and self-managed clusters.

Auth is an Elasticsearch API key (``Authorization: ApiKey <base64(id:key)>``);
the same key is sent to Kibana together with the mandatory ``kbn-xsrf`` header.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from utils.elastic_config import ELASTIC_SSL_VERIFY

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20
CONNECT_TIMEOUT = 10
SEARCH_TIMEOUT = 60
KIBANA_STATUS_TIMEOUT = 5
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0
MAX_SEARCH_SIZE = 500
MAX_ERROR_REASON_LEN = 300
MAX_FIELD_VALUE_LENGTH = 1000

# Fields surfaced first (and always) in compacted log hits.
PRIORITY_FIELDS: Tuple[str, ...] = (
    "@timestamp",
    "message",
    "log.level",
    "service.name",
    "host.name",
)
PRIORITY_PREFIXES: Tuple[str, ...] = ("kubernetes.", "event.", "error.")

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+(:[0-9]{2,5})?$")


class ElasticAPIError(Exception):
    """Raised when Elasticsearch/Kibana returns an error or the request fails."""

    def __init__(self, message: str, status_code: Optional[int] = None, reason: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


# --------------------------------------------------------------------------- #
# Input normalisation helpers
# --------------------------------------------------------------------------- #


def _decode_cloud_component(component: str, default_port: int = 443) -> Tuple[str, int]:
    """Split ``uuid[:port]`` into (uuid, port)."""
    if ":" in component:
        name, port_str = component.rsplit(":", 1)
        try:
            return name, int(port_str)
        except ValueError as exc:
            raise ValueError("Cloud ID contains an invalid port") from exc
    return component, default_port


def parse_cloud_id(cloud_id: str) -> Tuple[str, Optional[str]]:
    """Decode an Elastic Cloud ID into (elasticsearch_url, kibana_url).

    Format: ``name:base64(host[:port]$es_uuid[:port]$kibana_uuid[:port])``.
    Mirrors ``elastic_transport.client_utils.parse_cloud_id``. The Kibana
    component may be empty, in which case ``kibana_url`` is ``None``.
    """
    if not cloud_id or not isinstance(cloud_id, str):
        raise ValueError("Cloud ID is required")
    raw = cloud_id.strip()
    if ":" not in raw:
        raise ValueError("Cloud ID must look like 'name:<base64>'")
    _, encoded = raw.split(":", 1)
    encoded = encoded.strip()
    if not encoded:
        raise ValueError("Cloud ID is missing its encoded section")
    try:
        # Tolerate missing padding.
        padded = encoded + "=" * (-len(encoded) % 4)
        decoded = base64.b64decode(padded, validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Cloud ID is not valid base64") from exc

    parts = decoded.split("$")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError("Cloud ID is malformed (expected host$es_uuid$kibana_uuid)")

    host, host_port = _decode_cloud_component(parts[0])
    if not _HOST_RE.match(host):
        raise ValueError("Cloud ID host is invalid")

    def _build(uuid_component: str) -> str:
        uuid, port = _decode_cloud_component(uuid_component, host_port)
        if not re.match(r"^[A-Za-z0-9._-]+$", uuid):
            raise ValueError("Cloud ID contains an invalid component")
        port_suffix = "" if port == 443 else f":{port}"
        return f"https://{uuid}.{host}{port_suffix}"

    es_url = _build(parts[1])
    kibana_url = _build(parts[2]) if len(parts) > 2 and parts[2] else None
    return es_url, kibana_url


_PATH_RE = re.compile(r"^[A-Za-z0-9._~/-]*$")


def normalize_url(raw: Optional[str], keep_path: bool = False) -> Optional[str]:
    """Normalise an Elasticsearch/Kibana endpoint to ``scheme://host[:port]``.

    Defaults to https, strips query/fragment, rejects userinfo. The path is
    dropped unless ``keep_path`` is set (Kibana is commonly served under a
    ``server.basePath`` such as ``https://ops.example.com/kibana``). Private
    IPs are deliberately allowed (Aurora is self-hosted inside customer
    networks; matches the Splunk/Coroot/Jenkins connectors).
    """
    if not raw or not isinstance(raw, str):
        return None
    url = raw.strip()
    if not url:
        return None
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return None
    if not parsed.netloc or not _HOST_RE.match(parsed.netloc):
        return None
    base = f"{parsed.scheme.lower()}://{parsed.netloc}"
    if not keep_path:
        return base
    path = parsed.path.rstrip("/")
    if path and (not _PATH_RE.match(path) or ".." in path):
        return None
    return f"{base}{path}"


def normalize_index_pattern(raw: Optional[str]) -> Optional[str]:
    """Normalise a comma-separated index pattern list for storage.

    Strips whitespace around each pattern (``"logs-*, filebeat-*"`` →
    ``"logs-*,filebeat-*"``) and validates the result with the same rule
    ``_safe_index`` applies at query time, so a stored default can never be
    rejected later. Returns ``None`` when the pattern is unusable.
    """
    if not raw or not isinstance(raw, str):
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return None
    pattern = ",".join(parts)
    try:
        return _safe_index(pattern)
    except ElasticAPIError:
        return None


def normalize_api_key(raw: Optional[str]) -> Optional[str]:
    """Accept the base64 *encoded* API key or ``id:api_key`` and return the encoded form."""
    if not raw or not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value:
        return None
    if value.lower().startswith("apikey "):
        value = value[7:].strip()
    # id:api_key form → encode. Encoded keys never contain ':'.
    if ":" in value:
        key_id, secret = value.split(":", 1)
        if not key_id or not secret:
            return None
        return base64.b64encode(f"{key_id}:{secret}".encode("utf-8")).decode("ascii")
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return value


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #


def parse_time_range(value: Optional[str]) -> Optional[timedelta]:
    """Parse ``15m``/``2h``/``3d``/``30s``/``1w`` into a timedelta."""
    if not value:
        return None
    match = re.match(r"^\s*(\d+)\s*([smhdw])\s*$", str(value), re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
        "w": timedelta(weeks=amount),
    }[unit]


def resolve_window(
    time_range: Optional[str] = None,
    start_time: Optional[str] = None,
    end_time: Optional[str] = None,
    minutes: Optional[int] = None,
    default: str = "1h",
) -> Tuple[str, str]:
    """Resolve a time window into ISO-8601 ``(start, end)`` strings."""
    now = datetime.now(timezone.utc)
    if start_time or end_time:
        end = _parse_iso(end_time) or now
        start = _parse_iso(start_time) or (end - (parse_time_range(time_range or default) or timedelta(hours=1)))
        return start.isoformat(), end.isoformat()
    if minutes is not None:
        try:
            minutes_int = max(1, min(int(minutes), 60 * 24 * 90))
        except (TypeError, ValueError):
            minutes_int = 60
        return (now - timedelta(minutes=minutes_int)).isoformat(), now.isoformat()
    delta = parse_time_range(time_range) or parse_time_range(default) or timedelta(hours=1)
    return (now - delta).isoformat(), now.isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def build_log_query(
    query_string: Optional[str],
    start: str,
    end: str,
    extra_filter: Optional[Dict[str, Any]] = None,
    timestamp_field: str = "@timestamp",
) -> Dict[str, Any]:
    """Build a Query DSL body: ``query_string`` + time range (+ optional filter)."""
    must: List[Dict[str, Any]] = []
    filters: List[Dict[str, Any]] = [
        {"range": {timestamp_field: {"gte": start, "lte": end}}}
    ]
    if query_string and query_string.strip() and query_string.strip() != "*":
        must.append({
            "query_string": {
                "query": query_string,
                "default_operator": "AND",
                "lenient": True,
            }
        })
    if extra_filter:
        filters.append(extra_filter)
    return {"bool": {"must": must or [{"match_all": {}}], "filter": filters}}


def _truncate_value(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_VALUE_LENGTH:
        return value[:MAX_FIELD_VALUE_LENGTH] + "...[truncated]"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, default=str)
        if len(text) > MAX_FIELD_VALUE_LENGTH:
            return text[:MAX_FIELD_VALUE_LENGTH] + "...[truncated]"
    return value


def _flatten(obj: Any, prefix: str = "", out: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Flatten nested dicts into dotted keys (``log.level``)."""
    if out is None:
        out = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                _flatten(value, dotted, out)
            else:
                out[dotted] = value
    return out


def compact_hits(hits: List[Dict[str, Any]], fields: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Compact ``_source`` docs: priority fields first, long values truncated.

    ``fields`` restricts output to those dotted field names (plus ``_index``/``_id``).
    """
    compacted: List[Dict[str, Any]] = []
    wanted = set(fields or [])
    for hit in hits:
        source = hit.get("_source") or {}
        flat = _flatten(source)
        # Field-API results (when _source is disabled) come as {field: [values]}.
        for key, value in (hit.get("fields") or {}).items():
            if key not in flat:
                flat[key] = value[0] if isinstance(value, list) and len(value) == 1 else value
        doc: Dict[str, Any] = {"_index": hit.get("_index"), "_id": hit.get("_id")}
        if wanted:
            for key in fields or []:
                if key in flat:
                    doc[key] = _truncate_value(flat[key])
            compacted.append(doc)
            continue
        for key in PRIORITY_FIELDS:
            if key in flat:
                doc[key] = _truncate_value(flat[key])
        for key in sorted(flat):
            if key in doc:
                continue
            if key.startswith(PRIORITY_PREFIXES):
                doc[key] = _truncate_value(flat[key])
        for key in sorted(flat):
            if key not in doc:
                doc[key] = _truncate_value(flat[key])
        compacted.append(doc)
    return compacted


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class ElasticClient:
    """Thin, read-only client for Elasticsearch + Kibana REST APIs."""

    def __init__(
        self,
        es_url: str,
        api_key: str,
        kibana_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not es_url:
            raise ValueError("Elasticsearch URL is required")
        if not api_key:
            raise ValueError("Elastic API key is required")
        self.es_url = es_url.rstrip("/")
        self.kibana_url = kibana_url.rstrip("/") if kibana_url else None
        self.api_key = api_key
        self.timeout = timeout

    # -- transport ---------------------------------------------------------- #

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"ApiKey {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _kibana_headers(self) -> Dict[str, str]:
        return {**self.headers, "kbn-xsrf": "true"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        base: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[Any] = None,
        service: str = "Elasticsearch",
        retries: int = MAX_RETRIES,
    ) -> Any:
        """Issue one request with retries for transient transport failures.

        ``timeout`` may be an int or a ``(connect, read)`` tuple. Only connect
        timeouts, connection errors and 429s are retried; a *read* timeout means
        the server accepted the request (e.g. a slow search) and re-submitting
        it would just repeat the expensive work.
        """
        url = f"{(base or self.es_url)}{path}"
        req_headers = headers or self.headers
        req_timeout = timeout or self.timeout
        last_error: Optional[Exception] = None

        for attempt in range(retries + 1):
            try:
                response = requests.request(
                    method,
                    url,
                    headers=req_headers,
                    params=params,
                    json=json_body,
                    timeout=req_timeout,
                    verify=ELASTIC_SSL_VERIFY,
                )
            except requests.exceptions.Timeout as exc:
                last_error = exc
                is_connect_timeout = isinstance(exc, requests.exceptions.ConnectTimeout)
                if is_connect_timeout and attempt < retries:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                logger.error("[ELASTIC] %s %s timed out", method, _safe_path(path))
                if is_connect_timeout:
                    raise ElasticAPIError(
                        f"Connection to {service} timed out. Check that the endpoint is reachable from Aurora."
                    ) from exc
                raise ElasticAPIError(
                    f"{service} did not respond in time. Narrow the query or time range and try again."
                ) from exc
            except requests.exceptions.SSLError as exc:
                # SSLError is a ConnectionError subclass — must be handled first.
                logger.error("[ELASTIC] %s %s SSL error: %s", method, _safe_path(path), type(exc).__name__)
                raise ElasticAPIError(
                    "SSL/TLS error — check the certificate or set ELASTIC_SSL_VERIFY"
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if attempt < retries:
                    time.sleep(RETRY_BACKOFF * (2 ** attempt))
                    continue
                logger.error("[ELASTIC] %s %s connection error: %s", method, _safe_path(path), type(exc).__name__)
                lowered = str(exc).lower()
                if "name or service not known" in lowered or "nodename nor servname" in lowered or "name resolution" in lowered:
                    raise ElasticAPIError("DNS resolution failed. Check the endpoint URL or Cloud ID.") from exc
                if "connection refused" in lowered:
                    raise ElasticAPIError(f"Connection refused. Ensure {service} is running and the port is open.") from exc
                raise ElasticAPIError(
                    f"Unable to connect. Ensure Aurora has network access to your {service} endpoint."
                ) from exc
            except requests.RequestException as exc:
                logger.error("[ELASTIC] %s %s request error: %s", method, _safe_path(path), type(exc).__name__)
                raise ElasticAPIError(f"Unable to reach {service}") from exc

            if response.status_code == 429 and attempt < retries:
                retry_after = _int_header(response.headers.get("Retry-After"), 2)
                time.sleep(min(retry_after, 15))
                continue

            return self._handle_response(response, method, path, service)

        raise ElasticAPIError(f"Unable to reach {service}") from last_error

    def _handle_response(self, response: requests.Response, method: str, path: str, service: str) -> Any:
        status = response.status_code
        if 200 <= status < 300:
            if not response.content:
                return {}
            try:
                return response.json()
            except ValueError as exc:
                raise ElasticAPIError(f"{service} returned a non-JSON response", status_code=status) from exc

        reason = _extract_reason(response)
        if status == 401:
            raise ElasticAPIError("Invalid API key (Elasticsearch returned 401)", status_code=401, reason=reason)
        if status == 403:
            hint = f" ({reason})" if reason else ""
            raise ElasticAPIError(f"API key lacks privileges for this operation{hint}", status_code=403, reason=reason)
        if status == 404:
            raise ElasticAPIError(f"{service} resource not found", status_code=404, reason=reason)
        if status == 429:
            raise ElasticAPIError(f"{service} rate limit exceeded", status_code=429, reason=reason)
        if status == 400:
            detail = f": {reason}" if reason else ""
            raise ElasticAPIError(f"Invalid request{detail}", status_code=400, reason=reason)
        if status >= 500:
            logger.error("[ELASTIC] %s %s returned %s", method, _safe_path(path), status)
            raise ElasticAPIError(f"{service} returned a server error ({status})", status_code=status, reason=reason)
        raise ElasticAPIError(f"{service} request failed ({status})", status_code=status, reason=reason)

    # -- Elasticsearch: cluster / auth ------------------------------------- #

    def info(self) -> Dict[str, Any]:
        """``GET /`` → cluster name, version, build flavor. Requires cluster ``monitor`` (403 otherwise)."""
        return self._request("GET", "/")

    def authenticate(self) -> Dict[str, Any]:
        """``GET /_security/_authenticate`` — works with any valid key (no cluster privilege needed).

        Note ``info()`` (``GET /``) requires the cluster ``monitor`` privilege, which even
        the built-in Viewer role lacks; callers should treat it as best-effort.
        """
        return self._request("GET", "/_security/_authenticate")

    # -- Elasticsearch: indices ------------------------------------------- #

    def resolve_indices(self, pattern: str = "*") -> Dict[str, Any]:
        """``GET /_resolve/index/{pattern}`` — indices, aliases and data streams."""
        safe = _safe_index(pattern)
        return self._request(
            "GET",
            f"/_resolve/index/{safe}",
            params={"expand_wildcards": "open"},
        )

    def cat_indices(self, pattern: str = "*") -> Optional[List[Dict[str, Any]]]:
        """``GET /_cat/indices`` (needs cluster ``monitor``). Returns None on 403."""
        safe = _safe_index(pattern)
        try:
            data = self._request(
                "GET",
                f"/_cat/indices/{safe}",
                params={
                    "format": "json",
                    "h": "index,health,status,docs.count,store.size,pri,rep",
                    "bytes": "b",
                    "expand_wildcards": "open",
                },
            )
        except ElasticAPIError as exc:
            if exc.status_code == 403:
                return None
            raise
        return data if isinstance(data, list) else []

    def field_caps(self, index: str, fields: str = "*") -> Dict[str, Any]:
        """``GET /{index}/_field_caps?fields=...``."""
        safe = _safe_index(index)
        return self._request(
            "GET",
            f"/{safe}/_field_caps",
            params={
                "fields": fields or "*",
                "ignore_unavailable": "true",
                "allow_no_indices": "true",
                "expand_wildcards": "open",
            },
        )

    # -- Elasticsearch: search --------------------------------------------- #

    def search(self, index: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /{index}/_search`` with a Query DSL body. ``size`` clamped to 500."""
        safe = _safe_index(index)
        payload = dict(body)
        size = payload.get("size", 100)
        try:
            payload["size"] = max(0, min(int(size), MAX_SEARCH_SIZE))
        except (TypeError, ValueError):
            payload["size"] = 100
        payload.setdefault("track_total_hits", 10000)
        return self._request(
            "POST",
            f"/{safe}/_search",
            params={
                "ignore_unavailable": "true",
                "allow_no_indices": "true",
                "expand_wildcards": "open",
            },
            json_body=payload,
            timeout=(CONNECT_TIMEOUT, SEARCH_TIMEOUT),
        )

    def esql(self, query: str, filter_dsl: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """``POST /_query`` (ES|QL). Returns ``{columns:[{name,type}], values:[[...]]}``."""
        body: Dict[str, Any] = {"query": query}
        if filter_dsl:
            body["filter"] = filter_dsl
        # ``format`` is a query parameter (a body field is rejected on 9.x).
        return self._request(
            "POST", "/_query", params={"format": "json"}, json_body=body,
            timeout=(CONNECT_TIMEOUT, SEARCH_TIMEOUT),
        )

    def search_alerts(
        self,
        status: Optional[str] = "active",
        hours: int = 24,
        size: int = 50,
        rule_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Search Kibana alert documents in ``.alerts-*``."""
        try:
            hours_int = max(1, min(int(hours), 24 * 90))
        except (TypeError, ValueError):
            hours_int = 24
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=hours_int)).isoformat()
        filters: List[Dict[str, Any]] = [
            {"range": {"@timestamp": {"gte": start, "lte": now.isoformat()}}}
        ]
        if status and status != "all":
            filters.append({"term": {"kibana.alert.status": status}})
        if rule_name:
            # simple_query_string never raises on syntax: rule names routinely
            # contain ':', '[', '(' and '>' which are Lucene reserved characters.
            filters.append({
                "simple_query_string": {
                    "query": rule_name,
                    "fields": ["kibana.alert.rule.name"],
                    "default_operator": "and",
                    "lenient": True,
                }
            })
        body = {
            "size": max(1, min(int(size or 50), MAX_SEARCH_SIZE)),
            "sort": [{"@timestamp": {"order": "desc"}}],
            "query": {"bool": {"filter": filters}},
            "_source": [
                "@timestamp",
                "kibana.alert.uuid",
                "kibana.alert.status",
                "kibana.alert.start",
                "kibana.alert.end",
                "kibana.alert.duration.us",
                "kibana.alert.reason",
                "kibana.alert.rule.name",
                "kibana.alert.rule.uuid",
                "kibana.alert.rule.category",
                "kibana.alert.rule.tags",
                "kibana.alert.instance.id",
                "kibana.alert.evaluation.value",
                "kibana.alert.evaluation.threshold",
                "kibana.alert.severity",
                "kibana.alert.workflow_status",
                "kibana.space_ids",
                "service.name",
                "host.name",
            ],
            "track_total_hits": True,
        }
        try:
            return self.search(".alerts-*", body)
        except ElasticAPIError as exc:
            if exc.status_code == 403:
                raise ElasticAPIError(
                    "API key lacks privileges to read Kibana alerts. Grant 'read' on the '.alerts-*' indices.",
                    status_code=403,
                    reason=exc.reason,
                ) from exc
            raise

    # -- Kibana ------------------------------------------------------------ #

    def kibana_status(self) -> Optional[Dict[str, Any]]:
        """Best-effort ``GET {kb}/api/status``. Returns None when unreachable/unauthorised."""
        if not self.kibana_url:
            return None
        try:
            # Single short attempt: this runs inside /connect and must not push
            # the request past the frontend proxy's timeout when Kibana is down.
            data = self._request(
                "GET",
                "/api/status",
                base=self.kibana_url,
                headers=self._kibana_headers(),
                timeout=KIBANA_STATUS_TIMEOUT,
                service="Kibana",
                retries=0,
            )
        except ElasticAPIError as exc:
            logger.info("[ELASTIC] Kibana status check failed: %s", exc)
            return None
        if not isinstance(data, dict):
            return None
        version = (data.get("version") or {}).get("number")
        status = ((data.get("status") or {}).get("overall") or {})
        return {
            "version": version,
            "level": status.get("level") or status.get("state"),
        }

    def find_rules(
        self,
        page: int = 1,
        per_page: int = 50,
        search: Optional[str] = None,
        space: Optional[str] = None,
    ) -> Dict[str, Any]:
        """``GET {kb}/api/alerting/rules/_find``. Requires Kibana application privileges."""
        if not self.kibana_url:
            raise ElasticAPIError("Kibana URL is not configured for this connection", status_code=400)
        params: Dict[str, Any] = {
            "page": max(1, int(page or 1)),
            "per_page": max(1, min(int(per_page or 50), 100)),
            "sort_field": "name",
            "sort_order": "asc",
        }
        if search:
            params["search"] = search
            params["search_fields"] = "name"
        prefix = f"/s/{_safe_space(space)}" if space else ""
        try:
            return self._request(
                "GET",
                f"{prefix}/api/alerting/rules/_find",
                base=self.kibana_url,
                params=params,
                headers=self._kibana_headers(),
                service="Kibana",
            )
        except ElasticAPIError as exc:
            if exc.status_code == 403:
                raise ElasticAPIError(
                    "API key lacks Kibana privileges. Create the key as a user with the Viewer role "
                    "or add the 'kibana-.kibana' read application privilege.",
                    status_code=403,
                    reason=exc.reason,
                ) from exc
            raise


# --------------------------------------------------------------------------- #
# Private helpers
# --------------------------------------------------------------------------- #

_INDEX_RE = re.compile(r"^[A-Za-z0-9_.*,\-]+$")


def _safe_index(value: Optional[str]) -> str:
    """Validate an index pattern (letters, digits, ``_ . - * ,``) for path use."""
    if not value or not isinstance(value, str):
        return "*"
    pattern = value.strip()
    if len(pattern) > 512 or not _INDEX_RE.match(pattern) or "/" in pattern or ".." in pattern:
        raise ElasticAPIError("Invalid index pattern", status_code=400)
    return pattern


def _safe_space(value: str) -> str:
    if not re.match(r"^[A-Za-z0-9_-]{1,64}$", value or ""):
        raise ElasticAPIError("Invalid Kibana space id", status_code=400)
    return value


def _safe_path(path: str) -> str:
    return path.split("?", 1)[0][:120]


def _int_header(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value else default
    except ValueError:
        return default


def _extract_reason(response: requests.Response) -> Optional[str]:
    """Pull ``error.root_cause[0].reason`` (or ``error.reason``/``message``) from an error body."""
    try:
        data = response.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    reason: Optional[str] = None
    if isinstance(error, dict):
        root = error.get("root_cause")
        if isinstance(root, list) and root and isinstance(root[0], dict):
            reason = root[0].get("reason")
        reason = reason or error.get("reason")
    elif isinstance(error, str):
        reason = error
    reason = reason or data.get("message")
    if not reason:
        return None
    return str(reason)[:MAX_ERROR_REASON_LEN]
