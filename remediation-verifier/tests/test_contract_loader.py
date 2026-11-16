"""Black-box tests for the contract loader and the two views it exposes.

The loader is the boundary between a frozen YAML file and every check that
runs later, so two things matter here: the real contract loads with the pinned
values, and every way of breaking the file fails loudly instead of silently.
The agent view is tested as a leak surface: oracle vocabulary appears there and
the suite fails.

Broken contracts are built in `tmp_path` from the real file. `schema.json` is
copied next to each copy because the loader resolves the schema by sibling
path. Nothing inside the repository is modified.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
import yaml

from verifier import config
from verifier.contract import ContractError, agent_view, load_contract, oracle_view
from verifier.model import Contract

CONTRACT_FILE = config.DEFAULT_CONTRACT_PATH
SCHEMA_FILE = config.CONTRACTS_DIR / config.SCHEMA_NAME

# One compiled alternation scans the serialized view once. A substring test per
# term would walk the same string seven times (re documentation, "re.compile").
ORACLE_TERMS = ("tier", "selector", "BATCH_SIZE", "OOMKilled", "kettle", "mug", "teapot")
ORACLE_PATTERN = re.compile("|".join(re.escape(term) for term in ORACLE_TERMS), re.IGNORECASE)

FAULT_NAMES = {"worker-oom", "frontend-selector"}
FIX_IDS = ["full", "p1_raise_limit", "partial_worker_only", "partial_frontend_only", "noop"]
VARIANT_NAMES = {
    "baseline",
    "full",
    "p1_raise_limit",
    "partial_worker_only",
    "partial_frontend_only",
    "noop",
}


def assert_no_oracle_words(view: dict[str, Any]) -> None:
    """Raise when a serialized view carries any oracle vocabulary."""
    serialized = json.dumps(view, sort_keys=True)
    leaked = sorted({match.lower() for match in ORACLE_PATTERN.findall(serialized)})
    assert leaked == [], f"view leaks oracle vocabulary: {leaked}"


def contract_data() -> dict[str, Any]:
    return copy.deepcopy(yaml.safe_load(CONTRACT_FILE.read_text(encoding="utf-8")))


def write_contract(directory: Path, data: dict[str, Any]) -> Path:
    shutil.copyfile(SCHEMA_FILE, directory / config.SCHEMA_NAME)
    path = directory / CONTRACT_FILE.name
    # Keep the source order so a broken fixture still reads like the real file.
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding=config.ENCODING)
    return path


def reject(tmp_path: Path, data: dict[str, Any], needle: str) -> None:
    path = write_contract(tmp_path, data)
    with pytest.raises(ContractError) as excinfo:
        load_contract(path)
    assert needle in str(excinfo.value), str(excinfo.value)


@pytest.fixture(scope="module")
def contract() -> Any:
    return load_contract()


class TestRealContract:
    def test_the_default_path_loads(self, contract: Contract) -> None:
        assert contract.scenario == "12-two-faults-oom-and-selector"
        assert contract.title

    def test_the_lab_commit_is_pinned(self, contract: Contract) -> None:
        assert contract.source.commit == config.LAB_COMMIT

    def test_the_source_records_the_read_date_and_the_fork(self, contract: Contract) -> None:
        assert contract.source.read_on == "2026-11-15"
        assert contract.source.fork_commit == "12c67cffd45c6b0b8c5da1d2e160a945e2936f49"

    def test_the_oracle_dataclasses_expose_the_frozen_values(self, contract: Contract) -> None:
        assert contract.oracle.observation.window_seconds == 60
        assert list(contract.oracle.expected_faults) == ["worker-oom", "frontend-selector"]
        assert set(contract.oracle.faults) == FAULT_NAMES
        assert list(contract.oracle.fixes) == FIX_IDS
        assert set(contract.oracle.variants) == VARIANT_NAMES

    def test_the_loaded_dataclasses_are_frozen(self, contract: Contract) -> None:
        with pytest.raises(FrozenInstanceError):
            contract.source.commit = "0" * 40
        with pytest.raises(FrozenInstanceError):
            contract.agent.alert = "changed"


class TestAgentView:
    def test_the_agent_view_holds_only_the_alert(self, contract: Contract) -> None:
        view = agent_view(contract)
        assert list(view) == ["alert"]
        assert view["alert"] == contract.agent.alert

    def test_the_agent_view_leaks_no_oracle_word(self, contract: Contract) -> None:
        assert_no_oracle_words(agent_view(contract))

    def test_the_leak_helper_passes_a_clean_view_and_catches_a_planted_one(
        self, contract: Contract
    ) -> None:
        clean = agent_view(contract)
        assert_no_oracle_words(clean)
        planted = {"alert": clean["alert"], "tier": "web"}
        with pytest.raises(AssertionError, match="tier"):
            assert_no_oracle_words(planted)


class TestOracleView:
    def test_the_oracle_view_has_the_five_sections(self, contract: Contract) -> None:
        assert set(oracle_view(contract)) == {
            "observation",
            "baseline",
            "faults",
            "accepted_fixes",
            "variants",
        }

    def test_the_oracle_view_carries_six_checks_across_two_faults(self, contract: Contract) -> None:
        faults = oracle_view(contract)["faults"]
        assert set(faults) == FAULT_NAMES
        assert sum(len(fault["checks"]) for fault in faults.values()) == 6

    def test_the_oracle_view_carries_five_fixes_and_six_variants(self, contract: Contract) -> None:
        oracle = oracle_view(contract)
        assert oracle["baseline"]["expected_faults"] == ["worker-oom", "frontend-selector"]
        assert [fix["id"] for fix in oracle["accepted_fixes"]] == FIX_IDS
        for fix in oracle["accepted_fixes"]:
            assert set(fix) >= {"id", "source", "steps"}
        assert set(oracle["variants"]) == VARIANT_NAMES


class TestBrokenContracts:
    def test_the_error_is_a_value_error(self) -> None:
        assert issubclass(ContractError, ValueError)

    def test_a_missing_commit_fails(self, tmp_path: Path) -> None:
        data = contract_data()
        del data["source"]["commit"]
        reject(tmp_path, data, "commit")

    def test_an_unknown_key_fails(self, tmp_path: Path) -> None:
        data = contract_data()
        data["verifier_hint"] = "the second fault is a selector"
        reject(tmp_path, data, "verifier_hint")

    def test_a_fix_without_a_source_fails(self, tmp_path: Path) -> None:
        data = contract_data()
        del data["oracle_view"]["accepted_fixes"][0]["source"]
        reject(tmp_path, data, "source")

    def test_a_variant_pointing_at_an_unknown_fix_fails(self, tmp_path: Path) -> None:
        data = contract_data()
        data["oracle_view"]["variants"]["full"]["fix"] = "ghost"
        reject(tmp_path, data, "ghost")

    def test_a_baseline_expecting_an_unknown_fault_fails(self, tmp_path: Path) -> None:
        data = contract_data()
        data["oracle_view"]["baseline"]["expected_faults"].append("cache-miss")
        reject(tmp_path, data, "cache-miss")

    def test_a_missing_file_fails_and_names_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent.yaml"
        with pytest.raises(ContractError) as excinfo:
            load_contract(missing)
        assert "absent.yaml" in str(excinfo.value), str(excinfo.value)

    def test_a_string_path_is_accepted(self) -> None:
        assert load_contract(str(CONTRACT_FILE)).source.commit == config.LAB_COMMIT

    def test_the_environment_can_point_at_another_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = write_contract(tmp_path, contract_data())
        monkeypatch.setenv(config.CONTRACT_PATH_ENV, str(path))
        assert config.default_contract_path() == path
        assert load_contract().source.commit == config.LAB_COMMIT

    def test_without_the_variable_the_frozen_contract_is_used(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(config.CONTRACT_PATH_ENV, raising=False)
        assert config.default_contract_path() == config.DEFAULT_CONTRACT_PATH
