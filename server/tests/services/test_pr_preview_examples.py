"""The pre-run examples on /pr-preview: valid data, free to open, paste flow intact.

``client/src/app/pr-preview/examples.json`` is written by
``scripts/generate_pr_preview_examples.py`` from real preview runs. The page
imports it statically, so opening an example sends no request: no session,
no LLM call, nothing counted against the daily limit. These tests pin that,
and that the examples sit beside the paste box rather than replacing it.
"""

import json
import os
import re
from datetime import datetime

import pytest

from services.change_gating.public_github import parse_pr_url
from services.change_gating.verdict import parse_verdict

_PAGE_DIR = os.path.join(
    os.path.dirname(__file__), *([os.pardir] * 3), "client", "src", "app", "pr-preview",
)

pytestmark = pytest.mark.skipif(
    not os.path.isdir(_PAGE_DIR), reason="client/ not present in this checkout"
)


def _read(name: str) -> str:
    with open(os.path.join(_PAGE_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _examples() -> list:
    return json.loads(_read("examples.json"))


def test_there_are_three_to_five_examples_including_a_control():
    examples = _examples()
    assert 3 <= len(examples) <= 5
    assert len({e["url"] for e in examples}) == len(examples)
    assert any(e["incident"] is None for e in examples)  # at least one SAFE-expected control


@pytest.mark.parametrize("index", range(5))
def test_each_example_has_provenance_and_a_real_verdict_shape(index):
    examples = _examples()
    if index >= len(examples):
        pytest.skip("fewer examples")
    example = examples[index]

    assert parse_pr_url(example["url"]) == (example["repo"], example["pr_number"])
    assert re.fullmatch(r"[0-9a-f]{40}", example["head_sha"])
    assert example["model"].strip()
    datetime.fromisoformat(example["generated_at"])
    assert example["title"].strip() and example["kind"].strip()
    assert isinstance(example["duration_seconds"], int)
    # An incident note always cites its source, and vice versa.
    assert (example["incident"] is None) == (example["incident_url"] is None)

    assert example["status"] in ("completed", "failed", "cancelled")
    verdict = example["verdict"]
    if verdict is not None:  # a run without a verdict stays in the file and says so on the page
        assert example["status"] == "completed"
        # Exactly what the preview API returns: re-parsing changes nothing.
        assert parse_verdict(json.dumps(verdict)) == verdict
        if verdict["verdict"] == "SAFE":
            assert verdict["findings"] == []


def test_opening_examples_sends_no_request():
    source = _read("ExampleReviews.tsx")
    assert "from './examples.json'" in source
    for call in ("fetch(", "useQuery", "/api/", "axios"):
        assert call not in source


def test_paste_box_stays_first_and_still_posts_to_the_preview_api():
    page = _read("page.tsx")
    assert "fetch('/api/pr-preview'" in page
    assert page.index("<form onSubmit={onSubmit}") < page.index("<ExampleReviews />")
