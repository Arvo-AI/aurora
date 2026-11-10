"""Offline alert-rule verification, available without monitoring credentials."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from services.alert_validation import evaluate_alert


class VerifyPrometheusAlertArgs(BaseModel):
    rules_yaml: str = Field(description="Prometheus YAML with exactly one group and one alert rule.")
    tests_yaml: str = Field(description=(
        "promtool test YAML with explicit finite numeric input_series values and alert_rule_test assertions. "
        "Include at least one firing and one nonfiring expectation. rule_files must be omitted or ['rules.yml']. "
        "Hold this suite fixed when comparing candidate rules; do not weaken expectations to obtain a pass."
    ))
    evidence_kind: Literal["synthetic", "recorded"] = Field(
        default="synthetic", description="Label sample provenance honestly; recorded provenance is caller-declared."
    )


def verify_prometheus_alert(rules_yaml: str, tests_yaml: str, evidence_kind="synthetic", user_id=None, **kwargs) -> str:
    """Evaluate supplied examples with promtool; no cloud access or repository writes."""
    return json.dumps(evaluate_alert(rules_yaml, tests_yaml, evidence_kind=evidence_kind))


def build_prometheus_alert_tool(func=None):
    """Shared registration path for the production agent and integration tests."""
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=func or verify_prometheus_alert,
        name="verify_prometheus_alert",
        description=(
            "Test one Prometheus alert against a fixed series of firing and nonfiring examples using real promtool. "
            "Returns passed/failed/error, execution output, input hashes, and exportable rules.yml/tests.yml. "
            "A pass proves only the supplied examples, not production coverage or false-positive rates. "
            "No monitoring connection is required."
        ),
        args_schema=VerifyPrometheusAlertArgs,
    )
