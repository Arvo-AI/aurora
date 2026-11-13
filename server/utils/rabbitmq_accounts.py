"""One RabbitMQ broker per management host:port, stored like a Splunk instance.

The account id is the scope a role or team is granted. Passwords stay in Vault;
``user_connections`` only records the id so connector access can tell brokers apart.
A pre-multi-broker Vault blob is read as one broker, so existing connections keep working.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

PROVIDER = "rabbitmq"
MAX_NAME_LENGTH = 100

_DENIAL = (
    "You don't have access to rabbitmq. "
    "Ask an admin to change this permission in org Settings, on the Access tab."
)


class RabbitMQAccessError(Exception):
    """The caller named a broker they cannot use, or one that is not connected."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code


def account_id_for_url(url: str) -> Optional[str]:
    """Host and port of a normalized management URL. That is the stable account id."""
    netloc = urlparse(url or "").netloc.lower()
    return netloc or None


def clean_name(raw: Any) -> Optional[str]:
    """Optional display name. Raises ValueError on a name that cannot be shown or matched."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("Name must be a string")
    name = raw.strip()
    if not name:
        return None
    if len(name) > MAX_NAME_LENGTH or any(ord(ch) < 32 for ch in name):
        raise ValueError("Name is invalid")
    return name


def accounts_from_creds(creds: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Brokers in a Vault blob. A pre-multi-broker blob is one broker."""
    raw = creds.get("accounts")
    if isinstance(raw, list) and raw:
        accounts = []
        for item in raw:
            account = _normalize_account(item)
            if account:
                accounts.append(account)
        if accounts:
            return accounts
    account = _normalize_account(creds)
    return [account] if account else []


def _normalize_account(item: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None
    url = item.get("url")
    username = item.get("username")
    password = item.get("password")
    if not all(isinstance(v, str) and v for v in (url, username, password)):
        return None
    account_id = item.get("account_id") or account_id_for_url(url)
    if not isinstance(account_id, str) or not account_id:
        return None
    vhost = item.get("vhost")
    name = item.get("name")
    tags = item.get("tags")
    return {
        "account_id": account_id.lower(),
        "name": name if isinstance(name, str) and name else None,
        "url": url,
        "username": username,
        "password": password,
        "vhost": vhost if isinstance(vhost, str) and vhost else "/",
        "cluster_name": item.get("cluster_name"),
        "rabbitmq_version": item.get("rabbitmq_version"),
        "tags": tags if isinstance(tags, list) else [],
        "validated_at": item.get("validated_at"),
    }


def display_name(account: Dict[str, Any]) -> str:
    return account.get("name") or account.get("cluster_name") or account.get("account_id") or "RabbitMQ"


def account_summary(account: Dict[str, Any]) -> Dict[str, Any]:
    """Password-free view safe to return to the browser and the agent."""
    return {
        "accountId": account.get("account_id"),
        "name": account.get("name"),
        "displayName": display_name(account),
        "url": account.get("url"),
        "username": account.get("username"),
        "vhost": account.get("vhost") or "/",
        "clusterName": account.get("cluster_name"),
        "version": account.get("rabbitmq_version"),
        "tags": account.get("tags") or [],
        "validatedAt": account.get("validated_at"),
    }


def upsert_account(existing: List[Dict[str, Any]], incoming: Dict[str, Any]) -> tuple:
    """Replace the broker with this host in place. A new host is appended.

    Replacing in place keeps the primary stable, so rotating one password does
    not silently make a different broker the default.
    """
    account_id = incoming["account_id"]
    for index, account in enumerate(existing):
        if account.get("account_id") == account_id:
            updated = list(existing)
            merged = dict(incoming)
            # Reconnecting without a name keeps the one already chosen.
            if not merged.get("name"):
                merged["name"] = account.get("name")
            updated[index] = merged
            return updated, True
    return [*existing, incoming], False


def name_conflict(accounts: List[Dict[str, Any]], name: Optional[str], account_id: str) -> bool:
    """True when another broker already uses this name, which would make it ambiguous."""
    if not name:
        return False
    lowered = name.lower()
    return any(
        (a.get("name") or "").lower() == lowered and a.get("account_id") != account_id
        for a in accounts
    )


def find_account(accounts: List[Dict[str, Any]], selector: str) -> Optional[Dict[str, Any]]:
    """Match an account id, the management URL, or the name the broker was saved under."""
    keys = _selector_keys(selector)
    if not keys:
        return None
    for account in accounts:
        account_id = str(account.get("account_id") or "").lower()
        url = str(account.get("url") or "").lower().rstrip("/")
        if account_id in keys or url in keys:
            return account
    for account in accounts:
        name = str(account.get("name") or "").lower()
        if name and name in keys:
            return account
    return None


def _selector_keys(selector: str) -> set:
    text = (selector or "").strip().lower().rstrip("/")
    if not text:
        return set()
    keys = {text}
    try:
        parsed = urlparse(text if "://" in text else f"https://{text}")
        if parsed.netloc:
            keys.add(parsed.netloc.lower())
    except ValueError:
        # A malformed host (bad port, stray bracket) still matches by name or exact id.
        logger.debug("RabbitMQ selector is not a URL; matching it as written")
    return keys


def account_allowed(user_id: str, account_id: str) -> bool:
    """Whether this caller's role and team may use this broker."""
    try:
        from utils.auth.connector_access import may_use
    # Access control is not deployed yet — nothing is limited.
    except ImportError:
        return True
    try:
        return bool(may_use(user_id, PROVIDER, account_id))
    # A failed lookup must not open a broker this team was never granted.
    except Exception:
        logger.warning("RabbitMQ access check failed for user %s", hash_for_log(user_id))
        return False


def rabbitmq_denial(user_id: str) -> str:
    """Same wording the Access tab's tool gate uses."""
    try:
        from utils.auth.connector_access import denial_text
    except ImportError:
        return _DENIAL
    try:
        return denial_text(user_id, PROVIDER)
    except Exception:
        logger.warning("RabbitMQ denial text failed for user %s", hash_for_log(user_id))
        return _DENIAL


def resolve_targets(
    user_id: str, accounts: List[Dict[str, Any]], selector: Optional[str]
) -> List[Dict[str, Any]]:
    """Brokers this caller may query.

    No selector returns every granted broker. A selector returns that one,
    or raises when it is unknown or not granted.
    """
    permitted = [a for a in accounts if account_allowed(user_id, a["account_id"])]
    if selector and str(selector).strip():
        match = find_account(accounts, str(selector))
        # Named broker exists, but this role or team was not granted it.
        if match is not None and not account_allowed(user_id, match["account_id"]):
            raise RabbitMQAccessError(rabbitmq_denial(user_id), "CONNECTOR_DENIED")
        # Not connected — name only the brokers this caller can use.
        if match is None:
            known = ", ".join(a["account_id"] for a in permitted) or "none"
            raise RabbitMQAccessError(f"Unknown RabbitMQ broker. Connected: {known}")
        return [match]
    if not permitted:
        # Connected, but every broker is off limits for this role or team.
        if accounts:
            raise RabbitMQAccessError(rabbitmq_denial(user_id), "CONNECTOR_DENIED")
        return []
    return permitted


def list_rabbitmq_accounts(user_id: str, *, raise_on_error: bool = False) -> List[Dict[str, Any]]:
    from utils.auth.token_management import get_token_data

    try:
        creds = get_token_data(user_id, PROVIDER, raise_on_error=raise_on_error)
    except Exception:
        # Callers that write the blob back cannot treat a Vault error as "none connected".
        if raise_on_error:
            raise
        logger.exception("Failed to load RabbitMQ brokers for user %s", hash_for_log(user_id))
        return []
    if not isinstance(creds, dict):
        return []
    return accounts_from_creds(creds)


def store_rabbitmq_accounts(user_id: str, accounts: List[Dict[str, Any]]) -> None:
    """Save brokers in Vault and one connection row per host:port.

    The first broker is also kept at the top level, so a reader that predates
    the ``accounts`` list still finds a working connection.
    """
    if not accounts:
        raise ValueError("no rabbitmq accounts")
    from utils.auth.token_management import store_tokens_in_db

    records = [_normalize_account(a) for a in accounts]
    records = [a for a in records if a]
    if not records:
        raise ValueError("no rabbitmq accounts")
    stored = dict(records[0])
    stored["accounts"] = records
    store_tokens_in_db(user_id, stored, PROVIDER)
    sync_rabbitmq_connections(user_id, records)


def sync_rabbitmq_connections(user_id: str, accounts: List[Dict[str, Any]]) -> None:
    """Make active connection rows match the brokers currently stored."""
    from utils.db.connection_utils import (
        delete_connection_secret,
        get_all_user_connections,
        save_connection_metadata,
    )

    wanted_ids = [account.get("account_id") for account in accounts if account.get("account_id")]
    wanted = set(wanted_ids)
    existing = get_all_user_connections(user_id, PROVIDER, raise_on_error=True)
    existing_ids = {row.get("account_id") for row in existing}
    # Status polls this. Skip the write when the rows already match.
    if existing_ids == wanted:
        return
    for account_id in wanted_ids:
        saved = save_connection_metadata(
            user_id,
            PROVIDER,
            account_id,
            connection_method="basic_auth",
            status="active",
        )
        if not saved:
            raise RuntimeError("failed to save rabbitmq connection")
    for row in existing:
        account_id = row.get("account_id")
        # Dropped broker — an inactive row no longer shows up as its own scope.
        if account_id and account_id not in wanted:
            if not delete_connection_secret(user_id, PROVIDER, account_id):
                raise RuntimeError("failed to retire rabbitmq connection")


def retire_rabbitmq_connections(user_id: str) -> None:
    """Mark every RabbitMQ connection inactive. Used when the last broker is removed."""
    from utils.db.connection_utils import delete_connection_secret, get_all_user_connections

    existing = get_all_user_connections(user_id, PROVIDER, raise_on_error=True)
    for row in existing:
        account_id = row.get("account_id")
        if account_id and not delete_connection_secret(user_id, PROVIDER, account_id):
            raise RuntimeError("failed to retire rabbitmq connection")
