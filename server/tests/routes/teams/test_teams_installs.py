"""Bot-install state: persisted list, probe cache and fail-safe behaviour."""

import pytest

from routes.teams import teams_installs as ti


class FakeRedis:
    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value


class FakeClient:
    def __init__(self, primary=None):
        self.primary = primary or {}

    def get_primary_channel(self, team_id):
        return {"id": self.primary.get(team_id, f"19:{team_id}@thread.tacv2")}


@pytest.fixture
def env(monkeypatch):
    state = {"prefs": {}, "redis": FakeRedis(), "probe": {}, "probed": []}
    monkeypatch.setattr(ti, "get_org_id_for_user", lambda user_id: "org-1")
    monkeypatch.setattr(ti, "get_org_preference", lambda org, key, default=None: state["prefs"].get(key, default))
    monkeypatch.setattr(ti, "store_org_preference", lambda org, key, value: state["prefs"].__setitem__(key, value))
    monkeypatch.setattr(ti, "get_redis_client", lambda: state["redis"])

    def fake_probe(service_url, thread_id):
        state["probed"].append((service_url, thread_id))
        result = state["probe"].get(thread_id)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(ti, "team_has_bot", fake_probe)
    return state


def test_mark_installed_and_uninstalled_round_trip(env):
    ti.mark_team_installed("uid-self", "t1")
    assert ti.get_installed_team_ids("uid-self") == {"t1"}
    ti.mark_team_uninstalled("uid-self", "t1")
    assert ti.get_installed_team_ids("uid-self") == set()


def test_uninstall_is_remembered_so_the_probe_cache_says_no(env):
    ti.mark_team_installed("uid-self", "t1")
    ti.mark_team_uninstalled("uid-self", "t1")
    installed, unknown = ti.resolve_installed_teams("uid-self", FakeClient(), ["t1"])
    # Cached "no" is trusted — no probe fired
    assert installed == set() and unknown == set()
    assert env["probed"] == []


def test_remember_service_url_only_stores_microsoft_hosts(env):
    ti.remember_service_url("uid-self", "https://evil.example.com/")
    assert ti._SERVICE_URL_PREF not in env["prefs"]
    ti.remember_service_url("uid-self", "https://smba.trafficmanager.net/emea/")
    assert env["prefs"][ti._SERVICE_URL_PREF] == "https://smba.trafficmanager.net/emea/"


def test_probe_uses_the_general_channel_id_and_stored_host(env):
    env["prefs"][ti._SERVICE_URL_PREF] = "https://smba.trafficmanager.net/emea/"
    env["probe"] = {"19:thread-1": True}
    installed, unknown = ti.resolve_installed_teams(
        "uid-self", FakeClient({"t1": "19:thread-1"}), ["t1"],
    )
    assert installed == {"t1"} and unknown == set()
    assert env["probed"] == [("https://smba.trafficmanager.net/emea/", "19:thread-1")]
    # Positive result is persisted for next time
    assert ti.get_installed_team_ids("uid-self") == {"t1"}


def test_probe_no_marks_team_not_installed_and_drops_stale_positive(env):
    ti.mark_team_installed("uid-self", "t1")
    env["redis"].store.clear()  # cache expired -> re-verify
    env["probe"] = {"19:t1@thread.tacv2": False}
    installed, unknown = ti.resolve_installed_teams("uid-self", FakeClient(), ["t1"])
    assert installed == set() and unknown == set()
    assert ti.get_installed_team_ids("uid-self") == set()


def test_probe_failure_keeps_a_known_install_and_flags_a_new_team_unknown(env):
    ti.mark_team_installed("uid-self", "known")
    env["redis"].store.clear()
    env["probe"] = {
        "19:known@thread.tacv2": RuntimeError("503"),
        "19:fresh@thread.tacv2": None,
    }
    installed, unknown = ti.resolve_installed_teams("uid-self", FakeClient(), ["known", "fresh"])
    # An outage must not flip state either way
    assert installed == {"known"}
    assert unknown == {"fresh"}
    assert ti.get_installed_team_ids("uid-self") == {"known"}


def test_cached_answers_skip_the_probe(env):
    env["probe"] = {"19:t1@thread.tacv2": True}
    ti.resolve_installed_teams("uid-self", FakeClient(), ["t1"])
    ti.resolve_installed_teams("uid-self", FakeClient(), ["t1"])
    assert len(env["probed"]) == 1


def test_without_redis_known_installs_are_trusted_and_new_teams_probed(env, monkeypatch):
    monkeypatch.setattr(ti, "get_redis_client", lambda: None)
    ti.mark_team_installed("uid-self", "known")
    env["probe"] = {"19:fresh@thread.tacv2": True}
    installed, _unknown = ti.resolve_installed_teams("uid-self", FakeClient(), ["known", "fresh"])
    assert installed == {"known", "fresh"}
    # Re-probing every known team on every sync would be a Graph + Connector call each
    assert [t for _u, t in env["probed"]] == ["19:fresh@thread.tacv2"]


def test_probe_budget_is_capped_per_sync(env, monkeypatch):
    monkeypatch.setattr(ti, "MAX_PROBES_PER_SYNC", 2)
    env["probe"] = {f"19:t{i}@thread.tacv2": True for i in range(4)}
    installed, unknown = ti.resolve_installed_teams("uid-self", FakeClient(), [f"t{i}" for i in range(4)])
    assert len(env["probed"]) == 2
    assert len(installed) == 2 and len(unknown) == 2


def test_no_org_means_nothing_is_verifiable(monkeypatch):
    monkeypatch.setattr(ti, "get_org_id_for_user", lambda user_id: None)
    assert ti.resolve_installed_teams("uid-self", FakeClient(), ["t1"]) == (set(), {"t1"})
    assert ti.get_installed_team_ids("uid-self") == set()
