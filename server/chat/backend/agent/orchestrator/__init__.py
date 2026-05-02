"""Multi-agent RCA orchestrator.

A lead orchestrator triages each background RCA. For trivial incidents it
falls through to the existing single-agent ReAct loop. For complex
incidents it fans out N parallel read-only sub-agents via LangGraph's
``Send`` API, each with an isolated context window and a curated tool
subset. Each sub-agent writes a schema-validated ``findings.md`` artifact
to object storage; the lead reads only those artifacts (never their
transcripts) and synthesizes.

Gated by the ``ORCHESTRATOR_ENABLED`` env var. When false (default),
``is_orchestrator_enabled()`` returns False and ``workflow._create_workflow``
returns the existing single-node graph unchanged.
"""

import os


def is_orchestrator_enabled() -> bool:
    """Return True iff the multi-agent orchestrator is enabled.

    Single source of truth for the feature flag. Read at graph-build time,
    not at module import — toggling the env var and restarting the worker
    picks up the new value.
    """
    return os.getenv("ORCHESTRATOR_ENABLED", "false").strip().lower() == "true"


__all__ = ["is_orchestrator_enabled"]
