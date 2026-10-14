"""bq argv built by cloud_exec. The access token must not reach the log string."""

from chat.backend.agent.tools.cloud_exec_tool import command_for_gate, prepare_bq_exec


def test_bq_is_gated_as_bq_not_gcloud():
    assert command_for_gate("gcp", "bq query --nouse_legacy_sql 'SELECT 1'") == (
        "bq query --nouse_legacy_sql 'SELECT 1'"
    )
    assert command_for_gate("gcp", "compute instances list") == "gcloud compute instances list"


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
