"""
New Relic NerdGraph API client.

Provides a GraphQL client for authenticating and validating credentials
against New Relic's NerdGraph API. Query/RCA methods live on the RCA branch.
"""

import logging
import time
from typing import Any, Dict, List, Optional

import re
import requests

logger = logging.getLogger(__name__)

NERDGRAPH_US = "https://api.newrelic.com/graphql"
NERDGRAPH_EU = "https://api.eu.newrelic.com/graphql"

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 2
RETRY_BACKOFF = 1.0


class NewRelicAPIError(Exception):
    """Raised when NerdGraph returns an error or the request fails."""

    def __init__(self, message: str, status_code: Optional[int] = None, errors: Optional[List[Dict]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


class NewRelicClient:
    """GraphQL client for New Relic NerdGraph API.

    Handles authentication, region routing (US/EU), retries with exponential
    backoff, and provides typed methods for common NerdGraph operations.
    """

    def __init__(
        self,
        api_key: str,
        account_id: str,
        region: str = "us",
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not api_key:
            raise ValueError("New Relic API key is required")
        if not account_id:
            raise ValueError("New Relic account ID is required")

        self.api_key = api_key
        self.account_id = str(account_id).strip()
        if not re.match(r"^\d+$", self.account_id):
            raise ValueError("Account ID must be numeric")
        self.region = region.lower().strip()
        self.timeout = timeout
        self.endpoint = NERDGRAPH_EU if self.region == "eu" else NERDGRAPH_US

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "API-Key": self.api_key,
        }

    def _execute_graphql(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a GraphQL query against NerdGraph with retry logic."""
        payload: Dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        last_error: Optional[Exception] = None

        for attempt in range(MAX_RETRIES + 1):
            try:
                response = requests.post(
                    self.endpoint,
                    json=payload,
                    headers=self.headers,
                    timeout=self.timeout,
                )

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 5))
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "[NEWRELIC] Rate limited (429), retrying in %ds (attempt %d/%d)",
                            retry_after, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(min(retry_after, 30))
                        continue
                    raise NewRelicAPIError(
                        "NerdGraph rate limit exceeded",
                        status_code=429,
                    )

                if response.status_code == 401:
                    raise NewRelicAPIError(
                        "Invalid New Relic API key",
                        status_code=401,
                    )

                if response.status_code == 403:
                    raise NewRelicAPIError(
                        "API key lacks required permissions",
                        status_code=403,
                    )

                response.raise_for_status()

                data = response.json()

                if "errors" in data and data["errors"]:
                    error_messages = [e.get("message", "Unknown error") for e in data["errors"]]
                    combined = "; ".join(error_messages)
                    raise NewRelicAPIError(
                        f"NerdGraph query errors: {combined}",
                        errors=data["errors"],
                    )

                return data.get("data", {})

            except requests.ConnectionError as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "[NEWRELIC] Connection error, retrying in %.1fs (attempt %d/%d): %s",
                        wait, attempt + 1, MAX_RETRIES, exc,
                    )
                    time.sleep(wait)
                    continue

            except requests.Timeout as exc:
                last_error = exc
                if attempt < MAX_RETRIES:
                    wait = RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(
                        "[NEWRELIC] Timeout, retrying in %.1fs (attempt %d/%d)",
                        wait, attempt + 1, MAX_RETRIES,
                    )
                    time.sleep(wait)
                    continue

            except NewRelicAPIError:
                raise

            except requests.HTTPError as exc:
                raise NewRelicAPIError(
                    f"NerdGraph HTTP error: {exc}",
                    status_code=getattr(exc.response, "status_code", None),
                ) from exc

        raise NewRelicAPIError(
            f"NerdGraph request failed after {MAX_RETRIES + 1} attempts: {last_error}"
        )

    # ------------------------------------------------------------------
    # Validation & account info
    # ------------------------------------------------------------------

    def validate_credentials(self) -> Dict[str, Any]:
        """Validate API key by fetching the authenticated user info."""
        query = """
        {
            actor {
                user {
                    email
                    name
                    id
                }
            }
        }
        """
        data = self._execute_graphql(query)
        user_info = data.get("actor", {}).get("user", {})
        if not user_info.get("email"):
            raise NewRelicAPIError("Unable to validate API key: no user info returned")
        return user_info

    def get_account_info(self) -> Dict[str, Any]:
        """Fetch account name and ID for the configured account."""
        query = """
        {
            actor {
                account(id: %s) {
                    id
                    name
                }
            }
        }
        """ % self.account_id
        data = self._execute_graphql(query)
        account = data.get("actor", {}).get("account")
        if not account:
            raise NewRelicAPIError(f"Account {self.account_id} not found or inaccessible")
        return account

    def list_accessible_accounts(self) -> List[Dict[str, Any]]:
        """List all accounts accessible by the API key."""
        query = """
        {
            actor {
                accounts {
                    id
                    name
                }
            }
        }
        """
        data = self._execute_graphql(query)
        return data.get("actor", {}).get("accounts", [])

    # ------------------------------------------------------------------
    # NRQL queries
    # ------------------------------------------------------------------

    def execute_nrql(self, nrql: str, timeout: int = 30) -> Dict[str, Any]:
        """Execute a NRQL query against the configured account."""
        query = """
        query($accountId: Int!, $nrql: Nrql!, $timeout: Seconds) {
            actor {
                account(id: $accountId) {
                    nrql(query: $nrql, timeout: $timeout) {
                        results
                        metadata {
                            facets
                            eventTypes
                            timeWindow { begin end }
                        }
                    }
                }
            }
        }
        """
        variables = {
            "accountId": int(self.account_id),
            "nrql": nrql,
            "timeout": timeout,
        }
        data = self._execute_graphql(query, variables=variables)
        nrql_data = data.get("actor", {}).get("account", {}).get("nrql", {})
        return {
            "results": nrql_data.get("results", []),
            "metadata": nrql_data.get("metadata", {}),
        }

    # ------------------------------------------------------------------
    # Issues (AiIssues)
    # ------------------------------------------------------------------

    def get_issues(
        self,
        states: Optional[List[str]] = None,
        since_epoch_ms: Optional[int] = None,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        """Fetch alert issues from NerdGraph AiIssuesSearch."""
        filter_parts = []
        if states:
            states_str = ", ".join(states)
            filter_parts.append(f"states: [{states_str}]")
        if since_epoch_ms:
            filter_parts.append(f"startTime: {since_epoch_ms}")

        issue_args = [f"first: {page_size}"]
        if filter_parts:
            filter_clause = ", ".join(filter_parts)
            issue_args.insert(0, f"filter: {{ {filter_clause} }}")

        query = """
        {
            actor {
                account(id: %s) {
                    aiIssues {
                        issues(%s) {
                            issues {
                                issueId
                                title
                                priority
                                state
                                sources
                                entityNames
                                entityGuids
                                conditionName
                                policyName
                                activatedAt
                                closedAt
                                totalIncidents
                                isCorrelated
                                mutingState
                                issueUrl
                            }
                        }
                    }
                }
            }
        }
        """ % (self.account_id, ", ".join(issue_args))
        try:
            data = self._execute_graphql(query)
        except Exception:
            logger.exception("[NEWRELIC] get_issues GraphQL query failed")
            raise
        issues_data = (
            data.get("actor", {})
            .get("account", {})
            .get("aiIssues", {})
            .get("issues", {})
            .get("issues", [])
        )
        return {"issues": issues_data, "count": len(issues_data)}

    # ------------------------------------------------------------------
    # Entity search
    # ------------------------------------------------------------------

    def search_entities(
        self,
        query_str: str = "",
        entity_type: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Search for entities (APM apps, hosts, etc.) via NerdGraph.

        Scopes results to the configured account_id by appending an
        accountId filter to the entity search query.
        """
        parts = []
        if query_str:
            parts.append(query_str)
        parts.append(f"accountId = '{self.account_id}'")
        if entity_type:
            parts.append(f"type = '{self._sanitize_graphql_string(entity_type)}'")
        combined_query = " AND ".join(parts)

        gql = """
        query($query: String!, $limit: Int) {
            actor {
                entitySearch(query: $query) {
                    results(limit: $limit) {
                        entities {
                            guid
                            name
                            entityType
                            domain
                            reporting
                            alertSeverity
                            tags { key values }
                        }
                    }
                }
            }
        }
        """
        variables = {"query": combined_query, "limit": limit}
        data = self._execute_graphql(gql, variables=variables)
        return (
            data.get("actor", {})
            .get("entitySearch", {})
            .get("results", {})
            .get("entities", [])
        )
