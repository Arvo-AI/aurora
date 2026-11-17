"""Black-box tests for the report layer of the scenario 12 contract."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from verifier.contract import load_contract
from verifier.decision import (
    EXECUTION_ERROR,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
    Verdict,
    decide_remediation,
    decide_reproduction,
    execution_error,
)
from verifier.model import FAIL, INCONCLUSIVE, PASS, CheckResult, Contract, Status, Variant
from verifier.report import ReportError, Run, metrics_for, report_document, write_report
from verifier.results import build_result

CHECK_IDS = (
    "no_oomkilled_in_window restart_count_not_increasing processing_evidence "
    "service_has_endpoints endpoints_ready http_expected_response"
)
CHECK_ORDER = CHECK_IDS.split()
ALL_FAIL: Mapping[str, Status] = {check_id: FAIL for check_id in CHECK_ORDER}
ALL_PASS: Mapping[str, Status] = {check_id: PASS for check_id in CHECK_ORDER}
ONE_PASS: Mapping[str, Status] = {**ALL_FAIL, "processing_evidence": PASS}
FRONTEND = ("service_has_endpoints", "endpoints_ready", "http_expected_response")
THREE_AND_THREE: Mapping[str, Status] = {**ALL_PASS, **dict.fromkeys(FRONTEND, FAIL)}
GAP: Mapping[str, Status] = {**ALL_PASS, "endpoints_ready": INCONCLUSIVE}
GAP_EVIDENCE: Mapping[str, Mapping[str, Any]] = {
    "endpoints_ready": {"missing_metadata": ["endpoints"]}
}
GENERATED_AT = "2026-11-15T09:30:00Z"
CAPTURED_AT = "2026-11-15T09:00:00Z"

TOP_LEVEL = "advisory scenario title generated_at source mode metrics variants"
TOP_LEVEL_KEYS = frozenset(TOP_LEVEL.split())
VARIANT_FIELDS = (
    "variant kind mode captured_at status reproduction_status reason "
    "weak_checks unresolved checks timing"
)
VARIANT_KEYS = frozenset(VARIANT_FIELDS.split())


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


def variant(contract: Contract, name: str) -> Variant:
    return contract.oracle.variants[name]


def results(
    contract: Contract,
    statuses: Mapping[str, Status] | None = None,
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CheckResult, ...]:
    chosen = statuses or {}
    extra = evidence or {}
    return tuple(
        build_result(
            contract,
            check_id,
            chosen.get(check_id, PASS),
            f"fixture: {check_id}",
            extra.get(check_id, {"check": check_id}),
        )
        for check_id in CHECK_ORDER
    )


def confirmed(contract: Contract) -> Verdict:
    return decide_reproduction(contract, variant(contract, "baseline"), results(contract, ALL_FAIL))


def remediated(
    contract: Contract,
    name: str,
    statuses: Mapping[str, Status],
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> Verdict:
    return decide_remediation(
        contract,
        variant(contract, name),
        results(contract, statuses, evidence),
        baseline=confirmed(contract),
    )


def verdicts(contract: Contract) -> dict[str, Verdict]:
    return {
        VERIFIED_WITHIN_CONTRACT: remediated(contract, "full", ALL_PASS),
        REJECTED: remediated(contract, "partial_worker_only", THREE_AND_THREE),
        INCONCLUSIVE: remediated(contract, "partial_frontend_only", GAP, GAP_EVIDENCE),
        EXECUTION_ERROR: execution_error(contract, variant(contract, "noop"), "kubectl failed"),
    }


def run(verdict: Verdict, mode: str = "SNAPSHOT", timing: Mapping[str, float] | None = None) -> Run:
    return Run(verdict=verdict, mode=mode, captured_at=CAPTURED_AT, timing=timing or {})


def document_for(contract: Contract, runs: list[Run]) -> dict[str, Any]:
    return report_document(contract, runs, generated_at=GENERATED_AT)


@pytest.fixture(scope="module")
def verified(contract: Contract) -> Verdict:
    return verdicts(contract)[VERIFIED_WITHIN_CONTRACT]


@pytest.fixture(scope="module")
def two_runs(contract: Contract, verified: Verdict) -> list[Run]:
    return [run(confirmed(contract)), run(verified)]


@pytest.fixture(scope="module")
def two_run_document(contract: Contract, two_runs: list[Run]) -> dict[str, Any]:
    return document_for(contract, two_runs)


class TestTopLevel:
    def test_the_header_is_the_contract_report_of_the_call(self, contract) -> None:
        document = document_for(contract, [run(confirmed(contract))])
        assert set(document) == TOP_LEVEL_KEYS
        assert document["advisory"] is True
        assert document["scenario"] == contract.scenario
        assert document["title"] == contract.title
        assert document["generated_at"] == GENERATED_AT

    @pytest.mark.parametrize("field", ("repo", "url", "commit", "fork_repo", "fork_commit"))
    def test_the_source_cites_the_contract(self, field, contract) -> None:
        document = document_for(contract, [run(confirmed(contract))])
        assert document["source"][field] == getattr(contract.source, field)

    @pytest.mark.parametrize(
        ("modes", "expected"),
        (
            ((), "UNKNOWN"),
            (("SNAPSHOT",), "SNAPSHOT"),
            (("LIVE",), "LIVE"),
            (("SNAPSHOT", "LIVE"), "MIXED"),
        ),
    )
    def test_the_mode_reports_the_shared_value_or_mixed(self, modes, expected, contract) -> None:
        runs = [run(confirmed(contract), mode=mode) for mode in modes]
        assert document_for(contract, runs)["mode"] == expected

    def test_a_snapshot_run_never_serializes_as_live(self, contract) -> None:
        document = document_for(contract, [run(confirmed(contract), mode="SNAPSHOT")])
        entry = document["variants"][0]
        assert entry["mode"] == "SNAPSHOT"
        assert "LIVE" not in json.dumps(entry)


class TestVariantEntry:
    def test_entries_are_the_runs_in_order_with_the_declared_keys(self, two_run_document) -> None:
        variants = two_run_document["variants"]
        assert [entry["variant"] for entry in variants] == ["baseline", "full"]
        assert [entry["status"] for entry in variants] == [None, VERIFIED_WITHIN_CONTRACT]
        assert all(set(entry) == VARIANT_KEYS for entry in variants)

    @pytest.mark.parametrize("index", (0, 1))
    def test_checks_keep_the_order_and_the_values(self, index, two_run_document, two_runs) -> None:
        entry = two_run_document["variants"][index]
        source_results = two_runs[index].verdict.results
        assert [check["id"] for check in entry["checks"]] == [
            result.id for result in source_results
        ]
        assert list(entry["weak_checks"]) == list(two_runs[index].verdict.weak_checks)
        assert list(entry["unresolved"]) == list(two_runs[index].verdict.unresolved)
        for check_entry, result in zip(entry["checks"], source_results, strict=True):
            for name in ("id", "fault", "status", "reason", "source"):
                assert check_entry[name] == getattr(result, name)
            assert dict(check_entry["evidence"]) == dict(result.evidence)

    def test_timing_is_the_run_mapping_and_defaults_to_empty(self, contract) -> None:
        bare = Run(verdict=confirmed(contract), mode="SNAPSHOT", captured_at=CAPTURED_AT)
        assert dict(bare.timing) == {}
        assert dict(document_for(contract, [bare])["variants"][0]["timing"]) == {}
        timed = run(confirmed(contract), timing={"observe": 1.25})
        entry = document_for(contract, [timed])["variants"][0]
        assert dict(entry["timing"]) == {"observe": 1.25}

    def test_the_list_fields_mirror_the_verdict(self, contract) -> None:
        weak = decide_reproduction(
            contract, variant(contract, "baseline"), results(contract, ONE_PASS)
        )
        assert weak.weak_checks == ("processing_evidence",)
        entry = document_for(contract, [run(weak)])["variants"][0]
        assert list(entry["weak_checks"]) == list(weak.weak_checks)
        gap = verdicts(contract)[INCONCLUSIVE]
        assert gap.unresolved
        elements = document_for(contract, [run(gap)])["variants"][0]["unresolved"]
        assert [element["id"] for element in elements] == [element.id for element in gap.unresolved]
        for item, element in zip(elements, gap.unresolved, strict=True):
            for name in ("id", "source", "reason"):
                assert item[name] == getattr(element, name)
            assert list(item["missing"]) == list(element.missing)


class TestMetrics:
    def test_the_ratios_count_the_first_baseline(self, contract, verified) -> None:
        baseline = variant(contract, "baseline")
        first = decide_reproduction(contract, baseline, results(contract, ALL_FAIL))
        second = decide_reproduction(contract, baseline, results(contract, ONE_PASS))
        for runs in ([run(first)], [run(first), run(second)]):
            metrics = metrics_for(runs)
            assert metrics["fault_detection"] == {"failed": 6, "checks": 6}
            assert metrics["weak_checks"] == {"passed": 0, "checks": 6}
        without = metrics_for([run(verified)])
        assert without["fault_detection"] is None
        assert without["weak_checks"] is None

    @pytest.mark.parametrize(
        ("status", "counter"),
        (
            (VERIFIED_WITHIN_CONTRACT, "verified"),
            (REJECTED, "rejected"),
            (INCONCLUSIVE, "inconclusive"),
            (EXECUTION_ERROR, "execution_error"),
        ),
    )
    def test_each_remediation_status_has_its_own_counter(self, status, counter, contract) -> None:
        metrics = metrics_for([run(verdicts(contract)[status])])
        assert metrics["remediation"][counter] == 1
        assert metrics["remediation"]["runs"] == 1
        for other in {"verified", "rejected", "inconclusive", "execution_error"} - {counter}:
            assert metrics["remediation"][other] == 0

    def test_the_counts_add_up_and_the_document_carries_them(self, contract) -> None:
        runs = [run(verdict) for verdict in verdicts(contract).values()]
        metrics = metrics_for(runs)
        assert metrics["remediation"] == {
            "verified": 1,
            "rejected": 1,
            "inconclusive": 1,
            "execution_error": 1,
            "runs": 4,
        }
        assert document_for(contract, runs)["metrics"] == metrics
        mixed = [run(confirmed(contract)), *runs]
        assert metrics_for(mixed)["remediation"]["runs"] == 4

    def test_runs_per_variant_counts_repeats(self, contract, verified) -> None:
        baseline = confirmed(contract)
        metrics = metrics_for([run(baseline), run(baseline), run(verified)])
        assert metrics["runs_per_variant"] == {"baseline": 2, "full": 1}


class TestEvidence:
    def test_a_positive_without_evidence_raises(self, contract) -> None:
        assert issubclass(ReportError, ValueError)
        bare = tuple(
            build_result(contract, check_id, PASS, "fixture: no evidence", {})
            for check_id in CHECK_ORDER
        )
        positive = decide_remediation(
            contract, variant(contract, "full"), bare, baseline=confirmed(contract)
        )
        assert positive.status == VERIFIED_WITHIN_CONTRACT
        assert all(dict(result.evidence) == {} for result in positive.results)
        positive_run = run(positive)
        with pytest.raises(ReportError):
            document_for(contract, [positive_run])

    def test_the_same_positive_with_evidence_is_reported(self, contract, verified) -> None:
        entry = document_for(contract, [run(verified)])["variants"][0]
        assert entry["status"] == VERIFIED_WITHIN_CONTRACT
        assert all(check["evidence"] for check in entry["checks"])


class TestWriting:
    def test_write_creates_parents_writes_exactly_and_overwrites(
        self, contract, tmp_path, two_runs
    ) -> None:
        target = tmp_path / "missing" / "deeper" / "report.json"
        assert write_report(contract, two_runs, target, generated_at=GENERATED_AT) == target
        document = report_document(contract, two_runs, generated_at=GENERATED_AT)
        expected = json.dumps(document, indent=2, sort_keys=True) + "\n"
        assert target.read_text(encoding="utf-8") == expected
        target.write_text("stale\n", encoding="utf-8")
        write_report(contract, two_runs, target, generated_at=GENERATED_AT)
        assert target.read_text(encoding="utf-8") == expected

    def test_writes_are_byte_identical_and_parse_back(self, contract, tmp_path, two_runs) -> None:
        first = tmp_path / "first.json"
        second = tmp_path / "second.json"
        write_report(contract, two_runs, first, generated_at=GENERATED_AT)
        write_report(contract, two_runs, second, generated_at=GENERATED_AT)
        assert first.read_bytes() == second.read_bytes()
        loaded = json.loads(second.read_text(encoding="utf-8"))
        assert loaded == report_document(contract, two_runs, generated_at=GENERATED_AT)
