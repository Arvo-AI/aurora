"""Tests for the per-install ``change_intercept_dry_run`` flag helper.

DB-touching paths are integration territory; here we pin the safety
defaults so a regression can never silently flip an install to live
mode without an explicit operator action.
"""

from __future__ import annotations

import pytest

from services.change_intercept import install_config


def test_negative_installation_id_defaults_to_dry_run() -> None:
    assert install_config.is_dry_run(-1) is True


def test_none_installation_id_defaults_to_dry_run() -> None:
    # type-ignored to mirror the runtime call sites where the column
    # could legitimately be None for older rows.
    assert install_config.is_dry_run(None) is True  # type: ignore[arg-type]


def test_lookup_failure_returns_dry_run_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the DB read raises, we MUST default to calibration mode.

    The opposite default (live mode on failure) would silently post
    customer-visible reviews any time the DB hiccups — the exact
    failure mode this fail-closed default exists to prevent.
    """

    class BrokenPool:
        def get_admin_connection(self):  # noqa: ANN201
            raise RuntimeError("simulated DB outage")

    import utils.db.connection_pool as connection_pool_mod

    monkeypatch.setattr(connection_pool_mod, "db_pool", BrokenPool())
    assert install_config.is_dry_run(42) is True


def test_set_dry_run_failure_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenPool:
        def get_admin_connection(self):  # noqa: ANN201
            raise RuntimeError("simulated DB outage")

    import utils.db.connection_pool as connection_pool_mod

    monkeypatch.setattr(connection_pool_mod, "db_pool", BrokenPool())
    # Failure path returns False; caller treats this as "no row updated."
    assert install_config.set_dry_run(42, dry_run=False) is False
