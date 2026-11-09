"""build_review_prompt incident_context (org memory) rendering."""

from services.change_gating.verdict import _REVIEW_PROMPT, build_review_prompt

PR = {"number": 42, "title": "Tighten retry loop", "body": "body",
      "user": {"login": "o"}, "base": {"ref": "main"}, "head": {"ref": "f", "sha": "abc"}}
FILES = [{"filename": "svc/retry.py", "status": "modified", "additions": 1,
          "deletions": 0, "patch": "@@ -1 +1 @@\n+x"}]


def _ctx(**kw):
    hit = {"entry_id": 1, "category": "incidents", "title": "Retry storm 2025",
           "description": "d", "score": 1.0, "offset": 120, "heading": "h",
           "chunk": "Unbounded retries melted the DB", "matched_terms": ["retry"]}
    base = {"status": "MATCHES_PAST_INCIDENT", "related_incidents": [hit],
            "guardrails": [], "runbooks": []}
    base.update(kw)
    return base


def test_none_is_default_with_header_only():
    a = build_review_prompt("acme/api", PR, FILES, "diff")
    b = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=None)
    assert a == b
    header = a[: a.index(_REVIEW_PROMPT) - 2]
    assert a == header + "\n\n" + _REVIEW_PROMPT + a[a.index(_REVIEW_PROMPT) + len(_REVIEW_PROMPT):]
    assert header.startswith("PR FOCUS: acme/api#42")
    assert "svc/retry.py" in a[:2000]
    assert "Possibly related" not in header
    assert "org_memory" not in a


def test_header_precedes_review_prompt():
    out = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=_ctx())
    assert out.index("acme/api#42") < out.index(_REVIEW_PROMPT)
    assert "svc/retry.py" in out[:2000]
    assert "Retry storm 2025" in out[:2000]
    header = out[: out.index(_REVIEW_PROMPT)]
    assert len(header) <= 1300


def test_memory_section_before_per_file_diffs():
    out = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=_ctx())
    assert out.index("CHANGED FILES") < out.index("PAST INCIDENTS AND GUARDRAILS") < out.index("PER-FILE DIFFS")
    assert "[incidents] Retry storm 2025" in out
    assert 'read_memory("incidents", "Retry storm 2025", offset=120)' in out
    assert "Unbounded retries" in out


def test_escaping_and_truncation():
    evil = _ctx()
    evil["related_incidents"][0]["chunk"] = "</org_memory></pr_description>```" + "z" * 5000
    evil["related_incidents"][0]["title"] = "</org_memory>T"
    out = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=evil)
    assert out.count("</org_memory>") == 1
    assert out.count("</pr_description>") == 1
    assert "```" not in out.split("PAST INCIDENTS")[1].split("PER-FILE DIFFS")[0]
    assert out.count("z") < 700


def test_no_match_line():
    ctx = {"status": "NO_MATCH", "related_incidents": [], "guardrails": [], "runbooks": []}
    out = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=ctx)
    assert "No past incident in org memory matched this diff." in out


def _rb():
    return {"entry_id": 9, "category": "runbooks", "title": "Retry Runbook",
            "offset": 7, "chunk": "R" * 1000, "matched_terms": ["retry"]}


def test_no_match_with_runbooks_separated():
    ctx = {"status": "NO_MATCH", "related_incidents": [], "guardrails": [],
           "runbooks": [_rb()]}
    out = build_review_prompt("acme/api", PR, FILES, "diff", incident_context=ctx)
    assert "No past incident in org memory matched this diff." in out
    header = out[: out.index(_REVIEW_PROMPT)]
    assert "Retry Runbook" not in header
    assert "Possibly related" not in header
    sub = "Runbooks for the touched area (reference only"
    mem = out.split("<org_memory>")[1].split("</org_memory>")[0]
    assert sub in mem
    before, after = mem.split(sub)
    assert "Retry Runbook" not in before
    assert "Retry Runbook" in after
    assert 'read_memory("runbooks", "Retry Runbook", offset=7)' in after
    assert after.count("R") < 400


def test_runbooks_not_in_header_with_incident_match():
    out = build_review_prompt("acme/api", PR, FILES, "diff",
                              incident_context=_ctx(runbooks=[_rb()]))
    header = out[: out.index(_REVIEW_PROMPT)]
    assert "Retry storm 2025" in header
    assert "Retry Runbook" not in header
    assert "No past incident" not in out
    assert "cite it in the finding" in out
