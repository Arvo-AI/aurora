"""incident.io REST API client, shared by the connector routes, the webhook
tasks and the RCA post-back notification service."""

import logging
from typing import Any, Dict, Optional

import requests

INCIDENTIO_API_HOST = "https://api.incident.io"
# incident.io versions per service, not per API: most of what we call is v2, but
# alert notes only exist on v1, so the version is part of the request not the base.
INCIDENTIO_DEFAULT_API_VERSION = "v2"
INCIDENTIO_TIMEOUT = 15
# Settings re-check identity whenever scopes are incomplete (that result is not
# cached), so a hung call must not hold the page for the normal API timeout.
_POSTBACK_IDENTITY_TIMEOUT = 3
# incident_alerts paging: 50 is the API maximum; 20 pages = 1000 links per lookup
_PAGE_SIZE = 50
_MAX_PAGES = 20

logger = logging.getLogger(__name__)

# Account-level "Edit incidents". A message-only incident update is this role,
# not a separate "create updates" permission.
INCIDENT_WRITE_ROLE = "incident_editor"
# "Create and manage on call ressources" (on_call_editor) bundles alerts.edit.
# Account-level covers every alert. The same role in team_roles covers only the key's teams.
ALERT_WRITE_ROLE = "on_call_editor"


def classify_postback_roles(roles, team_roles=None) -> Dict[str, bool]:
    """Which RCA destinations this key's roles can write, from GET /v1/identity.

    team_roles apply only to resources owned by the key's teams, so a team-only
    on_call_editor is not full alert write: another team's alert still 403s.
    """
    account = set(roles or [])
    teams = set(team_roles or [])
    # Account role already covers every team; team_roles don't narrow it.
    alerts = ALERT_WRITE_ROLE in account
    return {
        "incidents": INCIDENT_WRITE_ROLE in account,
        "alerts": alerts,
        "alertsScoped": (not alerts) and ALERT_WRITE_ROLE in teams,
    }


def postback_can_enable(access: Dict[str, Any]) -> bool:
    """Whether turning post-back on would reach at least one destination."""
    # Identity was unreachable — don't block the toggle on a blip.
    if not access.get("checked"):
        return True
    # Team-scoped alert write still reaches that key's teams, so the toggle can turn on.
    return bool(access.get("incidents") or access.get("alerts") or access.get("alertsScoped"))


class IncidentioAPIError(Exception):
    """Error codes avoid leaking HTTP response bodies through str(exc).

    status_code is the HTTP status incident.io answered with, or None when no
    response arrived (timeout, connection reset): the request's outcome is
    then unknown to the caller.
    """
    INVALID_KEY = "invalid_key"
    FORBIDDEN = "forbidden"
    TIMEOUT = "timeout"
    UNREACHABLE = "unreachable"
    API_ERROR = "api_error"

    _USER_MESSAGES = {
        INVALID_KEY: "Invalid API key",
        FORBIDDEN: "API key lacks required permissions",
        TIMEOUT: "Connection to incident.io timed out",
        UNREACHABLE: "Unable to reach incident.io API",
        API_ERROR: "Failed to validate API key with incident.io",
    }

    def __init__(self, code: str, status_code: Optional[int] = None):
        self.code = code
        self.status_code = status_code
        super().__init__(self._USER_MESSAGES.get(code, self._USER_MESSAGES[self.API_ERROR]))


class IncidentioClient:
    """Client for the incident.io REST API."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    @property
    def headers(self) -> Dict[str, str]:
        """Bearer auth + JSON headers for every request."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        api_version: str = INCIDENTIO_DEFAULT_API_VERSION,
        timeout: float = INCIDENTIO_TIMEOUT,
        **kwargs,
    ) -> requests.Response:
        """One API call; network and HTTP failures surface as IncidentioAPIError."""
        url = f"{INCIDENTIO_API_HOST}/{api_version}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=timeout, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout:
            raise IncidentioAPIError(IncidentioAPIError.TIMEOUT)
        except requests.exceptions.ConnectionError:
            raise IncidentioAPIError(IncidentioAPIError.UNREACHABLE)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            logger.warning("[INCIDENTIO] HTTP %s from /%s%s", status, api_version, path)
            if status == 401:
                raise IncidentioAPIError(IncidentioAPIError.INVALID_KEY, status)
            if status == 403:
                raise IncidentioAPIError(IncidentioAPIError.FORBIDDEN, status)
            raise IncidentioAPIError(IncidentioAPIError.API_ERROR, status)

    def get_identity(self, *, timeout: float = INCIDENTIO_TIMEOUT) -> Dict[str, Any]:
        """Roles on this API key. Any valid key can call it, and it writes nothing."""
        return self._request("GET", "/identity", api_version="v1", timeout=timeout).json()

    def read_postback_access(self) -> Dict[str, Any]:
        """Whether this key can post incident updates and alert notes.

        ``checked`` is false when identity could not be read, so the caller
        does not treat a timeout as a missing permission.
        """
        unknown = {"checked": False, "incidents": False, "alerts": False, "alertsScoped": False}
        try:
            payload = self.get_identity(timeout=_POSTBACK_IDENTITY_TIMEOUT) or {}
        except IncidentioAPIError:
            logger.warning("[INCIDENTIO] Could not read API key roles for the post-back check")
            return unknown
        identity = payload.get("identity") or {}
        caps = classify_postback_roles(identity.get("roles"), identity.get("team_roles"))
        return {"checked": True, **caps}

    def list_incidents(self, page_size: int = 5) -> Dict[str, Any]:
        """First page of the org's incidents (used to validate a key on connect)."""
        return self._request("GET", "/incidents", params={"page_size": page_size}).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        """One incident by id."""
        return self._request("GET", f"/incidents/{incident_id}").json()

    def get_incident_updates(self, incident_id: str) -> Dict[str, Any]:
        """Timeline updates of one incident."""
        return self._request("GET", "/incident_updates", params={"incident_id": incident_id}).json()

    def post_incident_update(
        self, incident_id: str, message: str, idempotency_key: Optional[str] = None
    ) -> Dict[str, Any]:
        """Post a message to the incident's timeline; incident.io notifies the
        incident's channel and followers through its own update flow. The
        idempotency key makes a repeated POST a no-op on incident.io's side."""
        body: Dict[str, Any] = {"incident_id": incident_id, "message": message}
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        return self._request("POST", "/incident_updates", json=body).json()

    def get_alert(self, alert_id: str) -> Dict[str, Any]:
        """One alert, including the groups it belongs to."""
        return self._request("GET", f"/alerts/{alert_id}").json()

    def post_alert_note(
        self,
        content: str,
        *,
        alert_id: Optional[str] = None,
        alert_group_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Add a markdown note to one alert, or to an alert group's shared thread.

        Provide exactly one of alert_id or alert_group_id. Grouped alerts share
        one pulse thread, so a group note is posted once instead of per alert.
        Alert notes are v1 and have no idempotency key.
        """
        # The API rejects a body that sets both ids, or neither.
        if bool(alert_id) == bool(alert_group_id):
            raise ValueError("provide exactly one of alert_id or alert_group_id")
        body: Dict[str, Any] = {"content": content}
        if alert_group_id:
            body["alert_group_id"] = alert_group_id
        else:
            body["alert_id"] = alert_id
        return self._request("POST", "/alert_notes", api_version="v1", json=body).json()

    def get_alert_note(self, note_id: str) -> Dict[str, Any]:
        """One alert note, so a later finding can be appended instead of duplicated."""
        return self._request("GET", f"/alert_notes/{note_id}", api_version="v1").json()

    def update_alert_note(self, note_id: str, content: str) -> Dict[str, Any]:
        """Replace a note's markdown. incident.io has no patch; the body is the whole note."""
        return self._request(
            "PUT", f"/alert_notes/{note_id}", api_version="v1", json={"content": content},
        ).json()

    def list_incident_alerts(
        self, alert_id: Optional[str] = None, *, incident_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """All alert <-> incident links, by alert (incidents it is attached to; empty
        when it only escalated) or by incident (alerts attached to it).

        Pages through the API (50 per page, the maximum) so an incident with many
        attached alerts still returns every link; capped at _MAX_PAGES pages.
        """
        if not alert_id and not incident_id:
            raise ValueError("alert_id or incident_id is required")
        base: Dict[str, Any] = {"page_size": _PAGE_SIZE}
        if alert_id:
            base["alert_id"] = alert_id
        if incident_id:
            base["incident_id"] = incident_id
        links: list = []
        after = None
        for _ in range(_MAX_PAGES):
            params = dict(base, after=after) if after else dict(base)
            page = self._request("GET", "/incident_alerts", params=params).json() or {}
            batch = page.get("incident_alerts") or []
            links.extend(batch)
            after = (page.get("pagination_meta") or {}).get("after")
            # incident.io returns `after` even on the final page: stop on a short page
            if len(batch) < _PAGE_SIZE or not after:
                break
        return {"incident_alerts": links}

    def _paged(self, path: str, key: str, params: Dict[str, Any]) -> list:
        """All items of a paginated catalog endpoint (page_size 250, capped at 20 pages)."""
        items: list = []
        after = None
        for _ in range(20):  # hard cap: 20 pages x 250 = 5000 items
            page_params = dict(params, page_size=250)
            if after:
                page_params["after"] = after
            page = self._request("GET", path, params=page_params).json()
            batch = page.get(key, []) or []
            items.extend(batch)
            after = (page.get("pagination_meta") or {}).get("after")
            # incident.io returns `after` even on the final page, so stop when a
            # page came back short (fewer than page_size) or empty.
            if len(batch) < 250 or not after:
                break
        return items

    def _alert_priority_type_id(self) -> Optional[str]:
        """Id of the org-specific "AlertPriority" catalog type, paging until found."""
        after = None
        for _ in range(20):
            params: Dict[str, Any] = {"page_size": 250}
            if after:
                params["after"] = after
            types = self._request("GET", "/catalog_types", params=params).json()
            batch = types.get("catalog_types", []) or []
            type_id = next((t.get("id") for t in batch if t.get("type_name") == "AlertPriority"), None)
            if type_id:
                return type_id
            after = (types.get("pagination_meta") or {}).get("after")
            if len(batch) < 250 or not after:
                return None
        return None

    def list_alert_priorities(self) -> Dict[str, Any]:
        """Fetch the org's *alert priorities* as {name, rank} entries.

        Alerts are categorized by priority, which incident.io models as a
        ranked Catalog type named "AlertPriority" (entries like "Urgent",
        "In-hours"). Higher rank = more urgent. We first resolve the catalog
        type id (org-specific), then page through its entries.

        Returns {"severities": [{"name": str, "rank": int}, ...]} to match the
        shape the caller expects. Raises IncidentioAPIError on API failure.
        """
        type_id = self._alert_priority_type_id()
        if not type_id:
            # Org has no alert-priority catalog configured: nothing to rank by.
            return {"severities": []}
        entries = self._paged("/catalog_entries", "catalog_entries", {"catalog_type_id": type_id})
        severities = [
            {"name": e.get("name"), "rank": e.get("rank")}
            for e in entries
            if isinstance(e.get("name"), str) and isinstance(e.get("rank"), int)
        ]
        return {"severities": severities}
