"""Unit tests for the risk taxonomy.

The taxonomy is a fixed contract: the validator drops findings whose
``category`` isn't in it, the prompt builder renders descriptions from
it, and the review-poster pulls human-readable labels from it. Any
change to the slug set is a schema migration on
``change_investigations.findings``, so we pin the surface here.
"""

from __future__ import annotations

from services.change_intercept import risk_taxonomy
from services.change_intercept.risk_taxonomy import (
    CATEGORIES,
    CATEGORIES_BY_SLUG,
    RiskCategory,
    all_categories,
    get_category,
)


def test_categories_tuple_is_frozen_and_non_empty() -> None:
    assert isinstance(CATEGORIES, tuple)
    assert len(CATEGORIES) == 12, (
        "Taxonomy size is a contract — adding/removing a category is a "
        "data migration on change_investigations.findings. Update the "
        "migration plan before changing this number."
    )


def test_every_slug_is_lowercase_snake_case_unique() -> None:
    seen: set[str] = set()
    for slug in CATEGORIES:
        assert slug == slug.lower(), f"category slug must be lowercase: {slug!r}"
        assert " " not in slug, f"slug must use underscores, not spaces: {slug!r}"
        assert slug not in seen, f"duplicate slug in taxonomy: {slug!r}"
        seen.add(slug)


def test_categories_by_slug_matches_categories_tuple() -> None:
    assert set(CATEGORIES_BY_SLUG.keys()) == set(CATEGORIES)


def test_every_category_has_label_description_and_at_least_one_example() -> None:
    for cat in all_categories():
        assert cat.label, f"missing label for {cat.slug}"
        assert cat.description, f"missing description for {cat.slug}"
        assert cat.examples, f"missing examples for {cat.slug}"
        assert all(
            isinstance(e, str) and e.strip() for e in cat.examples
        ), f"empty example in {cat.slug}"


def test_taxonomy_covers_phase_1a_promises() -> None:
    # Each of these is a category the design doc and the resolved open
    # questions explicitly promise to flag — surface them here so a
    # future refactor can't silently drop one.
    must_have = {
        "memory_leak",
        "unbounded_retry",
        "missing_timeout",
        "blocking_in_hot_path",
        "concurrency",
        "n_plus_one",
        "dangerous_config",
        "unsafe_migration",
        "secret_handling",
        "error_swallowing",
        "breaking_api_change",
        "dependency_risk",
    }
    assert set(CATEGORIES) == must_have


def test_get_category_returns_none_for_unknown_slug() -> None:
    assert get_category("not_a_real_category") is None
    assert get_category("") is None


def test_get_category_returns_full_category_for_known_slug() -> None:
    cat = get_category("memory_leak")
    assert isinstance(cat, RiskCategory)
    assert cat.slug == "memory_leak"
    assert "memory" in cat.label.lower()


def test_risk_category_is_frozen_dataclass() -> None:
    cat = get_category("memory_leak")
    assert cat is not None
    try:
        cat.slug = "rewritten"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("RiskCategory should be frozen")


def test_all_categories_returns_immutable_tuple() -> None:
    cats = all_categories()
    assert isinstance(cats, tuple)
    # Returned tuple is the same one referenced by CATEGORIES_BY_SLUG
    # — caller can't reorder or mutate.
    assert len(cats) == len(CATEGORIES)
