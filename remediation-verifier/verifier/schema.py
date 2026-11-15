"""The subset of JSON Schema that `contracts/schema.json` uses.

The contract is validated with this module rather than a `jsonschema`
dependency, because one file does not justify a runtime dependency (D12).

The class fails closed. A keyword, a type or a reference it does not implement
raises instead of being ignored, so an edit to the schema cannot weaken the
validation without failing the build. The constructor walks the whole schema
and refuses the unknown keywords, which means the coverage of this class is
checked on every run rather than by reading it.
"""

from __future__ import annotations

import re
from typing import Any

KNOWN_KEYWORDS = frozenset(
    {
        "$defs",
        "$ref",
        "$schema",
        "additionalProperties",
        "description",
        "enum",
        "items",
        "minItems",
        "minLength",
        "minimum",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
    }
)

NAMED_SUBSCHEMAS = ("properties", "$defs")


class SchemaValidator:
    """Validate one document against one schema whose references stay local."""

    def __init__(self, root: dict[str, Any]) -> None:
        self.root = root
        self._assert_supported(root, "$")

    def errors(self, value: Any, schema: dict[str, Any] | None = None) -> list[str]:
        return self._validate(value, schema if schema is not None else self.root, "$")

    def _assert_supported(self, schema: Any, where: str) -> None:
        if not isinstance(schema, dict):
            return
        unknown = sorted(set(schema) - KNOWN_KEYWORDS)
        if unknown:
            raise AssertionError(f"{where}: keywords this validator cannot honour: {unknown}")
        for keyword in NAMED_SUBSCHEMAS:
            named = schema.get(keyword, {})
            if isinstance(named, dict):
                for name, child in named.items():
                    self._assert_supported(child, f"{where}.{keyword}.{name}")
        if "items" in schema:
            self._assert_supported(schema["items"], f"{where}.items")

    def _resolve(self, reference: str) -> dict[str, Any]:
        if not reference.startswith("#/"):
            raise AssertionError(f"only local references are supported: {reference}")
        node = self.root
        for part in reference.removeprefix("#/").split("/"):
            child = node.get(part)
            if not isinstance(child, dict):
                raise AssertionError(f"reference does not resolve to a schema: {reference}")
            node = child
        return node

    def _validate(self, value: Any, schema: dict[str, Any], where: str) -> list[str]:
        if "$ref" in schema:
            return self._validate(value, self._resolve(str(schema["$ref"])), where)

        if "type" in schema:
            declared = schema["type"]
            names = declared if isinstance(declared, list) else [declared]
            if not any(self._is_type(value, str(name)) for name in names):
                return [f"{where}: expected {declared}, found {type(value).__name__}"]

        errors: list[str] = []
        if "enum" in schema and value not in schema["enum"]:
            errors.append(f"{where}: {value!r} is not one of {schema['enum']}")
        if isinstance(value, str):
            errors.extend(self._check_string(value, schema, where))
        if isinstance(value, int | float) and not isinstance(value, bool):
            errors.extend(self._check_number(value, schema, where))
        if isinstance(value, dict):
            errors.extend(self._check_object(value, schema, where))
        if isinstance(value, list):
            errors.extend(self._check_array(value, schema, where))
        return errors

    def _check_number(self, value: float, schema: dict[str, Any], where: str) -> list[str]:
        if "minimum" in schema and value < schema["minimum"]:
            return [f"{where}: {value} is below {schema['minimum']}"]
        return []

    def _check_string(self, value: str, schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']} characters")
        if "pattern" in schema and re.search(str(schema["pattern"]), value) is None:
            errors.append(f"{where}: does not match {schema['pattern']}")
        return errors

    def _check_object(self, value: dict[str, Any], schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        properties: dict[str, Any] = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{where}: missing required key {key!r}")
        if schema.get("additionalProperties") is False:
            errors.extend(
                f"{where}: unexpected key {key!r}" for key in value if key not in properties
            )
        for key, child in properties.items():
            if key in value:
                errors.extend(self._validate(value[key], child, f"{where}.{key}"))
        return errors

    def _check_array(self, value: list[Any], schema: dict[str, Any], where: str) -> list[str]:
        errors: list[str] = []
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{where}: fewer than {schema['minItems']} items")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(self._validate(item, schema["items"], f"{where}[{index}]"))
        return errors

    def _is_type(self, value: Any, name: str) -> bool:
        if name == "object":
            return isinstance(value, dict)
        if name == "array":
            return isinstance(value, list)
        if name == "string":
            return isinstance(value, str)
        if name == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if name == "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        if name == "boolean":
            return isinstance(value, bool)
        if name == "null":
            return value is None
        raise AssertionError(f"schema uses a type this validator cannot honour: {name}")
