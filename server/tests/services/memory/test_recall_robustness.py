"""How robust is check_change's abstention rule outside the corpus it was tuned on?

Conditions (see aurora_lab_corpus.robustness): A baseline bank, B split-half
calibration, C website docs + lab FINDINGS/REPORT as distractors, D long
Aurora-style postmortems, E = C + D. Criteria: absolute score (current),
idf coverage, margin over the rest of the bank, strong-term count.

Run with ``-s`` to print the tables (or ``scripts/eval_memory_retrieval.py
--robustness``). The assertions are loose and honest tripwires, not targets;
in particular no criterion reaches 0.85 held-out hit@3 on 21 change sentences
at split-half, which the printed tables show.
"""

import pytest

from tests.services.memory import aurora_lab_corpus as lab

pytestmark = pytest.mark.skipif(not lab.DOCS_DIR.is_dir(), reason="website/docs not present")


@pytest.fixture(scope="module")
def scenarios():
    return lab.load_scenarios()


@pytest.fixture(scope="module")
def res(scenarios):
    r = lab.robustness(scenarios)
    print("\n" + lab.robustness_markdown(r, scenarios))
    return r


def test_distractors_are_loaded(res):
    d = res["data"]
    assert d["A baseline"].n_entries == 42
    assert d["C distractors"].n_entries > 42 + 20


def test_recall_at_5_survives_distractors(scenarios):
    q = lab.quality_with_distractors(scenarios, True)
    m = lab.METHODS[2]
    assert q["alert"][m]["r@5"] >= 0.6
    assert q["change"][m]["r@5"] >= 0.9
    assert q["root_cause"][m]["r@5"] >= 0.9
    # and it still beats the legacy shortlist at r@5 on every query set
    for qset in q.values():
        assert qset[m]["r@5"] >= qset[lab.METHODS[1]]["r@5"]


def test_best_criterion_split_half_hit_at_3(res):
    """Best criterion = highest mean held-out (hit@3 - fp_rate), per input kind."""
    for kind in ("change", "break.sh"):
        def score(c):
            rs = [r["heldout"] for r in res["split"][kind][c].values()]
            return sum(r["hit@3"] - r["fp_rate"] for r in rs) / len(rs)
        best = max(lab.CRITERIA, key=score)
        rs = [r["heldout"] for r in res["split"][kind][best].values()]
        hit = sum(r["hit@3"] for r in rs) / len(rs)
        print(f"best criterion on {kind}: {best}, mean held-out hit@3 {hit:.2f}")
        assert hit >= 0.75  # honest: well under the 0.85 one might hope for


def test_absolute_threshold_drifts_under_distractors_and_long_postmortems(res):
    """The score threshold calibrated on A lets more neighbours through in E."""
    a = res["metrics"]["A baseline"]["change"]["absolute"]["fp_rate"]
    e = res["metrics"]["E C+D"]["change"]["absolute"]["fp_rate"]
    assert e >= a


def test_shipped_rule_false_matches_stay_bounded_across_conditions(res):
    """The point of coverage-based abstention: no condition sends false matches past ~0.45."""
    for data in res["data"].values():
        for kind in ("change", "break.sh"):
            m = lab.evaluate_rule(data.samples[kind], lab.shipped_rule)
            assert m["fp_rate"] <= 0.45, (kind, m)
            assert m["hit@3"] >= 0.8, (kind, m)
