"""Smoke tests: the package installs, imports, and its entry points respond.

These are not the verification tests. They guard the delivery surface: the
version the package reports, the parser the console script exposes, and the
environment report a reader runs first.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
import yaml

import verifier
from verifier import cli, doctor

PROJECT_FILE = Path(__file__).resolve().parent.parent / "pyproject.toml"


def declared_version() -> str:
    with PROJECT_FILE.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    return str(project["version"])


class TestPackage:
    def test_reported_version_matches_pyproject(self) -> None:
        assert verifier.__version__ == declared_version()

    def test_pyyaml_parses_a_contract_shaped_document(self) -> None:
        document = yaml.safe_load(
            """
            scenario: 12-two-faults-oom-and-selector
            checks:
              - no_oomkilled_in_window
              - http_expected_response
            """
        )
        assert document["scenario"] == "12-two-faults-oom-and-selector"
        assert len(document["checks"]) == 2


class TestConsoleScript:
    def test_version_flag_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exit_info:
            cli.main(["--version"])
        assert exit_info.value.code == 0
        assert verifier.__version__ in capsys.readouterr().out

    def test_default_invocation_verifies_from_snapshots(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        argv = ["--fixtures", str(tmp_path), "--out", str(tmp_path / "out")]
        assert cli.main(argv) == cli.EXIT_NOT_RUN
        assert "has no snapshot" in capsys.readouterr().err


class TestDoctor:
    def test_every_check_carries_a_name_a_status_and_a_detail(self) -> None:
        checks = doctor.run_checks()
        assert checks
        for check in checks:
            assert check.name
            assert check.detail
            assert check.status in tuple(doctor.Status)

    def test_checks_are_sorted_so_the_report_is_stable(self) -> None:
        names = [check.name for check in doctor.run_checks()]
        assert names == sorted(names)

    def test_the_interpreter_running_the_tests_is_supported(self) -> None:
        statuses = {check.name: check.status for check in doctor.run_checks()}
        assert statuses["python"] is doctor.Status.OK

    def test_pyyaml_is_present(self) -> None:
        statuses = {check.name: check.status for check in doctor.run_checks()}
        assert statuses["pyyaml"] is doctor.Status.OK

    def test_json_output_is_parseable_and_complete(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        exit_code = doctor.main(["--json"])
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload
        assert set(payload[0]) == {"name", "status", "detail"}

    def test_text_output_names_the_available_mode(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert doctor.main([]) == 0
        output = capsys.readouterr().out
        assert "summary:" in output
        assert "available mode:" in output

    def test_missing_lab_is_a_warning_not_a_failure(self, tmp_path: Path) -> None:
        checks = doctor.run_checks(lab_dir=tmp_path)
        lab = next(check for check in checks if check.name == "lab checkout")
        assert lab.status is doctor.Status.WARN
