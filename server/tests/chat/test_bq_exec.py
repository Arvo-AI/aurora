"""bq argv built by cloud_exec. The access token must not reach the log string."""

from chat.backend.agent.tools.cloud_exec_tool import (
    _BQ_BYTES_BILLED_CEILING,
    _BQ_DEFAULT_BYTES_BILLED,
    _BQ_DEFAULT_MAX_ROWS,
    _BQ_MAX_ROWS_CEILING,
    explain_bq_timeout,
    get_command_timeout,
    prepare_bq_exec,
)
from utils.terminal.terminal_run import CompletedProcess, terminal_run


def test_prepare_bq_exec_adds_token_and_project_and_redacts_the_log():
    token = "ya29.super-secret-token"
    argv, logged, _job_id = prepare_bq_exec(
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
    argv, logged, _job_id = prepare_bq_exec(
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


def _flag_tokens(argv, name):
    return [part for part in argv if part == name or part.startswith(name + "=")]


def test_query_gets_default_caps_and_a_job_id():
    argv, logged, job_id = prepare_bq_exec("bq query 'SELECT 1'", None, "my-project")
    assert f"--maximum_bytes_billed={_BQ_DEFAULT_BYTES_BILLED}" in argv
    assert f"--max_rows={_BQ_DEFAULT_MAX_ROWS}" in argv
    assert "--format=json" in argv
    assert "--nouse_legacy_sql" in argv
    assert job_id.startswith("aurora-")
    assert f"--job_id={job_id}" in argv
    assert argv.index("--format=json") < argv.index("query")
    assert argv.index("query") < argv.index(f"--job_id={job_id}")
    assert "SELECT 1" in argv
    assert job_id in logged


def test_explicit_byte_cap_under_the_ceiling_is_kept():
    fifty_gb = 50_000_000_000
    argv, _logged, _job_id = prepare_bq_exec(
        f"bq query --maximum_bytes_billed={fifty_gb} 'SELECT 1'",
        None,
        "my-project",
    )
    assert _flag_tokens(argv, "--maximum_bytes_billed") == [f"--maximum_bytes_billed={fifty_gb}"]


def test_byte_cap_above_one_tebibyte_is_clamped():
    over = _BQ_BYTES_BILLED_CEILING + 1
    argv, _logged, _job_id = prepare_bq_exec(
        f"bq query --maximum_bytes_billed={over} 'SELECT 1'",
        None,
        "my-project",
    )
    assert _flag_tokens(argv, "--maximum_bytes_billed") == [
        f"--maximum_bytes_billed={_BQ_BYTES_BILLED_CEILING}"
    ]


def test_max_rows_above_the_ceiling_is_clamped_and_a_lower_value_is_kept():
    kept, _logged, _job_id = prepare_bq_exec(
        "bq query --max_rows=500 'SELECT 1'", None, "p"
    )
    assert _flag_tokens(kept, "--max_rows") == ["--max_rows=500"]

    clamped, _logged, _job_id = prepare_bq_exec(
        "bq query --max_rows 5000 'SELECT 1'", None, "p"
    )
    assert "--max_rows" in clamped
    assert clamped[clamped.index("--max_rows") + 1] == str(_BQ_MAX_ROWS_CEILING)


def test_format_and_legacy_sql_already_set_are_left_alone():
    argv, _logged, _job_id = prepare_bq_exec(
        "bq --format=csv query --use_legacy_sql 'SELECT 1'",
        None,
        "p",
    )
    assert _flag_tokens(argv, "--format") == ["--format=csv"]
    assert "--nouse_legacy_sql" not in argv
    assert "--use_legacy_sql" in argv


def test_ls_does_not_get_query_caps():
    argv, _logged, job_id = prepare_bq_exec("bq ls", None, "my-project")
    assert job_id is None
    assert "--maximum_bytes_billed" not in " ".join(argv)
    assert "--max_rows" not in " ".join(argv)
    assert "--job_id" not in " ".join(argv)
    assert "--project_id=my-project" in argv


def test_bq_query_timeout_follows_the_verb_and_an_explicit_value_wins():
    assert get_command_timeout("bq query --nouse_legacy_sql 'SELECT 1'") == 300
    assert get_command_timeout("bq query 'SELECT created_at FROM t'") == 300
    assert get_command_timeout("bq --project_id=p wait job_123") == 300
    assert get_command_timeout("bq ls") == 60
    assert get_command_timeout("bq show dataset.table") == 60
    assert get_command_timeout("bq query 'SELECT 1'", 3000) == 3000
    assert get_command_timeout("bq query 'SELECT 1'", 86400) == 3600
    assert get_command_timeout("bq ls", 3000) == 3000
    assert get_command_timeout("bq query 'SELECT 1'", 0) == 300
    assert get_command_timeout("bq --location US query 'SELECT 1'") == 300


def test_location_before_the_verb_still_gets_query_caps():
    argv, _logged, job_id = prepare_bq_exec(
        "bq --location US query 'SELECT 1'", None, "my-project"
    )
    assert job_id is not None
    assert f"--maximum_bytes_billed={_BQ_DEFAULT_BYTES_BILLED}" in argv
    assert "--location" in argv
    assert argv[argv.index("--location") + 1] == "US"


def test_timeout_cancels_the_query_job(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return Result()

    monkeypatch.setattr("chat.backend.agent.tools.cloud_exec_tool.terminal_run", fake_run)
    result = CompletedProcess(args=[], returncode=124, stdout="", stderr="timed out")
    explain_bq_timeout(
        result,
        job_id="aurora-abc",
        project_id="my-project",
        location="US",
        access_token="ya29.secret",
        env={"PATH": "/usr/bin"},
        timeout_s=300,
    )
    argv, kwargs = calls[0]
    assert argv[0] == "bq"
    assert "--project_id=my-project" in argv
    assert "--location=US" in argv
    assert argv[-2:] == ["cancel", "aurora-abc"]
    assert "ya29.secret" not in argv
    assert kwargs["oauth_access_token"] == "ya29.secret"
    assert kwargs["trusted"] is True
    assert result.stderr == "Query exceeded the 300s timeout. The BigQuery job was cancelled."


def test_failed_cancel_says_the_job_may_still_be_running(monkeypatch):
    def fake_run(args, **kwargs):
        class Result:
            returncode = 1
            stdout = ""
            stderr = "already done"

        return Result()

    monkeypatch.setattr("chat.backend.agent.tools.cloud_exec_tool.terminal_run", fake_run)
    result = CompletedProcess(args=[], returncode=124, stdout="", stderr="timed out")
    explain_bq_timeout(
        result,
        job_id="aurora-abc",
        project_id="p",
        location=None,
        access_token=None,
        env=None,
        timeout_s=300,
    )
    assert "may still be running" in result.stderr


def test_a_finished_query_is_not_cancelled(monkeypatch):
    def fake_run(args, **kwargs):
        raise AssertionError("cancel should not run")

    monkeypatch.setattr("chat.backend.agent.tools.cloud_exec_tool.terminal_run", fake_run)
    result = CompletedProcess(args=[], returncode=0, stdout="[]", stderr="")
    explain_bq_timeout(
        result,
        job_id="aurora-abc",
        project_id="p",
        location=None,
        access_token=None,
        env=None,
        timeout_s=300,
    )
    assert result.stderr == ""
