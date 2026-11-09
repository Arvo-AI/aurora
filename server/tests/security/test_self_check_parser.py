"""The input rail must read the classifier's verdict, not its Markdown.

Regression for a rail that blocked 12 of 20 harmless merged PR descriptions in
Aurora's own repo: the classifier answered "**Answer: No**" (allow) and NeMo's
default parser, which only inspects the first two whitespace tokens, read it as
a block. Every answer below marked REAL was returned by the rail's model for
PR #781 ("docs: link lastwords from the README"), which the rail blocked.
"""

from __future__ import annotations

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

_server_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
if os.path.abspath(_server_dir) not in sys.path:
    sys.path.insert(0, os.path.abspath(_server_dir))

from guardrails.self_check_parser import PARSER_NAME, parse_self_check_verdict  # noqa: E402

_PROMPTS_YML = os.path.join(_server_dir, "guardrails", "config", "prompts.yml")

# REAL: returned by the rail's model for PR #781, which was then blocked.
REAL_ALLOW_ANSWER_BOLD_LABEL = (
    "**Answer: No**\n\nThis is an ordinary pull request description for a "
    "documentation change: it adds a link to the `lastwords` project in the README.\n\n"
    "- **No prompt injection.** The HTML comments are standard CodeRabbit auto-generated "
    "release-note markers. They contain no instructions aimed at the assistant.\n"
    "- **No credential access.** Nothing asks for secrets, environment variables, or "
    "sensitive files.\n"
    "- **No social engineering.** There are no claims of authority and no requests to "
    "bypass controls.\n"
    "- **No destructive operations.** The change only adds a link to the README."
)
REAL_ALLOW_ANSWER_BOLD = (
    "**No**\n\nThis is a standard pull request description for a documentation change. "
    "None of the four categories apply."
)


# ---------------------------------------------------------------------------
# Allow: every way a model says "No"
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        REAL_ALLOW_ANSWER_BOLD_LABEL,
        REAL_ALLOW_ANSWER_BOLD,
        "No",
        "no",
        "No.",
        " No\n",
        "Answer: No",
        "**Answer:** No",
        "Final answer: No",
        "My final answer is: No",
        "Verdict - No, this is a normal ops request.",
        "`No`",
        "## No",
        "safe",
    ],
)
def test_allows_when_the_verdict_is_no(answer):
    assert parse_self_check_verdict(answer) == [True]


# ---------------------------------------------------------------------------
# Block: "Yes", and anything that is not a clear verdict (fail closed)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "Yes",
        "yes.",
        "**Answer: Yes**",
        "**Yes**\n\nThis tries to override the system prompt.",
        "Answer: Yes\n\nNo legitimate ops request asks for /etc/shadow.",
        "unsafe",
        "Unsafe: prompt injection",
    ],
)
def test_blocks_when_the_verdict_is_yes(answer):
    assert parse_self_check_verdict(answer) == [False]


@pytest.mark.parametrize(
    "answer",
    [
        "",
        "   ",
        "**",
        None,
        42,
        ["No"],
        "Answer:",
        "Answer: maybe",
        "Answer: Not sure",
        "I cannot classify this message.",
        "Let me think about whether this is safe. No obvious injection, but...",
        "This message is about deploying code. no",
        "The the the the the no",  # verdict buried past the label window
    ],
)
def test_fails_closed_without_a_clear_verdict(answer):
    assert parse_self_check_verdict(answer) == [False]


def test_the_first_verdict_wins_over_later_prose():
    # A "Yes" verdict followed by an explanation full of "No ..." bullets.
    assert parse_self_check_verdict("**Yes**\n\n- No context\n- No reason given") == [False]
    # A "No" verdict followed by prose that mentions "yes".
    assert parse_self_check_verdict("No. The user says yes to a restart, which is normal ops.") == [True]


# ---------------------------------------------------------------------------
# Wiring: an unregistered parser name makes NeMo fail OPEN, so pin both ends
# ---------------------------------------------------------------------------


def test_prompt_config_names_the_registered_parser():
    yaml = pytest.importorskip("yaml")
    with open(_PROMPTS_YML, encoding="utf-8") as fh:
        prompts = yaml.safe_load(fh)["prompts"]
    task = next(p for p in prompts if p["task"] == "self_check_input")
    # If this name drifts from the registration, NeMo returns the raw answer
    # string, bool("...") is True, and every input is allowed.
    assert task["output_parser"] == PARSER_NAME


def test_build_rails_registers_the_parser(monkeypatch):
    from guardrails import input_rail

    registered = {}

    class FakeRails:
        def __init__(self, config, llm):
            self.config, self.llm = config, llm

        def register_output_parser(self, fn, name):
            registered[name] = fn
            return self

    fake = ModuleType("nemoguardrails")
    fake.LLMRails = FakeRails
    fake.RailsConfig = SimpleNamespace(from_path=lambda path: {"path": path})
    monkeypatch.setitem(sys.modules, "nemoguardrails", fake)
    monkeypatch.setattr(input_rail, "_build_llm", lambda: "llm")

    rails = input_rail._build_rails_sync()

    assert isinstance(rails, FakeRails)
    assert registered == {PARSER_NAME: parse_self_check_verdict}


# ---------------------------------------------------------------------------
# End to end through NeMo's real task manager and Aurora's real config
# (runs wherever nemoguardrails is installed: CI and the server image)
# ---------------------------------------------------------------------------


def _task_manager():
    nemo = pytest.importorskip("nemoguardrails")
    pytest.importorskip("nemoguardrails.llm.taskmanager")
    from nemoguardrails.llm.taskmanager import LLMTaskManager

    config = nemo.RailsConfig.from_path(os.path.dirname(_PROMPTS_YML))
    manager = LLMTaskManager(config)
    return manager


def test_nemo_default_parser_misreads_a_bold_no():
    """Documents the upstream behaviour this change works around."""
    pytest.importorskip("nemoguardrails")
    from nemoguardrails.llm.output_parsers import is_content_safe

    assert is_content_safe("No") == [True]
    assert is_content_safe(REAL_ALLOW_ANSWER_BOLD_LABEL) == [False]


def test_configured_rail_allows_the_real_answer():
    manager = _task_manager()
    from nemoguardrails.library.self_check.utils import parse_self_check_output

    # Without registration the configured name resolves to nothing and NeMo
    # returns the raw string (fail open). Registration is what makes it safe.
    manager.register_output_parser(parse_self_check_verdict, PARSER_NAME)

    assert parse_self_check_output(manager, "self_check_input", REAL_ALLOW_ANSWER_BOLD_LABEL)[0] is True
    assert parse_self_check_output(manager, "self_check_input", "**Answer: Yes**")[0] is False
    assert parse_self_check_output(manager, "self_check_input", "I can't tell.")[0] is False
