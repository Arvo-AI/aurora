"""Turn dbt artifacts, an Airflow failure payload or a raw log into evidence.

Every text field that came from the caller passes through Aurora's secret
scrubber (``utils.security.output_redaction.redact``) before it is stored,
shown to the LLM, or written anywhere. Fields are clipped *before* scrubbing so
the scrubber always sees the whole stored value (it only scans its first
512 KB).

No parser calls out to Airflow, dbt Cloud or a warehouse: the evidence is only
what the caller sent.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from services.pipeline_intake.evidence import (
    SOURCE_AIRFLOW,
    SOURCE_DBT,
    SOURCE_LOG,
    FailingNode,
    PipelineFailureEvidence,
)
from utils.security.output_redaction import redact

# ---------------------------------------------------------------------------
# Size limits. Request-level limit is enforced by the route before parsing;
# per-file limits here keep a single oversized artifact from slipping through
# a multipart body that is under the request cap.
# ---------------------------------------------------------------------------
MAX_REQUEST_BYTES = 60 * 1024 * 1024
MAX_RUN_RESULTS_BYTES = 10 * 1024 * 1024
MAX_MANIFEST_BYTES = 45 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_AIRFLOW_FIELD_BYTES = 2 * 1024 * 1024

# Stored/LLM-facing caps (characters).
MAX_ERROR_TEXT_CHARS = 8_000
MAX_SQL_CHARS = 20_000
MAX_LOG_EXCERPT_CHARS = 60_000
MAX_RECENT_CHANGES_CHARS = 30_000
MAX_ERROR_LINES = 40
MAX_LINE_CHARS = 400
MAX_FAILING_NODES = 25

FAILED_STATUSES = {"error", "fail", "runtime error"}
WARN_STATUSES = {"warn"}
SKIPPED_STATUSES = {"skipped"}

_GENERIC_DBT_TESTS = ("not_null", "unique", "accepted_values", "relationships")


class IntakeError(Exception):
    """Bad or oversized input. ``status`` is the HTTP code the route returns."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

# Two credential shapes that show up in pipeline logs and are not reliably
# covered by the Gitleaks corpus: URL userinfo (``postgres://u:pw@host``) and
# bare bearer tokens in echoed HTTP headers.
_URL_USERINFO = re.compile(r"(?P<scheme>\b[a-zA-Z][a-zA-Z0-9+.\-]*://)(?P<user>[^\s:/@]+):(?P<pw>[^\s/@]+)@")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/]{12,}=*")


class Scrubber:
    """Clips then redacts caller text, counting what was removed."""

    def __init__(self) -> None:
        self.count = 0
        self.truncated: List[str] = []

    def __call__(self, text: Any, limit: int, field: str, keep: str = "head") -> Optional[str]:
        if text is None:
            return None
        if not isinstance(text, str):
            text = str(text)
        if not text:
            return text
        text = _clip(text, limit, keep)
        if len(text) > limit:
            self.truncated.append(field)
        redacted, findings = redact(text)
        self.count += len(findings)
        redacted, n_url = _URL_USERINFO.subn(r"\g<scheme>\g<user>:[REDACTED:url-password]@", redacted)
        redacted, n_bearer = _BEARER.subn(r"\1 [REDACTED:bearer-token]", redacted)
        self.count += n_url + n_bearer
        return redacted


def _clip(text: str, limit: int, keep: str) -> str:
    """Clip to ``limit`` chars. ``tail`` keeps mostly the end (where logs put errors)."""
    if len(text) <= limit:
        return text
    dropped = len(text) - limit
    marker = f"\n... [{dropped} characters truncated] ...\n"
    if keep == "tail":
        head_n = limit // 10
        return text[:head_n] + marker + text[-(limit - head_n):]
    return text[:limit] + marker


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_ERROR_LINE = re.compile(
    r"(?i)(\berror\b|exception|traceback|\bfail(ed|ure)?\b|\bfatal\b|\bkilled\b|oom|denied|"
    r"timed?\s?out|refused|too many requests|rate.?limit|quota|unauthori[sz]ed|forbidden|"
    r"does not exist|not found|no such|mismatch|could not|cannot|unable to|"
    r"\b(4\d\d|5\d\d) (client|server) error|got \d+ results?)"
)
# Lines that look like errors but are summary counters ("ERROR=0", "0 errors").
_NOISE_LINE = re.compile(r"(?i)(\berror=0\b|\b0 errors?\b|\bfail(ed)?=0\b|on_failure_callback)")


def extract_error_lines(text: Optional[str], limit: int = MAX_ERROR_LINES) -> List[str]:
    """Return the error-looking lines of ``text``, newest last, de-duplicated."""
    if not text:
        return []
    seen = set()
    picked: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or not _ERROR_LINE.search(line) or _NOISE_LINE.search(line):
            continue
        line = line[:MAX_LINE_CHARS]
        if line in seen:
            continue
        seen.add(line)
        picked.append(line)
    # Errors cluster at the end of a log; keep the last ``limit``.
    return picked[-limit:]


def load_json_bytes(raw: Optional[bytes], name: str, max_bytes: int, required: bool) -> Optional[Dict[str, Any]]:
    """Size-check then decode one uploaded JSON artifact."""
    if raw is None or len(raw) == 0:
        if required:
            raise IntakeError(f"{name} is required")
        return None
    if len(raw) > max_bytes:
        raise IntakeError(f"{name} is larger than the {max_bytes // (1024 * 1024)} MB limit", status=413)
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise IntakeError(f"{name} is not valid JSON")
    if not isinstance(data, dict):
        raise IntakeError(f"{name} must be a JSON object")
    return data


def _node_name(unique_id: str) -> str:
    parts = unique_id.split(".")
    if parts[0] == "test" and len(parts) >= 4:
        return parts[2]
    return parts[-1] if parts else unique_id


def _resource_type(unique_id: str) -> str:
    return unique_id.split(".", 1)[0] if "." in unique_id else "unknown"


def _infer_generic_test(name: str) -> Optional[str]:
    """Name-based guess for dbt's built-in generic tests (used when there is no manifest)."""
    for t in _GENERIC_DBT_TESTS:
        if name == t or name.startswith(t + "_"):
            return t
    return None


# ---------------------------------------------------------------------------
# dbt
# ---------------------------------------------------------------------------

def parse_dbt(
    run_results: Dict[str, Any],
    manifest: Optional[Dict[str, Any]] = None,
    previous_run_results: Optional[Dict[str, Any]] = None,
    recent_changes: Optional[str] = None,
    scrub: Optional[Scrubber] = None,
) -> PipelineFailureEvidence:
    """Build evidence from ``run_results.json`` (+ optional ``manifest.json``)."""
    scrub = scrub or Scrubber()
    results = run_results.get("results")
    if not isinstance(results, list):
        raise IntakeError("run_results.json has no 'results' list; is this a dbt run_results.json?")

    nodes = (manifest or {}).get("nodes") or {}
    meta = run_results.get("metadata") or {}
    man_meta = (manifest or {}).get("metadata") or {}

    statuses: Dict[str, str] = {}
    failed, warned, skipped = [], [], []
    for r in results:
        if not isinstance(r, dict) or not r.get("unique_id"):
            continue
        uid = str(r["unique_id"])
        status = str(r.get("status") or "").lower()
        statuses[uid] = status
        if status in FAILED_STATUSES:
            failed.append(r)
        elif status in WARN_STATUSES:
            warned.append(r)
        elif status in SKIPPED_STATUSES:
            skipped.append(uid)

    # A run that only warned is still what the user wants explained.
    chosen = failed or warned
    if not chosen:
        raise IntakeError("run_results.json contains no failed, errored or warning nodes")

    failing_nodes: List[FailingNode] = []
    for r in chosen[:MAX_FAILING_NODES]:
        uid = str(r["unique_id"])
        mnode = nodes.get(uid) or {}
        name = mnode.get("name") or _node_name(uid)
        rtype = mnode.get("resource_type") or _resource_type(uid)
        test_meta = mnode.get("test_metadata") or {}
        test_name = test_meta.get("name") or (_infer_generic_test(name) if rtype == "test" else None)
        test_kwargs = test_meta.get("kwargs") or {}
        attached = mnode.get("attached_node")
        if rtype == "test" and not attached:
            deps = [d for d in (mnode.get("depends_on") or {}).get("nodes") or [] if not d.startswith("test.")]
            attached = deps[0] if deps else None
        compiled = r.get("compiled_code") or mnode.get("compiled_code")
        failing_nodes.append(
            FailingNode(
                unique_id=uid,
                name=name,
                resource_type=rtype,
                status=str(r.get("status") or "").lower(),
                error_text=scrub(r.get("message") or "", MAX_ERROR_TEXT_CHARS, f"{uid}.message") or "",
                compiled_sql=scrub(compiled, MAX_SQL_CHARS, f"{uid}.compiled_sql"),
                file_path=mnode.get("original_file_path"),
                execution_time=r.get("execution_time"),
                failures=r.get("failures"),
                test_name=test_name,
                test_column=mnode.get("column_name") or test_kwargs.get("column_name"),
                attached_node=attached,
            )
        )

    project = man_meta.get("project_name") or _project_from_ids([n.unique_id for n in failing_nodes])
    ev = PipelineFailureEvidence(
        source_kind=SOURCE_DBT,
        title="",
        pipeline_name=project or "dbt project",
        run_id=meta.get("invocation_id"),
        failing_nodes=failing_nodes,
        skipped_nodes=skipped[:200],
        recent_changes=scrub(recent_changes, MAX_RECENT_CHANGES_CHARS, "recent_changes"),
        timings={
            "generated_at": meta.get("generated_at"),
            "elapsed_time": run_results.get("elapsed_time"),
            "nodes": {n.unique_id: n.execution_time for n in failing_nodes},
        },
        context={
            "dbt_version": meta.get("dbt_version"),
            "adapter_type": man_meta.get("adapter_type"),
            "command": _dbt_command(run_results),
            "results_total": len(results),
            "failed_total": len(failed),
            "warned_total": len(warned),
            "skipped_total": len(skipped),
            "manifest_supplied": manifest is not None,
        },
    )
    ev.error_lines = _node_error_lines(failing_nodes)
    ev.previous_run = _previous_run_status(previous_run_results, ev.node_keys())
    ev.title = _dbt_title(ev)
    ev.redactions = scrub.count
    ev.truncated_fields = list(scrub.truncated)
    ev.context["run_statuses"] = statuses  # used by lineage; stripped before storage
    return ev


def _project_from_ids(ids: Iterable[str]) -> Optional[str]:
    for uid in ids:
        parts = uid.split(".")
        if len(parts) >= 3:
            return parts[1]
    return None


def _dbt_command(run_results: Dict[str, Any]) -> Optional[str]:
    args = run_results.get("args") or {}
    which = args.get("which")
    return f"dbt {which}" if which else None


def _node_error_lines(nodes: List[FailingNode]) -> List[str]:
    lines: List[str] = []
    for n in nodes:
        for line in (n.error_text or "").splitlines():
            line = line.strip()
            if line:
                lines.append(f"[{n.name}] {line[:MAX_LINE_CHARS]}")
    return lines[:MAX_ERROR_LINES]


def _previous_run_status(previous: Optional[Dict[str, Any]], node_ids: List[str]) -> Dict[str, Any]:
    if not previous:
        return {}
    prev = {
        str(r.get("unique_id")): str(r.get("status") or "").lower()
        for r in previous.get("results") or []
        if isinstance(r, dict) and r.get("unique_id")
    }
    return {
        "source": "previous_run_results",
        "generated_at": (previous.get("metadata") or {}).get("generated_at"),
        "nodes": {uid: prev.get(uid, "not in previous run") for uid in node_ids},
    }


def _dbt_title(ev: PipelineFailureEvidence) -> str:
    primary = ev.primary_node()
    if not primary:
        return f"dbt run failed in {ev.pipeline_name}"
    more = len(ev.failing_nodes) - 1
    suffix = f" (+{more} more)" if more > 0 else ""
    summary = _first_line(primary.error_text)
    return f"dbt {primary.resource_type} {primary.name} failed{suffix}: {summary}"[:250]


# ---------------------------------------------------------------------------
# Airflow
# ---------------------------------------------------------------------------

_AIRFLOW_TEXT_FIELDS = ("exception", "exception_type", "log_excerpt")


def parse_airflow(payload: Dict[str, Any], scrub: Optional[Scrubber] = None) -> PipelineFailureEvidence:
    """Build evidence from the ``on_failure_callback`` payload Aurora documents.

    Required: ``dag_id`` and ``task_id``. If the failing task ran dbt and the
    callback attached ``dbt_artifacts`` (run_results + manifest from the
    worker's ``target/``), the dbt nodes become the failing nodes and the
    Airflow fields become context.
    """
    scrub = scrub or Scrubber()
    if not isinstance(payload, dict):
        raise IntakeError("Airflow payload must be a JSON object")
    dag_id = str(payload.get("dag_id") or "").strip()
    task_id = str(payload.get("task_id") or "").strip()
    if not dag_id or not task_id:
        raise IntakeError("Airflow payload needs 'dag_id' and 'task_id'")
    for f in _AIRFLOW_TEXT_FIELDS:
        v = payload.get(f)
        if isinstance(v, str) and len(v.encode("utf-8", "ignore")) > MAX_AIRFLOW_FIELD_BYTES * 4:
            raise IntakeError(f"'{f}' is too large", status=413)

    raw_exc = payload.get("exception") or ""
    raw_log = payload.get("log_excerpt")
    exception = scrub(_ANSI.sub("", str(raw_exc)), MAX_ERROR_TEXT_CHARS, "exception", keep="tail") or ""
    log_excerpt = scrub(_ANSI.sub("", str(raw_log)) if raw_log else None, MAX_LOG_EXCERPT_CHARS,
                        "log_excerpt", keep="tail")
    recent_changes = payload.get("recent_changes")

    artifacts = payload.get("dbt_artifacts")
    if isinstance(artifacts, dict) and isinstance(artifacts.get("run_results"), dict):
        try:
            ev = parse_dbt(
                artifacts["run_results"],
                artifacts.get("manifest") if isinstance(artifacts.get("manifest"), dict) else None,
                recent_changes=recent_changes,
                scrub=scrub,
            )
        except IntakeError:
            ev = None  # artifacts present but nothing failed in them; fall back to the task
    else:
        ev = None

    task_uid = f"airflow.{dag_id}.{task_id}"
    task_node = FailingNode(
        unique_id=task_uid,
        name=task_id,
        resource_type="task",
        status="failed",
        error_text=exception,
        execution_time=_as_float(payload.get("duration")),
    )

    airflow_ctx = {
        "dag_id": dag_id,
        "task_id": task_id,
        "try_number": payload.get("try_number"),
        "max_tries": payload.get("max_tries"),
        "operator": payload.get("operator"),
        "exception_type": scrub(payload.get("exception_type"), 200, "exception_type"),
        "logical_date": payload.get("logical_date"),
        "hostname": scrub(payload.get("hostname"), 200, "hostname"),
        "upstream_task_ids": _str_list(payload.get("upstream_task_ids")),
        "downstream_task_ids": _str_list(payload.get("downstream_task_ids")),
        "airflow_version": payload.get("airflow_version"),
    }
    timings = {
        "start_date": payload.get("start_date"),
        "end_date": payload.get("end_date"),
        "duration": _as_float(payload.get("duration")),
    }

    if ev is None:
        ev = PipelineFailureEvidence(
            source_kind=SOURCE_AIRFLOW,
            title="",
            pipeline_name=dag_id,
            failing_nodes=[task_node],
            recent_changes=scrub(recent_changes, MAX_RECENT_CHANGES_CHARS, "recent_changes"),
        )
    else:
        ev.source_kind = SOURCE_AIRFLOW
        ev.context["dbt_project"] = ev.pipeline_name
        ev.pipeline_name = dag_id
        ev.context["airflow_task_error"] = exception

    ev.run_id = str(payload.get("run_id")) if payload.get("run_id") else ev.run_id
    ev.log_excerpt = log_excerpt
    ev.timings = {**ev.timings, **{k: v for k, v in timings.items() if v is not None}}
    ev.context.update({k: v for k, v in airflow_ctx.items() if v not in (None, [], "")})
    ev.error_lines = (ev.error_lines + extract_error_lines(exception) + extract_error_lines(log_excerpt))[-MAX_ERROR_LINES:]
    prev_state = payload.get("previous_state")
    if prev_state and not ev.previous_run:
        ev.previous_run = {"source": "airflow_previous_task_instance", "state": str(prev_state)[:50]}
    ev.lineage = {
        "upstream_tasks": airflow_ctx["upstream_task_ids"],
        "downstream_tasks": airflow_ctx["downstream_task_ids"],
    }
    short = _first_line(exception) or "task failed"
    ev.title = f"Airflow {dag_id}.{task_id} failed: {short}"[:250]
    ev.redactions = scrub.count
    ev.truncated_fields = list(scrub.truncated)
    return ev


def _as_float(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _str_list(v: Any) -> List[str]:
    if not isinstance(v, (list, tuple)):
        return []
    return [str(x)[:250] for x in v][:100]


_UNINFORMATIVE = re.compile(
    r"^(Traceback \(most recent call last\):|File \"|\[(ERROR|WARN|FAIL)\]:\s+in\s|"
    r"(Runtime|Database|Compilation) Error in |compiled code at |raise |\^+$)",
    re.IGNORECASE,
)


def _first_line(text: Optional[str]) -> str:
    """The most informative line: the last error-looking line that is not a header or frame."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    useful = [ln for ln in lines if not _UNINFORMATIVE.search(ln)]
    errorish = [ln for ln in useful if _ERROR_LINE.search(ln)]
    pick = (errorish or useful or lines or [""])[-1]
    return pick[:160]


# ---------------------------------------------------------------------------
# Raw log (CI output, dbt CLI output, Airflow task log)
# ---------------------------------------------------------------------------

# dbt <= 1.9: "Database Error in model customers (models/customers.sql)"
#             "Failure in test not_null_orders_amount (models/schema.yml)"
# dbt >= 1.10: "[ERROR]: in test not_null_orders_customer_id (models/schema.yml)"
_DBT_LOG_NODE = re.compile(
    r"^\s*(?:\[(?P<bracket>ERROR|WARN|FAIL)\]:\s+in|"
    r"(?P<kind>Database Error|Runtime Error|Compilation Error|Failure|Error)\s+in)\s+"
    r"(?P<type>model|test|seed|snapshot|unit_test|sql operation)\s+(?P<name>[\w\-.]+)\s*(?:\((?P<path>[^)]+)\))?",
    re.IGNORECASE,
)
_DBT_TEST_ROWS = re.compile(r"Got (\d+) results?, configured to (fail|warn)", re.IGNORECASE)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_LOG_TIMESTAMP = re.compile(r"^(\d\d:\d\d:\d\d(\.\d+)?|\d{4}-\d\d-\d\d[T ][\d:.,]+Z?)\s+")


def _clean_log(text: str) -> str:
    """Drop terminal colour codes and dbt's leading HH:MM:SS so lines can be matched."""
    text = _ANSI.sub("", text)
    return "\n".join(_LOG_TIMESTAMP.sub("", line) for line in text.splitlines())
_DBT_LOG_RESULT = re.compile(
    r"\d+\s+of\s+\d+\s+(?P<status>ERROR|FAIL|WARN)\s+.*?\b(?P<type>model|test|seed|snapshot)\s+(?P<name>[\w\-.]+)",
    re.IGNORECASE,
)
_AIRFLOW_LOG_TASK = re.compile(r"dag_id=(?P<dag>[\w\-.]+),\s*task_id=(?P<task>[\w\-.]+)(?:,\s*run_id=(?P<run>[^,\s]+))?")


def parse_log(
    text: str,
    pipeline_name: Optional[str] = None,
    recent_changes: Optional[str] = None,
    scrub: Optional[Scrubber] = None,
) -> PipelineFailureEvidence:
    """Build evidence from pasted log text (dbt CLI, Airflow task log, any CI log)."""
    scrub = scrub or Scrubber()
    if not isinstance(text, str) or not text.strip():
        raise IntakeError("log_text is empty")
    if len(text.encode("utf-8", "ignore")) > MAX_LOG_BYTES:
        raise IntakeError(f"log_text is larger than the {MAX_LOG_BYTES // (1024 * 1024)} MB limit", status=413)

    body = scrub(_clean_log(text), MAX_LOG_EXCERPT_CHARS, "log_text", keep="tail") or ""
    lines = body.splitlines()

    failing: List[FailingNode] = []
    seen: set = set()
    detected = "generic"

    for idx, line in enumerate(lines):
        m = _DBT_LOG_NODE.search(line)
        if not m:
            continue
        detected = "dbt"
        rtype = m.group("type").lower().replace(" ", "_")
        name = m.group("name")
        uid = f"{rtype}.{name}"
        if uid in seen:
            continue
        seen.add(uid)
        block = _block_after(lines, idx, name)
        rows = _DBT_TEST_ROWS.search(block)
        if rtype == "test" and (rows or (m.group("kind") or "").lower() == "failure"):
            status = "warn" if (m.group("bracket") or "").upper() == "WARN" else "fail"
        else:
            status = "error"
        failing.append(
            FailingNode(
                unique_id=uid,
                name=name,
                resource_type=rtype,
                status=status,
                error_text=block,
                file_path=m.group("path"),
                failures=int(rows.group(1)) if rows else None,
                test_name=_infer_generic_test(name) if rtype == "test" else None,
            )
        )

    if not failing:
        for line in lines:
            m = _DBT_LOG_RESULT.search(line)
            if m:
                detected = "dbt"
                rtype = m.group("type").lower()
                name = m.group("name").split(".")[-1]
                uid = f"{rtype}.{name}"
                if uid not in seen:
                    seen.add(uid)
                    status = "fail" if m.group("status").upper() == "FAIL" else m.group("status").lower()
                    failing.append(FailingNode(unique_id=uid, name=name, resource_type=rtype, status=status,
                                               error_text=line.strip()[:MAX_LINE_CHARS],
                                               test_name=_infer_generic_test(name) if rtype == "test" else None))

    airflow_task = None
    for line in reversed(lines):
        m = _AIRFLOW_LOG_TASK.search(line)
        if m:
            airflow_task = m
            break

    error_lines = extract_error_lines(body)
    if not failing:
        if airflow_task:
            detected = "airflow"
            failing.append(
                FailingNode(
                    unique_id=f"airflow.{airflow_task.group('dag')}.{airflow_task.group('task')}",
                    name=airflow_task.group("task"),
                    resource_type="task",
                    status="failed",
                    error_text="\n".join(error_lines[-15:]),
                )
            )
        else:
            failing.append(
                FailingNode(
                    unique_id="log.step",
                    name=pipeline_name or "pipeline step",
                    resource_type="step",
                    status="failed",
                    error_text="\n".join(error_lines[-15:]),
                )
            )

    name = (pipeline_name or "").strip()[:200] or (
        airflow_task.group("dag") if airflow_task else ("dbt project" if detected == "dbt" else "pasted log")
    )
    ev = PipelineFailureEvidence(
        source_kind=SOURCE_LOG,
        title="",
        pipeline_name=name,
        run_id=airflow_task.group("run") if airflow_task else None,
        failing_nodes=failing[:MAX_FAILING_NODES],
        error_lines=error_lines,
        log_excerpt=body,
        recent_changes=scrub(recent_changes, MAX_RECENT_CHANGES_CHARS, "recent_changes"),
        context={"detected_format": detected, "log_lines": len(lines)},
    )
    primary = ev.primary_node()
    first = _first_line(primary.error_text if primary else "") or (error_lines[-1] if error_lines else "failed")
    label = f"{primary.resource_type} {primary.name}" if primary and primary.resource_type != "step" else name
    ev.title = f"Pipeline {label} failed: {first}"[:250]
    ev.redactions = scrub.count
    ev.truncated_fields = list(scrub.truncated)
    return ev


def _block_after(lines: List[str], idx: int, name: str, max_lines: int = 25) -> str:
    """The error block dbt prints under an 'Error in model X' header."""
    out: List[str] = []
    for line in lines[idx: idx + max_lines]:
        if out and not line.strip():
            break
        header = _DBT_LOG_NODE.search(line) if out else None
        if header and header.group("name") != name:
            break
        out.append(line.rstrip())
    return "\n".join(out)[:MAX_ERROR_TEXT_CHARS]


def parse_airflow_json_text(raw: str) -> Dict[str, Any]:
    """Decode an Airflow payload pasted into the UI as text."""
    if len(raw.encode("utf-8", "ignore")) > MAX_REQUEST_BYTES:
        raise IntakeError("Airflow payload is too large", status=413)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise IntakeError("Airflow payload is not valid JSON")
    if not isinstance(data, dict):
        raise IntakeError("Airflow payload must be a JSON object")
    return data


def split_statuses(ev: PipelineFailureEvidence) -> Tuple[PipelineFailureEvidence, Dict[str, str]]:
    """Pop the per-node run statuses (used for lineage only, too big to store)."""
    statuses = ev.context.pop("run_statuses", {}) or {}
    return ev, statuses
