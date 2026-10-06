"""SAML SSO helpers: per-org SP settings, assertion attributes, JIT provisioning.

Which org a login lands in is decided by which org's IdP certificate verified
the assertion (the ACS URL is per-org) — never by the email the user typed.
The email domain must additionally be verified by that same org.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
from urllib.parse import urlparse

import bcrypt

from utils.auth import VALID_ROLES, normalize_email

logger = logging.getLogger(__name__)

SAML_REQUEST_TTL_SEC = 10 * 60
DNS_TXT_PREFIX = "_aurora-sso"
DNS_TXT_VALUE_PREFIX = "aurora-sso-verification="

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)([a-z0-9-]{1,63}(?<!-)\.)+[a-z]{2,63}$")

# Attribute names, in preference order. Entra uses the long claim URIs;
# Okta/Google/Keycloak typically send the short forms.
_SUBJECT_ATTRS = ("http://schemas.microsoft.com/identity/claims/objectidentifier",)
_EMAIL_ATTRS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "email",
    "mail",
    "urn:oid:0.9.2342.19200300.100.1.3",
)
_NAME_ATTRS = (
    "http://schemas.microsoft.com/identity/claims/displayname",
    "displayName",
    "name",
    "urn:oid:2.16.840.1.113730.3.1.241",
)
_GIVEN_ATTRS = ("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname", "firstName", "givenName")
_SURNAME_ATTRS = ("http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname", "lastName", "sn")


class SsoError(Exception):
    """A login refusal. ``code`` is a fixed string safe to put in a redirect."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def public_backend_url() -> str:
    return (os.getenv("NEXT_PUBLIC_BACKEND_URL") or "").rstrip("/")


def skip_domain_verification() -> bool:
    """Self-hosted operators own every org, so DNS proof adds nothing there."""
    return os.getenv("SSO_SKIP_DOMAIN_VERIFICATION", "false").lower() == "true"


def sp_urls(org_id: str) -> dict:
    base = f"{public_backend_url()}/api/auth/saml"
    return {
        "entityId": f"{base}/metadata/{org_id}",
        "acsUrl": f"{base}/acs/{org_id}",
        "loginUrl": f"{base}/login/{org_id}",
        "metadataUrl": f"{base}/metadata/{org_id}",
    }


def build_settings(org_id: str, config: dict | None) -> dict:
    """python3-saml settings for one org. ``config`` is None for SP-only metadata."""
    urls = sp_urls(org_id)
    settings = {
        "strict": True,
        "debug": False,
        "sp": {
            "entityId": urls["entityId"],
            "assertionConsumerService": {
                "url": urls["acsUrl"],
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
        },
        "security": {
            "authnRequestsSigned": False,
            # Every major IdP signs the assertion by default; some don't also
            # sign the envelope, so requiring both would break them.
            "wantAssertionsSigned": True,
            "wantMessagesSigned": False,
            "wantNameId": True,
            "requestedAuthnContext": False,
            "rejectDeprecatedAlgorithm": True,
            "allowRepeatAttributeName": True,
        },
    }
    # The SP metadata endpoint has no IdP yet, but python3-saml validates the
    # idp block, so a placeholder keeps settings construction valid.
    idp = config or {"idp_entity_id": "urn:unset", "idp_sso_url": "https://unset.invalid", "idp_x509_cert": ""}
    settings["idp"] = {
        "entityId": idp["idp_entity_id"],
        "singleSignOnService": {
            "url": idp["idp_sso_url"],
            "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
        },
    }
    certs = split_certs(idp["idp_x509_cert"])
    # During IdP key rollover the metadata lists old and new certs; accept either.
    if len(certs) > 1:
        settings["idp"]["x509certMulti"] = {"signing": certs}
    else:
        settings["idp"]["x509cert"] = certs[0] if certs else ""
    return settings


_PEM_RE = re.compile(r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)


def split_certs(bundle: str) -> list[str]:
    """PEM bundle (one or more certs) -> list of PEM certs."""
    return _PEM_RE.findall(bundle or "")


def request_data(flask_request) -> dict:
    """Describe the request as the configured public URL, not the proxied one.

    python3-saml checks the response's Destination against this URL; behind a
    TLS-terminating proxy Flask sees http://internal-host, which never matches.
    """
    parsed = urlparse(public_backend_url())
    return {
        "https": "on" if parsed.scheme == "https" else "off",
        "http_host": parsed.netloc,
        "script_name": (parsed.path or "").rstrip("/") + flask_request.path,
        "get_data": flask_request.args.copy(),
        "post_data": flask_request.form.copy(),
    }


def _first(attrs: dict, names: tuple) -> str:
    for name in names:
        values = attrs.get(name) or []
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def extract_identity(auth) -> dict:
    """Pull subject/email/name out of a validated ``OneLogin_Saml2_Auth``."""
    attrs = auth.get_attributes() or {}
    name_id = (auth.get_nameid() or "").strip()

    # Entra's NameID defaults to the UPN, which can be renamed; the object ID
    # attribute is immutable, so prefer it as the account key.
    subject = _first(attrs, _SUBJECT_ATTRS) or name_id

    email = normalize_email(_first(attrs, _EMAIL_ATTRS))
    # Many IdPs send the email only as the NameID.
    if not email and "@" in name_id:
        email = normalize_email(name_id)

    name = _first(attrs, _NAME_ATTRS)
    if not name:
        name = " ".join(p for p in (_first(attrs, _GIVEN_ATTRS), _first(attrs, _SURNAME_ATTRS)) if p)

    return {"subject": subject, "email": email, "name": name or None}


def normalize_domain(domain) -> str:
    if not isinstance(domain, str):
        return ""
    domain = domain.strip().lower().rstrip(".")
    return domain if _DOMAIN_RE.match(domain) else ""


def email_domain(email: str) -> str:
    return normalize_domain(email.rsplit("@", 1)[-1]) if "@" in email else ""


def new_verification_token() -> str:
    return secrets.token_hex(16)


def dns_txt_record(domain: str, token: str) -> dict:
    return {"name": f"{DNS_TXT_PREFIX}.{domain}", "value": f"{DNS_TXT_VALUE_PREFIX}{token}"}


def domain_has_txt_token(domain: str, token: str) -> bool:
    import dns.exception
    import dns.resolver

    expected = f"{DNS_TXT_VALUE_PREFIX}{token}"
    try:
        answers = dns.resolver.resolve(f"{DNS_TXT_PREFIX}.{domain}", "TXT", lifetime=5)
    except (dns.exception.DNSException, OSError):
        return False
    for rdata in answers:
        # A TXT record may be split into several <=255-byte strings.
        # Bytes, not str: compare_digest raises on non-ASCII strings, and any
        # TXT record on the name (not just ours) reaches this comparison.
        value = b"".join(rdata.strings).strip()
        if secrets.compare_digest(value, expected.encode()):
            return True
    return False


def find_org_for_email(cur, email: str) -> str | None:
    """Route an email to the single org that verified its domain."""
    domain = email_domain(normalize_email(email))
    if not domain:
        return None
    cur.execute(
        """SELECT d.org_id FROM org_sso_domains d
             JOIN org_sso_configs c ON c.org_id = d.org_id
            WHERE d.domain = %s AND d.verified_at IS NOT NULL AND c.enabled""",
        (domain,),
    )
    row = cur.fetchone()
    return row[0] if row else None


def load_enabled_config(cur, org_id: str) -> dict | None:
    cur.execute(
        """SELECT idp_entity_id, idp_sso_url, idp_x509_cert, default_role
             FROM org_sso_configs WHERE org_id = %s AND enabled""",
        (org_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    return {
        "idp_entity_id": row[0],
        "idp_sso_url": row[1],
        "idp_x509_cert": row[2],
        "default_role": row[3] if row[3] in VALID_ROLES else "viewer",
    }


def store_request_id(cur, request_id: str, org_id: str) -> None:
    cur.execute("DELETE FROM saml_requests WHERE expires_at < NOW()")
    cur.execute(
        """INSERT INTO saml_requests (request_id, org_id, expires_at)
           VALUES (%s, %s, NOW() + make_interval(secs => %s))""",
        (request_id, org_id, SAML_REQUEST_TTL_SEC),
    )


def burn_request_id(cur, request_id: str, org_id: str) -> bool:
    """Single-use: concurrent replays race on the row and exactly one wins."""
    cur.execute(
        """DELETE FROM saml_requests
            WHERE request_id = %s AND org_id = %s AND expires_at > NOW()
        RETURNING 1""",
        (request_id, org_id),
    )
    return cur.fetchone() is not None


def resolve_user(cur, org_id: str, identity: dict, default_role: str) -> tuple[str, bool]:
    """Find, link or create the Aurora user for a verified assertion.

    Returns ``(user_id, created)``; raises ``SsoError`` on refusal.
    """
    subject, email = identity["subject"], identity["email"]
    if not subject:
        raise SsoError("missing_subject")
    if not email:
        raise SsoError("missing_email")

    # The org's IdP vouching for an address is only trusted for domains the
    # org proved it owns; otherwise any IdP could mint any victim's email.
    cur.execute(
        """SELECT 1 FROM org_sso_domains
            WHERE org_id = %s AND domain = %s AND verified_at IS NOT NULL""",
        (org_id, email_domain(email)),
    )
    if not cur.fetchone():
        raise SsoError("domain_not_allowed")

    # Returning SSO user — matched on the IdP's immutable ID.
    cur.execute(
        "SELECT id FROM users WHERE org_id = %s AND sso_subject = %s",
        (org_id, subject),
    )
    row = cur.fetchone()
    if row:
        return row[0], False

    cur.execute(
        "SELECT id, org_id, sso_subject FROM users WHERE LOWER(email) = %s",
        (email,),
    )
    existing = cur.fetchall()
    same_org = [r for r in existing if r[1] == org_id]

    # Existing member logging in via SSO for the first time — link them.
    # Safe: the domain is verified by this org and its IdP signed the email.
    if same_org:
        user_id, _, linked_subject = same_org[0]
        # Already linked to a different IdP identity: the IdP reassigned the
        # address, and silently re-pointing the account would hand it over.
        if linked_subject and linked_subject != subject:
            raise SsoError("identity_mismatch")
        cur.execute(
            "UPDATE users SET sso_subject = %s, email_verified = TRUE WHERE id = %s",
            (subject, user_id),
        )
        return user_id, False

    # Address belongs to another org's account; never move people across orgs.
    if existing:
        raise SsoError("account_in_other_org")

    # First login — just-in-time provision. Unusable password: SSO users never
    # need one, and must_change_password would block them on first login.
    unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt()).decode("utf-8")
    cur.execute(
        """INSERT INTO users (email, password_hash, name, role, org_id,
                              email_verified, must_change_password,
                              sso_subject, created_at)
           VALUES (%s, %s, %s, %s, %s, TRUE, FALSE, %s, NOW())
           RETURNING id""",
        (email, unusable, identity["name"], default_role, org_id, subject),
    )
    return cur.fetchone()[0], True


def org_requires_sso(cur, org_id: str | None, email: str) -> bool:
    """Whether password login is closed for this user's org and email domain."""
    if not org_id:
        return False
    cur.execute(
        """SELECT 1 FROM org_sso_configs c
             JOIN org_sso_domains d ON d.org_id = c.org_id
            WHERE c.org_id = %s AND c.enabled AND c.require_sso
              AND d.domain = %s AND d.verified_at IS NOT NULL""",
        (org_id, email_domain(normalize_email(email))),
    )
    return cur.fetchone() is not None
