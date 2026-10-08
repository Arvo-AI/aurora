"""Channel-description prompt template: the Slack rendering is byte-identical to
the pre-refactor literal, and a second platform only swaps the two placeholders."""

from routes.slack import slack_channel_metadata as scm
from services.channels.metadata import render_metadata_prompt
from tests.services.channels import channels_golden as golden


def test_slack_rendering_matches_pre_refactor_literal():
    assert scm.METADATA_PROMPT == golden.METADATA_TEMPLATE
    assert render_metadata_prompt("Slack", scm._SLACK_FIELDS_HINT, golden.METADATA_CONTEXT) \
        == golden.METADATA_PROMPT_RENDERED


def test_other_platform_substitutes_only_the_placeholders():
    out = render_metadata_prompt("Other Chat", "team, channel name, description", "ctx-here")
    assert out.startswith("Write a 2-3 sentence description of this Other Chat channel for an AI SRE teammate.")
    assert "Infer from the team, channel name, description, and recent messages." in out
    assert out.endswith("\n\nctx-here")
    assert "Slack" not in out
