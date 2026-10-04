"""Platform-generic team routing: the Slack prompt is byte-identical to the
pre-refactor module (golden), a second platform can swap every Slack token, and
the routing session now names its platform in trigger_metadata."""

from unittest.mock import patch

from services.memory import PLATFORM_MEMORY_IDENTITIES, PlatformMemoryIdentity
from tests.utils.notifications import team_routing_golden as golden
from tests.utils.notifications.slack_fakes import folded_incident, standalone_incident
from utils.notifications import slack_team_routing as shim
from utils.notifications import team_routing as tr

SLACK = tr.SPECS["slack"]


def test_slack_prompt_matches_pre_refactor_golden():
    # Through the shim (what slack_notification_service calls) and the generic module.
    assert shim._build_prompt(standalone_incident(), "") == golden.STANDALONE_NO_INDEX
    assert shim._build_prompt(folded_incident(), golden.INDEX) == golden.FOLDED_WITH_INDEX
    assert tr._build_prompt(folded_incident(), golden.INDEX, SLACK) == golden.FOLDED_WITH_INDEX
    assert shim._recurrence_context(folded_incident()) == golden.RECURRENCE_FOLDED
    assert shim._recurrence_context(standalone_incident()) == ""


def test_spec_fields_are_the_only_substitution_points():
    """A second platform must be able to swap every Slack-specific token."""
    other = tr.PlatformRoutingSpec(
        platform="other", display_name="Other", surface_noun="Other channel",
        title_prefix="Other routing",
        list_channels_tool="list_other", history_tool="other_history",
        post_tool="post_other", thread_param="reply_to", scope_examples="'x only'",
        mapping_example="\"<service> -> <chan>\"",
        connected_check=("builtins", "bool"), log_prefix="[Other]",
    )
    # The memory ref comes from the identity registry, not the spec, so the
    # prompt and the force-injector can never disagree about which entry to read.
    other_identity = PlatformMemoryIdentity("other", "context", "Other", frozenset({"other"}))
    with patch.dict(PLATFORM_MEMORY_IDENTITIES, {"other": other_identity}):
        prompt = tr._build_prompt(folded_incident(), golden.INDEX, other)
    for slack_token in ("Slack", "get_connected_slack_channels", "get_channel_history",
                        "post_slack_message", "thread_ts", "#db-team", "#<channel>"):
        assert slack_token not in prompt, slack_token
    for token in ("Other channel", "context/Other", "list_other", "other_history",
                  "post_other(reply_to=...)", "'x only'", "\"<service> -> <chan>\"",
                  "using reply_to, rather than"):
        assert token in prompt, token
    # Untrusted blocks still fenced.
    assert "<<INCIDENT_TITLE>>\nHigh CPU\n<<END_INCIDENT_TITLE>>" in prompt
    assert "<<END_INCIDENT_INDEX>>" in prompt


def test_routing_session_names_its_platform():
    with patch("chat.backend.agent.tools.slack_tool.is_slack_connected", return_value=True), \
         patch("services.memory.incident_index.read_index", return_value=""), \
         patch("chat.background.task.create_background_chat_session", return_value="sess-1") as sess, \
         patch("chat.background.task.is_background_chat_allowed", return_value=True), \
         patch("chat.background.task.run_background_chat") as run_mock:
        assert shim.trigger_team_routing_agent("u1", standalone_incident()) is True
    kwargs = run_mock.delay.call_args.kwargs
    assert kwargs["trigger_metadata"] == {
        "source": "team_routing",
        "platform": "slack",
        "incident_id": standalone_incident()["incident_id"],
    }
    assert kwargs["initial_message"] == golden.STANDALONE_NO_INDEX
    assert sess.call_args.kwargs["title"].startswith("Slack routing: High CPU")
    assert sess.call_args.kwargs["trigger_metadata"] == kwargs["trigger_metadata"]
