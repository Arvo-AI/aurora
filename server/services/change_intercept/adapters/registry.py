"""Vendor-string to adapter-instance mapping.

Adding a new vendor: import the adapter, add one entry to ``_ADAPTERS``.
"""

from __future__ import annotations

from services.change_intercept.adapters.base import ChangeAdapter
from services.change_intercept.adapters.github import GitHubAdapter

_ADAPTERS: dict[str, ChangeAdapter] = {
    "github": GitHubAdapter(),
}


def get_adapter(vendor: str) -> ChangeAdapter:
    adapter = _ADAPTERS.get(vendor)
    if adapter is None:
        raise ValueError(f"Unknown change-intercept vendor: {vendor!r}")
    return adapter
