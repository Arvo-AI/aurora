"""Filter-matrix tests for the Bitbucket change-gating webhook path.

Pins two contracts:

1. ``verify_bitbucket_signature`` (``routes/bitbucket/bitbucket_webhook.py``):
   HMAC-SHA256 over the raw body against ``X-Hub-Signature`` (``sha256=…``),
   constant-time, False on malformed headers.
2. The enqueue contract of ``_handle_pullrequest_event``
   (``tasks/bitbucket_webhook_tasks.py``): ``investigate_bitbucket_pr.delay``
   fires ONLY when a ``pullrequest:*`` delivery passes the full filter chain
   — gated event, non-draft, OPEN, destination == default branch, enrolled,
   owner resolvable, Redis dedupe won. Every skip AFTER the seen-claim
   releases the key (otherwise that (repo, pr, sha) is blocked for 24h).
3. Delivery-side webhook verification: ``_mark_webhook_verified`` runs
   BEFORE the filter chain, because a delivery that got here already passed
   HMAC and so proves the hook is live even when the PR itself is filtered
   out. This is what flips the UI badge without needing the webhook read
   scope. It stamps only repos that are still enabled, and records the URL
   the delivery arrived at, so neither a disable nor a URL change can leave
   a green badge on a hook that no longer works.

DB, Redis and Celery ``delay`` are all mocked — no I/O.
"""

from __future__ import annotations

import hashlib
import hmac
from unittest.mock import MagicMock

import pytest

import tasks.bitbucket_webhook_tasks as bb_tasks
from routes.bitbucket.bitbucket_webhook import verify_bitbucket_signature

_ORG_ID = "org-1"
_DELIVERY_ID = "bb:d-0001"
_USER_ID = "user-1"
_REPO = "acme/api"
_PR_NUMBER = 7
_HEAD_SHA = "abc123"
_SECRET = "s3cret"


# ---------------------------------------------------------------------------
# Signature validation
# ---------------------------------------------------------------------------


def _sign(body: bytes, secret: str = _SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestSignature:
    def test_valid_signature(self):
        body = b'{"pullrequest": {"id": 7}}'
        assert verify_bitbucket_signature(body, _sign(body), _SECRET) is True

    def test_invalid_signature(self):
        body = b'{"pullrequest": {"id": 7}}'
        assert verify_bitbucket_signature(body, _sign(b"other body"), _SECRET) is False

    def test_wrong_secret(self):
        body = b"{}"
        assert verify_bitbucket_signature(body, _sign(body, "wrong"), _SECRET) is False

    @pytest.mark.parametrize("header", ["", "sha256=", "sha1=abcd", "abcdef", None])
    def test_malformed_headers_return_false(self, header):
        assert verify_bitbucket_signature(b"{}", header or "", _SECRET) is False

    def test_body_must_be_byte_exact(self):
        body = b'{"a": 1}'
        reserialized = b'{"a":1}'
        assert verify_bitbucket_signature(reserialized, _sign(body), _SECRET) is False


# ---------------------------------------------------------------------------
# Filter chain / enqueue contract
# ---------------------------------------------------------------------------


def _payload(
    *,
    state: str = "OPEN",
    draft: bool = False,
    dest_branch: str = "main",
    head_sha: str = _HEAD_SHA,
) -> dict:
    return {
        "repository": {"full_name": _REPO},
        "pullrequest": {
            "id": _PR_NUMBER,
            "state": state,
            "draft": draft,
            "title": "Tighten retry loop",
            "source": {"commit": {"hash": head_sha}, "branch": {"name": "feat/x"}},
            "destination": {"branch": {"name": dest_branch}},
        },
    }


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.deleted = []

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def delete(self, key):
        self.deleted.append(key)
        self.store.pop(key, None)


@pytest.fixture
def gating_env(monkeypatch):
    """Mock flag, repo state, owner, Redis, marking, and the Celery task."""
    state = {
        "flag": True,
        "default_branch": "main",
        "enrolled": True,
        "owner": _USER_ID,
    }
    redis = _FakeRedis()
    delay = MagicMock()
    marked: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "utils.flags.feature_flags.is_incident_prevention_enabled",
        lambda: state["flag"],
    )
    monkeypatch.setattr(
        bb_tasks, "_repo_gating_state",
        lambda org_id, repo: (state["default_branch"], state["enrolled"]),
    )
    monkeypatch.setattr(
        bb_tasks, "_resolve_bitbucket_owner", lambda org_id: state["owner"],
    )
    monkeypatch.setattr(
        bb_tasks, "_mark_webhook_verified",
        lambda org_id, repo: marked.append((org_id, repo)),
    )
    monkeypatch.setattr(
        "utils.cache.redis_client.get_redis_client", lambda: redis,
    )

    from tasks.change_gating import investigate_bitbucket_pr
    monkeypatch.setattr(investigate_bitbucket_pr, "delay", delay)

    return state, redis, delay, marked


def _run(event="pullrequest:created", payload=None):
    bb_tasks._handle_pullrequest_event(
        _ORG_ID, payload if payload is not None else _payload(), event, _DELIVERY_ID
    )


class TestFilterChain:
    def test_enqueue_on_happy_path_created(self, gating_env):
        _, redis, delay, _ = gating_env
        _run("pullrequest:created")
        delay.assert_called_once_with(
            user_id=_USER_ID,
            repo_full_name=_REPO,
            pr_number=_PR_NUMBER,
            head_sha=_HEAD_SHA,
            action="opened",
            delivery_id=_DELIVERY_ID,
        )
        # seen key claimed and NOT released on success
        assert redis.deleted == []
        assert any("seen" in k for k in redis.store)

    def test_updated_maps_to_synchronize(self, gating_env):
        _, _, delay, _ = gating_env
        _run("pullrequest:updated")
        assert delay.call_args.kwargs["action"] == "synchronize"

    def test_seen_key_is_provider_prefixed(self, gating_env):
        _, redis, _, _ = gating_env
        _run()
        key = next(k for k in redis.store if "seen" in k)
        assert key == f"change_gating:seen:bitbucket:{_REPO}:{_PR_NUMBER}:{_HEAD_SHA}"

    def test_feature_flag_off_skips(self, gating_env):
        state, redis, delay, _ = gating_env
        state["flag"] = False
        _run()
        delay.assert_not_called()
        assert redis.store == {}  # skipped before the claim

    def test_draft_skips_before_claim(self, gating_env):
        _, redis, delay, _ = gating_env
        _run(payload=_payload(draft=True))
        delay.assert_not_called()
        assert redis.store == {}

    @pytest.mark.parametrize("state_val", ["MERGED", "DECLINED", "SUPERSEDED"])
    def test_not_open_skips(self, gating_env, state_val):
        _, _, delay, _ = gating_env
        _run(payload=_payload(state=state_val))
        delay.assert_not_called()

    def test_non_default_destination_skips(self, gating_env):
        _, _, delay, _ = gating_env
        _run(payload=_payload(dest_branch="develop"))
        delay.assert_not_called()

    def test_missing_default_branch_skips(self, gating_env):
        state, _, delay, _ = gating_env
        state["default_branch"] = None
        _run()
        delay.assert_not_called()

    def test_missing_fields_skip(self, gating_env):
        _, _, delay, _ = gating_env
        payload = _payload()
        del payload["pullrequest"]["source"]
        _run(payload=payload)
        delay.assert_not_called()

    def test_duplicate_delivery_skips_without_enqueue(self, gating_env):
        _, _, delay, _ = gating_env
        _run()
        delay.reset_mock()
        _run()  # same (repo, pr, sha) → duplicate
        delay.assert_not_called()

    def test_not_enrolled_releases_seen_key(self, gating_env):
        state, redis, delay, _ = gating_env
        state["enrolled"] = False
        _run()
        delay.assert_not_called()
        assert len(redis.deleted) == 1
        assert "seen" in redis.deleted[0]
        assert redis.store == {}  # a later redelivery can claim again

    def test_no_owner_releases_seen_key(self, gating_env):
        state, redis, delay, _ = gating_env
        state["owner"] = None
        _run()
        delay.assert_not_called()
        assert len(redis.deleted) == 1

    def test_enqueue_failure_releases_seen_key_and_raises(self, gating_env):
        _, redis, delay, _ = gating_env
        delay.side_effect = RuntimeError("broker down")
        with pytest.raises(RuntimeError):
            _run()
        assert len(redis.deleted) == 1

    def test_ungated_event_skips(self, gating_env):
        _, redis, delay, _ = gating_env
        _run(event="pullrequest:approved")
        delay.assert_not_called()
        assert redis.store == {}


class TestWebhookVerifiedOnDelivery:
    """A delivery reaching this task already passed HMAC, so it proves the
    hook is live regardless of whether the PR itself qualifies for review.
    Marking must therefore happen BEFORE the filter chain, or a healthy
    setup that only ever sees drafts / feature-branch PRs stays amber."""

    def test_marks_on_happy_path(self, gating_env):
        _, _, _, marked = gating_env
        _run()
        assert marked == [(_ORG_ID, _REPO)]

    @pytest.mark.parametrize(
        "payload_kwargs",
        [
            {"draft": True},
            {"state": "MERGED"},
            {"dest_branch": "develop"},
        ],
        ids=["draft", "not_open", "non_default_base"],
    )
    def test_marks_even_when_pr_is_filtered_out(self, gating_env, payload_kwargs):
        _, _, delay, marked = gating_env
        _run(payload=_payload(**payload_kwargs))
        delay.assert_not_called()
        assert marked == [(_ORG_ID, _REPO)]

    def test_dispatcher_still_calls_mark_when_not_enrolled(self, gating_env):
        """The dispatcher marks unconditionally; the enabled-repo guard lives in
        the SQL (see TestMarkWebhookVerifiedSql), not in the call site."""
        state, _, delay, marked = gating_env
        state["enrolled"] = False
        _run()
        delay.assert_not_called()
        assert marked == [(_ORG_ID, _REPO)]

    def test_no_mark_without_a_repo_name(self, gating_env):
        _, _, _, marked = gating_env
        payload = _payload()
        del payload["repository"]
        _run(payload=payload)
        assert marked == []


class TestMarkWebhookVerifiedSql:
    """An orphan hook Aurora could not delete keeps delivering after a disable.
    Stamping those rows would silently undo the disable and re-green the badge,
    so the UPDATE must only touch repos that are still enabled."""

    @staticmethod
    def _capture(monkeypatch):
        import contextlib
        from utils.db import connection_pool

        calls: list[tuple[str, tuple]] = []

        class _Cur:
            rowcount = 0

            def execute(self, sql, params=None):
                calls.append((sql, params))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class _Conn:
            def cursor(self):
                return _Cur()

            def commit(self):
                pass

        @contextlib.contextmanager
        def _get_admin_connection():
            yield _Conn()

        monkeypatch.setattr(
            connection_pool.db_pool, "get_admin_connection", _get_admin_connection
        )
        return calls

    def test_update_is_scoped_to_enabled_repos_and_current_url(self, monkeypatch):
        calls = self._capture(monkeypatch)
        monkeypatch.delenv("NGROK_URL", raising=False)
        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com")

        bb_tasks._mark_webhook_verified(_ORG_ID, _REPO)

        update = next(sql for sql, _ in calls if "UPDATE connected_repos" in sql)
        assert "change_gating_enabled = TRUE" in update
        expected = f"https://api.example.com/bitbucket/webhook/{_ORG_ID}"
        params = next(p for sql, p in calls if "UPDATE connected_repos" in sql)
        assert expected in params and _ORG_ID in params


class TestWebhookUrlScoping:
    """Verification is scoped to the URL a delivery arrived at, so a change to
    Aurora's public URL invalidates it instead of leaving a green badge on a
    hook that can no longer reach us."""

    def test_base_url_prefers_ngrok_only_for_local_backend(self, monkeypatch):
        from connectors.bitbucket_connector.webhook_secret import webhook_base_url

        monkeypatch.setenv("NGROK_URL", "https://tunnel.example.dev/")
        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "http://localhost:5080")
        assert webhook_base_url() == "https://tunnel.example.dev"

        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com")
        assert webhook_base_url() == "https://api.example.com"

    def test_base_url_is_empty_outside_a_request_context(self, monkeypatch):
        """The dispatcher runs in a Celery worker: no Flask request exists, so
        the host_url fallback must return "" rather than raise."""
        from connectors.bitbucket_connector.webhook_secret import (
            webhook_base_url, webhook_url_for_org,
        )

        monkeypatch.delenv("NGROK_URL", raising=False)
        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "")
        assert webhook_base_url() == ""
        assert webhook_url_for_org(_ORG_ID) == ""

    def test_url_for_org_matches_the_ingress_route(self, monkeypatch):
        """Must equal the path registered in main_compute (/bitbucket prefix)
        and the url handed to the admin by the enable endpoint."""
        from connectors.bitbucket_connector.webhook_secret import webhook_url_for_org

        monkeypatch.delenv("NGROK_URL", raising=False)
        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "https://api.example.com")
        assert webhook_url_for_org(_ORG_ID) == (
            f"https://api.example.com/bitbucket/webhook/{_ORG_ID}"
        )

    def test_stale_predicate_only_fires_on_a_url_mismatch(self):
        """Mirrors the SQL in get_workspace_selection: configured vs stale."""
        current = f"https://api.example.com/bitbucket/webhook/{_ORG_ID}"
        old = f"https://tunnel.example.dev/bitbucket/webhook/{_ORG_ID}"

        def state(verified_url, hook_uuid=None):
            configured = verified_url == current
            stale = verified_url is not None and verified_url != current
            return configured, stale

        assert state(current) == (True, False)      # verified here -> Active
        assert state(old) == (False, True)          # verified elsewhere -> red
        assert state(None) == (False, False)        # never verified -> amber
        # A stored hook uuid must NOT imply configured: it only says Aurora once
        # created a hook, not that the hook still exists or can reach us.
        assert state(None, hook_uuid="{abc}") == (False, False)


class TestProviderKeys:
    def test_github_keys_keep_legacy_shape(self):
        from tasks.change_gating import change_gating_keys

        keys = change_gating_keys(_REPO, _PR_NUMBER, _HEAD_SHA)
        assert keys["seen"] == f"change_gating:seen:{_REPO}:{_PR_NUMBER}:{_HEAD_SHA}"

    def test_bitbucket_keys_are_namespaced(self):
        from tasks.change_gating import change_gating_keys

        gh = change_gating_keys(_REPO, _PR_NUMBER, _HEAD_SHA)
        bb = change_gating_keys(_REPO, _PR_NUMBER, _HEAD_SHA, provider="bitbucket")
        assert set(gh) == set(bb) == {"seen", "run", "posted", "verdict"}
        assert all(gh[k] != bb[k] for k in gh)
