"""Built-in Monitoring Coverage Scan action.

Finds what nothing watches and watches it. The agent lists the org's assets,
finds the monitoring that already covers each one (the provider's own monitors
and alarms, and Aurora's checks), and saves a read-only detection check for
each real blind spot. An org with no monitoring at all gets its baseline this
way, with no incident and no person involved.

Runs weekly, and right after prediscovery when a new integration is connected
(prediscovery has just refreshed the infrastructure map).

Not the Alert Gap Audit (alert_gap_action.py): that one reads alert
definitions in the org's repos and opens PRs. This scan looks at the live
systems and closes gaps with checks Aurora runs itself.
"""

import logging

logger = logging.getLogger(__name__)

SYSTEM_KEY = "monitoring_coverage_scan"

# New checks per run. The org-wide cap (store.MAX_ACTIVE_CHECKS_PER_ORG) still
# applies; this keeps one run from filling it with low-value checks.
MAX_NEW_CHECKS_PER_RUN = 20
COVERAGE_MEMORY_TITLE = "Monitoring Coverage"

# Prediscovery triggers that start a scan at once. A scheduled prediscovery
# does not: the action's own weekly schedule covers that.
_START_AFTER = ("new_connector", "manual")

DEFAULT_COVERAGE_SCAN_INSTRUCTIONS = """# MONITORING COVERAGE SCAN

Your goal: no blind spots. Every asset this organization runs should have something that
notices when it breaks. Find what nothing watches, and watch it with a detection check.
Read-only: change nothing in the customer's systems.

## STEP 1: LIST THE ASSETS
- read_memory(category='infrastructure', title='Infrastructure Context') for the map prediscovery wrote.
- If a service graph tool is available (list_services), use it too.
- For on-prem Kubernetes, call get_connected_clusters first and pass its cluster_id to
  on_prem_kubectl. The cluster's display name is not its id.
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
- read_memory(category='infrastructure', title='%(memory)s') for what the last scan found.

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
- Only save a check whose exact command you ran in this scan and that returned a healthy value
  now. Memory and the service graph can be out of date: if a live read fails, fix the call
  (identifiers, region, namespace) and retry. If it still fails, save nothing for that asset and
  list it in the memory entry as unverified.
Save at most %(max_new)d new checks in this run, most important assets first
(user-facing and data stores before internal tooling). Skip a gap you cannot express as one
read-only command with a clear healthy condition, and list it in the memory entry instead.

## STEP 5: WRITE DOWN THE COVERAGE
Write or update the memory entry category='infrastructure', title='%(memory)s':
one line per asset with what watches it (existing alert, Aurora check, or nothing), the gaps you
closed this run, and the gaps left open and why. Use edit_memory/append_to_memory when it exists.
The next scan starts from it.

## RULES
- The local filesystem is Aurora's own code: never use terminal_exec to read local files.
- Never run a command that changes anything. Never open PRs.
- Write no memory entry other than '%(memory)s'. Do not save these instructions.
- Only describe infrastructure you actually found. If nothing is connected that you can read,
  write that in the memory entry and stop.""" % {
    "max_new": MAX_NEW_CHECKS_PER_RUN, "memory": COVERAGE_MEMORY_TITLE,
}


def _owner_can_save_checks(owner_id: str, org_id: str) -> bool:
    from utils.auth.enforcer import enforce_with_reload

    return bool(owner_id) and enforce_with_reload(owner_id, org_id, "detection_checks", "write")


_FIRST_ADMIN_SQL = """
    SELECT id FROM users WHERE org_id = %s AND role = %s ORDER BY created_at LIMIT 1
"""


def owner_to_run_as(action_id: str, org_id: str, owner_id: str):
    """Who the scan runs as: its owner if they may save checks, else the org's first admin.

    The scan's whole output is checks, and save_detection_check refuses a
    viewer. The action is seeded with whoever triggered the seeding, which
    can be a viewer, so it is handed to an admin instead (and kept there),
    the way orphaned actions are (ownership.py). None when the org has no
    one who can save checks: the run is skipped rather than spent.
    """
    if _owner_can_save_checks(owner_id, org_id):
        return owner_id
    from utils.auth import ROLE_ADMIN
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(_FIRST_ADMIN_SQL, (org_id, ROLE_ADMIN))
        row = cur.fetchone()
        admin = str(row[0]) if row else None
        if not admin or not _owner_can_save_checks(admin, org_id):
            logger.warning("[CoverageScan] No one in org %s can save checks; scan not started", org_id)
            return None
        # actions is RLS-protected: without the org's context the update matches nothing.
        from utils.auth.stateless_auth import set_rls_context
        if set_rls_context(cur, conn, admin, log_prefix="[CoverageScan]") != org_id:
            return admin
        cur.execute("UPDATE actions SET created_by = %s, updated_at = NOW() WHERE id = %s AND org_id = %s",
                    (admin, action_id, org_id))
        moved = cur.rowcount
        conn.commit()
    if moved:
        logger.info("[CoverageScan] Scan action %s now runs as an admin of org %s", action_id, org_id)
    else:
        logger.warning("[CoverageScan] Could not reassign scan action %s; running it as an admin "
                       "this time only", action_id)
    return admin


def start_after_prediscovery(user_id: str, trigger: str) -> bool:
    """Start the org's scan once prediscovery has mapped a new integration.

    Returns True when a run was dispatched. Skipped when checks are off, the
    trigger is a routine scheduled prediscovery, the org turned the action
    off, or a run is already pending or running.
    """
    from services.detection import checks_enabled

    if not checks_enabled() or trigger not in _START_AFTER:
        return False

    from services.actions.executor import dispatch_action
    from services.actions.system_actions import seed_system_actions
    from utils.auth.stateless_auth import get_org_id_for_user, set_rls_context
    from utils.db.connection_pool import db_pool

    org_id = get_org_id_for_user(user_id)
    if not org_id:
        return False
    # Orgs created before this action existed get it here.
    seed_system_actions(org_id, user_id)
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if not set_rls_context(cur, conn, user_id, log_prefix="[CoverageScan]"):
            return False
        # Two prediscovery runs finishing together must not start two scans.
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("coverage-scan:%s" % org_id,))
        cur.execute(
            """SELECT a.id, a.created_by FROM actions a
               WHERE a.org_id = %s AND a.system_key = %s AND a.enabled
                 AND NOT EXISTS (SELECT 1 FROM action_runs r WHERE r.action_id = a.id
                                 AND r.status IN ('pending', 'running'))""",
            (org_id, SYSTEM_KEY),
        )
        row = cur.fetchone()
        owner = owner_to_run_as(str(row[0]), org_id, row[1]) if row else None
        # Dispatched under the lock: the run row it creates is what the
        # second prediscovery's query then sees.
        if owner:
            dispatch_action(action_id=str(row[0]), user_id=owner,
                            trigger_context={"source": "prediscovery", "trigger": trigger})
        conn.commit()
    return bool(owner)
