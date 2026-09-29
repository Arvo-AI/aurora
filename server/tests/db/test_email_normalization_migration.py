"""Guard the users.email normalization migration.

Two layers:

1. Structural — ``utils/db/db_utils.py`` is imported extremely early at boot
   (before Flask, and by Celery workers), so pulling ``utils.log_sanitizer``
   into it must not create an import cycle, and the savepoint block must
   actually be registered inside ``initialize_tables()``.

2. Behavioural — the migration's SQL is executed against real Postgres rows to
   prove it lowercases non-colliding emails, refuses the unique index while
   collisions remain, and never deletes or merges a colliding row. Skips
   cleanly when Postgres is unreachable.
"""

import inspect

import pytest

# The three statements the migration runs, kept in the same order as
# initialize_tables(). Extracted here so the behavioural tests exercise the
# real SQL rather than a paraphrase of it.
_LOWERCASE_NONCOLLIDING = """
    UPDATE users u
    SET email = LOWER(u.email)
    WHERE u.email <> LOWER(u.email)
      AND NOT EXISTS (
          SELECT 1 FROM users o
          WHERE o.id <> u.id AND LOWER(o.email) = LOWER(u.email)
      );
"""

_FIND_COLLISIONS = """
    SELECT LOWER(email), COUNT(*) FROM users
    GROUP BY LOWER(email) HAVING COUNT(*) > 1;
"""

_CREATE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower ON users (LOWER(email));"
)


# ---------------------------------------------------------------------------
# Structural
# ---------------------------------------------------------------------------

def test_db_utils_imports_mask_email_without_a_cycle():
    import utils.db.db_utils as db_utils

    assert db_utils.mask_email("someone@example.com") == "som***@***"


def test_log_sanitizer_stays_dependency_light():
    """It must not reach back into the DB layer — that would be the cycle."""
    import utils.log_sanitizer as ls

    src = inspect.getsource(ls)
    assert "db_utils" not in src
    assert "connection_pool" not in src


def test_migration_is_registered_in_initialize_tables():
    """The savepoint block must actually be inside initialize_tables()."""
    import utils.db.db_utils as db_utils

    src = inspect.getsource(db_utils.initialize_tables)
    assert "sp_email_ci" in src
    # The index must be conditional on there being zero collisions.
    assert "idx_users_email_lower" in src
    assert "HAVING COUNT(*) > 1" in src
    # Duplicates are reported, never merged or deleted.
    assert "DELETE FROM users" not in src


def test_migration_sql_constants_match_the_implementation():
    """Keeps the behavioural SQL below honest if the migration is edited.

    Compares on a quote/whitespace-stripped form, because the implementation
    builds these statements from adjacent string literals and doesn't always
    carry a trailing semicolon.
    """
    import utils.db.db_utils as db_utils

    def _canon(sql: str) -> str:
        return " ".join(sql.replace('"', " ").replace("'", " ").split()).rstrip("; ")

    src = _canon(inspect.getsource(db_utils.initialize_tables))
    for statement in (_LOWERCASE_NONCOLLIDING, _FIND_COLLISIONS, _CREATE_INDEX):
        assert _canon(statement) in src


# ---------------------------------------------------------------------------
# Behavioural — real Postgres, rolled back
# ---------------------------------------------------------------------------

@pytest.fixture
def users_table(db_session):
    """An isolated schema with a prod-shaped users table, rolled back after.

    A dedicated schema (rather than the live ``public.users``) keeps the test
    from touching real rows even though ``db_session`` also rolls back, and
    lets the unique index be created under its own name without colliding
    with the real one.
    """
    with db_session.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS email_ci_test")
        cur.execute("SET search_path TO email_ci_test")
        cur.execute("""
            CREATE TABLE users (
                id VARCHAR(255) PRIMARY KEY,
                email VARCHAR(255) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
    yield db_session
    # db_session rolls back, which also drops the schema and table.


def _seed(conn, rows):
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO users (id, email, password_hash) VALUES (%s, %s, 'h')", rows
        )


def _emails(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT id, email FROM users ORDER BY id")
        return dict(cur.fetchall())


def _collisions(conn):
    with conn.cursor() as cur:
        cur.execute(_FIND_COLLISIONS)
        return cur.fetchall()


def _index_exists(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM pg_indexes "
            "WHERE indexname = 'idx_users_email_lower' AND schemaname = 'email_ci_test'"
        )
        return cur.fetchone()[0] == 1


def test_noncolliding_mixed_case_email_is_lowercased(users_table):
    _seed(users_table, [
        ("a", "Mixed.Case@example.com"),
        ("b", "already.lower@example.com"),
    ])

    with users_table.cursor() as cur:
        cur.execute(_LOWERCASE_NONCOLLIDING)
        assert cur.rowcount == 1

    assert _emails(users_table) == {
        "a": "mixed.case@example.com",
        "b": "already.lower@example.com",
    }


def test_index_is_created_when_no_collisions_remain(users_table):
    _seed(users_table, [("a", "Mixed.Case@example.com")])

    with users_table.cursor() as cur:
        cur.execute(_LOWERCASE_NONCOLLIDING)
        assert not _collisions(users_table)
        cur.execute(_CREATE_INDEX)

    assert _index_exists(users_table)


def test_index_blocks_a_case_variant_duplicate_once_applied(users_table):
    """The whole point of the index: no future code path can re-split an email."""
    import psycopg2.errors

    _seed(users_table, [("a", "user@example.com")])
    with users_table.cursor() as cur:
        cur.execute(_CREATE_INDEX)

    with pytest.raises(psycopg2.errors.UniqueViolation):
        _seed(users_table, [("b", "USER@example.com")])


def test_colliding_rows_are_left_intact_and_block_the_index(users_table):
    """Mirrors the real prod shape: 2 colliding pairs + 1 lone mixed-case row."""
    _seed(users_table, [
        ("a1", "dup.one@example.com"),
        ("a2", "Dup.One@example.com"),
        ("b1", "dup.two@example.com"),
        ("b2", "Dup.Two@example.com"),
        ("c1", "Lone.Mixed@example.com"),
    ])

    with users_table.cursor() as cur:
        cur.execute(_LOWERCASE_NONCOLLIDING)
        # Only the lone non-colliding row is touched.
        assert cur.rowcount == 1

    after = _emails(users_table)
    # Every colliding row survives with its original casing — nothing merged,
    # nothing deleted, so neither account loses access.
    assert after["a1"] == "dup.one@example.com"
    assert after["a2"] == "Dup.One@example.com"
    assert after["b1"] == "dup.two@example.com"
    assert after["b2"] == "Dup.Two@example.com"
    assert after["c1"] == "lone.mixed@example.com"

    # Both pairs are reported for manual reconciliation...
    assert sorted(_collisions(users_table)) == [
        ("dup.one@example.com", 2),
        ("dup.two@example.com", 2),
    ]

    # ...and the index is correctly withheld. This is the intended outcome on
    # current production data.
    assert not _index_exists(users_table)


def test_index_applies_after_collisions_are_reconciled(users_table):
    """Once an operator resolves the pair, a later boot enforces uniqueness."""
    _seed(users_table, [
        ("a1", "dup.one@example.com"),
        ("a2", "Dup.One@example.com"),
    ])
    assert _collisions(users_table)

    # Operator picks a winner manually (never done by the migration itself).
    with users_table.cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = 'a2'")
        cur.execute(_LOWERCASE_NONCOLLIDING)
        assert not _collisions(users_table)
        cur.execute(_CREATE_INDEX)

    assert _index_exists(users_table)


def test_migration_is_idempotent(users_table):
    """initialize_tables() runs on every boot — a second pass must be a no-op."""
    _seed(users_table, [("a", "Mixed.Case@example.com")])

    with users_table.cursor() as cur:
        cur.execute(_LOWERCASE_NONCOLLIDING)
        cur.execute(_CREATE_INDEX)
        # Second pass: nothing left to lowercase, index creation is a no-op.
        cur.execute(_LOWERCASE_NONCOLLIDING)
        assert cur.rowcount == 0
        cur.execute(_CREATE_INDEX)

    assert _emails(users_table) == {"a": "mixed.case@example.com"}
    assert _index_exists(users_table)
