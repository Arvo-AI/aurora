"""Content evidence in the selector manifest at every bank size."""

import asyncio
from datetime import datetime, timedelta

import services.memory.retrieval as r
from services.memory import injector
from services.memory.injector import _select_relevant_memories_async
from services.memory.retrieval import Hit


def _hit(cat, title, terms):
    return Hit(entry_id="x", category=cat, title=title, description="", score=1.0,
               offset=0, heading="", chunk="", matched_terms=terms)


def _bank(n):
    old = datetime(2020, 1, 1)
    es = [{"category": "context", "title": f"note-{i}", "description": "office note",
           "updated_at": old + timedelta(days=i)} for i in range(n)]
    es.append({"category": "learned", "title": "Postmortem: High CPU on api",
               "description": "cpu", "updated_at": datetime(2019, 1, 1)})
    return es


def _run(monkeypatch, entries, user_id="u"):
    cap = {}

    class _Resp:
        content = '{"selected": []}'

    class _LLM:
        async def ainvoke(self, messages, config=None):
            cap["prompt"] = messages[1].content
            return _Resp()

    monkeypatch.setattr(injector, "create_chat_model", lambda *a, **k: _LLM())
    asyncio.run(_select_relevant_memories_async("why cpu spiked", entries, user_id))
    return cap["prompt"]


def test_small_bank_annotates_only_matching_entry(monkeypatch):
    monkeypatch.setattr(r, "search_memory_for_user", lambda *a, **k: [
        _hit("learned", "Postmortem: High CPU on api",
             ["gc\nthrash", "[heap]", "x" * 80, "a", "b", "c"])])
    lines = _run(monkeypatch, _bank(5)).splitlines()
    pm = [l for l in lines if "Postmortem: High CPU" in l][0]
    assert pm.endswith(" [body matches: gc thrash, heap, " + "x" * 40 + ", a]")
    assert sum("[body matches" in l for l in lines) == 1


def test_search_failure_keeps_old_format(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(r, "search_memory_for_user", boom)
    p1 = _run(monkeypatch, _bank(5))
    monkeypatch.setattr(r, "search_memory_for_user", lambda *a, **k: [])
    assert p1 == _run(monkeypatch, _bank(5))
    assert "body matches" not in p1


def test_timeout_keeps_old_format(monkeypatch):
    import time
    monkeypatch.setattr(injector, "_CONTENT_SHORTLIST_TIMEOUT", 0.05)
    monkeypatch.setattr(r, "search_memory_for_user",
                        lambda *a, **k: time.sleep(0.3) or [_hit("learned", "Postmortem: High CPU on api", ["gc"])])
    assert "body matches" not in _run(monkeypatch, _bank(5))


def test_no_user_id_no_search(monkeypatch):
    monkeypatch.setattr(r, "search_memory_for_user", lambda *a, **k: 1 / 0)
    assert "body matches" not in _run(monkeypatch, _bank(5), user_id=None)


def test_large_bank_front_loads_and_searches_once(monkeypatch):
    calls = []

    def search(*a, **k):
        calls.append(1)
        return [_hit("learned", "Postmortem: High CPU on api", ["gc"])]

    monkeypatch.setattr(r, "search_memory_for_user", search)
    prompt = _run(monkeypatch, _bank(150))
    assert len(calls) == 1
    first = [l for l in prompt.splitlines() if l.startswith("- [")][0]
    assert "Postmortem: High CPU" in first and "[body matches: gc]" in first


def test_system_prompt_mentions_marker():
    assert "[body matches:" in injector._SELECTOR_SYSTEM
