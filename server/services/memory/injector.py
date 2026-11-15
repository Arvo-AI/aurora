"""
Memory Injector — Async Prefetch + Single-Pass LLM Selection

Runs as a non-blocking prefetch in parallel with prompt building.
One fast, non-thinking LLM call selects up to 5 relevant memories from the
index (title + description). Whatever it selects gets injected into the system prompt.
- Hard caps: 5 entries max, 4KB per entry, 100 candidates in the selector prompt.
- Dedup: never re-surface a memory already shown this session.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Set, Tuple

from utils.cache.redis_client import get_redis_client
from services.memory.queries import get_memory_entries, fetch_memory_content
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.utils.message_content import extract_text_from_content
from langchain_core.messages import SystemMessage, HumanMessage
from chat.backend.agent.llm import ModelConfig


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_SELECTED = 5
MAX_MEMORY_BYTES_PER_ENTRY = 4096
STALENESS_DAYS = 2

# The TOC index stops at 200. The selector has to read past that, or a memory
# outside the newest slice can never be injected (a 420-entry bank hid ~220).
_SELECTOR_FETCH_LIMIT = 2000
# Lexical rank misses a paraphrase. 100 keeps more of those in the prompt
# than 60 did, without sending the whole bank.
_MAX_SELECTOR_CANDIDATES = 100
# Flash-lite thinks at "minimal" by default, and thought tokens count against
# this cap. 256 can be spent before the JSON is written.
SELECTOR_MAX_TOKENS = 1024
# Shown when the selector times out or returns nothing usable. A valid empty
# selection must not use this — that one really found nothing.
_SELECTOR_MISSED = (
    "MEMORY SELECTOR DID NOT FINISH. No memories were pre-loaded for this turn. "
    "That is not a finding that none are relevant. Use the org memory index and "
    "read_memory(category, title) before assuming the bank has nothing for this query."
)
SELECTOR_TIMEOUT = 5.0

# Letters and digits. CJK, kana, and Hangul have no spaces, so a whole
# sentence would be one token and never overlap a shorter title.
_WORD_RE = re.compile(r"[^\W_]{2,}", re.UNICODE)
_UNSPACED_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")
_STOPWORDS = frozenset({
    "an", "the", "and", "or", "to", "of", "in", "on", "for", "is", "it",
    "be", "as", "at", "by", "with", "from", "this", "that", "our", "we", "my",
})

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

    def __init__(
        self,
        user_id: str,
        session_id: str,
        user_message: str,
        force_entries: Optional[List[Tuple[str, str]]] = None,
    ):
        self.user_id = user_id
        self.session_id = session_id
        self.user_message = user_message
        # (category, title) pairs to ALWAYS inject regardless of the LLM
        # selector — used for source-specific policy memories (e.g. the "Slack"
        # entry on Slack-sourced sessions) that must always steer behaviour.
        self.force_entries = force_entries or []
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
            # An empty string looks like "nothing relevant". Say the call died.
            self._result = _SELECTOR_MISSED
        except Exception as e:
            elapsed = (time.perf_counter() - self._started_at) * 1000
            logger.warning("[MemoryInjector] Prefetch failed after %.0fms: %s", elapsed, e)
            self._result = _SELECTOR_MISSED

        return self._result

    async def _execute(self) -> str:
        try:
            already_surfaced = await asyncio.to_thread(_get_surfaced_set, self.session_id)

            entries = await asyncio.to_thread(
                get_memory_entries, self.user_id, _SELECTOR_FETCH_LIMIT,
            )
            if not entries:
                return ""

            # Forced entries always inject (bypass selector + dedup) so
            # source-specific policy (e.g. the "Slack" memory) is guaranteed
            # present. Match against the real index so we never inject a
            # title that doesn't exist.
            forced_keys = {f"{c}/{t}" for c, t in self.force_entries}
            forced = [e for e in entries if _entry_key(e) in forced_keys]

            # Remove already-surfaced entries (don't waste selection slots).
            # Forced entries are excluded from the selector pool since they're
            # added unconditionally below.
            available = [
                e for e in entries
                if _entry_key(e) not in already_surfaced
                and _entry_key(e) not in forced_keys
            ]

            selected: List[Dict] = list(forced)
            # A valid empty list means nothing matched. A failed call must not
            # look the same, or the agent treats a timeout as "no memories".
            selector_missed = False
            if available:
                try:
                    llm_selected = await _select_relevant_memories_async(self.user_message, available)
                except _SelectorFailed:
                    selector_missed = True
                    llm_selected = []
                selected.extend(llm_selected)

            if not selected and not selector_missed:
                return ""
            # The model call failed and there is nothing forced to show.
            if not selected:
                return _SELECTOR_MISSED

            # Fetch content of the selected entries
            memories = await asyncio.to_thread(fetch_memory_content, self.user_id, selected)
            if not memories:
                return _SELECTOR_MISSED if selector_missed else ""

            # Format and inject (with staleness headers + per-entry cap)
            injection_text, surfaced_keys = _format_for_injection(memories)
            if not injection_text:
                return _SELECTOR_MISSED if selector_missed else ""

            await asyncio.to_thread(_mark_surfaced, self.session_id, surfaced_keys)

            logger.info(
                "[MemoryInjector] Injected %d memories for session %s",
                len(surfaced_keys), self.session_id,
            )
            if selector_missed:
                return injection_text + "\n" + _SELECTOR_MISSED
            return injection_text

        except Exception:
            logger.exception("[MemoryInjector] Unhandled error in prefetch")
            return _SELECTOR_MISSED


class _SelectorFailed(Exception):
    """The selector replied, but not with a list we can use."""


# ---------------------------------------------------------------------------
# Select (single async LLM call)
# ---------------------------------------------------------------------------


def _tokens(text: str) -> Set[str]:
    raw = text or ""
    tokens = {t for t in _WORD_RE.findall(raw.lower()) if t not in _STOPWORDS}
    # Bigrams, so "数据库故障" overlaps a query that says "数据库" without sharing the whole string.
    for run in _UNSPACED_RE.findall(raw):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _complete_objects(text: str) -> List[Dict]:
    """Pull closed JSON objects out of a reply that was cut off mid-string."""
    found: List[Dict] = []
    for match in re.finditer(r"\{[^{}]*\}", text or ""):
        try:
            obj = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("title"), str):
            found.append(obj)
    return found


def _as_naive(ts) -> datetime:
    # Missing timestamps sort last, behind any real updated_at.
    if ts is None:
        return datetime.min
    # Aware DB timestamps can't be ordered against datetime.min.
    if getattr(ts, "tzinfo", None) is not None:
        return ts.replace(tzinfo=None)
    return ts


def _shortlist_entries(user_message: str, entries: List[Dict], limit: int) -> List[Dict]:
    """Keep the entries most likely to matter so the selector prompt stays small."""
    query_tokens = _tokens(user_message[:2000])

    def rank(entry: Dict):
        title = _tokens(entry.get("title") or "")
        category = _tokens(entry.get("category") or "")
        desc = _tokens((entry.get("description") or "")[:150])
        overlap = (
            3 * len(query_tokens & title)
            + 2 * len(query_tokens & category)
            + len(query_tokens & desc)
        )
        # Recency breaks ties, and is the only signal when nothing overlaps.
        return (overlap, _as_naive(entry.get("updated_at")))

    ranked = sorted(entries, key=rank, reverse=True)
    return ranked[:limit]


async def _select_relevant_memories_async(user_message: str, entries: List[Dict]) -> List[Dict]:
    """One strict async LLM call: select up to 5 from the index."""

    # A few hundred full descriptions blow the prefetch budget. Rank first.
    pool = entries
    if len(entries) > _MAX_SELECTOR_CANDIDATES:
        pool = _shortlist_entries(user_message, entries, _MAX_SELECTOR_CANDIDATES)
        logger.info(
            "[MemoryInjector] Shortlisted %d/%d memories before selection",
            len(pool), len(entries),
        )

    manifest_lines = []
    for entry in pool:
        ts = entry["updated_at"].isoformat() if entry["updated_at"] else "unknown"
        desc = entry["description"][:150] if entry["description"] else "(no description)"
        manifest_lines.append(f"- [{entry['category']}] \"{entry['title']}\" | updated: {ts} | {desc}")

    prompt = _SELECTOR_USER.format(
        user_message=user_message[:2000],
        manifest="\n".join(manifest_lines),
    )

    model = ModelConfig.memory_selector_model()
    extra: Dict = {}
    native = model.split("/", 1)[-1].lower()
    # The OpenAI provider pins gpt-5+ to reasoning effort=high. That spends
    # the prefetch window. gpt-4 and older reject the field entirely.
    if model.startswith("openai/") and not native.startswith(("gpt-3", "gpt-4")):
        extra["reasoning"] = {"effort": "low"}
    # Opus 5.5 rejects thinking.type=disabled with a 400. Haiku 5.5 thinks
    # by default and accepts off, which is what keeps this call inside the window.
    if "haiku-5" in native or "haiku-4" in native:
        provider_mode_for_thinking = (os.getenv("LLM_PROVIDER_MODE") or "direct").lower()
        # Converse wants the field beside the prompt. ChatAnthropic takes `thinking`.
        if provider_mode_for_thinking == "bedrock" or model.startswith("bedrock/"):
            extra["additional_model_request_fields"] = {"thinking": {"type": "disabled"}}
        elif model.startswith("anthropic/"):
            extra["thinking"] = {"type": "disabled"}
    # Gemini 3+ flash-lite counts minimal thoughts against max_tokens.
    # 2.5 flash-lite rejects thinking_level, and OpenRouter rejects the field.
    thinking_off = os.getenv("GEMINI_DISABLE_THINKING", "").lower() in ("true", "1", "yes")
    provider_mode = (os.getenv("LLM_PROVIDER_MODE") or "direct").lower()
    gemini3_flash_lite = "flash-lite" in native and ("gemini-3" in native or "gemini-4" in native)
    if gemini3_flash_lite and provider_mode != "openrouter" and not thinking_off:
        extra["thinking_level"] = "minimal"
    try:
        llm = create_chat_model(
            model, temperature=0.0, streaming=False, max_tokens=SELECTOR_MAX_TOKENS, **extra,
        )
        response = await llm.ainvoke(
            [
                SystemMessage(content=_SELECTOR_SYSTEM),
                HumanMessage(content=prompt),
            ],
            config={"run_name": "memory_selector"},
        )

        # Thinking-capable models return a list of content blocks, not a str —
        # extract_text_from_content normalizes both so .strip() can't blow up.
        content = extract_text_from_content(getattr(response, "content", response))
        json_str = content.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[1] if "\n" in json_str else json_str
            json_str = json_str.rsplit("```", 1)[0]

        try:
            parsed = json.loads(json_str)
        except json.JSONDecodeError as e:
            # A cut reply still has the objects that closed. An empty body does not.
            selected_raw = _complete_objects(json_str)
            if not selected_raw:
                logger.warning("[MemoryInjector] Selector returned invalid JSON: %s", e)
                raise _SelectorFailed from e
            logger.warning(
                "[MemoryInjector] Selector JSON was cut off; kept %d complete entries",
                len(selected_raw),
            )
        else:
            selected_raw = parsed.get("selected", []) if isinstance(parsed, dict) else None
            # A non-list "selected" is a malformed reply, not an empty selection.
            if not isinstance(selected_raw, list):
                raise _SelectorFailed

        # Validate against the candidates we actually showed the model.
        entry_lookup = {_entry_key(e): e for e in pool}
        validated = []
        for sel in selected_raw[:MAX_SELECTED]:
            title = sel.get("title", "").strip().strip('"')
            key = f"{sel.get('category', '')}/{title}"
            if key in entry_lookup:
                validated.append(entry_lookup[key])

        logger.info(
            "[MemoryInjector] Selected %d/%d entries with %s",
            len(validated), len(entries), model,
        )
        return validated

    except _SelectorFailed:
        raise
    except Exception as e:
        logger.warning("[MemoryInjector] Selector failed (%s): %s", model, e)
        raise _SelectorFailed from e


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
