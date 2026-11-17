"""Tests for the replay investigator's deterministic parts.

The model call itself is exercised in the manual end-to-end check; what is
tested here is everything that must hold regardless of what the model returns:
prompt structure (evidence is fenced as data) and response parsing.
"""

from __future__ import annotations

import pytest

from incident_replay.evidence import normalize_bundle
from incident_replay.investigator import (
    EVIDENCE_CLOSE,
    EVIDENCE_OPEN,
    SYSTEM_PROMPT,
    build_prompt,
    parse_json_object,
)


@pytest.fixture(scope="module")
def bundle():
    return normalize_bundle("checkout-api")


class TestPromptStructure:
    def test_evidence_is_fenced_as_data(self, bundle):
        prompt = build_prompt(bundle)
        assert prompt.count(EVIDENCE_OPEN) == 1
        assert prompt.count(EVIDENCE_CLOSE) == 1
        assert prompt.index(EVIDENCE_OPEN) < prompt.index(EVIDENCE_CLOSE)

    def test_every_evidence_record_is_inside_the_fence(self, bundle):
        prompt = build_prompt(bundle)
        inside = prompt.split(EVIDENCE_OPEN, 1)[1].split(EVIDENCE_CLOSE, 1)[0]
        for record in bundle.records:
            assert f"[{record.id}]" in inside

    def test_system_prompt_declares_the_block_untrusted(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "untrusted" in lowered
        # The instruction must name the injection shape explicitly, because the
        # fixture bundle deliberately contains one.
        assert "ignore previous instructions" in lowered

    def test_repair_turn_keeps_the_original_rules(self):
        # The retry used to send only a format nudge, dropping the evidence fence
        # and the claim-type list for the one call that is already misbehaving.
        from incident_replay.investigator import REPAIR_SYSTEM

        assert SYSTEM_PROMPT in REPAIR_SYSTEM
        assert "untrusted" in REPAIR_SYSTEM.lower()
        assert "not a single JSON object" in REPAIR_SYSTEM

    def test_system_prompt_forbids_promoting_a_hypothesis(self):
        lowered = SYSTEM_PROMPT.lower()
        assert "causal_hypothesis" in lowered
        assert "never present a hypothesis as a fact" in lowered


class TestParseJsonObject:
    def test_plain_object(self):
        assert parse_json_object('{"a": 1}') == {"a": 1}

    def test_fenced_object(self):
        assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}

    def test_object_after_prose(self):
        assert parse_json_object('Sure, here it is:\n{"a": 1}') == {"a": 1}

    def test_braces_inside_strings_do_not_confuse_the_scanner(self):
        assert parse_json_object('{"a": "}{"}') == {"a": "}{"}

    def test_escaped_quote_inside_string(self):
        assert parse_json_object('{"a": "he said \\"hi\\""}') == {"a": 'he said "hi"'}

    @pytest.mark.parametrize(
        "text",
        ["", "no json here", '{"a": 1', '{"a": }', "```json\nnot json\n```", None],
    )
    def test_unparseable_returns_none(self, text):
        # A truncated or malformed answer must be reported, never half-read.
        assert parse_json_object(text) is None

    def test_first_of_two_objects_wins(self):
        assert parse_json_object('{"a": 1}\n{"b": 2}') == {"a": 1}
