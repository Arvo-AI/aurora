"""SAML SSO: public login flow (``/api/auth/saml``) and org admin settings.

Only SP-initiated logins are accepted: each response must answer a request we
issued to this browser (cookie) for this org (DB row, burned on use). IdP
dashboard tiles work by setting the app's sign-on URL to /login/<org_id>.
"""

import logging
import os
import re
from urllib.parse import urlparse

import flask
import psycopg2
from flask import Blueprint, jsonify, request

from utils.auth import VALID_ROLES
from utils.auth import saml_sso as sso
from utils.auth.handoff import mint_handoff
from utils.auth.rbac_decorators import require_permission
from utils.auth.stateless_auth import get_org_id_from_request
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_SAML_PREFIX = "/api/auth/saml"
_DOMAIN_TAKEN = "This domain is already verified by another organization"

saml_bp = Blueprint("saml_sso", __name__, url_prefix=_SAML_PREFIX)
# Separate so it can be rate limited per email: it's reached via the Next
# proxy, where a per-IP key would put every caller in one bucket.
saml_discover_bp = Blueprint("saml_sso_discover", __name__, url_prefix=_SAML_PREFIX)
org_sso_bp = Blueprint("org_sso", __name__, url_prefix="/api/orgs/sso")

FRONTEND_URL = os.getenv("FRONTEND_URL") or ""
_REQUEST_COOKIE = "aurora_saml_rid"
_ORG_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_MAX_METADATA_BYTES = 256 * 1024


def _fail(code: str) -> flask.Response:
    """Redirect to sign-in with a fixed error code the frontend maps to text."""
    resp = flask.redirect(f"{FRONTEND_URL}/sign-in?error=sso_{code}", code=302)
    resp.delete_cookie(_REQUEST_COOKIE, path=_SAML_PREFIX)
    return resp


def _cookie_flags() -> dict:
    # The ACS is a cross-site POST from the IdP, which only carries
    # SameSite=None cookies — and browsers only accept those when Secure.
    # Plain-http dev (IdP on localhost) is same-site, so Lax suffices there.
    https = sso.backend_is_https()
    return {"secure": https, "samesite": "None" if https else "Lax", "httponly": True}


def _saml_auth(org_id: str, config: dict | None):
    from onelogin.saml2.auth import OneLogin_Saml2_Auth

    return OneLogin_Saml2_Auth(sso.request_data(request), sso.build_settings(org_id, config))


# ---------------------------------------------------------------------------
# Public login flow
# ---------------------------------------------------------------------------

@saml_discover_bp.route("/discover", methods=["POST"])
def discover():
    data = request.get_json(silent=True)
    email = data.get("email") if isinstance(data, dict) else None
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            org_id = sso.find_org_for_email(cur, email or "")
    except Exception:
        logger.exception("[SSO] discovery failed")
        return jsonify({"error": "SSO lookup failed"}), 500

    if not org_id:
        return jsonify({"error": "SSO is not set up for this email domain"}), 404
    return jsonify({"loginUrl": sso.sp_urls(org_id)["loginUrl"]})


@saml_bp.route("/login/<org_id>", methods=["GET"])
def login(org_id):
    if not _ORG_ID_RE.match(org_id):
        return _fail("not_configured")
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            config = sso.load_enabled_config(cur, org_id)
            if not config:
                return _fail("not_configured")
            auth = _saml_auth(org_id, config)
            redirect_url = auth.login()
            request_id = auth.get_last_request_id()
            sso.store_request_id(cur, request_id, org_id)
            conn.commit()
    except Exception:
        logger.exception("[SSO] could not start login for org=%s", sanitize(org_id))
        return _fail("internal")

    resp = flask.redirect(redirect_url, code=302)
    resp.set_cookie(
        _REQUEST_COOKIE, request_id, max_age=sso.SAML_REQUEST_TTL_SEC,
        path=_SAML_PREFIX, **_cookie_flags(),
    )
    return resp


@saml_bp.route("/acs/<org_id>", methods=["POST"])
def acs(org_id):
    if not _ORG_ID_RE.match(org_id):
        return _fail("not_configured")
    request_id = request.cookies.get(_REQUEST_COOKIE)
    # No cookie: IdP-initiated, expired, or a different browser than started it.
    if not request_id:
        return _fail("expired")

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            config = sso.load_enabled_config(cur, org_id)
            conn.commit()
    except Exception:
        logger.exception("[SSO] config load failed for org=%s", sanitize(org_id))
        return _fail("internal")
    if not config:
        return _fail("not_configured")

    auth = _saml_auth(org_id, config)
    try:
        # Validates signature, issuer, audience, destination, timestamps and
        # that InResponseTo matches the request this browser started.
        auth.process_response(request_id=request_id)
    except Exception:
        logger.exception("[SSO] malformed SAML response for org=%s", sanitize(org_id))
        return _fail("invalid_response")
    if auth.get_errors() or not auth.is_authenticated():
        logger.warning(
            "[SSO] rejected SAML response for org=%s: %s (%s)",
            sanitize(org_id), sanitize(",".join(auth.get_errors())),
            sanitize(auth.get_last_error_reason() or ""),
        )
        return _fail("invalid_response")

    identity = sso.extract_identity(auth)
    refusal = None
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            # Committed on its own so a refused response stays burned.
            burned = sso.burn_request_id(cur, request_id, org_id)
            conn.commit()
            if not burned:
                return _fail("expired")
            try:
                user_id, created = sso.resolve_user(cur, org_id, identity, config["default_role"])
                handoff_token = mint_handoff(cur, user_id)
                conn.commit()
            except sso.SsoError as e:
                conn.rollback()
                refusal = e.code
    except Exception:
        logger.exception("[SSO] provisioning failed for org=%s", sanitize(org_id))
        return _fail("internal")

    if refusal:
        logger.info("[SSO] login refused for org=%s: %s", sanitize(org_id), refusal)
        _audit(org_id, "", "login_failed", "session", {"via": "saml", "reason": refusal})
        return _fail(refusal)

    if created:
        try:
            from utils.auth.enforcer import assign_role_to_user

            assign_role_to_user(user_id, config["default_role"], org_id)
        except Exception:
            logger.warning("[SSO] Casbin role assignment failed for user=%s", user_id, exc_info=True)
        _audit(org_id, user_id, "register", "user", {"via": "saml"})
    _audit(org_id, user_id, "login", "session", {"via": "saml"})

    resp = flask.redirect(f"{FRONTEND_URL}/sign-in?handoff={handoff_token}&via=sso", code=302)
    resp.delete_cookie(_REQUEST_COOKIE, path=_SAML_PREFIX)
    return resp


@saml_bp.route("/metadata/<org_id>", methods=["GET"])
def metadata(org_id):
    from onelogin.saml2.settings import OneLogin_Saml2_Settings

    if not _ORG_ID_RE.match(org_id):
        return jsonify({"error": "Not found"}), 404
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 FROM organizations WHERE id = %s", (org_id,))
            exists = cur.fetchone() is not None
    except Exception:
        logger.exception("[SSO] metadata org lookup failed")
        return jsonify({"error": "Metadata unavailable"}), 500
    if not exists:
        return jsonify({"error": "Not found"}), 404

    settings = OneLogin_Saml2_Settings(sso.build_settings(org_id, None), sp_validation_only=True)
    xml = settings.get_sp_metadata()
    return flask.Response(xml, mimetype="application/samlmetadata+xml")


def _audit(org_id, user_id, action, resource_type, detail, resource_id=None):
    try:
        from routes.audit_routes import record_audit_event

        record_audit_event(org_id, user_id, action, resource_type, resource_id or user_id or None, detail, request)
    except Exception:
        logger.warning("[SSO] audit event failed", exc_info=True)


# ---------------------------------------------------------------------------
# Org admin settings
# ---------------------------------------------------------------------------

def _serialize_domain(row) -> dict:
    domain_id, domain, token, verified_at = row
    return {
        "id": domain_id,
        "domain": domain,
        "verified": verified_at is not None,
        "verifiedAt": verified_at.isoformat() if verified_at else None,
        "dnsRecord": sso.dns_txt_record(domain, token),
    }


def _load_domains(cur, org_id: str) -> list:
    cur.execute(
        """SELECT id, domain, verification_token, verified_at
             FROM org_sso_domains WHERE org_id = %s ORDER BY created_at""",
        (org_id,),
    )
    return [_serialize_domain(r) for r in cur.fetchall()]


@org_sso_bp.route("", methods=["GET"])
@require_permission("org", "manage")
def get_sso_settings(user_id):
    return _settings_response(get_org_id_from_request())


def _settings_response(org_id: str):
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT idp_entity_id, idp_sso_url, idp_x509_cert, default_role,
                          enabled, require_sso
                     FROM org_sso_configs WHERE org_id = %s""",
                (org_id,),
            )
            row = cur.fetchone()
            domains = _load_domains(cur, org_id)
    except Exception:
        logger.exception("[SSO] failed to load settings")
        return jsonify({"error": "Failed to load SSO settings"}), 500

    config = None
    if row:
        config = {
            "idpEntityId": row[0],
            "idpSsoUrl": row[1],
            "idpX509Cert": row[2],
            "defaultRole": row[3],
            "enabled": row[4],
            "requireSso": row[5],
        }
    return jsonify({
        "config": config,
        "domains": domains,
        "serviceProvider": sso.sp_urls(org_id),
        "domainVerificationRequired": not sso.skip_domain_verification(),
    })


def _idp_from_metadata(xml: str) -> tuple[dict | None, list, str | None]:
    if len(xml.encode()) > _MAX_METADATA_BYTES:
        return None, [], "Metadata XML is too large"
    from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser

    try:
        idp = OneLogin_Saml2_IdPMetadataParser.parse(xml).get("idp") or {}
    except Exception:
        return None, [], "Could not parse the IdP metadata XML"
    # Metadata with more than one signing cert (key rollover) comes back
    # as x509certMulti instead of x509cert.
    certs = (idp.get("x509certMulti") or {}).get("signing") or [idp.get("x509cert") or ""]
    fields = {
        "idp_entity_id": idp.get("entityId") or "",
        "idp_sso_url": (idp.get("singleSignOnService") or {}).get("url") or "",
    }
    return fields, certs, None


def _idp_from_form(data: dict) -> tuple[dict, list]:
    def _text(key):
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    fields = {"idp_entity_id": _text("idpEntityId"), "idp_sso_url": _text("idpSsoUrl")}
    raw_cert = _text("idpX509Cert")
    # A saved config round-trips as a PEM bundle; bare base64 is one cert.
    certs = sso.split_certs(raw_cert) if "BEGIN CERTIFICATE" in raw_cert else [raw_cert]
    return fields, certs


def _sso_url_allowed(url: str) -> bool:
    parsed = urlparse(url)
    if not parsed.hostname:
        return False
    if sso.backend_is_https():
        return parsed.scheme == "https"
    # Over http the request cookie is SameSite=Lax, so a cross-site IdP's POST
    # would arrive without it; only a same-host IdP (local dev) can work.
    return parsed.hostname == urlparse(sso.public_backend_url()).hostname


def _validated_pems(certs: list) -> list[str] | None:
    from cryptography import x509
    from onelogin.saml2.utils import OneLogin_Saml2_Utils

    pems = [OneLogin_Saml2_Utils.format_cert(c) for c in certs]
    try:
        for pem in pems:
            x509.load_pem_x509_certificate(pem.encode())
    except Exception:
        return None
    return pems


def _parse_idp_fields(data: dict) -> tuple[dict | None, str | None]:
    """IdP fields from pasted metadata XML or explicit values; returns (fields, error)."""
    xml = data.get("idpMetadataXml")
    if isinstance(xml, str) and xml.strip():
        fields, certs, error = _idp_from_metadata(xml)
        if error:
            return None, error
    else:
        fields, certs = _idp_from_form(data)

    certs = [c for c in certs if isinstance(c, str) and c.strip()]
    if not (fields["idp_entity_id"] and fields["idp_sso_url"] and certs):
        return None, "IdP entity ID, SSO URL and certificate are all required"
    if len("".join(certs)) > _MAX_METADATA_BYTES:
        return None, "IdP certificate is too large"
    if not _sso_url_allowed(fields["idp_sso_url"]):
        return None, (
            "IdP SSO URL must use https"
            if sso.backend_is_https()
            else "Aurora is served over http, so only an IdP on the same host can be used; serve Aurora over https"
        )

    pems = _validated_pems(certs)
    if pems is None:
        return None, "IdP certificate is not a valid X.509 certificate"
    fields["idp_x509_cert"] = "\n".join(pems)
    return fields, None


@org_sso_bp.route("", methods=["PUT"])
@require_permission("org", "manage")
def update_sso_settings(user_id):
    org_id = get_org_id_from_request()
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid request body"}), 400

    fields, error = _parse_idp_fields(data)
    if error:
        return jsonify({"error": error}), 400

    default_role = data.get("defaultRole", "viewer")
    if not isinstance(default_role, str) or default_role not in VALID_ROLES:
        return jsonify({"error": "Invalid default role"}), 400
    enabled = data.get("enabled") is True
    require_sso = data.get("requireSso") is True
    # Requiring SSO while it's off would lock every password user out.
    if require_sso and not enabled:
        return jsonify({"error": "SSO must be enabled before it can be required"}), 400

    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO org_sso_configs (org_id, idp_entity_id, idp_sso_url,
                                               idp_x509_cert, default_role, enabled,
                                               require_sso, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (org_id) DO UPDATE SET
                        idp_entity_id = EXCLUDED.idp_entity_id,
                        idp_sso_url = EXCLUDED.idp_sso_url,
                        idp_x509_cert = EXCLUDED.idp_x509_cert,
                        default_role = EXCLUDED.default_role,
                        enabled = EXCLUDED.enabled,
                        require_sso = EXCLUDED.require_sso,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW()""",
                (org_id, fields["idp_entity_id"], fields["idp_sso_url"],
                 fields["idp_x509_cert"], default_role, enabled, require_sso, user_id),
            )
            conn.commit()
    except Exception:
        logger.exception("[SSO] failed to save settings")
        return jsonify({"error": "Failed to save SSO settings"}), 500

    _audit(org_id, user_id, "sso_config_updated", "organization",
           {"enabled": enabled, "requireSso": require_sso, "defaultRole": default_role},
           resource_id=org_id)
    return _settings_response(org_id)


@org_sso_bp.route("/domains", methods=["POST"])
@require_permission("org", "manage")
def add_domain(user_id):
    org_id = get_org_id_from_request()
    data = request.get_json(silent=True)
    domain = sso.normalize_domain(data.get("domain") if isinstance(data, dict) else None)
    if not domain:
        return jsonify({"error": "Enter a valid domain, like example.com"}), 400

    skip = sso.skip_domain_verification()
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM org_sso_domains
                    WHERE domain = %s AND verified_at IS NOT NULL AND org_id <> %s""",
                (domain, org_id),
            )
            if cur.fetchone():
                return jsonify({"error": _DOMAIN_TAKEN}), 409
            cur.execute(
                """INSERT INTO org_sso_domains (org_id, domain, verification_token, verified_at)
                   VALUES (%s, %s, %s, CASE WHEN %s THEN NOW() END)
                   ON CONFLICT (org_id, domain) DO NOTHING""",
                (org_id, domain, sso.new_verification_token(), skip),
            )
            domains = _load_domains(cur, org_id)
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": _DOMAIN_TAKEN}), 409
    except Exception:
        logger.exception("[SSO] failed to add domain")
        return jsonify({"error": "Failed to add domain"}), 500

    _audit(org_id, user_id, "sso_domain_added", "organization", {"domain": domain}, resource_id=org_id)
    return jsonify({"domains": domains}), 201


@org_sso_bp.route("/domains/<domain_id>/verify", methods=["POST"])
@require_permission("org", "manage")
def verify_domain(user_id, domain_id):
    org_id = get_org_id_from_request()
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT domain, verification_token, verified_at
                     FROM org_sso_domains WHERE id = %s AND org_id = %s""",
                (domain_id, org_id),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "Domain not found"}), 404
            domain, token, verified_at = row

            if not verified_at:
                if not sso.skip_domain_verification() and not sso.domain_has_txt_token(domain, token):
                    return jsonify({
                        "error": "DNS record not found yet. DNS changes can take a few minutes to propagate.",
                    }), 400
                cur.execute(
                    "UPDATE org_sso_domains SET verified_at = NOW() WHERE id = %s AND org_id = %s",
                    (domain_id, org_id),
                )
            domains = _load_domains(cur, org_id)
            conn.commit()
    except psycopg2.errors.UniqueViolation:
        return jsonify({"error": _DOMAIN_TAKEN}), 409
    except Exception:
        logger.exception("[SSO] failed to verify domain")
        return jsonify({"error": "Failed to verify domain"}), 500

    _audit(org_id, user_id, "sso_domain_verified", "organization", {"domain": domain}, resource_id=org_id)
    return jsonify({"domains": domains})


@org_sso_bp.route("/domains/<domain_id>", methods=["DELETE"])
@require_permission("org", "manage")
def delete_domain(user_id, domain_id):
    org_id = get_org_id_from_request()
    try:
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            cur.execute(
                "DELETE FROM org_sso_domains WHERE id = %s AND org_id = %s RETURNING domain",
                (domain_id, org_id),
            )
            row = cur.fetchone()
            domains = _load_domains(cur, org_id)
            conn.commit()
    except Exception:
        logger.exception("[SSO] failed to delete domain")
        return jsonify({"error": "Failed to remove domain"}), 500
    if not row:
        return jsonify({"error": "Domain not found"}), 404

    _audit(org_id, user_id, "sso_domain_removed", "organization", {"domain": row[0]}, resource_id=org_id)
    return jsonify({"domains": domains})
