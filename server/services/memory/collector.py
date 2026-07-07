"""
Memory Extraction Agent — Background Celery Task

Triggered after each turn (interactive or RCA) to analyze the conversation
and extract durable learnings into org memory. Modeled after Claude Code's
extraction agent:

- Fires post-turn (non-blocking, via Celery)
- Reads the recent conversation context
- Asks an LLM what should be remembered
- Writes/appends to memory using existing memory tools
- Tracks cursor (last processed message) to avoid re-processing
- Throttle: skips if last extraction was < N minutes ago
- Dedup: checks existing memory before writing

Key differences from Claude Code:
- Uses Celery instead of forked process
- DB-backed memory instead of filesystem
- RLS context required for all DB operations
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from celery_config import celery_app
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context, get_org_id_for_user
from utils.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EXTRACTION_MODEL = os.getenv("MEMORY_EXTRACTION_MODEL", "anthropic/claude-haiku-4.5")
MAX_TURNS_PER_EXTRACTION = 10  # Max messages to look at per extraction
MAX_EXTRACTIONS_PER_TURN = 3  # Max new memories to write per extraction

_REDIS_CURSOR_PREFIX = "mem:extract:cursor:"
_REDIS_LOCK_PREFIX = "mem:extract:lock:"
_LOCK_TTL = 120  # seconds — prevent parallel extractions for same session

# ---------------------------------------------------------------------------
# Extraction prompt
# ---------------------------------------------------------------------------

_EXTRACTION_SYSTEM = """You are a memory extraction agent. Your job is to analyze a conversation between a user and an AI assistant, and identify durable facts that should be saved to organizational memory.

CATEGORIES:
- context: Team preferences, escalation paths, org policies, behavioral instructions, communication style
- infrastructure: Service topology, deployment chains, monitoring configs, dependencies
- runbook: Step-by-step procedures for known issues
- learned: Non-obvious root causes, debugging patterns, resolution steps from incidents
- postmortem: Incident postmortem summaries and key takeaways

RULES:
1. Only extract facts that will be useful in FUTURE conversations
2. DO NOT extract ephemeral data (specific log lines, timestamps, one-off metrics)
3. DO NOT extract information that's already in the existing memory index
4. DO NOT extract obvious/generic knowledge (e.g. "kubectl get pods lists pods")
5. Prefer UPDATING existing entries over creating new ones (use append/edit)
6. Keep entries focused — one topic per entry
7. Use clear, specific titles and descriptions
8. If nothing is worth remembering, return an empty array

Return a JSON object with this structure:
{
  "extractions": [
    {
      "action": "write" | "append",
      "category": "<category>",
      "title": "<clear title>",
      "description": "<one-line description for indexing>",
      "content": "<the fact/knowledge to save, in markdown>",
      "reasoning": "<why this is worth remembering>"
    }
  ]
}

If nothing should be extracted, return: {"extractions": []}"""

_EXTRACTION_USER_TEMPLATE = """EXISTING MEMORY INDEX:
{memory_index}

RECENT CONVERSATION (analyze this for durable learnings):
{conversation}

Extract 0-{max_extractions} facts worth remembering. Return JSON only."""


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="services.memory.collector.extract_memories_from_session",
    bind=True,
    soft_time_limit=60,
    time_limit=90,
    max_retries=0,
    acks_late=True,
)
def extract_memories_from_session(
    self,
    session_id: str,
    user_id: str,
):
    """Extract and persist learnings from a conversation turn.

    Should be enqueued after each meaningful turn completes.
    """
    log_prefix = f"[MemoryCollector:{session_id[:8]}]"

    # Cooldown check — don't extract too frequently for the same session
    if not _acquire_extraction_lock(session_id):
        logger.debug("%s Skipped — extraction already in progress", log_prefix)
        return {"status": "skipped", "reason": "lock"}

    try:
        # Check before_llm_call hook
        from utils.hooks import get_hook
        hook_allowed, hook_message = get_hook("before_llm_call")(get_org_id_for_user(user_id), user_id)
        if not hook_allowed:
            logger.info("%s Hook blocked: %s", log_prefix, hook_message)
            return {"status": "hook_blocked"}

        # Load recent conversation messages
        messages = _load_recent_messages(session_id, user_id)
        if not messages:
            logger.debug("%s No messages to process", log_prefix)
            return {"status": "skipped", "reason": "no_messages"}

        # Load existing memory index (for dedup context)
        memory_index = _build_existing_index(user_id)

        # Run extraction LLM
        extractions = _run_extraction(messages, memory_index, log_prefix)
        if not extractions:
            return {"status": "ok", "extracted": 0}

        # Write extractions to memory
        written = _persist_extractions(extractions, user_id, session_id, log_prefix)

        logger.info("%s Extracted %d memories (wrote %d)", log_prefix, len(extractions), written)
        return {"status": "ok", "extracted": len(extractions), "written": written}

    except Exception as e:
        logger.exception("%s Extraction failed", log_prefix)
        return {"status": "error", "error": str(e)}
    finally:
        _release_extraction_lock(session_id)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_recent_messages(session_id: str, user_id: str) -> List[Dict]:
    """Load the most recent messages from the session's llm_context_history."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[MemoryCollector:load]")
                cursor.execute(
                    """SELECT llm_context_history FROM chat_sessions
                       WHERE session_id = %s""",
                    (session_id,),
                )
                row = cursor.fetchone()

        if not row or not row[0]:
            return []

        history = row[0]
        if isinstance(history, str):
            history = json.loads(history)

        # Get the last N messages (most recent turn)
        cursor_idx = _get_cursor(session_id)
        recent = history[cursor_idx:] if cursor_idx < len(history) else history[-MAX_TURNS_PER_EXTRACTION:]

        # Cap to reasonable size
        if len(recent) > MAX_TURNS_PER_EXTRACTION:
            recent = recent[-MAX_TURNS_PER_EXTRACTION:]

        # Update cursor to current position
        _set_cursor(session_id, len(history))

        return recent

    except Exception:
        logger.exception("[MemoryCollector] Failed to load messages")
        return []


def _build_existing_index(user_id: str) -> str:
    """Build a compact index of existing memories for dedup."""
    from services.memory.index_builder import build_memory_index
    try:
        return build_memory_index(user_id) or "(no existing memories)"
    except Exception:
        return "(failed to load memory index)"


def _run_extraction(
    messages: List[Dict], memory_index: str, log_prefix: str
) -> List[Dict]:
    """Call the extraction LLM and parse results."""
    from chat.backend.agent.providers import create_chat_model
    from langchain_core.messages import SystemMessage, HumanMessage

    # Format conversation for the prompt
    conversation_parts = []
    for msg in messages:
        role = msg.get("type", msg.get("role", "unknown"))
        content = msg.get("content", "")

        # Handle complex content structures
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = " ".join(text_parts)
        elif not isinstance(content, str):
            content = str(content)

        # Skip tool messages (too noisy for extraction)
        if role == "tool":
            content = f"[tool output: {content[:200]}...]" if len(content) > 200 else f"[tool output: {content}]"

        conversation_parts.append(f"[{role}]: {content[:1500]}")

    conversation_text = "\n\n".join(conversation_parts)

    # Cap total prompt size
    if len(conversation_text) > 15000:
        conversation_text = conversation_text[-15000:]

    prompt = _EXTRACTION_USER_TEMPLATE.format(
        memory_index=memory_index[:5000],
        conversation=conversation_text,
        max_extractions=MAX_EXTRACTIONS_PER_TURN,
    )

    try:
        llm = create_chat_model(EXTRACTION_MODEL, temperature=0.0, streaming=False)
        response = llm.invoke([
            SystemMessage(content=_EXTRACTION_SYSTEM),
            HumanMessage(content=prompt),
        ])

        content = response.content if hasattr(response, "content") else str(response)

        # Parse JSON (handle code blocks)
        json_str = content.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[1] if "\n" in json_str else json_str
            json_str = json_str.rsplit("```", 1)[0]
        parsed = json.loads(json_str)

        extractions = parsed.get("extractions", [])
        if not isinstance(extractions, list):
            return []

        # Validate structure
        valid = []
        for ext in extractions[:MAX_EXTRACTIONS_PER_TURN]:
            if all(k in ext for k in ("action", "category", "title", "content")):
                valid.append(ext)

        return valid

    except json.JSONDecodeError as e:
        logger.warning("%s Extraction returned invalid JSON: %s", log_prefix, e)
        return []
    except Exception as e:
        logger.warning("%s Extraction LLM failed: %s", log_prefix, e)
        return []


def _persist_extractions(
    extractions: List[Dict], user_id: str, session_id: str, log_prefix: str
) -> int:
    """Write extracted memories using the memory tool functions."""
    from chat.backend.agent.tools.memory_tool import write_memory, append_to_memory
    from services.memory import MEMORY_CATEGORIES

    written = 0
    for ext in extractions:
        category = ext.get("category", "")
        title = ext.get("title", "")
        content = ext.get("content", "")
        description = ext.get("description", "")
        action = ext.get("action", "write")

        # Validate category
        if category not in MEMORY_CATEGORIES:
            logger.debug("%s Skipping invalid category: %s", log_prefix, category)
            continue

        try:
            if action == "append":
                result = append_to_memory(
                    category=category,
                    title=title,
                    content=content,
                    user_id=user_id,
                    session_id=session_id,
                )
            else:
                result = write_memory(
                    category=category,
                    title=title,
                    content=content,
                    description=description,
                    overwrite=False,
                    user_id=user_id,
                    session_id=session_id,
                )

            result_data = json.loads(result)
            if result_data.get("status") == "ok":
                written += 1
                logger.info(
                    "%s Wrote memory: %s/%s (%s)",
                    log_prefix, category, title, action,
                )
            elif result_data.get("status") == "already_exists":
                # Try appending instead
                result = append_to_memory(
                    category=category,
                    title=title,
                    content=content,
                    user_id=user_id,
                    session_id=session_id,
                )
                result_data = json.loads(result)
                if result_data.get("status") == "ok":
                    written += 1
                    logger.info("%s Appended to existing: %s/%s", log_prefix, category, title)
            else:
                logger.debug(
                    "%s Memory write returned: %s", log_prefix, result_data.get("status")
                )
        except Exception as e:
            logger.warning("%s Failed to persist extraction: %s", log_prefix, e)

    return written


# ---------------------------------------------------------------------------
# Redis-based coordination
# ---------------------------------------------------------------------------


def _acquire_extraction_lock(session_id: str) -> bool:
    """Try to acquire an extraction lock. Returns True if acquired."""
    try:
        r = get_redis_client()
        key = f"{_REDIS_LOCK_PREFIX}{session_id}"
        return bool(r.set(key, "1", nx=True, ex=_LOCK_TTL))
    except Exception:
        return True  # Fail open — allow extraction if Redis is down


def _release_extraction_lock(session_id: str):
    """Release the extraction lock."""
    try:
        r = get_redis_client()
        r.delete(f"{_REDIS_LOCK_PREFIX}{session_id}")
    except Exception:
        pass


def _get_cursor(session_id: str) -> int:
    """Get the message cursor (index of last processed message)."""
    try:
        r = get_redis_client()
        val = r.get(f"{_REDIS_CURSOR_PREFIX}{session_id}")
        return int(val) if val else 0
    except Exception:
        return 0


def _set_cursor(session_id: str, position: int):
    """Update the message cursor."""
    try:
        r = get_redis_client()
        r.setex(f"{_REDIS_CURSOR_PREFIX}{session_id}", 86400, str(position))
    except Exception:
        pass
