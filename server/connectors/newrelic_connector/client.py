"""
New Relic NerdGraph API client.

Provides a production-grade GraphQL client for querying New Relic's NerdGraph API,
executing NRQL queries, and fetching issues/incidents for root cause analysis.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

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
    # Validation
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
    # NRQL Queries
    # ------------------------------------------------------------------

    def execute_nrql(
        self,
        nrql: str,
        account_id: Optional[str] = None,
        timeout_seconds: int = 30,
    ) -> Dict[str, Any]:
        """Execute an NRQL query against an account.

        Uses GraphQL variables for the NRQL string to prevent injection.
        """
        target_account = account_id or self.account_id
        query = """
        query ExecuteNRQL($nrqlQuery: Nrql!) {
            actor {
                account(id: %s) {
                    nrql(query: $nrqlQuery, timeout: %d) {
                        results
                        totalResult
                        metadata {
                            timeWindow {
                                begin
                                end
                            }
                            eventTypes
                            facets
                        }
                    }
                }
            }
        }
        """ % (target_account, timeout_seconds)

        variables = {"nrqlQuery": nrql}
        data = self._execute_graphql(query, variables)
        nrql_data = data.get("actor", {}).get("account", {}).get("nrql", {})
        return nrql_data

    # ------------------------------------------------------------------
    # Issues & Incidents (Alerts)
    # ------------------------------------------------------------------

    def get_issues(
        self,
        states: Optional[List[str]] = None,
        priorities: Optional[List[str]] = None,
        since_epoch_ms: Optional[int] = None,
        until_epoch_ms: Optional[int] = None,
        cursor: Optional[str] = None,
        page_size: int = 25,
    ) -> Dict[str, Any]:
        """Fetch alert issues from NerdGraph.

        Supports filtering by state (CREATED, ACTIVATED, DEACTIVATED, CLOSED),
        priority (LOW, MEDIUM, HIGH, CRITICAL), and time window.
        """
        filter_parts = []
        filter_params = []

        if states:
            states_enum = ", ".join(states)
            filter_parts.append(f"states: [{states_enum}]")
        if priorities:
            priorities_enum = ", ".join(priorities)
            filter_parts.append(f"priorities: [{priorities_enum}]")

        time_window = ""
        if since_epoch_ms or until_epoch_ms:
            tw_parts = []
            if since_epoch_ms:
                tw_parts.append(f"startTime: {since_epoch_ms}")
            if until_epoch_ms:
                tw_parts.append(f"endTime: {until_epoch_ms}")
            time_window = ", timeWindow: {%s}" % ", ".join(tw_parts)

        filter_str = ""
        if filter_parts:
            filter_str = ", filter: {%s}" % ", ".join(filter_parts)

        cursor_str = f', cursor: "{cursor}"' if cursor else ""

        query = """
        {
            actor {
                account(id: %s) {
                    aiIssues {
                        issues(first: %d%s%s%s) {
                            issues {
                                issueId
                                title
                                priority
                                state
                                activatedAt
                                closedAt
                                createdAt
                                updatedAt
                                totalIncidents
                                entityNames
                                entityGuids
                                conditionName
                                policyName
                                sources
                                isCorrelated
                                mutingState
                                acknowledgedAt
                                acknowledgedBy
                            }
                            nextCursor
                        }
                    }
                }
            }
        }
        """ % (self.account_id, page_size, filter_str, time_window, cursor_str)

        data = self._execute_graphql(query)
        ai_issues = data.get("actor", {}).get("account", {}).get("aiIssues", {}).get("issues", {})
        return ai_issues

    def get_issue_details(self, issue_id: str) -> Dict[str, Any]:
        """Fetch full details of a single issue including its incidents."""
        query = """
        {
            actor {
                account(id: %s) {
                    aiIssues {
                        issues(filter: {ids: ["%s"]}, first: 1) {
                            issues {
                                issueId
                                title
                                priority
                                state
                                activatedAt
                                closedAt
                                createdAt
                                updatedAt
                                totalIncidents
                                entityNames
                                entityGuids
                                conditionName
                                policyName
                                sources
                                isCorrelated
                                mutingState
                                acknowledgedAt
                                acknowledgedBy
                                description
                            }
                        }
                    }
                }
            }
        }
        """ % (self.account_id, issue_id)

        data = self._execute_graphql(query)
        issues = (
            data.get("actor", {})
            .get("account", {})
            .get("aiIssues", {})
            .get("issues", {})
            .get("issues", [])
        )
        return issues[0] if issues else {}

    # ------------------------------------------------------------------
    # Entity search
    # ------------------------------------------------------------------

    def search_entities(
        self,
        query_str: str = "",
        entity_type: Optional[str] = None,
        limit: int = 25,
    ) -> List[Dict[str, Any]]:
        """Search for entities (services, hosts, etc.) by name or type."""
        type_filter = f', type: "{entity_type}"' if entity_type else ""
        search_query = f'name LIKE \'%{query_str}%\'' if query_str else ""

        query = """
        {
            actor {
                entitySearch(query: "%s"%s) {
                    results(limit: %d) {
                        entities {
                            guid
                            name
                            type
                            domain
                            entityType
                            reporting
                            tags {
                                key
                                values
                            }
                            alertSeverity
                        }
                        nextCursor
                    }
                }
            }
        }
        """ % (search_query, type_filter, limit)

        data = self._execute_graphql(query)
        return (
            data.get("actor", {})
            .get("entitySearch", {})
            .get("results", {})
            .get("entities", [])
        )

    # ------------------------------------------------------------------
    # Convenience: telemetry queries
    # ------------------------------------------------------------------

    def query_metrics(
        self,
        metric_query: str,
        since: str = "1 hour ago",
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Run an NRQL metrics query with a time range."""
        nrql = f"{metric_query} SINCE {since}"
        return self.execute_nrql(nrql, account_id=account_id)

    def query_logs(
        self,
        filter_query: str = "",
        since: str = "1 hour ago",
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query log events via NRQL."""
        where_clause = f"WHERE {filter_query}" if filter_query else ""
        nrql = f"SELECT * FROM Log {where_clause} SINCE {since} LIMIT {limit}"
        return self.execute_nrql(nrql, account_id=account_id)

    def query_transactions(
        self,
        app_name: Optional[str] = None,
        since: str = "1 hour ago",
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query APM transaction data via NRQL."""
        where_clause = f"WHERE appName = '{app_name}'" if app_name else ""
        nrql = f"SELECT * FROM Transaction {where_clause} SINCE {since} LIMIT {limit}"
        return self.execute_nrql(nrql, account_id=account_id)

    def query_spans(
        self,
        service_name: Optional[str] = None,
        trace_id: Optional[str] = None,
        since: str = "1 hour ago",
        limit: int = 100,
        account_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Query distributed tracing spans via NRQL."""
        conditions = []
        if service_name:
            conditions.append(f"service.name = '{service_name}'")
        if trace_id:
            conditions.append(f"trace.id = '{trace_id}'")
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        nrql = f"SELECT * FROM Span {where_clause} SINCE {since} LIMIT {limit}"
        return self.execute_nrql(nrql, account_id=account_id)

    # ------------------------------------------------------------------
    # Deep-link URL builder
    # ------------------------------------------------------------------

    def build_nrql_console_url(self, nrql: str) -> str:
        """Build a URL that opens the New Relic query console with the NRQL pre-filled."""
        import base64
        base = "one.newrelic.com" if self.region != "eu" else "one.eu.newrelic.com"
        encoded = base64.urlsafe_b64encode(nrql.encode()).decode()
        return f"https://{base}/nrql-editor?account={self.account_id}&query={encoded}"
