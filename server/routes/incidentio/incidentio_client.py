"""incident.io REST API client, shared by the connector routes, the webhook
tasks and the RCA post-back notification service."""

import logging
from typing import Any, Dict, Optional

import requests

INCIDENTIO_API_BASE = "https://api.incident.io/v2"
INCIDENTIO_TIMEOUT = 15

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
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
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
        return self._request("GET", "/incidents", params={"page_size": page_size}).json()

    def get_incident(self, incident_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/incidents/{incident_id}").json()

    def get_incident_updates(self, incident_id: str) -> Dict[str, Any]:
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
        """Alert <-> incident links: by alert (incidents it is attached to; empty when
        it only escalated) or by incident (alerts attached to it)."""
        if not alert_id and not incident_id:
            raise ValueError("alert_id or incident_id is required")
        params: Dict[str, Any] = {"page_size": 50}
        if alert_id:
            params["alert_id"] = alert_id
        if incident_id:
            params["incident_id"] = incident_id
        return self._request("GET", "/incident_alerts", params=params).json()

    def list_alert_priorities(self) -> Dict[str, Any]:
        """Fetch the org's *alert priorities* as {name, rank} entries.

        Alerts are categorized by priority, which incident.io models as a
        ranked Catalog type named "AlertPriority" (entries like "Urgent",
        "In-hours"). Higher rank = more urgent. We first resolve the catalog
        type id (org-specific), then page through its entries.

        Returns {"severities": [{"name": str, "rank": int}, ...]} to match the
        shape the caller expects. Raises IncidentioAPIError on API failure.
        """
        # Find the AlertPriority catalog type — its id is org-specific. Orgs
        # with many catalog types paginate, so page through until we find it
        # rather than reading only the first page (else priority filtering is
        # silently disabled for orgs where AlertPriority isn't on page 1).
        type_id = None
        after = None
        for _ in range(20):  # hard cap: 20 pages × 250 = 5000 types
            params: Dict[str, Any] = {"page_size": 250}
            if after:
                params["after"] = after
            types = self._request("GET", "/catalog_types", params=params).json()
            batch = types.get("catalog_types", []) or []
            type_id = next(
                (t.get("id") for t in batch if t.get("type_name") == "AlertPriority"),
                None,
            )
            if type_id:
                break
            after = (types.get("pagination_meta") or {}).get("after")
            # incident.io returns `after` even on the final page, so stop when a
            # page came back short (fewer than page_size) or empty.
            if len(batch) < 250 or not after:
                break

        # Org has no alert-priority catalog configured — nothing to rank by.
        if not type_id:
            return {"severities": []}

        # Page through the catalog entries (page_size is required by the API).
        entries: list = []
        after = None
        for _ in range(20):  # hard cap: 20 pages × 250 = 5000 entries
            params: Dict[str, Any] = {"catalog_type_id": type_id, "page_size": 250}
            if after:
                params["after"] = after
            page = self._request("GET", "/catalog_entries", params=params).json()
            batch = page.get("catalog_entries", []) or []
            entries.extend(batch)
            after = (page.get("pagination_meta") or {}).get("after")
            # incident.io returns `after` even on the final page, so stop when a
            # page came back short (fewer than page_size) or empty.
            if len(batch) < 250 or not after:
                break

        severities = [
            {"name": e.get("name"), "rank": e.get("rank")}
            for e in entries
            if isinstance(e.get("name"), str) and isinstance(e.get("rank"), int)
        ]
        return {"severities": severities}
