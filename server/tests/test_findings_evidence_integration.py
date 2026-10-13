"""Integration checks for optional evidence_graph in findings.md frontmatter."""

import pytest
import yaml

from chat.backend.agent.orchestrator.findings_schema import (
    FindingsValidationError,
    make_stub,
    parse_findings,
)


SECTIONS = (
    "## Summary\nInvestigation complete.\n\n"
    "## Evidence\nA log entry was inspected.\n\n"
    "## Reasoning\nThe log supports the hypothesis.\n\n"
    "## What I ruled out\nNothing yet.\n"
)


def findings(graph=None, **overrides):
    meta = {
        "agent_id": "sa_w1_1",
        "purpose": "Investigate errors",
        "status": "succeeded",
        "tools_used": ["logs"],
        "citations": ["log:123"],
        "self_assessed_strength": "moderate",
        "follow_ups_suggested": [],
    }
    if graph is not None:
        meta["evidence_graph"] = graph
    meta.update(overrides)
    return f"---\n{yaml.safe_dump(meta, sort_keys=False)}---\n{SECTIONS}"


@pytest.fixture
def graph():
    return {
        "evidence": [
            {"id": "E1", "observation": "HTTP 500 spike", "source": "logs", "reference": "log:123"}
        ],
        "hypotheses": [{"id": "H1", "statement": "Service regression"}],
        "relationships": [{"evidence_id": "E1", "hypothesis_id": "H1", "relation": "supports"}],
    }


def test_legacy_findings_without_graph():
    parsed = parse_findings(findings())
    assert parsed["agent_id"] == "sa_w1_1"
    assert "evidence_graph" not in parsed


def test_valid_graph_is_normalized(graph):
    parsed = parse_findings(findings(graph))
    assert parsed["evidence_graph"]["relationships"][0]["relation"] == "supports"
    assert parsed["evidence_graph"]["evidence"][0]["verification"] == "unverified"


def test_invalid_evidence_reference_is_rejected(graph):
    graph["relationships"][0]["evidence_id"] = "missing"
    with pytest.raises(FindingsValidationError, match="invalid evidence_graph"):
        parse_findings(findings(graph))


def test_graph_size_limit_is_enforced(graph):
    graph["evidence"] = [
        {"id": f"E{i}", "observation": "event", "source": "logs", "reference": "log:123"}
        for i in range(65)
    ]
    with pytest.raises(FindingsValidationError, match="invalid evidence_graph"):
        parse_findings(findings(graph))


@pytest.mark.parametrize("key", ["tools_used", "citations", "follow_ups_suggested"])
def test_all_required_list_fields_are_checked(key):
    with pytest.raises(FindingsValidationError, match=key):
        parse_findings(findings(**{key: "not a list"}))


def test_stub_is_still_accepted():
    stub = make_stub("sa_w1_1", "investigator", "incident-1", "Investigate", "failed")
    assert parse_findings(stub)["status"] == "failed"
