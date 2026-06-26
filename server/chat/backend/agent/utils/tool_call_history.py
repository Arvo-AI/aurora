"""Shared rendering of RCA sub-agent tool-call history.

Tool calls surface in several places that must render identically: the live
sub-agent capture (``sub_agent.py`` / ``tool_context_capture.py``), the findings
API (``incidents_findings.py``), and the internal-agent introspection tool
(``introspection_tools.py``). This module owns the single definition of the
provider-CLI prefixing, the field-size caps, and the ``execution_steps`` → entry
shaping so those copies can't drift out of sync.
"""

from utils.query_helpers import iso_utc
from utils.text.text_utils import truncate

# Field-size caps for persisted/rendered history entries. Centralized so the
# live capture and the archived JSONB blob always render with the same limits.
OUTPUT_EXCERPT_MAX_CHARS = 1000
COMMAND_MAX_CHARS = 1024
MAX_HISTORY_ENTRIES = 30

# Sub-agent lifecycle states that mean "done" — once terminal we can trust the
# archived history blob instead of re-reading live execution_steps.
TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "timeout", "cancelled", "inconclusive"}
)

# Map a cloud_exec provider to its CLI prefix, mirroring the frontend's
# getProviderCli so the captured command already includes the prefix
# (e.g. "aws cloudwatch ...") and the UI can pick the right icon from
# command.startswith without needing a separate provider field.
PROVIDER_CLI = {
    "aws": "aws", "gcp": "gcloud", "gcloud": "gcloud", "azure": "az", "az": "az",
    "ovh": "ovhcloud", "ovhcloud": "ovhcloud", "scaleway": "scw", "scw": "scw",
}
RECOGNIZED_CLI_PREFIXES = (
    "aws ", "gcloud ", "gsutil ", "bq ", "az ", "ovhcloud ", "scw ",
    "kubectl ", "helm ", "docker ",
)


def derive_command(tool_input, limit: int = COMMAND_MAX_CHARS) -> str:
    """Render a readable CLI command from a tool's structured input.

    Prefixes a raw shell command with the implied provider CLI; otherwise falls
    back to the first query-like field present.
    """
    if not isinstance(tool_input, dict):
        return ""
    cmd = tool_input.get("command")
    # A raw shell command — prefix with the provider CLI when one is implied.
    if isinstance(cmd, str) and cmd.strip():
        provider = tool_input.get("provider")
        cli = PROVIDER_CLI.get(str(provider).lower()) if provider else None
        if cli and not cmd.lstrip().startswith(RECOGNIZED_CLI_PREFIXES):
            cmd = f"{cli} {cmd.lstrip()}"
        return truncate(cmd, limit)
    # Otherwise fall back to whichever query-like field is present.
    for key in ("query", "path", "promql"):
        if tool_input.get(key):
            return truncate(tool_input[key], limit)
    return ""


def history_from_step_rows(rows) -> list[dict]:
    """Shape ``execution_steps`` rows into tool-call history entries.

    Each row must be ``(tool_name, tool_input, tool_output, status, started_at,
    completed_at)`` — matching the column order in the queries that feed this.
    """
    return [
        {
            "tool_name": tool_name or "unknown",
            "args": tool_input,
            "command": derive_command(tool_input),
            "output_excerpt": truncate(tool_output, OUTPUT_EXCERPT_MAX_CHARS),
            "is_error": status == "error",
            "status": status,
            "started_at": iso_utc(started_at),
            "completed_at": iso_utc(completed_at),
        }
        for tool_name, tool_input, tool_output, status, started_at, completed_at in rows
    ]
