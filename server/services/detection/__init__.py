"""Detection memory: checks Aurora writes, runs and retires by itself.

A check is a read-only command plus an assertion about its output, run on a
schedule with no model in the loop. Every check starts ``silent``: its failures
are recorded and reach no one.
"""

STATE_SILENT = "silent"
STATE_PROVEN = "proven"
STATE_RETIRED = "retired"

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERROR = "error"
# A run-log entry, not a verdict: a fire drill found the check no longer catches its incident.
STATUS_DRILL_FAILED = "drill_failed"

# Same floor as scheduled actions (services/actions/scheduler.py).
MIN_INTERVAL_SECONDS = 300
DEFAULT_INTERVAL_SECONDS = 900
MAX_INTERVAL_SECONDS = 7 * 24 * 3600

# Where a check came from. Answers "why are you watching this?".
ORIGIN_KINDS = ("incident", "miss", "graph", "explore", "gap", "person")

# A cluster connected by agent or kubeconfig, reached through on_prem_kubectl.
# Every other provider value is a cloud_exec provider.
PROVIDER_ONPREM_KUBECTL = "onprem_kubectl"
