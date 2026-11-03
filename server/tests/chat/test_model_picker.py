"""Chat picker shows only models that can run under the current provider mode."""
from routes.llm_config import picker_local_models, picker_prefixes


def test_openrouter_shows_all():
    assert picker_prefixes("openrouter", {"OPENAI_API_KEY": "sk", "VERTEX_AI_PROJECT": "p"}) is None


def test_ollama_shows_all():
    assert picker_prefixes("ollama", {"OLLAMA_BASE_URL": "http://localhost:11434"}) is None


def test_vertex_mode_hides_openai_and_google_ai():
    assert picker_prefixes("vertex", {"OPENAI_API_KEY": "sk", "GOOGLE_AI_API_KEY": "g"}) == ["vertex"]


def test_bedrock_mode_only_bedrock():
    assert picker_prefixes("bedrock", {"OPENAI_API_KEY": "sk"}) == ["bedrock"]


def test_direct_openai_key_only():
    assert picker_prefixes("direct", {"OPENAI_API_KEY": "sk"}) == ["openai"]


def test_direct_google_ai_is_not_vertex():
    assert picker_prefixes("direct", {"GOOGLE_AI_API_KEY": "g"}) == ["google"]
    assert picker_prefixes("direct", {"VERTEX_AI_PROJECT": "p"}) == ["vertex"]


def test_direct_no_keys_shows_all():
    assert picker_prefixes("direct", {}) is None
    assert picker_prefixes("direct", {"AWS_DEFAULT_REGION": "us-east-1"}) is None


def test_ollama_only_direct_deploy_offers_configured_ollama_models():
    """#749: the cloud catalog has no ollama/ ids, so the picker needs the local ones."""
    env = {"OLLAMA_BASE_URL": "http://ollama:11434", "MAIN_MODEL": "ollama/qwen3:8b", "RCA_MODEL": "ollama/llama3.3:70b"}
    prefixes = picker_prefixes("direct", env)
    assert prefixes == ["ollama"]
    assert picker_local_models(prefixes, env) == ["ollama/qwen3:8b", "ollama/llama3.3:70b"]


def test_local_models_are_deduplicated():
    env = {"MAIN_MODEL": "ollama/qwen3:8b", "RCA_MODEL": "ollama/qwen3:8b"}
    assert picker_local_models(["ollama"], env) == ["ollama/qwen3:8b"]


def test_local_models_skip_cloud_ids_and_unset_vars():
    env = {"MAIN_MODEL": "anthropic/claude-sonnet-5", "RCA_MODEL": ""}
    assert picker_local_models(["ollama", "anthropic"], env) == []


def test_no_local_models_without_ollama_in_the_picker():
    env = {"MAIN_MODEL": "ollama/qwen3:8b"}
    assert picker_local_models(["openai"], env) == []
    # Full-catalog modes (OpenRouter, Ollama mode, no keys) keep their existing behaviour.
    assert picker_local_models(None, env) == []
