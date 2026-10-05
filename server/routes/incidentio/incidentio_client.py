"""incident.io REST API client, shared by the connector routes, the webhook
tasks and the RCA post-back notification service."""

import logging
from typing import Any, Dict, Optional

import requests

INCIDENTIO_API_BASE = "https://api.incident.io/v2"
INCIDENTIO_TIMEOUT = 15
# incident_alerts paging: 50 is the API maximum; 20 pages = 1000 links per lookup
_PAGE_SIZE = 50
_MAX_PAGES = 20

logger = logging.getLogger(__name__)


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

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """One API call; network and HTTP failures surface as IncidentioAPIError."""
        url = f"{INCIDENTIO_API_BASE}{path}"
        try:
            response = requests.request(
                method, url, headers=self.headers, timeout=INCIDENTIO_TIMEOUT, **kwargs
            )
            response.raise_for_status()
            return response
        except requests.exceptions.Timeout:
            raise IncidentioAPIError(IncidentioAPIError.TIMEOUT)
        except requests.exceptions.ConnectionError:
            raise IncidentioAPIError(IncidentioAPIError.UNREACHABLE)
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            logger.warning("[INCIDENTIO] HTTP %s from %s", status, path)
            if status == 401:
                raise IncidentioAPIError(IncidentioAPIError.INVALID_KEY, status)
            if status == 403:
                raise IncidentioAPIError(IncidentioAPIError.FORBIDDEN, status)
            raise IncidentioAPIError(IncidentioAPIError.API_ERROR, status)

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
