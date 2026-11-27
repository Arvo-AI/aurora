"""Monitoring Coverage Scan: find what nothing watches and watch it.

Runs after prediscovery has mapped the org's infrastructure. The agent lists
the org's assets, finds the monitoring that already covers each one (the
provider's own monitors and alarms, and Aurora's checks), and saves a
read-only detection check for each real blind spot. An org with no
monitoring at all gets its baseline this way, with no incident and no person
involved.

Not the Alert Gap Audit (services/actions/alert_gap_action.py): that action
reads alert definitions in the org's repos and opens PRs. This scan looks at
the live systems and closes gaps with checks Aurora runs itself.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from celery_config import celery_app
from services.detection import COVERAGE_SCAN_SOURCE
from chat.investigation_timeout import (
    investigation_soft_timeout_seconds,
    investigation_timeout_seconds,
)

logger = logging.getLogger(__name__)

_LOG = "[CoverageScan]"
_HARD_TIMEOUT = investigation_timeout_seconds()
_SOFT_TIMEOUT = investigation_soft_timeout_seconds()

# A full scan is about the size of a prediscovery run. Weekly is enough to
# pick up new assets; a new connection runs one right away.
RESCAN_AFTER = timedelta(days=7)
# New checks per run. The org-wide cap (store.MAX_ACTIVE_CHECKS_PER_ORG)
# still applies; this keeps one run from filling it with low-value checks.
MAX_NEW_CHECKS_PER_RUN = 20
COVERAGE_MEMORY_TITLE = "Monitoring Coverage"


def build_coverage_scan_prompt(providers: List[str], integrations: Dict[str, bool]) -> str:
    connected = [name for name, is_connected in integrations.items() if is_connected]
    provider_list = ", ".join(providers) if providers else "none"
    integration_list = ", ".join(connected) if connected else "none"

    return f"""# MONITORING COVERAGE SCAN

Your goal: no blind spots. Every asset this organization runs should have something that
notices when it breaks. Find what nothing watches, and watch it with a detection check.
Read-only: change nothing in the customer's systems.

## CONNECTED
- Cloud providers: {provider_list}
- Other integrations: {integration_list}

## STEP 1: LIST THE ASSETS
- read_memory(category='infrastructure', title='Infrastructure Context') for the map prediscovery wrote.
- If a service graph tool is available (list_services), use it too.
- Confirm against the live systems with read-only commands (cloud_exec, on_prem_kubectl, the
  integration tools): services and deployments, databases, caches, queues, load balancers and
  endpoints, scheduled jobs, certificates, nodes.

## STEP 2: FIND WHAT ALREADY WATCHES EACH ONE
- The provider's own monitoring: query_datadog(resource_type='monitors'); CloudWatch alarms
  (cloud_exec('aws', 'cloudwatch describe-alarms ...')); GCP alert policies
  (cloud_exec('gcp', 'alpha monitoring policies list ...')); Azure metric alerts
  (cloud_exec('azure', 'monitor metrics alert list ...')); Grafana, Dynatrace, New Relic and
  other connected tools where they can list their rules.
- Aurora's own checks: list_detection_checks().
- read_memory(category='infrastructure', title='{COVERAGE_MEMORY_TITLE}') for what the last scan found.

An alert covers a failure only if it can actually fire: it is not muted or disabled, it
watches a metric or log that still exists, and it notifies someone. A muted, broken or
unrouted alert is a blind spot.

## STEP 3: DECIDE WHAT A BLIND SPOT IS
For each asset, think about the failures that would hurt users and how they would show:
- A service or endpoint: down / not answering, error rate, latency, zero traffic when it should have some.
- A database, cache or queue: unreachable, saturated (connections, storage, memory), backlog growing.
- A scheduled job: did not run, or failed.
- Nodes and capacity: not ready, out of disk or memory.
- Certificates and domains: about to expire.
Use these to think, not as a checklist: a batch job needs no latency check.

Do NOT save a check for something an existing working alert or Aurora check already covers.
Duplicating it only adds noise.

## STEP 4: CLOSE THE GAPS
For each real blind spot that one read-only command can observe, call save_detection_check:
- origin_kind 'graph' when it is the standard check for this kind of asset, 'explore' when it
  is specific to something you noticed. origin_ref: what you found (e.g. "no monitor on payments-db").
- asset: the bare service name exactly as incidents name it.
- unmonitored: true only when nothing else watches this failure on this asset. Such a check
  opens a case when it fails from the start, so only claim it when it is true.
- A clear healthy condition. Pick thresholds from what you see the asset do today, with margin,
  so a normal day does not fire it.
- Prefer one check per failure over one check that mixes several.
Save at most {MAX_NEW_CHECKS_PER_RUN} new checks in this run, most important assets first
(user-facing and data stores before internal tooling). Skip a gap you cannot express as one
read-only command with a clear healthy condition, and list it in the memory entry instead.

## STEP 5: WRITE DOWN THE COVERAGE
Write or update the memory entry category='infrastructure', title='{COVERAGE_MEMORY_TITLE}':
one line per asset with what watches it (existing alert, Aurora check, or nothing), the gaps you
closed this run, and the gaps left open and why. Use edit_memory/append_to_memory when it exists.
The next scan starts from it.

## RULES
- The local filesystem is Aurora's own code: never use terminal_exec to read local files.
- Never run a command that changes anything. Never open PRs.
- Only describe infrastructure you actually found. If nothing is connected that you can read,
  write that in the memory entry and stop."""


def _last_scan_at(user_id: str):
    """When this org's last scan started, or None."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        org_id = set_rls_context(cur, conn, user_id, log_prefix=_LOG)
        if not org_id:
            return None
        cur.execute(
            """SELECT MAX(created_at) FROM chat_sessions
               WHERE org_id = %s AND ui_state->'triggerMetadata'->>'source' = %s""",
            (org_id, COVERAGE_SCAN_SOURCE),
        )
        row = cur.fetchone()
        return row[0] if row else None


def scan_is_due(user_id: str, trigger: str) -> bool:
    """A new connection or a manual run always scans; otherwise once a week."""
    if trigger in ("new_connector", "manual"):
        return True
    try:
        last = _last_scan_at(user_id)
    except Exception:
        logger.exception("%s Could not read the last scan time", _LOG)
        return False
    return last is None or datetime.now() - last > RESCAN_AFTER


def schedule_after_prediscovery(user_id: str, trigger: str) -> bool:
    """Start a scan once prediscovery has refreshed the infrastructure map.

    Prediscovery runs on a new connection, on its own schedule (daily by
    default) and by hand, so this gives every connected org a scan on
    connect and then weekly, without a beat entry of its own.
    """
    from services.detection import checks_enabled

    if not checks_enabled() or not scan_is_due(user_id, trigger):
        return False
    run_coverage_scan.delay(user_id=user_id, trigger=trigger)
    return True


@celery_app.task(
    bind=True,
    name="chat.background.coverage_scan_task.run_coverage_scan",
    time_limit=_HARD_TIMEOUT,
    soft_time_limit=_SOFT_TIMEOUT,
)
def run_coverage_scan(self, user_id: str, trigger: str = "manual") -> Dict[str, Any]:
    """Run the Monitoring Coverage Scan for the org of *user_id*."""
    from celery.exceptions import SoftTimeLimitExceeded
    from services.detection import checks_enabled

    if not checks_enabled():
        return {"status": "skipped", "reason": "checks_disabled"}

    from chat.background.liveness import run_watched
    from chat.background.rca_prompt_builder import get_user_providers
    from chat.background.task import (
        _execute_background_chat,
        _get_connected_integrations,
        _update_session_status,
        create_background_chat_session,
    )
    from utils.auth.stateless_auth import get_org_id_for_user
    from utils.hooks import get_hook

    org_id = get_org_id_for_user(user_id)
    allowed, message = get_hook("before_llm_call")(org_id, user_id)
    if not allowed:
        logger.warning("%s Hook blocked for user %s: %s", _LOG, user_id, message)
        return {"status": "hook_blocked", "error": message}

    try:
        providers = get_user_providers(user_id)
        integrations = _get_connected_integrations(user_id)
        if not providers and not any(integrations.values()):
            return {"status": "skipped", "reason": "no_integrations"}

        trigger_metadata = {"source": COVERAGE_SCAN_SOURCE, "trigger": trigger, "task_id": self.request.id}
        session_id = create_background_chat_session(
            user_id=user_id,
            title="Monitoring Coverage Scan (%s)" % trigger,
            trigger_metadata=trigger_metadata,
        )
        run_watched(
            _execute_background_chat(
                user_id=user_id,
                session_id=session_id,
                initial_message=build_coverage_scan_prompt(providers, integrations),
                trigger_metadata=trigger_metadata,
                provider_preference=providers,
                mode="ask",
                rail_text="",
            ),
            session_id,
            user_id,
        )
        _update_session_status(session_id, "completed", user_id=user_id)
        logger.info("%s Completed for user %s, session %s", _LOG, user_id, session_id)
        return {"status": "completed", "session_id": session_id}
    except SoftTimeLimitExceeded:
        logger.error("%s Timeout for user %s", _LOG, user_id)
        return {"status": "timeout"}
    except Exception as e:
        logger.exception("%s Failed for user %s", _LOG, user_id)
        return {"status": "error", "error": str(e)}
