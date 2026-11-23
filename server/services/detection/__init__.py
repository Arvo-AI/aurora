"""Detection memory: checks Aurora writes, runs and retires by itself.

A check is a read-only command plus an assertion about its output, run on a
schedule with no model in the loop. Every check starts ``silent``: its failures
are recorded and reach no one.
"""

import os


def checks_enabled() -> bool:
    """The switch for the whole feature (DETECTION_CHECKS_ENABLED=false turns it off).

    Off means off everywhere: no check runs, the agent has no check tools and
    is not told to save checks, nothing is replayed, and the page says so.
    """
    return (os.getenv("DETECTION_CHECKS_ENABLED") or "true").strip().lower() != "false"


def chat_intake_enabled() -> bool:
    """Reading chat channels is opt-in (DETECTION_CHAT_INTAKE_ENABLED=true), and off with the feature.

    It sends the messages of the public channels Aurora is in through a model,
    which is a decision for whoever runs the install, not a default.
    """
    return checks_enabled() and (
        (os.getenv("DETECTION_CHAT_INTAKE_ENABLED") or "false").strip().lower() == "true"
    )


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
