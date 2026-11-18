"""Aurora Watch's SQL against a real Postgres: detector state, settings, clusters, incidents.

Runs when POSTGRES_HOST/PORT point at an initialized Aurora database and skips
otherwise, like the other DB tests. The code under test opens its own pooled
connections; here they all borrow the test's connection, whose transaction is
rolled back at the end, so nothing is left behind.
"""

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import psycopg2.errors
import pytest
from psycopg2 import sql

from services.watch import report, store
from services.watch.detector import group_findings

from .k8s import healthy_logs, shop
from .test_detector import learned, run


class _Borrowed:
    """The test's connection, lent to code that opens its own: commit() leaves the work
    inside the test transaction, which teardown rolls back."""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        pass

    def rollback(self):
        raise AssertionError("the code under test rolled back its transaction")

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def db(db_session, monkeypatch):
    from utils.db.connection_pool import db_pool

    @contextmanager
    def lend():
        yield _Borrowed(db_session)

    monkeypatch.setattr(db_pool, "get_admin_connection", lend)
    monkeypatch.setattr(db_pool, "get_user_connection", lend)
    return db_session


def _org(cur, name="watch"):
    org, user = str(uuid.uuid4()), str(uuid.uuid4())
    cur.execute("INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)", (org, name, org))
    cur.execute(
        """INSERT INTO users (id, email, password_hash, name, org_id, role)
           VALUES (%s, %s, 'x', 't', %s, 'admin')""",
        (user, f"{user}@example.com", org),
    )
    return org, user


def _uid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TestState:
    def test_round_trip_and_status(self, db):
        with db.cursor() as cur:
            _, user = _org(cur)
        finding = {"subject": "Deployment/shop/api", "severity": "high", "summary": "0 of 2 replicas ready"}
        state = {"polls": 3, "config": {"learning_polls": 5}, "open": {"k1": finding}}

        store.save_state(user, "kc-1", state)

        assert store.load_state(user, "kc-1") == state
        [row] = store.status(user)
        assert (row["cluster_id"], row["polls"], row["learning"]) == ("kc-1", 3, True)
        assert row["open_findings"] == [finding]
        assert (row["last_error"], row["blind_spots"]) == (None, [])
        assert row["last_poll_at"]

    def test_a_failed_poll_records_its_error_and_keeps_the_learned_state(self, db):
        with db.cursor() as cur:
            _, user = _org(cur)
        state = {"polls": 7, "open": {}}
        store.save_state(user, "kc-1", state)

        store.save_state(user, "kc-1", None, blind_spots=["networkpolicies"], error="kubectl timed out")

        assert store.load_state(user, "kc-1") == state
        [row] = store.status(user)
        assert (row["polls"], row["last_error"], row["blind_spots"]) == (7, "kubectl timed out", ["networkpolicies"])

    def test_a_first_poll_that_fails_stores_no_state(self, db):
        with db.cursor() as cur:
            _, user = _org(cur)

        store.save_state(user, "kc-1", None, error="forbidden")

        assert store.load_state(user, "kc-1") is None
        [row] = store.status(user)
        assert (row["polls"], row["learning"], row["last_error"]) == (0, True, "forbidden")

    def test_each_org_sees_only_its_own_rows_under_row_level_security(self, db):
        with db.cursor() as cur:
            org_a, user_a = _org(cur, "a")
            org_b, user_b = _org(cur, "b")
        store.save_state(user_a, "kc-1", {"polls": 1, "who": "a"})
        store.save_state(user_b, "kc-1", {"polls": 2, "who": "b"})  # the same cluster id in another org

        assert store.load_state(user_a, "kc-1")["who"] == "a"
        assert store.load_state(user_b, "kc-1")["who"] == "b"

        # The queries filter by org themselves; the policy must hold even where one would
        # not. Superusers bypass RLS, so probe as an unprivileged role.
        with db.cursor() as cur:
            cur.execute("SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()[0]:
                pytest.skip("probing RLS needs a role that can create roles")
            role = sql.Identifier(f"watch_rls_probe_{uuid.uuid4().hex[:8]}")
            cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
            cur.execute(sql.SQL("GRANT SELECT, INSERT ON aurora_watch_state TO {}").format(role))
            cur.execute(sql.SQL("SET LOCAL ROLE {}").format(role))

            cur.execute("SET myapp.current_org_id = %s", (org_b,))
            cur.execute("SELECT org_id FROM aurora_watch_state")
            assert cur.fetchall() == [(org_b,)]
            cur.execute("SAVEPOINT probe")
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("INSERT INTO aurora_watch_state (org_id, cluster_id) VALUES (%s, 'kc-2')", (org_a,))
            cur.execute("ROLLBACK TO SAVEPOINT probe")

            cur.execute("SET myapp.current_org_id = ''")
            cur.execute("SELECT count(*) FROM aurora_watch_state")
            assert cur.fetchone()[0] == 0  # no org context, no rows
            cur.execute("RESET ROLE")


class TestSettings:
    def test_round_trip_and_the_tick_finds_the_org(self, db):
        from services.watch.tasks import _enabled_orgs

        with db.cursor() as cur:
            org, user = _org(cur)
            other_org, other_user = _org(cur, "off")
        assert store.get_settings(org)["enabled"] is False

        store.save_settings(org, enabled=True, owner_user_id=user, namespaces=["shop"])
        store.save_settings(other_org, enabled=False, owner_user_id=other_user, namespaces=None)

        got = store.get_settings(org)
        assert (got["enabled"], got["owner_user_id"], got["namespaces"]) == (True, user, ["shop"])
        enabled = dict(_enabled_orgs())
        assert enabled[org]["owner_user_id"] == user
        assert other_org not in enabled

    def test_saving_again_replaces_the_setting(self, db):
        with db.cursor() as cur:
            org, user = _org(cur)
        store.save_settings(org, enabled=True, owner_user_id=user, namespaces=["shop"])

        store.save_settings(org, enabled=False, owner_user_id=user, namespaces=None)

        assert store.get_settings(org)["enabled"] is False
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM user_preferences WHERE org_id = %s AND preference_key = %s",
                        (org, store.PREF_KEY))
            assert cur.fetchone()[0] == 1


class TestClusters:
    def test_lists_the_orgs_connected_agent_and_kubeconfig_clusters(self, db):
        live, gone, kc, retired, foreign = (_uid(p) for p in ("agent", "agent", "kc", "kc", "kc"))
        with db.cursor() as cur:
            org, user = _org(cur)
            other_org, other_user = _org(cur, "other")
            cur.execute("SET myapp.current_org_id = %s", (org,))
            for cluster_id, name, status in ((live, "prod-east", "active"), (gone, "old", "stale")):
                token = _uid("token")
                cur.execute(
                    """INSERT INTO kubectl_agent_tokens (token, user_id, org_id, cluster_name, cluster_id, status)
                       VALUES (%s, %s, %s, %s, %s, 'active')""",
                    (token, user, org, name, cluster_id),
                )
                cur.execute("INSERT INTO active_kubectl_connections (token, cluster_id, status) VALUES (%s, %s, %s)",
                            (token, cluster_id, status))
            cur.execute(
                """INSERT INTO kubeconfig_clusters
                     (user_id, org_id, cluster_id, context_name, cluster_name, vault_provider, is_active)
                   VALUES (%s, %s, %s, 'ctx', 'staging', 'vault', TRUE),
                          (%s, %s, %s, 'ctx', 'retired', 'vault', FALSE)""",
                (user, org, kc, user, org, retired),
            )
            cur.execute("SET myapp.current_org_id = %s", (other_org,))
            cur.execute(
                """INSERT INTO kubeconfig_clusters
                     (user_id, org_id, cluster_id, context_name, cluster_name, vault_provider, is_active)
                   VALUES (%s, %s, %s, 'ctx', 'not-ours', 'vault', TRUE)""",
                (other_user, other_org, foreign),
            )

        got = sorted(store.list_clusters(user), key=lambda c: c["source"])

        assert got == [{"cluster_id": live, "name": "prod-east", "source": "agent"},
                       {"cluster_id": kc, "name": "staging", "source": "kubeconfig"}]


CLUSTER = {"cluster_id": "kc-1", "name": "prod-east"}


def _group():
    """A real group: a Service selector edited to match no pod, its client timing out."""
    c = shop()
    state = learned(c)
    c.set_selector("frontend", {"app": "frontend", "tier": "web"})
    logs = healthy_logs()
    logs["Deployment/shop/loadgen"] = ["GET /api/items -> timeout"] * 3
    return next(g for g in group_findings(run(c, state, 3, logs)) if g["opens_incident"])


def _joined(group, name):
    """The same group a poll later, when one more workload's finding confirmed."""
    late = {**group["findings"][0], "subject": f"Deployment/shop/{name}", "name": name}
    return {**group, "findings": group["findings"] + [late]}


@pytest.fixture
def dispatched(monkeypatch):
    """SSE, the quick summary and the Celery RCA task are stubbed; the RCA chat session is real."""
    import chat.background.summarization as summarization
    import chat.background.task as task_mod
    import routes.incidents_sse as sse

    calls = SimpleNamespace(rca=[], summaries=[])

    def delay(**kwargs):
        # Where Celery is stubbed, every task is the same mock; tell the two apart by arguments.
        sent = calls.rca if "session_id" in kwargs else calls.summaries
        sent.append(kwargs)
        return SimpleNamespace(id=f"celery-{len(sent)}")

    monkeypatch.setattr(sse, "broadcast_incident_update_to_user_connections", lambda *a, **k: None)
    monkeypatch.setattr(summarization.generate_incident_summary, "delay", delay)
    monkeypatch.setattr(task_mod.run_background_chat, "delay", delay)
    monkeypatch.setattr(task_mod, "is_background_chat_allowed", lambda user_id: True)
    return calls


class TestIncidents:
    def test_opens_an_incident_with_its_alert_lifecycle_and_rca(self, db, dispatched):
        from services.watch.incidents import open_incident

        with db.cursor() as cur:
            org, user = _org(cur)
        group = _group()

        result = open_incident(user, org, CLUSTER, group)

        assert result["status"] == "opened"
        incident_id = result["incident_id"]
        with db.cursor() as cur:
            cur.execute(
                """SELECT source_type, status, severity, alert_title, alert_service, affected_services,
                          rca_celery_task_id, alert_metadata
                     FROM incidents WHERE id = %s""",
                (incident_id,),
            )
            source, status, severity, title, service, affected, task_id, metadata = cur.fetchone()
            assert (source, status, severity) == ("aurora_watch", "investigating", group["severity"])
            assert (title, service) == (report.incident_title(group), report.incident_service(group))
            assert affected == sorted({f["name"] for f in group["findings"]})
            assert task_id == "celery-1"
            assert metadata["aurora_watch"]["findings"]
            cur.execute("SELECT source_type, correlation_strategy FROM incident_alerts WHERE incident_id = %s",
                        (incident_id,))
            assert cur.fetchall() == [("aurora_watch", "primary")]
            cur.execute("SELECT event_type, new_value FROM incident_lifecycle_events WHERE incident_id = %s",
                        (incident_id,))
            assert cur.fetchall() == [("created", "investigating")]
            cur.execute("SELECT title, ui_state FROM chat_sessions WHERE incident_id = %s", (incident_id,))
            [(chat_title, ui_state)] = cur.fetchall()
        assert chat_title == f"RCA: {title}"
        assert ui_state["triggerMetadata"]["source"] == "aurora_watch"
        [rca] = dispatched.rca
        assert rca["incident_id"] == incident_id
        assert "ROOT CAUSE ANALYSIS REQUIRED - AURORA_WATCH ALERT" in rca["initial_message"]
        assert rca["rail_text"]
        assert len(dispatched.summaries) == 1

    def test_findings_confirmed_later_join_the_open_incident(self, db, dispatched):
        from services.watch.incidents import open_incident

        with db.cursor() as cur:
            org, user = _org(cur)
        group = _group()
        first = open_incident(user, org, CLUSTER, group)["incident_id"]

        assert open_incident(user, org, CLUSTER, _joined(group, "extra")) == {"incident_id": first, "status": "updated"}

        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM incidents WHERE org_id = %s", (org,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT alert_metadata, affected_services FROM incidents WHERE id = %s", (first,))
            metadata, affected = cur.fetchone()
        subjects = [f["subject"] for f in metadata["aurora_watch"]["findings"]]
        assert subjects.count("Deployment/shop/extra") == 1
        assert "extra" in affected
        assert len(dispatched.rca) == 1  # one RCA per incident, not per finding

    def test_a_recurrence_after_resolve_opens_a_new_incident_that_collects_what_follows(self, db, dispatched):
        from services.watch.incidents import open_incident

        with db.cursor() as cur:
            org, user = _org(cur)
        group = _group()
        first = open_incident(user, org, CLUSTER, group)["incident_id"]
        with db.cursor() as cur:
            cur.execute("UPDATE incidents SET status = 'resolved' WHERE id = %s", (first,))

        again = open_incident(user, org, CLUSTER, group)
        later = open_incident(user, org, CLUSTER, _joined(group, "extra"))

        assert again["status"] == "opened" and again["incident_id"] != first
        assert later == {"incident_id": again["incident_id"], "status": "updated"}
        with db.cursor() as cur:
            cur.execute("SELECT count(*) FROM incidents WHERE org_id = %s", (org,))
            assert cur.fetchone()[0] == 2
        assert len(dispatched.rca) == 2

    def test_a_finding_that_got_worse_raises_the_incident_and_replaces_its_old_copy(self, db, dispatched):
        from services.watch.incidents import open_incident

        with db.cursor() as cur:
            org, user = _org(cur)
        group = _group()
        first = open_incident(user, org, CLUSTER, {**group, "severity": "high"})["incident_id"]
        worse = {**group["findings"][0], "severity": "critical", "summary": "it got worse"}
        escalated = {**group, "severity": "critical", "findings": [worse] + group["findings"][1:]}

        assert open_incident(user, org, CLUSTER, escalated) == {"incident_id": first, "status": "updated"}

        with db.cursor() as cur:
            cur.execute("SELECT severity, alert_metadata FROM incidents WHERE id = %s", (first,))
            severity, metadata = cur.fetchone()
        assert severity == "critical"
        findings = metadata["aurora_watch"]["findings"]
        assert [f["summary"] for f in findings if f["subject"] == worse["subject"]] == ["it got worse"]
        assert metadata["aurora_watch"]["severity"] == "critical"
