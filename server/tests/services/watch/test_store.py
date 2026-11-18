"""What Aurora Watch writes to jsonb columns."""

import json

from services.watch.store import jsonb_dumps


def test_jsonb_text_drops_nul_but_keeps_text_that_only_looks_like_one():
    # jsonb rejects \u0000. A NUL is dropped; the six characters "\u0000" written in a
    # log line (an escaped backslash in JSON) are text and stay.
    value = {"line": "name=widget\x00\x00", "literal": "\\u0000 is how JSON spells NUL", "both": "\\\x00"}
    text = jsonb_dumps(value)
    assert json.loads(text) == {"line": "name=widget", "literal": "\\u0000 is how JSON spells NUL", "both": "\\"}
