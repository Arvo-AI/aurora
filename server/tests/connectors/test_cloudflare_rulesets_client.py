"""Cloudflare retired the Firewall Rules API and the previous Rate Limiting API
on 2025-06-15 (both answer 410 Gone). The client must read every rule type
through the Rulesets API, and must see every account a token spans."""
from unittest.mock import MagicMock

import pytest
import requests

from connectors.cloudflare_connector.api_client import (
    CloudflareAPIError,
    CloudflareClient,
    PHASE_FIREWALL_CUSTOM,
    PHASE_RATELIMIT,
)


def _http_error(status: int) -> requests.exceptions.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = b'{"success": false}'
    return requests.exceptions.HTTPError(response=resp)


def _client(side_effect=None, return_value=None) -> CloudflareClient:
    client = CloudflareClient("token")
    client._request = MagicMock(side_effect=side_effect, return_value=return_value)
    return client


def test_list_accounts_paginates_instead_of_first_only():
    client = _client(side_effect=[
        {"result": [{"id": "a1"}], "result_info": {"total_pages": 2}},
        {"result": [{"id": "a2"}], "result_info": {"total_pages": 2}},
    ])
    assert [a["id"] for a in client.list_accounts()] == ["a1", "a2"]
    first = client._request.call_args_list[0]
    assert first.args[:2] == ("GET", "/accounts")
    assert first.kwargs["params"] == {"per_page": 50, "page": 1}


def test_get_zone_returns_the_zone_with_its_account():
    client = _client(return_value={"result": {"id": "z1", "account": {"id": "a1", "name": "Acme"}}})
    zone = client.get_zone("z1")
    assert zone["account"] == {"id": "a1", "name": "Acme"}
    assert client._request.call_args.args[:2] == ("GET", "/zones/z1")


def test_firewall_rules_come_from_custom_rules_phase_entrypoint():
    client = _client(return_value={
        "result": {"id": "rs1", "rules": [{"id": "r1", "enabled": False, "expression": "ip.src eq 1.1.1.1"}]},
    })
    assert client.list_firewall_rules("z1") == [
        {"id": "r1", "enabled": False, "expression": "ip.src eq 1.1.1.1"}]
    method, path = client._request.call_args.args[:2]
    assert (method, path) == ("GET", f"/zones/z1/rulesets/phases/{PHASE_FIREWALL_CUSTOM}/entrypoint")


def test_rate_limits_come_from_ratelimit_phase_entrypoint():
    client = _client(return_value={"result": {"id": "rs2", "rules": [{"id": "rl1"}]}})
    assert client.list_rate_limits("z1") == [{"id": "rl1"}]
    assert client._request.call_args.args[1] == f"/zones/z1/rulesets/phases/{PHASE_RATELIMIT}/entrypoint"


def test_retired_endpoints_are_never_called():
    client = _client(return_value={"result": {"id": "rs", "rules": []}})
    client.list_firewall_rules("z1")
    client.list_rate_limits("z1")
    paths = [c.args[1] for c in client._request.call_args_list]
    assert not any("/firewall/rules" in p or "/rate_limits" in p for p in paths)


def test_phase_without_a_ruleset_is_empty_not_an_error():
    """Cloudflare answers 404 for a phase nothing was ever deployed in."""
    client = _client(side_effect=_http_error(404))
    assert client.list_rate_limits("z1") == []
    assert client.get_phase_entrypoint("zones", "z1", PHASE_FIREWALL_CUSTOM) is None


def test_other_http_errors_still_propagate():
    client = _client(side_effect=_http_error(403))
    with pytest.raises(requests.exceptions.HTTPError):
        client.list_firewall_rules("z1")


def test_toggle_resends_the_whole_rule_with_enabled_flipped():
    rule = {"id": "r1", "version": "3", "action": "block", "expression": "ip.src eq 1.1.1.1",
            "description": "block bad ip", "enabled": True, "last_updated": "2026-01-01", "ref": "r1"}
    client = _client(side_effect=[
        {"result": {"id": "rs1", "rules": [rule]}},
        {"result": {"id": "rs1", "rules": [dict(rule, enabled=False)]}},
    ])
    out = client.update_firewall_rule_paused("z1", "r1", paused=True)
    patch_call = client._request.call_args_list[1]
    assert patch_call.args[:2] == ("PATCH", "/zones/z1/rulesets/rs1/rules/r1")
    # version / last_updated / id are read-only and must not be re-sent
    assert patch_call.kwargs["json_data"] == {
        "action": "block", "description": "block bad ip", "enabled": False,
        "expression": "ip.src eq 1.1.1.1", "ref": "r1",
    }
    assert out == {"id": "r1", "enabled": False, "paused": True, "description": "block bad ip"}


def test_toggle_of_a_rule_not_in_the_zone_raises():
    client = _client(return_value={"result": {"id": "rs1", "rules": []}})
    with pytest.raises(CloudflareAPIError):
        client.update_firewall_rule_paused("z1", "missing", paused=True)
    assert all(c.args[0] == "GET" for c in client._request.call_args_list)
