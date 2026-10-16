"""Connector reassignment against Postgres: RLS-visible rows and unique keys."""

import uuid

import psycopg2.errors
import pytest

from services.connectors.ownership import reassign_connectors_to_admin


def _ids():
    return {
        "org": str(uuid.uuid4()),
        "admin": str(uuid.uuid4()),
        "leaving": str(uuid.uuid4()),
    }


def _seed(cur, ids):
    cur.execute(
        "INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)",
        (ids["org"], "ownership-test", ids["org"]),
    )
    for user_id, email, created in (
        (ids["admin"], f"admin-{ids['admin']}@example.com", "2000-01-01"),
        (ids["leaving"], f"leaving-{ids['leaving']}@example.com", "2000-01-02"),
    ):
        cur.execute(
            """INSERT INTO users (id, email, password_hash, name, org_id, role, created_at)
               VALUES (%s, %s, %s, %s, %s, 'admin', %s)""",
            (user_id, email, "x", "t", ids["org"], created),
        )


def test_reassign_moves_org_credentials_and_drops_personal_oauth(db_session):
    ids = _ids()
    with db_session.cursor() as cur:
        _seed(cur, ids)
        cur.execute("SET myapp.current_org_id = %s", (ids["org"],))
        cur.execute(
            """INSERT INTO user_tokens (user_id, org_id, secret_ref, provider, is_active)
               VALUES (%s, %s, %s, 'grafana', TRUE),
                      (%s, %s, %s, 'slack', TRUE)""",
            (
                ids["leaving"], ids["org"], "vault:test/grafana",
                ids["leaving"], ids["org"], "vault:test/slack",
            ),
        )
        cur.execute(
            """INSERT INTO user_connections (user_id, org_id, provider, account_id, status)
               VALUES (%s, %s, 'aws', 'acct-shared', 'active'),
                      (%s, %s, 'aws', 'acct-shared', 'active'),
                      (%s, %s, 'ovh', 'proj-1', 'active')""",
            (
                ids["admin"], ids["org"],
                ids["leaving"], ids["org"],
                ids["leaving"], ids["org"],
            ),
        )
        # Superusers bypass RLS, so only they can insert the null-org row the
        # policy hides from the app role. The reassignment must still ignore it.
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        superuser = cur.fetchone()[0]
        if superuser:
            cur.execute(
                """INSERT INTO user_tokens (user_id, org_id, secret_ref, provider, is_active)
                   VALUES (%s, NULL, %s, 'datadog', TRUE)""",
                (ids["leaving"], "vault:test/datadog"),
            )

        moved = reassign_connectors_to_admin(cur, ids["org"], ids["leaving"])

        cur.execute(
            "SELECT user_id, provider FROM user_tokens WHERE org_id = %s ORDER BY provider",
            (ids["org"],),
        )
        assert cur.fetchall() == [(ids["admin"], "grafana")]
        if superuser:
            cur.execute(
                "SELECT user_id FROM user_tokens WHERE provider = 'datadog' AND secret_ref = %s",
                ("vault:test/datadog",),
            )
            assert cur.fetchone()[0] == ids["leaving"]
        cur.execute(
            """SELECT user_id, provider, account_id FROM user_connections
               WHERE org_id = %s ORDER BY provider, user_id""",
            (ids["org"],),
        )
        assert cur.fetchall() == [
            (ids["admin"], "aws", "acct-shared"),
            (ids["admin"], "ovh", "proj-1"),
        ]
        # grafana moved, slack deleted, shared aws dropped, ovh moved.
        assert moved == 4


def test_second_token_for_the_same_org_provider_is_rejected(db_session):
    ids = _ids()
    with db_session.cursor() as cur:
        _seed(cur, ids)
        cur.execute(
            """INSERT INTO user_tokens (user_id, org_id, secret_ref, provider, is_active)
               VALUES (%s, %s, %s, 'grafana', TRUE)""",
            (ids["leaving"], ids["org"], "vault:test/grafana"),
        )
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                """INSERT INTO user_tokens (user_id, org_id, secret_ref, provider, is_active)
                   VALUES (%s, %s, %s, 'grafana', TRUE)""",
                (ids["admin"], ids["org"], "vault:test/grafana-2"),
            )
        db_session.rollback()
