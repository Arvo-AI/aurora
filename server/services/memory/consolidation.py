"""
Memory Dream Consolidation — Nightly GC Agent

Runs once per night (via Celery Beat) for each org. Uses a full agentic loop
with memory tools to review, merge, rewrite, and delete entries.

- Dedup/merge near-duplicates
- Remove stale/contradicted entries
- Fix formatting, split mega-entries
- Convert relative dates to absolute
- DB handles concurrency (FOR UPDATE, ON CONFLICT)
"""

import logging
import time
from datetime import datetime, timezone
from typing import Dict

from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

from celery_config import celery_app
from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.tools.memory_tool import build_memory_tools
from services.memory import MEMORY_CATEGORIES
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context, get_org_id_for_user
from utils.cache.redis_client import get_redis_client
from utils.hooks import get_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_AGENT_TURNS = 10
MIN_ENTRIES_TO_CONSOLIDATE = 3
_REDIS_LAST_RUN_KEY = "mem:dream:last_run:{org_id}"
_MIN_HOURS_BETWEEN_RUNS = 24

# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are a memory maintenance agent. You run nightly to keep the org's memory bank lean, accurate, and well-organized.

You have tools to list, read, write, edit, and delete memory entries. Your job:
1. Review what exists (list_memories + read_memory on entries that look duplicated or stale)
2. Merge duplicates (write the merged content to the better entry, delete the other)
3. Rewrite entries that need cleanup (edit_memory for surgical fixes, write_memory with overwrite for full rewrites)
4. Delete entries that are clearly stale or fully subsumed by another

GOALS:
- MERGE duplicates — if two entries cover the same topic, combine into one
- REMOVE stale entries — facts clearly outdated or contradicted by newer entries
- FIX formatting — ensure entries follow consistent structure
- DEDUPLICATE within entries — remove repeated paragraphs within a single entry
- CONVERT relative dates — "yesterday", "last week" → absolute dates where context allows

RULES:
- Be CONSERVATIVE — only act when confident the change improves things
- NEVER delete entries with unique, non-redundant information
- ALWAYS prefer merging over deleting
- Preserve all factual content during merges — don't lose information
- If unsure, leave the entry alone

TURN BUDGET: You have {max_turns} turns. Be efficient:
- Turn 1: Call list_memories to survey what exists.
- Turn 2-3: Call read_memory on entries that look like duplicates or are potentially stale.
- Remaining turns: Execute changes (write_memory, edit_memory, delete_memory).
- Final turn: Respond with "DONE: <summary of what you changed>".

If the memory bank looks clean, just respond "DONE: no changes needed" without making any modifications.

Today's date: {today}"""


# ---------------------------------------------------------------------------
# Celery Tasks
# ---------------------------------------------------------------------------


@celery_app.task(
    name="services.memory.consolidation.run_memory_consolidation",
    bind=True,
    soft_time_limit=300,
    time_limit=360,
    max_retries=0,
    acks_late=True,
)
def run_memory_consolidation(self, org_id: str, user_id: str):
    """Run memory consolidation for a single org."""
    log_prefix = f"[MemoryDream:{org_id[:8]}]"

    try:
        # Check before_llm_call hook (billing/entitlement gate)
        hook_allowed, hook_message = get_hook("before_llm_call")(org_id, user_id)
        if not hook_allowed:
            logger.info("%s Hook blocked: %s", log_prefix, hook_message)
            return {"status": "hook_blocked"}

        # Quick check: does this org have enough memories to bother?
        entry_count = _count_memories(org_id, user_id)
        if entry_count < MIN_ENTRIES_TO_CONSOLIDATE:
            logger.info("%s Only %d entries — skipping", log_prefix, entry_count)
            _mark_completed(org_id)
            return {"status": "skipped", "reason": "too_few_entries"}

        # Run the consolidation agent
        start_time = time.time()
        _run_consolidation_agent(user_id, log_prefix)
        elapsed_ms = int((time.time() - start_time) * 1000)

        _mark_completed(org_id)
        logger.info("%s Consolidation completed in %dms", log_prefix, elapsed_ms)
        return {"status": "ok", "elapsed_ms": elapsed_ms}

    except Exception as e:
        logger.exception("%s Consolidation failed", log_prefix)
        return {"status": "error", "error": str(e)}


@celery_app.task(
    name="services.memory.consolidation.schedule_memory_consolidation",
    soft_time_limit=60,
    time_limit=90,
)
def schedule_memory_consolidation():
    """Celery Beat entry point: find orgs that need consolidation and enqueue them."""
    log_prefix = "[MemoryDream:Scheduler]"

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """SELECT DISTINCT a.org_id, u.id as user_id
                       FROM artifacts a
                       JOIN users u ON u.org_id = a.org_id
                       WHERE a.category = ANY(%s)
                       GROUP BY a.org_id, u.id
                       HAVING COUNT(*) >= %s""",
                    (list(MEMORY_CATEGORIES), MIN_ENTRIES_TO_CONSOLIDATE),
                )
                org_rows = cursor.fetchall()

        if not org_rows:
            logger.info("%s No orgs with enough memories to consolidate", log_prefix)
            return {"status": "ok", "orgs_scheduled": 0}

        # Deduplicate orgs (pick first user per org)
        org_map: Dict[str, str] = {}
        for org_id, uid in org_rows:
            if org_id not in org_map:
                org_map[org_id] = uid

        scheduled = 0
        for org_id, uid in org_map.items():
            # Time gate — don't re-run if we already ran recently
            if _should_run(org_id):
                run_memory_consolidation.delay(org_id=org_id, user_id=uid)
                scheduled += 1

        logger.info("%s Scheduled %d/%d orgs for consolidation", log_prefix, scheduled, len(org_map))
        return {"status": "ok", "orgs_scheduled": scheduled}

    except Exception as e:
        logger.exception("%s Scheduling failed", log_prefix)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


def _run_consolidation_agent(user_id: str, log_prefix: str):
    """Multi-turn agent that reviews and cleans up org memory."""
    tools = build_memory_tools(
        user_id,
        include_append=False,
        include_edit=True,
        include_delete=True,
    )

    llm = create_chat_model(ModelConfig.MAIN_MODEL, temperature=0.1, streaming=False)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    system_prompt = _AGENT_SYSTEM.format(max_turns=MAX_AGENT_TURNS, today=today)

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    agent.invoke(
        {"messages": [HumanMessage(content="Review and consolidate the org memory bank.")]},
        config={"recursion_limit": MAX_AGENT_TURNS * 2 + 2},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _count_memories(org_id: str, user_id: str) -> int:
    """Quick count of memory entries for this org."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[MemoryDream:count]")
                cursor.execute(
                    """SELECT COUNT(*) FROM artifacts
                       WHERE org_id = %s AND category = ANY(%s)""",
                    (org_id, list(MEMORY_CATEGORIES)),
                )
                return cursor.fetchone()[0]
    except Exception:
        return 0


def _should_run(org_id: str) -> bool:
    """Check if enough time has elapsed since the last consolidation."""
    try:
        r = get_redis_client()
        key = _REDIS_LAST_RUN_KEY.format(org_id=org_id)
        last_run = r.get(key)
        if not last_run:
            return True
        hours_elapsed = (time.time() - int(last_run)) / 3600
        return hours_elapsed >= _MIN_HOURS_BETWEEN_RUNS
    except Exception:
        return True


def _mark_completed(org_id: str):
    """Record that consolidation ran for this org."""
    try:
        r = get_redis_client()
        key = _REDIS_LAST_RUN_KEY.format(org_id=org_id)
        r.setex(key, _MIN_HOURS_BETWEEN_RUNS * 3600 * 2, str(int(time.time())))
    except Exception:
        pass
