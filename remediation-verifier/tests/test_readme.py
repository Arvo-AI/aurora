"""The README makes promises the brief fixes in words, so they are a test.

The brief requires the scope sentence verbatim, an explicit advisory and
non-blocking statement, the limitations and the next step. An edit that drops
one of them still reads fine in review, which is why the check is here.

The escapes in `SCOPE` keep this file ASCII (W16) while comparing the
accented characters the brief requires.
"""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"
TEXT = README.read_text(encoding="utf-8")

SCOPE = (
    "Este verificador comprueba las condiciones declaradas en el contrato del incidente. "
    "No demuestra que no exista ning\u00fan otro defecto, y no reemplaza el juicio humano "
    "ni la revisi\u00f3n asesora de Aurora."
)

SCOPE_2 = (
    "El veredicto es advisory. Se adjunta como evidencia al hilo del incidente o al PR "
    "de remediaci\u00f3n, pero no bloquea el merge. Aurora es un revisor asesor por "
    "dise\u00f1o, y este verificador respeta esa filosof\u00eda."
)

LAB_COMMIT = "4edcd9dfbc48cbd9ca8ea031e429832cf72842d0"
FORK_COMMIT = "12c67cffd45c6b0b8c5da1d2e160a945e2936f49"


def test_the_scope_sentence_is_verbatim() -> None:
    assert f"> {SCOPE}" in TEXT


def test_the_advisory_paragraph_is_verbatim() -> None:
    assert f"> {SCOPE_2}" in TEXT


def test_the_readme_explains_why_the_work_matters() -> None:
    assert "## Why now" in TEXT
    assert "machine-checkable" in TEXT


def test_the_verdict_is_advisory_and_non_blocking() -> None:
    assert "advisory" in TEXT
    assert "never blocks a merge" in TEXT
    assert "no branch protection" in TEXT


def test_the_readme_says_what_it_does_not_do() -> None:
    assert "## What it does not do" in TEXT


def test_the_limitations_and_the_next_step_are_present() -> None:
    assert "## Limitations" in TEXT
    assert "## Next step" in TEXT


def test_the_readme_cites_the_pin_and_the_fork_base() -> None:
    assert LAB_COMMIT in TEXT
    assert FORK_COMMIT in TEXT


def test_the_readme_keeps_the_typography_rules() -> None:
    banned = re.compile("[\u2013\u2014\u2018\u2019\u201c\u201d\u2026]")
    assert banned.search(TEXT) is None


def test_the_readme_does_not_promise_a_gate_or_a_model() -> None:
    lowered = TEXT.lower()
    for word in ("holmesgpt", "cost_usd"):
        assert word not in lowered
