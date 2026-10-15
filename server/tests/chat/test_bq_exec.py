"""bq argv built by cloud_exec. The access token must not reach the log string."""

from chat.backend.agent.tools.cloud_exec_tool import prepare_bq_exec
from utils.terminal.terminal_run import terminal_run


def test_prepare_bq_exec_adds_token_and_project_and_redacts_the_log():
    token = "ya29.super-secret-token"
    argv, logged = prepare_bq_exec(
        "bq query --nouse_legacy_sql 'SELECT 1'",
        token,
        "my-project",
    )
    assert argv[0] == "bq"
    assert "--project_id=my-project" in argv
    assert f"--oauth_access_token={token}" in argv
    assert argv.index("--project_id=my-project") < argv.index("query")
    assert argv.index(f"--oauth_access_token={token}") < argv.index("query")
    assert token not in logged
    assert "--oauth_access_token=[REDACTED]" in logged
    assert "--project_id=my-project" in logged


def test_prepare_bq_exec_does_not_duplicate_project_id():
    argv, logged = prepare_bq_exec(
        "bq --project_id=other query 'SELECT 1'",
        "ya29.token",
        "my-project",
    )
    project_flags = [part for part in argv if part.startswith("--project_id")]
    assert project_flags == ["--project_id=other"]
    assert "ya29.token" not in logged


def test_token_is_added_after_the_guardrail_hash(monkeypatch):
    seen = {}

    def guard(args):
        seen["guard"] = list(args)
        return None

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("utils.terminal.terminal_run._check_guardrails", guard)
    monkeypatch.setenv("ENABLE_POD_ISOLATION", "false")
    monkeypatch.setattr("utils.terminal.terminal_run.get_user_context", lambda: {})
    monkeypatch.setattr(
        "utils.terminal.terminal_run.subprocess.run",
        lambda cmd, **kwargs: seen.update(ran=list(cmd)) or Result(),
    )

    terminal_run(
        ["bq", "--project_id=my-project", "query", "SELECT 1"],
        capture_output=True,
        text=True,
        oauth_access_token="ya29.secret",
    )

    assert seen["guard"] == ["bq", "--project_id=my-project", "query", "SELECT 1"]
    assert seen["ran"][2] == "--oauth_access_token=ya29.secret"
    assert "ya29.secret" not in seen["guard"]
