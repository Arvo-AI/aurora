"""check_change on multi-file PRs: one relevant file among unrelated ones.

Each scenario's real change script (break.sh) becomes one file of a 16-file
unified diff; the other 15 files are real Aurora source modules added
wholesale, as code noise. Scoring only the whole diff dilutes the relevant
file to a sliver of the query (and can push it past the term cap); scoring
each file on its own as well is what lets it match.

Run with -s to print the table.
"""

from __future__ import annotations

import pathlib

import pytest

from services.memory import change_check as cc
from services.memory.retrieval import MemoryIndex
from tests.services.memory import aurora_lab_corpus as lab

SERVER_DIR = pathlib.Path(__file__).resolve().parents[3]
NOISE_FILES = 15
NOISE_LINES = 60


def _section(path: str, lines) -> str:
    body = "\n".join("+" + line for line in lines)
    return f"diff --git a/{path} b/{path}\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _noise_sections():
    files = sorted((SERVER_DIR / "utils").glob("*.py"))[:NOISE_FILES]
    return [_section(f"server/utils/{f.name}", f.read_text().splitlines()[:NOISE_LINES]) for f in files]


@pytest.fixture(scope="module")
def setup():
    scenarios = [s for s in lab.load_scenarios() if s.break_sh]
    noise = _noise_sections()
    diffs = {
        s.id: "\n".join([*noise[:7], _section(f"deploy/{s.id}.sh", s.break_sh.splitlines()), *noise[7:]])
        for s in scenarios
    }
    full = MemoryIndex(lab.memory_bank(scenarios))
    loo = {s.id: MemoryIndex(lab.memory_bank(scenarios, skip=s.id)) for s in scenarios}
    return scenarios, diffs, full, loo


def _run(setup, per_file: bool):
    scenarios, diffs, full, loo = setup
    old = cc.MAX_FILE_QUERIES
    cc.MAX_FILE_QUERIES = old if per_file else 0
    try:
        hit = fp = 0
        for s in scenarios:
            res = cc.check_change(full, diff=diffs[s.id], description="Routine maintenance")
            hit += any(h["entry_id"] == f"pm-{s.id}" for h in res["related_incidents"])
            fp += cc.check_change(loo[s.id], diff=diffs[s.id], description="Routine maintenance")["status"] != "NO_MATCH"
        n = len(scenarios)
        return {"n": n, "hit@3": hit / n, "fp_rate": fp / n}
    finally:
        cc.MAX_FILE_QUERIES = old


def test_multifile_diffs_split_into_one_section_per_file(setup):
    _, diffs, _, _ = setup
    assert len(cc.split_diff(next(iter(diffs.values())))) == NOISE_FILES + 1


def test_per_file_scoring_recovers_the_relevant_file(setup):
    whole = _run(setup, per_file=False)
    per_file = _run(setup, per_file=True)
    print(
        "\n| scoring | right incident in top 3 | false-match rate (leave-one-out) |\n|---|---:|---:|\n"
        f"| whole diff only | {whole['hit@3']:.2f} | {whole['fp_rate']:.2f} |\n"
        f"| whole diff + per file | {per_file['hit@3']:.2f} | {per_file['fp_rate']:.2f} |"
    )
    assert per_file["hit@3"] > whole["hit@3"]
    assert per_file["hit@3"] >= 0.75
    assert per_file["fp_rate"] <= 0.5
