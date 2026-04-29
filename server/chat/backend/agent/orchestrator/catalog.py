"""Built-in sub-agent catalog — planner inspiration only, not a fixed taxonomy."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


BUILTIN_CATALOG: dict[str, dict] = {
    "builtin:db": {
        "id": "builtin:db",
        "ui_label": "DB Investigator",
        "domain": "db",
        "default_skills": ["postgres", "datadog", "rds", "mysql"],
        "purpose_template": "investigate database layer for query/connection regressions tied to the incident",
        "rca_priority": 10,
    },
    "builtin:deploy": {
        "id": "builtin:deploy",
        "ui_label": "Deploy Investigator",
        "domain": "deploy",
        "default_skills": ["github", "argocd", "kubectl", "datadog"],
        "purpose_template": "investigate recent deploys and config changes that could explain the incident",
        "rca_priority": 5,
    },
    "builtin:network": {
        "id": "builtin:network",
        "ui_label": "Network Investigator",
        "domain": "network",
        "default_skills": ["datadog", "grafana", "kubectl", "newrelic"],
        "purpose_template": "investigate network/edge/DNS/CDN paths for the incident's failure mode",
        "rca_priority": 20,
    },
    "builtin:observability": {
        "id": "builtin:observability",
        "ui_label": "Observability Investigator",
        "domain": "observability",
        "default_skills": ["datadog", "grafana", "newrelic", "splunk"],
        "purpose_template": "correlate metrics/logs/traces around the incident timeframe",
        "rca_priority": 15,
    },
    "builtin:infra": {
        "id": "builtin:infra",
        "ui_label": "Infra Investigator",
        "domain": "infra",
        "default_skills": ["kubectl", "aws", "gcp", "azure", "terraform"],
        "purpose_template": "investigate infra/cluster/node-level signals for the incident",
        "rca_priority": 25,
    },
    "builtin:app": {
        "id": "builtin:app",
        "ui_label": "Application Investigator",
        "domain": "app",
        "default_skills": ["github", "datadog", "newrelic", "sentry"],
        "purpose_template": "investigate application-layer errors, exceptions, and code-paths for the incident",
        "rca_priority": 30,
    },
}


def get_enabled_catalog(org_id: str) -> dict[str, dict]:
    if not org_id:
        return dict(BUILTIN_CATALOG)
    try:
        from utils.db.connection_pool import db_pool

        disabled_ids: set[str] = set()
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
                conn.commit()
                cursor.execute(
                    "SELECT subagent_id FROM subagent_overrides "
                    "WHERE org_id = %s AND enabled = FALSE",
                    (org_id,),
                )
                disabled_ids = {row[0] for row in cursor.fetchall() if row and row[0]}
        return {k: v for k, v in BUILTIN_CATALOG.items() if k not in disabled_ids}
    except Exception as e:
        logger.warning("[orchestrator:get_enabled_catalog] fail-open for org=%s: %s", org_id, e)
        return dict(BUILTIN_CATALOG)
