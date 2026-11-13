"""Behavioral checks use real promtool when installed; validation needs no binary."""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from services.alert_validation import evaluate_alert


EXAMPLES = Path(__file__).resolve().parents[3] / "examples" / "tested-alerts"


@pytest.fixture
def inputs():
    return (
        (EXAMPLES / "rules-corrected.yml").read_text(),
        (EXAMPLES / "tests.yml").read_text(),
    )


@pytest.fixture
def promtool():
    binary = shutil.which("promtool")
    if not binary:
        pytest.skip("Real promtool required; dedicated alert-validation CI installs it")
    return binary


@pytest.mark.parametrize("candidate,expected", [
    ("missed", "failed"), ("noisy", "failed"), ("corrected", "passed"),
])
def test_real_engine_distinguishes_detection_and_noise(candidate, expected, inputs, promtool):
    rules = (EXAMPLES / f"rules-{candidate}.yml").read_text()
    result = evaluate_alert(rules, inputs[1], promtool_path=promtool)
    assert result["status"] == expected, result
    assert result["evidence_kind"] == "synthetic"
    assert result["elapsed_ms"] >= 0
    if expected == "failed":
        assert "FAILED" in result["output"]


def test_same_expectations_are_used_for_all_three_rules(inputs, promtool):
    receipts = [evaluate_alert((EXAMPLES / f"rules-{candidate}.yml").read_text(),
                              inputs[1], promtool_path=promtool)
                for candidate in ("missed", "noisy", "corrected")]
    assert len({r["input_hashes"]["tests_sha256"] for r in receipts}) == 1
    assert len({r["input_hashes"]["rules_sha256"] for r in receipts}) == 3


def test_exported_files_run_without_aurora(inputs, promtool, tmp_path):
    result = evaluate_alert(*inputs, promtool_path=promtool)
    assert result["status"] == "passed", result
    for name, content in result["exports"].items():
        assert name in {"rules.yml", "tests.yml"}
        (tmp_path / name).write_text(content)
        key = "rules_sha256" if name == "rules.yml" else "tests_sha256"
        assert hashlib.sha256(content.encode()).hexdigest() == result["input_hashes"][key]
    run = subprocess.run([promtool, "test", "rules", "tests.yml"], cwd=tmp_path,
                         env={"LANG": "C", "GOMAXPROCS": "2"},
                         capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stdout + run.stderr


def test_missing_binary_is_error_not_a_pass(inputs):
    result = evaluate_alert(*inputs, promtool_path="/nonexistent/aurora-promtool")
    assert result["status"] == "error"


def test_invalid_promql_cannot_pass(inputs, promtool):
    rules = yaml.safe_load(inputs[0])
    rules["groups"][0]["rules"][0]["expr"] = "sum("
    result = evaluate_alert(yaml.safe_dump(rules), inputs[1], promtool_path=promtool)
    assert result["status"] in {"failed", "error"}


@pytest.mark.parametrize("mutation", [
    "external_rules", "glob_rules", "missing_positive", "missing_negative",
    "wrong_alert", "no_assertions", "long_evaluation", "expanded_samples",
    "recording_rule", "second_rule", "unknown_test_field", "nonfinite_sample",
    "past_sample_window",
])
def test_invalid_or_misleading_suites_rejected_before_execution(inputs, monkeypatch, mutation):
    rules, tests = (yaml.safe_load(value) for value in inputs)
    alert = rules["groups"][0]["rules"][0]
    if mutation == "external_rules":
        tests["rule_files"] = ["/etc/passwd"]
    elif mutation == "glob_rules":
        tests["rule_files"] = ["../*.yml"]
    elif mutation == "missing_positive":
        for case in tests["tests"]:
            for check in case["alert_rule_test"]:
                check["exp_alerts"] = []
    elif mutation == "missing_negative":
        tests["tests"] = [tests["tests"][0]]
        tests["tests"][0]["alert_rule_test"] = [
            check for check in tests["tests"][0]["alert_rule_test"] if check["exp_alerts"]
        ]
    elif mutation == "wrong_alert":
        tests["tests"][0]["alert_rule_test"][0]["alertname"] = "AnotherAlert"
    elif mutation == "no_assertions":
        tests["tests"][0]["alert_rule_test"] = []
    elif mutation == "long_evaluation":
        tests["tests"][0]["alert_rule_test"][0]["eval_time"] = "100000h"
    elif mutation == "expanded_samples":
        tests["tests"][0]["input_series"][0]["values"] = "1+1x999999999"
    elif mutation == "recording_rule":
        alert["record"] = alert.pop("alert")
    elif mutation == "second_rule":
        rules["groups"][0]["rules"].append(dict(alert))
    elif mutation == "unknown_test_field":
        tests["tests"][0]["promql_expr_test"] = []
    elif mutation == "nonfinite_sample":
        tests["tests"][0]["input_series"][0]["values"] = "NaN +Inf"
    elif mutation == "past_sample_window":
        tests["tests"][0]["alert_rule_test"][-1]["eval_time"] = "50m"

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid input reached subprocess execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = evaluate_alert(yaml.safe_dump(rules), yaml.safe_dump(tests), promtool_path="/unused")
    assert result["status"] == "error", result


@pytest.mark.parametrize("bad", [
    "groups: &a [*a]", "!!python/object/apply:os.system ['echo forbidden']",
    "[" * 1000 + "]" * 1000, "x" * 100000,
], ids=["aliases", "python-object", "deep-nesting", "oversized-input"])
def test_hostile_yaml_is_rejected_without_execution(inputs, monkeypatch, bad):
    def forbidden(*args, **kwargs):
        pytest.fail("Invalid YAML reached subprocess execution")

    monkeypatch.setattr(subprocess, "run", forbidden)
    result = evaluate_alert(bad, inputs[1], promtool_path="/unused")
    assert result["status"] == "error", result


def test_unknown_provenance_is_rejected(inputs):
    result = evaluate_alert(*inputs, evidence_kind="verified-production", promtool_path="/unused")
    assert result["status"] == "error"


def test_timeout_is_error_not_test_success(inputs, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    result = evaluate_alert(*inputs, promtool_path="/unused/promtool")
    assert result["status"] == "error"
    assert "limit" in result["output"]


def test_crashed_executor_is_error_not_an_alert_test_failure(inputs, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args[0], -9))
    result = evaluate_alert(*inputs, promtool_path="/unused/promtool")
    assert result["status"] == "error"
    assert result["exit_code"] == -9
    assert "signal 9" in result["output"]


def test_subprocess_is_isolated_bounded_and_temporary_files_are_removed(inputs, monkeypatch):
    from types import SimpleNamespace
    seen = {}
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-should-not-reach-promtool")

    def capture(argv, **kwargs):
        seen.update(kwargs)
        assert argv[1:] == ["test", "rules", "tests.yml"]
        assert not kwargs.get("shell")
        assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
        assert kwargs["timeout"] <= 15
        assert (Path(kwargs["cwd"]) / "rules.yml").is_file()
        assert (Path(kwargs["cwd"]) / "tests.yml").is_file()
        kwargs["stdout"].write(b"x" * 20000)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", capture)
    result = evaluate_alert(*inputs, promtool_path="/unused/promtool")
    assert result["status"] == "passed"
    assert len(result["output"]) < 5000
    assert "truncated" in result["output"]
    assert not Path(seen["cwd"]).exists()


def test_duplicate_yaml_keys_cannot_rewrite_expectations(inputs):
    tests = inputs[1] + "\ntests: []\n"
    result = evaluate_alert(inputs[0], tests, promtool_path="/unused/promtool")
    assert result["status"] == "error"
    assert "unique" in result["output"]
