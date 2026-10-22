"""Seed the "ServiceNow RCA" memory: the policy Auto mode's judge follows."""

from services.memory import SERVICENOW_MEMORY_CATEGORY, SERVICENOW_MEMORY_TITLE
from services.memory.seed import seed_memory_entry

SERVICENOW_MEMORY_DESCRIPTION = (
    "ServiceNow automatic RCA: which ServiceNow incidents Aurora investigates on its own. "
    "Update this whenever someone says a kind of incident should or shouldn't get an RCA."
)

SERVICENOW_MEMORY_DEFAULT_CONTENT = """\
This is Aurora's policy for which new ServiceNow incidents get an automatic \
root cause analysis (RCA). Aurora reads it before judging each incident. Update it \
as the team says what should or shouldn't be investigated.

## Investigate
- Outages, errors, timeouts, and degraded performance of a service or application.
- Priority 1 and 2 incidents that describe a technical failure.
- Incidents on infrastructure, platforms, databases, networks, or CI/CD pipelines.
- Repeat incidents on the same configuration item or symptom.

## Usually skip
- Priority 4 and 5 incidents, unless they describe a production failure.
- Single-user problems with a laptop, phone, printer, or desk setup.

## Skip
- Access requests, password resets, account unlocks, and onboarding.
- Hardware orders, software installs, licensing, and other service requests.
- How-to questions, training, and documentation requests.
- Administrative tasks with nothing broken to explain.

## When unsure
Investigate only if the incident describes a concrete failure in a running system.

## Team rules
(none yet — add rules like "never investigate incidents assigned to Service Desk" here)
"""


def seed_servicenow_memory(user_id: str, org_id: str | None = None) -> bool:
    """Create the default "ServiceNow RCA" memory for an org if absent. True if created."""
    return seed_memory_entry(
        user_id,
        category=SERVICENOW_MEMORY_CATEGORY,
        title=SERVICENOW_MEMORY_TITLE,
        content=SERVICENOW_MEMORY_DEFAULT_CONTENT,
        description=SERVICENOW_MEMORY_DESCRIPTION,
        org_id=org_id,
        log_prefix="[ServiceNowMemory]",
    )
