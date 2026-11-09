"""Production Recall: content-level retrieval over an org's memory bank.

Memory is an agent-maintained notebook (the ``artifacts`` table). The agent
navigates it by title and description: the prompt index, the prefetch
selector and ``search_runbooks`` all match on those two fields, and
``grep_memories`` returns the ten newest regex hits. None of them rank an
entry by what its body says, and none can answer "what does this org's memory
say about *this change*?" for a caller that is not an agent mid-turn (the PR
reviewer's prompt, an MCP client, ``check_change``).

This module is a *derived* index over that notebook, not a second store:

* The artifacts table stays the only source of truth. The index is rebuilt
  from it whenever the org's bank changes and is never written to directly.
* Each entry is cut into heading-aware chunks (about ``CHUNK_WORDS`` words,
  ``CHUNK_OVERLAP`` overlap) and ranked with BM25. An entry scores as its best
  chunk, and each hit carries the chunk's character offset, which
  ``read_memory(offset=...)`` accepts directly.
* Lexical, not dense. Incident memory is full of exact tokens (``BATCH_SIZE``,
  ``6379``, ``nginx:1.27-alpne``). On Aurora's own published runbooks BM25
  over chunked bodies beat both the title/description ranking and a small
  embedding model (see ``scripts/eval_memory_retrieval.py``).
* Org-scoped and in-process. Rows are read under RLS like every other memory
  query, the cache is keyed by org id, and nothing leaves the process: no
  vector store, no embedding provider, no new data flow.
* Returned chunk text passes through the Gitleaks-derived ``redact()`` used
  for tool output, since memory write paths store whatever they are given.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

CHUNK_WORDS = 220
CHUNK_OVERLAP = 40
# What a hit returns of its chunk. Enough to judge relevance; the agent reads
# the full entry from ``offset`` when it wants more.
MAX_HIT_CHARS = 1200
_BM25_K1 = 1.5
_BM25_B = 0.75
# Orgs whose index stays warm in one process.
_CACHE_MAX_ORGS = 32
_MAX_TOKEN_CHARS = 80

# Kept small on purpose: BM25's IDF already discounts common words, so this
# only drops function words that would otherwise pad every query.
STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "but", "if", "then", "else", "to", "of", "in",
    "on", "for", "is", "it", "its", "be", "as", "at", "by", "with", "from",
    "this", "that", "these", "those", "our", "we", "my", "you", "your", "are",
    "was", "were", "been", "has", "have", "had", "do", "does", "did", "not",
    "no", "so", "can", "will", "would", "should", "into", "than", "there",
    "which", "who", "what", "when", "where", "how", "all", "any", "each",
})

# An identifier: alphanumerics joined by the separators config keys, paths,
# image refs and hostnames use (``BATCH_SIZE``, ``api-secrets/db-password``,
# ``nginx:1.27-alpne``, ``redis.shop.svc``).
_IDENT_RE = re.compile(r"[A-Za-z0-9]+(?:[_.\-/:][A-Za-z0-9]+)*")
_SEP_RE = re.compile(r"[_.\-/:]+")
# ``KubePodCrashLooping`` -> Kube Pod Crash Looping; ``HTTPServer`` -> HTTP Server.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_HEADING_START_RE = re.compile(r"(?m)^(?=#{1,6}[ \t])")
_WORD_SPAN_RE = re.compile(r"\S+")


def tokenize(text: str) -> List[str]:
    """Lowercase search terms for *text*, in order, repeats kept.

    A compound identifier yields itself *and* its parts, so ``BATCH_SIZE``
    matches a postmortem that writes ``batch_size`` and one that says "the
    batch size", and ``KubePodCrashLooping`` matches "crash looping".
    """
    out: List[str] = []
    for match in _IDENT_RE.finditer(text or ""):
        raw = match.group(0)[:_MAX_TOKEN_CHARS]
        words: List[str] = []
        for part in _SEP_RE.split(raw):
            words.extend(_CAMEL_RE.sub(" ", part).split())
        lowered = [w.lower() for w in words]
        if len(lowered) > 1:
            out.append(raw.lower())
        for w in lowered:
            if len(w) >= 2 and w not in STOPWORDS:
                out.append(w)
                out.extend(_singulars(w))
    return out


def _singulars(word: str) -> List[str]:
    """Extra singular forms, added alongside the word (never replacing it).

    ``batches`` also yields ``batch``; ``replicas`` yields ``replica``. Adding
    rather than replacing keeps exact matches exact, and the same rule runs on
    both the bank and the query, so a wrong guess costs a little recall noise
    at most. Digits and short words are left alone.
    """
    if len(word) < 4 or not word.isalpha():
        return []
    if word.endswith("ies") and len(word) > 4:
        return [word[:-3] + "y"]
    if word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return [word[:-2]]
    if word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return [word[:-1]]
    return []


@dataclass(frozen=True)
class Chunk:
    """One window of an entry's content."""

    offset: int  # character offset into the entry's content
    end: int
    heading: str


def chunk_content(content: str) -> List[Chunk]:
    """Heading-aware windows over *content* with their character spans."""
    content = content or ""
    if not content.strip():
        return [Chunk(0, 0, "")]
    starts = [m.start() for m in _HEADING_START_RE.finditer(content)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    bounds = list(zip(starts, starts[1:] + [len(content)]))

    chunks: List[Chunk] = []
    step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
    for sec_start, sec_end in bounds:
        section = content[sec_start:sec_end]
        first_line = section.split("\n", 1)[0].strip()
        heading = first_line.lstrip("#").strip() if first_line.startswith("#") else ""
        spans = [(sec_start + m.start(), sec_start + m.end()) for m in _WORD_SPAN_RE.finditer(section)]
        if not spans:
            continue
        for i in range(0, len(spans), step):
            window = spans[i:i + CHUNK_WORDS]
            chunks.append(Chunk(window[0][0], window[-1][1], heading))
            if i + CHUNK_WORDS >= len(spans):
                break
    return chunks or [Chunk(0, len(content), "")]


@dataclass
class Hit:
    """One ranked memory entry and the chunk that matched best."""

    entry_id: str
    category: str
    title: str
    description: str
    score: float
    offset: int
    heading: str
    chunk: str
    matched_terms: List[str] = field(default_factory=list)
    # Every query term the chunk matched, strongest first. ``matched_terms`` is
    # the display cut; abstention (change_check) needs all of them.
    all_terms: List[str] = field(default_factory=list, repr=False)

    def to_dict(self) -> Dict:
        out = asdict(self)
        out.pop("all_terms", None)
        out["score"] = round(self.score, 3)
        return out


class MemoryIndex:
    """BM25 over the chunks of one org's memory entries."""

    def __init__(self, entries: Sequence[Dict]):
        self.entries: List[Dict] = list(entries)
        self._chunk_owner: List[int] = []
        self._chunks: List[Chunk] = []
        self._chunk_len: List[int] = []
        self._postings: Dict[str, List[Tuple[int, int]]] = {}
        for entry_idx, entry in enumerate(self.entries):
            content = entry.get("content") or ""
            # The title and description belong to every chunk: they are what
            # the entry is *about*, even when a chunk never repeats them.
            header = tokenize(f"{entry.get('title') or ''}\n{entry.get('description') or ''}")
            for chunk in chunk_content(content):
                counts = Counter(header)
                counts.update(tokenize(content[chunk.offset:chunk.end]))
                chunk_id = len(self._chunks)
                self._chunks.append(chunk)
                self._chunk_owner.append(entry_idx)
                self._chunk_len.append(sum(counts.values()))
                for term, tf in counts.items():
                    self._postings.setdefault(term, []).append((chunk_id, tf))
        n = len(self._chunks)
        self._avg_len = (sum(self._chunk_len) / n) if n else 0.0
        self._idf = {
            term: math.log(1 + (n - len(posts) + 0.5) / (len(posts) + 0.5))
            for term, posts in self._postings.items()
        }

    def __len__(self) -> int:
        return len(self.entries)

    def idf(self, term: str) -> float:
        return self._idf.get(term, 0.0)

    def query_idf(self, terms: Iterable[str]) -> float:
        """Summed IDF of the distinct *terms* this bank knows: a query's total weight."""
        return sum(self._idf[t] for t in dict.fromkeys(terms) if t in self._idf)

    def search(
        self,
        query: str | Sequence[str],
        *,
        categories: Optional[Iterable[str]] = None,
        k: int = 5,
        redact_chunks: bool = True,
    ) -> List[Hit]:
        """Top *k* entries for *query* (text or pre-tokenized terms)."""
        terms = tokenize(query) if isinstance(query, str) else list(query)
        # Repeating a term in a long query (a diff) must not multiply its weight.
        terms = list(dict.fromkeys(t for t in terms if t in self._postings))
        if not terms or not self._chunks:
            return []
        allowed = set(categories) if categories else None

        chunk_scores: Dict[int, float] = {}
        chunk_terms: Dict[int, List[Tuple[float, str]]] = {}
        for term in terms:
            idf = self._idf[term]
            for chunk_id, tf in self._postings[term]:
                if allowed is not None and self.entries[self._chunk_owner[chunk_id]].get("category") not in allowed:
                    continue
                norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * self._chunk_len[chunk_id] / self._avg_len)
                part = idf * tf * (_BM25_K1 + 1) / (tf + norm)
                chunk_scores[chunk_id] = chunk_scores.get(chunk_id, 0.0) + part
                chunk_terms.setdefault(chunk_id, []).append((part, term))

        best: Dict[int, Tuple[float, int]] = {}
        for chunk_id, score in chunk_scores.items():
            owner = self._chunk_owner[chunk_id]
            if owner not in best or score > best[owner][0]:
                best[owner] = (score, chunk_id)

        ranked = sorted(best.items(), key=lambda kv: kv[1][0], reverse=True)[:max(1, k)]
        hits: List[Hit] = []
        for entry_idx, (score, chunk_id) in ranked:
            entry = self.entries[entry_idx]
            chunk = self._chunks[chunk_id]
            text = (entry.get("content") or "")[chunk.offset:chunk.end][:MAX_HIT_CHARS]
            if redact_chunks:
                text = _redact(text)
            ordered = [t for _, t in sorted(chunk_terms[chunk_id], reverse=True)]
            hits.append(Hit(
                entry_id=str(entry.get("id") or ""),
                category=entry.get("category") or "",
                title=entry.get("title") or "",
                description=entry.get("description") or "",
                score=score,
                offset=chunk.offset,
                heading=chunk.heading,
                chunk=text,
                matched_terms=ordered[:8],
                all_terms=ordered,
            ))
        return hits


def _redact(text: str) -> str:
    try:
        from utils.security.output_redaction import redact
        return redact(text)[0]
    except Exception:  # redact() is fail-open itself; this covers the import
        logger.warning("[Recall] redaction unavailable; withholding chunk text")
        return ""


# ---------------------------------------------------------------------------
# Per-org cache
# ---------------------------------------------------------------------------

Fingerprint = Tuple

_cache: "OrderedDict[str, Tuple[Fingerprint, MemoryIndex]]" = OrderedDict()
_cache_lock = threading.Lock()


def get_org_index(
    org_id: str,
    fingerprint: Fingerprint,
    load_entries: Callable[[], List[Dict]],
) -> MemoryIndex:
    """The org's index, rebuilt only when its bank's *fingerprint* changes.

    Keyed by org id alone, so one org's index can never answer another's
    query; the fingerprint (entry count, latest ``updated_at``) is only the
    staleness check.
    """
    with _cache_lock:
        cached = _cache.get(org_id)
        if cached and cached[0] == fingerprint:
            _cache.move_to_end(org_id)
            return cached[1]
    index = MemoryIndex(load_entries())
    with _cache_lock:
        _cache[org_id] = (fingerprint, index)
        _cache.move_to_end(org_id)
        while len(_cache) > _CACHE_MAX_ORGS:
            _cache.popitem(last=False)
    return index


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def search_org_memory(
    cursor,
    org_id: str,
    query: str | Sequence[str],
    *,
    categories: Optional[Iterable[str]] = None,
    k: int = 5,
) -> List[Hit]:
    """Search *org_id*'s memory on a cursor already pinned to it under RLS.

    For request handlers: pass the org RBAC authorized and pin RLS to it first
    (see the module docstring of ``services.memory.queries``).
    """
    from services.memory.queries import fetch_memory_corpus, fetch_memory_fingerprint

    index = get_org_index(
        org_id,
        fetch_memory_fingerprint(cursor, org_id),
        lambda: fetch_memory_corpus(cursor, org_id),
    )
    return index.search(query, categories=categories, k=k)


def search_memory_for_user(
    user_id: str,
    query: str | Sequence[str],
    *,
    categories: Optional[Iterable[str]] = None,
    k: int = 5,
) -> List[Hit]:
    """Background flavour: resolve the user's org and pin RLS, then search."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Recall]")
                if not org_id:
                    return []
                return search_org_memory(cursor, org_id, query, categories=categories, k=k)
    except Exception:
        logger.warning("[Recall] search failed for user %s", user_id, exc_info=True)
        return []
