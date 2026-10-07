"""OAuth 2.1 + Dynamic Client Registration for customer-registered MCP servers.

Pure functions, no Flask. The flow spans two HTTP requests to Aurora (start,
then callback), so it cannot use the SDK's ``OAuthClientProvider`` -- that helper
blocks awaiting the authorization code on a loopback listener, which only works
for a desktop app running in one process. We reuse the SDK's models and drive
the steps ourselves.

Every outbound call here targets a URL derived from customer input, so each one
goes through ``assert_allowed_target``. A server whose metadata points its
authorization endpoint at 169.254.169.254 is the obvious SSRF bypass.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlencode, urlparse

import httpx
from mcp.client.auth import PKCEParameters

from connectors.mcp_connector.net import assert_allowed_target

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 15.0
CLIENT_NAME = "Aurora"
# Refresh this long before nominal expiry so a token does not die mid-call.
EXPIRY_SKEW_SECONDS = 60


class OAuthDiscoveryError(Exception):
    """The server does not advertise a usable OAuth authorization server."""


class OAuthRegistrationUnsupported(OAuthDiscoveryError):
    """No registration_endpoint: the user must supply a client ID manually."""


@dataclass(frozen=True)
class AuthServer:
    """The subset of authorization-server metadata this flow needs."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: Optional[str]
    scopes_supported: Tuple[str, ...]

    @property
    def supports_dcr(self) -> bool:
        return bool(self.registration_endpoint)


def _same_origin(a: str, b: str) -> bool:
    pa, pb = urlparse(a), urlparse(b)
    return (pa.scheme, pa.hostname, pa.port) == (pb.scheme, pb.hostname, pb.port)


async def _get_json(client: httpx.AsyncClient, url: str) -> Optional[Dict[str, Any]]:
    """GET a metadata document, or None when absent/unparseable."""
    assert_allowed_target(url)
    try:
        response = await client.get(url, headers={"Accept": "application/json"})
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


async def discover(server_url: str) -> AuthServer:
    """Resolve the authorization server protecting an MCP endpoint.

    Spec path: the MCP server's protected-resource metadata names its
    ``authorization_servers``, whose own metadata carries the endpoints. Falls
    back to the authorization-server well-known on the MCP server's origin,
    which is what servers that skip the resource-metadata hop expose.
    """
    assert_allowed_target(server_url)
    parsed = urlparse(server_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        # Resource metadata is published per-resource (…/oauth-protected-resource/mcp)
        # and also bare, depending on the server.
        issuer = origin
        for candidate in (
            f"{origin}/.well-known/oauth-protected-resource{path}",
            f"{origin}/.well-known/oauth-protected-resource",
        ):
            resource_meta = await _get_json(client, candidate)
            servers = (resource_meta or {}).get("authorization_servers") or []
            if servers and isinstance(servers[0], str):
                issuer = servers[0]
                break

        # Pin the issuer to the MCP server's own origin. Honouring an arbitrary
        # cross-origin issuer would let a malicious server redirect the user's
        # consent to a host of its choosing.
        if not _same_origin(issuer, server_url):
            raise OAuthDiscoveryError(
                f"Server names an authorization server on a different origin "
                f"({urlparse(issuer).netloc}). Aurora requires it to match "
                f"{parsed.netloc}."
            )

        for candidate in (
            f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
            f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        ):
            meta = await _get_json(client, candidate)
            if meta and meta.get("authorization_endpoint") and meta.get("token_endpoint"):
                return AuthServer(
                    issuer=meta.get("issuer") or issuer,
                    authorization_endpoint=meta["authorization_endpoint"],
                    token_endpoint=meta["token_endpoint"],
                    registration_endpoint=meta.get("registration_endpoint"),
                    scopes_supported=tuple(meta.get("scopes_supported") or ()),
                )

    raise OAuthDiscoveryError(
        "This server did not advertise OAuth metadata. If it needs a token, "
        "register it with the bearer or header option instead."
    )


async def register_client(auth_server: AuthServer, redirect_uri: str) -> Dict[str, Any]:
    """Register Aurora as a client (RFC 7591). Returns {client_id, client_secret?}.

    Requested as a public client: with PKCE there is no secret to store, which
    is both simpler and safer. Servers that insist on a confidential client
    return one anyway and we keep it.
    """
    if not auth_server.supports_dcr:
        raise OAuthRegistrationUnsupported(
            "This server does not support automatic client registration. Create "
            "an OAuth app on the provider and supply its client ID."
        )
    assert_allowed_target(auth_server.registration_endpoint)

    payload = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(auth_server.registration_endpoint, json=payload)
    if response.status_code not in (200, 201):
        raise OAuthDiscoveryError(
            f"Client registration was rejected (HTTP {response.status_code})."
        )
    body = response.json()
    client_id = body.get("client_id")
    if not client_id:
        raise OAuthDiscoveryError("Client registration returned no client_id.")
    # Deliberately not logging the response: it may carry a client_secret.
    return {"client_id": client_id, "client_secret": body.get("client_secret")}


def build_authorize_url(
    auth_server: AuthServer,
    client_id: str,
    redirect_uri: str,
    state: str,
    resource: str,
) -> Tuple[str, str]:
    """Build the authorization URL. Returns (url, code_verifier).

    The caller must persist the verifier against ``state``; it is required to
    redeem the code and never leaves Aurora.
    """
    pkce = PKCEParameters.generate()
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": pkce.code_challenge,
        "code_challenge_method": "S256",
        # RFC 8707: binds the token to this MCP server, so a token minted for
        # one resource cannot be replayed against another.
        "resource": resource,
    }
    if auth_server.scopes_supported:
        params["scope"] = " ".join(auth_server.scopes_supported)
    separator = "&" if urlparse(auth_server.authorization_endpoint).query else "?"
    return (
        f"{auth_server.authorization_endpoint}{separator}{urlencode(params)}",
        pkce.code_verifier,
    )


async def _token_request(token_endpoint: str, form: Dict[str, str]) -> Dict[str, Any]:
    """POST to the token endpoint and return the parsed grant."""
    assert_allowed_target(token_endpoint)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(
            token_endpoint,
            data=form,
            headers={"Accept": "application/json"},
        )
    if response.status_code != 200:
        # The body can echo the client_secret back; surface only the status.
        raise OAuthDiscoveryError(
            f"Token endpoint returned HTTP {response.status_code}."
        )
    body = response.json()
    if not body.get("access_token"):
        raise OAuthDiscoveryError("Token endpoint returned no access_token.")
    return body


def _to_auth_blob(
    grant: Dict[str, Any],
    client_id: str,
    client_secret: Optional[str],
    token_endpoint: str,
    previous_refresh: Optional[str] = None,
) -> Dict[str, Any]:
    """Shape a token response into the stored auth config.

    ``previous_refresh`` is kept when the server omits a new one: servers that
    do not rotate send ``refresh_token`` only on the first grant, and dropping
    it would disconnect the server at the next expiry.
    """
    expires_in = grant.get("expires_in")
    return {
        "type": "oauth",
        "access_token": grant["access_token"],
        "refresh_token": grant.get("refresh_token") or previous_refresh,
        "expires_at": (time.time() + float(expires_in)) if expires_in else None,
        "client_id": client_id,
        "client_secret": client_secret,
        "token_endpoint": token_endpoint,
        "scope": grant.get("scope"),
    }


async def exchange_code(
    auth_server: AuthServer,
    client_id: str,
    client_secret: Optional[str],
    code: str,
    code_verifier: str,
    redirect_uri: str,
    resource: str,
) -> Dict[str, Any]:
    """Redeem an authorization code. Returns the auth blob to store."""
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
        "resource": resource,
    }
    if client_secret:
        form["client_secret"] = client_secret
    grant = await _token_request(auth_server.token_endpoint, form)
    return _to_auth_blob(grant, client_id, client_secret, auth_server.token_endpoint)


async def refresh_token(auth: Dict[str, Any]) -> Dict[str, Any]:
    """Exchange a refresh token for a new access token. Returns a new auth blob.

    Raises ``OAuthDiscoveryError`` when the grant is gone -- the caller must
    surface that as "reconnect this server" rather than retrying.
    """
    refresh = auth.get("refresh_token")
    token_endpoint = auth.get("token_endpoint")
    client_id = auth.get("client_id")
    if not (refresh and token_endpoint and client_id):
        raise OAuthDiscoveryError("This server has no refresh token; reconnect it.")

    form = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client_id,
    }
    if auth.get("client_secret"):
        form["client_secret"] = auth["client_secret"]
    grant = await _token_request(token_endpoint, form)
    # Carry the old refresh token forward when the server does not rotate it.
    return _to_auth_blob(
        grant, client_id, auth.get("client_secret"), token_endpoint, previous_refresh=refresh
    )


def is_expired(auth: Optional[Dict[str, Any]]) -> bool:
    """Whether an OAuth access token is at or near expiry.

    Servers that omit ``expires_in`` report False: there is nothing to act on,
    and a 401 mid-call triggers refresh anyway.
    """
    if not auth or auth.get("type") != "oauth":
        return False
    expires_at = auth.get("expires_at")
    if not expires_at:
        return False
    return time.time() >= (float(expires_at) - EXPIRY_SKEW_SECONDS)
