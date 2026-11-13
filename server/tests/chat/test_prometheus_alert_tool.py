"""Exercise the production tool factory with real LangChain and promtool."""

import json
import shutil
from pathlib import Path

import pytest
from pydantic import ValidationError

from chat.backend.agent.tools.prometheus_alert_tool import (
    VerifyPrometheusAlertArgs,
    build_prometheus_alert_tool,
)
from chat.backend.agent.access.mode_access_controller import ModeAccessController


EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "tested-alerts"


def test_registered_tool_is_available_in_read_only_mode():
    tool = build_prometheus_alert_tool()
    assert tool.name == "verify_prometheus_alert"
    assert ModeAccessController.filter_tools("ask", [tool]) == [tool]
    assert ModeAccessController.is_tool_allowed("ask", tool.name)


def test_tool_exposes_only_data_not_command_or_binary_path():
    schema = VerifyPrometheusAlertArgs.model_json_schema()
    assert set(schema["properties"]) == {"rules_yaml", "tests_yaml", "evidence_kind"}


def test_schema_rejects_false_provenance_claim():
    with pytest.raises(ValidationError):
        VerifyPrometheusAlertArgs(rules_yaml="x", tests_yaml="y", evidence_kind="proven")


def test_production_factory_preserves_context_wrapper():
    seen = []

    def wrapped(rules_yaml, tests_yaml, evidence_kind="synthetic"):
        seen.append((rules_yaml, tests_yaml, evidence_kind))
        return "wrapped"

    tool = build_prometheus_alert_tool(wrapped)
    assert tool.invoke({"rules_yaml": "rules", "tests_yaml": "tests"}) == "wrapped"
    assert seen == [("rules", "tests", "synthetic")]


def test_tool_invocation_executes_real_promtool_without_connectors():
    if not shutil.which("promtool"):
        pytest.skip("Dedicated CI supplies the real engine")
    tool = build_prometheus_alert_tool()
    result = json.loads(tool.invoke({
        "rules_yaml": (EXAMPLES / "rules-corrected.yml").read_text(),
        "tests_yaml": (EXAMPLES / "tests.yml").read_text(),
        "evidence_kind": "synthetic",
    }))
    assert result["status"] == "passed", result
    assert set(result["exports"]) == {"rules.yml", "tests.yml"}
    assert len(result["input_hashes"]["tests_sha256"]) == 64
