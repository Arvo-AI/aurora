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
    RECORD_ALERT,
    RECORD_DEPLOY_CHANGE,
    RECORD_DEPLOY_EVENT,
    RECORD_LOG,
    RECORD_METRIC,
    EvidenceBundle,
    parse_ts,
)

VERIFIED_OBSERVATION = "verified_observation"
SUPPORTED_INFERENCE = "supported_inference"
UNVERIFIED_HYPOTHESIS = "unverified_hypothesis"
INVALID_CITATION = "invalid_citation"

#: claim_type -> the record_type it must cite at least one of.
OBSERVABLE_TYPES = {
    "deployment_observed": RECORD_DEPLOY_EVENT,
    "configuration_change": RECORD_DEPLOY_CHANGE,
    # An alert firing is an observation in its own right. Without this type the
    # only bucket that fits is error_observed, which demands a log line — so a
    # correctly-cited alert was being demoted for citing a record of the wrong
    # kind when it was the taxonomy that was wrong, not the claim.
    "alert_observed": RECORD_ALERT,
    "error_observed": RECORD_LOG,
    "metric_degradation": RECORD_METRIC,
}

INFERENCE_TYPES = {"temporal_relationship", "causal_hypothesis"}

KNOWN_CLAIM_TYPES = set(OBSERVABLE_TYPES) | INFERENCE_TYPES

_FIELD_TOKEN = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_WORD_TOKEN = re.compile(r"[A-Za-z_][\w.]{5,}")
_STATUS_CODE = re.compile(r"\b(4\d{2}|5\d{2})\b")
#: An alert identifier such as ``PD-88213``: the one token two alerts cannot share.
_ALERT_ID = re.compile(r"\b[A-Z]{2,}-\d+\b")

#: Words that assert one event came before or after another. A temporal claim
#: containing one is demoted, because direction is not machine-checked here.
_ORDER_PHRASES = ("before", "preceded", "earlier than", "prior to", "ahead of",
                  "after", "followed")
#: A number that counts as a stated value. Deliberately loose on the trailing
#: side so "2400ms" counts, and deliberately anchored on a word boundary so the
#: digits inside "http_5xx_rate" and "p99_latency_ms" do not: those name
#: metrics, they do not state values. Single digits are ignored, so "5xx" at the
#: start of a sentence is not mistaken for a value either.
_VALUE_TOKEN = re.compile(r"\b\d+(?:\.\d+)?")


def _states_a_value(text: str) -> bool:
    return any(len(match) >= 2 for match in _VALUE_TOKEN.findall(text))
#: A deploy_change record's content is written by :mod:`.evidence` as
#: ``<file> <field>: <from> -> <to>``. Parsing it with ``partition`` keeps the
#: extraction linear; a pattern with adjacent greedy groups over the same text
#: made the analyzer (rightly) nervous about backtracking.
_CHANGE_SEP = ": "
_CHANGE_ARROW = " -> "


def _parse_change(content: str) -> Optional[Tuple[str, str, str]]:
    """Return ``(field, from, to)`` for a deploy_change record, else None."""
    head, separator, tail = content.partition(_CHANGE_SEP)
    if not separator:
        return None
    field = head.split()[-1] if head.split() else ""
    old, arrow, new = tail.partition(_CHANGE_ARROW)
    if not field or not arrow:
        return None
    return field, old.strip(), new.strip()

#: Seconds of slack when comparing a timeline entry's own timestamp with the
#: records it cites. Models routinely round "14:35:21" to "14:35:00"; a whole
#: minute of skew is imprecision, whereas citing a 14:32 event as 14:20 is not.
_TIMELINE_TOLERANCE_S = 60.0


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
    #: How many timeline entries were examined, and how many of those could
    #: actually be tied to evidence (a resolvable citation *and* a parseable
    #: timestamp). ``timeline_ok`` only reports whether the entries contradict
    #: each other or their citations; an entry with no citation used to count as
    #: fine, which made "consistent" sound stronger than it was.
    timeline_entries: int = 0
    timeline_evidenced: int = 0

    @property
    def citation_resolution_rate(self) -> float:
        """Share of cited IDs that exist in the bundle.

        This is *not* an accuracy measure. It says nothing about whether the
        claim is true; only that the reference is not fabricated.
        """
        return (self.resolved_citations / self.total_citations) if self.total_citations else 0.0

    @property
    def evidence_type_validity(self) -> float:
        """Share of *checkable* findings whose citations are of the required type.

        Causal claims are excluded from the denominator. They are never promoted
        whatever they cite, so counting them as a miss made a report where every
        verifiable claim was supported read as "83% valid" — the 17% being the two
        hypotheses the design deliberately refuses to confirm.
        """
        checkable = [f for f in self.findings if f.claim_type != "causal_hypothesis"]
        if not checkable:
            return 0.0
        return self.type_valid_findings / len(checkable)

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
            "timeline_entries": self.timeline_entries,
            "timeline_evidenced": self.timeline_evidenced,
            # The model's own prose is *not* checked. Saying so here keeps the
            # contract in the payload instead of only in the UI's wording.
            "narration": {
                "source": "model",
                "verified": False,
                "note": "prose written by the model; only the findings and the "
                        "timeline below are checked against the evidence",
            },
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
    """A config claim must name a field the evidence records, and any value it
    states must be the value the evidence records.

    Matching on the field name alone would let "maxPoolSize changed from 50 to
    10" be confirmed by a record that says 50 -> 25. When the claim states a
    number, the record's new value has to appear in it.
    """
    claimed_fields = set(_FIELD_TOKEN.findall(claim))
    states_a_value = _states_a_value(claim)

    for record in records:
        if record.record_type != RECORD_DEPLOY_CHANGE:
            continue
        parsed = _parse_change(record.content)
        if parsed is None:
            continue
        field, old_value, new_value = parsed
        if not (_claim_mentions_name(claim, field) or field in claimed_fields):
            continue
        if not states_a_value:
            return True, f"field {field} present in evidence [{record.id}]"
        if new_value and new_value in claim:
            return True, f"field {field} -> {new_value} matches evidence [{record.id}]"
        return False, (
            f"claim cites {field} but states a value the evidence does not record "
            f"(evidence: {old_value} -> {new_value})"
        )
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


def _supports_alert(claim: str, records: List[Any]) -> Tuple[bool, str]:
    """An alert claim must cite an alert record that names the same alert.

    The alert identifier is checked first because it is the one token that cannot
    be shared by accident; the word overlap is the fallback for alerts described
    without their id.
    """
    claimed_ids = set(_ALERT_ID.findall(claim))
    claimed_words = {token.lower() for token in _WORD_TOKEN.findall(claim)}
    for record in records:
        if record.record_type != RECORD_ALERT:
            continue
        if claimed_ids & set(_ALERT_ID.findall(record.content)):
            return True, f"alert evidence [{record.id}] names the same alert"
        words = {token.lower() for token in _WORD_TOKEN.findall(record.content)}
        if claimed_words & words:
            return True, f"alert evidence [{record.id}] contains the described alert"
    return False, "no cited alert record names the alert the claim describes"


def _name_tokens(name: str) -> set:
    """Distinctive words in an identifier, for tolerant matching.

    A record calls a field ``image.tag`` and a metric ``http_5xx_rate``; a model
    writing prose calls them "image tag" and "5xx rate". Requiring the exact
    identifier demoted correct claims, so the words are compared instead — but
    only the distinctive ones, and never a single word, so that "tag" alone does
    not match every field ending in ".tag".
    """
    return {token for token in re.split(r"[^a-z0-9]+", name.lower()) if len(token) >= 3}


def _claim_mentions_name(claim: str, name: str) -> bool:
    """True when the claim names this identifier, verbatim or as prose words."""
    if name in claim:
        return True
    tokens = _name_tokens(name)
    if len(tokens) < 2:
        return False
    lowered = claim.lower()
    return sum(1 for token in tokens if token in lowered) >= 2


def _claim_mentions_metric(claim: str, name: str) -> bool:
    return _claim_mentions_name(claim, name)


def _supports_metric(claim: str, records: List[Any]) -> Tuple[bool, str]:
    """A metric claim must name the metric and, if it states numbers, use values
    the cited samples actually contain.

    Values are collected across *all* cited samples: a claim spanning a window
    ("0.4 at 14:20 to 7.8 at 14:40") is legitimately checked against the earliest
    and the latest sample, not only the first one.
    """
    states_a_value = _states_a_value(claim)
    for record in records:
        if record.record_type != RECORD_METRIC or "=" not in record.content:
            continue
        name = record.content.split("=", 1)[0].split()[-1]
        if not name or not _claim_mentions_metric(claim, name):
            continue
        values = {
            r.content.split("=", 1)[1].split()[0]
            for r in records
            if r.record_type == RECORD_METRIC and "=" in r.content
            and r.content.split("=", 1)[0].split()[-1] == name
        }
        if not states_a_value or any(value in claim for value in values if value):
            return True, f"metric {name} cited as evidence [{record.id}]"
        return False, (
            f"claim names {name} but states values the cited samples do not contain "
            f"(samples: {sorted(v for v in values if v)[:4]})"
        )
    return False, "no cited metric record names the metric the claim describes"


_SUPPORT_RULES = {
    "deployment_observed": _supports_deployment,
    "configuration_change": _supports_config_change,
    "alert_observed": _supports_alert,
    "error_observed": _supports_error,
    "metric_degradation": _supports_metric,
}




def _check_ordering(claim: str) -> Optional[str]:
    """Return the ordering phrase a temporal claim asserts, if any.

    This does *not* try to judge whether the assertion is true — inferring which
    side is the subject proved unreliable and demoted correct statements. It only
    detects that an ordering is being asserted, so the validator can decline to
    call that "supported" when it has no way to check the direction.
    """
    lowered = claim.lower()
    return next((phrase for phrase in _ORDER_PHRASES if phrase in lowered), None)


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
        phrase = _check_ordering(claim)
        if phrase:
            # The records are timestamped, but nothing here checks that they are in
            # the order the sentence claims. Saying "supported" would vouch for a
            # direction that was never examined.
            return UNVERIFIED_HYPOTHESIS, (
                f"the claim asserts an ordering ('{phrase}') that is not independently "
                "verified; the cited records are timestamped, but their direction is "
                "not checked"
            )
        return SUPPORTED_INFERENCE, "all cited records carry timestamps; no ordering is asserted"

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


def _check_entry_citations(
    bundle: EvidenceBundle, report: ValidationReport, index: int, entry: Dict[str, Any]
) -> List[Any]:
    """Resolve an entry's citations, reporting the ones that do not resolve."""
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
    return [bundle.by_id(record_id) for record_id in ids]


def _check_entry_timestamp(
    report: ValidationReport,
    index: int,
    at: str,
    cited: List[Any],
) -> None:
    """Compare an entry's own timestamp with the records it cites."""
    entry_at = parse_ts(at)
    # Only records that actually carry a timestamp can corroborate one. A runbook
    # line has none, so citing it must not make an entry look evidenced.
    timed_records = [record for record in cited if record is not None and record.at]
    cited_ats = [record.at for record in timed_records]
    if not entry_at or not cited_ats:
        return

    if len(cited_ats) == 1:
        record = timed_records[0]
        # A record can state more than one instant in its own content (a deploy
        # records both its start and its completion). Quoting the second one is
        # accurate, so a timestamp that appears verbatim in the record is not a
        # mismatch even when it differs from the record's own stamp.
        if at and record and at in (record.content or ""):
            return
        cited_at = parse_ts(cited_ats[0])
        if cited_at and abs((entry_at - cited_at).total_seconds()) > _TIMELINE_TOLERANCE_S:
            report.timeline_ok = False
            report.timeline_problems.append(
                f"timeline entry {index} says {at} but its evidence is stamped {cited_ats[0]}"
            )
        return

    stamps = sorted(stamp for stamp in (parse_ts(value) for value in cited_ats) if stamp)
    if not stamps:
        return
    low = stamps[0].timestamp() - _TIMELINE_TOLERANCE_S
    high = stamps[-1].timestamp() + _TIMELINE_TOLERANCE_S
    if not low <= entry_at.timestamp() <= high:
        report.timeline_ok = False
        report.timeline_problems.append(
            f"timeline entry {index} says {at}, outside its evidence span "
            f"{cited_ats[0]}..{cited_ats[-1]}"
        )


def _timeline_entry_at(
    bundle: EvidenceBundle, report: ValidationReport, index: int, entry: Any
) -> Tuple[str, bool]:
    """Validate one timeline entry.

    Returns ``(timestamp, evidenced)``. ``evidenced`` means the entry carries a
    parseable time *and* cites at least one record that itself carries a
    timestamp. An entry backed only by a runbook line is not evidenced: the
    runbook has no time, so nothing ties the entry's clock to the bundle.
    """
    if not isinstance(entry, dict):
        return "", False
    at = str(entry.get("at") or "")
    cited = _check_entry_citations(bundle, report, index, entry)
    _check_entry_timestamp(report, index, at, cited)
    has_timed_citation = any(record is not None and record.at for record in cited)
    evidenced = bool(parse_ts(at)) and has_timed_citation
    return at, evidenced


def _check_timeline(bundle: EvidenceBundle, report: ValidationReport, timeline: List[Any]) -> None:
    previous = ""
    for index, entry in enumerate(timeline):
        at, evidenced = _timeline_entry_at(bundle, report, index, entry)
        report.timeline_entries += 1
        if evidenced:
            report.timeline_evidenced += 1
        if at and previous and at < previous:
            report.timeline_ok = False
            report.timeline_problems.append(
                f"timeline entry {index} is out of order: {at} < {previous}"
            )
        if at:
            previous = at


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


