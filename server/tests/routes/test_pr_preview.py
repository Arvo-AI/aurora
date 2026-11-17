"""POST/GET /api/pr-preview: start and follow a no-install review of a public PR."""

import json
import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest

ORG_ID = "org-test"
USER_ID = "user-test"
SESSION_ID = "11111111-2222-3333-4444-555555555555"
HEADERS = {"X-User-ID": USER_ID, "X-Org-ID": ORG_ID}
PR_URL = "https://github.com/acme/widgets/pull/7"

RISKY_TEXT = json.dumps({
    "verdict": "RISKY",
    "summary": "Drops a column the API still reads.",
    "findings": [{
        "severity": "HIGH",
        "file_path": "db/migrations/0042.sql",
        "line": 3,
        "title": "Column dropped while still queried",
        "explanation": "Every request to /orders returns 500 after deploy.",
    }],
})


class _HTTPError(Exception):
    def __init__(self, status, headers=None, text=""):
        super().__init__(f"status {status}")
        self.response = MagicMock(status_code=status, headers=headers or {}, text=text)


class _FakeRedis:
    def __init__(self):
        self.counts = {}
        self.expiries = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        self.expiries[key] = seconds


@pytest.fixture
def preview_client(monkeypatch):
    pytest.importorskip("flask")
    # Evict so Werkzeug proxies bind to the app this fixture creates; monkeypatch
    # restores the original module objects after the test.
    for mod in [m for m in list(sys.modules) if m.startswith(("routes.", "utils.auth.rbac"))]:
        monkeypatch.delitem(sys.modules, mod)
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            monkeypatch.setitem(sys.modules, heavy, MagicMock())
    monkeypatch.setattr(sys.modules["routes.audit_routes"], "record_audit_event", MagicMock(), raising=False)

    from flask import Flask

    from routes import pr_preview
    from utils.auth import rbac_decorators as rbac

    monkeypatch.setattr(rbac, "get_user_id_from_request", lambda *a, **k: USER_ID)
    monkeypatch.setattr(rbac, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(rbac, "enforce_with_reload", lambda *a, **k: True)
    monkeypatch.setattr(rbac, "_audit_auth_failure", lambda *a, **k: None)
    monkeypatch.setattr(pr_preview, "get_org_id_from_request", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(pr_preview, "set_rls_context", lambda *a, **k: ORG_ID)
    monkeypatch.setattr(pr_preview, "is_incident_prevention_enabled", lambda: True)

    reader = MagicMock()
    reader.get_pull_request.return_value = {"number": 7, "title": "Raise the pool size"}
    monkeypatch.setattr(pr_preview, "PublicGitHubReader", MagicMock(return_value=reader))

    background = ModuleType("chat.background.task")
    background.create_background_chat_session = MagicMock(return_value=SESSION_ID)
    background.is_background_chat_allowed = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "chat.background.task", background)

    tasks_mod = ModuleType("tasks.change_gating")
    tasks_mod.preview_public_pr = MagicMock()
    monkeypatch.setitem(sys.modules, "tasks.change_gating", tasks_mod)

    cursor = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value = cursor
    pool = MagicMock()
    pool.get_user_connection.return_value.__enter__ = lambda s: conn
    pool.get_user_connection.return_value.__exit__ = lambda s, *a: False
    monkeypatch.setattr(pr_preview, "db_pool", pool)

    redis = _FakeRedis()
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: redis)

    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(pr_preview.pr_preview_bp)
    client = app.test_client()
    client.redis = redis
    client.module = pr_preview
    client.reader = reader
    client.background = background
    client.task = tasks_mod.preview_public_pr
    client.cursor = cursor
    return client


def _start(client, url=PR_URL):
    return client.post("/api/pr-preview", json={"url": url}, headers=HEADERS)


def _session_row(status, messages, preview=True):
    trigger = {"source": "change_gating", "repo": "acme/widgets", "pr_number": 7}
    if preview:
        trigger["preview"] = True
    return (status, messages, {"triggerMetadata": trigger})


# ---------------------------------------------------------------------------
# POST: start a preview
# ---------------------------------------------------------------------------

def test_start_creates_a_preview_session_and_enqueues_the_review(preview_client):
    resp = _start(preview_client)

    assert resp.status_code == 202
    assert resp.get_json() == {
        "session_id": SESSION_ID,
        "repo": "acme/widgets",
        "pr_number": 7,
        "title": "Raise the pool size",
    }
    session_kwargs = preview_client.background.create_background_chat_session.call_args.kwargs
    assert session_kwargs["user_id"] == USER_ID
    assert session_kwargs["trigger_metadata"]["source"] == "change_gating"
    assert session_kwargs["trigger_metadata"]["preview"] is True
    preview_client.task.delay.assert_called_once_with(
        user_id=USER_ID, session_id=SESSION_ID, repo_full_name="acme/widgets", pr_number=7,
    )
    preview_client.reader.close.assert_called_once()


@pytest.mark.parametrize("url", ["", "not a link", "https://github.com/acme/widgets/issues/7"])
def test_bad_link_is_400_without_calling_github(preview_client, url):
    resp = _start(preview_client, url)
    assert resp.status_code == 400
    assert "pull request link" in resp.get_json()["error"]
    preview_client.module.PublicGitHubReader.assert_not_called()
    preview_client.task.delay.assert_not_called()


def test_missing_body_is_400(preview_client):
    resp = preview_client.post("/api/pr-preview", headers=HEADERS)
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "exc, status",
    [
        (_HTTPError(404), 404),  # private or missing: GitHub hides private repos as 404
        (_HTTPError(403, {"X-RateLimit-Remaining": "0"}), 429),
        (_HTTPError(403, text="API rate limit exceeded"), 429),
        (_HTTPError(429), 429),
        (_HTTPError(451), 404),
        (_HTTPError(502), 502),
        (ConnectionError("down"), 502),
    ],
)
def test_github_errors_map_to_actionable_responses(preview_client, exc, status):
    preview_client.reader.get_pull_request.side_effect = exc

    resp = _start(preview_client)

    assert resp.status_code == status
    assert resp.get_json()["error"]
    preview_client.background.create_background_chat_session.assert_not_called()
    preview_client.task.delay.assert_not_called()
    preview_client.reader.close.assert_called_once()


def test_aurora_rate_limit_is_429_before_any_session(preview_client):
    preview_client.background.is_background_chat_allowed.return_value = False

    resp = _start(preview_client)

    assert resp.status_code == 429
    preview_client.background.create_background_chat_session.assert_not_called()
    preview_client.task.delay.assert_not_called()


def test_disabled_when_incident_prevention_is_off(preview_client, monkeypatch):
    monkeypatch.setattr(preview_client.module, "is_incident_prevention_enabled", lambda: False)

    resp = _start(preview_client)

    assert resp.status_code == 404
    preview_client.module.PublicGitHubReader.assert_not_called()


def test_session_creation_failure_is_500_and_enqueues_nothing(preview_client):
    preview_client.background.create_background_chat_session.side_effect = ValueError("no org")

    resp = _start(preview_client)

    assert resp.status_code == 500
    preview_client.task.delay.assert_not_called()


# ---------------------------------------------------------------------------
# POST: monthly limit
# ---------------------------------------------------------------------------

def _month_key(user_id=USER_ID):
    from datetime import datetime, timezone

    return f"pr_preview:monthly:{user_id}:{datetime.now(timezone.utc):%Y-%m}"


def test_monthly_limit_allows_exactly_the_limit_then_offers_upgrades(preview_client):
    limit = preview_client.module.MONTHLY_PREVIEW_LIMIT
    assert limit == 10
    for _ in range(limit):
        assert _start(preview_client).status_code == 202

    resp = _start(preview_client)

    assert resp.status_code == 429
    assert resp.get_json()["limit_reached"] is True
    assert "this month" in resp.get_json()["error"]
    assert "Connect GitHub" in resp.get_json()["error"]
    assert preview_client.background.create_background_chat_session.call_count == limit
    assert preview_client.task.delay.call_count == limit
    # The key names the month, and outlives it.
    assert list(preview_client.redis.expiries) == [_month_key()]
    assert preview_client.redis.expiries[_month_key()] > 31 * 24 * 60 * 60


def test_monthly_limit_is_per_user(preview_client):
    preview_client.redis.counts[_month_key("someone-else")] = 999
    assert _start(preview_client).status_code == 202


def test_last_months_usage_does_not_count(preview_client):
    preview_client.redis.counts[f"pr_preview:monthly:{USER_ID}:2000-01"] = 999
    assert _start(preview_client).status_code == 202


@pytest.mark.parametrize(
    "url, lookup_error",
    [("not a link", None), (PR_URL, _HTTPError(404))],
)
def test_failed_starts_do_not_use_the_monthly_limit(preview_client, url, lookup_error):
    preview_client.reader.get_pull_request.side_effect = lookup_error
    _start(preview_client, url)
    assert preview_client.redis.counts == {}


def test_monthly_limit_fails_open_without_redis(preview_client, monkeypatch):
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: None)
    assert _start(preview_client).status_code == 202


def test_monthly_limit_fails_open_on_redis_errors(preview_client, monkeypatch):
    broken = MagicMock()
    broken.incr.side_effect = ConnectionError("redis down")
    monkeypatch.setattr("utils.cache.redis_client.get_redis_client", lambda: broken)
    assert _start(preview_client).status_code == 202


# ---------------------------------------------------------------------------
# GET: follow a preview
# ---------------------------------------------------------------------------

def test_status_while_running_has_no_verdict(preview_client):
    preview_client.cursor.fetchone.return_value = _session_row("in_progress", [])

    resp = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "status": "in_progress", "verdict": None, "repo": "acme/widgets", "pr_number": 7,
    }
    # Scoped to the caller's own session.
    assert preview_client.cursor.execute.call_args.args[1] == (SESSION_ID, USER_ID)


def test_completed_risky_verdict_is_returned(preview_client):
    messages = [{"sender": "user", "text": "prompt"}, {"sender": "bot", "text": RISKY_TEXT}]
    preview_client.cursor.fetchone.return_value = _session_row("completed", messages)

    verdict = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS).get_json()["verdict"]

    assert verdict["verdict"] == "RISKY"
    assert verdict["findings"][0]["file_path"] == "db/migrations/0042.sql"
    assert verdict["findings"][0]["severity"] == "HIGH"


def test_risky_without_findings_is_shown_as_safe(preview_client):
    # Same rule as the installed flow: nothing actionable means SAFE.
    text = json.dumps({"verdict": "RISKY", "summary": "Vague worry.", "findings": []})
    preview_client.cursor.fetchone.return_value = _session_row(
        "completed", json.dumps([{"sender": "bot", "text": text}])
    )

    verdict = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS).get_json()["verdict"]

    assert verdict == {"verdict": "SAFE", "summary": "Vague worry.", "findings": []}


def test_completed_without_parseable_verdict(preview_client):
    preview_client.cursor.fetchone.return_value = _session_row(
        "completed", [{"sender": "bot", "text": "I could not finish."}]
    )

    body = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS).get_json()

    assert body["status"] == "completed"
    assert body["verdict"] is None


def test_failed_session_reports_failed(preview_client):
    preview_client.cursor.fetchone.return_value = _session_row("failed", [])

    body = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS).get_json()

    assert body["status"] == "failed"
    assert body["verdict"] is None


def test_non_preview_sessions_are_not_exposed(preview_client):
    preview_client.cursor.fetchone.return_value = _session_row(
        "completed", [{"sender": "bot", "text": RISKY_TEXT}], preview=False
    )

    resp = preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS)

    assert resp.status_code == 404


def test_unknown_session_is_404(preview_client):
    preview_client.cursor.fetchone.return_value = None
    assert preview_client.get(f"/api/pr-preview/{SESSION_ID}", headers=HEADERS).status_code == 404


def test_malformed_session_id_is_404_without_db(preview_client):
    resp = preview_client.get("/api/pr-preview/not-a-uuid", headers=HEADERS)
    assert resp.status_code == 404
    preview_client.cursor.execute.assert_not_called()
