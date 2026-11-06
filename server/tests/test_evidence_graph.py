import pytest
from pydantic import ValidationError

from chat.backend.agent.orchestrator.evidence_graph import EvidenceGraph


def graph(**overrides):
    data = {
        "evidence": [{"id": "E1", "observation": "DB pool full", "source": "metrics", "reference": "query:123"}],
        "hypotheses": [{"id": "H1", "statement": "Connection leak"}],
        "relationships": [{"evidence_id": "E1", "hypothesis_id": "H1", "relation": "supports"}],
    }
    data.update(overrides)
    return EvidenceGraph.model_validate(data)


def test_valid_graph():
    result = graph()
    assert "supports: E1" in result.synthesis_context()


def test_missing_evidence_rejected():
    with pytest.raises(ValidationError, match="unknown evidence ID"):
        graph(relationships=[{"evidence_id": "E99", "hypothesis_id": "H1", "relation": "supports"}])


def test_missing_hypothesis_rejected():
    with pytest.raises(ValidationError, match="unknown hypothesis ID"):
        graph(relationships=[{"evidence_id": "E1", "hypothesis_id": "H99", "relation": "supports"}])


def test_duplicate_nodes_rejected():
    with pytest.raises(ValidationError, match="duplicate evidence ID"):
        graph(evidence=[{"id": "E1", "observation": "a", "source": "s", "reference": "r"}] * 2)


def test_duplicate_edges_rejected():
    edge = {"evidence_id": "E1", "hypothesis_id": "H1", "relation": "supports"}
    with pytest.raises(ValidationError, match="duplicate evidence relationship"):
        graph(relationships=[edge, edge])


def test_invalid_relation_rejected():
    with pytest.raises(ValidationError):
        graph(relationships=[{"evidence_id": "E1", "hypothesis_id": "H1", "relation": "proves"}])


def test_naive_timestamp_rejected():
    with pytest.raises(ValidationError, match="timezone"):
        graph(evidence=[{"id": "E1", "observation": "a", "source": "s", "reference": "r", "observed_at": "2026-10-11T10:00:00"}])


def test_graph_size_bounded():
    with pytest.raises(ValidationError):
        graph(evidence=[{"id": f"E{i}", "observation": "a", "source": "s", "reference": "r"} for i in range(65)])


def test_context_bounded():
    assert len(graph().synthesis_context(max_chars=40)) < 100


def test_empty_graph_allowed():
    assert EvidenceGraph().synthesis_context() == ""

def test_context_truncation_preserves_complete_lines():
    graph = EvidenceGraph.model_validate({
        "evidence": [{
            "id": "E1",
            "observation": "A" * 500,
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
    })

    context = graph.synthesis_context(max_chars=150)

    assert len(context) <= 150
    assert "[graph context truncated]" in context
    assert "supports:" not in context
    assert "Hypothesis H1" in context
