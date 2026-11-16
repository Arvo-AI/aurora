"""The CI template is a deliverable, so its guarantees are a test.

The workflow never runs from this branch: GitHub only reads
`.github/workflows/`, and this project keeps the template in `ci/` (D10).
That gap is exactly why the properties are asserted here instead of observed:
a read-only token, actions pinned to commits, a timeout, no secrets, no
cluster, and a bundle that survives a failing gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / "ci" / "github-actions-verify.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DOCUMENT: dict[Any, Any] = yaml.safe_load(TEXT)

# PyYAML reads the unquoted key `on` as the boolean True (YAML 1.1).
TRIGGERS = DOCUMENT.get("on") or DOCUMENT[True]
BUNDLE = "remediation-verifier-report"


def jobs() -> dict[str, Any]:
    return DOCUMENT["jobs"]


def steps() -> list[dict[str, Any]]:
    return [step for job in jobs().values() for step in job["steps"]]


def test_the_file_says_it_is_a_template() -> None:
    assert "Template, not an active workflow" in TEXT


def test_the_workflow_has_one_named_job() -> None:
    assert DOCUMENT["name"] == "remediation-verifier"
    assert list(jobs()) == ["verify"]


def test_it_runs_on_a_push_to_the_ci_branch_and_on_demand() -> None:
    assert TRIGGERS["push"]["branches"] == ["ci"]
    assert TRIGGERS["workflow_dispatch"]["inputs"]["ref"]["required"] is True
    assert set(TRIGGERS) == {"push", "workflow_dispatch"}


def test_the_token_is_read_only() -> None:
    assert DOCUMENT["permissions"] == {"contents": "read"}


def test_every_job_has_a_runner_and_a_timeout() -> None:
    for name, job in jobs().items():
        assert job["runs-on"] == "ubuntu-24.04", name
        assert job["timeout-minutes"] <= 15, name


def test_superseded_runs_are_cancelled() -> None:
    concurrency = DOCUMENT["concurrency"]
    assert concurrency["group"].startswith("rv-")
    assert concurrency["cancel-in-progress"] is True


def test_every_action_is_pinned_to_a_commit_with_its_tag() -> None:
    for step in steps():
        reference = step.get("uses")
        if reference is None:
            continue
        name, _, revision = reference.partition("@")
        assert re.fullmatch(r"[0-9a-f]{40}", revision), name
        assert re.search(rf"{revision} # v\d", TEXT), name


def test_the_workflow_carries_no_secret() -> None:
    assert "secrets." not in TEXT


def test_every_gate_the_contract_names_runs() -> None:
    commands = " ".join(step.get("run", "") for step in steps())
    for target in ("lint", "typecheck", "test", "verify-snapshot"):
        assert f"make -C remediation-verifier {target}" in commands, target


def test_the_workflow_needs_no_cluster() -> None:
    assert "kind" not in TEXT
    assert "kubectl" not in TEXT
    assert "docker" not in TEXT


def test_the_bundle_is_uploaded_even_when_a_gate_fails() -> None:
    uploads = [
        step for step in steps() if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    assert len(uploads) == 1
    step = uploads[0]
    assert step["if"] == "always()"
    assert step["with"]["name"] == BUNDLE
    assert step["with"]["path"] == "remediation-verifier/out"
    assert step["with"]["if-no-files-found"] == "warn"
    assert step["with"]["retention-days"] <= 30


def test_the_workflow_publishes_nothing_but_the_bundle() -> None:
    assert "git push" not in TEXT
    assert "docker push" not in TEXT
    assert "gh pr" not in TEXT
