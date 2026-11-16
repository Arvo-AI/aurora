"""The snapshot path: five fixtures in, five verdicts out, no cluster.

The strongest test here compares SNAPSHOT against LIVE over the same simulated
cluster: the two paths share `checks` and `decision`, so a divergence means one
of them is reading an observation differently.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from simulated_session import simulate
from snapshot_fixtures import fixtures_from
from verifier.contract import load_contract
from verifier.decision import (
    CONFIRMED,
    INCONCLUSIVE,
    NOT_CONFIRMED,
    REJECTED,
    VERIFIED_WITHIN_CONTRACT,
)
from verifier.model import FAIL, PASS
from verifier.observations import MODE_SNAPSHOT, from_snapshot
from verifier.report import REPRODUCTION
from verifier.snapshots import (
    SnapshotError,
    fixture_path,
    load_observations,
    render_table,
    runs_of,
    verify,
)


@pytest.fixture()
def five(tmp_path: Path) -> Path:
    """The five fixtures a full simulated cycle would capture."""
    return fixtures_from(tmp_path / "fixtures", simulate())


def test_the_five_fixtures_reach_the_live_verdicts(five: Path) -> None:
    simulation = simulate()
    contract = simulation.contract
    attempts = verify(contract, five)
    assert [attempt.variant for attempt in attempts] == [
        "baseline",
        "full",
        "partial_worker_only",
        "partial_frontend_only",
        "noop",
    ]
    for attempt in attempts:
        live = simulation.attempt(attempt.variant).verdict
        assert attempt.verdict.status == live.status
        assert attempt.verdict.reproduction_status == live.reproduction_status
    assert attempts[0].verdict.reproduction_status == CONFIRMED
    assert attempts[1].verdict.status == VERIFIED_WITHIN_CONTRACT
    assert [attempt.verdict.status for attempt in attempts[2:]] == [REJECTED] * 3


def test_every_snapshot_verdict_says_snapshot_and_keeps_the_live_provenance(five: Path) -> None:
    attempts = verify(load_contract(), five)
    assert {attempt.mode for attempt in attempts} == {MODE_SNAPSHOT}
    for attempt in attempts:
        assert attempt.end is not None
        assert attempt.end.meta.provenance == "LIVE"
        assert attempt.end.meta.mode == MODE_SNAPSHOT
    document = json.loads(fixture_path(five, "baseline").read_text(encoding="utf-8"))
    assert document["meta"]["mode"] == "LIVE"
    assert document["meta"]["provenance"] == "LIVE"


def test_a_missing_fixture_names_the_path_it_expected(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError) as raised:
        verify(load_contract(), tmp_path)
    assert str(fixture_path(tmp_path, "baseline")) in str(raised.value)
    assert "baseline" in str(raised.value)


def test_a_fixture_for_another_variant_is_refused(five: Path) -> None:
    destination = five / "baseline.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    document["meta"]["variant"] = "full"
    destination.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SnapshotError) as raised:
        verify(load_contract(), five)
    assert "records the variant 'full'" in str(raised.value)
    assert "'baseline'" in str(raised.value)


def test_a_malformed_fixture_is_a_snapshot_error(tmp_path: Path) -> None:
    fixture_path(tmp_path, "baseline").write_text("{", encoding="utf-8")
    with pytest.raises(SnapshotError) as raised:
        verify(load_contract(), tmp_path)
    assert "cannot be read" in str(raised.value)


def test_an_incomplete_fixture_turns_every_check_inconclusive(five: Path) -> None:
    destination = five / "full.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    document.pop("pods")
    destination.write_text(json.dumps(document), encoding="utf-8")

    attempts = verify(load_contract(), five)
    full = attempts[1]
    assert full.verdict.status == INCONCLUSIVE
    assert {result.status for result in full.verdict.results} == {INCONCLUSIVE}
    assert "pods" in str(from_snapshot(destination).missing_metadata)


def test_a_fixture_that_is_not_live_provenance_is_inconclusive(five: Path) -> None:
    destination = five / "full.json"
    document = json.loads(destination.read_text(encoding="utf-8"))
    document["meta"]["provenance"] = "RECORDED"
    destination.write_text(json.dumps(document), encoding="utf-8")

    assert "provenance" in from_snapshot(destination).missing_metadata
    attempts = verify(load_contract(), five)
    assert attempts[1].verdict.status == INCONCLUSIVE


def test_a_baseline_that_does_not_reproduce_blocks_every_positive(tmp_path: Path) -> None:
    fixtures = fixtures_from(tmp_path / "fixtures", simulate("no_fault"))
    attempts = verify(load_contract(), fixtures)
    baseline = attempts[0]
    assert baseline.verdict.reproduction_status == NOT_CONFIRMED
    for attempt in attempts[1:]:
        assert attempt.verdict.status == INCONCLUSIVE
        assert "the pre-fix state was NOT_CONFIRMED" in attempt.verdict.reason


def test_snapshot_mode_reaches_five_verdicts_without_starting_a_process(
    five: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A negative test: SNAPSHOT must not shell out, so the test breaks it.

    The patch lasts for this test only. The undo at the end proves the process
    runner is back, so the next test cannot inherit a broken `subprocess.run`.
    """
    original = subprocess.run

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("snapshot mode started a subprocess")

    monkeypatch.setattr(subprocess, "run", forbidden)
    assert len(verify(load_contract(), five)) == 5
    monkeypatch.undo()
    assert subprocess.run is original


def test_the_table_shows_every_variant_and_the_failing_checks(five: Path) -> None:
    runs = runs_of(verify(load_contract(), five))
    text = render_table(runs)
    lines = text.splitlines()
    assert lines[0].split() == [
        "variant",
        "mode",
        "verdict",
        "reproduction",
        "checks",
        "failing",
    ]
    assert len(lines) == 10
    assert "SNAPSHOT" in text
    assert "VERIFIED_WITHIN_CONTRACT" in text
    assert "no_oomkilled_in_window" in text
    assert "baseline: CONFIRMED" in text
    assert "remediations: 4 runs, 1 verified, 3 rejected, 0 inconclusive" in text


def test_a_baseline_row_counts_the_checks_it_failed(five: Path) -> None:
    runs = runs_of(verify(load_contract(), five))
    baseline = runs[0]
    assert baseline.verdict.kind == REPRODUCTION
    assert {result.status for result in baseline.verdict.results} == {FAIL}
    assert "0/6 pass" in render_table(runs)


def test_the_table_counts_a_pass_for_the_full_fix(five: Path) -> None:
    runs = runs_of(verify(load_contract(), five))
    assert runs[1].verdict.status == VERIFIED_WITHIN_CONTRACT
    assert {result.status for result in runs[1].verdict.results} == {PASS}
    assert "6/6 pass" in render_table(runs)


def test_a_table_without_a_baseline_says_so(tmp_path: Path) -> None:
    simulation = simulate()
    fixtures = fixtures_from(tmp_path / "fixtures", simulation)
    attempts = verify(simulation.contract, fixtures)
    text = render_table(runs_of([attempts[1]]))
    lines = text.splitlines()
    assert lines[-2] == "baseline: absent"
    assert lines[-1].startswith("remediations: 1 run,")


def test_the_observation_loader_keeps_the_variant_it_read(five: Path) -> None:
    observations = load_observations(load_contract(), five, "noop")
    assert observations.meta.variant == "noop"
    assert observations.is_complete


def test_an_unknown_variant_is_a_missing_fixture_not_a_crash(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        load_observations(load_contract(), tmp_path, "full")
    with pytest.raises(SnapshotError):
        load_observations(load_contract(), tmp_path, "no_such_variant")
