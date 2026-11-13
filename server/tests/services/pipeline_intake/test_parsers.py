"""Parsers: dbt artifacts, Airflow payloads and raw logs normalize to one evidence shape."""

import pytest

from services.pipeline_intake.parsers import (
    MAX_LOG_BYTES,
    MAX_RUN_RESULTS_BYTES,
    IntakeError,
    load_json_bytes,
    parse_airflow,
    parse_dbt,
    parse_log,
)


# --- dbt -------------------------------------------------------------------

def test_dbt_run_without_failures_is_rejected():
    rr = {"results": [{"unique_id": "model.p.a", "status": "success"}]}
    with pytest.raises(IntakeError, match="no failed"):
        parse_dbt(rr)


def test_dbt_rejects_non_run_results():
    with pytest.raises(IntakeError):
        parse_dbt({"nodes": {}})


# --- Airflow -----------------------------------------------------------------

def test_airflow_payload_requires_ids():
    with pytest.raises(IntakeError):
        parse_airflow({"exception": "boom"})


# --- raw logs ----------------------------------------------------------------

def test_airflow_task_log_is_parsed():
    log = (
        "[2026-11-09, 06:00:05 UTC] {taskinstance.py:2905} ERROR - Task failed with exception\n"
        "requests.exceptions.ReadTimeout: HTTPSConnectionPool(host='api.x'): Read timed out. (read timeout=30)\n"
        "[2026-11-09, 06:00:05 UTC] {taskinstance.py:1206} INFO - Marking task as FAILED. "
        "dag_id=orders_ingest, task_id=extract_orders, run_id=manual__1, execution_date=20261109\n"
    )
    ev = parse_log(log)
    assert ev.context["detected_format"] == "airflow"
    assert ev.node_keys() == ["airflow.orders_ingest.extract_orders"]
    assert ev.run_id == "manual__1"


def test_generic_ci_log_falls_back_to_a_step():
    ev = parse_log("step 1 ok\nnpm ERR! code ELIFECYCLE\nError: Process completed with exit code 1.\n",
                   pipeline_name="ci build")
    assert ev.node_keys() == ["log.step"]
    assert ev.pipeline_name == "ci build"
    assert any("exit code 1" in line for line in ev.error_lines)


# --- redaction and limits ---------------------------------------------------

def test_secrets_in_logs_are_redacted():
    log = (
        "ERROR connecting to postgres://etl_user:hunter2supersecret@db.internal:5432/raw\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXZhbHVlLXh5eg\n"
        "client_secret=Zq8vT3kLmN0pR5sW7yB2dF4hJ6gK9cX1aE3uI5oQ\n"
        "Task failed with exception\n"
    )
    ev = parse_log(log)
    text = ev.log_excerpt + "\n".join(ev.error_lines)
    assert "hunter2supersecret" not in text
    assert "eyJzdWIiOiIxMjM0NTY3ODkwIn0" not in text
    assert "Zq8vT3kLmN0pR5sW7yB2dF4hJ6gK9cX1aE3uI5oQ" not in text
    assert ev.redactions >= 3


def test_secrets_in_dbt_messages_and_sql_are_redacted():
    rr = {"results": [{
        "unique_id": "model.p.m", "status": "error",
        "message": "Database Error: password authentication failed for postgres://u:s3cr3tpassw0rd@h/db",
        "compiled_code": "select * from read_csv('https://u:anotherSecret99@files.example/x.csv')",
    }]}
    ev = parse_dbt(rr)
    node = ev.failing_nodes[0]
    assert "s3cr3tpassw0rd" not in node.error_text
    assert "anotherSecret99" not in (node.compiled_sql or "")


def test_log_size_limit():
    with pytest.raises(IntakeError) as exc:
        parse_log("x" * (MAX_LOG_BYTES + 1))
    assert exc.value.status == 413


def test_artifact_size_limit():
    with pytest.raises(IntakeError) as exc:
        load_json_bytes(b" " * (MAX_RUN_RESULTS_BYTES + 1), "run_results.json", MAX_RUN_RESULTS_BYTES, True)
    assert exc.value.status == 413


def test_invalid_json_artifact():
    with pytest.raises(IntakeError, match="not valid JSON"):
        load_json_bytes(b"{nope", "manifest.json", 1024, False)


def test_long_log_keeps_the_tail():
    log = ("noise line\n" * 20000) + "FATAL: the real error is here\n"
    ev = parse_log(log)
    assert "the real error is here" in ev.log_excerpt
    assert "log_text" in ev.truncated_fields
