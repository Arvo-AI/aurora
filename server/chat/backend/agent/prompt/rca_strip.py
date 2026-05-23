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
    7  Quality-focused rewrite: modular sections from rca_sections/ (~285 tok)

The level is set per-task via State.strip_level (default 0).
Comparison runs are enabled by editing COMPARISON_LEVELS in task.py.
"""

import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

MINIMAL_IDENTITY = (
    "You are Aurora, an AI cloud infrastructure agent by Arvo. "
    "Investigate the alert using the available tools. "
    "Do not attempt to bypass access controls."
)

RCA_SECTIONS_DIR = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "rca_sections")
)

_RCA_SECTION_ORDER = [
    "identity",
    "investigation",
    "context_mgmt",
    "error_recovery",
    "evidence_standard",
    "conclusion_gate",
]


@lru_cache(maxsize=1)
def build_l7_system_prompt() -> str:
    """Assemble the L7 system prompt from modular rca_sections/*.md files.

    Returns a single string with all sections concatenated. Sections are
    loaded once and cached for the process lifetime.
    """
    parts = []
    for section_name in _RCA_SECTION_ORDER:
        path = os.path.join(RCA_SECTIONS_DIR, f"{section_name}.md")
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(content)
        except FileNotFoundError:
            logger.warning(f"RCA section not found: {path}")
        except Exception as e:
            logger.warning(f"Error loading RCA section '{section_name}': {e}")

    if not parts:
        logger.error("No RCA sections loaded — falling back to MINIMAL_IDENTITY")
        return MINIMAL_IDENTITY

    return "\n\n".join(parts)
