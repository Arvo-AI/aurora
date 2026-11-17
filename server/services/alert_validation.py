"""Evaluate one Prometheus alert against explicit examples, using real promtool.

This deliberately supports a small subset of promtool's YAML format. No caller
paths, external rule files, shell commands, or generated executable code run.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

import yaml

MAX_INPUT_BYTES = 8_000
MAX_OUTPUT_BYTES = 4_000
TIMEOUT_SECONDS = 10
MAX_SAMPLES = 4_096
MAX_DURATION_SECONDS = 3_600
_DURATION = re.compile(r"(?:\d+(?:ms|s|m|h))+")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently changing a test."""

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(k, deep=deep) for k, _ in node.value]
        if any(not isinstance(k, str) for k in keys) or len(set(keys)) != len(keys):
            raise ValueError("YAML mapping keys must be unique strings")
        return super().construct_mapping(node, deep=deep)


def _load(text, label):
    if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise ValueError(f"{label} must be text of at most {MAX_INPUT_BYTES} bytes")
    depth = events = 0
    for event in yaml.parse(text):
        events += 1
        if isinstance(event, yaml.events.AliasEvent) or getattr(event, "anchor", None):
            raise ValueError("YAML anchors and aliases are not supported")
        if isinstance(event, yaml.events.CollectionStartEvent):
            depth += 1
        elif isinstance(event, yaml.events.CollectionEndEvent):
            depth -= 1
        if depth > 12 or events > 20_000:
            raise ValueError("YAML is too deeply nested or too large")
    return yaml.load(text, Loader=_UniqueLoader)


def _mapping(value, allowed, required, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    if set(value) - set(allowed) or set(required) - set(value):
        raise ValueError(f"{label} has unknown or missing fields; allowed: {', '.join(allowed)}")
    return value


def _list(value, maximum, label, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{label} must contain {minimum} to {maximum} items")
    return value


def _text(value, label, maximum=4_096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} must be nonempty text of at most {maximum} characters")
    return value


def _duration(value, label, allow_zero=False):
    if not isinstance(value, str) or not _DURATION.fullmatch(value):
        raise ValueError(f"{label} must be a duration using ms, s, m, or h")
    units = {"ms": .001, "s": 1, "m": 60, "h": 3_600}
    parts = re.findall(r"(\d+)(ms|s|m|h)", value)
    if any(len(n) > 7 for n, _ in parts):
        raise ValueError(f"{label} exceeds the one-hour limit")
    seconds = sum(int(n) * units[u] for n, u in parts)
    if seconds > MAX_DURATION_SECONDS or seconds < (0 if allow_zero else 1):
        raise ValueError(f"{label} must be {'0' if allow_zero else '1'} to 3600 seconds")
    return seconds


def _labels(value, label):
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError(f"{label} must be a mapping with at most 32 entries")
    for key, text in value.items():
        _text(key, label, 128)
        if not isinstance(text, str) or len(text) > 1_024:
            raise ValueError(f"{label} values must be strings of at most 1024 characters")


def _validate(rules_yaml, tests_yaml):
    rules = _mapping(_load(rules_yaml, "rules_yaml"), ["groups"], ["groups"], "rules")
    group = _list(rules["groups"], 1, "groups")[0]
    _mapping(group, ["name", "interval", "rules"], ["name", "rules"], "group")
    _text(group["name"], "group name", 128)
    if "interval" in group:
        _duration(group["interval"], "group interval")
    rule = _list(group["rules"], 1, "rules")[0]
    _mapping(rule, ["alert", "expr", "for", "labels", "annotations"], ["alert", "expr"], "alert rule")
    alertname = _text(rule["alert"], "alert name", 128)
    _text(rule["expr"], "expression")
    if "for" in rule:
        _duration(rule["for"], "for", allow_zero=True)
    for field in ("labels", "annotations"):
        if field in rule:
            _labels(rule[field], field)

    tests = _mapping(_load(tests_yaml, "tests_yaml"),
                     ["rule_files", "evaluation_interval", "tests"], ["tests"], "test suite")
    if "rule_files" in tests and tests["rule_files"] != ["rules.yml"]:
        raise ValueError("rule_files must be omitted or exactly ['rules.yml']; paths/globs are forbidden")
    tests["rule_files"] = ["rules.yml"]
    _duration(tests.get("evaluation_interval", "1m"), "evaluation_interval")
    samples = assertions = 0
    has_firing = has_nonfiring = False
    for case in _list(tests["tests"], 8, "tests"):
        _mapping(case, ["name", "interval", "input_series", "alert_rule_test"],
                 ["input_series", "alert_rule_test"], "test case")
        if "name" in case:
            _text(case["name"], "case name", 128)
        interval = _duration(case.get("interval", tests.get("evaluation_interval", "1m")), "series interval")
        covered_until = MAX_DURATION_SECONDS
        for series in _list(case["input_series"], 16, "input_series"):
            _mapping(series, ["series", "values"], ["series", "values"], "series")
            _text(series["series"], "series selector", 1_024)
            values = _text(series["values"], "series values", MAX_INPUT_BYTES).split()
            if not values or any(not _NUMBER.fullmatch(v) or not math.isfinite(float(v)) for v in values):
                raise ValueError("values must be explicit finite numeric samples separated by spaces")
            samples += len(values)
            if samples > MAX_SAMPLES or (len(values) - 1) * interval > MAX_DURATION_SECONDS:
                raise ValueError("Series exceed the 4096-sample or one-hour limit")
            covered_until = min(covered_until, (len(values) - 1) * interval)
        for assertion in _list(case["alert_rule_test"], 16, "alert_rule_test"):
            _mapping(assertion, ["eval_time", "alertname", "exp_alerts"],
                     ["eval_time", "alertname", "exp_alerts"], "alert assertion")
            if _duration(assertion["eval_time"], "eval_time", allow_zero=True) > covered_until:
                raise ValueError("Every eval_time must be within the supplied sample span of every series")
            if assertion["alertname"] != alertname:
                raise ValueError("Every assertion must target the single supplied alert name")
            expected = _list(assertion["exp_alerts"], 16, "exp_alerts", minimum=0)
            has_firing |= bool(expected)
            has_nonfiring |= not expected
            assertions += 1
            if assertions > 32:
                raise ValueError("At most 32 assertions are supported")
            for alert in expected:
                _mapping(alert, ["exp_labels", "exp_annotations"], [], "expected alert")
                for field in ("exp_labels", "exp_annotations"):
                    if field in alert:
                        _labels(alert[field], field)
    if not has_firing or not has_nonfiring:
        raise ValueError("Include at least one firing and one nonfiring assertion")
    exports = {"rules.yml": yaml.safe_dump(rules, sort_keys=False),
               "tests.yml": yaml.safe_dump(tests, sort_keys=False)}
    if any(len(value.encode("utf-8")) > MAX_INPUT_BYTES for value in exports.values()):
        raise ValueError("Each canonical YAML export must fit within 8000 bytes")
    return exports


def evaluate_alert(rules_yaml: str, tests_yaml: str, *, evidence_kind="synthetic", promtool_path=None) -> dict:
    """Run a bounded, offline promtool check; passing proves only these examples.

    ``recorded`` means the caller labels the samples as recorded; this service
    cannot authenticate their provenance. Hashes identify the exact exported
    inputs, so the same tests can be held fixed while comparing candidate rules.
    ``promtool_path`` is for trusted callers/tests, never part of the agent schema.
    """
    start = time.monotonic()
    result = {"status": "error", "output": "", "elapsed_ms": 0,
              "input_hashes": {}, "evidence_kind": evidence_kind, "exports": {}}
    try:
        if evidence_kind not in ("synthetic", "recorded"):
            raise ValueError("evidence_kind must be synthetic or recorded")
        exports = _validate(rules_yaml, tests_yaml)
        result["exports"] = exports
        result["input_hashes"] = {
            "rules_sha256": hashlib.sha256(exports["rules.yml"].encode()).hexdigest(),
            "tests_sha256": hashlib.sha256(exports["tests.yml"].encode()).hexdigest(),
        }
        binary = promtool_path or shutil.which("promtool")
        if not binary:
            raise ValueError("promtool is unavailable; install the pinned Prometheus 3.5.0 tool")
        binary = str(Path(binary).resolve())
        with tempfile.TemporaryDirectory(prefix="aurora-alert-test-") as directory:
            for name, content in exports.items():
                Path(directory, name).write_text(content, encoding="utf-8")
            with tempfile.TemporaryFile() as output:
                try:
                    process = subprocess.run(
                        [binary, "test", "rules", "tests.yml"], cwd=directory,
                        stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                        env={"PATH": os.defpath, "TMPDIR": directory, "LANG": "C", "LC_ALL": "C",
                             "GOMEMLIMIT": "256MiB", "GOMAXPROCS": "2"},
                        timeout=TIMEOUT_SECONDS, check=False,
                    )
                    result["status"] = "passed" if process.returncode == 0 else "failed"
                    result["exit_code"] = process.returncode
                    if process.returncode < 0:
                        result.update(status="error", output=f"promtool terminated by signal {-process.returncode}.\n")
                except subprocess.TimeoutExpired:
                    result["output"] = f"promtool exceeded the {TIMEOUT_SECONDS}-second limit.\n"
                output.seek(0)
                captured = output.read(MAX_OUTPUT_BYTES + 1)
                result["output"] += captured[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
                if len(captured) > MAX_OUTPUT_BYTES:
                    result["output"] += "\n[output truncated]"
    except (ValueError, yaml.YAMLError, OSError) as exc:
        result["output"] = str(exc)[:MAX_OUTPUT_BYTES]
    result["elapsed_ms"] = round((time.monotonic() - start) * 1_000)
    # The agent wrapper caps JSON output. Never return a deceptively truncated
    # reusable suite; report an error if JSON escaping would exceed that cap.
    if len(json.dumps(result)) > 38_000:
        result.update(status="error", output="Serialized result exceeds the tool output budget; use smaller examples.", exports={})
    return result
