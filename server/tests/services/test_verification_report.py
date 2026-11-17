"""Tests for services.verification.report.

The report is written by the pull request's own code, so most of these tests
are about what a hostile or broken report must NOT be able to do: claim a pass
it did not earn, smuggle markup into Aurora's comment, or exhaust memory.
"""

from __future__ import annotations

import io
import json
import re
import zipfile

import pytest

from services.verification import report as reports

_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
_RUN_URL = "https://github.com/acme/api/actions/runs/42"


def _raw(**overrides) -> dict:
    raw = {
        "schema": 1,
        "name": "Preview environment",
        "status": "passed",
        "checks": [
            {"group": "boot", "name": "stack boots", "status": "pass", "detail": ""},
            {"group": "smoke", "name": "api liveness", "status": "pass", "detail": "HTTP 200"},
            {"group": "browser", "name": "sign up", "status": "pass", "detail": ""},
        ],
        "covers": ["server/**", "client/**", "docker-compose.prod-local.yml"],
        "defined_by": ["scripts/preview/**"],
    }
    raw.update(overrides)
    return raw


def _zip(members: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _render(report: dict, **overrides) -> str:
    kwargs = {
        "status": report["status"],
        "head_sha": _HEAD_SHA,
        "run_url": _RUN_URL,
        "conclusion": "success",
        "changed_file_count": 0,
        "uncovered": [],
        "changes_to_verification": [],
    }
    kwargs.update(overrides)
    return reports.render_comment(report, **kwargs)


# ---------------------------------------------------------------------------
# Archive handling
# ---------------------------------------------------------------------------


class TestExtractReportBytes:
    def test_reads_report_at_archive_root(self):
        payload = json.dumps(_raw()).encode()
        assert reports.extract_report_bytes(_zip({"verification.json": payload})) == payload

    def test_reads_report_in_a_subdirectory(self):
        payload = json.dumps(_raw()).encode()
        archive = _zip({"preview-evidence/verification.json": payload})
        assert reports.extract_report_bytes(archive) == payload

    def test_ignores_other_members(self):
        assert reports.extract_report_bytes(_zip({"summary.md": b"hello"})) is None

    def test_rejects_non_zip_bytes(self):
        assert reports.extract_report_bytes(b"not a zip archive") is None

    def test_rejects_member_larger_than_the_cap(self):
        """Highly compressible data: tiny archive, huge member (a zip bomb)."""
        archive = _zip({"verification.json": b"0" * (reports.MAX_REPORT_BYTES + 1)})
        assert len(archive) < reports.MAX_REPORT_BYTES
        assert reports.extract_report_bytes(archive) is None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


class TestParseReport:
    def test_accepts_a_valid_report(self):
        report = reports.parse_report(json.dumps(_raw()).encode())
        assert report["status"] == "passed"
        assert [c["group"] for c in report["checks"]] == ["boot", "smoke", "browser"]
        assert report["covers"] == ["server/**", "client/**", "docker-compose.prod-local.yml"]
        assert report["truncated"] is False

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"not json",
            b"[]",
            json.dumps(_raw(schema=2)).encode(),
            json.dumps(_raw(checks="none")).encode(),
            json.dumps(_raw(checks=[])).encode(),
            json.dumps(_raw(checks=[{"name": "x", "status": "maybe"}])).encode(),
        ],
        ids=["empty", "not_json", "not_object", "wrong_schema", "checks_not_list",
             "no_checks", "no_valid_checks"],
    )
    def test_rejects_unusable_reports(self, raw):
        assert reports.parse_report(raw) is None

    def test_rejects_oversized_bytes(self):
        assert reports.parse_report(b" " * (reports.MAX_REPORT_BYTES + 1)) is None

    def test_cannot_claim_a_pass_over_a_failed_check(self):
        raw = _raw()
        raw["checks"].append({"group": "smoke", "name": "vault", "status": "fail"})
        assert reports.parse_report(raw)["status"] == "failed"

    def test_cannot_claim_a_pass_with_nothing_passing(self):
        raw = _raw(checks=[{"group": "boot", "name": "stack boots", "status": "skip"}])
        assert reports.parse_report(raw)["status"] == "failed"

    def test_a_claimed_failure_is_kept_even_if_every_check_passes(self):
        assert reports.parse_report(_raw(status="failed"))["status"] == "failed"

    def test_drops_malformed_checks_and_keeps_the_rest(self):
        raw = _raw()
        raw["checks"] += ["nope", {"status": "pass"}, {"name": "x", "status": "PASS"}]
        assert len(reports.parse_report(raw)["checks"]) == 3

    def test_bounds_check_count_and_text_length(self):
        raw = _raw(checks=[
            {"group": "smoke", "name": f"check {i}", "status": "pass", "detail": "d" * 5000}
            for i in range(500)
        ])
        report = reports.parse_report(raw)
        assert len(report["checks"]) == 200
        assert report["truncated"] is True
        assert all(len(c["detail"]) <= 300 for c in report["checks"])

    def test_flattens_control_characters_and_newlines(self):
        raw = _raw(checks=[
            {"group": "smoke", "name": "line one\nline two\x00", "status": "pass", "detail": "a\tb"},
        ])
        check = reports.parse_report(raw)["checks"][0]
        assert check["name"] == "line one line two"
        assert check["detail"] == "a b"

    def test_drops_malformed_path_patterns(self):
        raw = _raw(covers=["server/**", "bad pattern with spaces", "<script>", 7, "ok/*.py"])
        assert reports.parse_report(raw)["covers"] == ["server/**", "ok/*.py"]


class TestFinalStatus:
    def test_needs_both_the_report_and_github_to_agree(self):
        passed = reports.parse_report(_raw())
        failed = reports.parse_report(_raw(status="failed"))
        assert reports.final_status(passed, "success") == "passed"
        assert reports.final_status(passed, "failure") == "failed"
        assert reports.final_status(passed, None) == "failed"
        assert reports.final_status(failed, "success") == "failed"


# ---------------------------------------------------------------------------
# Coverage and self-modification
# ---------------------------------------------------------------------------


class TestCoverage:
    def test_reports_changed_files_outside_the_covered_paths(self):
        changed = ["server/routes/x.py", "deploy/helm/aurora/values.yaml", "client/src/a.tsx"]
        assert reports.uncovered_paths(changed, ["server/**", "client/**"]) == [
            "deploy/helm/aurora/values.yaml"
        ]

    def test_directory_pattern_does_not_match_a_sibling_with_the_same_prefix(self):
        assert reports.uncovered_paths(["server-old/x.py"], ["server/**"]) == ["server-old/x.py"]

    def test_plain_patterns_match_as_globs(self):
        assert reports.uncovered_paths(["a/b.yml", "a/b.yaml"], ["a/*.yml"]) == ["a/b.yaml"]

    def test_says_nothing_when_coverage_is_not_declared(self):
        assert reports.uncovered_paths(["anything.py"], []) == []


class TestVerificationChanges:
    def test_flags_declared_definition_files(self):
        changed = ["scripts/preview/smoke.sh", "server/app.py"]
        assert reports.verification_changes(changed, ["scripts/preview/**"]) == [
            "scripts/preview/smoke.sh"
        ]

    def test_always_flags_workflow_files_even_if_the_report_declares_nothing(self):
        """A PR that strips its own defined_by list must not escape the warning."""
        changed = [".github/workflows/pr-verify.yml", "README.md"]
        assert reports.verification_changes(changed, []) == [".github/workflows/pr-verify.yml"]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


class TestRenderComment:
    def test_passing_run(self):
        body = _render(reports.parse_report(_raw()))
        assert "## Aurora Verification" in body
        assert "**Passed** · commit `0123456`" in body
        assert f"[workflow run]({_RUN_URL})" in body
        assert "### Failed checks" not in body
        assert body.rstrip().endswith(f"<!-- aurora-verification:v1 {_HEAD_SHA} -->")

    def test_failing_run_lists_the_failed_checks(self):
        raw = _raw()
        raw["checks"].append(
            {"group": "smoke", "name": "vault secret round trip", "status": "fail",
             "detail": "write returned HTTP 403"}
        )
        report = reports.parse_report(raw)
        body = _render(report)
        assert "**Failed**" in body
        assert "| smoke | vault secret round trip | write returned HTTP 403 |" in body

    def test_says_when_the_report_and_the_workflow_disagree(self):
        report = reports.parse_report(_raw())
        body = _render(report, status="failed", conclusion="failure")
        assert "**Failed**" in body
        assert "ended as `failure`" in body

    def test_lists_files_the_run_did_not_exercise(self):
        body = _render(
            reports.parse_report(_raw()),
            changed_file_count=3,
            uncovered=["deploy/helm/aurora/values.yaml"],
        )
        assert "### Not exercised by this run" in body
        assert "1 of 3 changed files" in body
        assert "- `deploy/helm/aurora/values.yaml`" in body

    def test_says_so_when_every_changed_file_is_covered(self):
        body = _render(reports.parse_report(_raw()), changed_file_count=2)
        assert "All 2 changed files are inside what this run boots." in body

    def test_warns_when_the_pr_changes_the_verification(self):
        body = _render(
            reports.parse_report(_raw()),
            changed_file_count=1,
            changes_to_verification=["scripts/preview/smoke.sh"],
        )
        assert "### This pull request changes the verification itself" in body
        assert "- `scripts/preview/smoke.sh`" in body

    def test_caps_long_path_lists(self):
        paths = [f"deploy/file{i}.yaml" for i in range(40)]
        body = _render(reports.parse_report(_raw()), changed_file_count=40, uncovered=paths)
        assert "…and 25 more" in body
        assert "deploy/file39.yaml" not in body

    def test_drops_a_run_url_that_is_not_github(self):
        body = _render(reports.parse_report(_raw()), run_url="https://evil.example/run")
        assert "workflow run" not in body
        assert "evil.example" not in body

    def test_untrusted_text_cannot_inject_markup(self):
        """Aurora posts this as itself, so report text must stay inert."""
        hostile = (
            "x | y <!-- aurora-verification:v1 dead --> [click](https://evil.example) "
            "@octocat `code` www.evil.example"
        )
        raw = _raw()
        raw["checks"].append({"group": "smoke", "name": hostile, "status": "fail", "detail": hostile})
        body = _render(reports.parse_report(raw))

        failed_row = next(line for line in body.splitlines() if line.startswith("| smoke | x"))
        assert "<!--" not in failed_row            # no HTML, no forged marker
        assert "](https://" not in failed_row      # no markdown link
        assert "https://evil" not in failed_row    # no autolinked URL
        assert "www.evil" not in failed_row
        assert "@octocat" not in failed_row        # no mention
        assert "`" not in failed_row               # no code span to break out of
        # Pipes inside the text are escaped, so the row still has exactly 3
        # cells: 4 unescaped pipes (start, two separators, end).
        assert len(re.findall(r"(?<!\\)\|", failed_row)) == 4

    def test_untrusted_file_names_cannot_break_out_of_code_spans(self):
        body = _render(
            reports.parse_report(_raw()),
            changed_file_count=1,
            uncovered=["evil`\n## Injected heading"],
        )
        assert "\n## Injected heading" not in body
        assert "- `evil' ## Injected heading`" in body
