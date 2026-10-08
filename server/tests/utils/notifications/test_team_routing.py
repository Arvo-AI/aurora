"""Platform-generic team routing: with Slack as the only connected platform the
prompt and dispatch are identical to the pre-refactor module (golden), one agent
run covers every connected platform, and the platform registries stay in step."""

from dataclasses import replace
from unittest.mock import patch

from services.channels import registry as channel_registry
from services.memory import PLATFORM_MEMORY_IDENTITIES, PlatformMemoryIdentity
from services.memory.platform_memory import PLATFORM_MEMORY_SPECS
from tests.utils.notifications import team_routing_golden as golden
from tests.utils.notifications.slack_fakes import folded_incident, standalone_incident
from utils.notifications import slack_team_routing as shim
from utils.notifications import team_routing as tr

SLACK = tr.SPECS["slack"]
OTHER = tr.PlatformRoutingSpec(
    platform="other", display_name="Other", surface_noun="Other channel",
    list_channels_tool="list_other", history_tool="other_history",
    post_tool="post_other", thread_param="reply_to", scope_examples="'x only'",
    mapping_example="\"<service> -> <chan>\"",
    connected_check=("builtins", "bool"),  # bool(user_id): connected for any user
)
# The memory ref comes from the identity registry, not the spec, so the prompt
# and the force-injector can never disagree about which entry to read.
OTHER_IDENTITY = PlatformMemoryIdentity("other", "context", "Other", frozenset({"other"}))


def _dispatch(fn, *, slack_connected=True):
    with patch("chat.backend.agent.tools.slack_tool.is_slack_connected", return_value=slack_connected), \
         patch("services.memory.incident_index.read_index", return_value=""), \
         patch("chat.background.task.create_background_chat_session", return_value="sess-1") as sess, \
         patch("chat.background.task.is_background_chat_allowed", return_value=True), \
         patch("chat.background.task.run_background_chat") as run_mock:
        result = fn("u1", standalone_incident())
    return result, run_mock, sess


def test_slack_prompt_matches_pre_refactor_golden():
    # Through the shim (what slack_notification_service calls) and the generic module.
    assert shim._build_prompt(standalone_incident(), "") == golden.STANDALONE_NO_INDEX
    assert shim._build_prompt(folded_incident(), golden.INDEX) == golden.FOLDED_WITH_INDEX
    assert tr._build_prompt(folded_incident(), golden.INDEX, [SLACK]) == golden.FOLDED_WITH_INDEX
    assert shim._recurrence_context(folded_incident()) == golden.RECURRENCE_FOLDED
    assert shim._recurrence_context(standalone_incident()) == ""
    # Platforms that share a thread parameter name it once.
    assert tr._recurrence_context(folded_incident(), [SLACK, SLACK]) == golden.RECURRENCE_FOLDED


def test_slack_only_dispatch_is_identical_to_pre_refactor():
    result, run_mock, sess = _dispatch(shim.trigger_team_routing_agent)
    assert result is True
    kwargs = run_mock.delay.call_args.kwargs
    assert kwargs["trigger_metadata"] == {
        "source": "team_routing",
        "incident_id": standalone_incident()["incident_id"],
    }
    assert kwargs["initial_message"] == golden.STANDALONE_NO_INDEX
    assert sess.call_args.kwargs["title"].startswith("Slack routing: High CPU")
    assert sess.call_args.kwargs["trigger_metadata"] == kwargs["trigger_metadata"]


def test_spec_fields_are_the_only_substitution_points():
    """A second platform must be able to swap every Slack-specific token."""
    with patch.dict(PLATFORM_MEMORY_IDENTITIES, {"other": OTHER_IDENTITY}):
        prompt = tr._build_prompt(folded_incident(), golden.INDEX, [OTHER])
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


def test_one_agent_run_covers_every_connected_platform():
    """Two connected platforms mean one session whose prompt describes the
    incident once and carries each platform's policy and tools, not two sessions."""
    with patch.dict(tr.SPECS, {"other": OTHER}), \
         patch.dict(PLATFORM_MEMORY_IDENTITIES, {"other": OTHER_IDENTITY}):
        result, run_mock, sess = _dispatch(tr.trigger_team_routing_agent)
        assert result is True
        run_mock.delay.assert_called_once()
        prompt = run_mock.delay.call_args.kwargs["initial_message"]
        assert prompt.count("<<INCIDENT_TITLE>>") == 1
        assert "any Slack team channel or Other channel about it" in prompt
        for token in ("context/Slack", "post_slack_message(thread_ts=...)",
                      "context/Other", "post_other(reply_to=...)"):
            assert token in prompt, token
        # Each section reads as if it were the only one ("post NOTHING and
        # stop"), so with several the prompt scopes them to their platform.
        assert tr._MULTI_PLATFORM_SCOPE in prompt
        assert sess.call_args.kwargs["title"].startswith("Slack + Other routing: High CPU")
        assert "platform" not in run_mock.delay.call_args.kwargs["trigger_metadata"]

        # A platform that is not connected is left out of the same single run.
        result, run_mock, sess = _dispatch(tr.trigger_team_routing_agent, slack_connected=False)
        assert result is True
        prompt = run_mock.delay.call_args.kwargs["initial_message"]
        assert "post_other(reply_to=...)" in prompt
        assert "post_slack_message" not in prompt
        assert tr._MULTI_PLATFORM_SCOPE not in prompt
        assert sess.call_args.kwargs["title"].startswith("Other routing: High CPU")


def test_a_failing_connection_check_only_drops_that_platform():
    """A platform whose connection check raises counts as not connected; the
    others still get their routing run."""
    broken = replace(OTHER, connected_check=("builtins", "int"))  # int("u1") raises
    with patch.dict(tr.SPECS, {"other": broken}), \
         patch.dict(PLATFORM_MEMORY_IDENTITIES, {"other": OTHER_IDENTITY}):
        result, run_mock, _sess = _dispatch(tr.trigger_team_routing_agent)
    assert result is True
    assert run_mock.delay.call_args.kwargs["initial_message"] == golden.STANDALONE_NO_INDEX


def test_platform_registries_stay_in_step():
    """A chat platform must be registered everywhere or nowhere: a routing spec
    without a memory identity raises when its prompt is built, and one without a
    memory spec or channel provider cannot be seeded or have channels."""
    platforms = set(tr.SPECS)
    assert platforms == set(PLATFORM_MEMORY_IDENTITIES)
    assert platforms == set(PLATFORM_MEMORY_SPECS)
    assert platforms == set(channel_registry.PROVIDERS)
