"""Issue classification table for the chat audit view."""

import re

import pytest

from services.monitor import issue_classifier as ic


@pytest.mark.parametrize("text, category", [
    # structured gate codes
    ('{"error": "Command x is blocked", "code": "READ_ONLY_MODE"}', ic.BLOCKED_COMMAND),
    ('{"error": "nope", "code": "POLICY_DENIED"}', ic.BLOCKED_COMMAND),
    ('{"error": "nope", "code":"USER_DECLINED"}', ic.BLOCKED_COMMAND),
    ('{"error": "org has not granted this connector", "code": "CONNECTOR_DENIED"}', ic.MISSING_PERMISSION),
    # gate wording
    ("Command 'aws ec2 terminate-instances' is blocked in Ask mode because ... "
     "Send the request in Agent mode to proceed.", ic.BLOCKED_COMMAND),
    # cloud / k8s permissions
    ("An error occurred (AccessDenied) when calling the DescribeInstances operation", ic.MISSING_PERMISSION),
    ("User: arn:aws:iam::1:user/x is not authorized to perform: ec2:DescribeVpcs", ic.MISSING_PERMISSION),
    ('Error from server (Forbidden): pods is forbidden: User "aurora" cannot list', ic.MISSING_PERMISSION),
    ("ERROR: (gcloud.compute.instances.list) PERMISSION_DENIED: Required 'compute.instances.list'", ic.MISSING_PERMISSION),
    ("(AuthorizationFailed) The client does not have authorization", ic.MISSING_PERMISSION),
    ("HTTP 403 returned by API", ic.MISSING_PERMISSION),
    # connectors
    ("Splunk not connected. Please connect Splunk first.", ic.NOT_CONNECTED),
    ("Datadog API key may be invalid or expired. Token may be expired.", ic.NOT_CONNECTED),
    # timeouts / unavailable
    ("SSH command timed out after 30s", ic.TIMEOUT),
    ("bash: helm: command not found", ic.UNAVAILABLE),
    # nothing
    ("ValueError: invalid literal for int()", ic.OTHER),
    ("", ic.OTHER),
    (None, ic.OTHER),
])
def test_tool_error_categories(text, category):
    assert ic.classify_tool_error(text) == category


def test_port_numbers_are_not_403():
    assert ic.classify_tool_error("connect to 10.0.0.1:14030 refused") == ic.OTHER


def test_gate_code_wins_over_text():
    # A gate refusal quoting a forbidden-looking command is still a blocked command.
    text = '{"error": "kubectl auth can-i --list forbidden", "code": "SAFETY_BLOCKED"}'
    assert ic.classify_tool_error(text) == ic.BLOCKED_COMMAND


@pytest.mark.parametrize("text, expected", [
    ('{"error": "blocked", "code": "READ_ONLY_MODE"}', ic.BLOCKED_COMMAND),
    ('{"error": "AccessDenied: not authorized"}', ic.MISSING_PERMISSION),
    ("Error: Splunk not connected", ic.NOT_CONNECTED),
    # successful output that happens to mention 403 or "forbidden" is not flagged
    ("GET /api 403 12ms\nGET /api 200 3ms", None),
    ('{"items": [{"reason": "Forbidden"}]}', None),
    ("", None),
])
def test_tool_output_only_flags_errors(text, expected):
    assert ic.classify_tool_output(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("I couldn't list the pods: the service account lacks the required RBAC permissions.", ic.MISSING_PERMISSION),
    ("The command was blocked in Ask mode. Switch to Agent mode to run it.", ic.BLOCKED_COMMAND),
    ("Datadog isn't connected, so I can't query metrics.", ic.NOT_CONNECTED),
    ("The deployment rolled out fine and all pods are healthy.", None),
    (None, None),
])
def test_reply_fallback(text, expected):
    assert ic.classify_reply(text) == expected


def test_patterns_are_portable_to_postgres():
    # Postgres ~* is POSIX ARE: no \b, lookarounds, inline flags or non-capturing groups.
    for _, pattern in ic.TOOL_ERROR_PATTERNS:
        assert not re.search(r"\\b|\(\?", pattern), pattern


def test_sql_case_binds_patterns_as_params():
    sql, params = ic.sql_category_case("es.error_message")
    assert sql.count("~* %s") == len(ic.TOOL_ERROR_PATTERNS) == len(params)
    assert "ELSE 'other'" in sql
    for _, pattern in ic.TOOL_ERROR_PATTERNS:
        assert pattern not in sql


def test_every_issue_category_has_a_label():
    assert set(ic.ISSUE_CATEGORIES) | {ic.OTHER} == set(ic.CATEGORY_LABELS)


def test_real_ask_mode_gate_messages_classify_as_blocked():
    # Built by the gate itself, so a wording change there fails here instead of
    # silently dropping issues from the audit view.
    from chat.backend.agent.access.mode_access_controller import ModeAccessController
    from utils.security.read_only_classifier import describe_rejection

    command = "aws logs delete-log-group --log-group-name x"
    allowed, message = ModeAccessController.ensure_cloud_command_allowed(
        "ask", False, command, describe_rejection(command))
    assert not allowed
    assert ic.classify_tool_error(message) == ic.BLOCKED_COMMAND

    allowed, message = ModeAccessController.ensure_iac_action_allowed("ask", "apply")
    assert not allowed
    assert ic.classify_tool_error(message) == ic.BLOCKED_COMMAND


@pytest.mark.parametrize("text", ["Datadog is connected and returning metrics.", "Splunk is now connected."])
def test_connected_is_not_an_issue(text):
    assert ic.classify_reply(text) is None


KUBECTL_REFUSED = (
    'E1112 10:01:02.123456   4242 memcache.go:265] couldn\'t get current server API group list: '
    'Get "https://10.0.0.5:6443/api?timeout=32s": dial tcp 10.0.0.5:6443: connect: connection refused\n'
    "The connection to the server 10.0.0.5:6443 was refused - did you specify the right host or port?"
)


def test_connection_refused_url_timeout_param_is_not_a_timeout():
    assert ic.classify_tool_error(KUBECTL_REFUSED) == ic.NOT_CONNECTED


@pytest.mark.parametrize("text, category", [
    ("error: current-context is not set", ic.NOT_CONNECTED),
    ("The connection to the server localhost:8080 was refused", ic.NOT_CONNECTED),
    ("error: no configuration has been provided, try setting KUBERNETES_MASTER", ic.NOT_CONNECTED),
    ("context deadline exceeded", ic.TIMEOUT),
    ("dial tcp 10.0.0.1:443: i/o timeout", ic.TIMEOUT),
    ("subprocess.TimeoutExpired: Command 'x' timed out after 60 seconds", ic.TIMEOUT),
    ("504 Gateway Timeout", ic.TIMEOUT),
    ("aws: error: --cli-read-timeout must be an integer", ic.OTHER),   # the bare word isn't a timeout
])
def test_timeout_and_connection_phrases(text, category):
    assert ic.classify_tool_error(text) == category
