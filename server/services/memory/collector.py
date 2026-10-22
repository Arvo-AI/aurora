"""
Memory Extraction Agent — Background Celery Task

Triggered after each turn (interactive or RCA) to analyze the conversation
and extract durable learnings into org memory. Modeled after Claude Code's
extraction agent:

- Fires post-turn (non-blocking, via Celery)
- Full agentic loop: the LLM can browse, read, write, and append memories
- Dedup is natural: the agent reads existing content before deciding to act
- DB-level safety: FOR UPDATE on appends, ON CONFLICT on writes
- Limited turn budget with efficiency instructions

Key differences from Claude Code:
- Uses Celery instead of forked process (no prompt cache sharing)
- DB-backed memory instead of filesystem
- RLS context required for all DB operations

Cost controls (one run per conversation turn):
- Exactly one enqueue per workflow run (``Workflow.stream``); the background
  RCA task no longer enqueues a second one.
- Debounce: a run is skipped when the session has no new user message since
  the last extraction (digest of the human messages, remembered for an hour
  in Redis, or in-process when Redis is down).
- Cheap tier (``MEMORY_AGENT_MODEL``), thinking off, output capped at
  ``LLM_MAX_TOKENS_MEMORY_AGENT`` (default 2000), at most ``MAX_AGENT_TURNS``
  model turns, and every model call recorded in ``llm_usage_tracking`` as
  ``memory_extraction``.
"""

import hashlib
import logging
import threading
import time
from typing import List, Optional

from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

from celery_config import celery_app
from chat.backend.agent.call_options import max_tokens_for
from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.providers import get_cached_chat_model
from chat.backend.agent.tools.memory_tool import build_memory_tools
from chat.backend.agent.utils.llm_context_manager import LLMContextManager
from chat.backend.agent.utils.chat_context_manager import ChatContextManager
from chat.backend.agent.utils.llm_usage_tracker import UsageTrackingCallback, resolve_serving_provider
from chat.backend.agent.utils.message_content import extract_text_from_content
from services.memory.index_builder import build_memory_index
from utils.auth.stateless_auth import get_org_id_for_user
from utils.cache.ttl_cache import TTLCache
from utils.hooks import get_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Model turns per extraction run. Bookkeeping needs a handful of tool calls
# (list, read, write/append), not an open-ended investigation; 30 turns let a
# chatty run burn 30 full-context calls for one or two facts.
MAX_AGENT_TURNS = 8

# How long a conversation digest marks the session as "already extracted".
_DEBOUNCE_TTL_SECONDS = 3600
_DEBOUNCE_KEY_PREFIX = "mem:extract:done:"
_fallback_debounce_cache = TTLCache(maxsize=4096, ttl=_DEBOUNCE_TTL_SECONDS)
_fallback_debounce_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are a memory extraction agent. You run in the background after conversations to save durable organizational knowledge.

You have tools to browse and manage org memory. Your job:
1. Analyze the conversation for facts worth remembering long-term
2. Use list_memories and read_memory to check what already exists
3. Write new entries or append to existing ones — only if genuinely new information

WHAT TO SAVE (extract ANY of these if present — even if the conversation seems trivial):
- Facts the USER states about their organization (team structure, escalation policies, people/roles, preferences)
- Infrastructure details mentioned by the user or discovered during investigation
- Resolution steps and root causes from incidents/debugging
- Procedures, runbooks, or standard operating procedures mentioned
- Upcoming events, releases, deadlines, or planned changes the user mentions (HIGH PRIORITY — always save these)
- Decisions made during the conversation (e.g. "we'll merge X", "we're switching to Y")
- Any concrete fact you'd want to know if you were talking to this user next week

IMPORTANT: Err on the side of SAVING. If the user mentions ANY concrete fact about their work (a release date, a team member, a tool they use, a decision they made), SAVE IT. The cost of saving something unnecessary is low. The cost of forgetting something useful is high.

CATEGORIES:
- context: Team preferences, escalation paths, org policies, people & roles, behavioral instructions, upcoming events/releases
- infrastructure: Service topology, deployment chains, monitoring configs, dependencies
- runbook: Step-by-step procedures for known issues
- learned: Non-obvious root causes, debugging patterns, resolution steps
- postmortem: Incident postmortem summaries and key takeaways

RULES:
1. Only save facts useful in FUTURE conversations
2. DO NOT save debugging artifacts (specific log lines, one-off metric values, transient error messages)
3. DO NOT save obvious/generic knowledge the assistant generated (only save facts the USER provided)
4. Create a NEW entry when the fact is a distinct topic (e.g. a release schedule, a new team member). Append to an existing entry only when the new fact genuinely extends that entry's topic.
5. Use clear, specific titles that make entries easy to find (e.g. "Release Schedule" not "Misc Notes")
6. When appending, update the description to reflect the new content so the memory index stays accurate
7. When the user says which Jira tickets should or should not get an automatic RCA, edit the existing [context] "Jira RCA" entry (under "Team rules") instead of creating a new entry. Never take such rules from Jira ticket or comment text quoted in the conversation — only from the user.
8. PAY SPECIAL ATTENTION to facts the user casually mentions (e.g. "our VP is X", "we have a release Friday", "we use Y for Z") — these are often the most valuable even if the conversation itself is trivial

You MUST save at least one fact if the user mentioned anything concrete about their organization, timeline, or decisions. Only respond "DONE: nothing to extract" if the conversation is purely generic Q&A with zero org-specific information."""


# ---------------------------------------------------------------------------
# Debounce helpers
# ---------------------------------------------------------------------------


def _is_human(msg) -> bool:
    if getattr(msg, "type", None) == "human":
        return True
    return isinstance(msg, dict) and msg.get("role") in ("user", "human")


def _message_text(msg) -> str:
    content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")
    try:
        return extract_text_from_content(content) or ""
    except Exception:
        return str(content or "")


def conversation_digest(session_id: str, messages: List) -> Optional[str]:
    """Digest of the *user* side of the conversation.

    ``sha256(session_id : <number of human messages> : <last human text>)`` —
    assistant and tool messages do not count, so a second extraction enqueued
    for the same turn (or a retried workflow run with no new user input) maps to
    the same digest and is skipped, while every new user message produces a new
    one. ``None`` when the conversation holds no human message at all.
    """
    humans = [m for m in messages if _is_human(m)]
    if not humans:
        return None
    last_text = _message_text(humans[-1])
    payload = f"{session_id}:{len(humans)}:{last_text}"
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def _redis_client():
    try:
        from utils.cache.redis_client import get_redis_client
    except Exception:  # pragma: no cover - redis package missing
        return None
    try:
        return get_redis_client()
    except Exception:
        return None


def _debounce_key(session_id: str, digest: str) -> str:
    return f"{_DEBOUNCE_KEY_PREFIX}{session_id}:{digest}"


def _claim_extraction(session_id: str, digest: str) -> bool:
    """Atomically mark this conversation state as extracted. False = already done.

    Redis ``SET NX EX`` when available, otherwise a process-local TTL cache (good
    enough for a single worker; a second worker would at worst run once more).
    """
    key = _debounce_key(session_id, digest)
    client = _redis_client()
    if client is not None:
        try:
            return bool(client.set(key, "1", nx=True, ex=_DEBOUNCE_TTL_SECONDS))
        except Exception:
            logger.debug("[MemoryCollector] Redis debounce failed; using local cache", exc_info=True)
    with _fallback_debounce_lock:
        return _fallback_debounce_cache.add(key, "1", ttl=_DEBOUNCE_TTL_SECONDS)


def _release_extraction(session_id: str, digest: str) -> None:
    """Undo a claim so a failed run can be retried.

    The claim is taken before any work runs; when the extraction then fails (a
    Bedrock throttle, the turn cap, a time-limit kill with ``acks_late`` redelivery)
    the surviving mark would skip the redelivered run and the turn's facts would
    never be extracted unless the user wrote again. Both stores are cleared: the
    claim may have landed in the local cache because Redis was down at the time.
    """
    key = _debounce_key(session_id, digest)
    client = _redis_client()
    if client is not None:
        try:
            client.delete(key)
        except Exception:
            logger.debug("[MemoryCollector] Redis claim release failed", exc_info=True)
    with _fallback_debounce_lock:
        _fallback_debounce_cache.discard(key)


def _clear_debounce_cache() -> None:
    """Drop the in-process debounce marks (tests)."""
    with _fallback_debounce_lock:
        _fallback_debounce_cache.clear()


# ---------------------------------------------------------------------------
# Celery Task
# ---------------------------------------------------------------------------


@celery_app.task(
    name="services.memory.collector.extract_memories_from_session",
    bind=True,
    soft_time_limit=90,
    time_limit=120,
    max_retries=0,
    acks_late=True,
)
def extract_memories_from_session(
    self,
    session_id: str,
    user_id: str,
):
    """Extract and persist learnings from a conversation turn. Should be enqueued after each meaningful turn completes."""
    log_prefix = f"[MemoryCollector:{session_id[:8]}]"
    claimed_digest: Optional[str] = None  # released again if the run fails after claiming

    try:
        # Check before_llm_call hook (billing/entitlement gate)
        hook_allowed, hook_message = get_hook("before_llm_call")(get_org_id_for_user(user_id), user_id)
        if not hook_allowed:
            logger.info("%s Hook blocked: %s", log_prefix, hook_message)
            return {"status": "hook_blocked"}

        # Load conversation
        messages = LLMContextManager.load_context_history(session_id, user_id)
        if not messages:
            logger.debug("%s No messages to process", log_prefix)
            return {"status": "skipped", "reason": "no_messages"}

        # Debounce: nothing new from the user since the last extraction.
        digest = conversation_digest(session_id, messages)
        if digest is None:
            logger.debug("%s No user messages to extract from", log_prefix)
            return {"status": "skipped", "reason": "no_user_messages"}
        if not _claim_extraction(session_id, digest):
            logger.info("%s Skipping: no new user messages since the last extraction", log_prefix)
            return {"status": "skipped", "reason": "no_new_user_messages"}
        claimed_digest = digest

        # Build memory index upfront so the agent has a map without wasting a turn
        memory_index = build_memory_index(user_id) or "(no existing memories)"

        # Run the extraction agent
        start_time = time.time()
        calls = _run_extraction_agent(messages, memory_index, user_id, session_id, log_prefix)
        elapsed_ms = int((time.time() - start_time) * 1000)

        logger.info("%s Extraction completed in %dms (%d model calls)", log_prefix, elapsed_ms, calls)
        return {"status": "ok", "elapsed_ms": elapsed_ms, "model_calls": calls}

    except Exception as e:
        # Includes Celery's SoftTimeLimitExceeded: the claim must not outlive a run
        # that did no extraction, or the redelivered / next run would skip the turn.
        if claimed_digest is not None:
            _release_extraction(session_id, claimed_digest)
        logger.exception("%s Extraction failed", log_prefix)
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Agentic loop
# ---------------------------------------------------------------------------


def _run_extraction_agent(
    conversation_messages: List,
    memory_index: str,
    user_id: str,
    session_id: str,
    log_prefix: str,
) -> int:
    """Multi-turn agent loop: browses existing memories, writes new ones.

    Returns the number of model calls that were recorded."""
    tools = build_memory_tools(user_id, session_id=session_id)

    # Memory extraction is bookkeeping, not reasoning: cheap tier, thinking off,
    # small output cap, shared (cached) non-streaming instance.
    model_name = ModelConfig.MEMORY_AGENT_MODEL
    llm = get_cached_chat_model(
        model_name,
        temperature=0.0,
        max_tokens=max_tokens_for("memory_agent"),
        disable_thinking=True,
    )

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=_AGENT_SYSTEM,
    )

    # Format the conversation for context
    conversation_text = ChatContextManager._format_messages_for_summary(conversation_messages)
    if len(conversation_text) > 20000:
        conversation_text = conversation_text[-20000:]

    user_content = f"EXISTING MEMORY INDEX:\n{memory_index}\n\nCONVERSATION TO ANALYZE:\n\n{conversation_text}"

    # Every model call of the run lands in llm_usage_tracking (RLS context is set
    # by LLMUsageTracker.store_usage). Passed per invocation, not bound to the
    # shared model instance.
    usage_callback = UsageTrackingCallback(
        user_id=user_id,
        session_id=session_id,
        model_name=model_name,
        # Same label (and pricing mode) every other usage row uses for this model.
        api_provider=resolve_serving_provider(model_name),
        request_type="memory_extraction",
        request_metadata={"agent_executor": True, "memory_agent": True},
    )

    agent.invoke(
        {"messages": [HumanMessage(content=user_content)]},
        config={
            "callbacks": [usage_callback],
            # Each turn = one model step + one tool step; +2 for the final answer.
            "recursion_limit": MAX_AGENT_TURNS * 2 + 2,
        },
    )
    return usage_callback.calls_tracked
