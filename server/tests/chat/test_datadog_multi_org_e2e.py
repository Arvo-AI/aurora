"""End-to-end check of the multi-organization Datadog flow.

Drives the real /connect, /status, /disconnect and query_datadog code paths with
a fake Vault (an in-memory blob) and a fake Datadog API, verifying the sequence
a user actually performs:

    connect prod -> connect dev -> agent discovers both -> agent queries dev
    -> remove dev -> remove last org

The regression this pins is the one that motivated the feature: before it, the
second /connect overwrote the first org's credentials, so connecting dev
silently destroyed prod.

Run:  python -m pytest server/tests/chat/test_datadog_multi_org_e2e.py -s
"""

import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_server_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _server_dir not in sys.path:
    sys.path.insert(0, _server_dir)

from flask import Flask  # noqa: E402

from routes.datadog import datadog_routes  # noqa: E402


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

VAULT = {}                      # the single Datadog secret blob
VALID_KEYS = {"api-prod", "api-dev"}   # keys the fake Datadog accepts
QUERIED = []                    # (api_key, path) per outbound call


def fake_store(user_id, token_data, provider, **kwargs):
    assert provider == "datadog"
    VAULT["blob"] = json.loads(json.dumps(token_data))  # deep copy, like a round-trip


def fake_read(user_id):
    return VAULT.get("blob")


def fake_request(self, method, path, **kwargs):
    QUERIED.append((self.api_key, path))
    if path == "/api/v1/validate":
        if self.api_key not in VALID_KEYS:
            raise datadog_routes.DatadogAPIError("403 Forbidden")
        return SimpleNamespace(json=lambda: {"valid": True})
    if path == "/api/v1/org":
        # Mirrors the real payload: the org is nested under an "org" key and its
        # identifier is public_id, not id. A flat {name, id} fake here would let
        # broken envelope handling pass, which is exactly what it did before.
        prod = self.api_key.startswith("api-prod")
        return SimpleNamespace(json=lambda: {
            "org": {
                "name": "Acme Prod" if prod else "Acme Dev",
                # Identity is the org's, not the key pair's, so it survives rotation.
                "public_id": "org-prod" if prod else "org-dev",
            }
        })
    if path == "/api/v2/logs/events/search":
        # Each org returns its own data: the whole point of selecting one.
        env = "prod" if self.api_key.startswith("api-prod") else "dev"
        return SimpleNamespace(json=lambda: {"data": [{"attributes": {"env": env}}]})
    raise AssertionError(f"unexpected path {path}")


def _labels_in_vault():
    return [datadog_routes._account_label(a) for a in VAULT["blob"]["accounts"]]


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}{f' -- {detail}' if detail else ''}")
    assert condition, f"{label}{f': {detail}' if detail else ''}"


def test_multi_org_flow():
    app = Flask(__name__)
    app.register_blueprint(datadog_routes.datadog_bp)

    patches = [
        patch.object(datadog_routes, "store_tokens_in_db", fake_store),
        patch.object(datadog_routes, "_get_stored_datadog_credentials", fake_read),
        patch.object(datadog_routes.DatadogClient, "_request", fake_request),
        # RBAC decorator injects user_id; bypass auth for the harness.
        patch.object(datadog_routes, "delete_user_secret", lambda u, p: (True, 1)),
    ]
    for p in patches:
        p.start()

    from chat.backend.agent.tools import datadog_tool

    print("\n1. Connect the prod organization")
    with app.test_request_context(json={"apiKey": "api-prod", "appKey": "app-prod",
                                       "site": "datadoghq.com", "label": "prod"}):
        body = json.loads(datadog_routes.connect.__wrapped__("u1").get_data())
    check("prod connected", body["success"])
    check("one organization stored", len(body["accounts"]) == 1, str(body["accounts"]))

    print("\n2. Connect the dev organization (the old overwrite bug)")
    with app.test_request_context(json={"apiKey": "api-dev", "appKey": "app-dev",
                                       "site": "datadoghq.eu", "label": "dev"}):
        body = json.loads(datadog_routes.connect.__wrapped__("u1").get_data())
    labels = [a["label"] for a in body["accounts"]]
    check("both organizations stored", labels == ["prod", "dev"], str(labels))
    check("prod credentials survived",
          any(a["api_key"] == "api-prod" for a in VAULT["blob"]["accounts"]))
    check("prod still mirrored at top level", VAULT["blob"]["api_key"] == "api-prod")
    check("dev kept its own site",
          [a["site"] for a in VAULT["blob"]["accounts"]] == ["datadoghq.com", "datadoghq.eu"])

    print("\n3. Status reports both, validated independently")
    with app.test_request_context():
        body = json.loads(datadog_routes.status.__wrapped__("u1").get_data())
    check("connected", body["connected"] is True)
    check("two organizations listed", len(body["accounts"]) == 2)
    check("both valid", all(a["valid"] for a in body["accounts"]))
    check("no credentials leaked into the response",
          "api_key" not in json.dumps(body), "response contains api_key")

    print("\n4. A revoked prod key must not hide the healthy dev org")
    VALID_KEYS.discard("api-prod")
    with app.test_request_context():
        body = json.loads(datadog_routes.status.__wrapped__("u1").get_data())
    check("still connected overall", body["connected"] is True)
    check("prod flagged invalid", body["accounts"][0]["valid"] is False)
    check("dev still valid", body["accounts"][1]["valid"] is True)
    VALID_KEYS.add("api-prod")

    print("\n5. Agent discovers the organizations")
    check("tool is registered", datadog_tool.is_datadog_connected("u1") is True)
    out = json.loads(datadog_tool.query_datadog(resource_type="accounts", user_id="u1"))
    check("accounts listed", [r["label"] for r in out["results"]] == ["prod", "dev"])
    check("default named", out["default_account"] == "prod")
    before = len(QUERIED)
    json.loads(datadog_tool.query_datadog(resource_type="accounts", user_id="u1"))
    check("discovery makes no Datadog API calls", len(QUERIED) == before)

    print("\n6. Agent queries a specific organization")
    out = json.loads(datadog_tool.query_datadog(resource_type="logs", query="status:error",
                                               account="dev", user_id="u1"))
    check("dev data returned", out["results"][0]["attributes"]["env"] == "dev")
    check("answering org named in the result", out["account"] == "dev")
    check("dev credentials used", QUERIED[-1][0] == "api-dev", str(QUERIED[-1]))

    out = json.loads(datadog_tool.query_datadog(resource_type="logs", query="status:error",
                                               user_id="u1"))
    check("no selector falls back to primary", out["account"] == "prod")

    out = json.loads(datadog_tool.query_datadog(resource_type="logs", account="staging",
                                               user_id="u1"))
    check("unknown org errors instead of silently using prod", "error" in out, str(out))
    check("error lists the real organizations", "prod" in out["error"] and "dev" in out["error"])

    print("\n6b. A different org claiming a taken label is refused, not merged")
    # Simulates the blank-label case: org_name is None so the label falls through
    # to the site, colliding with an already-connected org on the same site.
    with app.test_request_context(json={"apiKey": "api-prod", "appKey": "app-prod",
                                       "site": "datadoghq.com", "label": "dev"}):
        response = datadog_routes.connect.__wrapped__("u1")
    body, code = json.loads(response[0].get_data()), response[1]
    check("rejected with 409", code == 409, str(code))
    check("names the conflicting label", body["conflictingLabel"] == "dev")
    check("nothing was overwritten",
          [a["api_key"] for a in VAULT["blob"]["accounts"]] == ["api-prod", "api-dev"])

    print("\n6c. Re-connecting the SAME org rotates its keys in place")
    VALID_KEYS.add("api-dev-rotated")
    with app.test_request_context(json={"apiKey": "api-dev-rotated", "appKey": "app-dev2",
                                       "site": "datadoghq.eu", "label": "dev"}):
        body = json.loads(datadog_routes.connect.__wrapped__("u1").get_data())
    check("reported as a replacement", body["replaced"] is True)
    check("still two organizations", len(body["accounts"]) == 2, str(len(body["accounts"])))
    check("key rotated", VAULT["blob"]["accounts"][1]["api_key"] == "api-dev-rotated")
    check("prod remained primary (position preserved)",
          _labels_in_vault() == ["prod", "dev"], str(_labels_in_vault()))
    out = json.loads(datadog_tool.query_datadog(resource_type="logs", user_id="u1"))
    check("unqualified query still hits prod", out["account"] == "prod")
    # Restore for the removal steps below.
    VALID_KEYS.discard("api-dev-rotated")
    VAULT["blob"]["accounts"][1]["api_key"] = "api-dev"
    VALID_KEYS.add("api-dev")

    print("\n7. Remove one organization")
    with app.test_request_context("/?account=dev"):
        body = json.loads(datadog_routes.disconnect.__wrapped__("u1").get_data())
    check("dev removed", [a["label"] for a in body["accounts"]] == ["prod"])
    check("prod credentials intact", VAULT["blob"]["api_key"] == "api-prod")
    out = json.loads(datadog_tool.query_datadog(resource_type="accounts", user_id="u1"))
    check("agent sees only prod", [r["label"] for r in out["results"]] == ["prod"])

    print("\n8. Remove the last organization tears the provider down")
    db = MagicMock()
    db.get_admin_connection.return_value.__enter__.return_value.cursor.return_value.rowcount = 3
    with patch.object(datadog_routes, "db_pool", db), \
         patch.object(datadog_routes, "set_rls_context", MagicMock()):
        with app.test_request_context("/?account=prod"):
            body = json.loads(datadog_routes.disconnect.__wrapped__("u1").get_data())
    check("full teardown reported", body["accounts"] == [] and body["tokensDeleted"] == 1, str(body))
    check("ingested events purged", body["eventsDeleted"] == 3, str(body))

    print("\n9. A pre-existing single-org blob still works (no reconnect needed)")
    VAULT["blob"] = {"api_key": "api-prod", "app_key": "app-prod",
                     "site": "datadoghq.com", "org_name": "Acme Prod"}
    check("still connected", datadog_tool.is_datadog_connected("u1") is True)
    out = json.loads(datadog_tool.query_datadog(resource_type="accounts", user_id="u1"))
    check("labelled from org_name", [r["label"] for r in out["results"]] == ["Acme Prod"])
    out = json.loads(datadog_tool.query_datadog(resource_type="logs", user_id="u1"))
    check("queryable", out["results"][0]["attributes"]["env"] == "prod")

    for p in patches:
        p.stop()

    print("\nAll checks passed.\n")
