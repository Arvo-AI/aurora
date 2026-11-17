"""The Makefile is an entry point, so its shape is part of the contract.

A target that loses its ``--mode`` flag still exits zero: the verifier would
decide the fixtures and print a table that looks like LIVE evidence. The same
is true of a target that quietly stops depending on the lab pin. These tests
read the file and fail on the drift, which is cheaper and more reliable than
running every target in the suite.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"
TEXT = MAKEFILE.read_text(encoding="utf-8")

REQUIRED = (
    "help",
    "install",
    "doctor",
    "lab-pin",
    "lab-up",
    "format",
    "lint",
    "lint-ci",
    "typecheck",
    "test",
    "selfcheck",
    "verify-snapshot",
    "verify",
    "capture",
    "ci",
    "clean",
)

Rule = tuple[str, str]


def parse_rules(text: str) -> dict[str, Rule]:
    """Return target -> (prerequisites, recipe) for every line that names one.

    Assignment lines are not rules. They are left in the mapping under their
    own whitespace-collapsed name, so a malformed Makefile shows up as an
    unexpected key instead of being skipped in silence.
    """
    rules: dict[str, Rule] = {}
    current: str | None = None
    recipe: list[str] = []
    for line in text.splitlines():
        if line.startswith("\t"):
            recipe.append(line[1:])
            continue
        if current is not None:
            rules[current] = (" ".join(rules[current][0].split()), "\n".join(recipe))
            current = None
            recipe = []
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, separator, rest = line.partition(":")
        if not separator:
            continue
        current = name.strip()
        rules[current] = (rest.partition("##")[0], "")
    if current is not None:
        rules[current] = (" ".join(rules[current][0].split()), "\n".join(recipe))
    return rules


RULES = parse_rules(TEXT)


def test_every_target_the_assessment_names_exists() -> None:
    missing = sorted(set(REQUIRED) - set(RULES))
    assert missing == [], f"targets the Makefile does not define: {missing}"


def test_every_target_is_phony() -> None:
    phony = set(RULES[".PHONY"][0].split())
    missing = sorted(set(REQUIRED) - phony)
    assert missing == [], f"targets a same-named file could shadow: {missing}"


def test_every_target_carries_a_description() -> None:
    lines = TEXT.splitlines()
    missing = []
    for name in REQUIRED:
        line = next((item for item in lines if item.startswith(f"{name}:")), "")
        if "## " not in line:
            missing.append(name)
    assert missing == [], f"targets the help output would hide: {missing}"


def test_ci_runs_the_four_gates_in_order() -> None:
    prerequisites, recipe = RULES["ci"]
    assert prerequisites.split() == ["lint", "typecheck", "test", "verify-snapshot"]
    assert recipe == ""


@pytest.mark.parametrize(
    ("target", "mode"),
    [("verify-snapshot", "snapshot"), ("verify", "live"), ("capture", "capture")],
)
def test_each_target_calls_the_mode_it_names(target: str, mode: str) -> None:
    _, recipe = RULES[target]
    assert f"--mode {mode}" in recipe


def test_only_the_live_targets_name_the_lab() -> None:
    for target in ("verify", "capture"):
        assert '--lab "$(LAB)"' in RULES[target][1], target
    assert "--lab" not in RULES["verify-snapshot"][1]
    assert RULES["verify-snapshot"][0] == "", "the review path must not clone the lab"


def test_the_live_targets_need_the_pin_and_the_cluster() -> None:
    for target in ("verify", "capture"):
        assert RULES[target][0].split() == ["lab-pin", "lab-up"], target


def test_the_cluster_starts_from_the_pinned_checkout() -> None:
    assert RULES["lab-up"][0].split() == ["lab-pin"]


def test_the_capture_rewrites_the_committed_fixtures() -> None:
    assert "--fixtures fixtures/snapshots" in RULES["capture"][1]


def test_the_pin_comes_from_the_contract_and_not_a_literal() -> None:
    _, recipe = RULES["lab-pin"]
    assert "from verifier.config import LAB_COMMIT" in recipe
    assert re.search(r"\b[0-9a-f]{40}\b", TEXT) is None


def test_the_lab_lives_in_an_ignored_cache_directory() -> None:
    assert re.search(r"^LAB \?= \.cache/", TEXT, re.MULTILINE)
    ignored = (MAKEFILE.parent / ".gitignore").read_text(encoding="utf-8").split()
    assert ".cache/" in ignored


def test_the_lab_checkout_keeps_its_scripts_in_lf() -> None:
    """The workspace checkout of aurora-lab has CRLF and `lab.sh` refuses it."""
    _, recipe = RULES["lab-pin"]
    assert "core.autocrlf=false" in recipe
    assert "core.eol=lf" in recipe


def test_the_makefile_never_publishes_anything() -> None:
    assert "push" not in TEXT
    assert "gh pr" not in TEXT
