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

from services.memory.retrieval import Hit, MemoryIndex, get_org_index, tokenize

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

_DIFF_FILE_RE = re.compile(r"^(?:\+\+\+ b/|--- a/|diff --git a/\S+ b/)(\S+)", re.MULTILINE)


def extract_change_terms(
    diff: str = "",
    files: Optional[Sequence[str]] = None,
    description: str = "",
) -> List[str]:
    """Search terms for a change: description, paths, then changed lines.

    Added/removed lines count, plus unchanged lines within ``CONTEXT_LINES``
    of one (they carry the key a changed value belongs to). Order is kept
    (earlier sources first) and repeats dropped,
    so a huge diff cannot crowd out the description and file names.
    """
    diff = (diff or "")[:MAX_DIFF_CHARS]
    paths = list(files or [])[:MAX_FILES]
    paths += _DIFF_FILE_RE.findall(diff)

    lines = diff.splitlines()
    changed_idx = [
        i for i, line in enumerate(lines)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    changed = [lines[i][1:] for i in changed_idx]
    # The context right next to a change names what changed: in
    # `- name: BATCH_SIZE / -  value: "500" / +  value: "2500"` the key and the
    # limit beside it are context lines. They rank after the changed lines.
    near = sorted({
        j for i in changed_idx
        for j in range(i - CONTEXT_LINES, i + CONTEXT_LINES + 1)
        if 0 <= j < len(lines) and j not in changed_idx
        and lines[j].startswith(" ")
    })
    context = [lines[j][1:] for j in near]
    # A bare change script or command (no diff markers) is all "changed".
    if diff and not changed_idx and not _DIFF_FILE_RE.search(diff):
        changed = lines

    terms: List[str] = []
    for source in (description or "", "\n".join(paths), "\n".join(changed), "\n".join(context)):
        terms.extend(tokenize(source))
    return list(dict.fromkeys(terms))[:MAX_QUERY_TERMS]


def _strong_terms(index: MemoryIndex, hit: Hit) -> List[str]:
    return [t for t in (hit.all_terms or hit.matched_terms) if index.idf(t) >= STRONG_TERM_IDF]


def coverage(index: MemoryIndex, hit: Hit, query_idf: float) -> float:
    """Share of the query's IDF weight that *hit*'s chunk matched."""
    if query_idf <= 0:
        return 0.0
    return sum(index.idf(t) for t in (hit.all_terms or hit.matched_terms)) / query_idf


def _is_match(index: MemoryIndex, hit: Hit, query_idf: float) -> bool:
    if len(_strong_terms(index, hit)) < MIN_STRONG_TERMS:
        return False
    return coverage(index, hit, query_idf) >= MIN_COVERAGE or hit.score >= MATCH_THRESHOLD


def check_change(
    index: MemoryIndex,
    *,
    diff: str = "",
    files: Optional[Sequence[str]] = None,
    description: str = "",
    k: int = 3,
) -> Dict:
    """Run the check against one org's *index*. See the module docstring."""
    terms = extract_change_terms(diff, files, description)
    if not terms:
        return _result("NO_MATCH", terms, [], [], [])

    def group(categories: Iterable[str]) -> List[Hit]:
        return index.search(terms, categories=categories, k=k)

    query_idf = index.query_idf(terms)
    incidents = [h for h in group(INCIDENT_CATEGORIES) if _is_match(index, h, query_idf)]
    guardrails = [h for h in group(GUARDRAIL_CATEGORIES) if _is_match(index, h, query_idf)]
    runbooks = group(RUNBOOK_CATEGORIES)
    # Below the bar nothing is reported as related: a near-miss rendered as a
    # "past incident" in a PR review costs more attention than it saves.
    status = "MATCHES_PAST_INCIDENT" if (incidents or guardrails) else "NO_MATCH"
    return _result(status, terms, incidents, guardrails, runbooks)


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


def check_change_for_org(cursor, org_id: str, **kwargs) -> Dict:
    """Request flavour: *cursor* is already pinned to *org_id* under RLS."""
    from services.memory.queries import fetch_memory_corpus, fetch_memory_fingerprint

    index = get_org_index(
        org_id,
        fetch_memory_fingerprint(cursor, org_id),
        lambda: fetch_memory_corpus(cursor, org_id),
    )
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
                return check_change_for_org(cursor, org_id, **kwargs)
    except Exception:
        logging.getLogger(__name__).warning(
            "[Recall] check_change failed for user %s", user_id, exc_info=True,
        )
        return None
