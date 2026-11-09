"""Shared loading and measurement code for the Recall retrieval evidence.

Used by test_retrieval_quality.py, test_check_change_replay.py and
scripts/eval_memory_retrieval.py, so the PR tables and the CI assertions come
from one implementation.

Corpus: the 21 published runbooks of Arvo-AI/aurora-lab (fixtures/aurora_lab,
unmodified, see NOTICE). Each README becomes a ``runbook`` memory entry
(title = H1, description = the italic summary line, content = the body) with
every scenario's alert line removed, so no query can win by exact copy.

Baselines are Aurora's pre-Recall rankers:

* ``legacy_search_runbooks``: a frozen copy of the original all-tokens
  title/description filter of the ``search_runbooks`` MCP tool.
* ``legacy_shortlist``: the real ``_shortlist_entries`` from
  services/memory/injector.py, loaded from source with ``ast`` (importing the
  module pulls in the LLM stack).
"""

from __future__ import annotations

import ast
import pathlib
import random
import re
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

import yaml

SERVER_DIR = pathlib.Path(__file__).resolve().parents[3]
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "aurora_lab"
QUERY_SETS = ("alert", "change", "root_cause")
SHUFFLES = 20


# --------------------------------------------------------------------------- corpus
@dataclass(frozen=True)
class Scenario:
    id: str
    data: Dict[str, Any]
    readme: str
    break_sh: str  # "" when the scenario ships no break.sh

    @property
    def title(self) -> str:
        return self.data.get("title", self.id)


def load_scenarios() -> List[Scenario]:
    out = []
    for d in sorted(p for p in FIXTURES.iterdir() if p.is_dir()):
        brk = d / "break.sh"
        out.append(Scenario(
            id=d.name,
            data=yaml.safe_load((d / "scenario.yaml").read_text()),
            readme=(d / "README.md").read_text(),
            break_sh=brk.read_text() if brk.exists() else "",
        ))
    return out


def runbook_entries(scenarios: Sequence[Scenario]) -> List[Dict]:
    alerts = [s.data["alert"].strip() for s in scenarios]
    docs = []
    for s in scenarios:
        lines = s.readme.splitlines()
        title = lines[0].lstrip("# ").strip()
        desc = next((l.strip().strip("*") for l in lines[1:] if l.strip().startswith("*")), "")
        body = [l for l in lines[1:] if not any(a in l for a in alerts)]
        docs.append({"id": s.id, "title": title, "description": desc,
                     "content": "\n".join(body), "category": "runbook", "updated_at": None})
    return docs


def queries(scenarios: Sequence[Scenario]) -> Dict[str, List[Tuple[str, str]]]:
    return {k: [(s.id, " ".join(str(s.data.get(k, "")).split())) for s in scenarios]
            for k in QUERY_SETS}


# --------------------------------------------------------------------------- legacy rankers
def legacy_search_runbooks(docs: Sequence[Dict], query: str, limit: int = 5) -> List[str]:
    """Frozen copy of the original ``search_runbooks`` memory filter.

    From commit d2c193a, server/aurora_mcp/tools_always_on.py:93-120: every
    whitespace-separated query token must appear in title+description
    (substring, case-insensitive); input order, truncated to *limit*.
    """
    tokens = query.lower().split()
    out = []
    for e in docs:
        searchable = f"{e.get('title', '') or ''} {e.get('description', '') or ''}".lower()
        if all(tok in searchable for tok in tokens):
            out.append(e["id"])
    return out[:limit]


def _load_defs(path: pathlib.Path, names: set, ns: Dict[str, Any]) -> Dict[str, Any]:
    tree = ast.parse(path.read_text())
    body = [n for n in tree.body
            if (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names)
            or (isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in names for t in n.targets))]
    found = {getattr(n, "name", None) or n.targets[0].id for n in body}
    assert names <= found, f"not found in {path}: {names - found}"
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), ns)  # noqa: S102
    return ns


_injector: Optional[Dict[str, Any]] = None


def _injector_ns() -> Dict[str, Any]:
    global _injector
    if _injector is None:
        _injector = _load_defs(
            SERVER_DIR / "services/memory/injector.py",
            {"_WORD_RE", "_UNSPACED_RE", "_STOPWORDS", "_tokens", "_as_naive", "_shortlist_entries"},
            {"re": re, "datetime": datetime, "Dict": Dict, "List": List, "Set": set},
        )
    return _injector


def legacy_shortlist(docs: Sequence[Dict], query: str, seed: int) -> List[str]:
    """Injector pre-LLM ranking. Ties fall back to input order, so callers average over seeds."""
    pool = [dict(d) for d in docs]
    random.Random(seed).shuffle(pool)
    return [e["id"] for e in _injector_ns()["_shortlist_entries"](query, pool, len(pool))]


# --------------------------------------------------------------------------- quality
def metrics(gold: str, ranking: Sequence[str]) -> Dict[str, float]:
    r = ranking.index(gold) + 1 if gold in ranking else None
    return {"r@1": float(r == 1), "r@5": float(r is not None and r <= 5), "mrr": 1 / r if r else 0.0}


METHODS = ("legacy search_runbooks (MCP)", "legacy injector shortlist", "MemoryIndex (Recall)")


def quality(scenarios: Optional[Sequence[Scenario]] = None) -> Dict[str, Dict[str, Dict[str, float]]]:
    """{query_set: {method: {r@1, r@5, mrr}}} averaged over the scenarios' queries."""
    from services.memory.retrieval import MemoryIndex

    scenarios = scenarios or load_scenarios()
    docs = runbook_entries(scenarios)
    index = MemoryIndex(docs)
    results: Dict[str, Dict[str, Dict[str, float]]] = {}
    for qset, qs in queries(scenarios).items():
        acc: Dict[str, Counter] = {m: Counter() for m in METHODS}
        for gold, q in qs:
            acc[METHODS[0]].update(metrics(gold, legacy_search_runbooks(docs, q, 5)))
            sh: Counter = Counter()
            for seed in range(SHUFFLES):
                sh.update(metrics(gold, legacy_shortlist(docs, q, seed)))
            acc[METHODS[1]].update({k: v / SHUFFLES for k, v in sh.items()})
            ranking = [h.entry_id for h in index.search(q, k=len(docs))]
            acc[METHODS[2]].update(metrics(gold, ranking))
        results[qset] = {m: {k: v / len(qs) for k, v in c.items()} for m, c in acc.items()}
    return results


def quality_markdown(results: Dict[str, Dict[str, Dict[str, float]]], n: int = 21) -> str:
    lines = [f"| query set ({n} queries) | method | recall@1 | recall@5 | MRR |", "|---|---|---:|---:|---:|"]
    for qset, rows in results.items():
        for method, m in rows.items():
            lines.append(f"| {qset} | {method} | {m['r@1']:.2f} | {m['r@5']:.2f} | {m['mrr']:.2f} |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- check_change replay
def memory_bank(scenarios: Sequence[Scenario], skip: Optional[str] = None) -> List[Dict]:
    """One postmortem (root cause as content) and one runbook per scenario; *skip* leaves one out."""
    bank = []
    for s, rb in zip(scenarios, runbook_entries(scenarios)):
        if s.id == skip:
            continue
        bank.append({"id": f"pm-{s.id}", "category": "postmortem", "title": f"Postmortem: {s.title}",
                     "description": "", "content": " ".join(str(s.data["root_cause"]).split()),
                     "updated_at": None})
        bank.append(rb)
    return bank


@contextmanager
def change_check_constants(threshold: float, min_strong: int) -> Iterator[None]:
    from services.memory import change_check as cc

    old = (cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS)
    cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS = threshold, min_strong
    try:
        yield
    finally:
        cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS = old


class Replay:
    """Indexes built once (full bank and each leave-one-out bank); constants applied per run."""

    def __init__(self, scenarios: Optional[Sequence[Scenario]] = None):
        from services.memory.retrieval import MemoryIndex

        self.scenarios = list(scenarios or load_scenarios())
        self.full = MemoryIndex(memory_bank(self.scenarios))
        self.loo = {s.id: MemoryIndex(memory_bank(self.scenarios, skip=s.id)) for s in self.scenarios}

    def inputs(self, s: Scenario, kind: str) -> Optional[Dict[str, str]]:
        if kind == "change":
            return {"description": " ".join(str(s.data["change"]).split())}
        if kind == "break.sh":
            return {"diff": s.break_sh} if s.break_sh else None
        raise ValueError(kind)

    def run(self, kind: str, threshold: Optional[float] = None, min_strong: Optional[int] = None) -> Dict[str, float]:
        from services.memory import change_check as cc

        thr = cc.MATCH_THRESHOLD if threshold is None else threshold
        ms = cc.MIN_STRONG_TERMS if min_strong is None else min_strong
        n = hit = top1 = matched = matched_right = loo_n = loo_fp = 0
        with change_check_constants(thr, ms):
            for s in self.scenarios:
                kw = self.inputs(s, kind)
                if kw is None:
                    continue
                n += 1
                res = cc.check_change(self.full, k=3, **kw)
                ids = [h["entry_id"] for h in res["related_incidents"]]
                right = f"pm-{s.id}" in ids
                hit += right
                top1 += bool(ids) and ids[0] == f"pm-{s.id}"
                if res["status"] == "MATCHES_PAST_INCIDENT":
                    matched += 1
                    matched_right += right
                loo_n += 1
                loo_fp += cc.check_change(self.loo[s.id], k=3, **kw)["status"] == "MATCHES_PAST_INCIDENT"
        return {
            "n": n,
            "hit@3": hit / n if n else 0.0,           # right postmortem among related_incidents (k=3)
            "top1": top1 / n if n else 0.0,
            "match_rate": matched / n if n else 0.0,  # status MATCH on the full bank
            "precision": matched_right / matched if matched else 1.0,  # MATCH results naming the right postmortem
            "fp_rate": loo_fp / loo_n if loo_n else 0.0,  # MATCH after removing the scenario's own memory
        }


GRID_THRESHOLDS = (4.5, 6.0, 8.0, 10.0, 14.0, 15.0, 18.0, 24.0)
GRID_MIN_STRONG = (1, 2, 3, 4)


def sweep(replay: Replay, kind: str) -> List[Dict[str, float]]:
    rows = []
    for thr in GRID_THRESHOLDS:
        for ms in GRID_MIN_STRONG:
            r = replay.run(kind, thr, ms)
            rows.append({"threshold": thr, "min_strong": ms, **r})
    return rows


def sweep_markdown(rows: List[Dict[str, float]], kind: str, current: Tuple[float, int]) -> str:
    lines = [f"| MATCH_THRESHOLD | MIN_STRONG_TERMS | status MATCH (full bank) | precision | hit@3 | false-positive rate (leave-one-out) |",
             "|---:|---:|---:|---:|---:|---:|"]
    for r in rows:
        mark = " (current)" if (r["threshold"], r["min_strong"]) == current else ""
        lines.append(f"| {r['threshold']:g}{mark} | {r['min_strong']} | {r['match_rate']:.2f} | "
                     f"{r['precision']:.2f} | {r['hit@3']:.2f} | {r['fp_rate']:.2f} |")
    return f"Input: {kind} (n={int(rows[0]['n'])})\n\n" + "\n".join(lines)
