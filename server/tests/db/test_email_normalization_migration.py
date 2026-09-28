"""Guard the import chain the email-normalization migration added.

``utils/db/db_utils.py`` is imported extremely early at boot (before Flask, and
by Celery workers), so pulling ``utils.log_sanitizer`` into it must not create
an import cycle or drag in a heavy dependency.
"""


def test_db_utils_imports_mask_email_without_a_cycle():
    import utils.db.db_utils as db_utils

    assert db_utils.mask_email("someone@example.com") == "som***@***"


def test_log_sanitizer_stays_dependency_light():
    """It must not reach back into the DB layer — that would be the cycle."""
    import inspect

    import utils.log_sanitizer as ls

    src = inspect.getsource(ls)
    assert "db_utils" not in src
    assert "connection_pool" not in src


def test_migration_is_registered_in_initialize_tables():
    """The savepoint block must actually be inside initialize_tables()."""
    import inspect

    from utils.db.db_utils import initialize_tables

    src = inspect.getsource(initialize_tables)
    assert "sp_email_ci" in src
    # The index must be conditional on there being zero collisions.
    assert "idx_users_email_lower" in src
    assert "HAVING COUNT(*) > 1" in src
    # Duplicates are reported, never merged or deleted.
    assert "DELETE FROM users" not in src
