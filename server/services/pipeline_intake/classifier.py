"""Deterministic pre-classifier for pipeline failures. No LLM, no network.

Each failing node gets one category plus the exact evidence lines and rule id
that triggered it. The RCA agent receives this as a starting hypothesis to
confirm or overturn, and the user sees it within the submit response.

Categories:
    execution           task/model crashed, bad SQL/cast, permissions, missed schedule
    ingestion_upstream  an upstream API or source: HTTP 429/rate limit, quota,
                        expired auth, 403, network timeouts
    platform            OOM kill, disk full, warehouse unavailable/locked
    schema_drift        missing column/relation, type mismatch between models
    data_test           a dbt data test found rows (may be expected variation)
    unknown             nothing matched

Precedence when several categories match one node: a failing data test is a
structural fact and wins; after that the more specific *cause* beats the
generic *symptom* (an OOM kill or a 429 also prints a Python traceback, a
schema change surfaces as a dbt "Runtime Error").
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence

from services.pipeline_intake.evidence import FailingNode, PipelineFailureEvidence

EXECUTION = "execution"
INGESTION_UPSTREAM = "ingestion_upstream"
PLATFORM = "platform"
SCHEMA_DRIFT = "schema_drift"
DATA_TEST = "data_test"
UNKNOWN = "unknown"
CATEGORIES = (EXECUTION, INGESTION_UPSTREAM, PLATFORM, SCHEMA_DRIFT, DATA_TEST, UNKNOWN)

DATA_TEST_NOTE = (
    "A data test failing means rows did not meet an expectation. That can be expected "
    "variation in the source data (late-arriving rows, a new valid value, a real duplicate "
    "upstream) rather than a bug in the pipeline. Look at the failing rows before changing code."
)

_HIGH, _MEDIUM, _LOW = "high", "medium", "low"
_CONF_RANK = {_HIGH: 3, _MEDIUM: 2, _LOW: 1}


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    confidence: str
    pattern: "re.Pattern[str]"


def _r(rule_id: str, category: str, confidence: str, regex: str) -> Rule:
    return Rule(rule_id, category, confidence, re.compile(regex, re.IGNORECASE))


# Order inside a category does not matter; ordering across categories is
# decided by _PRECEDENCE below.
RULES: Sequence[Rule] = (
    # --- platform ---------------------------------------------------------
    _r("platform.oom", PLATFORM, _HIGH,
       r"OOMKilled|\bout of memory\b|Out of Memory Error|\bMemoryError\b|Cannot allocate memory|"
       r"exit code 137|return code -9\b|Negsignal\.SIGKILL|\bSIGKILL\b|Killed process|memory limit (exceeded|reached)"),
    _r("platform.disk_full", PLATFORM, _HIGH,
       r"No space left on device|\bdisk (is )?full\b|Disk quota exceeded|could not extend file"),
    _r("platform.warehouse_unavailable", PLATFORM, _HIGH,
       r"warehouse\b.{0,60}\b(suspended|unavailable|not available|cannot be resumed|does not exist or not authorized)|"
       r"the database system is (starting up|shutting down|in recovery mode)|"
       r"could not connect to server|server closed the connection unexpectedly|"
       r"Could not set lock on file|database is locked|statement timeout|canceling statement due to statement timeout"),
    _r("platform.worker_lost", PLATFORM, _MEDIUM,
       r"zombie (job|task)|Detected zombie|worker (lost|died)|Executor reports task instance .* finished \(failed\) although the task says"),

    # --- ingestion_upstream ---------------------------------------------
    _r("ingestion.rate_limit", INGESTION_UPSTREAM, _HIGH,
       r"\b429 Client Error\b|HTTP(/\d(\.\d)?)?\s*429\b|status(_code)?\W{0,4}429\b|\b429 Too Many Requests\b|"
       r"Too Many Requests|rate[ _-]?limit(ed|s)?\b|RateLimitError|\bthrottl(ed|ing)\b"),
    _r("ingestion.quota", INGESTION_UPSTREAM, _HIGH,
       r"quota (exceeded|exhausted)|exceeded (your|the)? ?.{0,30}quota|Resource has been exhausted|usage limit (reached|exceeded)"),
    _r("ingestion.auth_expired", INGESTION_UPSTREAM, _HIGH,
       r"\b401 Client Error\b|HTTP(/\d(\.\d)?)?\s*401\b|status(_code)?\W{0,4}401\b|401 Unauthorized|"
       r"token (has |is )?expired|expired (access |bearer |auth )?token|invalid_grant|invalid_token|"
       r"(access|refresh) token .{0,20}(expired|revoked|invalid)"),
    _r("ingestion.forbidden", INGESTION_UPSTREAM, _HIGH,
       r"\b403 Client Error\b|HTTP(/\d(\.\d)?)?\s*403\b|status(_code)?\W{0,4}403\b|403 Forbidden"),
    _r("ingestion.upstream_5xx", INGESTION_UPSTREAM, _MEDIUM,
       r"\b50[234] Server Error\b|HTTP(/\d(\.\d)?)?\s*50[234]\b|status(_code)?\W{0,4}50[234]\b|"
       r"Service Unavailable|Bad Gateway|Gateway Time-?out"),
    _r("ingestion.timeout", INGESTION_UPSTREAM, _MEDIUM,
       r"\b(Read|Connect)Timeout(Error)?\b|Read timed out|Connection timed out|\btimed out\b|"
       r"\bTimeoutError\b|Max retries exceeded|ConnectionResetError|Connection reset by peer|RemoteDisconnected"),

    # --- schema_drift -----------------------------------------------------
    _r("schema.missing_column", SCHEMA_DRIFT, _HIGH,
       r"column \"?[\w.]+\"? does not exist|Referenced column \"?[\w.]+\"? not found|"
       r"Unrecognized name: \w+|invalid identifier '?[\w.\"]+'?|no such column: [\w.]+|"
       r"column [\w.\"]+ (is )?not found|Column '?[\w.]+'? cannot be resolved|Values of type .* cannot be referenced|"
       r"Binder Error: .*column"),
    _r("schema.missing_relation", SCHEMA_DRIFT, _HIGH,
       r"relation \"?[\w.\"]+\"? does not exist|Table with name [\w.\"]+ does not exist|"
       r"Catalog Error: (Table|View|Schema)|Not found: Table|Object '?[\w.\"]+'? does not exist|"
       r"no such table: [\w.]+|depends on a node named '[\w.]+' which (was not found|is disabled)"),
    _r("schema.type_mismatch", SCHEMA_DRIFT, _HIGH,
       r"Cannot compare values of type|No function matches the given name and argument types|"
       r"operator does not exist: \w+|is of type \w+ but expression is of type|"
       r"Could not choose a best candidate function|\btype mismatch\b|Mismatch Type Error|"
       r"cannot be cast automatically to type|UNION types? .* cannot be matched|Set operation can only be applied to expressions with the same number"),

    # --- execution --------------------------------------------------------
    _r("execution.bad_cast", EXECUTION, _HIGH,
       r"Conversion Error|Could not convert (string|value)|invalid input syntax for (type )?\w+|"
       r"Numeric value '.*' is not recognized|Bad (int64|double|numeric|date|timestamp) value|"
       r"Invalid (date|timestamp|number|numeric) (value|format)|Error converting|out of range for type|"
       r"Could not parse string"),
    _r("execution.permissions", EXECUTION, _HIGH,
       r"permission denied|insufficient privileges|not authorized to|Access Denied|access denied for user|"
       r"must be owner of|User does not have permission"),
    _r("execution.sql_error", EXECUTION, _MEDIUM,
       r"syntax error at or near|Parser Error|SQL compilation error|Syntax error:|Compilation Error|"
       r"Parsing Error|division by zero|Division by zero|Constraint Error|violates (not-null|unique|check|foreign key) constraint|"
       r"duplicate key value|Ambiguous column|ambiguous column name|is ambiguous"),
    _r("execution.missed_schedule", EXECUTION, _MEDIUM,
       r"SLA miss|missed (its |the )?(SLA|schedule)|dagrun_timeout|AirflowTaskTimeout|Task exceeded (its )?execution_timeout"),
    _r("execution.task_crash", EXECUTION, _LOW,
       r"Traceback \(most recent call last\)|\b(Key|Value|Type|Attribute|Index|Name|Runtime|Assertion|ZeroDivision|"
       r"Import|ModuleNotFound|FileNotFound|OS|Lookup)Error\b|Runtime Error|returned non-zero exit status|"
       r"exited with (return )?code [1-9]|non-zero exit code|Bash command failed|AirflowException|Task failed with exception"),
)

# Highest first. data_test is decided structurally before rules run.
_PRECEDENCE = (PLATFORM, INGESTION_UPSTREAM, SCHEMA_DRIFT, EXECUTION)

# Capture the missing identifier for the lineage walk.
_IDENTIFIER_PATTERNS = (
    re.compile(r"column \"?([\w.]+)\"? does not exist", re.IGNORECASE),
    re.compile(r"Referenced column \"?([\w.]+)\"? not found", re.IGNORECASE),
    re.compile(r"Unrecognized name: (\w+)", re.IGNORECASE),
    re.compile(r"invalid identifier '?\"?([\w.]+)", re.IGNORECASE),
    re.compile(r"no such column: ([\w.]+)", re.IGNORECASE),
    re.compile(r"relation \"?([\w.\"]+?)\"? does not exist", re.IGNORECASE),
    re.compile(r"Table with name ([\w.\"]+) does not exist", re.IGNORECASE),
    re.compile(r"depends on a node named '([\w.]+)'", re.IGNORECASE),
)


@dataclass
class NodeClassification:
    unique_id: str
    category: str
    confidence: str
    rule_id: str
    evidence_lines: List[str] = field(default_factory=list)
    identifiers: List[str] = field(default_factory=list)


@dataclass
class Classification:
    category: str
    confidence: str
    rule_id: str
    primary_node: Optional[str]
    evidence_lines: List[str]
    note: Optional[str]
    identifiers: List[str]
    nodes: List[NodeClassification]
    classifier_version: str = "1"

    def to_dict(self) -> Dict:
        return asdict(self)


def _node_text(node: FailingNode) -> List[str]:
    return [line.strip() for line in (node.error_text or "").splitlines() if line.strip()]


def classify_lines(lines: Sequence[str]) -> NodeClassification:
    """Classify raw lines. Exposed for log-only inputs and tests."""
    hits: Dict[str, List[tuple]] = {}
    for line in lines:
        for rule in RULES:
            if rule.pattern.search(line):
                hits.setdefault(rule.category, []).append((rule, line))
    if not hits:
        return NodeClassification(unique_id="", category=UNKNOWN, confidence=_LOW, rule_id="none")

    # Strongest rule wins; precedence breaks ties between equally strong rules.
    best_per_category = {
        cat: max(matched, key=lambda rl: _CONF_RANK[rl[0].confidence])[0] for cat, matched in hits.items()
    }
    category = min(
        best_per_category,
        key=lambda cat: (-_CONF_RANK[best_per_category[cat].confidence], _PRECEDENCE.index(cat)),
    )
    best = best_per_category[category]
    matched = hits[category]
    evidence = _dedupe([ln for r, ln in matched if r.rule_id == best.rule_id] + [ln for _, ln in matched])
    return NodeClassification(
        unique_id="",
        category=category,
        confidence=best.confidence,
        rule_id=best.rule_id,
        evidence_lines=evidence[:6],
        identifiers=_identifiers(lines) if category == SCHEMA_DRIFT else [],
    )


def classify_node(node: FailingNode, extra_lines: Sequence[str] = ()) -> NodeClassification:
    if node.resource_type == "test" and node.status in ("fail", "warn"):
        line = (node.error_text or "").strip().splitlines()[0:1]
        what = f"{node.test_name or 'data'} test on {node.test_column or node.attached_node or node.name}"
        return NodeClassification(
            unique_id=node.unique_id,
            category=DATA_TEST,
            confidence=_HIGH,
            rule_id=f"data_test.{node.test_name or 'custom'}",
            evidence_lines=[f"{what} {node.status}ed" + (f": {line[0][:300]}" if line else "")],
        )
    result = classify_lines(_node_text(node) + list(extra_lines))
    result.unique_id = node.unique_id
    return result


def classify(evidence: PipelineFailureEvidence) -> Classification:
    """Classify every failing node and pick the run-level category."""
    primary = evidence.primary_node()
    per_node: List[NodeClassification] = []
    for node in evidence.failing_nodes:
        # Log/airflow nodes carry only a summary; give them the full error lines too.
        extra = evidence.error_lines if node.resource_type in ("task", "step") else ()
        per_node.append(classify_node(node, extra))

    if not per_node:
        fallback = classify_lines(evidence.error_lines)
        per_node.append(fallback)

    chosen = _choose(per_node, primary.unique_id if primary else None)
    return Classification(
        category=chosen.category,
        confidence=chosen.confidence,
        rule_id=chosen.rule_id,
        primary_node=chosen.unique_id or (primary.unique_id if primary else None),
        evidence_lines=chosen.evidence_lines,
        note=DATA_TEST_NOTE if chosen.category == DATA_TEST else None,
        identifiers=chosen.identifiers,
        nodes=per_node,
    )


def _choose(per_node: List[NodeClassification], primary_id: Optional[str]) -> NodeClassification:
    """Errors explain a run better than test failures; specific beats generic."""
    errors = [c for c in per_node if c.category != DATA_TEST]
    pool = errors or per_node
    order = {c: i for i, c in enumerate(_PRECEDENCE + (DATA_TEST, UNKNOWN))}

    def key(c: NodeClassification) -> tuple:
        return (
            c.category == UNKNOWN,
            -_CONF_RANK[c.confidence],
            order.get(c.category, 99),
            c.unique_id != primary_id,
        )

    return sorted(pool, key=key)[0]


def _identifiers(lines: Sequence[str]) -> List[str]:
    found: List[str] = []
    for line in lines:
        for pat in _IDENTIFIER_PATTERNS:
            for m in pat.finditer(line):
                ident = m.group(1).strip('"').strip()
                if ident and ident not in found:
                    found.append(ident)
    return found[:10]


def _dedupe(lines: Sequence[str]) -> List[str]:
    out: List[str] = []
    for ln in lines:
        ln = ln[:400]
        if ln not in out:
            out.append(ln)
    return out
