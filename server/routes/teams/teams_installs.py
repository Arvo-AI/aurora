"""Which Teams teams have the Aurora bot installed — the source of truth for channel sync.

Pushed by conversationUpdate events, confirmed by inbound messages, and
reconciled by probing Bot Connector (it only lists channels for a bot in the team).
"""

from __future__ import annotations

import json
import logging
from typing import Iterable, Optional

from connectors.teams_connector.bot_client import is_allowed_service_url, team_has_bot
from utils.auth.stateless_auth import get_org_id_for_user, get_org_preference, store_org_preference
from utils.cache.redis_client import get_redis_client
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_INSTALLED_PREF = "teams_bot_installed_team_ids"
_SERVICE_URL_PREF = "teams_bot_service_url"

# Installed teams are re-verified now and then; a "no" is rechecked sooner so a new install shows up fast
POSITIVE_TTL_SECONDS = 6 * 3600
NEGATIVE_TTL_SECONDS = 600
MAX_PROBES_PER_SYNC = 25


def _cache_key(org_id: str, team_id: str) -> str:
    return f"teams:bot_probe:{org_id}:{team_id}"


def _cache_get(org_id: str, team_id: str) -> Optional[bool]:
    """True/False from the probe cache; None when absent or Redis is unavailable."""
    try:
        redis = get_redis_client()
        raw = redis.get(_cache_key(org_id, team_id)) if redis else None
    except Exception:
        return None
    if raw is None:
        return None
    return raw in ("1", b"1")


def _cache_set(org_id: str, team_id: str, installed: bool) -> None:
    try:
        redis = get_redis_client()
        if redis:
            ttl = POSITIVE_TTL_SECONDS if installed else NEGATIVE_TTL_SECONDS
            redis.setex(_cache_key(org_id, team_id), ttl, "1" if installed else "0")
    except Exception:
        logger.debug("[TeamsInstalls] cache write failed", exc_info=True)


def _redis_available() -> bool:
    try:
        return get_redis_client() is not None
    except Exception:
        return False


def _read_ids(org_id: str) -> set[str]:
    raw = get_org_preference(org_id, _INSTALLED_PREF, default="[]")
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return {t for t in parsed if isinstance(t, str) and t}
    except (TypeError, json.JSONDecodeError):
        return set()


def _write_ids(org_id: str, ids: Iterable[str]) -> None:
    store_org_preference(org_id, _INSTALLED_PREF, json.dumps(sorted(ids)))


def get_installed_team_ids(user_id: str) -> set[str]:
    org_id = get_org_id_for_user(user_id)
    return _read_ids(org_id) if org_id else set()


def mark_team_installed(user_id: str, team_id: str) -> None:
    org_id = get_org_id_for_user(user_id)
    if not org_id or not team_id:
        return
    ids = _read_ids(org_id)
    # Already recorded — skip the write
    if team_id not in ids:
        _write_ids(org_id, ids | {team_id})
    _cache_set(org_id, team_id, True)


def mark_team_uninstalled(user_id: str, team_id: str) -> None:
    org_id = get_org_id_for_user(user_id)
    if not org_id or not team_id:
        return
    ids = _read_ids(org_id)
    if team_id in ids:
        _write_ids(org_id, ids - {team_id})
    _cache_set(org_id, team_id, False)


def remember_service_url(user_id: str, service_url: Optional[str]) -> None:
    """Keep the org's regional Bot Connector host for later probes."""
    if not is_allowed_service_url(service_url):
        return
    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return
    # Unchanged — skip the write
    if get_org_preference(org_id, _SERVICE_URL_PREF, default="") != service_url:
        store_org_preference(org_id, _SERVICE_URL_PREF, service_url)


def _service_url(org_id: str) -> Optional[str]:
    url = get_org_preference(org_id, _SERVICE_URL_PREF, default="")
    return url if isinstance(url, str) and url else None


def _probe(org_id: str, client, team_id: str) -> Optional[bool]:
    """Ask Bot Connector whether the bot is in this team; None if it couldn't be determined."""
    try:
        thread_id = (client.get_primary_channel(team_id) or {}).get("id")
        if not thread_id:
            return None
        return team_has_bot(_service_url(org_id), thread_id)
    except Exception as e:
        logger.warning("[TeamsInstalls] probe failed: %s", sanitize(e))
        return None


def _classify_team(
    org_id: str, client, team_id: str, persisted: set[str], *, redis_up: bool, may_probe: bool,
) -> tuple[Optional[bool], bool]:
    """(True installed | False absent | None unknown, whether a probe was spent)."""
    cached = _cache_get(org_id, team_id)
    # Verified recently — trust the cached answer
    if cached is not None:
        return cached, False
    known = team_id in persisted
    # Without a cache every sync would re-probe known installs, so trust the stored list
    if not redis_up and known:
        return True, False
    # Probe budget spent — leave the rest for the next sync
    if not may_probe:
        return (True if known else None), False

    found = _probe(org_id, client, team_id)
    if found is True:
        persisted.add(team_id)
        _cache_set(org_id, team_id, True)
    elif found is False:
        persisted.discard(team_id)
        _cache_set(org_id, team_id, False)
    # Couldn't tell — keep what we knew rather than flip state on an outage
    elif known:
        return True, True
    return found, True


def resolve_installed_teams(user_id: str, client, team_ids: Iterable[str]) -> tuple[set[str], set[str]]:
    """Split teams into (installed, unknown); anything else is confirmed not installed."""
    org_id = get_org_id_for_user(user_id)
    teams = list(dict.fromkeys(t for t in team_ids if t))
    if not org_id or not teams:
        return set(), set(teams)

    persisted = _read_ids(org_id)
    original = set(persisted)
    redis_up = _redis_available()
    installed: set[str] = set()
    unknown: set[str] = set()
    probes = 0

    for team_id in teams:
        result, probed = _classify_team(
            org_id, client, team_id, persisted,
            redis_up=redis_up, may_probe=probes < MAX_PROBES_PER_SYNC,
        )
        probes += probed
        if result is True:
            installed.add(team_id)
        elif result is None:
            unknown.add(team_id)

    if persisted != original:
        _write_ids(org_id, persisted)
    return installed, unknown
