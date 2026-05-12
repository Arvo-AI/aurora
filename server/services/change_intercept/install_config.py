"""Per-installation configuration for the change-intercept pipeline.

Phase 1a Part 3 introduces a single config flag:
``change_intercept_dry_run`` on the ``github_installations`` row.

Default value is ``TRUE``. That is the safe default — every install
starts in calibration mode and stays there until an operator
explicitly flips it to ``FALSE`` (see :func:`set_dry_run`). Once
``FALSE``, the launch_investigation task calls the adapter's
``post_verdict`` and the customer sees Aurora reviews on real PRs.

Why a column rather than a separate ``change_intercept_install_config``
table: there's exactly one flag today and a single-row read per
investigation. The table is overkill until we add a second flag
(severity threshold, category opt-outs, etc.). Future expansions
should still live on ``github_installations`` — they're per-install
state.

This module is intentionally thin (no caching, no decorators). The
investigation path runs at most a few times per minute even for very
busy customers; the extra round-trip is cheaper than a stale cache
that surfaces a "supposed to be live but still posted no review"
support ticket.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def is_dry_run(installation_id: int) -> bool:
    """Return the ``change_intercept_dry_run`` flag for ``installation_id``.

    Defaults to ``True`` when:
        - The installation row doesn't exist (defence in depth).
        - The DB read fails for any reason (treat as calibration mode
          rather than risking an unintended live review).

    Args:
        installation_id: GitHub installation id from the change_event row.

    Returns:
        ``True`` if the install is in calibration mode (don't post live
        reviews); ``False`` only when the operator has explicitly
        enabled live reviews.
    """
    if installation_id is None or installation_id < 0:
        return True

    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT change_intercept_dry_run
                         FROM github_installations
                        WHERE installation_id = %s""",
                    (installation_id,),
                )
                row = cur.fetchone()
                if row is None:
                    logger.warning(
                        "change_intercept_install_config=missing_row "
                        "installation_id=%s defaulting=dry_run_true",
                        installation_id,
                    )
                    return True
                value = row[0]
                # psycopg2 returns booleans natively; defensive cast for safety.
                return bool(value) if value is not None else True
    except Exception as exc:
        logger.warning(
            "change_intercept_install_config=lookup_failed "
            "installation_id=%s error_class=%s defaulting=dry_run_true",
            installation_id,
            type(exc).__name__,
        )
        return True


def set_dry_run(installation_id: int, *, dry_run: bool) -> bool:
    """Flip the ``change_intercept_dry_run`` flag for ``installation_id``.

    Intended for operator use via a Python shell or CLI script after
    the calibration window completes. NOT exposed as an HTTP endpoint
    in Phase 1a — flipping a customer to live reviews is a deliberate
    out-of-band action.

    Args:
        installation_id: GitHub installation id.
        dry_run: ``False`` to enable live reviews; ``True`` to revert
            to calibration mode.

    Returns:
        ``True`` when the row was updated, ``False`` when no matching
        installation exists.
    """
    try:
        from utils.db.connection_pool import db_pool

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE github_installations
                          SET change_intercept_dry_run = %s,
                              updated_at = NOW()
                        WHERE installation_id = %s""",
                    (dry_run, installation_id),
                )
                row_count = cur.rowcount
            conn.commit()
            logger.info(
                "change_intercept_install_config=updated installation_id=%s "
                "dry_run=%s rows=%s",
                installation_id,
                dry_run,
                row_count,
            )
            return row_count > 0
    except Exception as exc:
        logger.exception(
            "change_intercept_install_config=update_failed installation_id=%s "
            "error_class=%s",
            installation_id,
            type(exc).__name__,
        )
        return False
