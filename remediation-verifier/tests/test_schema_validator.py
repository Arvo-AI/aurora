"""Tests for the validator that decides whether the contract is well formed.

`verifier/schema.py` guards `contracts/scenario_12.yaml`. A validator that
accepted everything would leave every contract test green, so each test below
breaks one keyword of the schema and requires an error that names the path.
The schema under test is written here on purpose: these tests check the
instrument, and a fault in the contract must not be able to hide them.
"""

from __future__ import annotations

from typing import Any

import pytest

from verifier.schema import SchemaValidator

SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["name"],
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string", "minLength": 3, "pattern": "^[a-z]+$"},
        "count": {"type": "integer", "minimum": 1},
        "ratio": {"type": "number"},
        "flag": {"type": "boolean"},
        "nothing": {"type": "null"},
        "choice": {"type": "string", "enum": ["left", "right"]},
        "tags": {"type": "array", "minItems": 1, "items": {"type": "string"}},
        "nested": {"$ref": "#/$defs/nested"},
    },
    "$defs": {
        "nested": {
            "type": "object",
            "required": ["id"],
            "properties": {"id": {"type": "integer"}},
        }
    },
}


def errors_for(value: Any) -> list[str]:
    return SchemaValidator(SCHEMA).errors(value)


def test_a_valid_document_produces_no_errors() -> None:
    value = {"name": "abc", "count": 2, "tags": ["x"], "nested": {"id": 1}}
    assert errors_for(value) == []


def test_a_missing_required_key_is_reported_at_the_root() -> None:
    assert errors_for({}) == ["$: missing required key 'name'"]


def test_an_unexpected_key_is_reported() -> None:
    assert errors_for({"name": "abc", "extra": 1}) == ["$: unexpected key 'extra'"]


def test_a_wrong_type_is_reported_with_both_names() -> None:
    assert errors_for({"name": 7}) == ["$.name: expected string, found int"]


def test_a_short_string_is_reported() -> None:
    assert errors_for({"name": "ab"}) == ["$.name: shorter than 3 characters"]


def test_a_string_that_breaks_the_pattern_is_reported() -> None:
    (error,) = errors_for({"name": "ABC"})
    assert "does not match ^[a-z]+$" in error


def test_a_number_below_the_minimum_is_reported() -> None:
    assert errors_for({"name": "abc", "count": 0}) == ["$.count: 0 is below 1"]


def test_a_number_below_the_minimum_keeps_the_declared_value() -> None:
    assert errors_for({"name": "abc", "ratio": 0.5}) == []


def test_a_boolean_is_not_an_integer() -> None:
    assert errors_for({"name": "abc", "count": True}) == ["$.count: expected integer, found bool"]


def test_an_empty_array_is_reported() -> None:
    assert errors_for({"name": "abc", "tags": []}) == ["$.tags: fewer than 1 items"]


def test_an_array_item_carries_its_index() -> None:
    assert errors_for({"name": "abc", "tags": ["x", 2]}) == [
        "$.tags[1]: expected string, found int"
    ]


def test_a_value_outside_the_enum_is_reported() -> None:
    (error,) = errors_for({"name": "abc", "choice": "up"})
    assert "is not one of" in error


def test_a_local_reference_resolves_and_keeps_the_path() -> None:
    assert errors_for({"name": "abc", "nested": {}}) == ["$.nested: missing required key 'id'"]


def test_a_remote_reference_is_refused() -> None:
    with pytest.raises(AssertionError, match="only local references"):
        SchemaValidator({"$ref": "https://example.com/schema.json"}).errors("x")


def test_a_reference_to_a_missing_definition_is_refused() -> None:
    with pytest.raises(AssertionError, match="does not resolve"):
        SchemaValidator({"$ref": "#/$defs/absent"}).errors("x")


def test_a_type_the_validator_does_not_implement_is_refused() -> None:
    with pytest.raises(AssertionError, match="type this validator cannot honour"):
        SchemaValidator({"type": "colour"}).errors("x")


def test_a_keyword_the_validator_does_not_implement_is_refused() -> None:
    with pytest.raises(AssertionError, match="cannot honour"):
        SchemaValidator({"type": "string", "maxLength": 3})


def test_an_unimplemented_keyword_inside_a_definition_is_refused() -> None:
    schema = {"$defs": {"inner": {"type": "string", "format": "date"}}}
    with pytest.raises(AssertionError, match=r"\$\.\$defs\.inner"):
        SchemaValidator(schema)
