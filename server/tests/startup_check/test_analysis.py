"""Tests for the advisory mini-RCA (Layer 2) in ``analysis.py``.

The model is faked at the provider boundary (``create_chat_model`` is imported
inside ``_run_with_model``), exactly like the fact interpreter. These cover the
contract: the pass fails closed, the tool loop is bounded by the step budget,
file content is truncated, and observations are tagged ``source="agent"`` with
hallucinated paths and bad severities dropped — never flipping the verdict.
"""

import services.startup_check.analysis as analysis
from services.startup_check.analysis import (
    MAX_LINES_PER_FILE,
    MAX_STEPS,
    AgentAnalysis,
    _Observation,
    _list_files_text,
    _read_file_text,
    _search_text,
    _to_findings,
    _truncate_file,
    run_agent_analysis,
)


class _FakeToolResponse:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _FakeStructured:
    def __init__(self, parsed):
        self._parsed = parsed
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return {"parsed": self._parsed, "raw": None}


class _FakeModel:
    """Stands in for create_chat_model(). ``bind_tools`` returns self, whose
    ``invoke`` emits tool-call responses; ``with_structured_output`` returns a
    separate structured invoker."""

    def __init__(self, tool_calls=None, parsed=None):
        self._tool_calls = tool_calls if tool_calls is not None else []
        self.structured = _FakeStructured(parsed)
        self.invoke_calls = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.invoke_calls += 1
        return _FakeToolResponse(list(self._tool_calls))

    def with_structured_output(self, schema, include_raw=False, method=None):
        return self.structured


def _patch(monkeypatch, model, chain=("test/premium",)):
    monkeypatch.setattr(analysis, "_model_chain", lambda: list(chain))
    monkeypatch.setattr(
        "chat.backend.agent.providers.create_chat_model",
        lambda *a, **kw: model,
        raising=False,
    )


_FILES = {
    "docker-compose.yml": "version: '3'\nservices:\n  api:\n    image: api:latest\n",
    "k8s/deploy.yaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: api\n",
}


# ---------------------------------------------------------------------------
# Fail-closed
# ---------------------------------------------------------------------------


def test_empty_files_fails_closed_without_model(monkeypatch):
    result = run_agent_analysis("user-1", "a/b", {}, None)
    assert result["agent_used"] is False
    assert result["agent_observations"] == []
    assert "no files" in result["agent_error"]


def test_entire_chain_dead_fails_closed(monkeypatch):
    import chat.backend.agent.providers as providers

    def boom(*a, **kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(providers, "create_chat_model", boom)
    monkeypatch.setattr(analysis, "_model_chain", lambda: ["test/opus", "test/free"])

    result = run_agent_analysis("user-1", "a/b", _FILES, None)

    assert result["agent_used"] is False
    assert result["agent_observations"] == []
    assert result["agent_error"] == "agent models unavailable"


def test_unparsable_output_fails_closed(monkeypatch):
    _patch(monkeypatch, _FakeModel(tool_calls=[], parsed=None))
    result = run_agent_analysis("user-1", "a/b", _FILES, None)
    assert result["agent_used"] is False
    assert result["agent_error"] == "agent models unavailable"


# ---------------------------------------------------------------------------
# Tool loop + bounds
# ---------------------------------------------------------------------------


def test_tool_loop_respects_step_budget(monkeypatch):
    """A model that keeps asking for tools is cut off at MAX_STEPS, not forever."""
    always = [{"name": "list_files", "args": {}, "id": "1"}]
    parsed = AgentAnalysis(summary="ok")
    model = _FakeModel(tool_calls=always, parsed=parsed)
    _patch(monkeypatch, model)

    run_agent_analysis("user-1", "a/b", _FILES, None)

    assert model.invoke_calls == MAX_STEPS
    assert model.structured.calls == 1


def test_no_tool_calls_synthesizes(monkeypatch):
    parsed = AgentAnalysis(summary="looks fine", observations=[])
    model = _FakeModel(tool_calls=[], parsed=parsed)
    _patch(monkeypatch, model)

    result = run_agent_analysis("user-1", "a/b", _FILES, None)

    assert result["agent_used"] is True
    assert result["agent_summary"] == "looks fine"
    assert result["agent_observations"] == []
    assert model.invoke_calls == 1


# ---------------------------------------------------------------------------
# Observation validation + tagging
# ---------------------------------------------------------------------------


def test_observations_tagged_and_hallucinated_paths_dropped():
    parsed = AgentAnalysis(
        summary="two real, one fake",
        observations=[
            _Observation(severity="HIGH", file_path="docker-compose.yml", line=2,
                         title="latest tag", explanation="no pinned version"),
            _Observation(severity="medium", file_path="k8s/deploy.yaml", line=1,
                         title="ok", explanation="fine"),
            _Observation(severity="LOW", file_path="not/in/repo.yml", line=1,
                         title="ghost", explanation="must be dropped"),
            _Observation(severity="BOGUS", file_path="docker-compose.yml",
                         title="bad severity", explanation="must be dropped"),
        ],
    )

    findings = _to_findings(parsed, _FILES)

    assert len(findings) == 2
    assert all(f["source"] == "agent" for f in findings)
    assert all(f["rule"] == "agent" for f in findings)
    assert findings[0]["severity"] == "HIGH"
    assert findings[1]["severity"] == "MEDIUM"  # normalized from "medium"
    assert {f["file_path"] for f in findings} == {"docker-compose.yml", "k8s/deploy.yaml"}


def test_explicit_env_model_disables_fallback(monkeypatch):
    monkeypatch.setenv("STARTUP_CHECK_AGENT_MODEL", "test/only")
    assert analysis._model_chain() == ["test/only"]
    monkeypatch.delenv("STARTUP_CHECK_AGENT_MODEL")


# ---------------------------------------------------------------------------
# Tool behaviors (unit)
# ---------------------------------------------------------------------------


def test_read_file_truncates_long_files():
    content = "\n".join(f"line-{i}" for i in range(500))
    out = _truncate_file(content, "big.yml")

    assert out.count("\n") <= MAX_LINES_PER_FILE + 1  # + note line
    assert "line-0" in out
    assert "line-499" not in out
    assert f"500 lines" in out


def test_tools_list_and_read_and_search():
    listing = _list_files_text(_FILES)
    assert "docker-compose.yml" in listing
    assert "k8s/deploy.yaml" in listing

    read = _read_file_text(_FILES, "docker-compose.yml")
    assert "api:latest" in read

    missing = _read_file_text(_FILES, "nope.yml")
    assert "file not found" in missing

    hits = _search_text(_FILES, "image:")
    assert "docker-compose.yml" in hits
