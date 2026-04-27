#!/usr/bin/env python3
"""Generate the L5 output-redaction pattern module from a pinned gitleaks TOML.

Reads ``server/utils/security/rules/gitleaks-v<ver>.toml`` plus the Aurora
rule allowlist (``_aurora_filter.ALLOWED_RULE_IDS``), and emits
``server/utils/security/_generated_patterns.py`` as a reviewable Python
module of precompiled patterns.

Why codegen instead of runtime TOML parsing: the generated module diffs
cleanly on rule-version bumps so reviewers can see exactly which regexes
changed. Build-time failures are preferable to silent runtime mismatches.

Run: ``python scripts/gen_secret_patterns.py``
CI:  ``python scripts/gen_secret_patterns.py --check`` fails on drift.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import tomllib
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RULES_DIR = REPO_ROOT / "server" / "utils" / "security" / "rules"
OUT_PATH = REPO_ROOT / "server" / "utils" / "security" / "_generated_patterns.py"
TOML_GLOB = "gitleaks-v*.toml"

_INLINE_I = re.compile(r"\(\?i\)")


def _hoist_inline_flags(rule_id: str, pattern: str) -> str:
    """Python's ``re`` rejects mid-expression ``(?i)``; Go's regexp allows it.

    For Gitleaks rules that embed ``(?i)`` mid-pattern to case-insensitize a
    trailing character class, hoisting the flag to the start preserves the
    matched set (the flag only widens, never narrows, and the preceding
    literal portions of these rules are already lowercase).
    """
    try:
        re.compile(pattern)
        return pattern
    except re.error:
        pass
    stripped = _INLINE_I.sub("", pattern)
    if stripped == pattern:
        raise SystemExit(f"rule {rule_id!r}: regex incompatible with Python re")
    hoisted = "(?i)" + stripped
    try:
        re.compile(hoisted)
    except re.error as exc:
        raise SystemExit(f"rule {rule_id!r}: cannot normalize regex: {exc}") from exc
    return hoisted


def _find_toml() -> pathlib.Path:
    matches = sorted(RULES_DIR.glob(TOML_GLOB))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one {TOML_GLOB} in {RULES_DIR}, found {len(matches)}"
        )
    return matches[0]


def _load_allowlist() -> frozenset[str]:
    sys.path.insert(0, str(RULES_DIR))
    try:
        from _aurora_filter import ALLOWED_RULE_IDS  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)
    return ALLOWED_RULE_IDS


def _emit_rule(rule: dict[str, Any]) -> str:
    rid = rule["id"]
    pattern = _hoist_inline_flags(rid, rule["regex"])
    keywords = tuple(sorted({k.lower() for k in rule.get("keywords") or ()}))
    entropy = float(rule["entropy"]) if "entropy" in rule else 0.0

    allowlist_parts: list[tuple[str, str]] = []
    stopwords: set[str] = set()
    for al in rule.get("allowlists") or ():
        target = al.get("regexTarget", "match")
        for r in al.get("regexes") or ():
            allowlist_parts.append((target, _hoist_inline_flags(f"{rid}.allowlist", r)))
        for w in al.get("stopwords") or ():
            stopwords.add(w.lower())

    return (
        "    Rule(\n"
        f"        id={rid!r},\n"
        f"        regex=re.compile({pattern!r}),\n"
        f"        keywords={keywords!r},\n"
        f"        entropy_min={entropy!r},\n"
        "        allowlist=("
        + ", ".join(f"({t!r}, re.compile({r!r}))" for t, r in allowlist_parts)
        + (",)" if len(allowlist_parts) == 1 else ")")
        + ",\n"
        f"        stopwords={tuple(sorted(stopwords))!r},\n"
        "    ),"
    )


def _emit_global_allowlist(data: dict[str, Any]) -> tuple[list[str], list[str]]:
    al = data.get("allowlist") or {}
    g_regexes: list[str] = []
    for r in al.get("regexes") or ():
        g_regexes.append(_hoist_inline_flags("global.allowlist", r))
    stopwords = sorted({w.lower() for w in al.get("stopwords") or ()})
    return g_regexes, stopwords


def _render(toml_path: pathlib.Path, allowed: frozenset[str]) -> str:
    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    rules_in = data.get("rules") or []
    seen = {r["id"] for r in rules_in}
    unknown = allowed - seen
    if unknown:
        raise SystemExit(f"Allowlist has unknown rule IDs: {sorted(unknown)}")

    selected = [r for r in rules_in if r["id"] in allowed]
    rule_blocks = "\n".join(_emit_rule(r) for r in selected)

    g_regexes, g_stopwords = _emit_global_allowlist(data)
    g_regex_tuple = ", ".join(f"re.compile({r!r})" for r in g_regexes)

    return f'''"""Auto-generated from {toml_path.name} by scripts/gen_secret_patterns.py.

Do not edit by hand. Regenerate with ``python scripts/gen_secret_patterns.py``
and commit the result alongside the updated TOML.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern[str]
    keywords: tuple[str, ...]
    entropy_min: float
    allowlist: tuple[tuple[str, re.Pattern[str]], ...]
    stopwords: tuple[str, ...]


SOURCE_FILE = {toml_path.name!r}

RULES: tuple[Rule, ...] = (
{rule_blocks}
)


GLOBAL_ALLOWLIST_REGEXES: tuple[re.Pattern[str], ...] = ({g_regex_tuple},)
GLOBAL_STOPWORDS: frozenset[str] = frozenset({g_stopwords!r})
'''


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="Fail if the generated file would change (CI mode).")
    args = ap.parse_args()

    toml_path = _find_toml()
    allowed = _load_allowlist()
    rendered = _render(toml_path, allowed)

    if args.check:
        existing = OUT_PATH.read_text() if OUT_PATH.exists() else ""
        if existing != rendered:
            print(
                f"{OUT_PATH.relative_to(REPO_ROOT)} is stale; run "
                "`python scripts/gen_secret_patterns.py`.",
                file=sys.stderr,
            )
            return 1
        return 0

    OUT_PATH.write_text(rendered)
    print(f"Wrote {OUT_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
