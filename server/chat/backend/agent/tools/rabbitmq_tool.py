"""Read-only RabbitMQ tools for the RCA agent.

Credentials come from Vault via ``get_token_data(user_id, "rabbitmq")``.
None of these tools publish, purge, or dequeue messages.
"""

import functools
import json
import logging
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, Field

from connectors.rabbitmq_connector.client import MAX_PAGE_SIZE, RabbitMQAPIError, RabbitMQClient
from utils.auth.token_management import get_token_data

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 80_000
_PAGE_HELP = "Page number, starting at 1."


def _credentials(user_id: str) -> Optional[Dict[str, Any]]:
    try:
        creds = get_token_data(user_id, "rabbitmq")
    except Exception:
        logger.exception("[RABBITMQ-TOOL] Failed to get credentials")
        return None
    if not creds or not creds.get("url") or not creds.get("username") or not creds.get("password"):
        return None
    return creds


def is_rabbitmq_connected(user_id: str) -> bool:
    return _credentials(user_id) is not None


def _build_client(user_id: str) -> Optional[RabbitMQClient]:
    creds = _credentials(user_id)
    if not creds:
        return None
    vhost = creds.get("vhost")
    try:
        return RabbitMQClient(
            url=creds["url"],
            username=creds["username"],
            password=creds["password"],
            verify_tls=bool(creds.get("verify_tls", True)),
            default_vhost=vhost if isinstance(vhost, str) and vhost else "/",
        )
    except Exception:
        logger.exception("[RABBITMQ-TOOL] Failed to build client")
        return None


def _clamp_page(page: int, page_size: int) -> tuple:
    try:
        page_num = int(page)
    except (TypeError, ValueError):
        page_num = 1
    try:
        size = int(page_size)
    except (TypeError, ValueError):
        size = 50
    return max(1, min(page_num, 100)), max(1, min(size, MAX_PAGE_SIZE))


def _dump(payload: Any) -> str:
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return json.dumps({
        "truncated": True,
        "message": "Response exceeded the output cap. Narrow with vhost or name.",
        "preview": text[:MAX_OUTPUT_CHARS],
    })


def _rabbitmq_tool(fn: Callable[..., Any]) -> Callable[..., str]:
    """Resolve the user, open a client, and return JSON instead of raising."""

    @functools.wraps(fn)
    def wrapper(*, user_id: Optional[str] = None, **kwargs: Any) -> str:
        if not user_id:
            return json.dumps({"error": "User context not available"})
        client = _build_client(user_id)
        if not client:
            return json.dumps({"error": "RabbitMQ not connected. Ask the user to connect RabbitMQ first."})
        try:
            return _dump(fn(client=client, user_id=user_id, **kwargs))
        except RabbitMQAPIError as exc:
            return json.dumps({"error": str(exc)})
        except Exception as exc:
            logger.exception("[RABBITMQ-TOOL] %s failed", fn.__name__)
            return json.dumps({"error": "Unexpected error contacting RabbitMQ"})
        finally:
            client.close()

    return wrapper


class RabbitMQOverviewArgs(BaseModel):
    pass


class RabbitMQNodesArgs(BaseModel):
    pass


class RabbitMQListQueuesArgs(BaseModel):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to scan every vhost.")
    name: Optional[str] = Field(default=None, description="Queue name substring. Not a regex.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Queues per page (max 100). Sorted by depth, deepest first.")


class RabbitMQGetQueueArgs(BaseModel):
    name: str = Field(description="Queue name.")
    vhost: Optional[str] = Field(default=None, description="Vhost. Defaults to the vhost saved at connect time.")


class RabbitMQListExchangesArgs(BaseModel):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every vhost.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Exchanges per page (max 100).")


class RabbitMQListBindingsArgs(BaseModel):
    queue: str = Field(description="Queue name whose bindings to list.")
    vhost: Optional[str] = Field(default=None, description="Vhost. Defaults to the vhost saved at connect time.")


class RabbitMQListConnectionsArgs(BaseModel):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every connection.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Connections per page (max 100).")


class RabbitMQListConsumersArgs(BaseModel):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every consumer.")


class RabbitMQHealthArgs(BaseModel):
    pass


@_rabbitmq_tool
def rabbitmq_get_overview(*, client: RabbitMQClient, **kwargs: Any) -> Dict[str, Any]:
    """Cluster version, queue totals, message rates, and listeners."""
    return client.overview()


@_rabbitmq_tool
def rabbitmq_list_nodes(*, client: RabbitMQClient, **kwargs: Any) -> Dict[str, Any]:
    """Node memory and disk alarms. A true mem_alarm or disk_free_alarm blocks publishers."""
    return {"nodes": client.nodes()}


@_rabbitmq_tool
def rabbitmq_list_queues(
    *,
    client: RabbitMQClient,
    vhost: Optional[str] = None,
    name: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Queues sorted by depth. Zero consumers means nothing is subscribed to that queue."""
    page, page_size = _clamp_page(page, page_size)
    name_filter = name.strip() if isinstance(name, str) and name.strip() else None
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    return client.queues(vhost=vhost_filter, name=name_filter, page=page, page_size=page_size)


@_rabbitmq_tool
def rabbitmq_get_queue(
    *,
    client: RabbitMQClient,
    name: str,
    vhost: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """One queue, including dead-letter arguments and rates."""
    chosen = vhost.strip() if isinstance(vhost, str) and vhost.strip() else client.default_vhost
    return client.queue(chosen, name)


@_rabbitmq_tool
def rabbitmq_list_exchanges(
    *,
    client: RabbitMQClient,
    vhost: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Exchanges and their publish rates. The default exchange name is empty."""
    page, page_size = _clamp_page(page, page_size)
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    return client.exchanges(vhost=vhost_filter, page=page, page_size=page_size)


@_rabbitmq_tool
def rabbitmq_list_bindings(
    *,
    client: RabbitMQClient,
    queue: str,
    vhost: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Bindings into a queue. An empty list still allows the default exchange to route by queue name."""
    chosen = vhost.strip() if isinstance(vhost, str) and vhost.strip() else client.default_vhost
    return {"vhost": chosen, "queue": queue, "bindings": client.bindings(chosen, queue)}


@_rabbitmq_tool
def rabbitmq_list_connections(
    *,
    client: RabbitMQClient,
    vhost: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Open AMQP connections: user, peer, channel count, and client library."""
    page, page_size = _clamp_page(page, page_size)
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    return client.connections(vhost=vhost_filter, page=page, page_size=page_size)


@_rabbitmq_tool
def rabbitmq_list_consumers(
    *,
    client: RabbitMQClient,
    vhost: Optional[str] = None,
    **kwargs: Any,
) -> Dict[str, Any]:
    """Consumers and whether they are active. Capped at 100."""
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    consumers = client.consumers(vhost=vhost_filter)
    return {"consumers": consumers, "count": len(consumers)}


@_rabbitmq_tool
def rabbitmq_health_checks(*, client: RabbitMQClient, **kwargs: Any) -> Dict[str, Any]:
    """Alarms, vhost health, mirror sync, and whether AMQP ports are listening.

    A failed amqp_listener is normal when the broker only exposes AMQPS (5671), and the reverse.
    """
    return {"checks": client.health_checks()}
