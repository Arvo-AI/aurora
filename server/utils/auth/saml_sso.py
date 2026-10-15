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
import psycopg2.errors

from utils.auth import VALID_ROLES, normalize_email
from utils.web.public_url import external_backend_url

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
_GIVEN_ATTRS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/givenname",
    "urn:oid:2.5.4.42",
    "givenName",
    "firstName",
    "first_name",
    "given_name",
)
_SURNAME_ATTRS = (
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/surname",
    "urn:oid:2.5.4.4",
    "surname",
    "sn",
    "lastName",
    "last_name",
    "familyName",
    "family_name",
)
_NAME_MAX = 255  # users.name is VARCHAR(255)


class SsoError(Exception):
    """A login refusal. ``code`` is a fixed string safe to put in a redirect."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def public_backend_url() -> str:
    return external_backend_url()


def backend_is_https() -> bool:
    return urlparse(public_backend_url()).scheme == "https"


def enforce_domain_verification() -> bool:
    """DNS proof of domain ownership; required wherever orgs don't trust each other (SaaS)."""
    return os.getenv("SSO_ENFORCE_DOMAIN_VERIFICATION", "false").lower() == "true"


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


_PEM_BEGIN = "-----BEGIN CERTIFICATE-----"
_PEM_END = "-----END CERTIFICATE-----"


def split_certs(bundle: str) -> list[str]:
    """PEM bundle (one or more certs) -> list of PEM certs.

    Plain string splitting, not a regex: the input is admin-supplied and a
    lazy DOTALL pattern backtracks quadratically on repeated BEGIN markers.
    """
    certs = []
    for chunk in (bundle or "").split(_PEM_END)[:-1]:
        start = chunk.rfind(_PEM_BEGIN)
        if start != -1:
            certs.append(chunk[start:] + _PEM_END)
    return certs


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


def _attr_sources(auth) -> tuple:
    # Name is the claim URI; FriendlyName is the short form some IdPs send alone.
    raw = [auth.get_attributes() or {}]
    friendly = getattr(auth, "get_friendlyname_attributes", None)
    if callable(friendly):
        raw.append(friendly() or {})
    # Fold once per source. Group claims are large, and every lookup shares this.
    return tuple(_index_attrs(source) for source in raw)


def _index_attrs(attrs) -> tuple[dict, dict]:
    exact = attrs if isinstance(attrs, dict) else {}
    folded: dict = {}
    for key, values in exact.items():
        # Claim names are strings; anything else isn't a key we look up.
        if not isinstance(key, str):
            continue
        # Merge every casing so an empty first key cannot hide a later value.
        bucket = folded.setdefault(key.casefold(), [])
        if isinstance(values, list):
            bucket.extend(values)
        # A lone string must stay one value, not be split into characters.
        elif values is not None:
            bucket.append(values)
    return exact, folded


def _first_value(values) -> str:
    for value in values or []:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first(sources: tuple, names: tuple) -> str:
    for exact, folded in sources:
        for name in names:
            # The spelled key beats a case-only twin, in preference-list order.
            found = _first_value(exact.get(name))
            if found:
                return found
            found = _first_value(folded.get(name.casefold()))
            if found:
                return found
    return ""


def _has_token(text: str, token: str) -> bool:
    # Surnames can be several words ("van Dyke"); match that whole sequence.
    parts = [part.casefold() for part in text.split()]
    words = [word.casefold() for word in token.split()]
    if not words or len(words) > len(parts):
        return False
    width = len(words)
    return any(parts[i:i + width] == words for i in range(len(parts) - width + 1))


def _fit_name(name: str) -> str:
    return name[:_NAME_MAX].rstrip()


def _join_name(given: str, surname: str, display: str) -> str:
    # A display name that already includes the surname keeps middle names.
    if surname and _has_token(display, surname):
        return display
    # givenName is sometimes already "First Last"; don't append the surname twice.
    if surname and _has_token(given, surname):
        return given
    # Both parts are present — `name` is often only the given name.
    if given and surname:
        return f"{given} {surname}"
    # Only a one-word display is a bare first name. A longer one is already
    # the full name; punctuation and accents make token matching unreliable.
    if (
        display and surname and len(display.split()) == 1
        and display.casefold() != surname.casefold()
    ):
        return f"{display} {surname}"
    return display or given or surname


def _person_name(sources: tuple) -> str | None:
    """First and last name when the IdP sent both; otherwise the display name."""
    given = _first(sources, _GIVEN_ATTRS)
    surname = _first(sources, _SURNAME_ATTRS)
    display = _first(sources, _NAME_ATTRS)
    # Entra's `name` claim is the UPN, not a personal name.
    if "@" in display:
        display = ""
    name = _join_name(given, surname, display)
    return _fit_name(name) if name else None


def extract_identity(auth) -> dict:
    """Pull subject/email/name out of a validated ``OneLogin_Saml2_Auth``."""
    sources = _attr_sources(auth)
    name_id = (auth.get_nameid() or "").strip()

    # Entra's NameID defaults to the UPN, which can be renamed; the object ID
    # attribute is immutable, so prefer it as the account key.
    subject = _first(sources, _SUBJECT_ATTRS) or name_id

    email = normalize_email(_first(sources, _EMAIL_ATTRS))
    # Many IdPs send the email only as the NameID.
    if not email and "@" in name_id:
        email = normalize_email(name_id)

    return {"subject": subject, "email": email, "name": _person_name(sources)}


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


def _store_display_name(cur, user_id: str, name: str | None) -> None:
    # An assertion with no name must not wipe the one already stored.
    if not name:
        return
    # One round-trip, and no read/write window. Fill a blank, or extend a
    # given-name-only value ("Ada" -> "Ada Lovelace"). Any other stored name
    # — an invite, a user edit — stays put.
    cur.execute(
        """UPDATE users
              SET name = %s
            WHERE id = %s
              AND COALESCE(name, '') <> %s
              AND (
                    COALESCE(name, '') = ''
                    OR starts_with(%s, COALESCE(name, '') || ' ')
                  )""",
        (name, user_id, name, name),
    )


def resolve_user(
    cur, org_id: str, identity: dict, default_role: str, _retried: bool = False,
) -> tuple[str, bool]:
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
        # Later logins correct a name that was stored as only the given name.
        _store_display_name(cur, row[0], identity.get("name"))
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
        # An invited member's must_change_password would trap them on a
        # password screen they have no reason to use once SSO owns sign-in.
        cur.execute(
            """UPDATE users SET sso_subject = %s, email_verified = TRUE,
                              must_change_password = FALSE
                WHERE id = %s""",
            (subject, user_id),
        )
        # Invited members already have a name; don't replace it with the IdP's.
        return user_id, False

    # Address belongs to another org's account; never move people across orgs.
    if existing:
        raise SsoError("account_in_other_org")

    # First login — just-in-time provision. Unusable password: SSO users never
    # need one, and must_change_password would block them on first login.
    unusable = bcrypt.hashpw(os.urandom(32), bcrypt.gensalt()).decode("utf-8")
    cur.execute("SAVEPOINT sso_provision")
    try:
        cur.execute(
            """INSERT INTO users (email, password_hash, name, role, org_id,
                                  email_verified, must_change_password,
                                  sso_subject, created_at)
               VALUES (%s, %s, %s, %s, %s, TRUE, FALSE, %s, NOW())
               RETURNING id""",
            (email, unusable, identity["name"], default_role, org_id, subject),
        )
    except psycopg2.errors.UniqueViolation:
        cur.execute("ROLLBACK TO SAVEPOINT sso_provision")
        # A concurrent login (e.g. a second tab) provisioned the user first;
        # resolve again so this one finds that row instead of failing.
        if _retried:
            raise
        return resolve_user(cur, org_id, identity, default_role, _retried=True)
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
