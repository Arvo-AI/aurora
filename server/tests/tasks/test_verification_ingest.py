"""Tests for PR verification ingest.

Two halves, matching the two places the feature lives:

- the webhook filter chain, ``_maybe_enqueue_verification_ingest`` in
  ``tasks/github_webhook_tasks.py``: the task is enqueued ONLY for a completed
  pull-request workflow run on an enrolled repo;
- the task flow, ``_ingest_core`` in ``tasks/verification.py``: find the
  artifact, sanitise it, store the result, replace the PR comment.

GitHub, the database, Redis and Celery are all faked. No I/O.
"""

from __future__ import annotations

import io
import json
import sys
import zipfile
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

import tasks.github_webhook_tasks as webhook_tasks
import tasks.verification as verification
from services.verification import store

_DELIVERY_ID = "d-0001"
_INSTALLATION_ID = 555
_USER_ID = "user-1"
_REPO = "acme/api"
_PR_NUMBER = 7
_RUN_ID = 9001
_HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
_RUN_URL = "https://github.com/acme/api/actions/runs/9001"


# ---------------------------------------------------------------------------
# Webhook filter chain
# ---------------------------------------------------------------------------


def _payload(
    *,
    event: str = "pull_request",
    with_installation: bool = True,
    with_pull_requests: bool = True,
    run_id=_RUN_ID,
) -> dict:
    payload = {
        "action": "completed",
        "workflow_run": {
            "id": run_id,
            "name": "PR Verification",
            "event": event,
            "conclusion": "success",
            "head_sha": _HEAD_SHA,
            "head_branch": "feature",
            "run_attempt": 2,
            "html_url": _RUN_URL,
            "pull_requests": [{"number": _PR_NUMBER}] if with_pull_requests else [],
        },
        "repository": {"full_name": _REPO, "default_branch": "main"},
    }
    if with_installation:
        payload["installation"] = {"id": _INSTALLATION_ID}
    return payload


@pytest.fixture
def webhook_env(monkeypatch):
    """Fake owner resolution, Redis, delivery status and the task's delay()."""
    import utils.cache.redis_client as redis_client_mod

    owner = {"result": ("ok", _USER_ID)}
    monkeypatch.setattr(
        webhook_tasks, "_resolve_change_gating_owner", lambda inst, repo: owner["result"]
    )

    redis_mock = MagicMock()
    redis_mock.set.return_value = True  # NX won: not a duplicate
    monkeypatch.setattr(redis_client_mod, "get_redis_client", lambda: redis_mock)

    ingest = MagicMock()
    task_stub = ModuleType("tasks.verification")
    task_stub.ingest_verification_run = ingest
    monkeypatch.setitem(sys.modules, "tasks.verification", task_stub)

    update_status = MagicMock()
    monkeypatch.setattr(webhook_tasks, "_update_delivery_status", update_status)
    monkeypatch.delenv("NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION", raising=False)

    return SimpleNamespace(owner=owner, redis=redis_mock, ingest=ingest, update_status=update_status)


_WEBHOOK_MATRIX = [
    # (case_id, action, payload_overrides, owner_result, redis_nx_won, expect_enqueue)
    ("not_completed", "requested", {}, ("ok", _USER_ID), True, False),
    ("in_progress", "in_progress", {}, ("ok", _USER_ID), True, False),
    ("push_run", "completed", {"event": "push"}, ("ok", _USER_ID), True, False),
    ("missing_installation", "completed", {"with_installation": False}, ("ok", _USER_ID), True, False),
    ("missing_run_id", "completed", {"run_id": None}, ("ok", _USER_ID), True, False),
    ("suspended", "completed", {}, ("suspended", None), True, False),
    ("not_enrolled", "completed", {}, ("not_enrolled", None), True, False),
    ("duplicate_delivery", "completed", {}, ("ok", _USER_ID), False, False),
    ("happy_path", "completed", {}, ("ok", _USER_ID), True, True),
]


class TestWorkflowRunFilterMatrix:
    @pytest.mark.parametrize(
        "case_id, action, payload_overrides, owner_result, redis_nx_won, expect_enqueue",
        _WEBHOOK_MATRIX,
        ids=[case[0] for case in _WEBHOOK_MATRIX],
    )
    def test_enqueue_only_on_happy_path(
        self, webhook_env, case_id, action, payload_overrides, owner_result,
        redis_nx_won, expect_enqueue,
    ):
        webhook_env.owner["result"] = owner_result
        webhook_env.redis.set.return_value = redis_nx_won

        webhook_tasks._handle_workflow_run_event(
            _payload(**payload_overrides), action, _DELIVERY_ID
        )

        if expect_enqueue:
            webhook_env.ingest.delay.assert_called_once_with(
                user_id=_USER_ID,
                installation_id=_INSTALLATION_ID,
                repo_full_name=_REPO,
                run_id=_RUN_ID,
                run_attempt=2,
                head_sha=_HEAD_SHA,
                conclusion="success",
                run_url=_RUN_URL,
                pr_numbers=[_PR_NUMBER],
                delivery_id=_DELIVERY_ID,
            )
        else:
            webhook_env.ingest.delay.assert_not_called()

        # The existing audit behaviour must survive every branch.
        webhook_env.update_status.assert_called_once_with(_DELIVERY_ID, status="processed")

    def test_feature_flag_off_skips_everything(self, webhook_env, monkeypatch):
        monkeypatch.setenv("NEXT_PUBLIC_ENABLE_INCIDENT_PREVENTION", "false")

        webhook_tasks._handle_workflow_run_event(_payload(), "completed", _DELIVERY_ID)

        webhook_env.ingest.delay.assert_not_called()
        webhook_env.redis.set.assert_not_called()

    def test_fork_run_enqueues_with_no_pr_numbers(self, webhook_env):
        """GitHub omits pull_requests for forks; the task resolves the PR itself."""
        webhook_tasks._handle_workflow_run_event(
            _payload(with_pull_requests=False), "completed", _DELIVERY_ID
        )

        assert webhook_env.ingest.delay.call_args.kwargs["pr_numbers"] == []

    def test_dedupe_key_is_released_when_nothing_is_enqueued(self, webhook_env):
        """Otherwise enrolling the repo later would not help a redelivery."""
        webhook_env.owner["result"] = ("not_enrolled", None)

        webhook_tasks._handle_workflow_run_event(_payload(), "completed", _DELIVERY_ID)

        webhook_env.redis.delete.assert_called_once_with(
            f"verification:seen:{_REPO}:{_RUN_ID}:2"
        )


# ---------------------------------------------------------------------------
# Task flow
# ---------------------------------------------------------------------------


def _report(**overrides) -> dict:
    report = {
        "schema": 1,
        "name": "Preview environment",
        "status": "passed",
        "checks": [
            {"group": "boot", "name": "stack boots", "status": "pass", "detail": ""},
            {"group": "smoke", "name": "api liveness", "status": "pass", "detail": "HTTP 200"},
        ],
        "covers": ["server/**", "client/**"],
        "defined_by": ["scripts/preview/**"],
    }
    report.update(overrides)
    return report


def _archive(report: dict) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("verification.json", json.dumps(report))
    return buffer.getvalue()


class _HTTPError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.response = SimpleNamespace(status_code=status_code, headers={}, text="")


class _Retry(Exception):
    """Stands in for celery.exceptions.Retry."""


class _FakeTask:
    def retry(self, exc=None):
        return _Retry(str(exc))


class _FakeClient:
    """Records calls; every response is set per test."""

    def __init__(self):
        self.artifacts = [{"id": 31, "name": "aurora-verification", "size_in_bytes": 900}]
        self.archive = _archive(_report())
        self.pulls = []
        self.files = [{"filename": "server/routes/x.py"}]
        self.pull_request = {"state": "open", "head": {"sha": _HEAD_SHA}}
        self.posted = []
        self.deleted = []
        self.errors = {}

    def _maybe_raise(self, name):
        if name in self.errors:
            raise self.errors[name]

    def list_run_artifacts(self, run_id):
        self._maybe_raise("list_run_artifacts")
        return self.artifacts

    def download_artifact(self, artifact_id, max_bytes):
        self._maybe_raise("download_artifact")
        return self.archive

    def list_pulls_for_commit(self, head_sha):
        return self.pulls

    def list_files(self, pr_number):
        return self.files

    def get_pull_request(self, pr_number):
        return self.pull_request

    def post_issue_comment(self, pr_number, body):
        self.posted.append((pr_number, body))
        return {"id": 77}

    def delete_issue_comment(self, comment_id):
        self._maybe_raise("delete_issue_comment")
        self.deleted.append(comment_id)


@pytest.fixture
def ingest_env(monkeypatch):
    """Fake client and store; returns a callable that runs the core flow."""
    client = _FakeClient()
    db = SimpleNamespace(
        recorded=[], existing_comment_id=None, prior=[], comment_set=[], cleared=[]
    )

    def record_run(user_id, **fields):
        db.recorded.append(fields)
        return {"id": "row-1", "comment_id": db.existing_comment_id}

    monkeypatch.setattr(store, "record_run", record_run)
    monkeypatch.setattr(store, "prior_comment_ids", lambda *a, **kw: list(db.prior))
    monkeypatch.setattr(
        store, "set_comment_id", lambda user_id, row_id, cid: db.comment_set.append((row_id, cid))
    )
    monkeypatch.setattr(
        store, "clear_comment_ids", lambda user_id, ids: db.cleared.extend(ids)
    )

    enrollment = {"status": "ok"}

    def run(*, conclusion="success", pr_numbers=(_PR_NUMBER,), head_sha=_HEAD_SHA):
        return verification._ingest_core(
            _FakeTask(), _USER_ID, _REPO, _RUN_ID, 1, head_sha, conclusion,
            _RUN_URL, list(pr_numbers), _DELIVERY_ID,
            client=client,
            verify_enrollment=lambda: enrollment["status"],
        )

    return SimpleNamespace(client=client, db=db, enrollment=enrollment, run=run)


class TestIngestCore:
    def test_happy_path_records_and_comments(self, ingest_env):
        result = ingest_env.run()

        assert result == {"status": "completed", "verification": "passed", "pr_number": _PR_NUMBER}
        recorded = ingest_env.db.recorded[0]
        assert recorded["status"] == "passed"
        assert recorded["pr_number"] == _PR_NUMBER
        assert recorded["head_sha"] == _HEAD_SHA
        assert recorded["uncovered_paths"] == []
        assert recorded["modifies_verification"] is False
        pr_number, body = ingest_env.client.posted[0]
        assert pr_number == _PR_NUMBER
        assert "**Passed**" in body
        assert ingest_env.db.comment_set == [("row-1", 77)]

    @pytest.mark.parametrize("status", ["suspended", "not_enrolled"])
    def test_stops_before_any_github_call_when_not_enrolled(self, ingest_env, status):
        ingest_env.enrollment["status"] = status
        ingest_env.client.errors["list_run_artifacts"] = AssertionError("must not be called")

        assert ingest_env.run() == {"status": status}
        assert ingest_env.db.recorded == []

    def test_run_without_the_artifact_is_ignored(self, ingest_env):
        """The common case: most workflow runs are not verification runs."""
        ingest_env.client.artifacts = [{"id": 1, "name": "preview-evidence"}]

        assert ingest_env.run() == {"status": "no_report"}
        assert ingest_env.db.recorded == []
        assert ingest_env.client.posted == []

    def test_expired_artifact_is_ignored(self, ingest_env):
        ingest_env.client.artifacts[0]["expired"] = True

        assert ingest_env.run() == {"status": "no_report"}

    def test_oversized_artifact_is_not_downloaded(self, ingest_env):
        ingest_env.client.artifacts[0]["size_in_bytes"] = 50 * 1024 * 1024
        ingest_env.client.errors["download_artifact"] = AssertionError("must not download")

        assert ingest_env.run() == {"status": "report_too_large"}

    def test_download_that_hits_the_cap_is_rejected(self, ingest_env):
        ingest_env.client.archive = None

        assert ingest_env.run() == {"status": "report_too_large"}
        assert ingest_env.db.recorded == []

    @pytest.mark.parametrize(
        "archive",
        [b"not a zip", _archive({"schema": 99, "checks": []})],
        ids=["not_a_zip", "wrong_schema"],
    )
    def test_unusable_report_is_rejected(self, ingest_env, archive):
        ingest_env.client.archive = archive

        assert ingest_env.run() == {"status": "invalid_report"}
        assert ingest_env.db.recorded == []

    def test_malformed_head_sha_is_rejected(self, ingest_env):
        """head_sha is interpolated into a GitHub API path."""
        assert ingest_env.run(head_sha="../../orgs/acme") == {"status": "invalid_head_sha"}

    def test_failed_workflow_overrides_a_passing_report(self, ingest_env):
        result = ingest_env.run(conclusion="failure")

        assert result["verification"] == "failed"
        assert ingest_env.db.recorded[0]["status"] == "failed"
        assert "**Failed**" in ingest_env.client.posted[0][1]

    def test_reports_files_the_run_did_not_exercise(self, ingest_env):
        ingest_env.client.files = [
            {"filename": "server/routes/x.py"},
            {"filename": "deploy/helm/aurora/values.yaml"},
        ]

        ingest_env.run()

        assert ingest_env.db.recorded[0]["uncovered_paths"] == ["deploy/helm/aurora/values.yaml"]
        assert "- `deploy/helm/aurora/values.yaml`" in ingest_env.client.posted[0][1]

    def test_flags_a_pr_that_changes_the_verification(self, ingest_env):
        ingest_env.client.files = [{"filename": "scripts/preview/smoke.sh"}]

        ingest_env.run()

        assert ingest_env.db.recorded[0]["modifies_verification"] is True
        assert "changes the verification itself" in ingest_env.client.posted[0][1]

    def test_fork_run_resolves_the_pr_from_the_commit(self, ingest_env):
        ingest_env.client.pulls = [
            {"number": 3, "state": "closed"},
            {"number": 12, "state": "open"},
        ]

        result = ingest_env.run(pr_numbers=())

        assert result["pr_number"] == 12
        assert ingest_env.client.posted[0][0] == 12

    def test_run_with_no_pull_request_is_recorded_but_not_commented(self, ingest_env):
        result = ingest_env.run(pr_numbers=())

        assert result == {"status": "recorded_no_pr", "verification": "passed"}
        assert ingest_env.db.recorded[0]["pr_number"] is None
        assert ingest_env.client.posted == []

    def test_redelivery_does_not_comment_twice(self, ingest_env):
        ingest_env.db.existing_comment_id = 55

        assert ingest_env.run()["status"] == "already_commented"
        assert ingest_env.client.posted == []

    def test_stale_head_is_recorded_but_not_commented(self, ingest_env):
        """A newer push owns the PR now; its own run will report."""
        ingest_env.client.pull_request = {"state": "open", "head": {"sha": "f" * 40}}

        assert ingest_env.run()["status"] == "recorded_stale_head"
        assert len(ingest_env.db.recorded) == 1
        assert ingest_env.client.posted == []

    def test_closed_pr_is_recorded_but_not_commented(self, ingest_env):
        ingest_env.client.pull_request = {"state": "closed", "head": {"sha": _HEAD_SHA}}

        assert ingest_env.run()["status"] == "recorded_pr_not_open"
        assert ingest_env.client.posted == []

    def test_replaces_the_comment_from_an_earlier_run(self, ingest_env):
        ingest_env.db.prior = [41, 42]

        ingest_env.run()

        assert ingest_env.client.deleted == [41, 42]
        assert ingest_env.db.cleared == [41, 42]

    def test_failing_to_delete_an_old_comment_does_not_fail_the_run(self, ingest_env):
        ingest_env.db.prior = [41]
        ingest_env.client.errors["delete_issue_comment"] = _HTTPError(404)

        assert ingest_env.run()["status"] == "completed"
        assert ingest_env.db.cleared == []

    def test_transient_github_error_is_retried(self, ingest_env):
        ingest_env.client.errors["list_run_artifacts"] = _HTTPError(503)

        with pytest.raises(_Retry):
            ingest_env.run()

    def test_permanent_github_error_is_not_retried(self, ingest_env):
        ingest_env.client.errors["list_run_artifacts"] = _HTTPError(404)

        with pytest.raises(verification._PermanentProviderError):
            ingest_env.run()
