"""When a check may open cases, and what counts as it having been right.

Promotion is the only path from a model-written command to a person's
attention, so these are the rules that keep it narrow.
"""

from services.detection.record import (
    PatternRecord,
    asset_matches,
    earns_promotion,
    loses_proven,
)


def test_a_pattern_with_too_few_real_catches_does_not_promote():
    assert not earns_promotion(PatternRecord(hits=2, false_fires=0), 0)
    assert earns_promotion(PatternRecord(hits=3, false_fires=0), 0)


def test_a_pattern_that_is_often_wrong_does_not_promote_however_many_hits():
    # 8 of 11 is 73%: plenty of hits, still wrong too often to interrupt people.
    assert not earns_promotion(PatternRecord(hits=8, false_fires=3), 0)
    assert earns_promotion(PatternRecord(hits=8, false_fires=2), 0)


def test_a_check_that_fired_on_nothing_is_not_carried_by_its_pattern():
    strong = PatternRecord(hits=10, false_fires=0)
    assert not earns_promotion(strong, 1)


def test_two_false_fires_in_a_row_lose_the_right():
    assert not loses_proven(1)
    assert loses_proven(2)


def test_an_incident_on_a_similarly_named_service_is_not_credit():
    # Credit is what promotes a check, so `api` must not be paid for `api-gateway`.
    assert not asset_matches("api", ["api-gateway", "public-api"])
    assert asset_matches("api", ["api-gateway", " API "])


def test_missing_service_names_never_match():
    assert not asset_matches("checkout", [None, ""])
    assert not asset_matches("", ["", None])
