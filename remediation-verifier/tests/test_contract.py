"""Contract tests: the frozen scenario 12 values and the sources behind them.

Case 1 freezes the data file, its provenance table and its decisions. The
loader and the runtime view separation arrive in case 2, over the same file.
Until then these tests are the only thing standing between a silent edit of
the contract and a green build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from verifier import doctor

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = PROJECT_DIR / "contracts"
CONTRACT_FILE = CONTRACTS_DIR / "scenario_12.yaml"
SCHEMA_FILE = CONTRACTS_DIR / "schema.json"
PROVENANCE_FILE = CONTRACTS_DIR / "PROVENANCE.md"
DECISIONS_FILE = CONTRACTS_DIR / "DECISIONS.md"

CHECK_IDS = [
    "no_oomkilled_in_window",
    "restart_count_not_increasing",
    "processing_evidence",
    "service_has_endpoints",
    "endpoints_ready",
    "http_expected_response",
]

ORACLE_HINTS = ("tier", "selector", "endpoint", "BATCH_SIZE", "OOMKilled", "kettle", "teapot")

SOURCE_PATTERN = re.compile(r"^(?:scenarios|app)/.+#L\d+")


class SchemaValidator:
    """The subset of JSON Schema this contract uses, resolved locally.

    Adding jsonschema as a dependency for one file is not worth it, so the
    test suite checks the keywords the schema actually contains and refuses
    the ones it does not know.
    """

    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root

    def errors(self, value: Any, schema: dict[str, Any] | None = None) -> list[str]:
        return self._validate(value, schema if schema is not None else self.root, "$")

    def _resolve(self, reference: str) -> dict[str, Any]:
        if not reference.startswith("#/"):
            raise AssertionError(f"only local references are supported: {reference}")
        node: Any = self.root
        for part in reference.removeprefix("#/").split("/"):
            node = node[part]
        return node  # type: ignore[no-any-return]

    def _validate(self, value: Any, schema: dict[str, Any], where: str) -> list[str]:
        if "$ref" in schema:
            return self._validate(value, self._resolve(str(schema["$ref"])), where)

        if "type" in schema:
            declared = schema["type"]
            names = declared if isinstance(declared, list) else [declared]
            if not any(self._is_type(value, str(name)) for name in names):
                return [f"{where}: expected {declared}, found {type(value).__name__}"]

        errors: list[str] = []
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
        if isinstance(value, str):
            errors.extend(self._check_string(value, schema, where))
        is_number = isinstance(value, int | float) and not isinstance(value, bool)
        if is_number and "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{where}: {value} is below {schema['minimum']}")
        if isinstance(value, dict):
            errors.extend(self._check_object(value, schema, where))
        if isinstance(value, list):
            errors.extend(self._check_array(value, schema, where))
        return errors

    def _check_string(self, value: str, schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']} characters")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            errors.append(f"{where}: does not match {schema['pattern']}")
        return errors

    def _check_object(self, value: dict[str, Any], schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{where}: unexpected key {key!r}" for key in value if key not in properties
            )
        for key, child in properties.items():
            if key in value:
                errors.extend(self._validate(value[key], child, f"{where}.{key}"))
        return errors

    def _check_array(self, value: list[Any], schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(self._validate(item, schema["items"], f"{where}[{index}]"))
        return errors

    def _is_type(self, value: Any, name: str) -> bool:
        if name == "object":
            return isinstance(value, dict)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        raise AssertionError(f"schema uses a type the validator does not know: {name}")


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    return yaml.safe_load(read_text(CONTRACT_FILE))


@pytest.fixture(scope="module")
def oracle(contract: dict[str, Any]) -> dict[str, Any]:
    return contract["oracle_view"]


@pytest.fixture(scope="module")
def faults(oracle: dict[str, Any]) -> dict[str, Any]:
    return oracle["faults"]


@pytest.fixture(scope="module")
def fixes(oracle: dict[str, Any]) -> dict[str, Any]:
    return {fix["id"]: fix for fix in oracle["accepted_fixes"]}


def all_checks(faults: dict[str, Any]) -> list[dict[str, Any]]:
    return [check for fault in faults.values() for check in fault["checks"]]


def cited_paths(oracle: dict[str, Any]) -> set[str]:
    sources = [check["source"] for check in all_checks(oracle["faults"])]
    sources.extend(fix["source"] for fix in oracle["accepted_fixes"])
    paths: set[str] = set()
    for source in sources:
        paths.update(re.findall(r"(?:scenarios|app)/[\w./-]+", source))
    return paths


class TestContractFile:
    def test_the_frozen_records_are_present(self) -> None:
        for path in (CONTRACT_FILE, SCHEMA_FILE, PROVENANCE_FILE, DECISIONS_FILE):
            assert path.is_file(), f"missing {path.relative_to(PROJECT_DIR)}"

    def test_the_contract_matches_the_schema(self) -> None:
        schema = json.loads(read_text(SCHEMA_FILE))
        value = yaml.safe_load(read_text(CONTRACT_FILE))
        assert SchemaValidator(schema).errors(value) == []

    def test_the_scenario_id_matches_the_file_name(self, contract: dict[str, Any]) -> None:
        number = re.fullmatch(r"scenario_(\d{2})\.yaml", CONTRACT_FILE.name)
        assert number is not None
        assert contract["scenario"] == f"{number.group(1)}-two-faults-oom-and-selector"

    def test_no_value_is_left_pending(self) -> None:
        for path in CONTRACTS_DIR.glob("*"):
            assert "PENDIENTE" not in read_text(path), f"{path.name} still has an unfilled value"

    def test_every_contract_file_is_ascii(self) -> None:
        for path in CONTRACTS_DIR.glob("*"):
            read_text(path).encode("ascii")


class TestSourcePin:
    def test_the_lab_commit_is_a_full_sha(self, contract: dict[str, Any]) -> None:
        assert re.fullmatch(r"[0-9a-f]{40}", contract["source"]["commit"])

    def test_the_doctor_and_the_contract_agree_on_the_lab_pin(
        self, contract: dict[str, Any]
    ) -> None:
        assert contract["source"]["commit"] == doctor.LAB_COMMIT

    def test_the_commit_date_is_utc(self, contract: dict[str, Any]) -> None:
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", contract["source"]["commit_date"]
        )

    def test_the_fork_base_is_recorded_separately(self, contract: dict[str, Any]) -> None:
        source = contract["source"]
        assert re.fullmatch(r"[0-9a-f]{40}", source["fork_commit"])
        assert source["fork_commit"] != source["commit"]
        assert source["fork_repo"] == "kpnaranj/aurora"

    def test_the_five_source_files_are_relative_lab_paths(self, contract: dict[str, Any]) -> None:
        files = contract["source"]["files"]
        assert set(files) == {"scenario", "break", "check", "runbook", "app"}
        for path in files.values():
            assert path.startswith(("scenarios/", "app/"))
            assert "\\" not in path
            assert "#" not in path


class TestAgentView:
    def test_the_agent_view_holds_one_key(self, contract: dict[str, Any]) -> None:
        assert set(contract["agent_view"]) == {"alert"}

    def test_the_alert_is_the_lab_alert_text(self, contract: dict[str, Any]) -> None:
        alert = contract["agent_view"]["alert"]
        assert alert.startswith("KubePodCrashLooping [warning] namespace=shop deployment=worker")
        assert alert.endswith("(reason: CrashLoopBackOff).")

    def test_the_agent_view_names_no_second_fault(self, contract: dict[str, Any]) -> None:
        serialized = json.dumps(contract["agent_view"]).lower()
        leaked = [hint for hint in ORACLE_HINTS if hint.lower() in serialized]
        assert leaked == [], f"the agent view leaks oracle vocabulary: {leaked}"


class TestChecks:
    def test_six_checks_are_frozen_in_report_order(self, faults: dict[str, Any]) -> None:
        assert [check["id"] for check in all_checks(faults)] == CHECK_IDS

    def test_each_fault_owns_three_checks(self, faults: dict[str, Any]) -> None:
        assert set(faults) == {"worker-oom", "frontend-selector"}
        for fault in faults.values():
            assert len(fault["checks"]) == 3

    def test_each_check_has_one_unique_id(self, faults: dict[str, Any]) -> None:
        identifiers = [check["id"] for check in all_checks(faults)]
        assert len(identifiers) == len(set(identifiers))

    def test_each_check_cites_a_file_and_a_line(self, faults: dict[str, Any]) -> None:
        for check in all_checks(faults):
            assert SOURCE_PATTERN.match(check["source"]), check["id"]

    def test_the_worker_fault_points_at_the_worker_deployment(self, faults: dict[str, Any]) -> None:
        worker = faults["worker-oom"]
        assert worker["namespace"] == "shop"
        assert worker["deployment"] == "worker"
        assert worker["container"] == "worker"
        assert worker["selector"] == "app=worker"

    def test_processing_evidence_reads_the_batch_size_from_the_deployment(
        self, faults: dict[str, Any]
    ) -> None:
        check = next(
            item for item in faults["worker-oom"]["checks"] if item["id"] == "processing_evidence"
        )
        assert check["type"] == "logs"
        assert check["pattern"] == "processed batch of {batch_size} items"
        assert "BATCH_SIZE" in check["batch_size_source"]
        assert check["min_occurrences"] == 2

    def test_the_frontend_fault_points_at_the_service_and_its_slice(
        self, faults: dict[str, Any]
    ) -> None:
        frontend = faults["frontend-selector"]
        assert frontend["namespace"] == "shop"
        assert frontend["service"] == "frontend"
        assert frontend["endpoint_slice_selector"] == "kubernetes.io/service-name=frontend"
        assert frontend["selector_expected"] == "app=frontend"

    def test_the_http_check_is_parameterized_by_the_contract(self, faults: dict[str, Any]) -> None:
        frontend = faults["frontend-selector"]
        assert frontend["http_route"] == "http://frontend.shop.svc.cluster.local/api/items"
        assert frontend["http_method"] == "GET"
        assert frontend["expected_status"] == 200
        assert frontend["expected_json"] == {"items": ["kettle", "mug", "teapot"]}
        assert frontend["requests"] == 3

    def test_the_observation_window_is_sixty_seconds(self, oracle: dict[str, Any]) -> None:
        assert oracle["observation"]["window_seconds"] == 60

    def test_the_baseline_expects_both_faults(self, oracle: dict[str, Any]) -> None:
        assert oracle["baseline"]["expected_faults"] == ["worker-oom", "frontend-selector"]


class TestFixesAndVariants:
    def test_the_four_fixes_are_frozen(self, fixes: dict[str, Any]) -> None:
        assert list(fixes) == ["full", "partial_worker_only", "partial_frontend_only", "noop"]

    def test_every_fix_declares_where_it_comes_from(self, fixes: dict[str, Any]) -> None:
        for identifier, fix in fixes.items():
            assert fix["source"], identifier
            if identifier == "noop":
                assert fix["source"] == "deliberate absence of remediation"
                assert fix["steps"] == []
            else:
                assert fix["source"].startswith("scenarios/"), identifier
                assert fix["steps"], identifier

    def test_the_full_fix_follows_the_runbook(self, fixes: dict[str, Any]) -> None:
        steps = fixes["full"]["steps"]
        assert len(steps) == 2
        assert "--type=json" in steps[0]
        assert "/spec/selector/tier" in steps[0]
        assert steps[1] == "kubectl -n shop set env deploy/worker BATCH_SIZE=500"

    def test_no_fix_reintroduces_the_injected_batch_size(self, fixes: dict[str, Any]) -> None:
        for fix in fixes.values():
            assert all("BATCH_SIZE=2500" not in step for step in fix["steps"])

    def test_each_partial_fix_is_one_half_of_the_full_fix(self, fixes: dict[str, Any]) -> None:
        full = fixes["full"]["steps"]
        assert fixes["partial_frontend_only"]["steps"] == [full[0]]
        assert fixes["partial_worker_only"]["steps"] == [full[1]]

    def test_five_variants_point_at_the_frozen_fixes(
        self, oracle: dict[str, Any], fixes: dict[str, Any]
    ) -> None:
        variants = oracle["variants"]
        assert list(variants) == [
            "baseline",
            "full",
            "partial_worker_only",
            "partial_frontend_only",
            "noop",
        ]
        assert variants["baseline"] == {"kind": "reproduction", "fix": None}
        for name, variant in variants.items():
            if name == "baseline":
                continue
            assert variant["kind"] == "remediation"
            assert variant["fix"] in fixes


class TestProvenance:
    def test_the_table_cites_every_path_the_contract_uses(self, oracle: dict[str, Any]) -> None:
        provenance = read_text(PROVENANCE_FILE)
        missing = sorted(path for path in cited_paths(oracle) if path not in provenance)
        assert missing == [], f"paths with no provenance row: {missing}"

    def test_the_table_names_every_check_and_fix(self, oracle: dict[str, Any]) -> None:
        provenance = read_text(PROVENANCE_FILE)
        missing = [
            check["id"] for check in all_checks(oracle["faults"]) if check["id"] not in provenance
        ]
        missing.extend(fix["id"] for fix in oracle["accepted_fixes"] if fix["id"] not in provenance)
        assert missing == [], f"elements with no provenance row: {missing}"

    def test_the_table_carries_the_pinned_commit(self, contract: dict[str, Any]) -> None:
        assert contract["source"]["commit"] in read_text(PROVENANCE_FILE)

    def test_the_table_points_at_lines(self, oracle: dict[str, Any]) -> None:
        provenance = read_text(PROVENANCE_FILE)
        anchors = [check["source"].rsplit("#", 1)[1] for check in all_checks(oracle["faults"])]
        missing = sorted({anchor for anchor in anchors if f"#{anchor}" not in provenance})
        assert missing == [], f"line anchors missing from the table: {missing}"


class TestDecisions:
    def test_the_decision_ids_are_contiguous(self) -> None:
        numbers = [int(found) for found in re.findall(r"\bD(\d+)\b", read_text(DECISIONS_FILE))]
        assert numbers, "no decision identifiers found"
        assert sorted(set(numbers)) == list(range(1, max(numbers) + 1))

    def test_the_risky_choices_are_frozen(self) -> None:
        decisions = read_text(DECISIONS_FILE)
        for identifier in ("D2", "D5", "D11", "D12"):
            assert identifier in decisions, f"{identifier} is not recorded"

    def test_the_resolved_tool_versions_are_recorded(self) -> None:
        decisions = read_text(DECISIONS_FILE)
        for tool in ("ruff", "mypy", "pytest", "PyYAML"):
            assert tool in decisions, f"{tool} version not recorded"
