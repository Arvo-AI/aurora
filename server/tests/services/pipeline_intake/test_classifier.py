"""Deterministic pre-classifier: every rule, and precedence between categories."""

import pytest

from services.pipeline_intake.classifier import classify, classify_lines
from services.pipeline_intake.parsers import parse_dbt


@pytest.mark.parametrize("line,category,rule", [
    ("requests.exceptions.HTTPError: 429 Client Error: Too Many Requests for url: https://x", "ingestion_upstream", "ingestion.rate_limit"),
    ("google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded for quota metric", "ingestion_upstream", "ingestion.quota"),
    ("HTTPError: 401 Client Error: Unauthorized for url", "ingestion_upstream", "ingestion.auth_expired"),
    ("The access token has expired, please re-authenticate", "ingestion_upstream", "ingestion.auth_expired"),
    ("HTTPError: 403 Client Error: Forbidden for url: https://x", "ingestion_upstream", "ingestion.forbidden"),
    ("requests.exceptions.ReadTimeout: Read timed out. (read timeout=30)", "ingestion_upstream", "ingestion.timeout"),
    ("Pod nightly-ltv-7f9 terminated: OOMKilled", "platform", "platform.oom"),
    ("OSError: [Errno 28] No space left on device", "platform", "platform.disk_full"),
    ("IO Error: Could not set lock on file \"warehouse.duckdb\": Conflicting lock is held", "platform", "platform.warehouse_unavailable"),
    ("column \"customer_id\" does not exist", "schema_drift", "schema.missing_column"),
    ("Catalog Error: Table with name stg_orders does not exist!", "schema_drift", "schema.missing_relation"),
    ("Binder Error: Cannot compare values of type VARCHAR and type INTEGER", "schema_drift", "schema.type_mismatch"),
    ("operator does not exist: integer = text", "schema_drift", "schema.type_mismatch"),
    ("Conversion Error: Could not convert string 'abc' to INT32", "execution", "execution.bad_cast"),
    ("ERROR: permission denied for table payments", "execution", "execution.permissions"),
    ("Parser Error: syntax error at or near \"form\"", "execution", "execution.sql_error"),
    ("airflow.exceptions.AirflowTaskTimeout: Timeout, PID: 1234", "execution", "execution.missed_schedule"),
    ("KeyError: 'order_id'", "execution", "execution.task_crash"),
    ("all good, nothing to see", "unknown", "none"),
])
def test_rules(line, category, rule):
    result = classify_lines([line])
    assert (result.category, result.rule_id) == (category, rule)


def test_specific_cause_beats_generic_traceback():
    lines = [
        "Traceback (most recent call last):",
        "  File \"/opt/airflow/dags/x.py\", line 3, in run",
        "MemoryError",
        "Task exited with return code Negsignal.SIGKILL",
    ]
    assert classify_lines(lines).category == "platform"


def test_strong_cast_error_beats_weak_timeout_mention():
    lines = ["Conversion Error: Could not convert string 'x' to INT32", "warning: query took long, timed out once"]
    assert classify_lines(lines).category == "execution"


def test_row_counts_do_not_look_like_http_429():
    assert classify_lines(["Inserted 429 rows into stg_orders"]).category == "unknown"


def test_model_error_outranks_test_failure_in_same_run():
    rr = {"results": [
        {"unique_id": "test.p.not_null_a_id.1", "status": "fail", "failures": 2, "message": "Got 2 results"},
        {"unique_id": "model.p.b", "status": "error", "message": "Runtime Error\n  Referenced column \"x\" not found"},
    ]}
    result = classify(parse_dbt(rr))
    assert result.category == "schema_drift"
    assert result.primary_node == "model.p.b"
