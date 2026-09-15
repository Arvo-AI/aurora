"""
Memory Injector — Async Prefetch + Single-Pass LLM Selection

Runs as a non-blocking prefetch in parallel with prompt building.
One fast LLM call selects up to 5 relevant memories from the index
(title + description). Whatever it selects gets injected into the system prompt. 
- Hard caps: 5 entries max, 4KB per entry.
- Dedup: never re-surface a memory already shown this session.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

from utils.cache.redis_client import get_redis_client
from services.memory.queries import get_memory_entries, fetch_memory_content
from chat.backend.agent.providers import create_chat_model
from langchain_core.messages import SystemMessage, HumanMessage
from chat.backend.agent.llm import ModelConfig


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SELECTED = 5
MAX_MEMORY_BYTES_PER_ENTRY = 4096
STALENESS_DAYS = 2

SELECTOR_MODEL = ModelConfig.RCA_MODEL
SELECTOR_TIMEOUT = 5.0

_SURFACED_SET_TTL = 86400

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


class MemoryPrefetch:
    """Handle for an in-flight memory prefetch operation."""

    def __init__(self, user_id: str, session_id: str, user_message: str):
        self.user_id = user_id
        self.session_id = session_id
        self.user_message = user_message
        self._task: Optional[asyncio.Task] = None
        self._result: Optional[str] = None
        self._started_at: float = 0

    def start(self):
        """Launch the prefetch as an asyncio task (must be called from async context)."""
        self._started_at = time.perf_counter()
        self._task = asyncio.create_task(self._execute())

    async def get_result_async(self, timeout: float = SELECTOR_TIMEOUT + 2.0) -> str:
        """Await prefetch result without blocking the event loop."""
        if self._result is not None:
            return self._result

        if self._task is None:
            return ""

        try:
            self._result = await asyncio.wait_for(self._task, timeout=timeout)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - self._started_at) * 1000
            logger.warning("[MemoryInjector] Prefetch timed out after %.0fms", elapsed)
            self._task.cancel()
            self._result = ""
        except Exception as e:
            elapsed = (time.perf_counter() - self._started_at) * 1000
            logger.warning("[MemoryInjector] Prefetch failed after %.0fms: %s", elapsed, e)
            self._result = ""

        return self._result

    async def _execute(self) -> str:
        try:
            already_surfaced = await asyncio.to_thread(_get_surfaced_set, self.session_id)

            entries = await asyncio.to_thread(get_memory_entries, self.user_id)
            if not entries:
                return ""

            # Remove already-surfaced entries (don't waste selection slots)
            available = [e for e in entries if _entry_key(e) not in already_surfaced]
            if not available:
                return ""

            # Single async LLM pass: select up to 5
            selected = await _select_relevant_memories_async(self.user_message, available)
            if not selected:
                return ""

            # Fetch content of the selected entries
            memories = await asyncio.to_thread(fetch_memory_content, self.user_id, selected)
            if not memories:
                return ""

            # Format and inject (with staleness headers + per-entry cap)
            injection_text, surfaced_keys = _format_for_injection(memories)
            if not injection_text:
                return ""

            await asyncio.to_thread(_mark_surfaced, self.session_id, surfaced_keys)

            logger.info(
                "[MemoryInjector] Injected %d memories for session %s",
                len(surfaced_keys), self.session_id,
            )
            return injection_text

        except Exception:
            logger.exception("[MemoryInjector] Unhandled error in prefetch")
            return ""


# ---------------------------------------------------------------------------
# Select (single async LLM call)
# ---------------------------------------------------------------------------


async def _select_relevant_memories_async(user_message: str, entries: List[Dict]) -> List[Dict]:
    """One strict async LLM call: select up to 5 from the index."""

    manifest_lines = []
    for entry in entries:
        ts = entry["updated_at"].isoformat() if entry["updated_at"] else "unknown"
        desc = entry["description"][:150] if entry["description"] else "(no description)"
        manifest_lines.append(f"- [{entry['category']}] \"{entry['title']}\" | updated: {ts} | {desc}")

    prompt = _SELECTOR_USER.format(
        user_message=user_message[:2000],
        manifest="\n".join(manifest_lines),
    )

    try:
        llm = create_chat_model(SELECTOR_MODEL, temperature=0.0, streaming=False, max_tokens=256)
        response = await llm.ainvoke(
            [
                SystemMessage(content=_SELECTOR_SYSTEM),
                HumanMessage(content=prompt),
            ],
            config={"run_name": "memory_selector"},
        )

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
            title = sel.get("title", "").strip().strip('"')
            key = f"{sel.get('category', '')}/{title}"
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


def _format_for_injection(memories: List[Dict]) -> Tuple[str, Set[str]]:
    """Format memories with staleness headers and per-entry cap."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parts = []
    surfaced: Set[str] = set()

    parts.append("RELEVANT MEMORIES (pre-loaded — only call read_memory() if content below is marked truncated):\n")

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

        parts.append(f"\n--- {mem['category']}/{mem['title']}{staleness} ---\n{content}\n")
        surfaced.add(key)

    if not surfaced:
        return "", set()

    result = "".join(parts)
    result = result.replace("{", "{{").replace("}", "}}")
    return result, surfaced


# ---------------------------------------------------------------------------
# Session dedup (Redis)
# ---------------------------------------------------------------------------


def _entry_key(entry: Dict) -> str:
    return f"{entry.get('category', '')}/{entry.get('title', '')}"


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
        pipe.expire(f"mem:inject:surfaced:{session_id}", _SURFACED_SET_TTL)
        pipe.execute()
    except Exception:
        logger.warning("Failed to mark surfaced memory keys for session %s", session_id, exc_info=True)
