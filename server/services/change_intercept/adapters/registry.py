"""Registry mapping vendor strings to ``ChangeAdapter`` instances.

The dispatcher calls ``get_adapter("github")`` after extracting the
vendor from the inbound webhook (today: hardcoded to ``"github"`` since
the only ingest path is ``/github/webhook``; future GitLab/Bitbucket
endpoints will pass their own vendor string).

Adapters are constructed lazily on first lookup so importing this
module does not pay the cost of loading GitHub's REST client / Vault
secrets unless the dispatcher actually receives a GitHub event. Lookups
after the first hit are O(1) dict reads.

To register a new vendor, add an entry to ``_FACTORIES`` keyed by the
vendor string. Each factory is a zero-arg callable returning a fresh
adapter instance.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from .base import ChangeAdapter


class UnknownVendorError(KeyError):
    """Raised when ``get_adapter`` is called with an unregistered vendor.

    The dispatcher converts this to a 400-equivalent (acknowledge the
    webhook delivery so the sender stops retrying, but log a warning
    so ops can investigate). Distinct from ``KeyError`` so callers
    can ``except UnknownVendorError`` without swallowing genuine bugs.
    """


def _build_github_adapter() -> ChangeAdapter:
    """Lazy import of the GitHub adapter to avoid a load-time dependency
    on Vault / GitHub REST client when the dispatcher is processing a
    non-GitHub event (future-proofing for multi-vendor)."""
    from .github import GitHubChangeAdapter

    return GitHubChangeAdapter()


_FACTORIES: dict[str, Callable[[], ChangeAdapter]] = {
    "github": _build_github_adapter,
}

_instances: dict[str, ChangeAdapter] = {}
_lock = threading.Lock()


def get_adapter(vendor: str) -> ChangeAdapter:
    """Return the registered adapter for ``vendor``.

    Thread-safe lazy construction: the first caller for a given vendor
    pays the factory cost; concurrent callers serialise on ``_lock``
    long enough to double-check the cache, then return the shared
    instance. The factory is called at most once per vendor per
    process lifetime.

    Args:
        vendor: lowercase vendor string (e.g. ``"github"``).

    Raises:
        UnknownVendorError: when the vendor is not registered.
    """
    cached = _instances.get(vendor)
    if cached is not None:
        return cached

    factory = _FACTORIES.get(vendor)
    if factory is None:
        raise UnknownVendorError(
            f"No change-intercept adapter registered for vendor={vendor!r}"
        )

    with _lock:
        cached = _instances.get(vendor)
        if cached is not None:
            return cached
        instance = factory()
        _instances[vendor] = instance
        return instance


def registered_vendors() -> tuple[str, ...]:
    """Return the tuple of vendor strings the registry knows about.

    Used by the architectural test suite to assert every vendor in the
    registry has an adapter test file, and by ops endpoints that need
    to enumerate supported integrations.
    """
    return tuple(sorted(_FACTORIES.keys()))


def _reset_for_tests() -> None:
    """Drop cached adapter instances. Test-only helper."""
    with _lock:
        _instances.clear()
