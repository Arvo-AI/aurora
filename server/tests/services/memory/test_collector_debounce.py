"""Memory extraction agent cost controls (services.memory.collector).

Pins the debounce digest (user side of the conversation only), the
claim-once semantics of the Redis / in-process fallback, the turn budget and
the per-call options of the extraction model. The Celery app is stubbed: the
real ``celery_config`` pulls Redis in at import.
"""

import sys
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("langchain.agents")
pytest.importorskip("langchain_openai")

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402


@pytest.fixture(scope="module")
def collector():
    celery_cfg = types.ModuleType("celery_config")

    class _App:
        def task(self, *dargs, **dkwargs):
            def _decorator(fn):
                return fn

            if dargs and callable(dargs[0]):
                return dargs[0]
            return _decorator

    celery_cfg.celery_app = _App()
    previous = sys.modules.get("celery_config")
    sys.modules["celery_config"] = celery_cfg
    sys.modules.pop("services.memory.collector", None)
    try:
        import services.memory.collector as mod

        yield mod
    finally:
        sys.modules.pop("services.memory.collector", None)
        if previous is not None:
            sys.modules["celery_config"] = previous
        else:
            sys.modules.pop("celery_config", None)


@pytest.fixture(autouse=True)
def _no_redis(collector, monkeypatch):
    monkeypatch.setattr(collector, "_redis_client", lambda: None)
    collector._clear_debounce_cache()
    yield
    collector._clear_debounce_cache()


class TestConversationDigest:
    def test_no_human_messages_yields_none(self, collector):
        assert collector.conversation_digest("s", [AIMessage(content="hi")]) is None
        assert collector.conversation_digest("s", []) is None

    def test_same_user_side_same_digest_regardless_of_assistant_and_tool_messages(self, collector):
        before = [HumanMessage(content="investigate pods")]
        after = before + [
            AIMessage(content="looking"),
            ToolMessage(content="3 pods", tool_call_id="c1"),
            AIMessage(content="done"),
        ]
        assert collector.conversation_digest("s", before) == collector.conversation_digest("s", after)

    def test_new_user_message_changes_the_digest(self, collector):
        first = [HumanMessage(content="investigate pods"), AIMessage(content="done")]
        second = first + [HumanMessage(content="now check the nodes")]
        assert collector.conversation_digest("s", first) != collector.conversation_digest("s", second)

    def test_digest_is_session_scoped(self, collector):
        msgs = [HumanMessage(content="investigate pods")]
        assert collector.conversation_digest("s-1", msgs) != collector.conversation_digest("s-2", msgs)

    def test_dict_messages_and_block_content_are_supported(self, collector):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "hello"}]},
            {"role": "assistant", "content": "hi"},
        ]
        digest = collector.conversation_digest("s", msgs)
        assert digest is not None and len(digest) == 64


class TestClaimExtraction:
    def test_first_claim_wins_second_is_skipped(self, collector):
        assert collector._claim_extraction("s-1", "d1") is True
        assert collector._claim_extraction("s-1", "d1") is False

    def test_other_digest_or_session_claims_independently(self, collector):
        assert collector._claim_extraction("s-1", "d1") is True
        assert collector._claim_extraction("s-1", "d2") is True
        assert collector._claim_extraction("s-2", "d1") is True

    def test_redis_set_nx_is_used_when_available(self, collector, monkeypatch):
        redis = MagicMock()
        redis.set.return_value = True
        monkeypatch.setattr(collector, "_redis_client", lambda: redis)

        assert collector._claim_extraction("s-1", "d1") is True

        args, kwargs = redis.set.call_args
        assert args[0] == "mem:extract:done:s-1:d1"
        assert kwargs == {"nx": True, "ex": collector._DEBOUNCE_TTL_SECONDS}

    def test_redis_nx_miss_means_already_extracted(self, collector, monkeypatch):
        redis = MagicMock()
        redis.set.return_value = None
        monkeypatch.setattr(collector, "_redis_client", lambda: redis)
        assert collector._claim_extraction("s-1", "d1") is False

    def test_redis_failure_falls_back_to_local_cache(self, collector, monkeypatch):
        redis = MagicMock()
        redis.set.side_effect = ConnectionError("down")
        monkeypatch.setattr(collector, "_redis_client", lambda: redis)
        assert collector._claim_extraction("s-1", "d1") is True
        assert collector._claim_extraction("s-1", "d1") is False


class TestTaskDebounce:
    def _run(self, collector, monkeypatch, messages):
        monkeypatch.setattr(collector, "get_hook", lambda name: (lambda org, user: (True, "")))
        monkeypatch.setattr(collector, "get_org_id_for_user", lambda user_id: "org-1")
        monkeypatch.setattr(
            collector.LLMContextManager, "load_context_history", staticmethod(lambda s, u: messages)
        )
        monkeypatch.setattr(collector, "build_memory_index", lambda user_id: "(index)")
        agent_spy = MagicMock(return_value=3)
        monkeypatch.setattr(collector, "_run_extraction_agent", agent_spy)
        result = collector.extract_memories_from_session(MagicMock(), "sess-1", "user-1")
        return result, agent_spy

    def test_second_enqueue_for_the_same_turn_is_skipped(self, collector, monkeypatch):
        messages = [HumanMessage(content="investigate pods"), AIMessage(content="done")]

        first, spy = self._run(collector, monkeypatch, messages)
        second, spy2 = self._run(collector, monkeypatch, messages)

        assert first["status"] == "ok" and first["model_calls"] == 3
        spy.assert_called_once()
        assert second == {"status": "skipped", "reason": "no_new_user_messages"}
        spy2.assert_not_called()

    def test_new_user_message_runs_again(self, collector, monkeypatch):
        turn_one = [HumanMessage(content="investigate pods"), AIMessage(content="done")]
        turn_two = turn_one + [HumanMessage(content="check nodes too"), AIMessage(content="ok")]

        self._run(collector, monkeypatch, turn_one)
        result, spy = self._run(collector, monkeypatch, turn_two)

        assert result["status"] == "ok"
        spy.assert_called_once()

    def test_assistant_only_history_is_skipped_without_claiming(self, collector, monkeypatch):
        result, spy = self._run(collector, monkeypatch, [AIMessage(content="hello")])
        assert result == {"status": "skipped", "reason": "no_user_messages"}
        spy.assert_not_called()


class TestExtractionModelOptions:
    def test_turn_budget_is_small(self, collector):
        assert 5 <= collector.MAX_AGENT_TURNS <= 10

    def test_agent_uses_cached_cheap_model_with_caps_and_usage_callback(self, collector, monkeypatch):
        captured = {}

        def fake_cached(model, temperature=0.0, provider_mode=None, **kwargs):
            captured["model"] = model
            captured["temperature"] = temperature
            captured["kwargs"] = kwargs
            return MagicMock(name="llm")

        fake_agent = MagicMock()
        monkeypatch.setattr(collector, "get_cached_chat_model", fake_cached)
        monkeypatch.setattr(collector, "create_agent", lambda **kw: fake_agent)
        monkeypatch.setattr(collector, "build_memory_tools", lambda user_id, session_id=None: [])
        monkeypatch.setattr(collector, "_serving_provider", lambda model: "bedrock")
        monkeypatch.setattr(collector.ModelConfig, "MEMORY_AGENT_MODEL", "test/memory-model")
        monkeypatch.delenv("LLM_MAX_TOKENS_MEMORY_AGENT", raising=False)

        collector._run_extraction_agent(
            [HumanMessage(content="we release on Fridays")], "(index)", "user-1", "sess-1", "[t]"
        )

        assert captured["model"] == "test/memory-model"
        assert captured["temperature"] == 0.0
        assert captured["kwargs"] == {"max_tokens": 2000, "disable_thinking": True}
        config = fake_agent.invoke.call_args.kwargs["config"]
        assert config["recursion_limit"] == collector.MAX_AGENT_TURNS * 2 + 2
        (callback,) = config["callbacks"]
        assert callback.request_type == "memory_extraction"
        assert callback.api_provider == "bedrock"
        assert callback.user_id == "user-1" and callback.session_id == "sess-1"
