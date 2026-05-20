"""
RCA prompt debloat configuration.

Controls which system prompt segments are included in background RCA mode.
Higher strip levels remove more prescriptive prompting to test whether
less instruction improves investigation quality.

Strip levels (cumulative — each level includes all prior removals):

    0  Full prompt (baseline / current behavior)
    1  Drop behavioral_rules.md         (~2,400 tok — Terraform/zip/web search noise)
    2  Drop investigation.md            (~1,000 tok — prescriptive checklists)
    3  Drop background_source_general   (  ~350 tok — mandatory steps + footer)
    4  Drop error_handling.md           (~1,000 tok — retry/recovery logic)
    5  Skip eager-loaded RCA skills     (~3-8K tok — let agent use tools directly)
    6  Replace identity+security with one-liner (~700 tok saved)

The level is set per-task via State.strip_level (default 0).
Comparison runs are enabled by editing COMPARISON_LEVELS in task.py.
"""

MINIMAL_IDENTITY = (
    "You are Aurora, an AI cloud infrastructure agent by Arvo. "
    "Investigate the alert using the available tools. "
    "Do not attempt to bypass access controls."
)
