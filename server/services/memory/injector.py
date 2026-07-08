"""
Memory Injector — Async Prefetch + Single-Pass LLM Selection

Runs as a non-blocking prefetch in parallel with prompt building.
One fast LLM call selects up to 5 relevant memories from the index
(title + description). Whatever it selects gets injected into the system prompt. 
- Hard caps: 5 entries max, 4KB per entry, 60KB per session.
- Dedup: never re-surface a memory already shown this session.
"""

import json
import logging
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

from utils.cache.redis_client import get_redis_client
from services.memory.queries import get_memory_entries, fetch_memory_content

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SELECTED = 5
MAX_MEMORY_BYTES_PER_ENTRY = 4096
MAX_SESSION_BYTES = 60_000
MAX_INDEX_ENTRIES = 200
MIN_QUERY_LENGTH = 8  # Single words don't provide enough selection context
STALENESS_DAYS = 2

SELECTOR_MODEL = os.getenv("MEMORY_SELECTOR_MODEL", "anthropic/claude-haiku-4.5")
SELECTOR_TIMEOUT = float(os.getenv("MEMORY_SELECTOR_TIMEOUT", "5.0"))

_SESSION_BUDGET_TTL = 86400
_prefetch_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mem-inject")

# ---------------------------------------------------------------------------
# Selector prompt — strict, single-pass
# ---------------------------------------------------------------------------

_SELECTOR_SYSTEM = """You are selecting memories that will be useful to Aurora (a cloud operations AI) as it processes a user's query. Aurora investigates incidents, runs root cause analysis, manages infrastructure, and assists with DevOps/SRE tasks. You will be given the user's message and a list of available memory entries with their category, title, and description.

Return a list of entries that will clearly be useful to Aurora as it processes this query (up to 5). Only include memories that you are certain will be helpful based on their title and description.
- If you are unsure if a memory will be useful, do not include it. Be selective and discerning.
- If there are no memories that would clearly be useful, return an empty list.
- Do not select memories that merely repeat generic knowledge Aurora already has. DO still select memories containing org-specific context, warnings, known issues, past incident patterns, or team preferences — those always matter.
- Prioritize: runbooks when troubleshooting, infrastructure context for system questions, learned patterns from past incidents for debugging, org preferences for behavioral questions.

Return JSON:
{"selected": [{"category": "...", "title": "..."}, ...]}

If nothing is relevant: {"selected": []}"""

_SELECTOR_USER = """USER MESSAGE:
{user_message}

MEMORY INDEX:
{manifest}

Select 0-5 memories. Only include those you are certain will help. JSON only."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start_memory_prefetch(
    user_id: str,
    session_id: str,
    user_message: str,
) -> "MemoryPrefetch":
    """Fire a non-blocking memory prefetch. Returns a handle to collect results."""
    prefetch = MemoryPrefetch(user_id, session_id, user_message)
    prefetch.start()
    return prefetch


class MemoryPrefetch:
    """Handle for an in-flight memory prefetch operation."""

    def __init__(self, user_id: str, session_id: str, user_message: str):
        self.user_id = user_id
        self.session_id = session_id
        self.user_message = user_message
        self._future: Optional[Future] = None
        self._result: Optional[str] = None
        self._started_at: float = 0

    def start(self):
        self._started_at = time.perf_counter()
        self._future = _prefetch_pool.submit(self._execute)

    def get_result(self, timeout: float = SELECTOR_TIMEOUT + 2.0) -> str:
        """Block until prefetch settles. Returns injection text or empty string."""
        if self._result is not None:
            return self._result

        if self._future is None:
            return ""

        try:
            self._result = self._future.result(timeout=timeout)
        except Exception as e:
            elapsed = (time.perf_counter() - self._started_at) * 1000
            logger.warning("[MemoryInjector] Prefetch failed after %.0fms: %s", elapsed, e)
            self._result = ""

        return self._result

    def _execute(self) -> str:
        try:
            return _run_prefetch(self.user_id, self.session_id, self.user_message)
        except Exception:
            logger.exception("[MemoryInjector] Unhandled error in prefetch")
            return ""


# ---------------------------------------------------------------------------
# Pipeline: scan → select (1 LLM call) → fetch content → format → inject
# ---------------------------------------------------------------------------


def _run_prefetch(user_id: str, session_id: str, user_message: str) -> str:
    # Not enough context to select meaningfully
    if len(user_message.strip()) < MIN_QUERY_LENGTH:
        return ""

    # Session budget exhausted — memories already saturate context
    budget_used = _get_session_budget(session_id)
    if budget_used >= MAX_SESSION_BYTES:
        return ""

    already_surfaced = _get_surfaced_set(session_id)

    # Scan index (titles + descriptions, no content)
    entries = get_memory_entries(user_id, limit=MAX_INDEX_ENTRIES)
    if not entries:
        return ""

    # Remove already-surfaced entries (don't waste selection slots)
    available = [e for e in entries if _entry_key(e) not in already_surfaced]
    if not available:
        return ""

    # Single LLM pass: select up to 5
    selected = _select_relevant_memories(user_message, available)
    if not selected:
        return ""

    # Fetch content of the selected entries
    memories = fetch_memory_content(user_id, selected)
    if not memories:
        return ""

    # Format and inject (with staleness headers + budget enforcement)
    remaining_budget = MAX_SESSION_BYTES - budget_used
    injection_text, bytes_used, surfaced_keys = _format_for_injection(memories, remaining_budget)
    if not injection_text:
        return ""

    _update_session_budget(session_id, budget_used + bytes_used)
    _mark_surfaced(session_id, surfaced_keys)

    elapsed = (time.perf_counter() - time.perf_counter())  # logged by caller
    logger.info(
        "[MemoryInjector] Injected %d memories (%d bytes) for session %s",
        len(surfaced_keys), bytes_used, session_id,
    )
    return injection_text


# ---------------------------------------------------------------------------
# Select (single LLM call)
# ---------------------------------------------------------------------------


def _select_relevant_memories(user_message: str, entries: List[Dict]) -> List[Dict]:
    """One strict LLM call: select up to 5 from the index."""
    from chat.backend.agent.providers import create_chat_model
    from langchain_core.messages import SystemMessage, HumanMessage

    manifest_lines = []
    for entry in entries:
        ts = entry["updated_at"].isoformat() if entry["updated_at"] else "unknown"
        desc = entry["description"][:150] if entry["description"] else "(no description)"
        manifest_lines.append(f"- [{entry['category']}] {entry['title']} ({ts}): {desc}")

    prompt = _SELECTOR_USER.format(
        user_message=user_message[:2000],
        manifest="\n".join(manifest_lines),
    )

    try:
        llm = create_chat_model(SELECTOR_MODEL, temperature=0.0, streaming=False, max_tokens=256)
        response = llm.invoke([
            SystemMessage(content=_SELECTOR_SYSTEM),
            HumanMessage(content=prompt),
        ])

        content = response.content if hasattr(response, "content") else str(response)
        json_str = content.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[1] if "\n" in json_str else json_str
            json_str = json_str.rsplit("```", 1)[0]

        parsed = json.loads(json_str)
        selected_raw = parsed.get("selected", [])
        if not isinstance(selected_raw, list):
            return []

        # Validate against actual entries
        entry_lookup = {_entry_key(e): e for e in entries}
        validated = []
        for sel in selected_raw[:MAX_SELECTED]:
            key = f"{sel.get('category', '')}/{sel.get('title', '')}"
            if key in entry_lookup:
                validated.append(entry_lookup[key])

        logger.info("[MemoryInjector] Selected %d/%d entries", len(validated), len(entries))
        return validated

    except json.JSONDecodeError as e:
        logger.warning("[MemoryInjector] Selector returned invalid JSON: %s", e)
        return []
    except Exception as e:
        logger.warning("[MemoryInjector] Selector failed: %s", e)
        return []


# ---------------------------------------------------------------------------
# Format for injection
# ---------------------------------------------------------------------------


def _format_for_injection(
    memories: List[Dict], remaining_budget: int
) -> Tuple[str, int, Set[str]]:
    """Format memories with staleness headers, respecting per-entry and budget caps."""
    now = datetime.now(timezone.utc)
    parts = []
    total_bytes = 0
    surfaced: Set[str] = set()

    header = "RELEVANT MEMORIES (auto-loaded — verify stale data before acting):\n"
    header_bytes = len(header.encode("utf-8"))
    if header_bytes > remaining_budget:
        return "", 0, set()
    total_bytes += header_bytes
    parts.append(header)

    for mem in memories:
        key = _entry_key(mem)
        content = mem["content"]

        # Per-entry cap: 4KB
        if len(content.encode("utf-8")) > MAX_MEMORY_BYTES_PER_ENTRY:
            content = content[:MAX_MEMORY_BYTES_PER_ENTRY]
            content += "\n... (truncated — use read_memory() for full content)"

        # Staleness header
        staleness = ""
        if mem["updated_at"]:
            age = now - mem["updated_at"]
            if age > timedelta(days=STALENESS_DAYS):
                staleness = f" ⚠️ Last updated {age.days}d ago — verify before acting"

        entry_text = f"\n--- {mem['category']}/{mem['title']}{staleness} ---\n{content}\n"
        entry_bytes = len(entry_text.encode("utf-8"))

        # Budget check
        if total_bytes + entry_bytes > remaining_budget:
            break

        parts.append(entry_text)
        total_bytes += entry_bytes
        surfaced.add(key)

    if not surfaced:
        return "", 0, set()

    result = "".join(parts)
    result = result.replace("{", "{{").replace("}", "}}")
    return result, total_bytes, surfaced


# ---------------------------------------------------------------------------
# Session tracking (Redis)
# ---------------------------------------------------------------------------


def _entry_key(entry: Dict) -> str:
    return f"{entry.get('category', '')}/{entry.get('title', '')}"


def _get_session_budget(session_id: str) -> int:
    try:
        r = get_redis_client()
        val = r.get(f"mem:inject:budget:{session_id}")
        return int(val) if val else 0
    except Exception:
        return 0


def _update_session_budget(session_id: str, new_total: int):
    try:
        r = get_redis_client()
        r.setex(f"mem:inject:budget:{session_id}", _SESSION_BUDGET_TTL, str(new_total))
    except Exception:
        pass


def _get_surfaced_set(session_id: str) -> Set[str]:
    try:
        r = get_redis_client()
        members = r.smembers(f"mem:inject:surfaced:{session_id}")
        return {m.decode("utf-8") if isinstance(m, bytes) else m for m in members} if members else set()
    except Exception:
        return set()


def _mark_surfaced(session_id: str, keys: Set[str]):
    if not keys:
        return
    try:
        r = get_redis_client()
        pipe = r.pipeline()
        pipe.sadd(f"mem:inject:surfaced:{session_id}", *keys)
        pipe.expire(f"mem:inject:surfaced:{session_id}", _SESSION_BUDGET_TTL)
        pipe.execute()
    except Exception:
        pass
