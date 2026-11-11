"""A stalled Gemini call must end on its own.

The Google client waits for as long as the server says nothing unless it is
given a timeout. Both Gemini providers now set one.
"""

from pathlib import Path

import pytest

from chat.backend.agent.providers import base_provider
from chat.backend.agent.providers.base_provider import (
    GEMINI_DEFAULT_REQUEST_TIMEOUT_SECONDS,
    apply_gemini_request_timeout,
)

ENV = "GEMINI_REQUEST_TIMEOUT_SECONDS"
PROVIDERS = Path(base_provider.__file__).parent


def _config(monkeypatch, env_value=None, caller_kwargs=None):
    if env_value is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, env_value)
    config: dict = {}
    apply_gemini_request_timeout(config, caller_kwargs or {})
    return config


def test_default_is_above_the_longest_healthy_step_seen():
    # The client counts the whole streamed answer against the timeout. A healthy
    # step on a large context has taken 404 seconds; a lower limit would cut it off.
    assert GEMINI_DEFAULT_REQUEST_TIMEOUT_SECONDS == 600.0
    assert GEMINI_DEFAULT_REQUEST_TIMEOUT_SECONDS > 404


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_timeout_is_set_when_nothing_is_configured(monkeypatch, env_value):
    assert _config(monkeypatch, env_value) == {"timeout": 600.0}


@pytest.mark.parametrize("env_value, expected", [("900", 900.0), ("45.5", 45.5), (" 120 ", 120.0)])
def test_env_var_changes_the_limit(monkeypatch, env_value, expected):
    assert _config(monkeypatch, env_value) == {"timeout": expected}


@pytest.mark.parametrize("env_value", ["0", "0.0", "-1"])
def test_zero_or_less_turns_the_limit_off(monkeypatch, env_value):
    assert _config(monkeypatch, env_value) == {}


@pytest.mark.parametrize("env_value", ["ten minutes", "600s", "nan", "inf", "-inf"])
def test_a_value_that_is_not_a_number_of_seconds_falls_back_to_the_default(monkeypatch, env_value):
    # A typo must not leave the call without a limit, and "inf" would crash the
    # client when it converts the value to milliseconds.
    assert _config(monkeypatch, env_value) == {"timeout": 600.0}


@pytest.mark.parametrize("caller_kwargs", [{"timeout": 30}, {"request_timeout": 30}, {"timeout": None}])
def test_a_caller_that_chose_its_own_timeout_keeps_it(monkeypatch, caller_kwargs):
    # Both spellings name the same field; setting ours as well would conflict.
    assert _config(monkeypatch, "900", caller_kwargs) == {}


@pytest.mark.parametrize("provider_file", ["vertex_provider.py", "google_provider.py"])
def test_both_gemini_providers_apply_it_before_the_callers_overrides(provider_file):
    """The provider modules import the Google SDK, which CI does not install; pin the wiring from source."""
    source = (PROVIDERS / provider_file).read_text(encoding="utf-8")
    body = source[source.index("def get_chat_model("):]
    applied = body.index("apply_gemini_request_timeout(config, kwargs)")
    overrides = body.index("config.update(kwargs)")
    built = body.index("return ChatGoogleGenerativeAI(**config)")
    assert applied < overrides < built
