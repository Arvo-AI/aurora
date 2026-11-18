"""GET/PUT /api/aurora-watch: who can see it, who can turn it on, and what is validated."""

import pytest
from flask import Flask

ROLES = {("admin-1", "org-1"): "admin", ("editor-1", "org-1"): "editor", ("viewer-1", "org-1"): "viewer"}
GRANTS = {
    "admin": {("incidents", "read"), ("connectors", "write")},
    "editor": {("incidents", "read"), ("connectors", "write")},
    "viewer": {("incidents", "read")},
}


@pytest.fixture
def call(monkeypatch):
    import routes.watch_routes as mod

    stored = {"value": {}}
    audited = []

    def save_settings(org, **kw):
        stored["value"] = dict(kw)
        return kw

    def get_settings(org):
        v = stored["value"]
        return {"enabled": bool(v.get("enabled")), "owner_user_id": v.get("owner_user_id"),
                "namespaces": v.get("namespaces"), "updated_at": None}

    monkeypatch.setattr("services.watch.store.save_settings", save_settings)
    monkeypatch.setattr("services.watch.store.get_settings", get_settings)
    monkeypatch.setattr("utils.auth.stateless_auth.get_org_preference",
                        lambda org, key, default=None, strict=False: stored["value"])
    monkeypatch.setattr(mod, "record_audit_event",
                        lambda org, user, action, *a, **k: audited.append((org, user, action)))
    monkeypatch.setattr(mod, "_view", lambda user, org: {"enabled": bool(stored["value"].get("enabled"))})
    monkeypatch.setattr("utils.flags.feature_flags.is_aurora_watch_enabled", lambda: True)

    def _run(handler, user, method="GET", body=None):
        rbac_globals = getattr(mod, handler).__globals__
        for name, value in {
            "get_user_id_from_request": lambda: user,
            "get_org_id_from_request": lambda: "org-1",
            "_audit_auth_failure": lambda *a, **k: None,
            "enforce_with_reload": lambda u, o, res, act: (res, act) in GRANTS.get(ROLES.get((u, o)), set()),
        }.items():
            monkeypatch.setitem(rbac_globals, name, value)
        monkeypatch.setattr(mod, "get_org_id_from_request", lambda: "org-1")
        app = Flask(__name__)
        with app.test_request_context("/api/aurora-watch", method=method, json=body):
            rv = getattr(mod, handler)()
            resp, status = rv if isinstance(rv, tuple) else (rv, rv.status_code)
        return resp.get_json(), status

    return _run, stored, audited


def test_viewers_can_see_it_but_not_turn_it_on(call):
    run, stored, _ = call
    assert run("get_aurora_watch", "viewer-1")[1] == 200
    assert run("put_aurora_watch", "viewer-1", "PUT", {"enabled": True})[1] == 403
    assert stored["value"] == {}


def test_turning_it_on_records_who_it_acts_as_and_is_audited(call):
    run, stored, audited = call
    body, status = run("put_aurora_watch", "editor-1", "PUT", {"enabled": True, "namespaces": ["shop", "payments"]})
    assert status == 200 and body == {"enabled": True}
    assert stored["value"] == {"enabled": True, "owner_user_id": "editor-1", "namespaces": ["payments", "shop"]}
    assert audited == [("org-1", "editor-1", "aurora_watch_enabled")]


def test_saving_namespaces_keeps_the_user_it_acts_as(call):
    run, stored, _ = call
    run("put_aurora_watch", "admin-1", "PUT", {"enabled": True})
    run("put_aurora_watch", "editor-1", "PUT", {"enabled": True, "namespaces": ["shop"]})
    assert stored["value"]["owner_user_id"] == "admin-1"
    run("put_aurora_watch", "editor-1", "PUT", {"enabled": False})
    run("put_aurora_watch", "editor-1", "PUT", {"enabled": True})
    assert stored["value"]["owner_user_id"] == "editor-1"  # turned on again: acts as whoever did it


def test_a_write_that_did_not_land_is_an_error_and_not_audited(call, monkeypatch):
    run, _, audited = call
    monkeypatch.setattr("utils.auth.stateless_auth.get_org_preference",
                        lambda org, key, default=None, strict=False: {})
    assert run("put_aurora_watch", "admin-1", "PUT", {"enabled": True})[1] == 500
    assert audited == []


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": True, "namespaces": "shop"},
                                  {"enabled": True, "namespaces": ["Bad_Name"]}, None])
def test_bad_bodies_are_400(call, body):
    run, stored, _ = call
    assert run("put_aurora_watch", "admin-1", "PUT", body)[1] == 400
    assert stored["value"] == {}


def test_operator_switch_wins(call, monkeypatch):
    run, _, _ = call
    monkeypatch.setattr("utils.flags.feature_flags.is_aurora_watch_enabled", lambda: False)
    assert run("put_aurora_watch", "admin-1", "PUT", {"enabled": True})[1] == 409
    assert run("put_aurora_watch", "admin-1", "PUT", {"enabled": False})[1] == 200


def test_the_view_says_when_the_user_watch_acts_as_left(monkeypatch):
    import routes.watch_routes as mod

    monkeypatch.setattr("services.watch.store.get_settings", lambda org: {
        "enabled": True, "owner_user_id": "gone-1", "namespaces": None, "updated_at": "2026-11-17T00:00:00+00:00"})
    monkeypatch.setattr("services.watch.store.list_clusters", lambda user: [{"cluster_id": "kc-1", "name": "prod"}])
    monkeypatch.setattr("services.watch.store.status", lambda user: [{"cluster_id": "kc-1", "seconds_since_poll": 3600}])
    monkeypatch.setattr("services.watch.tasks._owner_in_org", lambda user, org: False)
    monkeypatch.setattr("utils.flags.feature_flags.is_aurora_watch_enabled", lambda: True)
    view = mod._view("admin-1", "org-1")
    # Not "the workers are busy": polls are skipped on purpose until Watch is turned on again.
    assert view["owner_left"] is True and view["clusters"][0]["stale"] is False
