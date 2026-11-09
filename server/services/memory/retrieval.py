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
import os
import re
import functools
import itertools
import threading
import time
from array import array
from collections import Counter, OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
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


@functools.lru_cache(maxsize=200_000)
def _word_terms(word: str) -> Tuple[str, ...]:
    """Terms of one whitespace-free word, cached: banks repeat words heavily.

    Identifiers never contain whitespace, so tokenizing word by word yields
    exactly what tokenizing the whole text would.
    """
    return tuple(tokenize(word))


def _sections(content: str):
    """(heading, [(start, end) word spans]) per heading-delimited section."""
    starts = [m.start() for m in _HEADING_START_RE.finditer(content)]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    for sec_start, sec_end in zip(starts, starts[1:] + [len(content)]):
        section = content[sec_start:sec_end]
        first_line = section.split("\n", 1)[0].strip()
        heading = first_line.lstrip("#").strip() if first_line.startswith("#") else ""
        spans = [(sec_start + m.start(), sec_start + m.end()) for m in _WORD_SPAN_RE.finditer(section)]
        if spans:
            yield heading, spans


def _windows(n_words: int):
    """(first, last_exclusive) word indexes of each chunk window in a section."""
    step = max(1, CHUNK_WORDS - CHUNK_OVERLAP)
    for i in range(0, n_words, step):
        yield i, min(i + CHUNK_WORDS, n_words)
        if i + CHUNK_WORDS >= n_words:
            break


def chunk_content(content: str) -> List[Chunk]:
    """Heading-aware windows over *content* with their character spans."""
    content = content or ""
    if not content.strip():
        return [Chunk(0, 0, "")]
    chunks = [
        Chunk(spans[a][0], spans[b - 1][1], heading)
        for heading, spans in _sections(content)
        for a, b in _windows(len(spans))
    ]
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
        # Compact columns, not per-chunk objects: a 500-entry bank is ~8.5k
        # chunks and ~1M postings, and Python tuples/Counters per posting cost
        # several times the text itself.
        self.entries: List[Dict] = list(entries)
        self._entry_category: List[str] = [e.get("category") or "" for e in self.entries]
        self._entry_id: List[str] = [str(e.get("id") or "") for e in self.entries]
        self._chunk_owner = array("I")
        self._chunk_start = array("I")
        self._chunk_end = array("I")
        self._chunk_len = array("I")
        self._chunk_heading = array("I")
        self._headings: List[str] = [""]
        heading_ids: Dict[str, int] = {"": 0}
        postings: Dict[str, Tuple[array, array]] = {}

        for entry_idx, entry in enumerate(self.entries):
            content = entry.get("content") or ""
            # The title and description belong to every chunk: they are what
            # the entry is *about*, even when a chunk never repeats them.
            header = tokenize(f"{entry.get('title') or ''}\n{entry.get('description') or ''}")
            sections = list(_sections(content)) if content.strip() else []
            if not sections:
                self._add_chunk(postings, entry_idx, 0, len(content), 0, Counter(header))
                continue
            for heading, spans in sections:
                hid = heading_ids.setdefault(heading, len(heading_ids))
                if hid == len(self._headings):
                    self._headings.append(heading)
                # Each word is tokenized once per section, then shared by
                # every overlapping window that contains it.
                word_terms = [_word_terms(content[a:b]) for a, b in spans]
                for first, last in _windows(len(spans)):
                    counts = Counter(header)
                    counts.update(itertools.chain.from_iterable(word_terms[first:last]))
                    self._add_chunk(postings, entry_idx, spans[first][0], spans[last - 1][1], hid, counts)

        self._postings = postings
        n = len(self._chunk_owner)
        self._avg_len = (sum(self._chunk_len) / n) if n else 0.0
        self._idf = {
            term: math.log(1 + (n - len(ids) + 0.5) / (len(ids) + 0.5))
            for term, (ids, _) in postings.items()
        }

    def _add_chunk(self, postings, entry_idx: int, start: int, end: int, heading_id: int, counts: Counter) -> None:
        chunk_id = len(self._chunk_owner)
        self._chunk_owner.append(entry_idx)
        self._chunk_start.append(start)
        self._chunk_end.append(end)
        self._chunk_heading.append(heading_id)
        self._chunk_len.append(sum(counts.values()))
        for term, tf in counts.items():
            ids_tfs = postings.get(term)
            if ids_tfs is None:
                ids_tfs = postings[term] = (array("I"), array("H"))
            ids_tfs[0].append(chunk_id)
            ids_tfs[1].append(min(tf, 65535))

    @property
    def n_chunks(self) -> int:
        return len(self._chunk_owner)

    def chunk(self, chunk_id: int) -> Chunk:
        return Chunk(self._chunk_start[chunk_id], self._chunk_end[chunk_id],
                     self._headings[self._chunk_heading[chunk_id]])

    def chunks_with(self, term: str):
        """Chunk ids containing *term* (empty if unknown)."""
        ids_tfs = self._postings.get(term)
        return ids_tfs[0] if ids_tfs else ()

    def approx_bytes(self) -> int:
        """Rough resident size: indexed text plus posting and chunk columns."""
        text = sum(len(e.get("content") or "") for e in self.entries)
        postings = sum(len(ids) * 6 + 120 for ids, _ in self._postings.values())
        return text + postings + self.n_chunks * 20 + len(self._idf) * 90

    def __len__(self) -> int:
        return len(self.entries)

    def idf(self, term: str) -> float:
        return self._idf.get(term, 0.0)

    @property
    def max_idf(self) -> float:
        """IDF of a term found in exactly one chunk: the rarest a term can be here."""
        n = self.n_chunks
        return math.log(1 + (n - 0.5) / 1.5) if n else 0.0

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
        entry_ids: Optional[Iterable[str]] = None,
    ) -> List[Hit]:
        """Top *k* entries for *query* (text or pre-tokenized terms).

        *entry_ids* restricts ranking to those entries (e.g. the rows a regex
        already matched), reusing this index instead of building another.
        """
        terms = tokenize(query) if isinstance(query, str) else list(query)
        # Repeating a term in a long query (a diff) must not multiply its weight.
        terms = list(dict.fromkeys(t for t in terms if t in self._postings))
        if not terms or not self.n_chunks:
            return []
        allowed = set(categories) if categories else None
        only_ids = {str(i) for i in entry_ids} if entry_ids is not None else None

        chunk_scores: Dict[int, float] = {}
        chunk_terms: Dict[int, List[Tuple[float, str]]] = {}
        cats, eids, owners, lens = self._entry_category, self._entry_id, self._chunk_owner, self._chunk_len
        for term in terms:
            idf = self._idf[term]
            ids, tfs = self._postings[term]
            for chunk_id, tf in zip(ids, tfs):
                owner = owners[chunk_id]
                if allowed is not None and cats[owner] not in allowed:
                    continue
                if only_ids is not None and eids[owner] not in only_ids:
                    continue
                norm = _BM25_K1 * (1 - _BM25_B + _BM25_B * lens[chunk_id] / self._avg_len)
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
            chunk = self.chunk(chunk_id)
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
#
# Memory changes constantly: the collector writes after most turns. Rebuilding
# on every change would put a full refetch + re-index on the chat, PR-review
# and MCP hot paths, so the cache is stale-while-revalidate:
#
# * fingerprint unchanged      -> serve the cached index.
# * fingerprint changed        -> serve the cached (stale) index now and rebuild
#                                 in the background, at most once per
#                                 ``REBUILD_MIN_INTERVAL`` seconds per org.
# * nothing cached yet         -> build in the background, one build per org,
#                                 and wait up to ``wait`` seconds for it. A
#                                 caller that cannot afford the build gets None
#                                 (routes answer "warming", prefetch skips) and
#                                 the next call finds the index ready.
#
# Staleness is bounded (a new memory is searchable within about a rebuild
# interval) and the rebuild cost no longer scales with write frequency. The
# cache is bounded by approximate size as well as by org count, so a few large
# banks cannot grow a worker without limit.

Fingerprint = Tuple

REBUILD_MIN_INTERVAL = 30.0
_CACHE_MAX_BYTES = int(float(os.getenv("RECALL_CACHE_MAX_MB", "256")) * 1024 * 1024)

_cache: "OrderedDict[str, Tuple[Fingerprint, MemoryIndex, float, int]]" = OrderedDict()
_cache_lock = threading.Lock()
_build_locks: Dict[str, threading.Lock] = {}
# org id -> the Future of its in-flight build (None while it is being submitted).
_pending: Dict[str, Optional[Future]] = {}
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="recall-index")

Loader = Callable[[], Tuple[Fingerprint, List[Dict]]]


def _store(org_id: str, fingerprint: Fingerprint, index: MemoryIndex) -> None:
    size = index.approx_bytes()
    with _cache_lock:
        _cache[org_id] = (fingerprint, index, time.monotonic(), size)
        _cache.move_to_end(org_id)
        total = sum(v[3] for v in _cache.values())
        # Evict least recently used orgs; always keep the one just stored.
        while len(_cache) > 1 and (len(_cache) > _CACHE_MAX_ORGS or total > _CACHE_MAX_BYTES):
            _, evicted = _cache.popitem(last=False)
            total -= evicted[3]


def _org_build_lock(org_id: str) -> threading.Lock:
    with _cache_lock:
        return _build_locks.setdefault(org_id, threading.Lock())


def _background_rebuild(org_id: str, loader: Loader) -> None:
    try:
        with _org_build_lock(org_id):
            fingerprint, entries = loader()
            _store(org_id, fingerprint, MemoryIndex(entries))
    except Exception:
        logger.warning("[Recall] background rebuild failed for org %s", org_id, exc_info=True)
    finally:
        with _cache_lock:
            _pending.pop(org_id, None)


def _schedule_rebuild(org_id: str, loader: Loader) -> Optional[Future]:
    """The org's in-flight build, starting one if none is running."""
    with _cache_lock:
        if org_id in _pending:
            return _pending[org_id]
        _pending[org_id] = None
    try:
        future = _executor.submit(_background_rebuild, org_id, loader)
    except Exception:  # interpreter shutdown
        with _cache_lock:
            _pending.pop(org_id, None)
        return None
    with _cache_lock:
        # A build that already finished has removed its own entry: leave it gone.
        if org_id in _pending:
            _pending[org_id] = future
    return future


def get_org_index(
    org_id: str,
    fingerprint: Fingerprint,
    load_entries: Callable[[], List[Dict]],
    *,
    rebuild: Optional[Loader] = None,
    block: bool = True,
    wait: Optional[float] = None,
) -> Optional[MemoryIndex]:
    """The org's index; see the cache notes above.

    Keyed by org id alone, so one org's index can never answer another's
    query; the fingerprint (entry count, latest ``updated_at``) is only the
    staleness check. *load_entries* reads on the caller's (RLS-pinned) cursor
    and is used only when no *rebuild* loader is given (tests, offline eval).
    *rebuild* opens its own connection and returns ``(fingerprint, entries)``;
    with it, every build runs in the background and a cold caller waits at
    most *wait* seconds (None: until done; ``block=False`` means 0).
    """
    if not block:
        wait = 0.0
    with _cache_lock:
        cached = _cache.get(org_id)
        if cached:
            _cache.move_to_end(org_id)
    if cached and cached[0] == fingerprint:
        return cached[1]
    if cached and rebuild is not None:
        if time.monotonic() - cached[2] >= REBUILD_MIN_INTERVAL:
            _schedule_rebuild(org_id, rebuild)
        return cached[1]
    if rebuild is not None:
        future = _schedule_rebuild(org_id, rebuild)
        if future is not None and wait != 0:
            try:
                future.result(timeout=wait)
            except FutureTimeout:
                pass
        with _cache_lock:
            ready = _cache.get(org_id)
        return ready[1] if ready else None

    with _org_build_lock(org_id):
        # Another caller may have built it while this one waited.
        with _cache_lock:
            cached = _cache.get(org_id)
        if cached and cached[0] == fingerprint:
            return cached[1]
        index = MemoryIndex(load_entries())
        _store(org_id, fingerprint, index)
        return index


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _pending.clear()


def _own_connection_loader(user_id: str, org_id: str) -> Loader:
    """Background loader: a fresh admin connection pinned to *org_id* under RLS."""

    def load() -> Tuple[Fingerprint, List[Dict]]:
        from services.memory.queries import fetch_memory_corpus, fetch_memory_fingerprint
        from utils.auth.stateless_auth import set_rls_context
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                pinned = set_rls_context(
                    cursor, conn, user_id, org_id=org_id, log_prefix="[Recall:rebuild]",
                )
                if pinned != org_id:
                    raise RuntimeError("RLS context not pinned to the cached org")
                return fetch_memory_fingerprint(cursor, org_id), fetch_memory_corpus(cursor, org_id)

    return load


def org_index_for_cursor(
    cursor,
    org_id: str,
    *,
    user_id: Optional[str] = None,
    block: bool = True,
    wait: Optional[float] = None,
) -> Optional[MemoryIndex]:
    """The org's index, reading through a cursor already pinned to it under RLS.

    With *user_id*, a stale index is served immediately and refreshed in the
    background; without it, it is rebuilt inline.
    """
    from services.memory.queries import fetch_memory_corpus, fetch_memory_fingerprint

    return get_org_index(
        org_id,
        fetch_memory_fingerprint(cursor, org_id),
        lambda: fetch_memory_corpus(cursor, org_id),
        rebuild=_own_connection_loader(user_id, org_id) if user_id else None,
        block=block,
        wait=wait,
    )


def search_org_memory(
    cursor,
    org_id: str,
    query: str | Sequence[str],
    *,
    categories: Optional[Iterable[str]] = None,
    k: int = 5,
    user_id: Optional[str] = None,
    block: bool = True,
    wait: Optional[float] = None,
) -> List[Hit]:
    """Search *org_id*'s memory on a cursor already pinned to it under RLS.

    For request handlers: pass the org RBAC authorized and pin RLS to it first
    (see the module docstring of ``services.memory.queries``).
    """
    index = org_index_for_cursor(cursor, org_id, user_id=user_id, block=block, wait=wait)
    if index is None:
        return []
    return index.search(query, categories=categories, k=k)


def search_memory_for_user(
    user_id: str,
    query: str | Sequence[str],
    *,
    categories: Optional[Iterable[str]] = None,
    k: int = 5,
    block: bool = True,
) -> List[Hit]:
    """Background flavour: resolve the user's org and pin RLS, then search.

    ``block=False`` never builds inline: a cold org returns [] and its index is
    built in the background for the next call.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix="[Recall]")
                if not org_id:
                    return []
                return search_org_memory(
                    cursor, org_id, query, categories=categories, k=k,
                    user_id=user_id, block=block,
                )
    except Exception:
        logger.warning("[Recall] search failed for user %s", user_id, exc_info=True)
        return []
