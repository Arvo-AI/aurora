"""The command line: three modes, one advisory exit code, one refusal path."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import verifier.cli as cli
from simulated_session import simulate
from snapshot_fixtures import fixtures_from
from verifier.decision import REJECTED, VERIFIED_WITHIN_CONTRACT


@pytest.fixture()
def five(tmp_path: Path) -> Path:
    return fixtures_from(tmp_path / "fixtures", simulate())


def run(*argv: str) -> int:
    return cli.main(list(argv))


def snapshot_args(five: Path, out: Path, *extra: str) -> list[str]:
    return [
        "--mode",
        "snapshot",
        "--fixtures",
        str(five),
        "--out",
        str(out),
        *extra,
    ]


def test_help_lists_the_three_modes(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--help"])
    assert exit_info.value.code == 0
    printed = capsys.readouterr().out
    for mode in ("snapshot", "live", "capture"):
        assert mode in printed


def test_snapshot_mode_writes_both_reports_and_prints_the_table(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert run(*snapshot_args(five, out)) == cli.EXIT_OK
    printed = capsys.readouterr().out
    assert "baseline" in printed
    assert "VERIFIED_WITHIN_CONTRACT" in printed
    assert f"report: {out / 'report.json'}" in printed
    assert f"markdown: {out / 'report.md'}" in printed
    document = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert document["mode"] == "SNAPSHOT"
    assert document["advisory"] is True
    assert len(document["variants"]) == 6


def test_a_rejected_fix_still_exits_zero(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "out"
    assert run(*snapshot_args(five, out)) == cli.EXIT_OK
    capsys.readouterr()
    document = json.loads((out / "report.json").read_text(encoding="utf-8"))
    statuses = {variant["variant"]: variant["status"] for variant in document["variants"]}
    assert statuses["full"] == VERIFIED_WITHIN_CONTRACT
    assert [statuses[name] for name in ("partial_worker_only", "noop")] == [REJECTED, REJECTED]


def test_a_missing_fixture_exits_two_and_names_the_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(*snapshot_args(tmp_path, tmp_path / "out")) == cli.EXIT_NOT_RUN
    printed = capsys.readouterr().err
    assert "has no snapshot" in printed
    assert "baseline.json" in printed


def test_snapshot_mode_ignores_the_lab_with_a_warning(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(*snapshot_args(five, tmp_path / "out", "--lab", str(tmp_path))) == cli.EXIT_OK
    assert "--lab is ignored" in capsys.readouterr().err


def test_snapshot_mode_ignores_variants_and_repeat(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = snapshot_args(five, tmp_path / "out", "--variants", "full", "--repeat", "2")
    assert run(*argv) == cli.EXIT_OK
    assert "--variants and --repeat are ignored" in capsys.readouterr().err


def test_live_without_a_lab_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--mode", "live") == cli.EXIT_NOT_RUN
    assert "--lab is required" in capsys.readouterr().err


def test_capture_without_a_lab_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--mode", "capture") == cli.EXIT_NOT_RUN
    assert "--lab is required" in capsys.readouterr().err


def test_a_repeat_below_one_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("--mode", "live", "--lab", str(tmp_path), "--repeat", "0") == cli.EXIT_NOT_RUN
    assert "--repeat has to be at least 1" in capsys.readouterr().err


def test_a_bad_generated_at_is_refused(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = snapshot_args(five, tmp_path / "out", "--generated-at", "yesterday")
    assert run(*argv) == cli.EXIT_NOT_RUN
    assert "ISO 8601" in capsys.readouterr().err


def test_generated_at_is_normalized_to_utc(five: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    argv = snapshot_args(five, out, "--generated-at", "2026-11-16T01:00:00+01:00")
    assert run(*argv) == cli.EXIT_OK
    document = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert document["generated_at"] == "2026-11-16T00:00:00Z"


def test_two_snapshot_runs_with_one_timestamp_are_byte_identical(
    five: Path, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    argv = ["--generated-at", "2026-11-16T00:00:00Z"]
    assert run(*snapshot_args(five, first, *argv)) == cli.EXIT_OK
    assert run(*snapshot_args(five, second, *argv)) == cli.EXIT_OK
    assert (first / "report.json").read_bytes() == (second / "report.json").read_bytes()
    assert (first / "report.md").read_bytes() == (second / "report.md").read_bytes()


def test_capture_refuses_a_lab_at_another_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_lab_head", lambda lab: "0" * 40)
    argv = [
        "--mode",
        "capture",
        "--lab",
        str(tmp_path),
        "--fixtures",
        str(tmp_path / "fixtures"),
        "--out",
        str(tmp_path / "out"),
    ]
    assert run(*argv) == cli.EXIT_NOT_RUN
    printed = capsys.readouterr().err
    assert "0" * 40 in printed
    assert "not the pinned" in printed


def test_capture_refuses_a_lab_without_a_readable_head(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_lab_head", lambda lab: None)
    argv = ["--mode", "capture", "--lab", str(tmp_path)]
    assert run(*argv) == cli.EXIT_NOT_RUN
    assert "cannot read the git HEAD" in capsys.readouterr().err


def test_the_lab_head_reader_reports_the_current_commit() -> None:
    root = Path(__file__).resolve().parents[1]
    head = cli._lab_head(root)
    if head is None:
        pytest.skip("the checkout has no git directory to read")
    assert len(head) == 40
    assert set(head) <= set("0123456789abcdef")


def test_a_crash_returns_three_and_does_not_look_like_a_verdict(
    five: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(contract: object, directory: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "verify", boom)
    assert run(*snapshot_args(five, tmp_path / "out")) == cli.EXIT_INTERNAL
    printed = capsys.readouterr().err
    assert "internal error" in printed
    assert not (tmp_path / "out" / "report.json").exists()


def test_write_snapshots_writes_one_file_per_captured_variant(tmp_path: Path) -> None:
    simulation = simulate()
    written = cli._write_snapshots(simulation.attempts, tmp_path)
    assert len(written) == 6
    assert {path.name for path in written} == {
        "baseline.json",
        "full.json",
        "p1_raise_limit.json",
        "partial_worker_only.json",
        "partial_frontend_only.json",
        "noop.json",
    }
    assert all(path.is_file() for path in written)


def test_write_snapshots_warns_about_a_variant_that_never_captured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    simulation = simulate()
    attempts = [replace(simulation.attempt("baseline"), end=None)]
    assert cli._write_snapshots(attempts, tmp_path) == ()
    assert "no snapshot written for baseline" in capsys.readouterr().err


def test_the_module_entry_point_is_the_console_entry_point() -> None:
    from verifier.__main__ import main as module_main

    assert module_main is cli.main
