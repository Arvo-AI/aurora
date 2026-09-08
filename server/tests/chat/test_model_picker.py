"""Chat picker shows only models that can run under the current provider mode."""
from routes.llm_config import picker_prefixes


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
