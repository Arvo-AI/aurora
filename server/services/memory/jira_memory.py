"""Seed the "Jira RCA" memory: the policy Auto mode's judge follows."""

from services.memory import JIRA_MEMORY_CATEGORY, JIRA_MEMORY_TITLE
from services.memory.seed import seed_memory_entry

JIRA_MEMORY_DESCRIPTION = (
    "Jira automatic RCA: which Jira tickets Aurora investigates on its own. "
    "Update this whenever someone says a kind of ticket should or shouldn't get an RCA."
)

JIRA_MEMORY_DEFAULT_CONTENT = """\
This is Aurora's policy for which new or updated Jira tickets get an automatic \
root cause analysis (RCA). Aurora reads it before judging each ticket. Update it \
as the team says what should or shouldn't be investigated.

## Investigate
- Bug reports from customers or users: something broken, erroring, slow, or down.
- Production incidents, outages, alerts, and degraded service.
- Regressions after a deploy, release, or config change.

## Usually skip
- Test or QA investigation tickets, unless they describe a production impact.
- Feature requests, stories, design work, and refactors.

## Skip
- Administrative or communication tasks (send an email, schedule a meeting, \
update a doc).
- Access requests, onboarding, and chores with nothing broken to explain.

## When unsure
Investigate only if the ticket describes a concrete failure in a running system.

## Team rules
(none yet — add rules like "never investigate tickets in project X" here)
"""


def seed_jira_memory(user_id: str, org_id: str | None = None) -> bool:
    """Create the default "Jira RCA" memory for an org if absent. True if created."""
    return seed_memory_entry(
        user_id,
        category=JIRA_MEMORY_CATEGORY,
        title=JIRA_MEMORY_TITLE,
        content=JIRA_MEMORY_DEFAULT_CONTENT,
        description=JIRA_MEMORY_DESCRIPTION,
        org_id=org_id,
        log_prefix="[JiraMemory]",
    )
