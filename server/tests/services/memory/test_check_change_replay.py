"""check_change replayed against the aurora-lab scenarios.

Memory bank: for every scenario a ``postmortem`` (root cause as content) and its
runbook. Inputs: the scenario's ``change`` sentence (as a PR description) and
its ``break.sh`` (as a diff).

* hit@3   the scenario's own postmortem is among ``related_incidents``
* precision  of the results with status MATCHES_PAST_INCIDENT, how many name the
          right postmortem
* false-positive rate  leave-one-out: remove the scenario's own postmortem and
          runbook; any MATCH is a false positive. Pessimistic by construction:
          the lab is 20 other incidents in the same namespace, so a wrong
          neighbour sharing ``worker`` or ``redis`` is often a *related* incident.

The sweep prints the trade-off between MATCH_THRESHOLD and MIN_STRONG_TERMS
(run with -s, or scripts/eval_memory_retrieval.py). The assertions pin the
current constants to what was observed, with slack; they are tripwires, not
targets.
"""

import pytest

from services.memory import change_check as cc
from tests.services.memory import aurora_lab_corpus as lab


@pytest.fixture(scope="module")
def replay():
    return lab.Replay()


@pytest.fixture(scope="module")
def sweeps(replay):
    out = {k: lab.sweep(replay, k) for k in ("change", "break.sh")}
    for kind, rows in out.items():
        print("\n" + lab.sweep_markdown(rows, kind, (cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS)))
    return out


def _row(rows, thr, ms):
    return next(r for r in rows if r["threshold"] == thr and r["min_strong"] == ms)


def test_bank_has_postmortem_and_runbook_per_scenario(replay):
    assert len(replay.full) == 42
    assert all(len(ix) == 40 for ix in replay.loo.values())
    assert sum(1 for s in replay.scenarios if s.break_sh) >= 20


@pytest.mark.parametrize("kind", ["change", "break.sh"])
def test_right_postmortem_is_found_at_current_constants(replay, kind):
    # Coverage-based abstention trades a little recall on this friendly corpus
    # for stability elsewhere (see test_recall_robustness.py): observed hit@3
    # 0.86 (change) / 0.81 (break.sh), precision 1.00.
    r = replay.run(kind)
    assert r["n"] >= 20
    assert r["hit@3"] >= 0.8
    assert r["match_rate"] >= 0.8
    assert r["precision"] >= 0.9


def test_change_description_ranks_own_postmortem_first_most_of_the_time(replay):
    assert replay.run("change")["top1"] >= 0.7


def test_leave_one_out_false_positive_rate_is_recorded(replay):
    """Honest tripwire on abstention at the shipped constants.

    Observed 0.05 (change) / 0.33 (break.sh) with coverage >= 0.35 or score
    >= 40, down from 0.81 with a bare score >= 6. Fails if it gets worse.
    """
    assert replay.run("change")["fp_rate"] <= 0.15
    assert replay.run("break.sh")["fp_rate"] <= 0.4


def test_stricter_threshold_lowers_false_positives_without_losing_the_hit(sweeps):
    for kind, rows in sweeps.items():
        cur = _row(rows, 6.0, 2)
        strict = _row(rows, 18.0, 2)
        assert strict["fp_rate"] < cur["fp_rate"]
        # Below-threshold incidents are not reported at all, so hit@3 here is
        # "the right incident is reported": 19/21 change sentences at 18.0.
        assert strict["hit@3"] >= 0.9
        assert strict["match_rate"] >= 0.9


def test_false_positive_rate_never_rises_with_threshold(sweeps):
    for rows in sweeps.values():
        for ms in lab.GRID_MIN_STRONG:
            seq = [r["fp_rate"] for r in rows if r["min_strong"] == ms]
            assert seq == sorted(seq, reverse=True)


def test_constants_restored_after_sweep(sweeps):
    assert (cc.MATCH_THRESHOLD, cc.MIN_STRONG_TERMS, cc.MIN_COVERAGE) == (40.0, 2, 0.35)


def test_leave_one_out_never_returns_the_removed_postmortem(replay):
    for s in replay.scenarios:
        res = cc.check_change(replay.loo[s.id], description=str(s.data["change"]))
        assert all(h["entry_id"] != f"pm-{s.id}" for h in res["related_incidents"])


def test_realistic_yaml_diff_matches_the_incident_it_repeats(replay):
    """The case the feature exists for: a PR that re-raises BATCH_SIZE.

    The key and the memory limit are *context* lines; only the value changed.
    """
    patch = (
        "@@ -18,7 +18,7 @@ spec:\n"
        "         env:\n"
        "         - name: BATCH_SIZE\n"
        '-          value: "500"\n'
        '+          value: "2500"\n'
        "         resources:\n"
        "           limits:\n"
        "             memory: 192Mi\n"
    )
    res = cc.check_change(
        replay.full, diff=patch, files=["k8s/shop/worker-deployment.yaml"],
        description="Speed up worker batches",
    )
    assert res["status"] == "MATCHES_PAST_INCIDENT"
    assert res["related_incidents"][0]["entry_id"] == "pm-01-worker-oom"
