"""Recall vs Aurora's previous memory rankers on the 21 aurora-lab runbooks.

Run with ``-s`` to see the table (``scripts/eval_memory_retrieval.py`` prints
the same one in markdown).
"""

import pytest

from tests.services.memory import aurora_lab_corpus as lab

OLD, SHORTLIST, NEW = lab.METHODS[0], lab.METHODS[1], lab.METHODS[2]
# Floors sit a little under the observed values so an unrelated tweak does not
# flap, while a real ranking regression does.
MIN_RECALL_AT_5 = {"alert": 0.65, "change": 0.95, "root_cause": 0.95}


@pytest.fixture(scope="module")
def results():
    res = lab.quality()
    print("\n" + lab.quality_markdown(res))
    return res


def test_corpus_is_complete():
    scenarios = lab.load_scenarios()
    assert len(scenarios) == 21
    docs = lab.runbook_entries(scenarios)
    alerts = [s.data["alert"].strip() for s in scenarios]
    assert all(a not in d["content"] for d in docs for a in alerts)
    assert all(d["title"] and d["description"] for d in docs)


@pytest.mark.parametrize("qset", lab.QUERY_SETS)
def test_recall_at_5_floor(results, qset):
    assert results[qset][NEW]["r@5"] >= MIN_RECALL_AT_5[qset]


@pytest.mark.parametrize("qset", lab.QUERY_SETS)
def test_mrr_beats_injector_shortlist(results, qset):
    assert results[qset][NEW]["mrr"] > results[qset][SHORTLIST]["mrr"]


@pytest.mark.parametrize("qset", lab.QUERY_SETS)
def test_beats_search_runbooks_filter(results, qset):
    # The all-tokens filter returns nothing for a natural-language query.
    assert results[qset][NEW]["mrr"] > results[qset][OLD]["mrr"]
