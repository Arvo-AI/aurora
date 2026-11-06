"""Evidence gathered early in an RCA must survive into the summary prompt even
when the agent ends the run with many artifact/memory writes.

Reproduces the v0 lab run 09-redis-pvc-pending: the PVC/StorageClass checks ran
early, then ~25 write_artifact retries followed. The summary prompt only lists
the most recent citations, so the writes pushed the PVC evidence out and the
report filed the PVC under "Not Checked". Messages are in the serialized
llm_context_history shape CitationExtractor parses.
"""

import json

from chat.background.citation_extractor import CitationExtractor
from chat.background.summarization import _build_summary_prompt_with_chat

PVC_OUTPUT = (
    "NAME         STATUS    VOLUME   CAPACITY   STORAGECLASS\n"
    "redis-data   Pending                       fast-ssd"
)


def _call(i, name, args, content):
    tc_id = f"tc-{i}"
    return [
        {"role": "ai", "tool_calls": [{"id": tc_id, "name": name, "args": args}]},
        {"role": "tool", "tool_call_id": tc_id, "name": name, "content": content},
    ]


def _kubectl(i, cmd, out):
    return _call(i, "on_prem_kubectl", {"command": cmd, "cluster_id": "c1"},
                 json.dumps({"success": True, "final_command": f"kubectl {cmd}", "chat_output": out}))


def _messages():
    msgs = []
    msgs += _kubectl(1, "get pods -n shop", "api-69c49c687f-bjcbd 0/1 CrashLoopBackOff")
    msgs += _kubectl(2, "get pvc,pv -n shop", PVC_OUTPUT)
    msgs += _kubectl(3, "get storageclass", "standard (default) rancher.io/local-path")
    msgs += _call(4, "read_artifact", {"title": "Incident Index"},
                  json.dumps({"status": "ok", "content": "- [INC 61ae] prior entry"}))
    msgs += _call(5, "append_to_memory", {"title": "shop", "content": "note"},
                  json.dumps({"status": "ok", "message": "Appended to learned/shop"}))
    for i in range(6, 36):
        msgs += _call(i, "write_artifact", {"title": "Incident Index"},
                      "Error invoking tool 'write_artifact' with kwargs {'title': 'Incident Index'} "
                      "with error:\n content: Field required\n Please fix the error and try again.")
    msgs += _call(36, "write_artifact", {"title": "Incident Index", "content": "x"},
                  json.dumps({"status": "ok", "message": "Artifact saved (version 19).", "version": 19}))
    return msgs


def test_write_receipts_are_not_citations():
    citations = CitationExtractor()._parse_tool_messages(_messages())
    names = [c.tool_name for c in citations]
    assert "write_artifact" not in names and "append_to_memory" not in names
    assert [c.index for c in citations] == list(range(1, len(citations) + 1))
    assert any(c.command == "get pvc,pv -n shop" and "fast-ssd" in c.output for c in citations)
    # Reads still count: the prior-incident index is real context.
    assert "read_artifact" in names


def test_early_pvc_evidence_reaches_summary_prompt():
    citations = CitationExtractor()._parse_tool_messages(_messages())
    prompt = _build_summary_prompt_with_chat(
        source_type="alertmanager", alert_title="KubePodCrashLooping", severity="high",
        service="shop", triggered_at="2026-10-02T15:24:29Z", citations=citations,
    )
    pvc = next(c for c in citations if c.command == "get pvc,pv -n shop")
    assert f"[{pvc.index}] {pvc.tool_name} - get pvc,pv -n shop" in prompt
    assert "redis-data   Pending" in prompt
    assert "Artifact saved" not in prompt
