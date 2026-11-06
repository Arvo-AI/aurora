"""Regression tests for graph-to-synthesis assembly; no DB or LLM needed."""

import yaml

from chat.backend.agent.orchestrator.evidence_synthesis import combine_findings_with_graphs


def finding(agent_id: str, graph=None, *, padding: int = 0) -> str:
    metadata = {
        "agent_id": agent_id,
        "purpose": "Investigate regression",
        "status": "succeeded",
        "tools_used": [],
        "citations": [],
        "self_assessed_strength": "moderate",
        "follow_ups_suggested": [],
    }
    if graph is not None:
        metadata["evidence_graph"] = graph
    return (
        "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n"
        "## Summary\nRegression found\n"
        "## Evidence\nHTTP 500 spike\n"
        "## Reasoning\nA deploy may explain the spike\n"
        "## What I ruled out\nNo DNS outage\n"
        + "X" * padding
    )


def graph():
    return {
        "evidence": [{"id": "E1", "observation": "HTTP 500 spike", "source": "logs", "reference": "log:123"}],
        "hypotheses": [{"id": "H1", "statement": "Deployment regression"}],
        "relationships": [{"evidence_id": "E1", "hypothesis_id": "H1", "relation": "supports"}],
    }


def test_legacy_findings_unchanged():
    body = finding("agent-a")
    assert combine_findings_with_graphs([body], {"agent-a": body}) == body


def test_graph_reaches_synthesis_with_agent_provenance():
    body = finding("agent-a", graph())
    result = combine_findings_with_graphs([body], {"agent-a": body})
    assert "Agent agent-a:" in result
    assert "Hypothesis H1" in result
    assert "supports: E1" in result
    assert "log:123" in result
    assert result.index("Agent agent-a:") < result.index("=== SUB-AGENT FINDINGS ===")


def test_same_ids_from_different_agents_are_scoped():
    a = finding("agent-a", graph())
    b = finding("agent-b", graph())
    result = combine_findings_with_graphs([a, b], {"agent-a": a, "agent-b": b})
    assert "Agent agent-a:" in result
    assert "Agent agent-b:" in result
    assert result.count("Hypothesis H1") >= 2


def test_graph_survives_large_findings():
    body = finding("agent-a", graph(), padding=20000)
    result = combine_findings_with_graphs([body], {"agent-a": body})
    assert len(result) <= 12000
    assert "Agent agent-a:" in result[:3000]
    assert "supports: E1" in result[:3000]


def test_invalid_graph_does_not_crash_synthesis():
    invalid = graph()
    invalid["relationships"][0]["evidence_id"] = "missing"
    body = finding("agent-a", invalid)
    assert combine_findings_with_graphs([body], {"agent-a": body}) == body


def test_empty_graph_does_not_change_findings():
    body = finding("agent-a", {"evidence": [], "hypotheses": [], "relationships": []})
    assert combine_findings_with_graphs([body], {"agent-a": body}) == body

def test_global_graph_budget_preserves_complete_lines():
    from chat.backend.agent.orchestrator.evidence_synthesis import (
        combine_findings_with_graphs,
    )

    import yaml

    bodies = []
    by_agent = {}

    for index in range(5):
        agent_id = f"agent-{index}"
        body = (
            "---\n"
            + yaml.safe_dump({
                "agent_id": agent_id,
                "purpose": "Investigate",
                "status": "completed",
                "tools_used": [],
                "citations": [],
                "self_assessed_strength": "medium",
                "follow_ups_suggested": [],
                "evidence_graph": {
                    "evidence": [{
                        "id": "E1",
                        "observation": "X" * 600,
                        "source": "logs",
                        "reference": "trace-123",
                    }],
                    "hypotheses": [{
                        "id": "H1",
                        "statement": "Possible service failure",
                    }],
                    "relationships": [{
                        "evidence_id": "E1",
                        "hypothesis_id": "H1",
                        "relation": "supports",
                    }],
                },
            }, sort_keys=False)
            + "---\n"
            + "## Summary\nTest\n"
            + "## Evidence\nTest\n"
            + "## Reasoning\nTest\n"
            + "## What I ruled out\nTest\n"
        )
        bodies.append(body)
        by_agent[agent_id] = body

    combined = combine_findings_with_graphs(bodies, by_agent)

    assert len(combined) <= 12000

    graph_section = combined.split("=== SUB-AGENT FINDINGS ===")[0]

    for line in graph_section.splitlines():
        if "supports:" in line:
            assert "ref=trace-123)" in line
