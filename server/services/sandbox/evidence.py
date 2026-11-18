"""Load only the allowlisted evidence bundles, never evaluation answers."""

from pathlib import Path

from .models import SampleCase

CASE_IDS = ("case-a", "case-b", "case-c")
_DATA = Path(__file__).with_name("data")


def load_case(case_id: str) -> SampleCase:
    if case_id not in CASE_IDS:
        raise ValueError("Unknown sample case")
    # Parse a fresh object per run: no shared mutable evidence or conversation.
    case = SampleCase.model_validate_json((_DATA / f"{case_id}.json").read_text())
    if case.id != case_id:
        raise ValueError("Sample case identity mismatch")
    return case


def list_cases() -> list[dict]:
    return [load_case(case_id).model_dump() for case_id in CASE_IDS]
