"""Auxiliary output caps only reach models whose thinking is actually off (review B3-01).

Every auxiliary call (judge, severity, summaries, memory agent) asks for
``max_tokens`` together with ``disable_thinking=True``; the cap is sized for an
answer with no chain-of-thought in it. Each provider translates both options
into its client's constructor fields, and this module pins the coupling at that
translation level, per provider:

- when the provider sends an explicit "thinking off" field (Bedrock native /
  Anthropic direct on Haiku 5.5), the cap goes through;
- when the model never thinks unless asked (Claude 4.x, gpt-4-class, Gemini
  flash-lite), the cap goes through;
- when thinking stays on — Opus 5.5 (always-on), OpenAI reasoning models (the
  Responses API charges reasoning against ``max_output_tokens``), Gemini 2.5+/3
  (cannot be disabled per call), anything through a gateway where the field is
  not portable (Bedrock gateway, OpenRouter), unknown local Ollama models — the
  cap is dropped and the client keeps its own default output limit, exactly the
  behaviour before the caps existed;
- a cap set WITHOUT ``disable_thinking`` (the agent's per-turn budget) always
  passes through: thinking tokens are meant to count against it.

The provider modules are loaded from source with the LangChain client classes
replaced by recording fakes, so the tests build no client and need none of the
optional SDKs (the CI pre-checks job installs only langchain-core). The Bedrock
translation is covered in ``test_bedrock_geo_profiles.py``.
"""
import importlib.util
import os
import sys
import types

import pytest

from chat.backend.agent.call_options import max_tokens_for
from chat.backend.agent.model_mapper import ModelMapper

_SERVER_DIR = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
_PROVIDERS_DIR = os.path.join(_SERVER_DIR, "chat", "backend", "agent", "providers")

JUDGE_CAP = max_tokens_for("judge")
SEVERITY_CAP = max_tokens_for("severity")
SUMMARY_CAP = max_tokens_for("summary")
AGENT_CAP = max_tokens_for("agent")


class _RecordingClient:
    """Stands in for ChatOpenAI / ChatAnthropic / ChatGoogleGenerativeAI / ChatOllama."""

    def __init__(self, **config):
        self.config = config


def _load_by_source(monkeypatch, module_name: str, path: str, *, package: str, rewrites=()):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    for old, new in rewrites:
        source = source.replace(old, new)
    spec = importlib.util.spec_from_loader(module_name, loader=None)
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    monkeypatch.setitem(sys.modules, module_name, module)
    exec(compile(source, path, "exec"), module.__dict__)
    return module


def _real_gemini_thinking_config(monkeypatch):
    """The real ``apply_gemini_thinking_config``, loaded from base_provider.py by path
    (so the providers package itself is never imported)."""
    module = _load_by_source(
        monkeypatch,
        "_caps_base_provider_under_test",
        os.path.join(_PROVIDERS_DIR, "base_provider.py"),
        package="",
    )
    return module.apply_gemini_thinking_config


def _load_provider(monkeypatch, filename: str, *, client_modules: dict, gemini_config=None):
    """Load ``providers/<filename>`` with its relative imports stubbed.

    ``client_modules`` maps a third-party module name (``langchain_openai``, ...) to a
    stub module exposing the recording client class. ``ModelMapper`` is the real one;
    ``base_provider`` / ``_sampling_guard`` are inert stand-ins.
    """
    pkg_name = f"_caps_pkg_{filename[:-3]}"
    pkg = types.ModuleType(pkg_name)
    pkg.__path__ = []
    monkeypatch.setitem(sys.modules, pkg_name, pkg)

    base_provider = types.ModuleType(f"{pkg_name}.base_provider")
    base_provider.BaseLLMProvider = type("BaseLLMProvider", (), {"__init__": lambda self: None})
    if gemini_config is not None:
        base_provider.apply_gemini_thinking_config = gemini_config
    monkeypatch.setitem(sys.modules, f"{pkg_name}.base_provider", base_provider)

    sampling_guard = types.ModuleType(f"{pkg_name}._sampling_guard")
    sampling_guard.make_adaptive_sampling_cls = lambda cls, **kw: cls
    monkeypatch.setitem(sys.modules, f"{pkg_name}._sampling_guard", sampling_guard)

    mapper_alias = f"{pkg_name}_model_mapper"
    mapper_mod = types.ModuleType(mapper_alias)
    mapper_mod.ModelMapper = ModelMapper
    monkeypatch.setitem(sys.modules, mapper_alias, mapper_mod)

    for name, module in client_modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    return _load_by_source(
        monkeypatch,
        f"{pkg_name}.{filename[:-3]}",
        os.path.join(_PROVIDERS_DIR, filename),
        package=pkg_name,
        rewrites=[("from ..model_mapper import", f"from {mapper_alias} import")],
    )


def _stub_module(name: str, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    return module


# ---------------------------------------------------------------------------
# OpenAI direct
# ---------------------------------------------------------------------------


@pytest.fixture()
def openai_provider(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    module = _load_provider(
        monkeypatch,
        "openai_provider.py",
        client_modules={"langchain_openai": _stub_module("langchain_openai", ChatOpenAI=_RecordingClient)},
    )
    return module.OpenAIProvider()


class TestOpenAIDirect:
    def test_reasoning_model_drops_the_judge_cap(self, openai_provider):
        """gpt-5.x reasons at effort=high on the Responses API, where langchain-openai
        renames max_tokens to max_output_tokens — a budget that includes reasoning
        tokens. A 400-token judge cap would end the response before the tool call
        and the fail-closed judge would block every command."""
        model = openai_provider.get_chat_model(
            "openai/gpt-5.5", temperature=0.0, max_tokens=JUDGE_CAP, disable_thinking=True
        )
        assert model.config["model"] == "gpt-5.5"
        assert model.config["reasoning"] == {"effort": "high", "summary": "auto"}
        assert model.config["use_responses_api"] is True
        assert "max_tokens" not in model.config
        assert "max_output_tokens" not in model.config
        assert "disable_thinking" not in model.config

    @pytest.mark.parametrize("model_id", ["openai/gpt-4.1", "openai/gpt-4o-mini"])
    def test_non_reasoning_model_keeps_the_cap(self, openai_provider, model_id):
        model = openai_provider.get_chat_model(
            model_id, temperature=0.0, max_tokens=JUDGE_CAP, disable_thinking=True
        )
        assert model.config["max_tokens"] == JUDGE_CAP
        assert "reasoning" not in model.config

    def test_cap_without_disable_thinking_passes_through_on_a_reasoning_model(self, openai_provider):
        model = openai_provider.get_chat_model("openai/gpt-5.5", max_tokens=AGENT_CAP)
        assert model.config["max_tokens"] == AGENT_CAP

    def test_request_timeout_still_translates_when_the_cap_is_dropped(self, openai_provider):
        model = openai_provider.get_chat_model(
            "openai/gpt-5.5", max_tokens=SUMMARY_CAP, disable_thinking=True, request_timeout=25
        )
        assert model.config["request_timeout"] == 25.0
        assert "max_tokens" not in model.config


# ---------------------------------------------------------------------------
# Anthropic direct
# ---------------------------------------------------------------------------


@pytest.fixture()
def anthropic_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    module = _load_provider(
        monkeypatch,
        "anthropic_provider.py",
        client_modules={"langchain_anthropic": _stub_module("langchain_anthropic", ChatAnthropic=_RecordingClient)},
    )
    return module.AnthropicProvider()


class TestAnthropicDirect:
    def test_haiku_5_5_gets_thinking_off_and_keeps_the_cap(self, anthropic_provider):
        model = anthropic_provider.get_chat_model(
            "anthropic/claude-haiku-5.5", temperature=0.0, max_tokens=JUDGE_CAP, disable_thinking=True
        )
        assert model.config["model"] == "claude-haiku-5-5"
        assert model.config["thinking"] == {"type": "disabled"}
        assert model.config["max_tokens"] == JUDGE_CAP

    @pytest.mark.parametrize(
        "model_id", ["anthropic/claude-opus-5.5", "anthropic/claude-sonnet-5.5", "anthropic/claude-fable-5.1"]
    )
    def test_models_that_keep_thinking_get_neither_field_nor_cap(self, anthropic_provider, model_id):
        """Opus 5.5 is always-on, Sonnet 5.5 rejects `disabled`, Fable 400s on any
        thinking field. Without the field the model thinks, so the cap is dropped
        and ChatAnthropic's own default (model profile / 4096) applies, as on main."""
        model = anthropic_provider.get_chat_model(
            model_id, temperature=0.0, max_tokens=SEVERITY_CAP, disable_thinking=True
        )
        assert "thinking" not in model.config
        assert "max_tokens" not in model.config

    @pytest.mark.parametrize("model_id", ["anthropic/claude-sonnet-4.6", "anthropic/claude-haiku-4.5"])
    def test_legacy_generation_keeps_the_cap_without_a_thinking_field(self, anthropic_provider, model_id):
        model = anthropic_provider.get_chat_model(
            model_id, temperature=0.0, max_tokens=SUMMARY_CAP, disable_thinking=True
        )
        assert "thinking" not in model.config
        assert model.config["max_tokens"] == SUMMARY_CAP

    def test_agent_cap_passes_through_on_opus_5_5(self, anthropic_provider):
        model = anthropic_provider.get_chat_model("anthropic/claude-opus-5.5", max_tokens=AGENT_CAP)
        assert model.config["max_tokens"] == AGENT_CAP

    def test_request_timeout_becomes_timeout(self, anthropic_provider):
        model = anthropic_provider.get_chat_model(
            "anthropic/claude-haiku-5.5", max_tokens=JUDGE_CAP, disable_thinking=True, request_timeout=10
        )
        assert model.config["timeout"] == 10.0
        assert "request_timeout" not in model.config


# ---------------------------------------------------------------------------
# Google AI Studio and Vertex AI (Gemini)
# ---------------------------------------------------------------------------


@pytest.fixture()
def google_provider(monkeypatch):
    monkeypatch.setenv("GOOGLE_AI_API_KEY", "test-key")
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    # `import google.generativeai` must fail so the provider never lists models.
    monkeypatch.setitem(sys.modules, "google.generativeai", None)
    module = _load_provider(
        monkeypatch,
        "google_provider.py",
        client_modules={
            "langchain_google_genai": _stub_module("langchain_google_genai", ChatGoogleGenerativeAI=_RecordingClient)
        },
        gemini_config=_real_gemini_thinking_config(monkeypatch),
    )
    assert module.genai is None
    return module.GoogleProvider()


@pytest.fixture()
def vertex_provider(monkeypatch):
    monkeypatch.setenv("VERTEX_AI_PROJECT", "test-project")
    monkeypatch.delenv("VERTEX_AI_SERVICE_ACCOUNT_JSON", raising=False)
    monkeypatch.delenv("GEMINI_DISABLE_THINKING", raising=False)
    module = _load_provider(
        monkeypatch,
        "vertex_provider.py",
        client_modules={
            "langchain_google_genai": _stub_module("langchain_google_genai", ChatGoogleGenerativeAI=_RecordingClient)
        },
        gemini_config=_real_gemini_thinking_config(monkeypatch),
    )
    return module.VertexAIProvider()


class TestGemini:
    def test_google_2_5_pro_thinks_and_drops_the_severity_cap(self, google_provider):
        """Gemini 2.5 Pro cannot have thinking turned off; with max_output_tokens=32
        the one-word severity answer would come back empty."""
        model = google_provider.get_chat_model(
            "google/gemini-2.5-pro", temperature=0.0, max_tokens=SEVERITY_CAP, disable_thinking=True
        )
        assert model.config["model"] == "gemini-2.5-pro"
        assert model.config["include_thoughts"] is True
        assert "max_output_tokens" not in model.config
        assert "max_tokens" not in model.config

    def test_google_flash_lite_keeps_the_cap(self, google_provider):
        model = google_provider.get_chat_model(
            "google/gemini-2.5-flash-lite", temperature=0.0, max_tokens=SEVERITY_CAP, disable_thinking=True
        )
        assert "include_thoughts" not in model.config
        assert model.config["max_output_tokens"] == SEVERITY_CAP

    def test_gemini_disable_thinking_env_does_not_make_the_cap_safe(self, google_provider, monkeypatch):
        """GEMINI_DISABLE_THINKING only stops the thoughts from being returned; the
        model still thinks server-side, so the cap stays dropped."""
        monkeypatch.setenv("GEMINI_DISABLE_THINKING", "true")
        model = google_provider.get_chat_model(
            "google/gemini-2.5-pro", temperature=0.0, max_tokens=SUMMARY_CAP, disable_thinking=True
        )
        assert "include_thoughts" not in model.config
        assert "max_output_tokens" not in model.config

    def test_vertex_2_5_pro_drops_the_cap(self, vertex_provider):
        model = vertex_provider.get_chat_model(
            "vertex/gemini-2.5-pro", temperature=0.0, max_tokens=SEVERITY_CAP, disable_thinking=True
        )
        assert model.config["vertexai"] is True
        assert "max_output_tokens" not in model.config

    def test_vertex_flash_lite_keeps_the_cap(self, vertex_provider):
        model = vertex_provider.get_chat_model(
            "vertex/gemini-3.5-flash-lite", temperature=0.0, max_tokens=SUMMARY_CAP, disable_thinking=True
        )
        assert model.config["max_output_tokens"] == SUMMARY_CAP

    def test_cap_without_disable_thinking_passes_through(self, vertex_provider):
        model = vertex_provider.get_chat_model("vertex/gemini-2.5-pro", max_tokens=AGENT_CAP)
        assert model.config["max_output_tokens"] == AGENT_CAP


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------


@pytest.fixture()
def openrouter_provider(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    module = _load_provider(
        monkeypatch,
        "openrouter_provider.py",
        client_modules={"langchain_openai": _stub_module("langchain_openai", ChatOpenAI=_RecordingClient)},
    )
    return module.OpenRouterProvider()


class TestOpenRouter:
    @pytest.mark.parametrize(
        "model_id", ["anthropic/claude-haiku-5.5", "anthropic/claude-opus-5.5", "openai/gpt-5.5", "google/gemini-2.5-pro"]
    )
    def test_thinking_models_drop_the_cap_through_the_gateway(self, openrouter_provider, model_id):
        """No portable thinking-off spelling through OpenRouter, so these keep thinking."""
        model = openrouter_provider.get_chat_model(
            model_id, temperature=0.0, max_tokens=JUDGE_CAP, disable_thinking=True
        )
        assert "max_tokens" not in model.config
        assert "disable_thinking" not in model.config

    def test_openrouter_judge_default_keeps_the_cap(self, openrouter_provider):
        """google/gemini-2.5-flash-lite is GUARDRAILS_MODEL under LLM_PROVIDER_MODE=openrouter."""
        model = openrouter_provider.get_chat_model(
            "google/gemini-2.5-flash-lite", temperature=0.0, max_tokens=JUDGE_CAP, disable_thinking=True
        )
        assert model.config["max_tokens"] == JUDGE_CAP

    @pytest.mark.parametrize("model_id", ["anthropic/claude-sonnet-4.6", "openai/gpt-4.1"])
    def test_non_thinking_models_keep_the_cap(self, openrouter_provider, model_id):
        model = openrouter_provider.get_chat_model(
            model_id, temperature=0.0, max_tokens=SUMMARY_CAP, disable_thinking=True
        )
        assert model.config["max_tokens"] == SUMMARY_CAP


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------


@pytest.fixture()
def ollama_provider(monkeypatch):
    module = _load_provider(
        monkeypatch,
        "ollama_provider.py",
        client_modules={"langchain_ollama": _stub_module("langchain_ollama", ChatOllama=_RecordingClient)},
    )
    provider = module.OllamaProvider()
    provider.base_url = "http://ollama.test:11434"
    provider.is_available = lambda: True  # no HTTP probe in tests
    return provider


class TestOllama:
    def test_unknown_local_model_drops_the_cap_when_thinking_off_was_asked(self, ollama_provider):
        """Local models are an open set (qwen3 / deepseek-r1 think by default) and
        there is no per-call thinking switch, so the cap is not forwarded."""
        model = ollama_provider.get_chat_model(
            "ollama/qwen3:8b", temperature=0.0, max_tokens=SUMMARY_CAP, disable_thinking=True
        )
        assert "num_predict" not in model.config
        assert "max_tokens" not in model.config

    def test_cap_without_disable_thinking_becomes_num_predict(self, ollama_provider):
        model = ollama_provider.get_chat_model("ollama/qwen3:8b", max_tokens=AGENT_CAP)
        assert model.config["num_predict"] == AGENT_CAP
