"""
Memory Extraction Agent — Background Celery Task

Triggered after each turn (interactive or RCA) to analyze the conversation
and extract durable learnings into org memory. Modeled after Claude Code's
extraction agent:

- Fires post-turn (non-blocking, via Celery)
- Full agentic loop: the LLM can browse, read, write, and append memories
- Dedup is natural: the agent reads existing content before deciding to act
- DB-level safety: FOR UPDATE on appends, ON CONFLICT on writes — no app-level lock needed
- Limited turn budget with efficiency instructions

Key differences from Claude Code:
- Uses Celery instead of forked process (no prompt cache sharing)
- DB-backed memory instead of filesystem
- RLS context required for all DB operations
"""

import logging
import time
from typing import List

from langchain_core.messages import HumanMessage
from langchain.agents import create_agent

from celery_config import celery_app
from chat.backend.agent.llm import ModelConfig
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.tools.memory_tool import build_memory_tools
from chat.backend.agent.utils.llm_context_manager import LLMContextManager
from chat.backend.agent.utils.chat_context_manager import ChatContextManager
from services.memory.index_builder import build_memory_index
from utils.auth.stateless_auth import get_org_id_for_user
from utils.hooks import get_hook

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_AGENT_TURNS = 30

# ---------------------------------------------------------------------------
# Agent system prompt
# ---------------------------------------------------------------------------

_AGENT_SYSTEM = """You are a memory extraction agent. You run in the background after conversations to save durable organizational knowledge.

You have tools to browse and manage org memory. Your job:
1. Analyze the conversation for facts worth remembering long-term
2. Use list_memories and read_memory to check what already exists
3. Write new entries or append to existing ones — only if genuinely new information

WHAT TO SAVE:
- Facts the USER states about their organization (team structure, escalation policies, people/roles, preferences)
- Infrastructure details mentioned by the user or discovered during investigation
- Resolution steps and root causes from incidents/debugging
- Procedures, runbooks, or standard operating procedures mentioned
- Upcoming events, releases, deadlines, or planned changes the user mentions
- Decisions made during the conversation (e.g. "we'll merge X", "we're switching to Y")

CATEGORIES:
- context: Team preferences, escalation paths, org policies, people & roles, behavioral instructions, upcoming events/releases
- infrastructure: Service topology, deployment chains, monitoring configs, dependencies
- runbook: Step-by-step procedures for known issues
- learned: Non-obvious root causes, debugging patterns, resolution steps
- postmortem: Incident postmortem summaries and key takeaways

RULES:
1. Only save facts useful in FUTURE conversations
2. DO NOT save debugging artifacts (specific log lines, one-off metric values, transient error messages)
3. DO NOT save obvious/generic knowledge
4. Prefer appending to existing entries over creating new ones
5. If nothing is worth remembering, just respond "DONE: nothing to extract"
6. PAY SPECIAL ATTENTION to facts the user casually mentions (e.g. "our VP is X", "we use Y for Z") — Any context that could be useful in the future should be saved.

If the conversation has nothing worth saving, just respond "DONE: nothing to extract" immediately without using any tools."""


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

        # Build memory index upfront so the agent has a map without wasting a turn
        memory_index = build_memory_index(user_id) or "(no existing memories)"

        # Run the extraction agent
        start_time = time.time()
        _run_extraction_agent(messages, memory_index, user_id, session_id, log_prefix)
        elapsed_ms = int((time.time() - start_time) * 1000)

        logger.info("%s Extraction completed in %dms", log_prefix, elapsed_ms)
        return {"status": "ok", "elapsed_ms": elapsed_ms}

    except Exception as e:
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
):
    """Multi-turn agent loop: browses existing memories, writes new ones."""
    tools = build_memory_tools(user_id, session_id=session_id)

    llm = create_chat_model(ModelConfig.MAIN_MODEL, temperature=0.0, streaming=False)

    system_prompt = _AGENT_SYSTEM.format(max_turns=MAX_AGENT_TURNS)

    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=system_prompt,
    )

    # Format the conversation for context
    conversation_text = ChatContextManager._format_messages_for_summary(conversation_messages)
    if len(conversation_text) > 20000:
        conversation_text = conversation_text[-20000:]

    user_content = f"EXISTING MEMORY INDEX:\n{memory_index}\n\nCONVERSATION TO ANALYZE:\n\n{conversation_text}"

    agent.invoke(
        {"messages": [HumanMessage(content=user_content)]},
        config={"recursion_limit": MAX_AGENT_TURNS * 2 + 2},
    )

