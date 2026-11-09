"""``check_change``: what does this org's memory already know about a change?

Given a diff, the files it touches and a description, return the past
incidents, guardrails and runbooks in org memory that share its specific terms
(config keys, file paths, service names, values), each with the chunk that
matched. Deterministic and LLM-free, so it is cheap enough to run on every PR
review and on every edit a coding agent makes over MCP.

``status`` is ``MATCHES_PAST_INCIDENT`` only when a postmortem, Incident Index
line or guardrail clears the abstention rule below (distinctive terms plus
query coverage, with an absolute-score backstop). Runbooks are returned as context
but never set the status: a runbook for a service describes how to operate it,
not that a change to it broke production.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

from services.memory.retrieval import Hit, MemoryIndex, org_index_for_cursor, tokenize

INCIDENT_CATEGORIES = ("postmortem", "artifact")
GUARDRAIL_CATEGORIES = ("learned",)
RUNBOOK_CATEGORIES = ("runbook", "context")

# A hit matches when it shares at least MIN_STRONG_TERMS distinctive terms
# with the change AND either covers MIN_COVERAGE of the query's total IDF
# weight, or scores at least MATCH_THRESHOLD outright.
#
# Coverage is the primary test because it is scale-free: an absolute BM25
# cut-off shifts with bank size, entry length and query length. Tuned on the
# 21 aurora-lab scenarios, a fixed score of 15 let leave-one-out false matches
# climb from 0.33 to 0.71 once realistic postmortems and ~30 unrelated docs
# joined the bank; this rule moves from 0.05 to 0.43 under the same changes,
# and its tuned value barely moves across held-out halves (0.375 vs 0.4).
# Coverage alone under-fires on long inputs (a whole change script spreads
# its weight over many terms), so a high absolute score is the backstop.
# Evidence: `scripts/eval_memory_retrieval.py --robustness`.
MIN_COVERAGE = 0.35
MATCH_THRESHOLD = 40.0
MIN_STRONG_TERMS = 2
# A term is distinctive when it is rarer than this in the org's bank (IDF).
STRONG_TERM_IDF = 1.5

MAX_DIFF_CHARS = 200_000
MAX_FILES = 500
MAX_QUERY_TERMS = 400
# Unchanged diff lines this close to a change count as part of it.
CONTEXT_LINES = 2



_HUNK_RE = re.compile(r"^@@ -\d", re.MULTILINE)
_FILE_HEADER_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)|^\+\+\+ b/(\S+)", re.MULTILINE)
# Files scored on their own beside the whole change; the rest only count there.
MAX_FILE_QUERIES = 20


def _looks_like_diff(text: str) -> bool:
    """A unified diff has hunk or file headers; a command or script does not.

    Lines that merely start with '-' or '+' (YAML list items, CLI flags) are
    not enough: treating such text as a diff would drop every other line.
    """
    return bool(_HUNK_RE.search(text) or _FILE_HEADER_RE.search(text))


def _diff_lines(diff: str) -> tuple:
    """(changed lines, nearby context lines) of a unified diff."""
    lines = diff.splitlines()
    changed_idx = [
        i for i, line in enumerate(lines)
        if line.startswith(("+", "-")) and not line.startswith(("+++ ", "--- "))
    ]
    changed_set = set(changed_idx)
    # The context right next to a change names what changed: in
    # `- name: BATCH_SIZE / -  value: "500" / +  value: "2500"` the key and the
    # limit beside it are context lines. They rank after the changed lines.
    near = sorted({
        j for i in changed_idx
        for j in range(i - CONTEXT_LINES, i + CONTEXT_LINES + 1)
        if 0 <= j < len(lines) and j not in changed_set and lines[j].startswith(" ")
    })
    return [lines[i][1:] for i in changed_idx], [lines[j][1:] for j in near]


def _terms(description: str, paths: Sequence[str], body: str) -> List[str]:
    if body and _looks_like_diff(body):
        changed, context = _diff_lines(body)
    else:
        changed, context = (body.splitlines() if body else []), []
    terms: List[str] = []
    for source in (description or "", "\n".join(paths), "\n".join(changed), "\n".join(context)):
        terms.extend(tokenize(source))
    return list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]


def split_diff(diff: str) -> List[tuple]:
    """[(path, section)] per file of a multi-file unified diff (empty if none)."""
    starts = [(m.start(), m.group(2) or m.group(3)) for m in _FILE_HEADER_RE.finditer(diff or "")]
    # A `diff --git` header is followed by its own `+++ b/` line: keep the first.
    sections, last_path = [], None
    for i, (pos, path) in enumerate(starts):
        if path == last_path and sections:
            continue
        end = next((p for p, q in starts[i + 1:] if q != path), len(diff))
        sections.append((path, diff[pos:end]))
        last_path = path
    return sections


def extract_change_terms(
    diff: str = "",
    files: Optional[Sequence[str]] = None,
    description: str = "",
) -> List[str]:
    """Search terms for the whole change: description, paths, changed lines.

    Added/removed lines count, plus unchanged lines within ``CONTEXT_LINES``
    of one (they carry the key a changed value belongs to). Input without
    hunk or file headers (a command, a script) counts as changed in full.
    Order is kept (earlier sources first) and repeats dropped, so a huge diff
    cannot crowd out the description and file names.
    """
    diff = (diff or "")[:MAX_DIFF_CHARS]
    paths = list(files or [])[:MAX_FILES]
    paths += [p for p, _ in split_diff(diff) if p not in paths]
    return _terms(description, paths, diff)


def _strong_terms(index: MemoryIndex, hit: Hit) -> List[str]:
    return [t for t in (hit.all_terms or hit.matched_terms) if index.idf(t) >= STRONG_TERM_IDF]


def coverage(index: MemoryIndex, hit: Hit, query_idf: float) -> float:
    """Share of the query's IDF weight that *hit*'s chunk matched."""
    if query_idf <= 0:
        return 0.0
    return sum(index.idf(t) for t in (hit.all_terms or hit.matched_terms)) / query_idf


def _is_match(
    index: MemoryIndex, hit: Hit, query_idf: float, backstop: bool = True,
    soft_terms: frozenset = frozenset(),
) -> bool:
    if len(_strong_terms(index, hit)) < MIN_STRONG_TERMS:
        return False
    # Words only the author's description contributes (a PR title such as
    # "Routine maintenance") count toward coverage when they match, and never
    # against it: the evidence is the change itself.
    matched = set(hit.all_terms or hit.matched_terms)
    denom = query_idf - sum(index.idf(t) for t in soft_terms if t not in matched)
    if coverage(index, hit, denom) >= MIN_COVERAGE:
        return True
    return backstop and hit.score >= MATCH_THRESHOLD


def _queries(diff: str, files: Optional[Sequence[str]], description: str) -> List[List[str]]:
    """The whole-change query, then one per file of a multi-file diff.

    Scoring each file on its own is what lets one relevant file in a 40-file
    PR match: in the whole-change query its terms are a sliver of the IDF
    weight, and may sit past the term cap entirely.
    """
    diff = (diff or "")[:MAX_DIFF_CHARS]
    queries = [extract_change_terms(diff, files, description)]
    sections = split_diff(diff)
    if len(sections) > 1:
        for path, section in sections[:MAX_FILE_QUERIES]:
            queries.append(_terms(description, [path], section))
    return [q for q in queries if q]


def _matches(
    index: MemoryIndex, queries: List[List[str]], categories, k: int,
    soft_terms: frozenset = frozenset(),
) -> List[Hit]:
    """Entries that clear the bar on any query, best score kept, best first.

    The absolute-score backstop is for one long input (a script, a big
    single-file diff). On the whole-change query of a multi-file diff it would
    fire on sheer volume -- generic code words across many files add up -- so
    there only coverage counts; each file still gets the full rule on its own.
    """
    best: Dict[str, Hit] = {}
    multi = len(queries) > 1
    for i, terms in enumerate(queries):
        query_idf = index.query_idf(terms)
        backstop = not (multi and i == 0)
        for hit in index.search(terms, categories=categories, k=k):
            if _is_match(index, hit, query_idf, backstop=backstop, soft_terms=soft_terms):
                prev = best.get(hit.entry_id)
                if prev is None or hit.score > prev.score:
                    best[hit.entry_id] = hit
    return sorted(best.values(), key=lambda h: h.score, reverse=True)[:k]


def check_change(
    index: MemoryIndex,
    *,
    diff: str = "",
    files: Optional[Sequence[str]] = None,
    description: str = "",
    k: int = 3,
) -> Dict:
    """Run the check against one org's *index*. See the module docstring."""
    queries = _queries(diff, files, description)
    if not queries:
        return _result("NO_MATCH", [], [], [], [])

    # Description-only terms: what the author wrote but the change does not
    # show. With no diff or files the description *is* the change, so all of
    # its terms count in full.
    change_terms = set(extract_change_terms(diff, files, ""))
    soft = frozenset(
        t for t in tokenize(description or "") if t not in change_terms
    ) if change_terms else frozenset()
    incidents = _matches(index, queries, INCIDENT_CATEGORIES, k, soft)
    guardrails = _matches(index, queries, GUARDRAIL_CATEGORIES, k, soft)
    # Runbooks are context, never evidence of a past failure, so they are not
    # gated; consumers must present them as such.
    runbooks = index.search(queries[0], categories=RUNBOOK_CATEGORIES, k=k)
    # Below the bar nothing is reported as related: a near-miss rendered as a
    # "past incident" in a PR review costs more attention than it saves.
    status = "MATCHES_PAST_INCIDENT" if (incidents or guardrails) else "NO_MATCH"
    return _result(status, queries[0], incidents, guardrails, runbooks)


def _result(status, terms, incidents, guardrails, runbooks) -> Dict:
    return {
        "status": status,
        "query_terms": terms[:40],
        "related_incidents": [h.to_dict() for h in incidents],
        "guardrails": [h.to_dict() for h in guardrails],
        "runbooks": [h.to_dict() for h in runbooks],
        "how_to_read_more": (
            "Each hit names its memory entry (category, title) and the character "
            "offset of the matched chunk; read_memory(category, title, offset=...) "
            "returns the full text from there."
        ),
    }


def check_change_for_org(cursor, org_id: str, *, user_id: Optional[str] = None, **kwargs) -> Dict:
    """Request flavour: *cursor* is already pinned to *org_id* under RLS.

    With *user_id*, a stale index is served and refreshed in the background.
    """
    index = org_index_for_cursor(cursor, org_id, user_id=user_id)
    return check_change(index, **kwargs)


def check_change_for_user(user_id: str, **kwargs) -> Optional[Dict]:
    """Background flavour (PR review). Returns None when memory is unreachable."""
    import logging

    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Recall:check_change]")
                if not org_id:
                    return None
                return check_change_for_org(cursor, org_id, user_id=user_id, **kwargs)
    except Exception:
        logging.getLogger(__name__).warning(
            "[Recall] check_change failed for user %s", user_id, exc_info=True,
        )
        return None
