"""End-to-end ACS checks with real signed SAML responses (routes/sso_routes.py).

A throwaway IdP key signs each assertion, so these exercise python3-saml's
actual signature, audience, destination and InResponseTo validation.
"""

from __future__ import annotations

import base64
import datetime as dt
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("onelogin.saml2.auth")
pytest.importorskip("cryptography")

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from cryptography.x509.oid import NameOID  # noqa: E402

BACKEND = "https://aurora.example.com"
IDP_ENTITY = "https://idp.example.org/tenant"
ACME, OTHER = "org-acme", "org-other"


def _keypair(cn="idp.example.org"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption(),
    ).decode()
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode()


IDP_KEY, IDP_CERT = _keypair()
ROGUE_KEY, ROGUE_CERT = _keypair("rogue.example.net")


def _ts(delta_min=0):
    t = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=delta_min)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _signed_response(*, org_id=ACME, in_response_to="rid-1", email="first.last@example.com",
                     key=IDP_KEY, cert=IDP_CERT, tamper=None):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    acs = f"{BACKEND}/api/auth/saml/acs/{org_id}"
    audience = f"{BACKEND}/api/auth/saml/metadata/{org_id}"
    assertion = f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xs="http://www.w3.org/2001/XMLSchema" ID="_assert1" Version="2.0" IssueInstant="{_ts()}"><saml:Issuer>{IDP_ENTITY}</saml:Issuer><saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID><saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData InResponseTo="{in_response_to}" NotOnOrAfter="{_ts(5)}" Recipient="{acs}"/></saml:SubjectConfirmation></saml:Subject><saml:Conditions NotBefore="{_ts(-1)}" NotOnOrAfter="{_ts(5)}"><saml:AudienceRestriction><saml:Audience>{audience}</saml:Audience></saml:AudienceRestriction></saml:Conditions><saml:AuthnStatement AuthnInstant="{_ts()}" SessionIndex="_sess1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement><saml:AttributeStatement><saml:Attribute Name="http://schemas.microsoft.com/identity/claims/objectidentifier"><saml:AttributeValue xsi:type="xs:string">oid-123</saml:AttributeValue></saml:Attribute><saml:Attribute Name="http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress"><saml:AttributeValue xsi:type="xs:string">{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement></saml:Assertion>"""
    signed = OneLogin_Saml2_Utils.add_sign(
        assertion, key, cert, sign_algorithm=C.RSA_SHA256, digest_algorithm=C.SHA256,
    )
    signed = signed.decode() if isinstance(signed, bytes) else signed
    signed = signed.split("?>", 1)[1] if signed.startswith("<?xml") else signed
    if tamper:
        signed = tamper(signed)
    response = f"""<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_resp1" Version="2.0" IssueInstant="{_ts()}" Destination="{acs}" InResponseTo="{in_response_to}"><saml:Issuer>{IDP_ENTITY}</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>{signed}</samlp:Response>"""
    return base64.b64encode(response.encode()).decode()


@pytest.fixture
def saml_app(monkeypatch):
    monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", BACKEND)
    monkeypatch.setenv("FRONTEND_URL", "https://app.example.com")
    for m in [m for m in sys.modules if m.startswith(("routes.sso_routes", "utils.auth.rbac"))]:
        del sys.modules[m]
    sys.modules.setdefault("routes.audit_routes", MagicMock())

    from flask import Flask
    from routes import sso_routes as mod

    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(mod.saml_bp)

    config = {"idp_entity_id": IDP_ENTITY, "idp_sso_url": "https://idp.example.org/sso",
              "idp_x509_cert": IDP_CERT, "default_role": "viewer"}
    resolve = MagicMock(return_value=("uid-1", False))
    with patch.object(mod, "db_pool", MagicMock()), \
         patch.object(mod.sso, "load_enabled_config", return_value=config), \
         patch.object(mod.sso, "burn_request_id", return_value=True), \
         patch.object(mod.sso, "resolve_user", resolve), \
         patch.object(mod, "mint_handoff", return_value="handoff-tok"):
        yield app, mod, resolve


def _post(app, org_id=ACME, saml_response=None, cookie="rid-1"):
    client = app.test_client()
    if cookie:
        client.set_cookie("aurora_saml_rid", cookie, domain="localhost", path="/api/auth/saml")
    return client.post(f"/api/auth/saml/acs/{org_id}", data={"SAMLResponse": saml_response or _signed_response()})


class TestAcs:
    def test_valid_response_logs_in_via_handoff(self, saml_app):
        app, _, resolve = saml_app
        resp = _post(app)
        assert resp.status_code == 302
        assert resp.location == "https://app.example.com/sign-in?handoff=handoff-tok&via=sso"
        identity = resolve.call_args.args[2]
        assert identity["subject"] == "oid-123"
        assert identity["email"] == "first.last@example.com"

    def test_tampered_email_breaks_signature(self, saml_app):
        app, _, resolve = saml_app
        forged = _signed_response(tamper=lambda x: x.replace("first.last@example.com", "admin@example.com"))
        resp = _post(app, saml_response=forged)
        assert "error=sso_invalid_response" in resp.location
        resolve.assert_not_called()

    def test_signed_by_wrong_key_is_rejected(self, saml_app):
        app, _, resolve = saml_app
        resp = _post(app, saml_response=_signed_response(key=ROGUE_KEY, cert=ROGUE_CERT))
        assert "error=sso_invalid_response" in resp.location
        resolve.assert_not_called()

    def test_response_for_one_org_cannot_be_posted_to_another(self, saml_app):
        # Even when both orgs trust the same IdP certificate, audience and
        # destination pin a response to the org that requested it.
        app, _, resolve = saml_app
        resp = _post(app, org_id=OTHER, saml_response=_signed_response(org_id=ACME))
        assert "error=sso_invalid_response" in resp.location
        resolve.assert_not_called()

    def test_response_to_a_different_request_is_rejected(self, saml_app):
        app, _, resolve = saml_app
        resp = _post(app, saml_response=_signed_response(in_response_to="rid-attacker"), cookie="rid-1")
        assert "error=sso_invalid_response" in resp.location
        resolve.assert_not_called()

    def test_unsolicited_response_without_cookie_is_rejected(self, saml_app):
        app, _, resolve = saml_app
        resp = _post(app, cookie=None)
        assert "error=sso_expired" in resp.location
        resolve.assert_not_called()

    def test_replayed_request_id_is_rejected(self, saml_app):
        app, mod, resolve = saml_app
        with patch.object(mod.sso, "burn_request_id", return_value=False):
            resp = _post(app)
        assert "error=sso_expired" in resp.location
        resolve.assert_not_called()

    def test_refusal_code_is_forwarded(self, saml_app):
        app, mod, resolve = saml_app
        resolve.side_effect = mod.sso.SsoError("account_in_other_org")
        resp = _post(app)
        assert "error=sso_account_in_other_org" in resp.location

    def test_rolled_over_cert_bundle_accepts_new_key(self, saml_app):
        # Mid-rollover the org stores old + new certs; a response signed with
        # either must verify.
        app, mod, resolve = saml_app
        bundle = f"{ROGUE_CERT}\n{IDP_CERT}"
        config = {"idp_entity_id": IDP_ENTITY, "idp_sso_url": "https://idp.example.org/sso",
                  "idp_x509_cert": bundle, "default_role": "viewer"}
        with patch.object(mod.sso, "load_enabled_config", return_value=config):
            resp = _post(app)
        assert "handoff=handoff-tok" in resp.location


class TestMetadataAndConfigParsing:
    def test_metadata_is_valid_sp_xml(self, saml_app):
        app, mod, _ = saml_app
        cur = mod.db_pool.get_admin_connection.return_value.__enter__.return_value.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = (1,)
        resp = app.test_client().get(f"/api/auth/saml/metadata/{ACME}")
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert f'entityID="{BACKEND}/api/auth/saml/metadata/{ACME}"' in body
        assert f'Location="{BACKEND}/api/auth/saml/acs/{ACME}"' in body

    def test_manual_fields_round_trip_a_cert_bundle(self, saml_app):
        _, mod, _ = saml_app
        fields, error = mod._parse_idp_fields({
            "idpEntityId": IDP_ENTITY, "idpSsoUrl": "https://idp.example.org/sso",
            "idpX509Cert": f"{IDP_CERT}\n{ROGUE_CERT}",
        })
        assert error is None
        assert len(mod.sso.split_certs(fields["idp_x509_cert"])) == 2

    def test_bare_base64_cert_is_accepted(self, saml_app):
        _, mod, _ = saml_app
        bare = "".join(IDP_CERT.splitlines()[1:-1])
        fields, error = mod._parse_idp_fields({
            "idpEntityId": IDP_ENTITY, "idpSsoUrl": "https://idp.example.org/sso", "idpX509Cert": bare,
        })
        assert error is None
        assert fields["idp_x509_cert"].startswith("-----BEGIN CERTIFICATE-----")

    @pytest.mark.parametrize("body", [
        {"idpEntityId": IDP_ENTITY, "idpSsoUrl": "http://idp.example.org/sso", "idpX509Cert": IDP_CERT},
        {"idpEntityId": IDP_ENTITY, "idpSsoUrl": "https://idp.example.org/sso", "idpX509Cert": "not-a-cert"},
        {"idpEntityId": ["x"], "idpSsoUrl": "https://idp.example.org/sso", "idpX509Cert": IDP_CERT},
    ])
    def test_invalid_fields_are_rejected(self, saml_app, body):
        _, mod, _ = saml_app
        fields, error = mod._parse_idp_fields(body)
        assert fields is None
        assert error

    @pytest.mark.parametrize("idp_url, allowed", [
        ("http://localhost:8080/realms/dev/protocol/saml", True),
        ("https://localhost:8443/realms/dev/protocol/saml", False),
        ("https://login.example.net/saml2", False),
        ("http://idp.example.org/sso", False),
    ])
    def test_http_backend_only_accepts_same_host_idp(self, saml_app, monkeypatch, idp_url, allowed):
        # A cross-site IdP's POST would drop the SameSite=Lax request cookie.
        _, mod, _ = saml_app
        # A configured tunnel would make the public URL https and flip this rule.
        monkeypatch.delenv("NGROK_URL", raising=False)
        monkeypatch.setenv("NEXT_PUBLIC_BACKEND_URL", "http://localhost:5080")
        assert mod._sso_url_allowed(idp_url) is allowed


class TestDnsVerification:
    def test_non_ascii_txt_record_does_not_crash(self, monkeypatch):
        from types import SimpleNamespace

        import dns.resolver
        from utils.auth import saml_sso as sso

        answers = [SimpleNamespace(strings=["café".encode()]),
                   SimpleNamespace(strings=[b"aurora-sso-verification=", b"tok123"])]
        monkeypatch.setattr(dns.resolver, "resolve", lambda *a, **kw: answers)
        assert sso.domain_has_txt_token("example.com", "tok123") is True
        assert sso.domain_has_txt_token("example.com", "other") is False
