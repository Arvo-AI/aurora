"""Unit tests for services.memory.retrieval (tokenizer, chunking, BM25 index, per-org cache)."""

import time

import pytest

from services.memory import retrieval
from services.memory.retrieval import MemoryIndex, chunk_content, get_org_index, tokenize


def _entry(eid, title, content, category="runbook", description=""):
    return {"id": eid, "category": category, "title": title, "description": description,
            "content": content, "updated_at": None}


# Unrelated entries so IDF is meaningful (a term in every chunk scores ~0).
FILLER = [
    _entry(f"f{i}", f"Filler topic {i}", f"routine maintenance note number {i} about logging rotation and dashboards")
    for i in range(6)
]


class TestTokenize:
    def test_snake_case_compound_and_parts(self):
        toks = tokenize("BATCH_SIZE")
        assert "batch_size" in toks and "batch" in toks and "size" in toks

    def test_camel_case_compound_and_parts(self):
        toks = tokenize("KubePodCrashLooping")
        assert "kubepodcrashlooping" in toks
        assert {"kube", "pod", "crash", "looping"} <= set(toks)

    def test_image_ref_keeps_typo_and_parts(self):
        toks = tokenize("image nginx:1.27-alpne failed")
        assert "nginx:1.27-alpne" in toks
        assert {"nginx", "27", "alpne", "image", "failed"} <= set(toks)

    def test_stopwords_dropped(self):
        assert tokenize("the batch of the worker is in a loop") == ["batch", "worker", "loop"]

    def test_order_and_repeats_kept(self):
        assert tokenize("oom crash oom") == ["oom", "crash", "oom"]

    def test_empty(self):
        assert tokenize("") == [] and tokenize(None) == []

    def test_batch_size_matches_prose_and_config_key(self):
        assert {"batch", "size"} <= set(tokenize("raised the batch size"))
        assert {"batch", "size"} <= set(tokenize("batch_size=2500"))


class TestChunkContent:
    def test_empty_content_is_one_empty_chunk(self):
        for empty in ("", "   \n", None):
            cs = chunk_content(empty)
            assert len(cs) == 1 and (cs[0].offset, cs[0].end, cs[0].heading) == (0, 0, "")

    def test_offsets_map_back_to_content(self):
        content = "# Title\nfirst words here\n\n## Fix it\n" + " ".join(f"w{i}" for i in range(600))
        for c in chunk_content(content):
            text = content[c.offset:c.end]
            assert text == text.strip() and text
            assert content[c.offset] != " " and content[c.end - 1] != " "
        assert content[chunk_content(content)[0].offset:].startswith("# Title")

    def test_headings_captured(self):
        content = "intro text\n\n# Overview\nabout it\n\n## Root cause\nthe cause\n"
        assert [c.heading for c in chunk_content(content)] == ["", "Overview", "Root cause"]

    def test_long_section_splits_with_overlap(self):
        words = [f"w{i}" for i in range(500)]
        content = " ".join(words)
        cs = chunk_content(content)
        assert len(cs) >= 2
        first, second = content[cs[0].offset:cs[0].end].split(), content[cs[1].offset:cs[1].end].split()
        assert len(first) == retrieval.CHUNK_WORDS
        overlap = set(first) & set(second)
        assert len(overlap) == retrieval.CHUNK_OVERLAP
        assert cs[1].offset < cs[0].end
        # every word is covered by some chunk
        covered = set()
        for c in cs:
            covered |= set(content[c.offset:c.end].split())
        assert covered == set(words)

    def test_heading_must_start_a_line(self):
        content = "use # not a heading here\nand more"
        assert len(chunk_content(content)) == 1


class TestMemoryIndex:
    def test_body_match_beats_title_only_near_miss(self):
        body = _entry("body", "Notes on worker restarts",
                      "The worker container was OOMKilled because the memory limit was exceeded by the batch.")
        near = _entry("near", "OOMKilled memory limit exceeded", "Unrelated text about dashboards and logging.")
        index = MemoryIndex(FILLER + [near, body])
        hits = index.search("worker OOMKilled memory limit exceeded batch", k=3)
        assert [h.entry_id for h in hits][0] == "body"
        assert "near" in [h.entry_id for h in hits]

    def test_hit_carries_chunk_offset_and_heading(self):
        content = "# Intro\nnothing\n\n## Root cause\nthe redis host was misspelled as redis-mastr"
        index = MemoryIndex(FILLER + [_entry("r", "Redis incident", content)])
        hit = index.search("redis-mastr misspelled", k=1)[0]
        assert hit.entry_id == "r" and hit.heading == "Root cause"
        assert content[hit.offset:].startswith("## Root cause")
        assert "redis-mastr" in hit.chunk
        assert "redis-mastr" in hit.matched_terms

    def test_category_filter(self):
        a = _entry("pm", "Postmortem checkout", "checkout crashed after the cache flag change", category="postmortem")
        b = _entry("rb", "Runbook checkout", "checkout crashed after the cache flag change", category="runbook")
        index = MemoryIndex(FILLER + [a, b])
        assert {h.entry_id for h in index.search("checkout cache flag")} == {"pm", "rb"}
        assert [h.entry_id for h in index.search("checkout cache flag", categories=["postmortem"])] == ["pm"]
        assert index.search("checkout cache flag", categories=["learned"]) == []

    def test_repeated_query_terms_do_not_change_ranking(self):
        a = _entry("a", "Alpha", "oomkilled once. " + "filler " * 50)
        b = _entry("b", "Beta", "oomkilled oomkilled oomkilled batch size. " + "filler " * 5)
        index = MemoryIndex(FILLER + [a, b])
        once = index.search("oomkilled batch", k=5)
        spam = index.search("oomkilled " * 30 + "batch", k=5)
        assert [h.entry_id for h in once] == [h.entry_id for h in spam]
        assert [round(h.score, 6) for h in once] == [round(h.score, 6) for h in spam]

    def test_unknown_terms_and_empty_index(self):
        assert MemoryIndex(FILLER).search("zzzunknownterm") == []
        assert MemoryIndex([]).search("anything") == []
        assert MemoryIndex(FILLER).search("") == []

    def test_k_limits_results_and_scores_descending(self):
        entries = [_entry(f"e{i}", f"Redis note {i}", "redis " * (i + 1)) for i in range(5)]
        hits = MemoryIndex(FILLER + entries).search("redis", k=3)
        assert len(hits) == 3
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)

    def test_title_and_description_count_for_every_chunk(self):
        content = " ".join(["filler"] * 400) + " tailword"
        e = _entry("long", "Quasar outage", content, description="pulsar relay")
        index = MemoryIndex(FILLER + [e])
        assert index.search("quasar pulsar", k=1)[0].entry_id == "long"

    def test_pretokenized_query(self):
        index = MemoryIndex(FILLER + [_entry("x", "Cache flag", "the cache flag flipped")])
        assert index.search(["cache", "flag"], k=1)[0].entry_id == "x"


class TestRedaction:
    SECRET = "AKIA6ODU7H4ZLXKDNQ3X"  # published fake from tests/test_output_redaction.py

    def _index(self):
        e = _entry("leaky", "Deploy notes",
                   f"Rotated the deploy credential. AWS_ACCESS_KEY_ID={self.SECRET} was pasted in the ticket.")
        return MemoryIndex(FILLER + [e])

    def test_chunk_redacted_by_default(self):
        hit = self._index().search("deploy credential rotated", k=1)[0]
        assert hit.entry_id == "leaky"
        assert self.SECRET not in hit.chunk
        assert "Rotated the deploy credential" in hit.chunk

    def test_redaction_can_be_disabled(self):
        hit = self._index().search("deploy credential rotated", k=1, redact_chunks=False)[0]
        assert self.SECRET in hit.chunk

    def test_secret_not_in_serialized_hit(self):
        hit = self._index().search("deploy credential rotated", k=1)[0]
        assert self.SECRET not in str(hit.to_dict())

    def test_withholds_chunk_when_redactor_unavailable(self, monkeypatch):
        import utils.security.output_redaction as red

        def boom(_):
            raise RuntimeError("engine down")

        monkeypatch.setattr(red, "redact", boom)
        hit = self._index().search("deploy credential rotated", k=1)[0]
        assert hit.chunk == ""


class TestOrgCache:
    @pytest.fixture(autouse=True)
    def _clean(self):
        retrieval.clear_cache()
        yield
        retrieval.clear_cache()

    @staticmethod
    def _loader(entries, calls):
        def load():
            calls.append(1)
            return entries
        return load

    def test_orgs_never_share_an_index(self):
        calls = []
        fp = (1, "t0")
        a = get_org_index("org-a", fp, self._loader([_entry("a1", "Secret of A", "alpha only content")], calls))
        b = get_org_index("org-b", fp, self._loader([_entry("b1", "Thing of B", "beta only content")], calls))
        assert a is not b and len(calls) == 2
        assert [h.entry_id for h in a.search("alpha beta", k=5)] == ["a1"]
        assert [h.entry_id for h in b.search("alpha beta", k=5)] == ["b1"]
        # asking again with the same fingerprint still returns each org's own index
        assert get_org_index("org-a", fp, self._loader([], calls)) is a
        assert get_org_index("org-b", fp, self._loader([], calls)) is b

    def test_same_fingerprint_does_not_reload(self):
        calls = []
        load = self._loader([_entry("x", "T", "content here")], calls)
        first = get_org_index("org", (1, "t"), load)
        assert get_org_index("org", (1, "t"), load) is first
        assert len(calls) == 1

    def test_changed_fingerprint_reloads(self):
        calls = []
        first = get_org_index("org", (1, "t"), self._loader([_entry("x", "T", "old content")], calls))
        second = get_org_index("org", (2, "t2"), self._loader([_entry("y", "T", "new content")], calls))
        assert second is not first and len(calls) == 2
        assert second.search("new")[0].entry_id == "y"
        assert first.search("new") == []

    def test_lru_bound(self, monkeypatch):
        monkeypatch.setattr(retrieval, "_CACHE_MAX_ORGS", 3)
        calls = []
        load = self._loader([_entry("x", "T", "content")], calls)
        for i in range(3):
            get_org_index(f"o{i}", (1,), load)
        get_org_index("o0", (1,), load)       # touch o0 so o1 is the least recently used
        get_org_index("o3", (1,), load)       # evicts o1
        assert len(retrieval._cache) == 3
        assert set(retrieval._cache) == {"o0", "o2", "o3"}
        n = len(calls)
        get_org_index("o1", (1,), load)       # was evicted: reloads
        assert len(calls) == n + 1
        assert len(retrieval._cache) == 3

    def test_clear_cache(self):
        calls = []
        load = self._loader([_entry("x", "T", "content")], calls)
        get_org_index("org", (1,), load)
        retrieval.clear_cache()
        get_org_index("org", (1,), load)
        assert len(calls) == 2


class TestSmallBankMatching:
    """A new org with one or two postmortems must still be able to match."""

    def test_single_postmortem_bank_matches_its_change(self):
        from services.memory import change_check as cc

        index = MemoryIndex([_entry("pm", "Postmortem: billing outage",
                                    "## Root Cause\nThe billing worker BATCH_SIZE was raised and it was OOMKilled.",
                                    category="postmortem")])
        diff = ("diff --git a/k8s/worker.yaml b/k8s/worker.yaml\n@@ -1,3 +1,3 @@\n"
                "   - name: BATCH_SIZE\n-    value: \"500\"\n+    value: \"2500\"\n")
        assert cc.check_change(index, diff=diff)["status"] == "MATCHES_PAST_INCIDENT"

    def test_single_postmortem_bank_ignores_an_unrelated_change(self):
        from services.memory import change_check as cc

        index = MemoryIndex([_entry("pm", "Postmortem: billing outage",
                                    "## Root Cause\nThe billing worker BATCH_SIZE was raised and it was OOMKilled.",
                                    category="postmortem")])
        diff = "diff --git a/README.md b/README.md\n@@ -1 +1 @@\n-# Shop\n+# Shop storefront\n"
        assert cc.check_change(index, diff=diff)["status"] == "NO_MATCH"


class TestIncidentScope:
    """Only postmortems and the Incident Index count as past incidents."""

    DIFF = ("diff --git a/k8s/worker.yaml b/k8s/worker.yaml\n@@ -1,3 +1,3 @@\n"
            "   - name: BATCH_SIZE\n-    value: \"500\"\n+    value: \"2500\"\n")
    TEXT = "The worker BATCH_SIZE was raised from 500 to 2500 and the worker was OOMKilled."

    # Enough unrelated entries that a term shared by two entries is still rare,
    # as it is in any real bank (hundreds of chunks).
    PADDING = [
        _entry(f"pad{i}", f"Runbook {i}", f"Service svc{i} handles queue q{i} and region r{i}. "
               f"Restart pod p{i} when latency on endpoint e{i} exceeds its SLO.")
        for i in range(40)
    ]

    def _bank(self, *extra):
        return MemoryIndex(FILLER + self.PADDING + list(extra))

    def test_ordinary_artifact_report_is_not_a_past_incident(self):
        from services.memory import change_check as cc

        report = _entry("rep", "Weekly capacity report", self.TEXT, category="artifact")
        res = cc.check_change(self._bank(report), diff=self.DIFF)
        assert res["status"] == "NO_MATCH"
        assert res["related_incidents"] == []

    def test_incident_index_line_is_a_past_incident(self):
        from services.memory import change_check as cc

        idx_entry = _entry("ii", "Incident Index",
                           f"- [INC 1a2b | 2026-10-02 | worker | resolved] {self.TEXT}", category="artifact")
        res = cc.check_change(self._bank(idx_entry), diff=self.DIFF)
        assert res["status"] == "MATCHES_PAST_INCIDENT"
        assert [h["entry_id"] for h in res["related_incidents"]] == ["ii"]

    def test_postmortem_still_matches_alongside_a_report(self):
        from services.memory import change_check as cc

        pm = _entry("pm", "Postmortem: worker OOM", f"## Root Cause\n{self.TEXT}", category="postmortem")
        report = _entry("rep", "Weekly capacity report", self.TEXT, category="artifact")
        res = cc.check_change(self._bank(pm, report), diff=self.DIFF)
        assert [h["entry_id"] for h in res["related_incidents"]] == ["pm"]


class TestColdBuildsAndBudget:
    @pytest.fixture(autouse=True)
    def _clean(self):
        retrieval.clear_cache()
        yield
        retrieval.clear_cache()

    @staticmethod
    def _slow_loader(entries, delay, calls):
        def load():
            calls.append(1)
            time.sleep(delay)
            return ("fp", entries)
        return load

    def test_cold_org_does_not_block_a_caller_that_cannot_wait(self):
        calls = []
        loader = self._slow_loader(FILLER, 0.3, calls)
        t = time.monotonic()
        got = retrieval.get_org_index("o", "fp", lambda: FILLER, rebuild=loader, wait=0)
        assert got is None
        assert time.monotonic() - t < 0.1
        for _ in range(50):
            time.sleep(0.05)
            got = retrieval.get_org_index("o", "fp", lambda: FILLER, rebuild=loader, wait=0)
            if got is not None:
                break
        assert got is not None and len(got) == len(FILLER)

    def test_bounded_wait_returns_the_index_when_it_is_ready_in_time(self):
        calls = []
        got = retrieval.get_org_index("o", "fp", lambda: FILLER,
                                      rebuild=self._slow_loader(FILLER, 0.05, calls), wait=2.0)
        assert got is not None

    def test_concurrent_cold_callers_share_one_build(self):
        import threading

        calls = []
        loader = self._slow_loader(FILLER, 0.2, calls)
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            retrieval.get_org_index("o", "fp", lambda: FILLER, rebuild=loader, wait=2.0)))
            for _ in range(5)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        assert len(calls) == 1
        assert all(r is not None for r in results)

    def test_size_budget_evicts_least_recently_used_orgs(self, monkeypatch):
        big = [_entry(f"b{i}", f"T{i}", "word " * 2000) for i in range(5)]
        one = MemoryIndex(big).approx_bytes()
        monkeypatch.setattr(retrieval, "_CACHE_MAX_BYTES", int(one * 2.5))
        for org in ("a", "b", "c"):
            retrieval.get_org_index(org, "fp", lambda: big)
        assert set(retrieval._cache) == {"b", "c"}

    def test_check_change_reports_warming_instead_of_no_match(self, monkeypatch):
        from services.memory import change_check as cc

        monkeypatch.setattr(cc, "org_index_for_cursor", lambda *a, **k: None)
        res = cc.check_change_for_org(object(), "o", user_id="u", diff="x")
        assert res["status"] == "INDEX_WARMING"
        assert res["related_incidents"] == []
