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


# MATCH_THRESHOLD is the absolute-score backstop beside MIN_COVERAGE.
GRID_THRESHOLDS = (4.5, 6.0, 8.0, 10.0, 14.0, 18.0, 24.0, 40.0)
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


# =========================================================================== robustness
# How does the check_change abstention rule hold up when the bank is not the
# tidy tuning corpus? Conditions: A baseline, B split-half calibration,
# C distractor docs, D long Aurora-style postmortems, E = C + D. Criteria are
# implemented here (eval code only) as functions of per-hit features.
import math  # noqa: E402

REPO_DIR = SERVER_DIR.parent
DOCS_DIR = REPO_DIR / "website" / "docs"
STRONG_IDF = 1.5  # same bar as change_check.STRONG_TERM_IDF

CRITERIA = ("absolute", "coverage", "margin", "strong-count")
GRIDS: Dict[str, List[float]] = {
    "absolute": [float(t) for t in range(6, 31)],
    "coverage": [round(0.05 + 0.025 * i, 3) for i in range(23)],        # 0.05 .. 0.60
    "margin": [round(1.1 + 0.1 * i, 1) for i in range(20)],             # 1.1 .. 3.0
    "strong-count": [float(n) for n in range(2, 9)],
}
CONDITIONS = {  # name -> (long postmortems, distractors)
    "A baseline": (False, False),
    "C distractors": (False, True),
    "D long postmortems": (True, False),
    "E C+D": (True, True),
}


def distractor_entries() -> List[Dict]:
    """website/docs/**/*.md and the lab's FINDINGS.md / REPORT.md as ``context`` entries."""
    paths = sorted(DOCS_DIR.rglob("*.md")) if DOCS_DIR.is_dir() else []
    paths += [p for p in (FIXTURES / "FINDINGS.md", FIXTURES / "REPORT.md") if p.exists()]
    out = []
    for p in paths:
        text = p.read_text(errors="replace")
        h1 = next((l[2:].strip() for l in text.splitlines() if l.startswith("# ")), "")
        out.append({"id": f"ctx-{p.name}-{len(out)}", "category": "context", "title": h1 or p.stem,
                    "description": "", "content": text, "updated_at": None})
    return out


def _as_list(v: Any) -> List[str]:
    if v is None:
        return []
    return [" ".join(str(x).split()) for x in (v if isinstance(v, list) else [v])]


_PM_BOILERPLATE = {
    "paged": "The on-call engineer was paged and acknowledged within 5 minutes.",
    "describe": "kubectl describe pod was used to inspect the pod.",
    "logs": "Recent container logs and Kubernetes events were reviewed for errors.",
    "slack": "An incident channel was opened and status updates were posted every 15 minutes.",
    "impact": "Customer-facing requests were degraded until the fix was rolled out; no data was lost.",
    "sla": "The error budget for the affected service was partially consumed during the incident.",
    "pressure": "The change was deployed late in the day without a second reviewer.",
    "alerts": "Alert routing worked as designed, but the alert text did not name the triggering change.",
    "docs": "The runbook for the affected service was not linked from the alert.",
    "ai1": "Review the deployment checklist with the team.",
    "ai2": "Add a pre-deploy validation step to the pipeline.",
    "lesson1": "Small, reversible changes with a documented rollback are easier to diagnose.",
    "lesson2": "Alerts should link to the runbook and to the most recent deployment.",
    "verify": "The fix was verified by watching the rollout complete and the alert resolve.",
}


def long_postmortem(s: Scenario) -> str:
    """Aurora-style postmortem (postmortem_action.DEFAULT_POSTMORTEM_INSTRUCTIONS sections),
    templated from scenario fields plus the same boilerplate for every scenario."""
    d, b = s.data, _PM_BOILERPLATE
    alert = " ".join(str(d.get("alert", "")).split())
    change = " ".join(str(d.get("change", "")).split())
    root = " ".join(str(d.get("root_cause", "")).split())
    ident, fixes, wrong = (_as_list(d.get(k)) for k in ("must_identify", "acceptable_fixes", "wrong_answers"))
    bullets = lambda xs: "\n".join(f"- {x}" for x in xs)  # noqa: E731
    return f"""## Summary
{s.title}. The alert "{alert}" fired after the change `{change}`. {b['impact']} The incident was resolved once the fix below was applied.

## Timeline
- **14:02 UTC** - The change was applied: `{change}`.
- **14:09 UTC** - The alert fired: {alert}
- **14:11 UTC** - {b['paged']}
- **14:15 UTC** - {b['describe']} {b['logs']}
- **14:24 UTC** - The root cause was identified. {b['slack']}
- **14:41 UTC** - The fix was applied. {b['verify']}

## Root Cause
{root}

Findings confirmed during the investigation:
{bullets(ident)}

## Impact
{b['impact']} {b['sla']} The alert that surfaced the problem was: {alert}

## Contributing Factors
{b['pressure']} {b['alerts']} {b['docs']}

Hypotheses considered and ruled out:
{bullets(wrong)}

## Resolution
Accepted fixes:
{bullets(fixes)}

## Action Items
- [ ] {b['ai1']}
- [ ] {b['ai2']}
- [ ] Add a guardrail for this change: `{change}`
- [ ] Link the runbook for "{s.title}" from the alert.

## Lessons Learned
{b['lesson1']} {b['lesson2']} In this case: {root}
"""


def robust_bank(scenarios: Sequence[Scenario], *, long_pm: bool = False, distractors: bool = False,
                skip: Optional[str] = None) -> List[Dict]:
    bank = memory_bank(scenarios, skip=skip)
    if long_pm:
        by_id = {s.id: s for s in scenarios}
        for e in bank:
            if e["category"] == "postmortem":
                e["content"] = long_postmortem(by_id[e["id"][3:]])
    if distractors:
        bank = bank + distractor_entries()
    return bank


# --------------------------------------------------------------------------- features
@dataclass
class Sample:
    sid: str
    full: List[Tuple[bool, Dict[str, float]]]  # top-3 incident hits on the full bank: (is own postmortem, features)
    loo: List[Dict[str, float]]                # top-3 incident hits with the scenario's own memory removed


class _FeatureIndex:
    """Wraps a MemoryIndex to compute hit features (uses its private postings for exact matched terms)."""

    def __init__(self, index):
        self.ix = index
        self._ent = {str(e.get("id") or ""): i for i, e in enumerate(index.entries)}
        self._chunk_of = {(o, c.offset): i for i, (o, c) in enumerate(zip(index._chunk_owner, index._chunks))}
        self._posting_sets: Dict[str, set] = {}

    def _chunks_with(self, term: str) -> set:
        if term not in self._posting_sets:
            self._posting_sets[term] = {c for c, _ in self.ix._postings.get(term, ())}
        return self._posting_sets[term]

    def features(self, terms: Sequence[str]) -> List[Tuple[Any, Dict[str, float]]]:
        """[(hit, features)] for the top-3 incident hits of check_change's query."""
        from services.memory import change_check as cc

        ix = self.ix
        vocab = [t for t in dict.fromkeys(terms) if ix.idf(t) > 0]
        total_idf = sum(ix.idf(t) for t in vocab) or 1.0
        hits = ix.search(terms, categories=cc.INCIDENT_CATEGORIES, k=3)
        overall = ix.search(terms, k=7)
        out = []
        for h in hits:
            cid = self._chunk_of.get((self._ent[h.entry_id], h.offset))
            matched = [t for t in vocab if cid in self._chunks_with(t)] if cid is not None else list(h.matched_terms)
            others = [o.score for o in overall if o.entry_id != h.entry_id][:5]
            base = sum(others) / len(others) if others else 0.0
            out.append((h, {
                "score": h.score,
                "strong": float(sum(ix.idf(t) >= STRONG_IDF for t in matched)),
                "coverage": sum(ix.idf(t) for t in matched) / total_idf,
                "margin": h.score / base if base > 0 else 1e9,
            }))
        return out


def shipped_rule(f: Dict[str, float]) -> bool:
    """change_check's rule, on this module's exact features."""
    from services.memory import change_check as cc

    return f["strong"] >= cc.MIN_STRONG_TERMS and (
        f["coverage"] >= cc.MIN_COVERAGE or f["score"] >= cc.MATCH_THRESHOLD
    )


def evaluate_rule(samples: Sequence["Sample"], rule) -> Dict[str, float]:
    n = len(samples)
    hit = sum(any(right for right, f in s.full if rule(f)) for s in samples)
    matched = sum(any(rule(f) for _, f in s.full) for s in samples)
    fp = sum(any(rule(f) for f in s.loo) for s in samples)
    return {"n": n, "match_rate": matched / n if n else 0.0, "hit@3": hit / n if n else 0.0,
            "fp_rate": fp / n if n else 0.0}


def fires(f: Dict[str, float], crit: str, p: float) -> bool:
    if crit == "absolute":
        return f["score"] >= p and f["strong"] >= 2
    if crit == "coverage":
        return f["coverage"] >= p
    if crit == "margin":
        return f["margin"] >= p
    if crit == "strong-count":
        return f["strong"] >= p
    raise ValueError(crit)


def evaluate(samples: Sequence[Sample], crit: str, p: float) -> Dict[str, float]:
    n = len(samples)
    hit = matched = matched_right = fp = 0
    for s in samples:
        rep = [right for right, f in s.full if fires(f, crit, p)]
        matched += bool(rep)
        hit += any(rep)
        matched_right += bool(rep) and any(rep)
        fp += any(fires(f, crit, p) for f in s.loo)
    return {"n": n, "match_rate": matched / n if n else 0.0, "hit@3": hit / n if n else 0.0,
            "fp_rate": fp / n if n else 0.0}


def calibrate(samples: Sequence[Sample], crit: str) -> float:
    """Maximize hit@3 - fp_rate; tie -> higher hit@3; tie -> stricter (larger) parameter."""
    def key(p):
        m = evaluate(samples, crit, p)
        return (round(m["hit@3"] - m["fp_rate"], 9), m["hit@3"], p)
    return max(GRIDS[crit], key=key)


class RobustnessData:
    """Indexes and features for one bank condition."""

    def __init__(self, scenarios: Sequence[Scenario], long_pm: bool, distractors: bool):
        from services.memory import change_check as cc
        from services.memory.retrieval import MemoryIndex

        self.scenarios = list(scenarios)
        full = _FeatureIndex(MemoryIndex(robust_bank(scenarios, long_pm=long_pm, distractors=distractors)))
        loo = {s.id: _FeatureIndex(MemoryIndex(robust_bank(scenarios, long_pm=long_pm, distractors=distractors,
                                                           skip=s.id))) for s in scenarios}
        self.n_entries = len(full.ix)
        self.samples: Dict[str, List[Sample]] = {}
        for kind in ("change", "break.sh"):
            rows = []
            for s in scenarios:
                kw = Replay.inputs(None, s, kind)  # type: ignore[arg-type]
                if kw is None:
                    continue
                terms = cc.extract_change_terms(**kw)
                rows.append(Sample(
                    s.id,
                    [(h.entry_id == f"pm-{s.id}", f) for h, f in full.features(terms)],
                    [f for _, f in loo[s.id].features(terms)],
                ))
            self.samples[kind] = rows


def robustness(scenarios: Optional[Sequence[Scenario]] = None) -> Dict[str, Any]:
    """Everything the ``--robustness`` report prints.

    Returns {"data": {cond: RobustnessData}, "A": {...}, "B": {...}, "drift": {...}}:
    * param[kind][crit]: parameter calibrated on all of condition A
    * metrics[cond][kind][crit]: metrics in each condition at that parameter (no re-tuning)
    * retuned[cond][kind][crit]: (param, metrics) calibrated in that condition (oracle)
    * split[kind][crit]: {"1-10->11-21": {...}, "11-21->1-10": {...}} each with param, heldout metrics,
      and the A-calibrated parameter's metrics on the same held-out half (in-sample for it)
    """
    scenarios = list(scenarios or load_scenarios())
    data = {name: RobustnessData(scenarios, *flags) for name, flags in CONDITIONS.items()}
    kinds = ("change", "break.sh")
    base = data["A baseline"]
    param = {k: {c: calibrate(base.samples[k], c) for c in CRITERIA} for k in kinds}
    metrics = {name: {k: {c: evaluate(d.samples[k], c, param[k][c]) for c in CRITERIA} for k in kinds}
               for name, d in data.items()}
    retuned = {}
    for name, d in data.items():
        retuned[name] = {}
        for k in kinds:
            retuned[name][k] = {}
            for c in CRITERIA:
                p = calibrate(d.samples[k], c)
                retuned[name][k][c] = (p, evaluate(d.samples[k], c, p))
    split: Dict[str, Dict] = {}
    for k in kinds:
        split[k] = {}
        for c in CRITERIA:
            split[k][c] = {}
            first = [s for s in base.samples[k] if int(s.sid[:2]) <= 10]
            second = [s for s in base.samples[k] if int(s.sid[:2]) > 10]
            for label, cal, ev in (("1-10 -> 11-21", first, second), ("11-21 -> 1-10", second, first)):
                p = calibrate(cal, c)
                split[k][c][label] = {"param": p, "heldout": evaluate(ev, c, p),
                                      "A_param_on_heldout": evaluate(ev, c, param[k][c])}
    return {"data": data, "param": param, "metrics": metrics, "retuned": retuned, "split": split}


def quality_with_distractors(scenarios: Optional[Sequence[Scenario]] = None, distractors: bool = True) -> Dict:
    """Like ``quality`` but the runbook pool also holds the distractor docs (gold ranks among all)."""
    from services.memory.retrieval import MemoryIndex

    scenarios = scenarios or load_scenarios()
    docs = runbook_entries(scenarios) + (distractor_entries() if distractors else [])
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
            acc[METHODS[2]].update(metrics(gold, [h.entry_id for h in index.search(q, k=len(docs))]))
        results[qset] = {m: {k: v / len(qs) for k, v in c.items()} for m, c in acc.items()}
    return results


def robustness_markdown(res: Dict[str, Any], scenarios: Optional[Sequence[Scenario]] = None) -> str:
    scenarios = list(scenarios or load_scenarios())
    f = lambda p: f"{p:g}"  # noqa: E731
    out = ["### Retrieval quality: runbooks alone (A) vs plus distractors (C)\n"]
    qa, qc = quality_with_distractors(scenarios, False), quality_with_distractors(scenarios, True)
    out += ["| query set | method | A r@1 | A r@5 | A MRR | C r@1 | C r@5 | C MRR |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for qset in qa:
        for m in METHODS:
            a, c = qa[qset][m], qc[qset][m]
            out.append(f"| {qset} | {m} | {a['r@1']:.2f} | {a['r@5']:.2f} | {a['mrr']:.2f} | "
                       f"{c['r@1']:.2f} | {c['r@5']:.2f} | {c['mrr']:.2f} |")
    sizes = ", ".join(f"{n}: {d.n_entries} entries" for n, d in res["data"].items())
    out.append(f"\nbank sizes: {sizes}")
    from services.memory import change_check as cc
    out.append(
        f"\n### Shipped rule across conditions (>= {cc.MIN_STRONG_TERMS} distinctive terms and "
        f"coverage >= {cc.MIN_COVERAGE:g} or score >= {cc.MATCH_THRESHOLD:g}; no re-tuning)\n"
    )
    out += ["| condition | input | match_rate | hit@3 | fp_rate (leave-one-out) |", "|---|---|---:|---:|---:|"]
    for cond, data in res["data"].items():
        for kind in ("change", "break.sh"):
            m = evaluate_rule(data.samples[kind], shipped_rule)
            out.append(f"| {cond} | {kind} | {m['match_rate']:.2f} | {m['hit@3']:.2f} | {m['fp_rate']:.2f} |")
    for kind in ("change", "break.sh"):
        n = len(res["data"]["A baseline"].samples[kind])
        out.append(f"\n### check_change abstention criteria, input = {kind} (n={n})\n")
        out.append("Parameter calibrated on condition A (maximize hit@3 - fp_rate); C/D/E use it unchanged. "
                   "`retuned` = oracle re-calibration inside that condition.\n")
        out += ["| condition | criterion | param | match_rate | hit@3 | fp_rate | hit@3-fp | retuned param | retuned hit@3 | retuned fp |",
                "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for cond in CONDITIONS:
            for c in CRITERIA:
                m = res["metrics"][cond][kind][c]
                rp, rm = res["retuned"][cond][kind][c]
                out.append(f"| {cond} | {c} | {f(res['param'][kind][c])} | {m['match_rate']:.2f} | {m['hit@3']:.2f} | "
                           f"{m['fp_rate']:.2f} | {m['hit@3'] - m['fp_rate']:+.2f} | {f(rp)} | {rm['hit@3']:.2f} | {rm['fp_rate']:.2f} |")
        out.append(f"\n#### B. split-half (baseline bank), input = {kind}\n")
        out += ["| criterion | calibrate -> evaluate | chosen param | held-out match_rate | held-out hit@3 | held-out fp_rate | A-param hit@3 | A-param fp |",
                "|---|---|---:|---:|---:|---:|---:|---:|"]
        for c in CRITERIA:
            for label, r in res["split"][kind][c].items():
                h, a = r["heldout"], r["A_param_on_heldout"]
                out.append(f"| {c} | {label} | {f(r['param'])} | {h['match_rate']:.2f} | {h['hit@3']:.2f} | "
                           f"{h['fp_rate']:.2f} | {a['hit@3']:.2f} | {a['fp_rate']:.2f} |")
    return "\n".join(out)
