"""Read-only client for the RabbitMQ management HTTP API.

Credentials are HTTP basic auth against the management plugin (port 15672).
Message bodies are never fetched: the ``/get`` endpoint dequeues.
"""

import ipaddress
import logging
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2_000_000
MAX_PAGE_SIZE = 100

# Exact hostnames that always resolve to a cloud metadata service.
_BLOCKED_HOSTS = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})

_QUEUE_ARGUMENTS = (
    "x-dead-letter-exchange",
    "x-dead-letter-routing-key",
    "x-message-ttl",
    "x-expires",
    "x-max-length",
    "x-max-length-bytes",
    "x-overflow",
    "x-queue-mode",
    "x-queue-type",
    "x-single-active-consumer",
    "x-delivery-limit",
)

_RATE_KEYS = (
    "publish",
    "deliver",
    "deliver_get",
    "ack",
    "redeliver",
    "return_unroutable",
)

_HEALTH_CHECKS = (
    ("alarms", "/api/health/checks/alarms"),
    ("local_alarms", "/api/health/checks/local-alarms"),
    ("virtual_hosts", "/api/health/checks/virtual-hosts"),
    ("mirror_sync", "/api/health/checks/node-is-mirror-sync-critical"),
    ("amqp_listener", "/api/health/checks/port-listener/5672"),
    ("amqps_listener", "/api/health/checks/port-listener/5671"),
)


_UNEXPECTED = "RabbitMQ returned an unexpected response"


class RabbitMQAPIError(Exception):
    """Safe, user-facing management API failure. Never carries a response body."""

    def __init__(self, message: str, status_code: Optional[int] = None, kind: str = "unreachable"):
        super().__init__(message)
        self.status_code = status_code
        self.kind = kind


def _origin(scheme: str, host: str, port: Optional[int]) -> str:
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def _checked_port(parsed) -> Optional[int]:
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Management URL has an invalid port") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Management URL has an invalid port")
    return port


def _checked_host(parsed) -> str:
    # Basic auth belongs in the vault payload, not the URL we store and log.
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https") or parsed.username or parsed.password:
        raise ValueError("Management URL must be an http(s) URL without embedded credentials")
    if parsed.query or parsed.fragment or parsed.params:
        raise ValueError("Management URL must not include a query string")
    if parsed.path not in ("", "/"):
        raise ValueError("Management URL must not include a path")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host in _BLOCKED_HOSTS:
        raise ValueError("Management URL points at an address Aurora will not connect to")
    return host


def normalize_management_url(raw: str) -> str:
    """Return ``scheme://host[:port]`` or raise ValueError.

    Rejects credentials embedded in the URL, non-HTTP schemes, and any host
    that resolves to loopback, link-local (cloud metadata), or multicast.
    Private addresses stay allowed: brokers usually sit on an internal network.
    """
    value = (raw or "").strip()
    if not value or len(value) > 500:
        raise ValueError("Management URL is required")
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    host = _checked_host(parsed)
    port = _checked_port(parsed)
    _reject_host(host, port)
    return _origin((parsed.scheme or "").lower(), host, port)


def _reject_host(host: str, port: Optional[int]) -> None:
    # DNS is checked here and again by the HTTP client. Pinning the socket
    # while keeping TLS SNI is the same open window as the MCP connector.
    literal = _parse_ip(host)
    if literal is not None:
        _reject_ip(literal)
        return

    try:
        infos = socket.getaddrinfo(host, port or 0)
    except socket.gaierror as exc:
        raise ValueError("Could not resolve the management host") from exc

    addresses = []
    for info in infos:
        ip = _parse_ip(info[4][0])
        if ip is not None:
            addresses.append(ip)
    if not addresses:
        raise ValueError("Could not resolve the management host")
    # One loopback A record beside a public one is still a metadata/local pivot.
    for ip in addresses:
        _reject_ip(ip)


def _parse_ip(value: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _reject_ip(ip: ipaddress._BaseAddress) -> None:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    # Link-local covers 169.254.169.254. Loopback would hit Aurora itself.
    if (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    ):
        logger.warning("[RABBITMQ] Rejected management address class for a blocked target")
        raise ValueError("Management URL points at an address Aurora will not connect to")


def path_segment(value: str) -> str:
    """Encode a vhost or queue name so ``..`` cannot climb out of ``/api``."""
    return quote(value, safe="").replace(".", "%2E")


def _rates(stats: Any) -> Dict[str, Any]:
    if not isinstance(stats, dict):
        return {}
    slim: Dict[str, Any] = {}
    for key in _RATE_KEYS:
        details = stats.get(f"{key}_details")
        if key not in stats and not isinstance(details, dict):
            continue
        rate = details.get("rate") if isinstance(details, dict) else None
        slim[key] = {"count": stats.get(key), "rate": rate}
    return slim


def _short(value: Any, limit: int = 200) -> Any:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)[:limit]


def summarize_queue(queue: Dict[str, Any]) -> Dict[str, Any]:
    arguments = queue.get("arguments") if isinstance(queue.get("arguments"), dict) else {}
    return {
        "name": queue.get("name"),
        "vhost": queue.get("vhost"),
        "state": queue.get("state"),
        "type": queue.get("type"),
        "messages": queue.get("messages"),
        "messages_ready": queue.get("messages_ready"),
        "messages_unacknowledged": queue.get("messages_unacknowledged"),
        "consumers": queue.get("consumers"),
        "consumer_utilisation": queue.get("consumer_utilisation"),
        "memory": queue.get("memory"),
        "idle_since": queue.get("idle_since"),
        "arguments": {key: _short(arguments.get(key)) for key in _QUEUE_ARGUMENTS if key in arguments},
        "rates": _rates(queue.get("message_stats")),
    }


def summarize_node(node: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": node.get("name"),
        "running": node.get("running"),
        "type": node.get("type"),
        "uptime": node.get("uptime"),
        "mem_used": node.get("mem_used"),
        "mem_limit": node.get("mem_limit"),
        "mem_alarm": node.get("mem_alarm"),
        "disk_free": node.get("disk_free"),
        "disk_free_limit": node.get("disk_free_limit"),
        "disk_free_alarm": node.get("disk_free_alarm"),
        "fd_used": node.get("fd_used"),
        "fd_total": node.get("fd_total"),
        "sockets_used": node.get("sockets_used"),
        "sockets_total": node.get("sockets_total"),
        "proc_used": node.get("proc_used"),
        "proc_total": node.get("proc_total"),
    }


def summarize_overview(overview: Dict[str, Any]) -> Dict[str, Any]:
    listeners = []
    for listener in overview.get("listeners") or []:
        if not isinstance(listener, dict):
            continue
        listeners.append({
            "protocol": listener.get("protocol"),
            "port": listener.get("port"),
            "node": listener.get("node"),
        })
    totals = overview.get("queue_totals") if isinstance(overview.get("queue_totals"), dict) else {}
    return {
        "cluster_name": overview.get("cluster_name"),
        "rabbitmq_version": overview.get("rabbitmq_version"),
        "erlang_version": overview.get("erlang_version"),
        "node": overview.get("node"),
        "object_totals": overview.get("object_totals"),
        "queue_totals": {
            "messages": totals.get("messages"),
            "messages_ready": totals.get("messages_ready"),
            "messages_unacknowledged": totals.get("messages_unacknowledged"),
        },
        "rates": _rates(overview.get("message_stats")),
        "listeners": listeners,
    }


def summarize_exchange(exchange: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": exchange.get("name"),
        "vhost": exchange.get("vhost"),
        "type": exchange.get("type"),
        "durable": exchange.get("durable"),
        "auto_delete": exchange.get("auto_delete"),
        "internal": exchange.get("internal"),
        "rates": _rates(exchange.get("message_stats")),
    }


def summarize_connection(conn: Dict[str, Any]) -> Dict[str, Any]:
    props = conn.get("client_properties") if isinstance(conn.get("client_properties"), dict) else {}
    return {
        "name": conn.get("name"),
        "user": conn.get("user"),
        "vhost": conn.get("vhost"),
        "state": conn.get("state"),
        "channels": conn.get("channels"),
        "protocol": conn.get("protocol"),
        "peer_host": conn.get("peer_host"),
        "peer_port": conn.get("peer_port"),
        "timeout": conn.get("timeout"),
        "connected_at": conn.get("connected_at"),
        "client": _short(props.get("product") or props.get("connection_name")),
        "platform": _short(props.get("platform")),
    }


def summarize_consumer(consumer: Dict[str, Any]) -> Dict[str, Any]:
    queue = consumer.get("queue") if isinstance(consumer.get("queue"), dict) else {}
    channel = consumer.get("channel_details") if isinstance(consumer.get("channel_details"), dict) else {}
    return {
        "queue": queue.get("name"),
        "vhost": queue.get("vhost"),
        "consumer_tag": consumer.get("consumer_tag"),
        "ack_required": consumer.get("ack_required"),
        "prefetch_count": consumer.get("prefetch_count"),
        "active": consumer.get("active"),
        "activity_status": consumer.get("activity_status"),
        "connection_name": channel.get("connection_name"),
        "peer_host": channel.get("peer_host"),
    }


def summarize_binding(binding: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source": binding.get("source"),
        "destination": binding.get("destination"),
        "destination_type": binding.get("destination_type"),
        "routing_key": binding.get("routing_key"),
        "vhost": binding.get("vhost"),
    }


def normalize_tags(tags: Any) -> List[str]:
    if isinstance(tags, str):
        return [part.strip() for part in tags.split(",") if part.strip()]
    if isinstance(tags, list):
        return [str(part) for part in tags if part]
    return []


def as_page(data: Any, page: int, page_size: int) -> Dict[str, Any]:
    """Accept either the paginated object (3.8+) or a bare list from older plugins."""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return {
            "items": data["items"],
            "page": data.get("page", page),
            "page_count": data.get("page_count"),
            "total_count": data.get("total_count"),
            "filtered_count": data.get("filtered_count"),
        }
    if isinstance(data, list):
        start = (page - 1) * page_size
        total = len(data)
        page_count = (total + page_size - 1) // page_size if total else 0
        return {
            "items": data[start:start + page_size],
            "page": page,
            "page_count": page_count,
            "total_count": total,
            "filtered_count": total,
        }
    raise RabbitMQAPIError(_UNEXPECTED)


def _read_limited(response: requests.Response) -> bytes:
    """Copy at most MAX_RESPONSE_BYTES so a huge broker reply is not fully buffered."""
    body = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise RabbitMQAPIError("RabbitMQ management response was too large")
    return bytes(body)


class RabbitMQClient:
    """HTTP basic-auth client. Construct one per call and ``close()`` it."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        verify_tls: bool = True,
        timeout: int = TIMEOUT_SECONDS,
        default_vhost: str = "/",
        session: Optional[requests.Session] = None,
    ):
        self.url = normalize_management_url(url)
        self.username = username
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.default_vhost = default_vhost or "/"
        self._session = session or requests.Session()
        self._session.auth = (username, password)
        self._session.verify = verify_tls
        # Ignore HTTP_PROXY so a process-wide proxy cannot re-resolve the host.
        self._session.trust_env = False
        self._session.headers["Accept"] = "application/json"
        self._owns_session = session is None

    def close(self) -> None:
        if not self._owns_session:
            return
        try:
            self._session.close()
        except Exception:
            logger.debug("[RABBITMQ] Failed to close session", exc_info=True)

    def whoami(self) -> Dict[str, Any]:
        body = self._get_json("/api/whoami")
        if not isinstance(body, dict):
            raise RabbitMQAPIError(_UNEXPECTED)
        return {"name": body.get("name"), "tags": normalize_tags(body.get("tags"))}

    def overview(self) -> Dict[str, Any]:
        body = self._get_json("/api/overview")
        if not isinstance(body, dict):
            raise RabbitMQAPIError(_UNEXPECTED)
        return summarize_overview(body)

    def nodes(self) -> List[Dict[str, Any]]:
        body = self._get_json("/api/nodes")
        if not isinstance(body, list):
            raise RabbitMQAPIError(_UNEXPECTED)
        return [summarize_node(node) for node in body if isinstance(node, dict)]

    def queues(
        self,
        vhost: Optional[str] = None,
        name: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        path = "/api/queues"
        if vhost:
            path = f"/api/queues/{path_segment(vhost)}"
        params: Dict[str, Any] = {
            "pagination": "true",
            "page": page,
            "page_size": page_size,
            "sort": "messages",
            "sort_reverse": "true",
            "columns": ",".join((
                "name", "vhost", "state", "type", "messages", "messages_ready",
                "messages_unacknowledged", "consumers", "consumer_utilisation",
                "memory", "idle_since", "arguments", "message_stats",
            )),
        }
        if name:
            params["name"] = name
            params["use_regex"] = "false"
        page_body = self._collection(path, params, page, page_size)
        page_body["items"] = [
            summarize_queue(item) for item in page_body["items"] if isinstance(item, dict)
        ]
        return page_body

    def queue(self, vhost: str, name: str) -> Dict[str, Any]:
        body = self._get_json(f"/api/queues/{path_segment(vhost)}/{path_segment(name)}")
        if not isinstance(body, dict):
            raise RabbitMQAPIError(_UNEXPECTED)
        return summarize_queue(body)

    def exchanges(
        self,
        vhost: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        path = "/api/exchanges"
        if vhost:
            path = f"/api/exchanges/{path_segment(vhost)}"
        params = {
            "pagination": "true",
            "page": page,
            "page_size": page_size,
            "columns": "name,vhost,type,durable,auto_delete,internal,message_stats",
        }
        page_body = self._collection(path, params, page, page_size)
        page_body["items"] = [
            summarize_exchange(item) for item in page_body["items"] if isinstance(item, dict)
        ]
        return page_body

    def bindings(self, vhost: str, queue: str) -> List[Dict[str, Any]]:
        body = self._get_json(
            f"/api/queues/{path_segment(vhost)}/{path_segment(queue)}/bindings"
        )
        if not isinstance(body, list):
            raise RabbitMQAPIError(_UNEXPECTED)
        return [summarize_binding(item) for item in body if isinstance(item, dict)]

    def connections(
        self,
        vhost: Optional[str] = None,
        page: int = 1,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        path = f"/api/vhosts/{path_segment(vhost)}/connections" if vhost else "/api/connections"
        params = {"pagination": "true", "page": page, "page_size": page_size}
        page_body = self._collection(path, params, page, page_size)
        page_body["items"] = [
            summarize_connection(item) for item in page_body["items"] if isinstance(item, dict)
        ]
        return page_body

    def consumers(self, vhost: Optional[str] = None) -> List[Dict[str, Any]]:
        path = f"/api/consumers/{path_segment(vhost)}" if vhost else "/api/consumers"
        body = self._get_json(path)
        if not isinstance(body, list):
            raise RabbitMQAPIError(_UNEXPECTED)
        return [summarize_consumer(item) for item in body[:MAX_PAGE_SIZE] if isinstance(item, dict)]

    def health_checks(self) -> Dict[str, Any]:
        checks: Dict[str, Any] = {}
        for name, path in _HEALTH_CHECKS:
            response = self._request("GET", path, accept_error=True)
            # A missing check on older plugins is not a cluster failure.
            if response.status_code == 404:
                checks[name] = {"status": "unsupported"}
                continue
            # A proxy in front of the broker sometimes returns an HTML error page.
            try:
                body = self._json_body(response)
            except RabbitMQAPIError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            reason = body.get("reason")
            checks[name] = {
                "status": body.get("status") or ("ok" if response.ok else "failed"),
                "http_status": response.status_code,
                "reason": _short(reason, 500) if reason else None,
            }
        return checks

    def _collection(
        self,
        path: str,
        params: Dict[str, Any],
        page: int,
        page_size: int,
    ) -> Dict[str, Any]:
        try:
            data = self._get_json(path, params=params)
        except RabbitMQAPIError as exc:
            # Plugins older than 3.8 reject the pagination query parameters.
            if exc.status_code != 400 or "pagination" not in params:
                raise
            basic = {
                key: value
                for key, value in params.items()
                if key not in ("pagination", "page", "page_size", "sort", "sort_reverse")
            }
            data = self._get_json(path, params=basic)
        return as_page(data, page, page_size)

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        response = self._request("GET", path, params=params)
        return self._json_body(response)

    def _json_body(self, response: requests.Response) -> Any:
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RabbitMQAPIError("RabbitMQ returned a non-JSON response") from exc

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        accept_error: bool = False,
    ) -> requests.Response:
        # Redirects would let a public host 302 into a metadata address.
        # stream=True so an oversized body is dropped before it is fully buffered.
        response = None
        try:
            response = self._session.request(
                method,
                f"{self.url}{path}",
                params=params,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            body = _read_limited(response)
        except requests.exceptions.Timeout as exc:
            raise RabbitMQAPIError(
                "Timed out reaching the RabbitMQ management API", kind="timeout",
            ) from exc
        except requests.exceptions.SSLError as exc:
            raise RabbitMQAPIError(
                "TLS verification failed. Reconnect with TLS verification disabled if the broker uses a private CA.",
                kind="tls",
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise RabbitMQAPIError("Unable to reach the RabbitMQ management API", kind="unreachable") from exc
        except requests.RequestException as exc:
            raise RabbitMQAPIError("Unable to reach the RabbitMQ management API", kind="unreachable") from exc
        finally:
            if response is not None:
                response.close()

        response._content = body
        response._content_consumed = True
        # A 3xx with redirects disabled is a failed call, not a success.
        if 300 <= response.status_code < 400:
            raise RabbitMQAPIError("RabbitMQ management API returned a redirect", response.status_code)
        if accept_error or response.ok:
            return response
        # Do not log the path or the response: both are tainted by the credentialed call.
        if response.status_code == 401:
            logger.warning("[RABBITMQ] management request was rejected")
            raise RabbitMQAPIError("RabbitMQ rejected the username or password", 401, kind="auth")
        if response.status_code == 403:
            logger.warning("[RABBITMQ] management request was forbidden")
            raise RabbitMQAPIError(
                "This RabbitMQ user cannot read the management API. Grant the monitoring tag.",
                403,
                kind="forbidden",
            )
        if response.status_code == 404:
            logger.warning("[RABBITMQ] management resource was not found")
            raise RabbitMQAPIError("RabbitMQ resource was not found", 404, kind="not_found")
        logger.warning("[RABBITMQ] management request failed")
        raise RabbitMQAPIError("RabbitMQ management API request failed", response.status_code)
