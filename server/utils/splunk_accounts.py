"""One Splunk instance per host:port, stored like an AWS account.

The account id is the scope a role or team is granted. Tokens stay in Vault;
``user_connections`` only records the id so connector access can tell instances apart.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

_DENIAL = (
    "You don't have access to splunk. "
    "Ask an admin to change this permission in org Settings, on the Access tab."
)


class SplunkAccessError(Exception):
    """The caller named an instance they cannot use, or one that is not connected."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code


def account_id_for_url(base_url: str) -> Optional[str]:
    """Host and port of a normalized Splunk URL. That is the stable account id."""
    netloc = urlparse(base_url or "").netloc.lower()
    return netloc or None


def accounts_from_creds(creds: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Instances in a Vault blob. A pre-multi-account blob is one instance."""
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
    base_url = item.get("base_url")
    api_token = item.get("api_token") or item.get("token") or item.get("access_token")
    if not isinstance(base_url, str) or not base_url or not isinstance(api_token, str) or not api_token:
        return None
    account_id = item.get("account_id") or account_id_for_url(base_url)
    if not isinstance(account_id, str) or not account_id:
        return None
    return {
        "account_id": account_id.lower(),
        "api_token": api_token,
        "base_url": base_url,
        "server_name": item.get("server_name"),
        "version": item.get("version"),
        "instance_type": item.get("instance_type"),
        "username": item.get("username"),
    }


def account_summary(account: Dict[str, Any]) -> Dict[str, Any]:
    """Credential-free view safe to return to the browser and the agent."""
    return {
        "accountId": account.get("account_id"),
        "baseUrl": account.get("base_url"),
        "serverName": account.get("server_name"),
        "version": account.get("version"),
        "instanceType": account.get("instance_type"),
        "username": account.get("username"),
    }


def upsert_account(existing: List[Dict[str, Any]], incoming: Dict[str, Any]) -> tuple:
    """Replace the instance with this host in place. A new host is appended.

    Replacing in place keeps the primary stable, so rotating one token does
    not silently make a different Splunk the default.
    """
    account_id = incoming["account_id"]
    for index, account in enumerate(existing):
        if account.get("account_id") == account_id:
            updated = list(existing)
            updated[index] = incoming
            return updated, True
    return [*existing, incoming], False


def find_account(accounts: List[Dict[str, Any]], selector: str) -> Optional[Dict[str, Any]]:
    """Match an account id or the instance URL the caller pasted."""
    keys = _selector_keys(selector)
    if not keys:
        return None
    for account in accounts:
        account_id = str(account.get("account_id") or "").lower()
        base_url = str(account.get("base_url") or "").lower().rstrip("/")
        if account_id in keys or base_url in keys:
            return account
    return None


def _selector_keys(selector: str) -> set:
    text = (selector or "").strip().lower().rstrip("/")
    if not text:
        return set()
    keys = {text}
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.netloc:
        keys.add(parsed.netloc.lower())
    return keys


def account_allowed(user_id: str, account_id: str) -> bool:
    """Whether this caller's role and team may use this Splunk.

    No connector-access module means nothing is limited yet. A failed check
    stays open so a missing table cannot take every Splunk offline.
    """
    try:
        from utils.auth.connector_access import may_use
    except ImportError:
        return True
    try:
        return bool(may_use(user_id, "splunk", account_id))
    except Exception:
        logger.warning("Splunk access check failed for user %s", hash_for_log(user_id))
        return True


def splunk_denial(user_id: str) -> str:
    """Same wording the Access tab's tool gate uses, once that module is present."""
    try:
        from utils.auth.connector_access import denial_text
    except ImportError:
        return _DENIAL
    try:
        return denial_text(user_id, "splunk")
    except Exception:
        logger.warning("Splunk denial text failed for user %s", hash_for_log(user_id))
        return _DENIAL


def resolve_targets(
    user_id: str, accounts: List[Dict[str, Any]], selector: Optional[str]
) -> List[Dict[str, Any]]:
    """Instances this caller may query.

    No selector returns every granted instance. A selector returns that one,
    or raises when it is unknown or not granted.
    """
    permitted = [a for a in accounts if account_allowed(user_id, a["account_id"])]
    if selector and str(selector).strip():
        match = find_account(accounts, str(selector))
        # Named instance exists, but this role or team was not granted it.
        if match is not None and not account_allowed(user_id, match["account_id"]):
            raise SplunkAccessError(splunk_denial(user_id), "CONNECTOR_DENIED")
        # Not connected — name only the instances this caller can use.
        if match is None:
            known = ", ".join(a["account_id"] for a in permitted) or "none"
            raise SplunkAccessError(f"Unknown Splunk instance. Connected: {known}")
        return [match]
    if not permitted:
        # Connected, but every instance is off limits for this role or team.
        if accounts:
            raise SplunkAccessError(splunk_denial(user_id), "CONNECTOR_DENIED")
        return []
    return permitted


def list_splunk_accounts(user_id: str) -> List[Dict[str, Any]]:
    from utils.auth.token_management import get_token_data

    try:
        creds = get_token_data(user_id, "splunk")
    except Exception:
        logger.exception("Failed to load Splunk accounts for user %s", hash_for_log(user_id))
        return []
    if not isinstance(creds, dict):
        return []
    return accounts_from_creds(creds)


def store_splunk_accounts(user_id: str, accounts: List[Dict[str, Any]]) -> None:
    """Save instances in Vault and one connection row per host:port.

    Those rows are the scopes the Access tab grants per team.
    """
    if not accounts:
        raise ValueError("no splunk accounts")
    from utils.auth.token_management import store_tokens_in_db

    records = [_normalize_account(a) for a in accounts]
    records = [a for a in records if a]
    if not records:
        raise ValueError("no splunk accounts")
    stored = dict(records[0])
    stored["accounts"] = records
    store_tokens_in_db(user_id, stored, "splunk")
    sync_splunk_connections(user_id, records)


def sync_splunk_connections(user_id: str, accounts: List[Dict[str, Any]]) -> None:
    """Make active connection rows match the instances currently stored."""
    from utils.db.connection_utils import (
        delete_connection_secret,
        get_all_user_connections,
        save_connection_metadata,
    )

    wanted_ids = [account.get("account_id") for account in accounts if account.get("account_id")]
    wanted = set(wanted_ids)
    existing = get_all_user_connections(user_id, "splunk", raise_on_error=True)
    existing_ids = {row.get("account_id") for row in existing}
    # Status polls this. Skip the write when the rows already match.
    if existing_ids == wanted:
        return
    for account_id in wanted_ids:
        saved = save_connection_metadata(
            user_id,
            "splunk",
            account_id,
            connection_method="api_token",
            status="active",
        )
        if not saved:
            raise RuntimeError("failed to save splunk connection")
    for row in existing:
        account_id = row.get("account_id")
        # Dropped instance — an inactive row no longer shows up as its own scope.
        if account_id and account_id not in wanted:
            if not delete_connection_secret(user_id, "splunk", account_id):
                raise RuntimeError("failed to retire splunk connection")


def retire_splunk_connections(user_id: str) -> None:
    """Mark every Splunk connection inactive. Used when the last instance is removed."""
    from utils.db.connection_utils import delete_connection_secret, get_all_user_connections

    existing = get_all_user_connections(user_id, "splunk", raise_on_error=True)
    for row in existing:
        account_id = row.get("account_id")
        if account_id and not delete_connection_secret(user_id, "splunk", account_id):
            raise RuntimeError("failed to retire splunk connection")
