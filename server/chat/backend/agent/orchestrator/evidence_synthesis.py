"""Pure, bounded evidence-graph context assembly for RCA synthesis."""

import logging
from collections.abc import Mapping

from chat.backend.agent.orchestrator.evidence_graph import EvidenceGraph
from chat.backend.agent.orchestrator.findings_schema import (
    FindingsValidationError,
    parse_findings,
)

logger = logging.getLogger(__name__)

_GRAPH_BUDGET = 3000
_PER_AGENT_BUDGET = 1000
_FINDINGS_BUDGET = 12000


def combine_findings_with_graphs(
    finding_bodies: list[str], body_by_agent: Mapping[str, str]
) -> str:
    """Prepend agent-scoped graph evidence without exceeding findings budget.

    Legacy findings retain their original concatenation and truncation behavior.
    Invalid optional graphs are skipped; original findings remain available.
    """
    combined = "\n\n---\n\n".join(finding_bodies)
    contexts: list[str] = []
    used = 0

    for agent_id, body in body_by_agent.items():
        if used >= _GRAPH_BUDGET:
            break

        try:
            meta = parse_findings(body)
            graph_data = meta.get("evidence_graph")
            if graph_data is None:
                continue

            graph = EvidenceGraph.model_validate(graph_data)
            context = graph.synthesis_context(max_chars=_PER_AGENT_BUDGET)
            if not context:
                continue

            block = f"Agent {agent_id}:\n{context}"
            remaining = _GRAPH_BUDGET - used

            if contexts:
                remaining -= 1  # separator newline

            if remaining <= 0:
                break

            if len(block) > remaining:
                marker = "[graph context truncated]"
                budget = remaining - len(marker) - 1

                if budget <= 0:
                    break

                kept_lines: list[str] = []
                kept_chars = 0

                for line in block.splitlines():
                    extra = len(line) + (1 if kept_lines else 0)
                    if kept_chars + extra > budget:
                        break
                    kept_lines.append(line)
                    kept_chars += extra

                if not kept_lines:
                    break

                block = "\n".join(kept_lines) + "\n" + marker

            contexts.append(block)
            used += len(block) + (1 if len(contexts) > 1 else 0)

        except (FindingsValidationError, ValueError, TypeError) as exc:
            logger.warning(
                "synthesis: skipping invalid graph for agent %s: %s",
                agent_id,
                exc,
            )

    if not contexts:
        return combined

    prefix = (
        "=== VALIDATED EVIDENCE RELATIONSHIPS (agent-scoped) ===\n"
        + "\n".join(contexts)
        + "\n\n=== SUB-AGENT FINDINGS ===\n"
    )

    # Reserve the prefix so the synthesis prompt's 12k character cap
    # cannot drop the graph context.
    return prefix + combined[: max(0, _FINDINGS_BUDGET - len(prefix))]
