"""Outbound-target safety for the MCP connector.

Separate module so ``client.py`` and ``oauth.py`` can both guard their requests
without importing each other -- the OAuth flow needs the MCP transport's refresh
helper, and the transport needs the OAuth refresh, which would otherwise be a
circular import.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


def allow_private_targets() -> bool:
    """Whether private/internal addresses are valid MCP targets.

    False on Aurora SaaS: a tenant must not be able to aim Aurora at Arvo's
    internal network. True for self-hosted installs, where the customer's MCP
    servers legitimately live on private addresses inside their own cluster.
    """
    return os.getenv("MCP_ALLOW_PRIVATE_TARGETS", "false").strip().lower() == "true"


def assert_allowed_target(url: str) -> None:
    """Validate an outbound URL, rejecting non-public hosts unless allowed.

    Raises ``ValueError`` with a user-facing message. Mirrors the SSRF guard in
    ``chat/backend/agent/tools/notion/workspace.py``: every resolved address
    must pass, so a hostname with both a public and a loopback A record cannot
    slip through.

    Applied to OAuth metadata, registration, and token endpoints too, not just
    the MCP connection -- a server whose metadata points its token endpoint at
    169.254.169.254 would otherwise be a clean bypass.

    ponytail: resolve-then-connect leaves a TOCTOU window (DNS can change
    between this check and the request). Accepted, same as the Notion path.
    Closing it needs a custom resolver pinning the validated IP.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must start with http:// or https://")
    host = parsed.hostname
    if not host:
        raise ValueError("URL has no hostname")

    if allow_private_targets():
        return

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise ValueError(f"DNS lookup failed for {host}: {exc}") from exc

    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"{host} resolves to the non-public address {ip}. Aurora refuses "
                "internal targets; set MCP_ALLOW_PRIVATE_TARGETS=true on a "
                "self-hosted deployment to allow them."
            )
