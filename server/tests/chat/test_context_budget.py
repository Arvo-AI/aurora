"""Background RCA context budget (``RCA_CONTEXT_TOKEN_BUDGET``) in ContextSafetyMiddleware.

The middleware module is loaded by path with its ``chat.background`` /
``utils.cloud.cloud_utils`` imports stubbed: importing it through the package would
run ``chat.background.__init__`` (Redis at import time). It needs the real
``langchain.agents.middleware``; in environments where langchain is stubbed the
middleware tests skip and only the pure budget parser runs.
"""
import asyncio
import importlib
import importlib.util
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_CONTEXT_TRIM_PATH = os.path.join(
    _SERVER_DIR, "chat", "backend", "agent", "middleware", "context_trim.py"
)


def _real_module(name: str):
    module = importlib.import_module(name)
    if not getattr(module, "__file__", None):
        pytest.skip(f"{name} is stubbed in this environment")
    return module


@pytest.fixture
def context_trim(monkeypatch):
    """Load ``context_trim.py`` with its background / cloud-context imports stubbed."""
    _real_module("langchain.agents.middleware.types")

    background_pkg = types.ModuleType("chat.background")
    background_pkg.__path__ = []
    context_updates = types.ModuleType("chat.background.context_updates")
    context_updates.apply_rca_context_updates = MagicMock(return_value=None)
    cloud_utils = types.ModuleType("utils.cloud.cloud_utils")
    cloud_utils.get_state_context = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "chat.background", background_pkg)
    monkeypatch.setitem(sys.modules, "chat.background.context_updates", context_updates)
    monkeypatch.setitem(sys.modules, "utils.cloud.cloud_utils", cloud_utils)

    with open(_CONTEXT_TRIM_PATH, encoding="utf-8") as fh:
        source = fh.read()
    source = source.replace(
        "from ..utils.chat_context_manager import ChatContextManager",
        "from chat.backend.agent.utils.chat_context_manager import ChatContextManager",
    )
    spec = importlib.util.spec_from_loader("_context_trim_under_test", loader=None)
    module = importlib.util.module_from_spec(spec)
    exec(compile(source, _CONTEXT_TRIM_PATH, "exec"), module.__dict__)
    return module


class TestBudgetParser:
    def test_default_is_200k(self, context_trim, monkeypatch):
        monkeypatch.delenv("RCA_CONTEXT_TOKEN_BUDGET", raising=False)
        assert context_trim.rca_context_token_budget() == 200_000

    @pytest.mark.parametrize("raw", ["", "  ", "lots", "0", "-100"])
    def test_invalid_values_mean_the_default(self, context_trim, monkeypatch, raw):
        monkeypatch.setenv("RCA_CONTEXT_TOKEN_BUDGET", raw)
        assert context_trim.rca_context_token_budget() == 200_000

    def test_override_is_honoured(self, context_trim, monkeypatch):
        monkeypatch.setenv("RCA_CONTEXT_TOKEN_BUDGET", "100000")
        assert context_trim.rca_context_token_budget() == 100_000


class TestMiddlewareBudget:
    """Interactive chat keeps 80% of the window; background runs take min(window, budget)."""

    MODEL = "anthropic/claude-haiku-5.5"  # 950K context limit -> 760K safety net

    def test_interactive_keeps_the_window_rule(self, context_trim):
        mw = context_trim.ContextSafetyMiddleware(self.MODEL)
        assert mw.max_tokens == int(950_000 * 0.80)

    def test_background_budget_caps_below_the_window(self, context_trim):
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=200_000)
        assert mw.max_tokens == 200_000

    def test_budget_never_raises_the_limit_above_the_window(self, context_trim):
        small_window_model = "anthropic/claude-3-haiku"  # 180K -> 144K safety net
        mw = context_trim.ContextSafetyMiddleware(small_window_model, token_budget=200_000)
        assert mw.max_tokens == int(180_000 * 0.80)

    @pytest.mark.parametrize("budget", [None, 0, -1])
    def test_missing_or_non_positive_budget_is_ignored(self, context_trim, budget):
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=budget)
        assert mw.max_tokens == int(950_000 * 0.80)


# ---------------------------------------------------------------------------
# Trimming an RCA-shaped history past the budget
# ---------------------------------------------------------------------------


class _FakeRequest:
    """The two members of ``ModelRequest`` the middleware touches."""

    def __init__(self, messages):
        self.messages = list(messages)

    def override(self, **changes):
        return _FakeRequest(changes.get("messages", self.messages))


def _rca_history(pairs: int, tool_chars: int = 2_000, first_pair: int = 0):
    """``HumanMessage`` task followed by ``pairs`` AI tool-call + ToolMessage pairs.

    Ids are set the way the LangGraph ``add_messages`` reducer does at runtime; the
    middleware pins its cut by id.
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    messages = [HumanMessage("ALERT: checkout-api p99 latency above 2s in eu-central-1", id="task")]
    for i in range(first_pair, first_pair + pairs):
        messages.append(
            AIMessage(
                "",
                tool_calls=[{"id": f"tc{i}", "name": "kubectl", "args": {"cmd": f"get pods --page {i}"}}],
                id=f"ai{i}",
            )
        )
        messages.append(ToolMessage("x" * tool_chars, tool_call_id=f"tc{i}", id=f"tool{i}"))
    return messages


def _orphan_tool_messages(messages):
    from langchain_core.messages import AIMessage, ToolMessage

    seen_tool_calls = set()
    orphans = []
    for msg in messages:
        if isinstance(msg, AIMessage):
            seen_tool_calls.update(tc["id"] for tc in msg.tool_calls)
        elif isinstance(msg, ToolMessage) and msg.tool_call_id not in seen_tool_calls:
            orphans.append(msg.tool_call_id)
    return orphans


class TestBudgetTrim:
    """What the model sees once a background investigation outgrows its budget."""

    MODEL = "anthropic/claude-haiku-5.5"
    BUDGET = 20_000  # tokens; each tool pair below is ~530 tokens

    def _model_call(self, mw, messages):
        """Run ``awrap_model_call`` and return the messages the handler received."""
        seen = {}

        async def handler(request):
            seen["messages"] = list(request.messages)
            return "ok"

        asyncio.run(mw.awrap_model_call(_FakeRequest(messages), handler))
        return seen["messages"]

    def test_under_budget_history_passes_through_untouched(self, context_trim):
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=5)
        assert self._model_call(mw, history) == history
        assert mw._cut_message_id is None

    def test_trimmed_request_keeps_the_task_and_starts_at_a_tool_call_boundary(self, context_trim):
        """The task HumanMessage is pinned, the tail starts with an AIMessage (never an
        orphan ToolMessage, never an assistant-first conversation) and the whole thing
        lands under the low watermark, not just under the budget."""
        from langchain_core.messages import AIMessage
        from langchain_core.messages.utils import count_tokens_approximately

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=60)  # ~32K tokens > 20K budget
        assert count_tokens_approximately(history) > self.BUDGET

        kept = self._model_call(mw, history)

        assert kept[0] is history[0], "the original task message must survive the trim"
        assert isinstance(kept[1], AIMessage)
        assert not _orphan_tool_messages(kept)
        assert kept[-1] is history[-1]
        assert len(kept) < len(history)
        watermark = int(self.BUDGET * context_trim._TRIM_LOW_WATERMARK_RATIO)
        assert count_tokens_approximately(kept) <= watermark
        # ...but as much as fits under the watermark: one more pair would overflow it.
        tail_len = len(kept) - 1
        with_one_more_pair = [history[0]] + history[len(history) - tail_len - 2 :]
        assert count_tokens_approximately(with_one_more_pair) > watermark

    def test_consecutive_over_budget_turns_share_the_same_prefix(self, context_trim):
        """Hysteresis: the cut made on turn N is reused on turn N+1 (one new tool pair),
        so the prompt-cache prefix is byte-identical instead of sliding by one pair."""
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        turn_1 = _rca_history(pairs=60)
        kept_1 = self._model_call(mw, turn_1)

        turn_2 = turn_1 + _rca_history(pairs=1, first_pair=60)[1:]
        kept_2 = self._model_call(mw, turn_2)

        assert [m.id for m in kept_2[: len(kept_1)]] == [m.id for m in kept_1]
        assert [m.id for m in kept_2[len(kept_1) :]] == ["ai60", "tool60"]
        assert not _orphan_tool_messages(kept_2)

    def test_a_new_cut_is_made_only_when_the_budget_is_exceeded_again(self, context_trim):
        from langchain_core.messages.utils import count_tokens_approximately

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=60)
        first_cut = self._model_call(mw, history)[1].id

        # Grow the history turn by turn; the pinned cut holds until the kept slice
        # itself crosses the budget, then a new cut lands under the watermark again.
        next_pair = 60
        kept = first_cut
        while kept == first_cut:
            history = history + _rca_history(pairs=1, first_pair=next_pair)[1:]
            next_pair += 1
            kept_messages = self._model_call(mw, history)
            kept = kept_messages[1].id
            assert kept_messages[0] is history[0]
            assert not _orphan_tool_messages(kept_messages)
            assert count_tokens_approximately(kept_messages) <= self.BUDGET
        assert count_tokens_approximately(kept_messages) <= int(
            self.BUDGET * context_trim._TRIM_LOW_WATERMARK_RATIO
        )
        assert mw._cut_message_id == kept

    def test_the_latest_tool_call_block_is_kept_even_when_it_alone_overflows(self, context_trim):
        """A single huge tool result cannot fit the watermark; the model still gets the
        task plus its latest tool call with that result, never an empty or orphaned tail."""
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=3, tool_chars=60_000)  # each pair ~15K tokens
        kept = self._model_call(mw, history)
        assert [m.id for m in kept] == ["task", "ai2", "tool2"]

    def test_the_latest_user_message_survives_a_cut_inside_the_current_turn(self, context_trim):
        """Interactive session with loaded history (review on #740): the current turn's
        tool results alone exceed the low watermark, so every AIMessage boundary that fits
        lies after the current question. The cut must land on that HumanMessage, never
        after it — the model needs the question it is answering."""
        from langchain_core.messages import HumanMessage

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        old_turns = _rca_history(pairs=10)  # old conversation: task + 10 small pairs
        question = HumanMessage("why is checkout-api slow right now?", id="current-question")
        current_turn = _rca_history(pairs=3, tool_chars=30_000, first_pair=10)[1:]  # ~7.5K tokens each
        history = old_turns + [question] + current_turn

        kept = self._model_call(mw, history)

        assert kept[0] is history[0]
        assert question in kept, "the current question must never be trimmed away"
        assert kept.index(question) < kept.index(current_turn[0])
        assert kept[-1] is history[-1]
        assert not _orphan_tool_messages(kept)
        # Everything after the question is kept; the old turns are what got dropped.
        assert kept[kept.index(question) :] == history[history.index(question) :]
        assert len(kept) < len(history)

    def test_older_turns_before_the_latest_user_message_are_still_trimmed(self, context_trim):
        """The floor only forbids cutting AFTER the latest HumanMessage: when the
        current turn fits, the cut lands at an AIMessage boundary before the question
        and the request still comes in under the low watermark."""
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.messages.utils import count_tokens_approximately

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        old_turns = _rca_history(pairs=60)  # ~32K tokens on their own
        question = HumanMessage("and the nodes?", id="current-question")
        current_turn = _rca_history(pairs=2, first_pair=60)[1:]
        history = old_turns + [question] + current_turn

        kept = self._model_call(mw, history)

        assert kept[0] is history[0]
        assert isinstance(kept[1], AIMessage)
        assert question in kept
        assert kept[-1] is history[-1]
        assert not _orphan_tool_messages(kept)
        assert count_tokens_approximately(kept) <= int(self.BUDGET * context_trim._TRIM_LOW_WATERMARK_RATIO)

    def test_a_context_update_appended_last_does_not_disable_trimming(self, context_trim):
        """Background RCA: correlated-alert updates arrive as a HumanMessage appended at
        the very end. Pinning everything up to it would keep the whole history; the
        floor instead keeps the update itself and still trims the old tool turns."""
        from langchain_core.messages import HumanMessage
        from langchain_core.messages.utils import count_tokens_approximately

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=60)
        update = HumanMessage("[RCA CONTEXT UPDATE] correlated alert: db-primary CPU 95%", id="update")
        history = history + [update]

        kept = self._model_call(mw, history)

        assert kept[0] is history[0]
        assert kept[-1] is update
        assert not _orphan_tool_messages(kept)
        assert count_tokens_approximately(kept) <= int(self.BUDGET * context_trim._TRIM_LOW_WATERMARK_RATIO)
        assert len(kept) < len(history)
        # The next call (one more tool pair after the update) reuses the same cut.
        turn_2 = history + _rca_history(pairs=1, first_pair=60)[1:]
        kept_2 = self._model_call(mw, turn_2)
        assert [m.id for m in kept_2[: len(kept)]] == [m.id for m in kept]
        assert [m.id for m in kept_2[len(kept) :]] == ["ai60", "tool60"]

    def test_the_latest_tool_call_block_survives_a_trailing_context_update(self, context_trim):
        """The invariant "the model always sees its latest tool call with its results"
        must hold when a correlated-alert update (a HumanMessage) is appended after an
        oversized tool result: the cut may not move past that tool call to the update."""
        from langchain_core.messages import HumanMessage

        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        history = _rca_history(pairs=3, tool_chars=60_000)  # each pair ~15K tokens
        history.append(HumanMessage("[RCA CONTEXT UPDATE] correlated alert: db-primary CPU 95%", id="update"))

        kept = self._model_call(mw, history)

        assert [m.id for m in kept] == ["task", "ai2", "tool2", "update"]
        assert not _orphan_tool_messages(kept)

    def test_bedrock_conversation_starts_with_the_user_turn(self, context_trim):
        """langchain-aws's own converter: a trimmed request still opens with the user
        role (Bedrock Converse rejects assistant-first conversations)."""
        bc = _real_module("langchain_aws.chat_models.bedrock_converse")
        mw = context_trim.ContextSafetyMiddleware(self.MODEL, token_budget=self.BUDGET)
        kept = self._model_call(mw, _rca_history(pairs=60))
        bedrock_messages, _system = bc._messages_to_bedrock(kept)
        roles = [m["role"] for m in bedrock_messages]
        assert roles[0] == "user"
        assert roles[1] == "assistant"
        assert all(a != b for a, b in zip(roles, roles[1:])), "roles must alternate"
