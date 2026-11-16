"""Read-only RabbitMQ tools for the RCA agent.

Credentials come from Vault via ``list_rabbitmq_accounts``. Several brokers may be
connected; each tool takes ``account_id`` to pick one, and fans out to every
broker this user can access when it is omitted, up to ``MAX_FANOUT``.
None of these tools publish, purge, or dequeue messages.
"""

import contextvars
import functools
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Optional

from pydantic import BaseModel, Field

from connectors.rabbitmq_connector.client import MAX_PAGE_SIZE, RabbitMQAPIError, RabbitMQClient
from utils.rabbitmq_accounts import (
    RabbitMQAccessError,
    display_name,
    list_rabbitmq_accounts,
    resolve_targets,
)

logger = logging.getLogger(__name__)

MAX_OUTPUT_CHARS = 80_000
# Past this many brokers, a call with no account_id lists them instead of querying all.
MAX_FANOUT = 5
# Room kept for the broker list and the wrapper keys when the budget is split.
_FANOUT_OVERHEAD_CHARS = 2_000
_NOT_CONNECTED = "RabbitMQ not connected. Ask the user to connect RabbitMQ first."
_PAGE_HELP = "Page number, starting at 1."
_VISIBILITY_NOTE = (
    "No resources were visible to this user. Overview counts the whole cluster. "
    "If that total is nonzero, this account is missing read permission on those vhosts."
)


def _tool_targets(user_id: str, account_id: Optional[str]):
    """Granted brokers for this call, or a JSON error string."""
    accounts = list_rabbitmq_accounts(user_id)
    if not accounts:
        return None, json.dumps({"error": _NOT_CONNECTED})
    try:
        targets = resolve_targets(user_id, accounts, account_id)
    except RabbitMQAccessError as exc:
        body = {"error": exc.message}
        if exc.code:
            body["code"] = exc.code
        return None, json.dumps(body)
    if not targets:
        return None, json.dumps({"error": _NOT_CONNECTED})
    return targets, None


def is_rabbitmq_connected(user_id: str) -> bool:
    """True when this user can query at least one connected broker."""
    targets, error = _tool_targets(user_id, None)
    return error is None and bool(targets)


def _build_client(account: Dict[str, Any]) -> RabbitMQClient:
    return RabbitMQClient(
        url=account["url"],
        username=account["username"],
        password=account["password"],
        default_vhost=account.get("vhost") or "/",
    )


def _broker_fields(account: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "account_id": account.get("account_id"),
        "broker": display_name(account),
        "broker_url": account.get("url"),
    }


def _broker_listing(targets: list) -> list:
    return [
        {**_broker_fields(a), "default_vhost": a.get("vhost") or "/", "cluster_name": a.get("cluster_name")}
        for a in targets
    ]


def _pick_a_broker(targets: list) -> str:
    """Too many brokers to query at once: name them so the agent can choose one."""
    return _dump({
        "error": (
            f"{len(targets)} RabbitMQ brokers are connected. Pass account_id to pick one. "
            "Choose from the brokers below by name or environment, then retry."
        ),
        "code": "ACCOUNT_REQUIRED",
        "brokers": _broker_listing(targets),
    })


def _fit(item: Dict[str, Any], budget: int, account: Dict[str, Any]) -> Dict[str, Any]:
    """Cut one broker's result to its share, so a large broker cannot crowd out the rest."""
    text = json.dumps(item, default=str)
    if len(text) <= budget:
        return item
    return {
        **_broker_fields(account),
        "truncated": True,
        "message": "This broker's result exceeded its share of the output. Retry with its account_id, or narrow by vhost or name.",
        "preview": text[:max(budget - 500, 0)],
    }


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


_REGEX_CHARS = set("*+?^$[](){}|\\")
_REGEX_NOTE = "The name filter is a plain substring match, not a regex. Retry with a literal part of the queue name."


def _page_fields(page_body: Dict[str, Any], requested_size: Any, used_size: int) -> Dict[str, Any]:
    """Say when page_size was capped, so a short page is not read as the whole set."""
    try:
        requested = int(requested_size)
    except (TypeError, ValueError):
        return page_body
    if requested == used_size:
        return page_body
    noted = dict(page_body)
    noted["page_size"] = used_size
    noted["page_size_note"] = f"page_size {requested} is outside 1-{MAX_PAGE_SIZE}; used {used_size}."
    return noted


def _note_if_invisible(payload: Dict[str, Any], *, filtered: bool) -> Dict[str, Any]:
    # A user with no set_permissions gets an empty 200, not an error.
    if filtered or payload.get("total_count") != 0:
        return payload
    noted = dict(payload)
    noted["note"] = _VISIBILITY_NOTE
    return noted


def _dump(payload: Any) -> str:
    text = json.dumps(payload, default=str)
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    wrapper: Dict[str, Any] = {}
    # Keep the broker list first and parseable, so findings still name their source.
    if isinstance(payload, dict) and isinstance(payload.get("brokers"), list):
        wrapper["brokers"] = payload["brokers"]
    return json.dumps({
        **wrapper,
        "truncated": True,
        "message": "Response exceeded the output cap. Narrow with account_id, vhost, or name.",
        "preview": text[:MAX_OUTPUT_CHARS],
    })


def _run_one(fn: Callable[..., Any], account: Dict[str, Any], user_id: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """One broker's result, or an error dict. Never raises."""
    try:
        client = _build_client(account)
    except Exception:
        logger.warning("[RABBITMQ-TOOL] Stored management URL is not usable")
        return {"error": "RabbitMQ connection is not usable. Reconnect it in Aurora."}
    try:
        result = fn(client=client, user_id=user_id, **kwargs)
        return result if isinstance(result, dict) else {"result": result}
    except RabbitMQAPIError as exc:
        return {"error": str(exc)}
    except Exception:
        logger.exception("[RABBITMQ-TOOL] %s failed", fn.__name__)
        return {"error": "Unexpected error contacting RabbitMQ"}
    finally:
        client.close()


def _by_account(fn: Callable[..., Any], targets: list, user_id: str, kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Run one call per granted broker. A slow broker does not block the others.

    Each broker gets an equal share of the output cap.
    """
    budget = max((MAX_OUTPUT_CHARS - _FANOUT_OVERHEAD_CHARS) // len(targets), 1_000)

    def one(account):
        item = dict(_run_one(fn, account, user_id, kwargs))
        item.update(_broker_fields(account))
        return account["account_id"], _fit(item, budget, account)

    # A Context cannot be entered on two threads at once, so each worker copies it.
    def guarded(account):
        return contextvars.copy_context().run(one, account)

    with ThreadPoolExecutor(max_workers=min(4, len(targets))) as pool:
        return dict(pool.map(guarded, targets))


def _rabbitmq_tool(fn: Callable[..., Any]) -> Callable[..., str]:
    """Resolve the brokers, run the call on each, and return JSON instead of raising.

    One broker returns its result with the broker fields alongside. Several
    return ``results_by_account`` keyed by account id. Either way ``brokers``
    comes first, so a truncated excerpt still names where the data came from.
    More than ``MAX_FANOUT`` brokers with no account_id returns the broker list
    and asks for one, instead of querying them all.
    """

    @functools.wraps(fn)
    def wrapper(*, user_id: Optional[str] = None, account_id: Optional[str] = None, **kwargs: Any) -> str:
        if not user_id:
            return json.dumps({"error": "User context not available"})
        targets, error = _tool_targets(user_id, account_id)
        if error:
            return error
        if len(targets) > MAX_FANOUT:
            return _pick_a_broker(targets)
        brokers = [_broker_fields(a) for a in targets]
        if len(targets) == 1:
            body: Dict[str, Any] = {"brokers": brokers, **brokers[0]}
            body.update(_run_one(fn, targets[0], user_id, kwargs))
            return _dump(body)
        return _dump({"brokers": brokers, "results_by_account": _by_account(fn, targets, user_id, kwargs)})

    return wrapper


_ACCOUNT_HELP = (
    "Broker to query: its account id (management host:port), URL, or saved name. "
    f"Omit to query every broker this user can access, up to {MAX_FANOUT}. "
    "With more, account_id is required."
)


class _BrokerArgs(BaseModel):
    account_id: Optional[str] = Field(default=None, description=_ACCOUNT_HELP)


class RabbitMQListBrokersArgs(BaseModel):
    pass


def rabbitmq_list_brokers(*, user_id: Optional[str] = None, **kwargs: Any) -> str:
    """Connected brokers this user can query. Makes no call to RabbitMQ."""
    if not user_id:
        return json.dumps({"error": "User context not available"})
    targets, error = _tool_targets(user_id, None)
    if error:
        return error
    return _dump({"brokers": _broker_listing(targets), "count": len(targets)})


class RabbitMQOverviewArgs(_BrokerArgs):
    pass


class RabbitMQNodesArgs(_BrokerArgs):
    pass


class RabbitMQListQueuesArgs(_BrokerArgs):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to scan every vhost.")
    name: Optional[str] = Field(default=None, description="Queue name substring. Not a regex.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Queues per page (max 100). Sorted by depth, deepest first.")


class RabbitMQGetQueueArgs(_BrokerArgs):
    name: str = Field(description="Queue name.")
    vhost: Optional[str] = Field(default=None, description="Vhost. Defaults to the vhost saved at connect time.")


class RabbitMQListExchangesArgs(_BrokerArgs):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every vhost.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Exchanges per page (max 100).")


class RabbitMQListBindingsArgs(_BrokerArgs):
    queue: str = Field(description="Queue name whose bindings to list.")
    vhost: Optional[str] = Field(default=None, description="Vhost. Defaults to the vhost saved at connect time.")


class RabbitMQListConnectionsArgs(_BrokerArgs):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every connection.")
    page: int = Field(default=1, description=_PAGE_HELP)
    page_size: int = Field(default=50, description="Connections per page (max 100).")


class RabbitMQListConsumersArgs(_BrokerArgs):
    vhost: Optional[str] = Field(default=None, description="Limit to one vhost. Omit to list every consumer.")


class RabbitMQHealthArgs(_BrokerArgs):
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
    """Queues this user can read, deepest first. Zero consumers means nothing is subscribed."""
    requested_size = page_size
    page, page_size = _clamp_page(page, page_size)
    name_filter = name.strip() if isinstance(name, str) and name.strip() else None
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    page_body = client.queues(vhost=vhost_filter, name=name_filter, page=page, page_size=page_size)
    page_body = _page_fields(page_body, requested_size, page_size)
    if name_filter and not page_body.get("items") and _REGEX_CHARS & set(name_filter):
        page_body = {**page_body, "note": _REGEX_NOTE}
    return _note_if_invisible(page_body, filtered=name_filter is not None)


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
    """Exchanges this user can read, and their publish rates. The default exchange name is empty."""
    requested_size = page_size
    page, page_size = _clamp_page(page, page_size)
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    page_body = client.exchanges(vhost=vhost_filter, page=page, page_size=page_size)
    return _note_if_invisible(_page_fields(page_body, requested_size, page_size), filtered=False)


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
    # RabbitMQ answers with the implicit default-exchange binding for any name,
    # even a queue that does not exist. Confirm the queue first.
    try:
        client.queue(chosen, queue)
    except RabbitMQAPIError as exc:
        if exc.kind != "not_found":
            raise
        return {
            "vhost": chosen,
            "queue": queue,
            "error": f"Queue {queue!r} does not exist in vhost {chosen!r} on this broker, or this user cannot read it.",
        }
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
    requested_size = page_size
    page, page_size = _clamp_page(page, page_size)
    vhost_filter = vhost.strip() if isinstance(vhost, str) and vhost.strip() else None
    page_body = client.connections(vhost=vhost_filter, page=page, page_size=page_size)
    return _page_fields(page_body, requested_size, page_size)


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
