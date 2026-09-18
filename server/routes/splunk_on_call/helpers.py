"""Splunk On-Call (VictorOps) API client."""

from __future__ import annotations

import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.victorops.com/api-public/v1"
REQUEST_TIMEOUT = 20


class SplunkOnCallAPIError(Exception):
    """Splunk On-Call API error."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class SplunkOnCallClient:
    def __init__(self, api_id: str, api_key: str):
        self.api_id = api_id
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "X-VO-Api-Id": self.api_id,
            "X-VO-Api-Key": self.api_key,
        }

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        try:
            response = requests.request(
                method,
                f"{BASE_URL}{path}",
                headers=self.headers,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        except requests.RequestException as exc:
            logger.error("[SPLUNK_ON_CALL] %s %s network error: %s", method, path, exc)
            raise SplunkOnCallAPIError("Unable to reach Splunk On-Call") from exc

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            logger.error(
                "[SPLUNK_ON_CALL] %s %s failed with status %s",
                method,
                path,
                response.status_code,
            )
            raise SplunkOnCallAPIError(
                response.text or str(exc),
                status_code=response.status_code,
            ) from exc
        return response

    def list_incidents(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/incidents").json()
        incidents = payload.get("incidents", []) if isinstance(payload, dict) else []
        return [item for item in incidents if isinstance(item, dict)]

    def validate_credentials(self) -> int:
        return len(self.list_incidents())
