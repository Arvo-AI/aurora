"""Black-box tests for `render_markdown` and `write_markdown`.

Pinned from the report contract: section order, the metrics rows, one table row
per run, per-check evidence, provenance and the advisory wording.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from verifier.contract import load_contract
from verifier.decision import (
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
    Verdict,
    decide_remediation,
    decide_reproduction,
)
from verifier.model import FAIL, INCONCLUSIVE, PASS, CheckResult, Contract, Status
from verifier.report import ReportError, Run, render_markdown, write_markdown
from verifier.results import build_result

CHECK_IDS = (
    "no_oomkilled_in_window restart_count_not_increasing processing_evidence "
    "service_has_endpoints endpoints_ready http_expected_response"
)
CHECKS = CHECK_IDS.split()
ALL_FAIL: Mapping[str, Status] = dict.fromkeys(CHECKS, FAIL)
ALL_PASS: Mapping[str, Status] = dict.fromkeys(CHECKS, PASS)
THREE_AND_THREE: Mapping[str, Status] = {**ALL_PASS, **dict.fromkeys(CHECKS[3:], FAIL)}
GAP: Mapping[str, Status] = {**ALL_PASS, "endpoints_ready": INCONCLUSIVE}
GAP_EVIDENCE = {"endpoints_ready": {"missing_metadata": ["endpoints"]}}
GENERATED_AT = "2026-11-15T09:30:00Z"
CAPTURED_AT = "2026-11-15T09:00:00Z"
TITLE = "# Remediation verification report"
ADVISORY = "Advisory. This report is evidence for a human reviewer and never a merge gate."
VARIANTS = ("baseline", "full", "partial_worker_only", "partial_frontend_only")


@pytest.fixture(scope="module")
def contract() -> Contract:
    return load_contract()


def results(
    contract: Contract,
    statuses: Mapping[str, Status],
    evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[CheckResult, ...]:
    extra = evidence or {}
    return tuple(
        build_result(
            contract,
            check_id,
            statuses.get(check_id, PASS),
            f"fixture: {check_id}",
            extra.get(check_id, {"check": check_id}),
        )
        for check_id in CHECKS
    )


@pytest.fixture(scope="module")
def verdicts(contract: Contract) -> dict[str, Verdict]:
    declared = contract.oracle.variants
    baseline = decide_reproduction(contract, declared["baseline"], results(contract, ALL_FAIL))

    def remediation(name: str, statuses: Mapping[str, Status], evidence=None) -> Verdict:
        return decide_remediation(
            contract, declared[name], results(contract, statuses, evidence), baseline=baseline
        )

    return {
        "baseline": baseline,
        "full": remediation("full", ALL_PASS),
        "partial_worker_only": remediation("partial_worker_only", THREE_AND_THREE),
        "partial_frontend_only": remediation("partial_frontend_only", GAP, GAP_EVIDENCE),
    }


def run(verdict: Verdict, mode: str = "SNAPSHOT") -> Run:
    return Run(verdict=verdict, mode=mode, captured_at=CAPTURED_AT)


def render(contract: Contract, runs: list[Run]) -> str:
    return render_markdown(contract, runs, generated_at=GENERATED_AT)


def until(text: str, heading: str, stops: tuple[str, ...]) -> str:
    start = text.index(heading) + len(heading)
    ends = [text.find(stop, start) for stop in stops if text.find(stop, start) != -1]
    return text[start : min(ends)]


def variants_table(text: str) -> str:
    return until(text, "## Variants", ("### ", "## Provenance"))


def variant_section(text: str, name: str) -> str:
    return until(text, f"### {name}", ("### ", "## Provenance"))


def row_containing(block: str, *needles: str) -> str:
    for line in block.splitlines():
        if all(needle in line for needle in needles):
            return line
    raise AssertionError(f"no line contains {needles!r}")


def payload(block: str) -> Any:
    body = re.sub(r"<summary>.*?</summary>", "", block, flags=re.DOTALL)
    body = "\n".join(line for line in body.splitlines() if not line.strip().startswith("```"))
    start, end = body.find("{"), body.rfind("}")
    assert start != -1 and end > start, f"no JSON object in the evidence block: {block!r}"
    return json.loads(body[start : end + 1])


class TestStructure:
    def test_it_opens_with_the_title_advisory_and_metadata(self, contract, verdicts) -> None:
        text = render(contract, [run(verdicts["baseline"], mode="LIVE")])
        assert text.startswith(TITLE)
        assert text.splitlines()[0] == TITLE
        assert ADVISORY in text.splitlines()
        head = text[: text.index("## Metrics")]
        needles = (
            contract.scenario,
            contract.title,
            "LIVE",
            GENERATED_AT,
            contract.source.commit,
            contract.source.fork_commit,
        )
        for needle in needles:
            assert needle in head, f"the metadata block omits {needle!r}"

    def test_the_sections_appear_in_the_declared_order(self, contract, verdicts) -> None:
        runs = [run(verdicts["baseline"]), run(verdicts["full"])]
        text = render(contract, runs)
        markers = (
            "## Metrics",
            "## Variants",
            "### baseline",
            "### full",
            "## Provenance",
            "## Limitations",
        )
        positions = [text.index(marker) for marker in markers]
        assert positions == sorted(positions)


class TestMetrics:
    def test_the_rows_report_counts_and_denominators(self, contract, verdicts) -> None:
        text = render(contract, [run(verdicts["baseline"])])
        metrics = until(text, "## Metrics", ("## Variants",))
        detection = row_containing(metrics, "6 failed")
        assert "6 checks" in detection and "fault" in detection.lower()
        weak = row_containing(metrics, "0 passed")
        assert "6 checks" in weak and "weak" in weak.lower()
        runs = [run(verdicts["full"]), run(verdicts["partial_worker_only"])]
        remediations = until(render(contract, runs), "## Metrics", ("## Variants",))
        row = row_containing(remediations, "2 remediation runs")
        assert "1" in row
        text = render(contract, [run(verdicts["full"])])
        metrics = until(text, "## Metrics", ("## Variants",))
        rows = [line for line in metrics.splitlines() if "not measured" in line]
        assert len(rows) >= 2, "the two unmeasured rows are missing"
        for line in rows:
            assert "0" not in line, f"an unmeasured row reports a zero: {line!r}"
        assert "0 passed" not in metrics
        assert "0 failed" not in metrics


class TestVariantTable:
    def test_each_run_is_one_row_in_the_given_order(self, contract, verdicts) -> None:
        runs = [run(verdicts["baseline"]), run(verdicts["partial_worker_only"], mode="LIVE")]
        block = variants_table(render(contract, runs))
        first = row_containing(block, "baseline", "reproduction")
        second = row_containing(block, "partial_worker_only", "remediation")
        assert all(x in first for x in ("SNAPSHOT", "not applicable", "CONFIRMED"))
        assert all(x in second for x in ("LIVE", REJECTED))
        assert block.index(first) < block.index(second)
        twice = variants_table(render(contract, [run(verdicts["baseline"])] * 2))
        assert len([line for line in twice.splitlines() if "baseline" in line]) == 2

    @pytest.mark.parametrize(
        ("name", "count"),
        (("baseline", "0/6"), ("full", "6/6"), ("partial_worker_only", "3/6")),
    )
    def test_the_row_reports_passes_over_checks(self, name, count, contract, verdicts) -> None:
        verdict = verdicts[name]
        row = row_containing(variants_table(render(contract, [run(verdict)])), name, verdict.kind)
        assert count in row


class TestVariantSection:
    @pytest.mark.parametrize("name", VARIANTS)
    def test_the_reason_and_the_checks_table(self, name, contract, verdicts) -> None:
        verdict = verdicts[name]
        body = variant_section(render(contract, [run(verdict)]), name)
        assert verdict.reason in body
        table = [line for line in body.splitlines() if line.lstrip().startswith("|")]
        positions = []
        for result in verdict.results:
            matches = [index for index, line in enumerate(table) if result.id in line]
            assert matches, f"no table row for {result.id}"
            row = table[matches[0]]
            for needle in (result.fault, result.status, result.reason, result.source):
                assert needle in row, f"the {result.id} row omits {needle!r}: {row!r}"
            positions.append(matches[0])
        assert positions == sorted(positions)

    @pytest.mark.parametrize("name", VARIANTS)
    def test_every_check_has_parsable_evidence(self, name, contract, verdicts) -> None:
        verdict = verdicts[name]
        blocks = re.findall(
            r"<details[^>]*>(.*?)</details>", render(contract, [run(verdict)]), flags=re.DOTALL
        )
        evidence = [block for block in blocks if "Evidence for " in block]
        assert len(evidence) == len(verdict.results)
        for result in verdict.results:
            summary = f"Evidence for {result.id}"
            matching = [block for block in evidence if summary in block]
            assert len(matching) == 1, f"no single {summary!r} block"
            assert payload(matching[0]) == dict(result.evidence)

    def test_unresolved_elements_carry_their_id_and_source(self, contract, verdicts) -> None:
        verdict = verdicts["partial_frontend_only"]
        assert verdict.unresolved
        body = variant_section(render(contract, [run(verdict)]), verdict.variant)
        for element in verdict.unresolved:
            row_containing(body, element.id, element.source)
            lines = [ln for ln in body.splitlines() if element.id in ln and element.source in ln]
            assert len(lines) >= 2, f"{element.id} has no unresolved entry with its source"


class TestProvenanceAndLimitations:
    def test_provenance_names_the_commit_and_every_check_source(self, contract, verdicts) -> None:
        verdict = verdicts["full"]
        body = until(render(contract, [run(verdict)]), "## Provenance", ("## Limitations",))
        assert contract.source.commit in body
        for result in verdict.results:
            row_containing(body, result.id, result.source)

    def test_the_scope_is_stated_and_no_fix_is_claimed(self, contract, verdicts) -> None:
        text = render(contract, [run(verdict) for verdict in verdicts.values()])
        body = text[text.index("## Limitations") :]
        assert all(s in body for s in ("does not prove", "never blocks a merge"))
        assert "within the contract" in text.lower()
        stripped = re.sub(r"within the contract", " ", text, flags=re.IGNORECASE)
        assert not re.search(r"\b(fixed|solved)\b", stripped, flags=re.IGNORECASE)


class TestGuardsAndWriting:
    @pytest.mark.parametrize("entry", ("render", "write"))
    def test_a_positive_without_evidence_raises(self, entry, contract, verdicts, tmp_path) -> None:
        bare = tuple(
            build_result(contract, check_id, PASS, "fixture: no evidence", {})
            for check_id in CHECKS
        )
        positive = decide_remediation(
            contract, contract.oracle.variants["full"], bare, baseline=verdicts["baseline"]
        )
        assert positive.status == VERIFIED_WITHIN_CONTRACT
        with pytest.raises(ReportError):
            if entry == "render":
                render(contract, [run(positive)])
            else:
                write_markdown(
                    contract, [run(positive)], tmp_path / "report.md", generated_at=GENERATED_AT
                )

    def test_write_creates_parents_and_overwrites(self, contract, verdicts, tmp_path) -> None:
        runs = [run(verdicts["baseline"]), run(verdicts["full"])]
        target = tmp_path / "missing" / "deeper" / "report.md"
        written = write_markdown(contract, runs, target, generated_at=GENERATED_AT)
        assert isinstance(written, Path)
        assert written == target
        expected = render(contract, runs) + "\n"
        assert not expected.endswith("\n\n")
        assert target.read_bytes() == expected.encode("utf-8")
        target.write_text("stale\n", encoding="utf-8")
        assert write_markdown(contract, runs, target, generated_at=GENERATED_AT) == target
        assert target.read_bytes() == expected.encode("utf-8")
        assert render(contract, runs) == render_markdown(
            contract, list(runs), generated_at=GENERATED_AT
        )

    def test_no_runs_still_renders_the_metrics_and_the_empty_variants(self, contract) -> None:
        text = render(contract, [])
        assert "No runs in this report." in variants_table(text)
        assert "not measured" in until(text, "## Metrics", ("## Variants",))
        assert "## Provenance" in text
        assert "## Limitations" in text
