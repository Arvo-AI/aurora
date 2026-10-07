"""query_cloudflare / cloudflare_action over the Rulesets API, across every
account a token spans, with honest limits (the agent is told the total)."""
import json
from unittest.mock import patch

import requests

import chat.backend.agent.tools.cloudflare_tool as cf
from connectors.cloudflare_connector.api_client import (
    PHASE_CACHE,
    PHASE_FIREWALL_CUSTOM,
    PHASE_FIREWALL_MANAGED,
    PHASE_RATELIMIT,
)

CREDS = {
    "api_token": "t", "account_id": "a1", "account_name": "Acme",
    "accounts": [{"id": "a1", "name": "Acme"}, {"id": "a2", "name": "Acme EU"}],
}


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = b"forbidden"
    return requests.exceptions.HTTPError(response=resp)


class FakeClient:
    def __init__(self, zone_phases=None, account_phases=None, rulesets=None,
                 workers=None, zones=None, dns=None, page_rules=None, toggle=None):
        self.zone_phases = zone_phases or {}        # (zone_id, phase) -> ruleset | Exception
        self.account_phases = account_phases or {}  # (account_id, phase) -> ruleset | Exception
        self.rulesets = rulesets or {}              # (scope_id, ruleset_id) -> ruleset
        self.workers = workers or {}                # account_id -> list | Exception
        self.zones = zones or []
        self.dns = dns or []
        self.page_rules = page_rules or []
        self.toggle = toggle
        self.calls = []

    def get_phase_entrypoint(self, scope, scope_id, phase):
        self.calls.append(("entrypoint", scope, scope_id, phase))
        store = self.zone_phases if scope == "zones" else self.account_phases
        value = store.get((scope_id, phase))
        if isinstance(value, Exception):
            raise value
        return value

    def list_phase_rules(self, scope, scope_id, phase):
        return list((self.get_phase_entrypoint(scope, scope_id, phase) or {}).get("rules") or [])

    def get_ruleset(self, scope, scope_id, ruleset_id):
        return self.rulesets[(scope_id, ruleset_id)]

    def list_rulesets(self, scope, scope_id):
        return [{"id": rid, "name": rs.get("name"), "kind": rs.get("kind"), "phase": rs.get("phase")}
                for (sid, rid), rs in self.rulesets.items() if sid == scope_id]

    def list_workers(self, account_id):
        value = self.workers.get(account_id, [])
        if isinstance(value, Exception):
            raise value
        return value

    def list_zones(self, account_id=None):
        self.calls.append(("zones", account_id))
        return self.zones

    def list_dns_records(self, zone_id, record_type=None, name=None):
        return self.dns

    def list_page_rules(self, zone_id):
        return self.page_rules

    def update_firewall_rule_paused(self, zone_id, rule_id, paused):
        if isinstance(self.toggle, Exception):
            raise self.toggle
        return self.toggle


def _query(client, creds=CREDS, **kwargs):
    with patch.object(cf, "_get_cloudflare_credentials", return_value=creds), \
            patch.object(cf, "_build_client", return_value=client), \
            patch.object(cf, "_get_enabled_zone_ids", return_value=None):
        return json.loads(cf.query_cloudflare(user_id="u1", **kwargs))


def _action(client, **kwargs):
    with patch.object(cf, "_get_cloudflare_credentials", return_value=CREDS), \
            patch.object(cf, "_build_client", return_value=client), \
            patch.object(cf, "_get_enabled_zone_ids", return_value=None):
        return json.loads(cf.cloudflare_action(user_id="u1", **kwargs))


def test_firewall_rules_zone_custom_rules_plus_account_level():
    client = FakeClient(
        zone_phases={("z1", PHASE_FIREWALL_CUSTOM): {"id": "rs-z", "rules": [
            {"id": "r1", "action": "block", "expression": "ip.src eq 1.1.1.1",
             "description": "bad ip", "enabled": False, "last_updated": "2026-01-01"},
        ]}},
        account_phases={
            ("a1", PHASE_FIREWALL_CUSTOM): {"id": "rs-a", "rules": [
                {"id": "x1", "action": "execute", "expression": "cf.zone.plan eq \"ENT\"",
                 "enabled": True, "action_parameters": {"id": "custom-1"}},
            ]},
            ("a2", PHASE_FIREWALL_CUSTOM): _http_error(403),
        },
        rulesets={("a1", "custom-1"): {"id": "custom-1", "name": "Org-wide blocks", "rules": [
            {"id": "c1", "action": "block", "expression": "cf.threat_score gt 50", "enabled": True},
        ]}},
    )
    out = _query(client, resource_type="firewall_rules", zone_id="z1")
    assert out["success"] is True
    assert out["ruleset_id"] == "rs-z"
    assert out["count"] == out["total"] == 1
    rule = out["results"][0]
    assert rule["id"] == "r1" and rule["expression"] == "ip.src eq 1.1.1.1"
    assert rule["enabled"] is False and rule["paused"] is True
    a1, a2 = out["account_level"]
    assert a1["account_id"] == "a1"
    assert a1["rules"][0]["ruleset_id"] == "custom-1"
    assert a1["rules"][0]["ruleset_name"] == "Org-wide blocks"
    assert a1["rules"][0]["rules"][0]["id"] == "c1"
    assert a2["rules"] == [] and "Account WAF Read" in a2["note"]


def test_rate_limits_expose_the_ratelimit_block():
    client = FakeClient(zone_phases={("z1", PHASE_RATELIMIT): {"id": "rs-rl", "rules": [
        {"id": "rl1", "action": "block", "expression": "http.request.uri.path eq \"/login\"",
         "enabled": True, "description": "login limiter",
         "ratelimit": {"characteristics": ["ip.src", "cf.colo.id"], "period": 60,
                       "requests_per_period": 100, "mitigation_timeout": 600}},
    ]}})
    out = _query(client, resource_type="rate_limits", zone_id="z1")
    assert out["success"] is True
    rl = out["results"][0]["ratelimit"]
    assert rl["requests_per_period"] == 100 and rl["period"] == 60
    assert rl["characteristics"] == ["ip.src", "cf.colo.id"]
    assert out["results"][0]["action"] == "block"


def test_phase_never_deployed_reads_as_zero_rules():
    out = _query(FakeClient(), resource_type="rate_limits", zone_id="z1")
    assert out["success"] is True
    assert out["count"] == 0 and out["total"] == 0 and out["results"] == []


def test_managed_rules_show_ruleset_name_and_overrides():
    client = FakeClient(
        zone_phases={("z1", PHASE_FIREWALL_MANAGED): {"id": "rs-m", "rules": [
            {"id": "m1", "action": "execute", "expression": "true", "enabled": True,
             "action_parameters": {"id": "managed-1", "overrides": {"action": "log"}}},
        ]}},
        rulesets={("z1", "managed-1"): {"id": "managed-1", "name": "Cloudflare Managed Ruleset",
                                          "kind": "managed", "phase": PHASE_FIREWALL_MANAGED}},
    )
    out = _query(client, resource_type="managed_rules", zone_id="z1")
    assert out["results"][0]["managed_ruleset_name"] == "Cloudflare Managed Ruleset"
    assert out["results"][0]["overrides"] == {"action": "log"}
    assert "action_parameters" not in out["results"][0]
    assert out["available_managed_rulesets"] == [
        {"id": "managed-1", "name": "Cloudflare Managed Ruleset", "phase": PHASE_FIREWALL_MANAGED}]
    assert "note" not in out


def test_managed_rules_with_nothing_deployed_still_lists_what_is_available():
    """A Free zone: no entry point in the managed phase (Cloudflare answers 404)
    but the Managed Free Ruleset is offered. The agent must see it."""
    client = FakeClient(rulesets={
        ("z1", "free-1"): {"id": "free-1", "name": "Cloudflare Managed Free Ruleset",
                           "kind": "managed", "phase": PHASE_FIREWALL_MANAGED},
        ("z1", "custom-x"): {"id": "custom-x", "name": "default", "kind": "zone",
                             "phase": PHASE_FIREWALL_CUSTOM},
    })
    out = _query(client, resource_type="managed_rules", zone_id="z1")
    assert out["count"] == 0 and out["ruleset_id"] is None
    assert [a["name"] for a in out["available_managed_rulesets"]] == ["Cloudflare Managed Free Ruleset"]
    assert "available_managed_rulesets" in out["note"]


def test_cache_rules_served_from_the_cache_phase():
    client = FakeClient(zone_phases={("z1", PHASE_CACHE): {"id": "rs-c", "rules": [
        {"id": "c1", "action": "set_cache_settings", "expression": "starts_with(http.request.uri.path, \"/static\")",
         "enabled": True, "action_parameters": {"cache": True, "edge_ttl": {"mode": "override_origin", "default": 3600}}},
    ]}})
    out = _query(client, resource_type="cache_rules", zone_id="z1")
    assert out["phases"] == [PHASE_CACHE]
    assert out["results"][0]["phase"] == PHASE_CACHE
    assert out["results"][0]["action_parameters"]["edge_ttl"]["default"] == 3600


def test_page_rules_empty_points_at_the_successors():
    out = _query(FakeClient(page_rules=[]), resource_type="page_rules", zone_id="z1")
    assert out["count"] == 0
    assert "cache_rules" in out["note"]


def test_workers_span_every_account_and_survive_one_denial():
    client = FakeClient(workers={"a1": [{"id": "w1"}, {"id": "w2"}], "a2": _http_error(403)})
    out = _query(client, resource_type="workers")
    assert out["success"] is True
    assert [w["id"] for w in out["results"]] == ["w1", "w2"]
    assert out["results"][0]["account_name"] == "Acme"
    assert out["accounts_queried"] == 2
    assert out["account_errors"][0]["account_id"] == "a2"


def test_workers_all_accounts_denied_is_an_error():
    client = FakeClient(workers={"a1": _http_error(403), "a2": _http_error(403)})
    out = _query(client, resource_type="workers")
    assert "error" in out and out["results"] == []


def test_old_connections_with_only_account_id_still_work():
    creds = {"api_token": "t", "account_id": "a1", "account_name": "Acme"}
    client = FakeClient(workers={"a1": [{"id": "w1"}]})
    out = _query(client, creds=creds, resource_type="workers")
    assert [w["id"] for w in out["results"]] == ["w1"]
    assert out["accounts_queried"] == 1


def test_zones_are_not_filtered_to_one_account():
    client = FakeClient(zones=[
        {"id": "z1", "name": "a.com", "account": {"id": "a1", "name": "Acme"}},
        {"id": "z2", "name": "b.eu", "account": {"id": "a2", "name": "Acme EU"}},
    ])
    with patch.object(cf, "_get_cloudflare_credentials", return_value=CREDS), \
            patch.object(cf, "_build_client", return_value=client), \
            patch.object(cf, "_get_enabled_zone_ids", return_value=None):
        out = json.loads(cf.cloudflare_list_zones(user_id="u1"))
    assert ("zones", None) in client.calls
    assert [z["account_id"] for z in out["results"]] == ["a1", "a2"]


def test_limit_reports_the_total_and_is_capped():
    dns = [{"id": f"d{i}", "type": "A", "name": f"h{i}"} for i in range(3)]
    out = _query(FakeClient(dns=dns), resource_type="dns_records", zone_id="z1", limit=2)
    assert out["count"] == 2 and out["total"] == 3
    assert "Showing 2 of 3" in out["note"]
    out = _query(FakeClient(dns=dns), resource_type="dns_records", zone_id="z1", limit=10_000)
    assert out["count"] == 3 and "note" not in out


def test_toggle_firewall_rule_reports_the_custom_rule_state():
    client = FakeClient(toggle={"id": "r1", "enabled": False, "paused": True, "description": "bad ip"})
    out = _action(client, action_type="toggle_firewall_rule", zone_id="z1", rule_id="r1", paused=True)
    assert out["success"] is True and out["paused"] is True
    assert out["message"] == "WAF custom rule disabled (paused)."


def test_toggle_denied_names_the_waf_write_permission():
    client = FakeClient(toggle=_http_error(403))
    out = _action(client, action_type="toggle_firewall_rule", zone_id="z1", rule_id="r1", paused=True)
    assert "Zone WAF Write" in out["error"]
