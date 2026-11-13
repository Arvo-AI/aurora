"""The single evidence shape every pipeline intake normalizes into.

dbt artifacts, an Airflow ``on_failure_callback`` payload and a pasted log all
become one ``PipelineFailureEvidence``. The classifier, the lineage walk and
the RCA prompt only ever read this object, so a new input format is a new
parser and nothing else.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

SOURCE_DBT = "dbt"
SOURCE_AIRFLOW = "airflow"
SOURCE_LOG = "log"
SOURCE_KINDS = (SOURCE_DBT, SOURCE_AIRFLOW, SOURCE_LOG)


@dataclass
class FailingNode:
    """One node (dbt model/test, Airflow task, log step) that did not succeed."""

    unique_id: str
    name: str
    resource_type: str
    status: str
    error_text: str = ""
    compiled_sql: Optional[str] = None
    file_path: Optional[str] = None
    execution_time: Optional[float] = None
    failures: Optional[int] = None
    test_name: Optional[str] = None
    test_column: Optional[str] = None
    attached_node: Optional[str] = None


@dataclass
class PipelineFailureEvidence:
    """Everything known about one failed pipeline run, already redacted."""

    source_kind: str
    title: str
    pipeline_name: str
    run_id: Optional[str] = None
    failing_nodes: List[FailingNode] = field(default_factory=list)
    skipped_nodes: List[str] = field(default_factory=list)
    error_lines: List[str] = field(default_factory=list)
    log_excerpt: Optional[str] = None
    recent_changes: Optional[str] = None
    timings: Dict[str, Any] = field(default_factory=dict)
    previous_run: Dict[str, Any] = field(default_factory=dict)
    context: Dict[str, Any] = field(default_factory=dict)
    # Filled by the lineage walk when a manifest was supplied.
    lineage: Dict[str, Any] = field(default_factory=dict)
    redactions: int = 0
    truncated_fields: List[str] = field(default_factory=list)

    def node_keys(self) -> List[str]:
        return [n.unique_id for n in self.failing_nodes]

    def primary_node(self) -> Optional[FailingNode]:
        """The node to explain first: errors before test failures, models before tests."""
        if not self.failing_nodes:
            return None

        def rank(node: FailingNode) -> tuple:
            errored = 0 if node.status in ("error", "runtime error", "failed") else 1
            is_test = 1 if node.resource_type == "test" else 0
            return (errored, is_test)

        return sorted(self.failing_nodes, key=rank)[0]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PipelineFailureEvidence":
        data = dict(data)
        data["failing_nodes"] = [FailingNode(**n) for n in data.get("failing_nodes") or []]
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})
