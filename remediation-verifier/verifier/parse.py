"""Narrowing helpers shared by the contract and observation loaders.

Both loaders read untrusted documents and must name the path of a bad value in
the error. Each helper takes the exception class to raise, so a contract error
stays a `ContractError` and a snapshot error stays an `ObservationError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ErrorType = type[ValueError]


def as_mapping(value: Any, where: str, error: ErrorType) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise error(f"{where}: expected a mapping")
    return value


def as_list(value: Any, where: str, error: ErrorType) -> list[Any]:
    if not isinstance(value, list):
        raise error(f"{where}: expected a list")
    return value


def as_text(value: Any, where: str, error: ErrorType) -> str:
    if not isinstance(value, str):
        raise error(f"{where}: expected a string")
    return value


def required(node: Mapping[str, Any], key: str, where: str, error: ErrorType) -> str:
    return as_text(node.get(key), f"{where}.{key}", error)


def optional_text(value: Any, where: str, error: ErrorType) -> str | None:
    return None if value is None else as_text(value, where, error)


def optional_int(value: Any, where: str, error: ErrorType) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise error(f"{where}: expected an integer")
    return value


def optional_number(value: Any, where: str, error: ErrorType) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise error(f"{where}: expected a number")
    return float(value)


def text_list(value: Any, where: str, error: ErrorType) -> list[str]:
    return [
        as_text(item, f"{where}[{index}]", error)
        for index, item in enumerate(as_list(value, where, error))
    ]


def int_list(value: Any, where: str, error: ErrorType) -> list[int]:
    items = as_list(value, where, error)
    return [optional_int(item, f"{where}[{index}]", error) or 0 for index, item in enumerate(items)]
