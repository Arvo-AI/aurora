"""Integration tests for RLS on knowledge_base_documents and knowledge_base_memory.

Requires a running PostgreSQL instance (the aurora-postgres Docker container).
Creates a non-superuser test role to validate RLS enforcement, inserts test
data across two orgs, and verifies that:
1. RLS blocks cross-org reads/writes when org context is set
2. The SECURITY DEFINER cleanup function works across orgs
3. Celery-style set_rls_context flows work correctly
4. Default-deny: no rows visible when session org_id is unset

Run with:
    cd server && python3 -m pytest tests/test_rls_kb_integration.py -v
"""

import os
import uuid

import psycopg2
import pytest

_ADMIN_PARAMS = {
    "dbname": os.getenv("POSTGRES_DB", "aurora_db"),
    "user": os.getenv("POSTGRES_USER", "aurora"),
    "password": os.getenv(
        "POSTGRES_PASSWORD",
        "07017d104f177cd0b8e093f82d102338f455e70f7f22327d497eb999241e7197",
    ),
    "host": os.getenv("POSTGRES_HOST", "localhost"),
    "port": int(os.getenv("POSTGRES_PORT", "5432")),
}

_TEST_ROLE = "aurora_rls_test"
_TEST_ROLE_PW = "rls_test_pw_ephemeral"

ORG_A = f"test-org-a-{uuid.uuid4().hex[:8]}"
ORG_B = f"test-org-b-{uuid.uuid4().hex[:8]}"
USER_A = f"user-a-{uuid.uuid4().hex[:8]}"
USER_B = f"user-b-{uuid.uuid4().hex[:8]}"


def _admin_connect():
    return psycopg2.connect(**_ADMIN_PARAMS)


def _app_connect():
    """Connect as the non-superuser test role (RLS is enforced)."""
    params = {**_ADMIN_PARAMS, "user": _TEST_ROLE, "password": _TEST_ROLE_PW}
    return psycopg2.connect(**params)


def _set_org(cursor, conn, org_id):
    cursor.execute("SET myapp.current_org_id = %s;", (org_id,))
    cursor.execute("SET myapp.current_user_id = %s;", ("test",))
    conn.commit()


def _reset_org(cursor, conn):
    cursor.execute("RESET myapp.current_org_id;")
    cursor.execute("RESET myapp.current_user_id;")
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def admin_conn():
    """Superuser connection for setup/teardown."""
    try:
        conn = _admin_connect()
    except psycopg2.OperationalError:
        pytest.skip("PostgreSQL not reachable — skipping integration tests")
    yield conn
    conn.close()


@pytest.fixture(scope="module")
def ensure_test_role(admin_conn):
    """Create a non-superuser role that mirrors the app connection.

    Superusers bypass RLS entirely, so we need a regular role to test policies.
    """
    admin_conn.autocommit = True
    cur = admin_conn.cursor()

    cur.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s", (_TEST_ROLE,)
    )
    if not cur.fetchone():
        cur.execute(
            f"CREATE ROLE {_TEST_ROLE} LOGIN PASSWORD %s", (_TEST_ROLE_PW,)
        )
    else:
        cur.execute(
            f"ALTER ROLE {_TEST_ROLE} LOGIN PASSWORD %s", (_TEST_ROLE_PW,)
        )

    cur.execute(
        f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_TEST_ROLE}"
    )
    cur.execute(
        f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_TEST_ROLE}"
    )
    cur.execute(
        f"GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO {_TEST_ROLE}"
    )
    admin_conn.autocommit = False
    yield


@pytest.fixture(scope="module")
def ensure_rls(admin_conn, ensure_test_role):
    """Ensure RLS + policies exist on KB tables (idempotent)."""
    cur = admin_conn.cursor()

    rls_using = """
        org_id IS NOT NULL
        AND COALESCE(current_setting('myapp.current_org_id', true), '') != ''
        AND org_id = current_setting('myapp.current_org_id', true)::text
    """

    for table in ("knowledge_base_documents", "knowledge_base_memory"):
        cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;")
        for policy, cmd, clause in [
            ("select_by_org", "SELECT", f"USING ({rls_using})"),
            ("insert_by_org", "INSERT", f"WITH CHECK ({rls_using})"),
            ("update_by_org", "UPDATE", f"USING ({rls_using})"),
            ("delete_by_org", "DELETE", f"USING ({rls_using})"),
        ]:
            cur.execute(f"DROP POLICY IF EXISTS {policy} ON {table};")
            cur.execute(
                f"CREATE POLICY {policy} ON {table} FOR {cmd} {clause};"
            )

    cur.execute("""
        CREATE OR REPLACE FUNCTION cleanup_stale_kb_documents()
        RETURNS TABLE(doc_id UUID, doc_user_id VARCHAR, doc_filename VARCHAR)
        LANGUAGE sql
        SECURITY DEFINER
        AS $$
            UPDATE knowledge_base_documents
            SET status = 'failed',
                error_message = 'Processing timed out. Please try uploading again.',
                updated_at = CURRENT_TIMESTAMP
            WHERE status IN ('processing', 'uploading')
              AND updated_at < CURRENT_TIMESTAMP - INTERVAL '3 minutes'
            RETURNING id, user_id, original_filename;
        $$;
    """)

    admin_conn.commit()


@pytest.fixture(scope="module")
def app_conn(ensure_rls):
    """Non-superuser connection where RLS is enforced."""
    conn = _app_connect()
    yield conn
    conn.close()


@pytest.fixture(autouse=True)
def seed_and_cleanup(admin_conn, app_conn, ensure_rls):
    """Insert test rows as superuser, run tests as app role, clean up after."""
    cur = admin_conn.cursor()

    # Seed as superuser (bypasses RLS)
    cur.execute(
        """
        INSERT INTO knowledge_base_documents
            (id, user_id, org_id, filename, original_filename, file_type,
             file_size_bytes, status)
        VALUES
            (gen_random_uuid(), %s, %s, 'a.pdf', 'a.pdf', 'pdf', 100, 'ready')
        """,
        (USER_A, ORG_A),
    )
    cur.execute(
        """
        INSERT INTO knowledge_base_documents
            (id, user_id, org_id, filename, original_filename, file_type,
             file_size_bytes, status)
        VALUES
            (gen_random_uuid(), %s, %s, 'b.pdf', 'b.pdf', 'pdf', 200, 'ready')
        """,
        (USER_B, ORG_B),
    )
    cur.execute(
        """
        INSERT INTO knowledge_base_memory (user_id, org_id, content)
        VALUES (%s, %s, 'memory for org A')
        ON CONFLICT (user_id, org_id) DO UPDATE SET content = EXCLUDED.content
        """,
        (USER_A, ORG_A),
    )
    cur.execute(
        """
        INSERT INTO knowledge_base_memory (user_id, org_id, content)
        VALUES (%s, %s, 'memory for org B')
        ON CONFLICT (user_id, org_id) DO UPDATE SET content = EXCLUDED.content
        """,
        (USER_B, ORG_B),
    )
    admin_conn.commit()

    yield

    # Cleanup as superuser
    cur = admin_conn.cursor()
    cur.execute(
        "DELETE FROM knowledge_base_documents WHERE org_id IN (%s, %s)",
        (ORG_A, ORG_B),
    )
    cur.execute(
        "DELETE FROM knowledge_base_memory WHERE org_id IN (%s, %s)",
        (ORG_A, ORG_B),
    )
    admin_conn.commit()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRLSIsolation:
    """Verify org A cannot see org B's data and vice versa."""

    def test_org_a_sees_only_own_documents(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_A)
        cur.execute("SELECT org_id, original_filename FROM knowledge_base_documents")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ORG_A
        assert rows[0][1] == "a.pdf"

    def test_org_b_sees_only_own_documents(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_B)
        cur.execute("SELECT org_id, original_filename FROM knowledge_base_documents")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ORG_B
        assert rows[0][1] == "b.pdf"

    def test_org_a_sees_only_own_memory(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_A)
        cur.execute("SELECT org_id, content FROM knowledge_base_memory")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ORG_A
        assert "org A" in rows[0][1]

    def test_org_b_sees_only_own_memory(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_B)
        cur.execute("SELECT org_id, content FROM knowledge_base_memory")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == ORG_B
        assert "org B" in rows[0][1]

    def test_org_a_cannot_update_org_b_documents(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_A)
        cur.execute(
            "UPDATE knowledge_base_documents SET status = 'hacked' WHERE org_id = %s",
            (ORG_B,),
        )
        assert cur.rowcount == 0
        app_conn.commit()

    def test_org_a_cannot_delete_org_b_documents(self, app_conn):
        cur = app_conn.cursor()
        _set_org(cur, app_conn, ORG_A)
        cur.execute(
            "DELETE FROM knowledge_base_documents WHERE org_id = %s", (ORG_B,)
        )
        assert cur.rowcount == 0
        app_conn.commit()


class TestDefaultDeny:
    """With no org context set, no rows should be visible (default-deny)."""

    def test_no_context_returns_no_documents(self, app_conn):
        cur = app_conn.cursor()
        _reset_org(cur, app_conn)
        cur.execute("SELECT * FROM knowledge_base_documents")
        assert cur.fetchall() == []

    def test_no_context_returns_no_memory(self, app_conn):
        cur = app_conn.cursor()
        _reset_org(cur, app_conn)
        cur.execute("SELECT * FROM knowledge_base_memory")
        assert cur.fetchall() == []

    def test_empty_org_returns_no_documents(self, app_conn):
        cur = app_conn.cursor()
        cur.execute("SET myapp.current_org_id = '';")
        app_conn.commit()
        cur.execute("SELECT * FROM knowledge_base_documents")
        assert cur.fetchall() == []


class TestSecurityDefinerCleanup:
    """The SECURITY DEFINER function should bypass RLS for the stale sweep."""

    def test_cleanup_finds_stale_docs_across_orgs(self, admin_conn, app_conn):
        cur = admin_conn.cursor()

        # Insert stale docs as superuser (bypasses RLS)
        cur.execute(
            """
            INSERT INTO knowledge_base_documents
                (id, user_id, org_id, filename, original_filename, file_type,
                 file_size_bytes, status, updated_at)
            VALUES
                (gen_random_uuid(), %s, %s, 'stale_a.pdf', 'stale_a.pdf', 'pdf',
                 50, 'processing', CURRENT_TIMESTAMP - INTERVAL '10 minutes')
            """,
            (USER_A, ORG_A),
        )
        cur.execute(
            """
            INSERT INTO knowledge_base_documents
                (id, user_id, org_id, filename, original_filename, file_type,
                 file_size_bytes, status, updated_at)
            VALUES
                (gen_random_uuid(), %s, %s, 'stale_b.pdf', 'stale_b.pdf', 'pdf',
                 50, 'uploading', CURRENT_TIMESTAMP - INTERVAL '10 minutes')
            """,
            (USER_B, ORG_B),
        )
        admin_conn.commit()

        # Call the SECURITY DEFINER function as non-superuser with NO org context
        app_cur = app_conn.cursor()
        _reset_org(app_cur, app_conn)
        app_cur.execute("SELECT * FROM cleanup_stale_kb_documents()")
        cleaned = app_cur.fetchall()
        app_conn.commit()

        cleaned_filenames = {row[2] for row in cleaned}
        assert "stale_a.pdf" in cleaned_filenames, "Cleanup missed stale doc from org A"
        assert "stale_b.pdf" in cleaned_filenames, "Cleanup missed stale doc from org B"

    def test_cleanup_does_not_touch_recent_processing_docs(self, admin_conn, app_conn):
        cur = admin_conn.cursor()

        cur.execute(
            """
            INSERT INTO knowledge_base_documents
                (id, user_id, org_id, filename, original_filename, file_type,
                 file_size_bytes, status, updated_at)
            VALUES
                (gen_random_uuid(), %s, %s, 'fresh.pdf', 'fresh.pdf', 'pdf',
                 50, 'processing', CURRENT_TIMESTAMP)
            """,
            (USER_A, ORG_A),
        )
        admin_conn.commit()

        app_cur = app_conn.cursor()
        _reset_org(app_cur, app_conn)
        app_cur.execute("SELECT * FROM cleanup_stale_kb_documents()")
        cleaned = app_cur.fetchall()
        app_conn.commit()

        cleaned_filenames = {row[2] for row in cleaned}
        assert "fresh.pdf" not in cleaned_filenames

    def test_cleanup_does_not_expose_data_via_return(self, admin_conn, app_conn):
        """The function only returns id, user_id, filename — no document content."""
        cur = admin_conn.cursor()

        cur.execute(
            """
            INSERT INTO knowledge_base_documents
                (id, user_id, org_id, filename, original_filename, file_type,
                 file_size_bytes, status, updated_at)
            VALUES
                (gen_random_uuid(), %s, %s, 'leak_test.pdf', 'leak_test.pdf', 'pdf',
                 50, 'processing', CURRENT_TIMESTAMP - INTERVAL '10 minutes')
            """,
            (USER_A, ORG_A),
        )
        admin_conn.commit()

        app_cur = app_conn.cursor()
        _reset_org(app_cur, app_conn)
        app_cur.execute("SELECT * FROM cleanup_stale_kb_documents()")
        cleaned = app_cur.fetchall()
        app_conn.commit()

        for row in cleaned:
            if row[2] == "leak_test.pdf":
                assert len(row) == 3


class TestCeleryStyleAccess:
    """Simulate Celery task access patterns with explicit set_rls_context."""

    def test_set_rls_context_allows_own_org_access(self, app_conn):
        cur = app_conn.cursor()
        cur.execute("SET myapp.current_user_id = %s;", (USER_A,))
        cur.execute("SET myapp.current_org_id = %s;", (ORG_A,))
        app_conn.commit()

        cur.execute(
            "SELECT original_filename FROM knowledge_base_documents WHERE user_id = %s",
            (USER_A,),
        )
        rows = cur.fetchall()
        assert len(rows) >= 1
        filenames = {r[0] for r in rows}
        assert "a.pdf" in filenames

    def test_set_rls_context_blocks_cross_org(self, app_conn):
        cur = app_conn.cursor()
        cur.execute("SET myapp.current_user_id = %s;", (USER_A,))
        cur.execute("SET myapp.current_org_id = %s;", (ORG_A,))
        app_conn.commit()

        cur.execute(
            "SELECT original_filename FROM knowledge_base_documents WHERE user_id = %s",
            (USER_B,),
        )
        assert cur.fetchall() == []

    def test_update_with_rls_context_works(self, app_conn):
        cur = app_conn.cursor()
        cur.execute("SET myapp.current_user_id = %s;", (USER_A,))
        cur.execute("SET myapp.current_org_id = %s;", (ORG_A,))
        app_conn.commit()

        cur.execute(
            """
            UPDATE knowledge_base_documents
            SET status = 'processing'
            WHERE user_id = %s AND original_filename = 'a.pdf'
            """,
            (USER_A,),
        )
        assert cur.rowcount == 1
        app_conn.rollback()

    def test_update_cross_org_blocked(self, app_conn):
        cur = app_conn.cursor()
        cur.execute("SET myapp.current_user_id = %s;", (USER_A,))
        cur.execute("SET myapp.current_org_id = %s;", (ORG_A,))
        app_conn.commit()

        cur.execute(
            """
            UPDATE knowledge_base_documents
            SET status = 'hacked'
            WHERE user_id = %s AND original_filename = 'b.pdf'
            """,
            (USER_B,),
        )
        assert cur.rowcount == 0
        app_conn.rollback()
