"""Deterministic evidence validation for a replay result.

The model is asked to classify every finding it makes, and this module decides
what each classification is actually allowed to claim. It never asks the model
whether it was right, and it never promotes a causal statement: ordering plus
correlation is not causation, so a ``causal_hypothesis`` stays a hypothesis even
when every citation resolves perfectly.

Claim types and what each one requires:

=====================  ====================================================
claim_type             requirement
=====================  ====================================================
deployment_observed    cites a ``deploy_event`` record
configuration_change   cites a ``deploy_change`` record naming the same field
error_observed         cites a ``log`` record sharing a distinctive token
metric_degradation     cites a ``metric`` record naming the same metric
temporal_relationship  every cited record has a timestamp
causal_hypothesis      never verified; always reported as a hypothesis
=====================  ====================================================

Anything citing an ID that does not exist, or an ID of the wrong record type,
is reported as such rather than silently dropped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .evidence import (
    RECORD_DEPLOY_CHANGE,
    RECORD_DEPLOY_EVENT,
    RECORD_LOG,
    RECORD_METRIC,
    EvidenceBundle,
)

VERIFIED_OBSERVATION = "verified_observation"
SUPPORTED_INFERENCE = "supported_inference"
UNVERIFIED_HYPOTHESIS = "unverified_hypothesis"
INVALID_CITATION = "invalid_citation"

#: claim_type -> the record_type it must cite at least one of.
OBSERVABLE_TYPES = {
    "deployment_observed": RECORD_DEPLOY_EVENT,
    "configuration_change": RECORD_DEPLOY_CHANGE,
    "error_observed": RECORD_LOG,
    "metric_degradation": RECORD_METRIC,
}

INFERENCE_TYPES = {"temporal_relationship", "causal_hypothesis"}

KNOWN_CLAIM_TYPES = set(OBSERVABLE_TYPES) | INFERENCE_TYPES

_FIELD_TOKEN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_WORD_TOKEN = re.compile(r"[A-Za-z_][\w.]{5,}")
_STATUS_CODE = re.compile(r"\b(4\d{2}|5\d{2})\b")


@dataclass
class FindingVerdict:
    index: int
    claim_type: str
    claim: str
    cited: List[int]
    status: str
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationReport:
    findings: List[FindingVerdict] = field(default_factory=list)
    unknown_claim_types: List[str] = field(default_factory=list)
    missing_ids: List[int] = field(default_factory=list)
    total_citations: int = 0
    resolved_citations: int = 0
    type_valid_findings: int = 0
    timeline_ok: bool = True
    timeline_problems: List[str] = field(default_factory=list)

    @property
    def citation_resolution_rate(self) -> float:
        """Share of cited IDs that exist in the bundle.

        This is *not* an accuracy measure. It says nothing about whether the
        claim is true; only that the reference is not fabricated.
        """
        return (self.resolved_citations / self.total_citations) if self.total_citations else 0.0

    @property
    def evidence_type_validity(self) -> float:
        """Share of findings whose citations are of the type the claim requires."""
        if not self.findings:
            return 0.0
        return self.type_valid_findings / len(self.findings)

    def status_counts(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for verdict in self.findings:
            counts[verdict.status] = counts.get(verdict.status, 0) + 1
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "status_counts": self.status_counts(),
            "unknown_claim_types": self.unknown_claim_types,
            "missing_ids": self.missing_ids,
            "total_citations": self.total_citations,
            "resolved_citations": self.resolved_citations,
            "citation_resolution_rate": round(self.citation_resolution_rate, 3),
            "evidence_type_validity": round(self.evidence_type_validity, 3),
            "timeline_ok": self.timeline_ok,
            "timeline_problems": self.timeline_problems,
        }


def _coerce_ids(raw: Any) -> Tuple[List[int], List[Any]]:
    """Split a claimed ID list into usable integers and rejected values.

    A string that happens to be numeric is accepted because models routinely
    serialise integers as strings; anything else is rejected rather than
    coerced, so a model cannot smuggle an arbitrary token into a citation.
    """
    good: List[int] = []
    bad: List[Any] = []
    if not isinstance(raw, list):
        return good, [raw]
    for item in raw:
        if isinstance(item, bool):
            bad.append(item)
        elif isinstance(item, int):
            good.append(item)
        elif isinstance(item, str) and item.strip().isdigit():
            good.append(int(item.strip()))
        else:
            bad.append(item)
    return good, bad


def _supports_deployment(claim: str, records: List[Any]) -> Tuple[bool, str]:
    for record in records:
        if record.record_type != RECORD_DEPLOY_EVENT:
            continue
        tokens = {t.lower() for t in _WORD_TOKEN.findall(record.content)}
        if any(t in claim.lower() for t in tokens):
            return True, f"deploy event [{record.id}] corroborates the claim"
    return False, "no cited deploy_event record mentions the deployment in the claim"


def _supports_config_change(claim: str, records: List[Any]) -> Tuple[bool, str]:
    claimed = set(_FIELD_TOKEN.findall(claim))
    for record in records:
        if record.record_type != RECORD_DEPLOY_CHANGE:
            continue
        present = set(_FIELD_TOKEN.findall(record.content))
        if claimed & present:
            return True, f"field {sorted(claimed & present)} present in evidence [{record.id}]"
    return False, "no cited deploy_change record names the field in the claim"


def _supports_error(claim: str, records: List[Any]) -> Tuple[bool, str]:
    claimed_words = {t.lower() for t in _WORD_TOKEN.findall(claim)}
    claimed_codes = set(_STATUS_CODE.findall(claim))
    for record in records:
        if record.record_type != RECORD_LOG:
            continue
        words = {t.lower() for t in _WORD_TOKEN.findall(record.content)}
        codes = set(_STATUS_CODE.findall(record.content))
        if claimed_words & words or claimed_codes & codes:
            return True, f"log evidence [{record.id}] contains the described error"
    return False, "no cited log record contains the error the claim describes"


def _supports_metric(claim: str, records: List[Any]) -> Tuple[bool, str]:
    for record in records:
        if record.record_type != RECORD_METRIC:
            continue
        name = record.content.split("=", 1)[0].split()[-1] if "=" in record.content else ""
        if name and name in claim:
            return True, f"metric {name} cited as evidence [{record.id}]"
    return False, "no cited metric record names the metric the claim describes"


_SUPPORT_RULES = {
    "deployment_observed": _supports_deployment,
    "configuration_change": _supports_config_change,
    "error_observed": _supports_error,
    "metric_degradation": _supports_metric,
}




def _classify(claim_type: str, claim: str, resolved: List[Any], untimed: List[int]) -> Tuple[str, str]:
    """Decide what one finding is allowed to claim. Pure.

    The returned status is the strongest the evidence supports; a causal claim
    has no path to ``verified_observation`` at all.
    """
    if claim_type == "causal_hypothesis":
        return (
            UNVERIFIED_HYPOTHESIS,
            "causal claim: order and correlation cannot establish causation from "
            "the evidence alone",
        )
    if claim_type == "temporal_relationship":
        if untimed:
            return UNVERIFIED_HYPOTHESIS, f"cited record(s) without a timestamp: {untimed}"
        return SUPPORTED_INFERENCE, "all cited records are timestamped and ordered"

    required = OBSERVABLE_TYPES[claim_type]
    if not any(record.record_type == required for record in resolved):
        return UNVERIFIED_HYPOTHESIS, f"{claim_type} must cite at least one {required} record"
    supported, why = _SUPPORT_RULES[claim_type](claim, resolved)
    return (VERIFIED_OBSERVATION if supported else UNVERIFIED_HYPOTHESIS), why


def _evaluate(bundle: EvidenceBundle, report: ValidationReport, index: int, raw: Any) -> Optional[FindingVerdict]:
    """Turn one raw finding dict into a verdict, updating the report counters."""
    if not isinstance(raw, dict):
        return None

    claim_type = str(raw.get("claim_type") or "").strip()
    claim = str(raw.get("claim") or "").strip()
    good_ids, bad_ids = _coerce_ids(raw.get("evidence_ids"))
    verdict = FindingVerdict(
        index=index, claim_type=claim_type, claim=claim, cited=good_ids,
        status=UNVERIFIED_HYPOTHESIS,
    )

    report.total_citations += len(good_ids) + len(bad_ids)
    missing = [i for i in good_ids if bundle.by_id(i) is None]
    report.resolved_citations += len(good_ids) - len(missing)

    if bad_ids or missing:
        if bad_ids:
            verdict.reasons.append(f"malformed evidence id(s): {bad_ids!r}")
        if missing:
            verdict.reasons.append(f"cited id(s) not in the bundle: {missing}")
            report.missing_ids.extend(missing)
        verdict.status = INVALID_CITATION
        return verdict

    if claim_type not in KNOWN_CLAIM_TYPES:
        verdict.reasons.append(f"unknown claim_type {claim_type!r}")
        report.unknown_claim_types.append(claim_type)
        return verdict

    resolved = [bundle.by_id(i) for i in good_ids]
    if not resolved:
        verdict.reasons.append("finding cites no evidence")
        return verdict

    status, reason = _classify(claim_type, claim, resolved, [r.id for r in resolved if not r.at])
    verdict.status = status
    verdict.reasons.append(reason)
    if status in (VERIFIED_OBSERVATION, SUPPORTED_INFERENCE):
        report.type_valid_findings += 1
    return verdict


def _check_timeline(bundle: EvidenceBundle, report: ValidationReport, timeline: List[Any]) -> None:
    previous = ""
    for index, entry in enumerate(timeline):
        if not isinstance(entry, dict):
            continue
        at = str(entry.get("at") or "")
        if at and previous and at < previous:
            report.timeline_ok = False
            report.timeline_problems.append(
                f"timeline entry {index} is out of order: {at} < {previous}"
            )
        if at:
            previous = at
        ids, bad = _coerce_ids(entry.get("evidence_ids"))
        for record_id in ids:
            if bundle.by_id(record_id) is None:
                report.timeline_ok = False
                report.timeline_problems.append(
                    f"timeline entry {index} cites missing id {record_id}"
                )
        if bad:
            report.timeline_ok = False
            report.timeline_problems.append(f"timeline entry {index} has malformed ids {bad!r}")


def validate(bundle: EvidenceBundle, result: Dict[str, Any]) -> ValidationReport:
    report = ValidationReport()
    for index, raw in enumerate(result.get("findings") or []):
        verdict = _evaluate(bundle, report, index, raw)
        if verdict is not None:
            report.findings.append(verdict)
    _check_timeline(bundle, report, result.get("timeline") or [])
    return report


def validate_payload(payload: Dict[str, Any], bundle: EvidenceBundle) -> Dict[str, Any]:
    """Convenience wrapper used by the route and the tests."""
    return validate(bundle, payload).to_dict()
