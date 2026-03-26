"""
Celery task for running prediscovery agent sessions.

The prediscovery agent autonomously explores connected integrations (GitHub, Jenkins,
cloud providers, monitoring tools) to map how services are interconnected. Findings
are saved to the knowledge base for fast retrieval during RCA investigations.
"""

import logging
from typing import Any, Dict, List

from celery_config import celery_app

logger = logging.getLogger(__name__)


def _get_users_with_integrations() -> List[Dict[str, Any]]:
    """Get all users who have at least one connected integration."""
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT DISTINCT u.id, u.org_id
                    FROM users u
                    WHERE u.org_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM user_tokens ut
                          WHERE ut.user_id = u.id AND ut.is_active = true
                          UNION
                          SELECT 1 FROM user_connections uc
                          WHERE uc.user_id = u.id AND uc.status = 'active'
                      )
                    ORDER BY u.id
                """)
                return [{"user_id": row[0], "org_id": row[1]} for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"[Prediscovery] Failed to get users: {e}")
        return []


def _cleanup_old_discovery_chunks(org_id: str) -> int:
    """Delete previous discovery findings from Weaviate for this org."""
    try:
        from routes.knowledge_base.weaviate_client import delete_discovery_chunks
        return delete_discovery_chunks(org_id)
    except Exception as e:
        logger.warning(f"[Prediscovery] Failed to cleanup old chunks: {e}")
        return 0


def build_prediscovery_prompt(user_id: str, providers: List[str], integrations: Dict[str, bool]) -> str:
    """Build the system prompt for the prediscovery agent."""
    connected = [name for name, is_connected in integrations.items() if is_connected]
    provider_list = ", ".join(providers) if providers else "none"
    integration_list = ", ".join(connected) if connected else "none"

    return f"""# INFRASTRUCTURE PRE-DISCOVERY

You are an infrastructure discovery agent. Your job is to query the connected external
integrations and map how the organization's services, repos, pipelines, and monitoring
are interconnected.

## CONNECTED INTEGRATIONS
- Cloud providers: {provider_list}
- Other integrations: {integration_list}

## CRITICAL RULES

- The local filesystem is Aurora's own code -- NEVER use terminal_exec to read local files (ls, cat, find, grep, env). There is nothing useful locally.
- terminal_exec is ONLY allowed for SSH into manual VMs (e.g. ssh -i ~/.ssh/id_key user@ip).
- Each finding must be a detailed paragraph describing real infrastructure you discovered by querying external APIs. Not a summary of how Aurora works.
- Call save_discovery_finding after EVERY interconnection chain you discover.

## EXPLORATION STRATEGY

1. **Cloud infrastructure** (if cloud providers connected):
   - cloud_exec('gcp'/'aws'/'azure', ...) to list clusters, VMs, databases, load balancers
   - For K8s: get namespaces, deployments, services, ingresses
   - Check container image tags to trace back to repos
   - List databases, caches, queues and what connects to them

2. **On-prem K8s clusters** (if listed in ON-PREM KUBERNETES CLUSTERS above):
   - on_prem_kubectl('get namespaces', cluster_id) to list what's running
   - on_prem_kubectl('get deployments -A', cluster_id) to list all deployments
   - Trace images, services, ingresses the same way as cloud K8s

3. **On-prem VMs** (if listed in MANUAL VMS above or Tailscale connected):
   - For manual VMs: use terminal_exec with the SSH command shown in MANUAL VMS section
   - For Tailscale devices: use tailscale_ssh(device, command) to explore what's running
   - Run: uname -a, docker ps, systemctl list-units, netstat -tlnp to discover services

4. **Source control** (if GitHub/Bitbucket connected):
   - github_rca(action='commits') and github_rca(action='pull_requests') to see active repos
   - github_rca(action='deployment_check') to see CI/CD workflow runs
   - Check what deployment targets are referenced in recent commits/PRs

5. **CI/CD** (if Jenkins/Spinnaker/CloudBees connected):
   - jenkins_rca(action='recent_deployments') or cloudbees_rca/spinnaker_rca to see what gets deployed where
   - For each deployment: what repo, what target environment, what K8s cluster/namespace

6. **Observability** (if Datadog/Splunk/Coroot/Dynatrace/ThousandEyes connected):
   - Datadog: query_datadog(resource_type='monitors') and query_datadog(resource_type='hosts')
   - Splunk: list_splunk_indexes(), search_splunk() to discover log sources
   - Coroot: coroot_get_service_map() for eBPF-discovered service dependencies, coroot_get_applications() for app inventory
   - Dynatrace: query_dynatrace() for entities and topology
   - ThousandEyes: thousandeyes_list_tests() for network monitoring targets
   - Map monitors/tests to the services and hosts discovered in earlier steps

7. **Documentation** (if Confluence/SharePoint connected):
   - confluence_search_similar(keywords='architecture') or sharepoint_search(query='infrastructure')
   - Look for architecture diagrams, service catalogs, runbooks that describe topology

## FINDING FORMAT

Each finding must be a detailed, descriptive paragraph that a human or AI can read during
an incident and immediately understand the topology. Write full sentences, not bullet lists.

GOOD example:
  save_discovery_finding(
    title='payment-api deployment and monitoring chain',
    content='The payment-api service lives in GitHub repo acme-org/payment-api on the main branch. It is deployed via Jenkins job payment-service-deploy which builds a Docker image pushed to ECR at 390403884122.dkr.ecr.us-east-1.amazonaws.com/payment-api. The deployment targets Kubernetes cluster prod-east-1 in namespace payments, running as deployment payment-api with 3 replicas. The service depends on RDS instance db-payments-prod (PostgreSQL) which it connects to via environment variable DATABASE_URL, and ElastiCache cluster redis-sessions for session storage. It is monitored by Datadog monitors payment-api-latency (threshold: p99 > 500ms) and payment-api-error-rate (threshold: 5xx > 1%), both tagged with service:payment-api and env:production.',
    tags='github,jenkins,k8s,aws,datadog,payment-api'
  )

BAD example (too brief, no real detail):
  save_discovery_finding(
    title='Aurora Platform - Connector Ecosystem',
    content='Aurora supports GCP, AWS, Azure connectors...',
    tags='connectors'
  )

## BEGIN EXPLORATION NOW
Start with cloud infrastructure to discover what services are running, then trace
their deployment sources and monitoring. Call integration tools immediately."""


@celery_app.task(
    bind=True,
    name="chat.background.prediscovery_task.run_prediscovery",
    time_limit=1800,
    soft_time_limit=1740,
)
def run_prediscovery(
    self,
    user_id: str,
    trigger: str = "manual",
) -> Dict[str, Any]:
    """Run prediscovery for a single user.

    Args:
        user_id: User to run prediscovery for (uses their connected integrations)
        trigger: What triggered this run ('manual', 'scheduled', 'new_connector')
    """
    from celery.exceptions import SoftTimeLimitExceeded
    from chat.background.task import (
        _get_connected_integrations,
        _execute_background_chat,
        create_background_chat_session,
    )
    from chat.background.rca_prompt_builder import get_user_providers
    import asyncio

    logger.info(f"[Prediscovery] Starting for user {user_id} (trigger={trigger})")

    try:
        providers = get_user_providers(user_id)
        integrations = _get_connected_integrations(user_id)

        connected_count = sum(1 for v in integrations.values() if v) + len(providers)
        if connected_count == 0:
            logger.info(f"[Prediscovery] No integrations for user {user_id}, skipping")
            return {"status": "skipped", "reason": "no_integrations"}

        # Get org_id for cleanup
        from utils.auth.stateless_auth import get_org_id_for_user
        org_id = get_org_id_for_user(user_id)
        if org_id:
            _cleanup_old_discovery_chunks(org_id)

        prompt = build_prediscovery_prompt(user_id, providers, integrations)

        session_id = create_background_chat_session(
            user_id=user_id,
            title=f"Infrastructure Pre-Discovery ({trigger})",
            trigger_metadata={"source": "prediscovery", "trigger": trigger},
        )

        asyncio.run(_execute_background_chat(
            user_id=user_id,
            session_id=session_id,
            initial_message=prompt,
            trigger_metadata={"source": "prediscovery", "trigger": trigger},
            provider_preference=providers,
            mode="prediscovery",
        ))

        from chat.background.task import _update_session_status
        _update_session_status(session_id, "completed")

        logger.info(f"[Prediscovery] Completed for user {user_id}, session {session_id}")
        return {"status": "completed", "session_id": session_id, "user_id": user_id}

    except SoftTimeLimitExceeded:
        logger.error(f"[Prediscovery] Timeout for user {user_id}")
        return {"status": "timeout", "user_id": user_id}
    except Exception as e:
        logger.exception(f"[Prediscovery] Failed for user {user_id}: {e}")
        return {"status": "error", "user_id": user_id, "error": str(e)}


DEFAULT_INTERVAL_HOURS = 24
MIN_INTERVAL_HOURS = 1


def _should_run_for_user(user_id: str) -> bool:
    """Check if enough time has passed since last prediscovery for this user."""
    from utils.auth.stateless_auth import get_user_preference
    from utils.db.connection_pool import db_pool
    from datetime import datetime, timedelta

    interval = get_user_preference(user_id, "prediscovery_interval_hours", DEFAULT_INTERVAL_HOURS)
    interval = max(MIN_INTERVAL_HOURS, int(interval or DEFAULT_INTERVAL_HOURS))

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT created_at FROM chat_sessions
                    WHERE user_id = %s
                      AND ui_state->'triggerMetadata'->>'source' = 'prediscovery'
                    ORDER BY created_at DESC LIMIT 1
                """, (user_id,))
                row = cur.fetchone()
                if not row:
                    return True
                return datetime.now() - row[0] > timedelta(hours=interval)
    except Exception:
        return True


@celery_app.task(
    name="chat.background.prediscovery_task.run_prediscovery_all_orgs",
    bind=True,
    max_retries=0,
)
def run_prediscovery_all_orgs(self) -> Dict[str, Any]:
    """Run prediscovery for orgs that are due based on their configured interval."""
    logger.info("[Prediscovery] Starting scheduled check for all orgs")

    users = _get_users_with_integrations()
    if not users:
        logger.info("[Prediscovery] No users with integrations found")
        return {"status": "no_users", "processed": 0}

    seen_orgs = set()
    unique_users = []
    for u in users:
        if u["org_id"] not in seen_orgs:
            seen_orgs.add(u["org_id"])
            unique_users.append(u)

    dispatched = 0
    for u in unique_users:
        if _should_run_for_user(u["user_id"]):
            run_prediscovery.delay(user_id=u["user_id"], trigger="scheduled")
            dispatched += 1

    logger.info(f"[Prediscovery] Dispatched {dispatched}/{len(unique_users)} orgs (others not due yet)")
    return {"status": "dispatched", "orgs_due": dispatched, "orgs_total": len(unique_users)}
