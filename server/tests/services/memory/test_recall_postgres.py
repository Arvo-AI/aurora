"""Production Recall end to end on a real Postgres: schema, RLS, routes, SQL.

Skipped unless Postgres is reachable through the POSTGRES_* variables (as the
other Postgres-backed tests are). Run it against a throwaway database with a
NON-superuser role -- superusers bypass row-level security, which is exactly
what these tests check:

    POSTGRES_HOST=... POSTGRES_PORT=5432 POSTGRES_USER=aurora \\
    POSTGRES_PASSWORD=... POSTGRES_DB=aurora_it \\
    python -m pytest tests/services/memory/test_recall_postgres.py -s

It builds Aurora's real schema (``initialize_tables``), creates two orgs,
writes memory through the real ``POST /api/memory/entries`` route, and then
exercises every recall path the PR touches against that data. Where the PR
replaces an older behaviour it runs the old one on the same rows (the
``legacy_*`` helpers are frozen copies of the main-branch logic) and prints
the comparison.
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from unittest.mock import MagicMock

import pytest

from tests.services.memory import aurora_lab_corpus as lab

psycopg2 = pytest.importorskip("psycopg2")


def _reachable() -> bool:
    try:
        psycopg2.connect(
            host=os.environ.get("POSTGRES_HOST"), port=os.environ.get("POSTGRES_PORT"),
            user=os.environ.get("POSTGRES_USER"), password=os.environ.get("POSTGRES_PASSWORD"),
            dbname=os.environ.get("POSTGRES_DB"), connect_timeout=2,
        ).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="PostgreSQL is not reachable")

RUN = uuid.uuid4().hex[:8]
ORG_A, ORG_B = f"org-a-{RUN}", f"org-b-{RUN}"
USER_A, USER_B = f"user-a-{RUN}", f"user-b-{RUN}"

YAML_DIFF = (
    "diff --git a/k8s/shop/worker-deployment.yaml b/k8s/shop/worker-deployment.yaml\n"
    "@@ -18,7 +18,7 @@ spec:\n"
    "         env:\n"
    "         - name: BATCH_SIZE\n"
    '-          value: "500"\n'
    '+          value: "2500"\n'
    "         resources:\n"
    "           limits:\n"
    "             memory: 192Mi\n"
)


@pytest.fixture(scope="module")
def schema():
    from utils.db import db_utils
    from utils.db.connection_pool import db_pool

    db_utils.initialize_tables()
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        if cur.fetchone()[0]:
            pytest.skip("connected as a superuser: RLS would be bypassed")
        for uid, org in ((USER_A, ORG_A), (USER_B, ORG_B)):
            cur.execute("INSERT INTO organizations (id, name, slug) VALUES (%s, %s, %s)", (org, org, org))
            cur.execute(
                "INSERT INTO users (id, email, password_hash, org_id) VALUES (%s, %s, 'x', %s)",
                (uid, f"{uid}@example.test", org),
            )
        conn.commit()
    yield
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        for uid, org in ((USER_A, ORG_A), (USER_B, ORG_B)):
            cur.execute("SET myapp.current_org_id = %s", (org,))
            cur.execute("DELETE FROM artifacts WHERE org_id = %s", (org,))
        cur.execute("RESET myapp.current_org_id")
        cur.execute("DELETE FROM users WHERE id IN (%s, %s)", (USER_A, USER_B))
        cur.execute("DELETE FROM organizations WHERE id IN (%s, %s)", (ORG_A, ORG_B))
        conn.commit()


@pytest.fixture(scope="module")
def client(schema):
    """The real memory blueprint on the real pool; RBAC resolves identity from headers."""
    pytest.importorskip("flask")
    mp = pytest.MonkeyPatch()
    for heavy in ("celery_config", "celery", "routes.audit_routes"):
        if heavy not in sys.modules:
            mp.setitem(sys.modules, heavy, MagicMock())
    from flask import Flask, request

    from routes.memory import routes as memory_routes
    from utils.auth import rbac_decorators as rbac

    who = lambda *a, **k: request.headers.get("X-User-ID")  # noqa: E731
    org = lambda *a, **k: request.headers.get("X-Org-ID")  # noqa: E731
    mp.setattr(rbac, "get_user_id_from_request", who)
    mp.setattr(rbac, "get_org_id_from_request", org)
    mp.setattr(rbac, "enforce_with_reload", lambda *a, **k: True)
    mp.setattr(rbac, "_audit_auth_failure", lambda *a, **k: None)
    mp.setattr(memory_routes, "get_org_id_from_request", org)
    import utils.auth.viewer_run as viewer_run
    mp.setattr(viewer_run, "viewer_may_edit_memory", lambda *a, **k: True)

    app = Flask(__name__)  # NOSONAR
    app.register_blueprint(memory_routes.memory_bp, url_prefix="/api/memory")
    c = app.test_client()

    scenarios = lab.load_scenarios()
    as_a = {"X-User-ID": USER_A, "X-Org-ID": ORG_A}
    for s, rb in zip(scenarios, lab.runbook_entries(scenarios)):
        for body in (
            {"category": "runbook", "title": rb["title"], "description": rb["description"], "content": rb["content"]},
            {"category": "postmortem", "title": f"Postmortem: {s.title}",
             # What every postmortem looked like before this PR: the alert name.
             "description": f"Postmortem for incident: {s.title}",
             "content": lab.long_postmortem(s)},
        ):
            r = c.post("/api/memory/entries", json=body, headers=as_a)
            assert r.status_code == 201, r.get_json()
    r = c.post("/api/memory/entries", headers={"X-User-ID": USER_B, "X-Org-ID": ORG_B}, json={
        "category": "postmortem", "title": "Postmortem: org B billing outage",
        "description": "Postmortem for incident: billing outage",
        "content": "## Root Cause\nThe billing worker BATCH_SIZE was raised and it was OOMKilled.",
    })
    assert r.status_code == 201
    yield c, scenarios
    mp.undo()


def _hdr(user, org):
    return {"X-User-ID": user, "X-Org-ID": org}


def test_search_route_ranks_by_body_on_real_rows(client):
    c, scenarios = client
    r = c.get("/api/memory/search", query_string={"q": "worker OOMKilled after BATCH_SIZE raised to 2500", "k": 3},
              headers=_hdr(USER_A, ORG_A))
    assert r.status_code == 200, r.get_json()
    titles = [h["title"] for h in r.get_json()["hits"]]
    assert any("Worker OOMKilled" in t or "OOMKilled" in t for t in titles), titles


def test_rls_keeps_orgs_apart_on_every_recall_path(client):
    from services.memory.change_check import check_change_for_user
    from services.memory.retrieval import search_memory_for_user

    c, _ = client
    r = c.get("/api/memory/search", query_string={"q": "BATCH_SIZE OOMKilled worker"}, headers=_hdr(USER_B, ORG_B))
    assert [h["title"] for h in r.get_json()["hits"]] == ["Postmortem: org B billing outage"]
    assert all("org B" not in h.title for h in search_memory_for_user(USER_A, "billing outage BATCH_SIZE", k=50))
    res = check_change_for_user(USER_B, diff=YAML_DIFF)
    assert {h["title"] for h in res["related_incidents"]} <= {"Postmortem: org B billing outage"}


def test_check_change_route_and_pr_review_path_match_the_repeated_incident(client):
    from services.memory.change_check import check_change_for_user

    c, _ = client
    r = c.post("/api/memory/check-change", json={"diff": YAML_DIFF, "description": "Speed up worker batches"},
               headers=_hdr(USER_A, ORG_A))
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body["status"] == "MATCHES_PAST_INCIDENT"
    assert "Worker OOMKilled" in body["related_incidents"][0]["title"]

    bg = check_change_for_user(USER_A, diff=YAML_DIFF, files=["k8s/shop/worker-deployment.yaml"],
                               description="Speed up worker batches")
    assert bg["status"] == "MATCHES_PAST_INCIDENT"


def test_reviewer_prompt_before_and_after_on_real_memory(client):
    from services.change_gating.verdict import _REVIEW_PROMPT, build_review_prompt
    from services.memory.change_check import check_change_for_user

    pr = {"number": 7, "title": "Speed up worker batches", "body": "", "base": {"ref": "main"},
          "head": {"ref": "f", "sha": "abc"}, "user": {"login": "agent"}}
    files = [{"filename": "k8s/shop/worker-deployment.yaml", "patch": YAML_DIFF.split("\n", 1)[1]}]
    ctx = check_change_for_user(USER_A, diff=YAML_DIFF, files=[f["filename"] for f in files],
                                description=pr["title"])
    prompt = build_review_prompt("acme/shop", pr, files, None, incident_context=ctx)
    head = prompt[:2000]
    print(f"\n[reviewer] before: selector input = fixed instructions (starts {_REVIEW_PROMPT[:40]!r}...)")
    print(f"[reviewer] after: selector input names the file: {'worker-deployment.yaml' in head}; "
          f"matched: {[h['title'] for h in ctx['related_incidents']]}")
    assert "worker-deployment.yaml" in head
    assert "<org_memory>" in prompt and "Worker OOMKilled" in prompt


def test_mcp_search_runbooks_before_and_after_on_real_rows(client):
    c, scenarios = client
    entries = c.get("/api/memory/entries", query_string={"category": "runbook"},
                    headers=_hdr(USER_A, ORG_A)).get_json()["entries"]
    old_hits = new_hits = 0
    for s in scenarios:
        q = " ".join(str(s.data["change"]).split())
        old = lab.legacy_search_runbooks(
            [{"id": e["title"], "title": e["title"], "description": e.get("description") or ""} for e in entries], q,
        )
        new = c.get("/api/memory/search", query_string={"q": q, "category": "runbook,context", "k": 5},
                    headers=_hdr(USER_A, ORG_A)).get_json()["hits"]
        rb = next(e["title"] for e in entries if e["title"] == lab.runbook_entries([s])[0]["title"])
        old_hits += rb in old
        new_hits += rb in [h["title"] for h in new]
    print(f"\n[search_runbooks] right runbook in top 5 for the causing change: "
          f"main {old_hits}/{len(scenarios)} -> branch {new_hits}/{len(scenarios)}")
    assert new_hits > old_hits


def test_grep_memories_runs_on_real_sql_and_ranks(client):
    import json

    from chat.backend.agent.tools.memory_tool import grep_memories
    from services.memory import retrieval

    retrieval.clear_cache()
    out = json.loads(grep_memories("BATCH_SIZE", user_id=USER_A))
    assert out.get("status") == "ok", out
    # Warm the org index (grep never builds inline), then grep again: ranked.
    retrieval.search_memory_for_user(USER_A, "warm", k=1)
    out = json.loads(grep_memories("BATCH_SIZE", user_id=USER_A))
    titles = [m.get("title") for m in out.get("matches") or out.get("results") or []]
    print(f"\n[grep_memories BATCH_SIZE] top results: {titles[:3]}")
    assert titles and "Worker OOMKilled" in " ".join(titles[:3])


def test_prefetch_search_never_blocks_on_a_cold_org_then_serves_hits(client):
    from services.memory import retrieval

    retrieval.clear_cache()
    t = time.monotonic()
    first = retrieval.search_memory_for_user(USER_A, "worker OOMKilled batch", k=3, block=False)
    assert first == [] and time.monotonic() - t < 1.0
    for _ in range(50):  # background build
        time.sleep(0.1)
        hits = retrieval.search_memory_for_user(USER_A, "worker OOMKilled batch", k=3, block=False)
        if hits:
            break
    assert hits, "background build never populated the cache"


def test_backfill_rewrites_placeholder_postmortem_descriptions(client):
    from services.memory.postmortem_backfill import run_backfill
    from utils.db.connection_pool import db_pool

    stats = run_backfill()
    assert stats["errors"] == 0, stats
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SET myapp.current_org_id = %s", (ORG_A,))
        cur.execute("SELECT description FROM artifacts WHERE org_id = %s AND category = 'postmortem'", (ORG_A,))
        descs = [r[0] for r in cur.fetchall()]
        cur.execute("RESET myapp.current_org_id")
    rewritten = [d for d in descs if not d.startswith("Postmortem for incident:")]
    print(f"\n[backfill] {len(rewritten)}/{len(descs)} postmortem descriptions now state the cause, e.g. {rewritten[:1]}")
    assert len(rewritten) >= len(descs) * 0.9
    assert run_backfill()["updated"] == 0  # idempotent


def test_memory_selector_input_before_and_after_for_a_pr_review(client, monkeypatch):
    """What the prefetch selector LLM is shown for a PR review, main vs branch.

    The LLM is stubbed to capture its prompt (no API key needed); everything
    else -- memory entries, content search, prompt assembly -- is the real code
    on the real rows.
    """
    import asyncio
    import re

    from services.change_gating.verdict import build_review_prompt
    from services.memory import injector, retrieval
    from services.memory.change_check import check_change_for_user
    from services.memory.queries import get_memory_entries

    seen = []

    class _LLM:
        async def ainvoke(self, messages, **_):
            seen.append(messages[-1].content)
            return type("R", (), {"content": '{"selected": []}'})()

    monkeypatch.setattr(injector, "create_chat_model", lambda *a, **k: _LLM())
    retrieval.search_memory_for_user(USER_A, "warm", k=1)  # warm the org index

    pr = {"number": 7, "title": "Speed up worker batches", "body": "", "base": {"ref": "main"},
          "head": {"ref": "f", "sha": "abc"}, "user": {"login": "agent"}}
    files = [{"filename": "k8s/shop/worker-deployment.yaml", "patch": YAML_DIFF.split("\n", 1)[1]}]
    entries = get_memory_entries(USER_A)
    # main: prompt starts with the fixed instructions; no content search.
    old_prompt = build_review_prompt("acme/shop", pr, files, None).split("\n\n", 1)[1]
    asyncio.run(injector._select_relevant_memories_async(old_prompt, entries))
    # branch: focus header + recall context, content search on.
    ctx = check_change_for_user(USER_A, diff=YAML_DIFF, files=[files[0]["filename"]], description=pr["title"])
    new_prompt = build_review_prompt("acme/shop", pr, files, None, incident_context=ctx)
    asyncio.run(injector._select_relevant_memories_async(new_prompt, entries, user_id=USER_A))

    before, after = seen
    oom = [l for l in after.splitlines() if l.startswith("- [postmortem]") and "Worker OOMKilled after a batch size change" in l]
    print("\n[selector] before: message mentions the changed file:", "worker-deployment" in before.split("MEMORY INDEX:")[0])
    print("[selector] after:  message mentions the changed file:", "worker-deployment" in after.split("MEMORY INDEX:")[0])
    print("[selector] before: entries marked [body matches]:", before.count("[body matches:"))
    print("[selector] after:  entries marked [body matches]:", after.count("[body matches:"))
    print("[selector] after, the repeated incident's line:", oom[:1])
    assert "worker-deployment" not in before.split("MEMORY INDEX:")[0]
    assert "worker-deployment" in after.split("MEMORY INDEX:")[0]
    assert "[body matches:" not in before and any("[body matches:" in l for l in oom)
