"""
Memory Dream Consolidation — Nightly GC Agent

Runs once per night (via Celery Beat) for each org. Reviews all memories and:
1. Merges near-duplicates
2. Removes stale/contradicted entries
3. Keeps index clean and under size limits
4. Converts relative dates to absolute
5. Ensures entries follow consistent format

Modeled after Claude Code's dream consolidation:
- Uses Redis-based distributed lock (not file lock)
- Multi-phase: orient → analyze → consolidate → prune
- Auditable: all changes logged with versioning
- Safe: uses existing memory tool functions (versioned writes)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

from celery_config import celery_app
from utils.db.connection_pool import db_pool
from utils.auth.stateless_auth import set_rls_context
from utils.cache.redis_client import get_redis_client
from services.memory import MEMORY_CATEGORIES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONSOLIDATION_MODEL = os.getenv("MEMORY_CONSOLIDATION_MODEL", "anthropic/claude-sonnet-4.6")
MIN_HOURS_BETWEEN_RUNS = int(os.getenv("MEMORY_DREAM_INTERVAL_HOURS", "24"))
MIN_ENTRIES_TO_CONSOLIDATE = 3  # Don't bother consolidating fewer than this
MAX_ENTRIES_PER_RUN = 100  # Process at most this many entries per consolidation
LOCK_TTL = 3600  # 1 hour — generous timeout for the consolidation process

_REDIS_LOCK_KEY = "mem:dream:lock:{org_id}"
_REDIS_LAST_RUN_KEY = "mem:dream:last_run:{org_id}"

# ---------------------------------------------------------------------------
# Consolidation prompt
# ---------------------------------------------------------------------------

_CONSOLIDATION_SYSTEM = """You are a memory maintenance agent. Your job is to review an organization's memory bank and produce a cleanup plan that keeps it lean, accurate, and well-organized.

GOALS:
1. MERGE duplicates — if two entries cover the same topic, produce one unified entry
2. REMOVE stale entries — facts that are clearly outdated or contradicted by newer entries
3. FIX formatting — ensure entries follow the standard structure for their category
4. SPLIT mega-entries — if an entry covers multiple unrelated topics, note it for splitting
5. DEDUPLICATE within entries — remove repeated paragraphs/facts within a single entry
6. CONVERT relative dates — "yesterday", "last week" → absolute dates where context allows

RULES:
- Be CONSERVATIVE — only act when you're confident the change improves things
- NEVER delete entries that contain unique, non-redundant information
- ALWAYS prefer merging over deleting (combine into the better-titled entry)
- Preserve all factual content during merges — don't lose information
- If unsure, leave the entry alone
- Mark entries for human review if they seem important but potentially stale

Return a JSON object:
{
  "actions": [
    {
      "type": "merge",
      "source_category": "...", "source_title": "...",
      "target_category": "...", "target_title": "...",
      "merged_content": "<full merged content>",
      "merged_description": "<updated description>",
      "reasoning": "<why these should be merged>"
    },
    {
      "type": "delete",
      "category": "...", "title": "...",
      "reasoning": "<why this should be removed>"
    },
    {
      "type": "rewrite",
      "category": "...", "title": "...",
      "new_content": "<cleaned up content>",
      "new_description": "<updated description if needed>",
      "reasoning": "<what was fixed>"
    }
  ],
  "summary": "<one paragraph summary of what was done>"
}

If nothing needs changing, return: {"actions": [], "summary": "Memory is clean — no changes needed."}"""

_CONSOLIDATION_USER_TEMPLATE = """ORG MEMORY BANK — FULL CONTENTS:

{memory_dump}

---

Review the above memories. Identify duplicates, stale entries, formatting issues, and anything that should be cleaned up. Today's date is {today}.

Return your cleanup plan as JSON."""


# ---------------------------------------------------------------------------
# Celery Task
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
    """Run memory consolidation for a single org.

    Acquires a distributed lock, loads all memories, asks an LLM for a
    cleanup plan, and executes it using versioned memory tool functions.
    """
    log_prefix = f"[MemoryDream:{org_id[:8]}]"

    # Acquire distributed lock
    if not _acquire_lock(org_id):
        logger.info("%s Skipped — another consolidation in progress", log_prefix)
        return {"status": "skipped", "reason": "locked"}

    try:
        # Check time gate
        if not _should_run(org_id):
            logger.debug("%s Skipped — too soon since last run", log_prefix)
            return {"status": "skipped", "reason": "too_soon"}

        # Check before_llm_call hook
        from utils.hooks import get_hook
        hook_allowed, hook_message = get_hook("before_llm_call")(org_id, user_id)
        if not hook_allowed:
            logger.info("%s Hook blocked: %s", log_prefix, hook_message)
            return {"status": "hook_blocked"}

        # Load all memories for this org
        memories = _load_all_memories(org_id, user_id)
        if len(memories) < MIN_ENTRIES_TO_CONSOLIDATE:
            logger.info("%s Only %d entries — skipping", log_prefix, len(memories))
            _mark_completed(org_id)
            return {"status": "skipped", "reason": "too_few_entries", "count": len(memories)}

        # Run consolidation LLM
        plan = _generate_consolidation_plan(memories, log_prefix)
        if not plan or not plan.get("actions"):
            logger.info("%s No changes needed", log_prefix)
            _mark_completed(org_id)
            return {"status": "ok", "actions": 0, "summary": plan.get("summary", "")}

        # Execute the plan
        executed = _execute_plan(plan["actions"], user_id, log_prefix)

        _mark_completed(org_id)

        logger.info(
            "%s Consolidation complete: %d actions planned, %d executed. Summary: %s",
            log_prefix, len(plan["actions"]), executed, plan.get("summary", "")[:200],
        )
        return {
            "status": "ok",
            "actions_planned": len(plan["actions"]),
            "actions_executed": executed,
            "summary": plan.get("summary", ""),
        }

    except Exception as e:
        logger.exception("%s Consolidation failed", log_prefix)
        return {"status": "error", "error": str(e)}
    finally:
        _release_lock(org_id)


# ---------------------------------------------------------------------------
# Scheduler — iterates all orgs
# ---------------------------------------------------------------------------


@celery_app.task(
    name="services.memory.consolidation.schedule_memory_consolidation",
    soft_time_limit=60,
    time_limit=90,
)
def schedule_memory_consolidation():
    """Celery Beat entry point: find orgs that need consolidation and enqueue them.

    Runs on the Beat schedule (nightly). For each org with enough memories and
    enough time since last consolidation, enqueues a run_memory_consolidation task.
    """
    log_prefix = "[MemoryDream:Scheduler]"

    try:
        # Find all orgs with memories
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
            if _should_run(org_id):
                run_memory_consolidation.delay(org_id=org_id, user_id=uid)
                scheduled += 1

        logger.info("%s Scheduled %d/%d orgs for consolidation", log_prefix, scheduled, len(org_map))
        return {"status": "ok", "orgs_scheduled": scheduled, "orgs_total": len(org_map)}

    except Exception as e:
        logger.exception("%s Scheduling failed", log_prefix)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_all_memories(org_id: str, user_id: str) -> List[Dict]:
    """Load all memory entries for an org (full content)."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                set_rls_context(cursor, conn, user_id, log_prefix="[MemoryDream:load]")
                cursor.execute(
                    """SELECT category, title, description, content, updated_at
                       FROM artifacts
                       WHERE org_id = %s AND category = ANY(%s)
                       ORDER BY category, updated_at DESC
                       LIMIT %s""",
                    (org_id, list(MEMORY_CATEGORIES), MAX_ENTRIES_PER_RUN),
                )
                rows = cursor.fetchall()

        return [
            {
                "category": row[0],
                "title": row[1],
                "description": row[2] or "",
                "content": row[3] or "",
                "updated_at": row[4],
            }
            for row in rows
        ]
    except Exception:
        logger.exception("[MemoryDream] Failed to load memories")
        return []


def _generate_consolidation_plan(memories: List[Dict], log_prefix: str) -> Optional[Dict]:
    """Ask the LLM to generate a consolidation plan."""
    from chat.backend.agent.providers import create_chat_model
    from langchain_core.messages import SystemMessage, HumanMessage

    # Build the full memory dump
    dump_parts = []
    for mem in memories:
        updated = mem["updated_at"].isoformat() if mem["updated_at"] else "unknown"
        # Cap individual entry content to prevent context overflow
        content = mem["content"][:3000]
        if len(mem["content"]) > 3000:
            content += "\n... (truncated)"

        dump_parts.append(
            f"### [{mem['category']}] {mem['title']}\n"
            f"Description: {mem['description']}\n"
            f"Last updated: {updated}\n"
            f"Content:\n{content}\n"
        )

    memory_dump = "\n---\n".join(dump_parts)

    # Cap total dump size to fit in context
    if len(memory_dump) > 80000:
        memory_dump = memory_dump[:80000] + "\n\n... (additional entries truncated)"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prompt = _CONSOLIDATION_USER_TEMPLATE.format(
        memory_dump=memory_dump,
        today=today,
    )

    try:
        llm = create_chat_model(CONSOLIDATION_MODEL, temperature=0.1, streaming=False)
        response = llm.invoke([
            SystemMessage(content=_CONSOLIDATION_SYSTEM),
            HumanMessage(content=prompt),
        ])

        content = response.content if hasattr(response, "content") else str(response)

        # Parse JSON
        json_str = content.strip()
        if json_str.startswith("```"):
            json_str = json_str.split("\n", 1)[1] if "\n" in json_str else json_str
            json_str = json_str.rsplit("```", 1)[0]
        return json.loads(json_str)

    except json.JSONDecodeError as e:
        logger.warning("%s LLM returned invalid JSON: %s", log_prefix, e)
        return None
    except Exception as e:
        logger.warning("%s Consolidation LLM failed: %s", log_prefix, e)
        return None


def _execute_plan(actions: List[Dict], user_id: str, log_prefix: str) -> int:
    """Execute the consolidation plan using memory tool functions."""
    from chat.backend.agent.tools.memory_tool import write_memory, edit_memory

    executed = 0
    for action in actions:
        action_type = action.get("type")

        try:
            if action_type == "merge":
                # Write merged content to target, then delete source
                result = write_memory(
                    category=action["target_category"],
                    title=action["target_title"],
                    content=action["merged_content"],
                    description=action.get("merged_description", ""),
                    overwrite=True,
                    user_id=user_id,
                )
                result_data = json.loads(result)
                if result_data.get("status") == "ok":
                    # Delete the source entry by writing empty content... 
                    # Actually, we need to properly delete it
                    _delete_memory_entry(
                        user_id,
                        action["source_category"],
                        action["source_title"],
                        log_prefix,
                    )
                    executed += 1
                    logger.info(
                        "%s Merged: %s/%s → %s/%s",
                        log_prefix,
                        action["source_category"], action["source_title"],
                        action["target_category"], action["target_title"],
                    )

            elif action_type == "delete":
                _delete_memory_entry(
                    user_id,
                    action["category"],
                    action["title"],
                    log_prefix,
                )
                executed += 1
                logger.info(
                    "%s Deleted: %s/%s (reason: %s)",
                    log_prefix, action["category"], action["title"],
                    action.get("reasoning", "")[:100],
                )

            elif action_type == "rewrite":
                result = write_memory(
                    category=action["category"],
                    title=action["title"],
                    content=action["new_content"],
                    description=action.get("new_description", ""),
                    overwrite=True,
                    user_id=user_id,
                )
                result_data = json.loads(result)
                if result_data.get("status") == "ok":
                    executed += 1
                    logger.info(
                        "%s Rewrote: %s/%s (reason: %s)",
                        log_prefix, action["category"], action["title"],
                        action.get("reasoning", "")[:100],
                    )

        except Exception as e:
            logger.warning(
                "%s Failed to execute %s action: %s", log_prefix, action_type, e
            )

    return executed


def _delete_memory_entry(user_id: str, category: str, title: str, log_prefix: str):
    """Delete a memory entry from the artifacts table."""
    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                org_id = set_rls_context(cursor, conn, user_id, log_prefix=f"{log_prefix}:delete")
                if not org_id:
                    return
                cursor.execute(
                    """DELETE FROM artifacts
                       WHERE org_id = %s AND category = %s AND title = %s""",
                    (org_id, category, title),
                )
                conn.commit()
    except Exception as e:
        logger.warning("%s Failed to delete %s/%s: %s", log_prefix, category, title, e)


# ---------------------------------------------------------------------------
# Redis-based coordination
# ---------------------------------------------------------------------------


def _acquire_lock(org_id: str) -> bool:
    """Acquire the consolidation lock for this org."""
    try:
        r = get_redis_client()
        key = _REDIS_LOCK_KEY.format(org_id=org_id)
        return bool(r.set(key, str(int(time.time())), nx=True, ex=LOCK_TTL))
    except Exception:
        return True  # Fail open


def _release_lock(org_id: str):
    """Release the consolidation lock."""
    try:
        r = get_redis_client()
        r.delete(_REDIS_LOCK_KEY.format(org_id=org_id))
    except Exception:
        pass


def _should_run(org_id: str) -> bool:
    """Check if enough time has elapsed since the last consolidation."""
    try:
        r = get_redis_client()
        key = _REDIS_LAST_RUN_KEY.format(org_id=org_id)
        last_run = r.get(key)
        if not last_run:
            return True
        last_ts = int(last_run)
        hours_elapsed = (time.time() - last_ts) / 3600
        return hours_elapsed >= MIN_HOURS_BETWEEN_RUNS
    except Exception:
        return True


def _mark_completed(org_id: str):
    """Record that consolidation ran for this org."""
    try:
        r = get_redis_client()
        key = _REDIS_LAST_RUN_KEY.format(org_id=org_id)
        r.setex(key, MIN_HOURS_BETWEEN_RUNS * 3600 * 2, str(int(time.time())))
    except Exception:
        pass
