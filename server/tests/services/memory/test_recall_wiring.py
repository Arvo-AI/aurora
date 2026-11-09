"""Postmortem recall, content-aware shortlist and ranked grep."""

import asyncio
import json
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import MagicMock

from services.memory import injector
from services.memory.injector import _MAX_SELECTOR_CANDIDATES, _content_first_shortlist
from services.memory.retrieval import Hit

INC = "11111111-2222-3333-4444-555555555555"


# --------------------------- A. postmortems ---------------------------

def test_description_uses_root_cause_section():
    from chat.backend.agent.tools.postmortem_tool import _postmortem_description

    md = (
        "## Summary\nSomething broke.\n\n## Root Cause\n"
        "The **BATCH_SIZE** ConfigMap change to 10000 OOM-killed api-worker. " + "x " * 200 +
        "\n\n## Impact\nlots\n"
    )
    d = _postmortem_description(md, "Alert", INC)
    assert d.startswith("The BATCH_SIZE ConfigMap change")
    assert len(d) <= 205 and "Impact" not in d


def test_description_falls_back():
    from chat.backend.agent.tools.postmortem_tool import _postmortem_description

    assert _postmortem_description("## Summary\nhi", "Disk full", INC) == "Postmortem for incident: Disk full"
    assert _postmortem_description("", None, INC) == f"Postmortem for incident: {INC}"


def test_save_postmortem_stores_cause_description(monkeypatch):
    from chat.backend.agent.tools import postmortem_tool as mod

    cursor = MagicMock()
    cursor.fetchone.side_effect = [("org1", "High CPU"), ("art1",)]
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor

    @contextmanager
    def _conn():
        yield conn

    monkeypatch.setattr(mod.db_pool, "get_admin_connection", _conn)
    monkeypatch.setattr(mod, "set_rls_context", lambda *a, **k: "org1")
    monkeypatch.setattr(mod, "create_version", lambda *a, **k: 1)
    out = json.loads(mod.save_postmortem(INC, "## Root Cause\nPool exhaustion in payments-db under load.\n", user_id="u"))
    assert out["status"] == "ok"
    insert_params = cursor.execute.call_args_list[1][0][1]
    assert insert_params[4] == "Pool exhaustion in payments-db under load."


def test_default_instructions_ask_for_guardrail():
    from services.actions.postmortem_action import DEFAULT_POSTMORTEM_INSTRUCTIONS as t

    assert t.rstrip().splitlines()[-1] != ""
    assert 'write_memory(category="learned", title="Guardrail:' in t
    assert "Invariant" in t and "How to check" in t and "root cause was identified" in t
    assert t.index("save_postmortem") < t.index("Guardrail")


# ------------------------ B. content shortlist ------------------------

def _entries(n=150):
    old = datetime(2020, 1, 1)
    es = [{"category": "context", "title": f"note-{i}", "description": "office note",
           "updated_at": old + timedelta(days=i)} for i in range(n)]
    es.append({"category": "learned", "title": "Guardrail: batch", "description": "misc",
               "updated_at": datetime(2019, 1, 1)})
    return es


def _hit(cat, title):
    return Hit(entry_id="x", category=cat, title=title, description="", score=1.0,
               offset=0, heading="", chunk="")


def test_content_hit_enters_shortlist_first(monkeypatch):
    import services.memory.retrieval as r

    monkeypatch.setattr(r, "search_memory_for_user",
                        lambda uid, q, k=5, **kw: [_hit("learned", "Guardrail: batch")])
    es = _entries()
    pool = asyncio.run(_content_first_shortlist("zzz unrelated", es, _MAX_SELECTOR_CANDIDATES, "u"))
    assert pool[0]["title"] == "Guardrail: batch"
    assert len(pool) == _MAX_SELECTOR_CANDIDATES
    assert len({injector._entry_key(e) for e in pool}) == len(pool)
    # remainder is the lexical ranking (newest first when nothing overlaps)
    assert pool[1]["title"] == "note-149"


def test_search_failure_falls_back_to_lexical(monkeypatch):
    import services.memory.retrieval as r

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(r, "search_memory_for_user", boom)
    es = _entries()
    pool = asyncio.run(_content_first_shortlist("zzz", es, _MAX_SELECTOR_CANDIDATES, "u"))
    assert pool == injector._shortlist_entries("zzz", es, _MAX_SELECTOR_CANDIDATES)
    assert "Guardrail: batch" not in [e["title"] for e in pool]


def test_no_user_id_skips_search(monkeypatch):
    import services.memory.retrieval as r

    monkeypatch.setattr(r, "search_memory_for_user", lambda *a, **k: 1 / 0)
    es = _entries()
    assert asyncio.run(_content_first_shortlist("zzz", es, 100, None)) == \
        injector._shortlist_entries("zzz", es, 100)


# ----------------------------- C. grep -----------------------------

def _grep(monkeypatch, rows, query):
    from chat.backend.agent.tools import memory_tool as mod

    cursor = MagicMock()
    cursor.fetchall.return_value = rows

    class _C:
        def __enter__(self):
            return cursor, MagicMock(), "org"

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(mod, "_memory_connection", lambda *a, **k: _C())
    return mod, cursor, json.loads(mod.grep_memories(query, user_id="u"))


def test_grep_ranks_best_match_above_recency(monkeypatch):
    rows = [(f"new-{i}", "learned", "mentions redis once in passing") for i in range(11)]
    rows.append(("oldest", "learned", "redis failover redis sentinel redis primary redis replica " * 3))
    mod, cursor, out = _grep(monkeypatch, rows, "redis")
    titles = [m["title"] for m in out["matches"]]
    assert len(titles) == 10 and "oldest" in titles
    assert cursor.execute.call_args_list[1][0][1][-1] == mod._GREP_CANDIDATES


def test_grep_ranking_error_keeps_recency(monkeypatch):
    import services.memory.retrieval as r

    monkeypatch.setattr(r, "MemoryIndex", lambda *a, **k: 1 / 0)
    rows = [(f"t{i}", "learned", "redis") for i in range(12)]
    _, _, out = _grep(monkeypatch, rows, "redis")
    assert [m["title"] for m in out["matches"]] == [f"t{i}" for i in range(10)]
