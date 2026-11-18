"""Regression detection against a learned baseline, with change attribution.

No threshold is configured by a person. For the first ``learning_polls`` polls
the detector only learns what normal looks like for each workload and Service:
how many replicas are ready, how many endpoints a Service has, which log lines
appear and whether a workload keeps logging successful requests, and which
Warning events fire anyway. Objects created later get the same learning window
of their own; a workload that was already failing when Watch started is not
reported while it is no worse than it was then. After that it reports
*regressions from that baseline*:

* a Service that had ready endpoints and now has none (not when its workloads
  were scaled to zero, or while a Recreate rollout replaces every pod and progresses);
* a workload that lost ready replicas it had (a scale-up waiting on new pods, a
  scale to zero, and a progressing rollout within its maxUnavailable are not losses);
* pods that cannot start (CrashLoopBackOff, image pull, bad config, unschedulable,
  an init container that never finishes) and rollouts that stop progressing;
* containers that restart (with the last exit reason, e.g. OOMKilled);
* Warning events that were not firing while the workload was healthy;
* error log lines never seen for that workload, a known one at a rate the
  baseline makes implausible, or a workload that logs failing requests and no
  successful ones where it used to log successes.

A symptom must be seen in ``confirm_polls`` polls before it becomes a finding,
so a single bad poll pages nobody; bursty signals stay active for a hold window
so back-off gaps do not reset them. Every finding carries its evidence and the
cluster changes most likely to have caused it, ranked by how the changed object
relates to the failing one and how close in time the change was. Findings are
grouped into incidents: one per causing change, with downstream symptoms
(clients timing out on a broken Service, a workload whose dependency is down)
joining their cause.

Everything here is pure and deterministic: no I/O, no LLM. The state is a plain
dict so the caller can persist it as JSON between polls.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any

from services.watch.changes import detect_changes
from services.watch.logs import normalize
from services.watch.snapshot import _QUERY_PARAM, BAD_WAITING, DIGEST_ID, TRACKED_OBJECTS, _mask_query
from utils.security.output_redaction import redact

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
INCIDENT_MIN_SEVERITY = "high"

DEFAULTS: dict[str, Any] = {
    "learning_polls": 5,  # polls that only learn; the first one's logs are history and ignored
    "confirm_polls": 2,  # consecutive polls a symptom must hold to become a finding
    "resolve_polls": 3,  # clean polls before a finding closes
    "change_window_seconds": 3600,  # older changes are not suspects
    # A pod unschedulable, or held in an init container, this long is stuck. Generous
    # on purpose: autoscaled clusters leave pods Pending while a node is added, and
    # init containers that run migrations take minutes.
    "stuck_seconds": 120,
    # A rollout with no new pod updated or ready for this long is stuck (by this
    # watcher's clock): a Deployment's own progressDeadlineSeconds when it sets one,
    # else Kubernetes' default. Pods that cannot start at all are reported sooner
    # by their own signals (image pull, bad config, quota, unschedulable, init).
    "rollout_stuck_seconds": 600,
    # Logs of a workload that appeared after learning are ignored this long: all
    # of its lines are new, normal startup chatter included.
    "new_workload_grace_seconds": 60,
    # Bursty signals (Warning events, error logs) stay active this long after they
    # were last seen, so a crash loop's back-off gaps neither delay confirmation
    # nor make the finding flap between open and resolved.
    "hold_seconds": 180,
    # Restarts are counted over this window: one is a notice, two or more a crash loop.
    "restart_window_seconds": 600,
    # A longer gap between polls (Watch turned off, a worker backlog, an unreachable
    # cluster) re-baselines instead of counting what piled up meanwhile as "now".
    "max_gap_seconds": 600,
    "max_changes": 200,
    "max_templates_per_workload": 400,
    "max_closed": 50,
}

_LOG_KINDS = frozenset({"new_error_logs", "error_log_surge", "success_stopped", "log"})
_UNATTRIBUTED_JOIN_SECONDS = 600
_MAX_HELD_TEMPLATES = 50  # error templates kept per open finding (a hostile client can mint new ones)
_NAMEABLE_KINDS = frozenset({"ResourceQuota", "LimitRange", "NetworkPolicy", "PersistentVolumeClaim",
                             "ConfigMap", "Secret", "Service"})
# Warning reasons that mean pods cannot be created or given their volumes, as
# opposed to being slow (FailedScheduling is slow until it has lasted a while).
_HARD_FAILURE_REASONS = frozenset({"FailedCreate", "FailedMount", "FailedAttachVolume", "ProvisioningFailed"})
# A Warning reason that fired in at least this share of learning polls is background noise.
_CHRONIC_EVENT_SHARE = 0.5
# Surge test: a known error line is "surging" when its count this poll would be
# this unlikely under its baseline rate (Poisson), and it fired at least _SURGE_MIN times.
_SURGE_P = 1e-6
_SURGE_MIN = 5
_SURGE_RATE_FLOOR = 0.2


def _key(*parts: Any) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:16]


def _scrubbed(text: str) -> str:
    """Log text with credentials redacted: samples are stored and quoted to the RCA."""
    return redact(text)[0]


def labels_match(selector: dict[str, str] | None, labels: dict[str, str] | None) -> bool:
    return bool(selector) and all((labels or {}).get(k) == v for k, v in (selector or {}).items())


def poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) for X ~ Poisson(lam)."""
    if k <= 0:
        return 1.0
    lam = max(lam, 1e-9)
    term = math.exp(-lam)
    cdf = term
    for i in range(1, k):
        term *= lam / i
        cdf += term
    return max(0.0, 1.0 - cdf)


# Bump when what a snapshot keeps (fields, redaction) or how log lines are templated
# changes: a state saved by another version is learned again, not compared against.
STATE_VERSION = 2


def current_state(state: dict[str, Any] | None) -> dict[str, Any]:
    """The stored state, or a fresh one when it is missing, from another version, or made
    with another digest key. An old snapshot was cleaned and templated under other
    rules (or its masked values digested with another key), so diffing against it
    would report changes and new log lines that never happened, quoting whatever the
    old rules kept."""
    if not state or state.get("version") != STATE_VERSION or state.get("digest_id") != DIGEST_ID:
        return new_state()
    return state


def new_state(config: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "digest_id": DIGEST_ID,
        "config": {**DEFAULTS, **(config or {})},
        "polls": 0,
        "prev": None,
        "baseline": {
            "workloads": {}, "services": {}, "event_polls": {},
            "templates": {}, "log_polls": {}, "success_polls": {},
        },
        "seen_events": {},
        "changes": [],
        "pending": {},
        "open": {},
        "closed": [],
    }


def is_learning(state: dict[str, Any]) -> bool:
    return state["polls"] < state["config"]["learning_polls"]


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def workload_of_object(kind: str | None, namespace: str | None, name: str | None,
                       snapshot: dict[str, Any]) -> str | None:
    """The workload key an event's involved object belongs to, if any."""
    workloads = snapshot["workloads"]
    if kind == "Pod":
        pod = snapshot["pods"].get(f"{namespace}/{name}")
        if pod:
            return pod["workload"]
        m = re.match(r"^(.*)-[a-z0-9]{8,10}-[a-z0-9]{5}$", name or "")
        if m and f"Deployment/{namespace}/{m.group(1)}" in workloads:
            return f"Deployment/{namespace}/{m.group(1)}"
        return None
    if kind == "ReplicaSet":
        m = re.match(r"^(.*)-[a-z0-9]{8,10}$", name or "")
        if m and f"Deployment/{namespace}/{m.group(1)}" in workloads:
            return f"Deployment/{namespace}/{m.group(1)}"
        return None
    if kind in ("Deployment", "StatefulSet", "DaemonSet"):
        return f"{kind}/{namespace}/{name}"
    if kind == "PersistentVolumeClaim":
        for key, w in workloads.items():
            if w["namespace"] == namespace and name in (w.get("references") or {}).get("PersistentVolumeClaim", []):
                return key
    return None


def _event_deltas(state: dict[str, Any], snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Warning events that are new, or fired again, since the previous poll."""
    seen = state["seen_events"]
    current: dict[str, int] = {}
    out = []
    for ev in snapshot["events"]:
        if ev.get("type") != "Warning":
            continue
        key = _key(ev.get("namespace"), ev.get("kind"), ev.get("name"), ev.get("reason"), (ev.get("message") or "")[:120])
        count = max(ev.get("count") or 1, current.get(key, 0))
        current[key] = count
        delta = count - seen.get(key, 0)
        if delta > 0:
            out.append({**ev, "delta": delta,
                        "workload": workload_of_object(ev.get("kind"), ev.get("namespace"), ev.get("name"), snapshot)})
    state["seen_events"] = current
    return out


# Before templating, in linear time on every line: tokens and ids (a long run mixing
# upper and lower case and digits, as keys, JWT segments and PATs do, or a lowercase
# run with one long segment mixing letters and digits, as cuid and session ids do;
# not names like checkout-service-v2 or a pod name), the value of a credential-shaped
# pair (password=..., "api_key": "...", logfmt or JSON, quoted with spaces or escaped
# inside a string), credential query parameters (?sig=..., ?pass=...), and the
# credential after an Authorization header. The full secret rules run where text is
# kept or quoted: a template the baseline stores, a finding's evidence, held lines.
_TOKEN_RUN = re.compile(r"[A-Za-z0-9+_=-]{20,}")
_CREDENTIAL_KEY = (r"(?:password|passwd|pass|pwd|passphrase|secret|token|api[_-]?key|apikey|access[_-]?key|"
                   r"private[_-]?key|auth[_-]?(?:token|key)|session[_-]?(?:id|token|key)|signature)")
# Anchored on the keyword (no scan before it): key, its closing quote (escaped or not),
# the separator, then a double-quoted value (escaped or not), a single-quoted one, or a
# bare one that stops at a delimiter.
_CREDENTIAL_PAIR = re.compile(
    rf"(?i)({_CREDENTIAL_KEY}\\?[\"']?[ \t]*([=:])[ \t]*)"
    r"(?:(\\?\")((?:[^\"\\]|\\(?!\"))*)|(')([^']*)|([^\s&\"',;|)\]}>\\]+))")
_AUTH_HEADER = re.compile(r"(?i)\b(authorization[\"']?[ \t]*[=:][ \t]*[\"']?(?:bearer|basic|token)[ \t]+)[^\s\"',;|)\]}>]+")


def _looks_like_token(run: str) -> bool:
    has_digit = any(c.isdigit() for c in run)
    has_upper = any(c.isupper() for c in run)
    has_lower = any(c.islower() for c in run)
    return has_digit and (has_upper and has_lower or has_upper and len(run) >= 20 and not has_lower
                          or any(len(s) >= 16 and any(c.isdigit() for c in s) and any(c.isalpha() for c in s)
                                 for s in re.split(r"[-_]", run)))


def _mask_pair(m: re.Match) -> str:
    prefix, separator, dquote, _, squote, _, bare = m.groups()
    if dquote is not None:
        return f"{prefix}{dquote}<redacted>"
    if squote is not None:
        return f"{prefix}'<redacted>"
    # Prose ("token: expired", "pass: ok") keeps its word unless it looks like a secret.
    if separator == ":" and not any(c.isdigit() for c in bare) and len(bare) < 12:
        return m.group(0)
    return f"{prefix}<redacted>"


def _mask_secrets(line: str) -> str:
    masked = _TOKEN_RUN.sub(lambda m: "<token>" if _looks_like_token(m.group(0)) else m.group(0), line)
    masked = _QUERY_PARAM.sub(lambda m: _mask_query(m, lambda _: "<redacted>"), masked)
    return _AUTH_HEADER.sub(r"\1<redacted>", _CREDENTIAL_PAIR.sub(_mask_pair, masked))


def template_counts(lines: list[str]) -> dict[str, dict[str, Any]]:
    """Log lines by template. Every line is masked in linear time before templating:
    normalize() masks digits and hex, which would hide a token from the secret rules
    and keep the rest of it in the template, and a token that differs per request
    would mint a new template per request. The full rules run later, only on what is
    kept or quoted, so their cost grows with findings, not with log volume."""
    counts: dict[str, dict[str, Any]] = {}
    for line in lines:
        masked = _mask_secrets(line.replace("\x00", ""))
        t = normalize(masked)
        if t is None:
            continue
        entry = counts.get(t.fingerprint)
        if entry is None:
            entry = counts[t.fingerprint] = {
                "template": t.template, "error": t.is_error, "request_failure": t.is_request_failure,
                "http_class": t.http_class, "count": 0, "sample": masked.strip()[:600],
            }
        entry["count"] += 1
    return counts


def _kept(entry: dict[str, Any]) -> dict[str, Any]:
    """A template entry about to be stored or quoted, through the full secret rules."""
    return {**entry, "template": _scrubbed(entry["template"]),
            "sample": _scrubbed(entry.get("sample") or "")[:300]}


def _absorb_templates(state: dict[str, Any], workload: str, counts: dict[str, dict[str, Any]]) -> None:
    known = state["baseline"]["templates"].setdefault(workload, {})
    limit = state["config"]["max_templates_per_workload"]
    for fp, entry in counts.items():
        k = known.get(fp)
        if k is None:
            if len(known) >= limit:
                continue
            known[fp] = {"template": _scrubbed(entry["template"]), "error": entry["error"],
                         "total": entry["count"], "polls": 1}
        else:
            k["total"] += entry["count"]
            k["polls"] += 1


def _absorb_resolved(state: dict[str, Any], workload: str, finding: dict[str, Any]) -> None:
    """A resolved finding's new error lines join the baseline at the rate they ran
    while it was open, on the same scale as what was learned: the same error coming
    back slowly does not page again, a surge of it still does. Lines the baseline
    already knew keep their learned rate (their counts here were the surge)."""
    held = finding.get("log_templates") or {}
    known = state["baseline"]["templates"].get(workload) or {}
    polls_open = max(1, finding.get("log_polls_open", 1))
    learned = max(1, state["baseline"]["log_polls"].get(workload, 0))
    _absorb_templates(state, workload, {
        fp: {**e, "count": max(1, round(e["count"] * learned / polls_open))}
        for fp, e in held.items() if fp not in known})


def _has_success(counts: dict[str, dict[str, Any]]) -> bool:
    return any(e.get("http_class") == "2xx" for e in counts.values())


def _failing_pods(pods: list[dict[str, Any]]) -> int:
    """Pods that cannot run: a container waiting in a bad state, or not schedulable."""
    return sum(1 for pod in pods
               if pod["unschedulable"] or any(c["waiting"] in BAD_WAITING for c in pod["containers"]))


def _failing_now(key: str, snapshot: dict[str, Any], prev: dict[str, Any] | None) -> bool:
    """A workload that is visibly broken in this snapshot (used to learn what was broken at start)."""
    w = snapshot["workloads"][key]
    if w["desired"] > 0 and w["ready"] < w["desired"]:
        return True
    prev_pods = (prev or {}).get("pods") or {}
    for pod in snapshot["pods"].values():
        if pod["workload"] != key:
            continue
        if pod["unschedulable"] or any(c["waiting"] in BAD_WAITING for c in pod["containers"]):
            return True
        old = prev_pods.get(f"{pod['namespace']}/{pod['name']}")
        if old and sum(c["restarts"] for c in pod["containers"]) > sum(c["restarts"] for c in old["containers"]):
            return True
    return False


def _learn(state: dict[str, Any], snapshot: dict[str, Any], events: list[dict[str, Any]],
           templates: dict[str, dict[str, dict[str, Any]]], only: set[str] | None = None) -> None:
    """Learn the baseline: of everything (the learning phase), or of ``only`` these
    subjects (workloads and Services that appeared later, in their own window).

    Late workloads learn only their non-error log lines: a new Deployment that
    logs failures is reported once its grace period is over, not taught them.
    """
    base = state["baseline"]
    now = snapshot["taken_at"]
    everything = only is None
    pods_by_workload: dict[str, list[dict[str, Any]]] = {}
    for pod in snapshot["pods"].values() if everything else ():
        if not pod["deleting"]:
            pods_by_workload.setdefault(pod["workload"], []).append(pod)
    for key, w in snapshot["workloads"].items():
        if not everything and key not in only:
            continue
        b = base["workloads"].setdefault(key, {"fully_ready_polls": 0, "polls": 0, "first_seen": now,
                                               "late": not everything})
        b["polls"] += 1
        if w["desired"] > 0 and w["ready"] >= w["desired"]:
            b["fully_ready_polls"] += 1
        if everything:
            # The workload at its worst while learning: as broken as that later is not a regression.
            b["min_ready"] = min(b.get("min_ready", w["ready"]), w["ready"])
            b["max_failing_pods"] = max(b.get("max_failing_pods", 0), _failing_pods(pods_by_workload.get(key, [])))
            if _failing_now(key, snapshot, state["prev"]):
                b["broken_at_start"] = True
    for key, s in snapshot["services"].items():
        if not everything and f"Service/{key}" not in only:
            continue
        b = base["services"].setdefault(key, {"max_ready": 0, "backends": [], "polls": 0,
                                              "late": not everything})
        b["polls"] = b.get("polls", 0) + 1
        b["max_ready"] = max(b["max_ready"], s["ready_endpoints"])
        b["backends"] = sorted(set(b["backends"]) | set(s["backends"]))
    if state["prev"] is not None:  # the first poll's events are history, not a rate
        for ev in events:
            if ev.get("workload") and (everything or ev["workload"] in only):
                k = f"{ev['workload']}|{ev['reason']}"
                base["event_polls"][k] = base["event_polls"].get(k, 0) + 1
    for workload, counts in templates.items():
        if not everything and workload not in only:
            continue
        _learn_logs(state, workload, counts, errors=everything)


def _learn_logs(state: dict[str, Any], workload: str, counts: dict[str, dict[str, Any]], *, errors: bool) -> None:
    """One more poll of a workload's log baseline: its lines (error lines only if
    ``errors``), and whether it logged successful requests."""
    base = state["baseline"]
    base["log_polls"][workload] = base["log_polls"].get(workload, 0) + 1
    if _has_success(counts):
        base["success_polls"][workload] = base["success_polls"].get(workload, 0) + 1
    _absorb_templates(state, workload, counts if errors else {fp: e for fp, e in counts.items() if not e["error"]})


def _learning_logs(state: dict[str, Any], snapshot: dict[str, Any],
                   templates: dict[str, dict[str, dict[str, Any]]]) -> set[str]:
    """Workloads there from the start whose logs were read in fewer polls than learning
    takes, as in a cluster with more workloads than one poll reads logs for. Their
    logs are learned the way every workload's were during learning, errors included,
    before any log line of theirs can count as new."""
    base, need = state["baseline"], state["config"]["learning_polls"] - 1
    return {w for w in templates
            if w in snapshot["workloads"] and not (base["workloads"].get(w) or {}).get("late")
            and base["log_polls"].get(w, 0) < need}


def _still_learning(state: dict[str, Any], snapshot: dict[str, Any]) -> set[str]:
    """Subjects that appeared after the learning phase and are inside their own window."""
    base, now, limit = state["baseline"], snapshot["taken_at"], state["config"]["learning_polls"]
    for key in snapshot["workloads"]:
        base["workloads"].setdefault(key, {"fully_ready_polls": 0, "polls": 0, "first_seen": now, "late": True})
    for key in snapshot["services"]:
        base["services"].setdefault(key, {"max_ready": 0, "backends": [], "polls": 0, "late": True})
    learning = {k for k, b in base["workloads"].items() if b.get("late") and b["polls"] < limit}
    learning |= {f"Service/{k}" for k, b in base["services"].items() if b.get("late") and b.get("polls", 0) < limit}
    return learning


# ---------------------------------------------------------------------------
# Symptoms
# ---------------------------------------------------------------------------

def _symptom(subject: str, kind: str, severity: str, summary: str, evidence: list[str],
             **extra: Any) -> dict[str, Any]:
    return {"subject": subject, "kind": kind, "severity": severity, "summary": summary,
            "evidence": evidence[:8], **extra}


def _log_symptoms(state: dict[str, Any], key: str, label: str, counts: dict[str, dict[str, Any]],
                  now: float) -> list[dict[str, Any]]:
    cfg, base = state["config"], state["baseline"]
    known = base["templates"].get(key) or {}
    wb = base["workloads"].get(key) or {}
    if wb.get("late") and now - wb.get("first_seen", now) < cfg["new_workload_grace_seconds"]:
        return []
    learned_polls = max(1, base["log_polls"].get(key, 0))
    open_fps = set(((state["open"].get(key) or {}).get("log_templates") or {}).keys())
    novel, surging = [], []
    for fp, entry in counts.items():
        if not entry["error"]:
            continue
        k = known.get(fp)
        if k is None:
            confirmed_before = state["pending"].get(f"{key}|log|{fp}", 0) >= 1 or fp in open_fps
            if entry["count"] >= 2 or confirmed_before:
                novel.append({**entry, "fp": fp})
        else:
            rate = k["total"] / learned_polls
            if entry["count"] >= _SURGE_MIN and poisson_tail(entry["count"], max(rate, _SURGE_RATE_FLOOR)) < _SURGE_P:
                surging.append({**entry, "fp": fp, "baseline_rate": round(rate, 2)})
    out = []
    for group, kind in ((novel, "new_error_logs"), (surging, "error_log_surge")):
        if not group:
            continue
        failing_requests = [e for e in group if e["request_failure"]]
        if failing_requests:
            severity, what = "high", "requests failing in"
        elif kind == "error_log_surge":
            severity, what = "medium", "error lines surging in"
        else:
            severity, what = "low", "error lines never seen before in"
        evidence = [f"{e['count']}x {_scrubbed(e.get('sample') or '')[:300]}"
                    for e in sorted(group, key=lambda e: -e["count"])[:8]]
        out.append(_symptom(key, kind, severity, f"{what} {label} logs", evidence,
                            fingerprints=[e["fp"] for e in group], traffic=bool(failing_requests)))
    # A workload that logged successful requests in (nearly) every learning poll
    # and now logs failures and no success at all. Any 2xx line counts as success,
    # so a log format change after a deploy does not trip it; failures must be
    # present, so a quiet night with no traffic does not either.
    learned_success = base["success_polls"].get(key, 0)
    # Failures that are new, or requests failing: a known background error line
    # in a quiet minute is not evidence that requests stopped succeeding.
    failing_now = any(e["error"] and (fp not in known or e["request_failure"]) for fp, e in counts.items())
    if (learned_polls >= 2 and learned_success >= learned_polls - 1
            and failing_now and not _has_success(counts)):
        out.append(_symptom(key, "success_stopped", "high", f"{label} stopped logging successful requests",
                            [f"logged successful (2xx) requests in {learned_success} of {learned_polls} baseline polls; none now"],
                            traffic=True))
    return out


def _workload_symptoms(state: dict[str, Any], snapshot: dict[str, Any], events: list[dict[str, Any]],
                       templates: dict[str, dict[str, dict[str, Any]]],
                       learning: set[str] | None = None,
                       log_learning: set[str] | None = None) -> list[dict[str, Any]]:
    cfg, base, prev = state["config"], state["baseline"], state["prev"] or {}
    now = snapshot["taken_at"]
    prev_pods = prev.get("pods") or {}
    pods_by_workload: dict[str, list[dict[str, Any]]] = {}
    for pod in snapshot["pods"].values():
        if not pod["deleting"]:
            pods_by_workload.setdefault(pod["workload"], []).append(pod)

    # Pods the cluster autoscaler is adding a node for: Pending is expected there.
    scaling_up = {ev.get("name") for ev in snapshot["events"]
                  if ev.get("kind") == "Pod" and ev.get("reason") == "TriggeredScaleUp"}

    learning = learning or set()
    out: list[dict[str, Any]] = []
    for key, w in snapshot["workloads"].items():
        pods = pods_by_workload.get(key, [])
        label = f"{w['kind'].lower()}/{w['name']}"
        stalled_for = _rollout_stalled_for(state, key, w, now)
        deadline = w.get("progress_deadline") or cfg["rollout_stuck_seconds"]
        rolling = stalled_for is not None and stalled_for < deadline
        b = base["workloads"].get(key) or {}
        capacity = state.setdefault("capacity", {}).get(key)
        # Already failing when Watch started: not a regression while it is no worse
        # than it was then. Fewer ready replicas or more failing pods than at start is.
        if b.get("broken_at_start"):
            if _failing_now(key, snapshot, prev):
                # No floor recorded (an older baseline): unknown, so quiet until it has been
                # healthy once, not "nothing was failing". Not filled in from this poll,
                # which may catch a crash-looping pod while it is briefly up.
                if (w["ready"] >= b.get("min_ready", 0)
                        and _failing_pods(pods) <= b.get("max_failing_pods", math.inf)):
                    continue
                if capacity is None:
                    capacity = b.get("min_ready")  # what it still had at its worst while learning
            else:
                b.pop("broken_at_start", None)

        # Lost capacity: fewer ready replicas than the workload had the last time
        # it was fully ready. A scale-up waiting on new pods is not a loss, and a
        # rollout that is progressing may take down as many pods as its strategy
        # allows (maxUnavailable), but not more.
        allowance = w.get("max_unavailable", w["desired"]) if rolling else 0
        if capacity and w["desired"] > 0 and w["ready"] < min(w["desired"], capacity) - allowance:
            out.append(_symptom(key, "unavailable", "critical" if w["ready"] == 0 else "high",
                                f"{label} has {w['ready']}/{w['desired']} replicas ready (it had {capacity})",
                                [f"{w['ready']} of {w['desired']} replicas ready"]))

        failing = []
        for pod in pods:
            age = now - (pod["created"] or now)
            for c in pod["containers"]:
                if c["waiting"] in BAD_WAITING:
                    last = f" (last exit: {c['last_reason']}, code {c['last_exit_code']})" if c.get("last_reason") else ""
                    msg = f": {c['waiting_message']}" if c.get("waiting_message") else ""
                    failing.append(f"pod {pod['name']} container {c['name']} {c['waiting']}{last}{msg}")
            if pod["unschedulable"] and age >= cfg["stuck_seconds"] and pod["name"] not in scaling_up:
                failing.append(f"pod {pod['name']} cannot be scheduled: {pod['unschedulable']}")
            if not pod["ready"] and age >= cfg["stuck_seconds"]:
                for c in pod["containers"]:
                    if c["init"] and c["running"]:
                        failing.append(f"pod {pod['name']} stuck in init container {c['name']} for {int(age)}s")
        if failing:
            out.append(_symptom(key, "pods_failing", "high", f"pods of {label} cannot start", failing))

        # Restarts are discrete events spaced out by back-off, so they are counted
        # over a window rather than required in consecutive polls.
        window = state.setdefault("restarts", {}).setdefault(key, {"at": [], "evidence": []})
        for pod in pods:
            old = prev_pods.get(f"{pod['namespace']}/{pod['name']}")
            if not old:
                continue
            old_counts = {(c["name"], c["init"]): c["restarts"] for c in old["containers"]}
            for c in pod["containers"]:
                delta = c["restarts"] - old_counts.get((c["name"], c["init"]), c["restarts"])
                if delta > 0:
                    why = f" (last exit: {c['last_reason']}, code {c['last_exit_code']})" if c.get("last_reason") else ""
                    window["at"] += [now] * min(delta, 10)
                    window["evidence"] = (window["evidence"] + [
                        f"pod {pod['name']} container {c['name']} restarted {delta}x{why}"])[-5:]
        window["at"] = [t for t in window["at"] if now - t <= cfg["restart_window_seconds"]]
        # Active while the window still counts a restart, so the count it reports is
        # true and a steady restart cadence keeps one finding open instead of closing
        # it between restarts and opening a new incident every cycle.
        if window["at"]:
            count = len(window["at"])
            oom = any("OOMKilled" in e for e in window["evidence"])
            out.append(_symptom(
                key, "restarting", "high" if count >= 2 else "medium",
                f"{label} containers are {'being OOM-killed' if oom else 'restarting'} "
                f"({count} restart{'s' if count > 1 else ''} in {cfg['restart_window_seconds'] // 60} min)",
                list(reversed(window["evidence"]))))
        else:
            state["restarts"].pop(key, None)

        # A rollout that stopped progressing: the controller has seen the new
        # generation, but no pod of it got updated or became ready for a while. One
        # Kubernetes holds on purpose (paused, OnDelete, a partition) waits, not stalls.
        if stalled_for is not None and not w.get("held"):
            progressing = w["conditions"].get("Progressing") or {}
            if stalled_for >= deadline or progressing.get("reason") == "ProgressDeadlineExceeded":
                out.append(_symptom(
                    key, "rollout_stuck", "high",
                    f"rollout of {label} is not progressing ({w['updated']}/{w['desired']} updated, "
                    f"no progress for {int(stalled_for)}s)",
                    [f"{w['updated']} of {w['desired']} replicas on the new revision, {w['ready']} ready",
                     f"Progressing: {progressing.get('reason')} {progressing.get('message') or ''}".strip()]))

        if key in learning:  # a late workload in its own learning window: no log or event signals yet
            continue
        chronic_floor = max(1, cfg["learning_polls"] - 1) * _CHRONIC_EVENT_SHARE
        fresh_events = [ev for ev in events
                        if ev.get("workload") == key
                        and base["event_polls"].get(f"{key}|{ev['reason']}", 0) < chronic_floor]
        lines = [f"{ev['reason']} on {(ev.get('kind') or '').lower()}/{ev.get('name')}: {ev.get('message')}"
                 for ev in fresh_events]
        # The controller cannot create or mount what the pods need: not slowness, a failure.
        hard = [line for ev, line in zip(fresh_events, lines) if ev["reason"] in _HARD_FAILURE_REASONS]
        if hard:
            out.append(_symptom(key, "cannot_create", "high", f"pods of {label} cannot be created", hard))
        if lines:
            out.append(_symptom(key, "warning_events", "medium", f"new Warning events on {label}", lines))

        # Logs were fetched for this workload this poll, and its log baseline is learned.
        if key in templates and key not in (log_learning or ()):
            out += _log_symptoms(state, key, label, templates[key], now)
    return out


def _rollout_stalled_for(state: dict[str, Any], key: str, w: dict[str, Any], now: float) -> float | None:
    """Seconds since a workload's in-flight rollout last made progress, by this watcher's clock.

    ``None`` when no rollout is in flight. A rollout Kubernetes holds on purpose (a
    paused Deployment, an OnDelete or partitioned StatefulSet) is still in flight
    while it replaces pods, so it keeps its maxUnavailable allowance; it is just never
    reported as stuck. Progress is a new generation, or more replicas updated or
    ready than this rollout has had so far: pods going unready again is not
    progress, so readiness that flaps cannot keep a stalled rollout looking alive.
    The object's own timestamps are not used (kubectl omits managedFields, so they
    only say when the workload was created).
    """
    rollouts = state.setdefault("rollouts", {})
    entry = rollouts.get(key)
    converging = w.get("converging", w["updated"] < w["desired"])
    partition = w.get("partition") or 0
    if (w.get("held") or partition) and w.get("observed_generation") == w.get("generation"):
        # Its revisions may never converge on their own (OnDelete, a partition), so it
        # is in flight only while it moves pods to the new revision: a new generation,
        # or more pods updated than at any poll of this generation. A deleted pod that
        # comes back is not progress; at rest, a fault in it is reported at once. The
        # mark is written only once the status describes this generation, drops when
        # pods leave (nodes gone from a DaemonSet), and a partitioned set that reached its
        # partition stays held for the generation, even while its canary pod is replaced.
        marks = state.setdefault("held_updated", {})
        before = ((state.get("prev") or {}).get("workloads") or {}).get(key) or {}
        seen = marks.get(key) or [before.get("generation"), before.get("updated", w["updated"])]
        new_generation = w.get("generation") != seen[0]
        mark = w["updated"] if new_generation else max(seen[1], w["updated"])
        if before.get("desired") is not None and w["desired"] < before["desired"]:
            mark = min(mark, w["updated"])
        mark = min(mark, w["desired"])
        if w.get("held") or mark >= w["desired"] - partition:
            w["held"] = True
            converging = new_generation or w["updated"] > seen[1]
        marks[key] = [w.get("generation"), mark]
    # Once under way, a rollout stays in flight until every replica is ready: the
    # last new pod counts as "updated" as soon as it is scheduled (DaemonSets).
    in_flight = w.get("observed_generation") == w.get("generation") and (
        converging or (entry is not None and w["ready"] < w["desired"]))
    if not in_flight:
        rollouts.pop(key, None)
        return None
    if entry is None or entry.get("generation") != w.get("generation") or "best" not in entry:
        rollouts[key] = {"generation": w.get("generation"), "best": [w["updated"], w["ready"]], "since": now}
        return 0.0
    best = entry["best"]
    if w["updated"] > best[0] or w["ready"] > best[1]:
        entry.update(best=[max(best[0], w["updated"]), max(best[1], w["ready"])], since=now)
        return 0.0
    return now - entry["since"]


def _record_capacity(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Remember how many replicas each workload had the last time it was fully ready."""
    capacity = state.setdefault("capacity", {})
    for key, w in snapshot["workloads"].items():
        if w["desired"] == 0:
            capacity[key] = 0  # scaled to zero on purpose: nothing is owed
        elif w["ready"] >= w["desired"]:
            capacity[key] = w["desired"]


def _service_symptoms(state: dict[str, Any], snapshot: dict[str, Any],
                      learning: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    base = state["baseline"]["services"]
    rollouts = state.get("rollouts") or {}
    now = snapshot["taken_at"]
    learning = learning or set()
    for key, s in snapshot["services"].items():
        b = base.get(key)
        if not b or not s["selector"] or f"Service/{key}" in learning or not b.get("polls", 1):
            continue
        b["max_ready"] = max(b["max_ready"], s["ready_endpoints"])  # it may scale up for good
        subject = f"Service/{key}"
        selected = [wk for wk, w in snapshot["workloads"].items()
                    if w["namespace"] == s["namespace"] and labels_match(s["selector"], w["pod_labels"])]
        # Scaled to zero on purpose, or a progressing rollout whose strategy may take every
        # pod down at once (Recreate): not an outage yet. Other rollouts keep pods serving.
        scaled_to_zero = bool(selected) and all(snapshot["workloads"][wk]["desired"] == 0 for wk in selected)

        def _may_have_none(wk: str) -> bool:
            w = snapshot["workloads"][wk]
            deadline = w.get("progress_deadline") or state["config"]["rollout_stuck_seconds"]
            return (wk in rollouts and now - rollouts[wk]["since"] < deadline
                    and w.get("max_unavailable", w["desired"]) >= w["desired"])

        rolling = bool(selected) and all(_may_have_none(wk) for wk in selected
                                         if snapshot["workloads"][wk]["desired"] > 0)
        if b["max_ready"] > 0 and s["ready_endpoints"] == 0 and not scaled_to_zero and not rolling:
            out.append(_symptom(subject, "no_endpoints", "critical",
                                f"service/{s['name']} has no ready endpoints (it had {b['max_ready']})",
                                [f"selector {s['selector']} matches no ready pod",
                                 f"baseline backends: {', '.join(b['backends']) or 'none'}"],
                                traffic=True))
        new_backends = sorted(set(s["backends"]) - set(b["backends"]))
        if b["backends"] and new_backends:
            out.append(_symptom(subject, "new_backend", "medium",
                                f"service/{s['name']} now also routes to {', '.join(new_backends)}",
                                [f"baseline backends: {', '.join(b['backends'])}",
                                 f"now: {', '.join(s['backends'])}"]))
    return out


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def dependencies(workload: dict[str, Any], snapshot: dict[str, Any]) -> list[str]:
    """Services this workload's env names as a host (REDIS_HOST=redis -> ns/redis)."""
    ns = workload["namespace"]
    by_name = {s["name"]: key for key, s in snapshot["services"].items() if s["namespace"] == ns}
    deps = set()
    for group in ("containers", "initContainers"):
        for c in (workload["template"].get(group) or {}).values():
            for value in (c.get("env") or {}).values():
                host = re.sub(r"^[a-z]+://", "", str(value))
                host = re.split(r"[:/]", host)[0].split(".")[0]
                if host in by_name:
                    deps.add(by_name[host])
    return sorted(deps)


def reapplies_only(change: dict[str, Any]) -> bool:
    """A rollout restart or a replaced pod: it changes nothing itself, the new pods run
    what the template and its ConfigMaps already say. So a content change it applied
    (a ConfigMap edited, then the workload restarted) is the better suspect."""
    if change.get("action") == "pod_replaced":
        return True
    details = change.get("details") or []
    return (change.get("action") == "changed" and bool(details)
            and all(d.startswith("rollout restart requested") for d in details))


def _scale_only(change: dict[str, Any]) -> bool:
    """A change of replica count and nothing else."""
    details = change.get("details") or []
    return bool(details) and all(d.startswith("replicas ") for d in details)


def _relation(finding: dict[str, Any], change: dict[str, Any], snapshot: dict[str, Any]) -> tuple[int, str]:
    """How strongly a change relates to a finding's subject: (weight, why)."""
    subject = finding["subject"]
    if change["key"] == subject:
        if reapplies_only(change):
            return 4, "same object (a restart)"
        return 5, "same object"
    kind, ns, name = subject.split("/", 2)
    if change["namespace"] != ns:
        return 0, ""
    is_workload_change = change["kind"] in ("Deployment", "StatefulSet", "DaemonSet")
    if kind == "Service":
        svc = snapshot["services"].get(f"{ns}/{name}") or {}
        if is_workload_change and labels_match(svc.get("selector"), change.get("pod_labels")):
            return 4, "workload behind this service"
        # Workloads the selector matches (the Service may have no endpoints right now).
        for w in snapshot["workloads"].values():
            if (w["namespace"] == ns and labels_match(svc.get("selector"), w["pod_labels"])
                    and change["name"] in (w.get("references") or {}).get(change["kind"], [])):
                return 4, f"{change['kind'].lower()} used by {w['name']}, behind this service"
        return 1, "same namespace"

    workload = snapshot["workloads"].get(subject)
    if not workload:
        return 1, "same namespace"
    refs = workload.get("references") or {}
    if change["name"] in refs.get(change["kind"], []):
        return 4, f"{change['kind'].lower()} used by {workload['name']}"
    if change["kind"] == "Service" and labels_match(change.get("selector"), workload["pod_labels"]):
        return 4, f"service routing to {workload['name']}"
    if change["kind"] == "NetworkPolicy" and labels_match((change.get("detail") or {}).get("podSelector"), workload["pod_labels"]):
        return 4, f"network policy selecting {workload['name']} pods"
    if change["kind"] in ("ResourceQuota", "LimitRange"):
        quota_hit = any("quota" in e.lower() or "forbidden" in e.lower() for e in finding.get("evidence", []))
        return (4 if quota_hit else 2), "namespace quota/limits"
    for dep in dependencies(workload, snapshot):
        svc = snapshot["services"].get(dep) or {}
        if change["key"] == f"Service/{dep}":
            return 3, f"service {svc.get('name')}, which {workload['name']} depends on"
        if is_workload_change and labels_match(svc.get("selector"), change.get("pod_labels")):
            return 3, f"{change['name']}, behind service {svc.get('name')} that {workload['name']} depends on"
        backends = [snapshot["workloads"].get(b) or {} for b in svc.get("backends") or []]
        if change["kind"] == "NetworkPolicy" and any(
                labels_match((change.get("detail") or {}).get("podSelector"), bw.get("pod_labels")) for bw in backends):
            return 3, f"network policy on {svc.get('name')}, which {workload['name']} depends on"
        for bw in backends:
            if change["name"] in (bw.get("references") or {}).get(change["kind"], []):
                return 3, f"{change['kind'].lower()} used by {bw.get('name')}, which {workload['name']} depends on"
    if finding.get("traffic"):
        if change["kind"] in ("Service", "NetworkPolicy"):
            return 3, "traffic path in the same namespace"
        if is_workload_change and any(
                labels_match(s["selector"], change.get("pod_labels"))
                for s in snapshot["services"].values() if s["namespace"] == ns):
            return 2, "a workload serving traffic in the same namespace"
    return 1, "same namespace"


def _users_of(change: dict[str, Any], snapshot: dict[str, Any]) -> set[str]:
    """Workloads whose pod template references the changed ConfigMap or Secret."""
    return {key for key, w in snapshot["workloads"].items()
            if w["namespace"] == change["namespace"]
            and change["name"] in (w.get("references") or {}).get(change["kind"], [])}


def _workloads_of(finding: dict[str, Any], snapshot: dict[str, Any]) -> set[str]:
    """The workloads a finding is about: its own, or the ones behind its Service."""
    subject = finding["subject"]
    if subject in snapshot["workloads"]:
        return {subject}
    kind, ns, name = subject.split("/", 2)
    if kind != "Service":
        return set()
    selector = (snapshot["services"].get(f"{ns}/{name}") or {}).get("selector")
    return {key for key, w in snapshot["workloads"].items()
            if w["namespace"] == ns and labels_match(selector, w["pod_labels"])}


def attribute(finding: dict[str, Any], changes: list[dict[str, Any]], snapshot: dict[str, Any],
              config: dict[str, Any]) -> list[dict[str, Any]]:
    """Up to three changes most likely to have caused ``finding``, best first."""
    ranked = []
    evidence = " ".join(finding.get("evidence") or [])
    reapplies = [c for c in changes if reapplies_only(c)]
    for change in changes:
        gap = finding["first_seen"] - change["at"]
        # Writes up to two minutes "after" the first symptom are still candidates:
        # the symptom can be confirmed before managedFields catches up, and clocks skew.
        if finding["first_seen"] - _recorded_at(change) > config["change_window_seconds"] or gap < -120:
            continue
        weight, why = _relation(finding, change, snapshot)
        if weight <= 0:
            continue
        # Pods read a ConfigMap when they start: an edit that a later restart (or
        # replaced pod) of a workload using it put into effect took effect then, so it
        # is as recent as that restart and ranks above it.
        took_effect = change["at"]
        if change["kind"] in ("ConfigMap", "Secret"):
            # The restart that matters is the failing workload's own (the latest one before
            # the symptom), not the first restart of any workload sharing the ConfigMap.
            users = _users_of(change, snapshot) & _workloads_of(finding, snapshot)
            applied = [r["at"] for r in reapplies
                       if r["key"] in users and change["at"] <= r["at"] <= finding["first_seen"] + 120]
            if applied:
                took_effect = max(applied)
        since = finding["first_seen"] - took_effect
        recency = 2 if since <= 180 else 1 if since <= 900 else 0
        seconds_before: int | None = round(gap)
        if change.get("during_gap"):
            # Made sometime during a polling gap: when exactly is unknown, so it is neither
            # recent nor old, and no time before the symptom is claimed for it.
            recency, seconds_before = 1, None
            why += ", made during a polling gap"
        # The error names the object ("exceeded quota: compute-resources"). Not for
        # workloads: their names are in every pod name.
        named = (change["kind"] in _NAMEABLE_KINDS and len(change["name"]) >= 4
                 and re.search(rf"(?<![\w-]){re.escape(change['name'])}(?![\w-])", evidence) is not None)
        if named:
            why += ", named in the error"
        ranked.append({**change, "score": weight + recency + (2 if named else 0), "relation": why,
                       "seconds_before": seconds_before})
    # On a tie, what the pods picked up ranks above the restart that made them pick it up.
    ranked.sort(key=lambda r: (-r["score"], reapplies_only(r), abs(r["seconds_before"] or 0)))
    return ranked[:3]


# ---------------------------------------------------------------------------
# Step
# ---------------------------------------------------------------------------

def step(state: dict[str, Any], snapshot: dict[str, Any], logs: dict[str, list[str]] | None = None
         ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Advance the detector by one poll.

    ``logs`` maps a workload key to the raw log lines it produced since the
    previous poll. A workload missing from ``logs`` was not fetched this poll,
    which is different from a workload that was fetched and logged nothing.

    Returns the updated state and the findings confirmed *this poll*. A finding
    is reported once; later polls extend it (new symptoms, escalation) or close
    it after ``resolve_polls`` clean polls.
    """
    if current_state(state) is not state:  # another version or digest key: learn again, in place
        fresh = new_state()
        state.clear()
        state.update(fresh)
    cfg = state["config"]
    first_poll = state["prev"] is None
    if not first_poll and snapshot["taken_at"] - state["prev"]["taken_at"] > cfg.get("max_gap_seconds", 600):
        _rebaseline_after_gap(state, snapshot)
        return state, []
    # Lines from before the first poll are history, not behaviour to learn.
    templates = {} if first_poll else {w: template_counts(lines) for w, lines in (logs or {}).items()}
    events_readable = _carry_forward_blind(state, snapshot)
    events = _event_deltas(state, snapshot) if events_readable else []
    now = snapshot["taken_at"]

    if not first_poll:
        cutoff = now - cfg["change_window_seconds"]
        changes = detect_changes(state["prev"], snapshot) + state["changes"]
        state["changes"] = _bounded_changes([c for c in changes if _recorded_at(c) >= cutoff][: cfg["max_changes"]])

    if is_learning(state):
        _learn(state, snapshot, events, templates)
        _record_capacity(state, snapshot)
        _forget_deleted(state, snapshot)
        state["prev"] = _for_next_poll(snapshot)
        state["polls"] += 1
        return state, []

    learning = _still_learning(state, snapshot)
    log_learning = _learning_logs(state, snapshot, templates)
    symptoms = _hold_bursty(state, _workload_symptoms(state, snapshot, events, templates, learning, log_learning)
                            + _service_symptoms(state, snapshot, learning), now)
    _record_capacity(state, snapshot)
    if learning:
        _learn(state, snapshot, events, templates, only=learning)
    for workload in log_learning:
        _learn_logs(state, workload, templates[workload], errors=True)

    active = set()
    for sym in symptoms:
        keys = [f"{sym['subject']}|{sym['kind']}"] + [f"{sym['subject']}|log|{fp}" for fp in sym.get("fingerprints", [])]
        for k in keys:
            active.add(k)
            # A held symptom keeps its count but does not add to it: confirmation
            # needs the signal seen again, not remembered.
            if not sym.get("held"):
                state["pending"][k] = state["pending"].get(k, 0) + 1

    def _still_pending(k: str) -> bool:
        # A log signal of a workload whose logs were not read this poll is
        # unknown, not absent: keep its count instead of resetting it.
        subject, _, kind = k.partition("|")
        return k in active or (kind.split("|")[0] in _LOG_KINDS and subject not in templates)

    state["pending"] = {k: v for k, v in state["pending"].items() if _still_pending(k)}

    by_subject: dict[str, list[dict[str, Any]]] = {}
    for sym in symptoms:
        if state["pending"].get(f"{sym['subject']}|{sym['kind']}", 0) < cfg["confirm_polls"]:
            continue
        # A held (not re-seen) symptom keeps an open finding alive; it never opens one.
        if sym.get("held") and sym["subject"] not in state["open"]:
            continue
        by_subject.setdefault(sym["subject"], []).append(sym)

    confirmed: list[dict[str, Any]] = []
    for subject, syms in by_subject.items():
        syms.sort(key=lambda s: -SEVERITY_RANK[s["severity"]])
        top = syms[0]
        traffic = any(s.get("traffic") for s in syms)
        existing = state["open"].get(subject)
        if existing:
            existing["last_seen"] = now
            existing["clean_polls"] = 0
            known_kinds = {s["kind"] for s in existing["symptoms"]}
            new_syms = [s for s in syms if s["kind"] not in known_kinds]
            existing["symptoms"] += new_syms
            existing["evidence"] = [e for s in existing["symptoms"] for e in s["evidence"]][:12]
            existing["traffic"] = existing.get("traffic") or traffic
            was, incident_rank = SEVERITY_RANK[existing["severity"]], SEVERITY_RANK[INCIDENT_MIN_SEVERITY]
            rose = SEVERITY_RANK[top["severity"]] > was
            if rose:
                existing.update(severity=top["severity"], summary=top["summary"], escalated_at=now)
            if was < incident_rank <= SEVERITY_RANK[existing["severity"]]:
                # A notice that became an outage is reported again, now as incident-worthy,
                # and blamed as of now: the change that turned it into an outage may be
                # newer than the notice. It never had an incident, so it gets a new key.
                existing["suspects"] = attribute({**existing, "first_seen": now}, state["changes"], snapshot, cfg)
                existing["blamed_at"] = now
                existing.pop("incident_key", None)
                confirmed.append(existing)
            elif was >= incident_rank and (rose or any(SEVERITY_RANK[s["severity"]] >= incident_rank
                                                       for s in new_syms)):
                # An outage that got worse, or gained an incident-worthy symptom. If a change
                # found since it was last blamed is the cause now, that is a new incident
                # blaming that change; otherwise it is more of the same outage (a second
                # symptom of one crash loop confirms a poll later) and stays on its
                # incident, which is sent the update. Re-ranking the changes it was already
                # blamed among only moves them between recency buckets: never a newer cause.
                fresh = attribute({**existing, "first_seen": now}, state["changes"], snapshot, cfg)
                old, new = primary_suspect(existing), primary_suspect({"suspects": fresh})
                # A newer cause must rank at least as high as the old one did when it was
                # blamed (not as the old one decays with time), and is never routine: a
                # restart, a replaced pod, or a scale (an HPA answering the outage).
                if (new is not None and not reapplies_only(new) and not _scale_only(new)
                        and new.get("detected_at", new["at"]) > existing.get("blamed_at", existing["first_seen"])
                        and (old is None or ((new["key"], new["at"]) != (old["key"], old["at"])
                                             and new["score"] >= old["score"]))):
                    existing["suspects"] = fresh
                    existing["blamed_at"] = now
                    existing.pop("incident_key", None)
                    confirmed.append(existing)
                existing.pop("incident_id", None)  # so its incident is opened or updated again
            continue
        kind, ns, name = subject.split("/", 2)
        finding = {
            "id": _key(subject, now), "subject": subject, "subject_kind": kind, "namespace": ns, "name": name,
            "severity": top["severity"], "summary": top["summary"], "symptoms": syms, "traffic": traffic,
            "evidence": [e for s in syms for e in s["evidence"]][:12],
            "first_seen": now, "last_seen": now, "clean_polls": 0,
        }
        finding["suspects"] = attribute(finding, state["changes"], snapshot, cfg)
        state["open"][subject] = finding
        confirmed.append(finding)

    # Incident keys once every new finding of this poll exists, so how findings
    # are grouped does not depend on the order they were confirmed in.
    _assign_incident_keys(state["open"], snapshot)

    # Non-error lines are always learned. An open finding's error lines stay
    # "novel" while it is open (that is what keeps it open) and join the
    # baseline once it resolves, so the same error coming back slowly does not
    # page again; a surge of it still can.
    for workload, counts in templates.items():
        if workload not in learning and workload not in log_learning:  # those learned theirs above
            _absorb_templates(state, workload, {fp: e for fp, e in counts.items() if not e["error"]})
        finding = state["open"].get(workload)
        if finding:
            finding["log_polls_open"] = finding.get("log_polls_open", 0) + 1
            held = finding.setdefault("log_templates", {})
            for fp, e in counts.items():
                if not e["error"]:
                    continue
                if fp not in held and len(held) >= _MAX_HELD_TEMPLATES:
                    finding["log_templates_dropped"] = finding.get("log_templates_dropped", 0) + 1
                    continue
                entry = held.get(fp) or held.setdefault(fp, {**_kept(e), "count": 0})
                entry["count"] += e["count"]

    for subject in list(state["open"]):
        if subject in by_subject:
            continue
        finding = state["open"][subject]
        finding["clean_polls"] = finding.get("clean_polls", 0) + 1
        if finding["clean_polls"] >= cfg["resolve_polls"]:
            _absorb_resolved(state, subject, finding)
            summary = {k: finding.get(k) for k in ("id", "subject", "severity", "summary", "first_seen",
                                                    "incident_key", "incident_id")}
            state["closed"] = (state["closed"] + [{**summary, "resolved_at": now}])[-cfg["max_closed"]:]
            del state["open"][subject]

    _forget_deleted(state, snapshot)
    state["prev"] = _for_next_poll(snapshot)
    state["polls"] += 1
    return state, confirmed


_MAX_CHANGES_BYTES = 1024 * 1024


def _recorded_at(change: dict[str, Any]) -> float:
    """When a change counts from: when it happened, or, for one made during a polling
    gap (dated to the gap's start; when it really happened is unknown), when it was
    found, so a gap longer than the change window does not lose it."""
    return change.get("detected_at", change["at"]) if change.get("during_gap") else change["at"]


def _bounded_changes(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The newest changes that fit in a bounded share of the stored state."""
    kept, size = [], 0
    for change in changes:  # newest first
        size += len(json.dumps(change, default=str))
        if size > _MAX_CHANGES_BYTES:
            break
        kept.append(change)
    return kept


def _rebaseline_after_gap(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Polling stopped for longer than the windows measure (Watch turned off, a worker
    backlog, an unreachable cluster). Restarts, Warning events and log lines that
    piled up meanwhile cannot be placed in time, so every window starts over and this
    poll becomes the previous one. Changes are still recorded (a deploy made during
    the gap may be what breaks next), dated to the gap; replaced pods are not, since a
    long gap replaces pods for reasons nobody needs to hear about. The learned
    baseline and the open findings are kept."""
    cfg = state["config"]
    _carry_forward_blind(state, snapshot)
    cutoff = snapshot["taken_at"] - cfg["change_window_seconds"]
    gap_changes = [{**c, "during_gap": True} for c in detect_changes(state["prev"], snapshot)
                   if c["action"] != "pod_replaced"]
    state["changes"] = _bounded_changes([c for c in gap_changes + state["changes"]
                                         if _recorded_at(c) >= cutoff][: cfg["max_changes"]])
    _event_deltas(state, snapshot)  # the gap's event counts become the counts seen
    for table in ("restarts", "held", "rollouts", "pending"):
        state[table] = {}
    _record_capacity(state, snapshot)
    _forget_deleted(state, snapshot)
    state["prev"] = _for_next_poll(snapshot)
    state["polls"] += 1


def _carry_forward_blind(state: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Treat kinds this poll could not read as unchanged since the last poll.

    Otherwise one unreadable ConfigMap list would read as every ConfigMap
    deleted (and re-created on the next poll), and those fake changes would be
    blamed for the next hour. Returns whether events were readable.
    """
    blind = set(snapshot.get("blind") or [])
    prev = state["prev"]
    if blind and prev is not None:
        kinds = {TRACKED_OBJECTS[r] for r in blind if r in TRACKED_OBJECTS}
        for key, obj in (prev.get("objects") or {}).items():
            if obj["kind"] in kinds:
                snapshot["objects"].setdefault(key, obj)
    return "events" not in blind


_HELD_KINDS = frozenset({"warning_events", "new_error_logs", "error_log_surge", "success_stopped"})


def _hold_bursty(state: dict[str, Any], symptoms: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """Keep bursty symptoms active for ``hold_seconds`` after they were last seen."""
    held = state.setdefault("held", {})
    fresh = set()
    for sym in symptoms:
        key = f"{sym['subject']}|{sym['kind']}"
        fresh.add(key)
        if sym["kind"] in _HELD_KINDS:
            held[key] = {"until": now + state["config"]["hold_seconds"], "symptom": sym}
    out = list(symptoms)
    for key, h in list(held.items()):
        if h["until"] < now:
            del held[key]
        elif key not in fresh:
            out.append({**h["symptom"], "held": True})
    return out


def _forget_deleted(state: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Drop what was learned about objects that no longer exist, so state tracks
    the cluster's size rather than its history (preview environments come and go)."""
    workloads, services = snapshot["workloads"], snapshot["services"]
    base = state["baseline"]
    for table in ("workloads", "templates", "log_polls", "success_polls"):
        for key in [k for k in base[table] if k not in workloads]:
            del base[table][key]
    for key in [k for k in base["services"] if k not in services]:
        del base["services"][key]
    for key in [k for k in base["event_polls"] if k.split("|")[0] not in workloads]:
        del base["event_polls"][key]
    for table in ("restarts", "capacity", "rollouts", "held_updated"):
        for key in [k for k in state.get(table, {}) if k not in workloads]:
            del state[table][key]
    for key in [k for k in state.get("log_cursors", {}) if k not in workloads]:
        del state["log_cursors"][key]
    subjects = set(workloads) | {f"Service/{k}" for k in services}
    for table in ("pending", "held"):  # "<subject>|<kind>[|<fingerprint>]"
        for key in [k for k in state.get(table, {}) if k.split("|", 1)[0] not in subjects]:
            del state[table][key]


def _for_next_poll(snapshot: dict[str, Any]) -> dict[str, Any]:
    """What the next poll needs of this snapshot. Events are tracked in seen_events."""
    return {**snapshot, "events": []}


def primary_suspect(finding: dict[str, Any]) -> dict[str, Any] | None:
    """The suspect worth naming: closely related to the finding, not just nearby in time."""
    suspects = finding.get("suspects") or []
    return suspects[0] if suspects and suspects[0]["score"] >= 4 else None


def related_objects(a: str, b: str, snapshot: dict[str, Any]) -> bool:
    """Same object, a workload and a ConfigMap/Secret/PVC it uses, or a Service and a workload it selects."""
    if a == b:
        return True
    for x, y in ((a, b), (b, a)):
        workload = snapshot["workloads"].get(x)
        if not workload:
            continue
        ykind, yns, yname = y.split("/", 2)
        if yname in (workload.get("references") or {}).get(ykind, []):
            return True
        if ykind == "Service":
            svc = snapshot["services"].get(f"{yns}/{yname}") or {}
            if labels_match(svc.get("selector"), workload["pod_labels"]):
                return True
    return False


def _upstream_finding(finding: dict[str, Any], open_findings: dict[str, Any],
                      snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """An open finding on a Service this workload depends on, or on a workload behind it."""
    workload = snapshot["workloads"].get(finding["subject"])
    if not workload:
        return None
    for dep in dependencies(workload, snapshot):
        svc = snapshot["services"].get(dep) or {}
        candidates = [f"Service/{dep}"] + [
            key for key, w in snapshot["workloads"].items()
            if w["namespace"] == svc.get("namespace") and labels_match(svc.get("selector"), w["pod_labels"])
        ]
        for key in candidates:
            other = open_findings.get(key)
            if other is not None and other is not finding:
                return other
    return None


def _same_incident(a: dict[str, Any], b: dict[str, Any], open_findings: dict[str, Any],
                   snapshot: dict[str, Any]) -> bool:
    """Whether two open findings are one incident.

    One is downstream of the other (a workload failing while a Service it depends
    on is down), or both blame the same change or changes to closely related
    objects (one deploy often touches a Deployment and the ConfigMap it mounts),
    or neither has a suspect and they started in the same namespace minutes apart.
    Two unrelated changes made in the same minute stay two incidents.
    """
    if a["namespace"] != b["namespace"]:
        return False
    if _upstream_finding(a, open_findings, snapshot) is b or _upstream_finding(b, open_findings, snapshot) is a:
        return True
    pa, pb = primary_suspect(a), primary_suspect(b)
    if pa and pb:
        return related_objects(pa["key"], pb["key"], snapshot)
    if not pa and not pb:
        return abs(a["first_seen"] - b["first_seen"]) <= _UNATTRIBUTED_JOIN_SECONDS
    return False


def _assign_incident_keys(open_findings: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """Give every open finding without one an incident key, by connected component.

    A component that already has an incident keeps its key (the oldest one);
    otherwise the key comes from its upstream-most, most severe finding.
    """
    nodes = list(open_findings.values())
    parent = list(range(len(nodes)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            if _same_incident(nodes[i], nodes[j], open_findings, snapshot):
                parent[root(j)] = root(i)
    components: dict[int, list[dict[str, Any]]] = {}
    for i, f in enumerate(nodes):
        components.setdefault(root(i), []).append(f)
    for members in components.values():
        unkeyed = [f for f in members if not f.get("incident_key")]
        if not unkeyed:
            continue
        keyed = sorted((f for f in members if f.get("incident_key")), key=lambda f: f["first_seen"])
        if keyed:
            key = keyed[0]["incident_key"]
        else:
            heads = [f for f in members if _upstream_finding(f, open_findings, snapshot) is None] or members
            head = sorted(heads, key=lambda f: (-SEVERITY_RANK[f["severity"]], f["first_seen"], f["subject"]))[0]
            mine = primary_suspect(head)
            key = (f"{head['namespace']}|{mine['key']}|{int(mine['at'])}" if mine
                   else f"{head['namespace']}|unattributed|{int(head['first_seen'])}")
        for f in unkeyed:
            f["incident_key"] = key


def group_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group findings by incident: the symptoms of one change (a broken Service and
    the clients timing out on it) become one incident, two changes stay two."""
    groups: dict[str, dict[str, Any]] = {}
    for f in findings:
        key = f.get("incident_key") or f"{f['namespace']}|unattributed|{int(f['first_seen'])}"
        group = groups.setdefault(key, {"key": key, "namespace": f["namespace"],
                                        "primary_suspect": primary_suspect(f), "findings": []})
        if group["primary_suspect"] is None:
            group["primary_suspect"] = primary_suspect(f)
        group["findings"].append(f)
    out = []
    for group in groups.values():
        group["findings"].sort(key=lambda f: -SEVERITY_RANK[f["severity"]])
        group["severity"] = group["findings"][0]["severity"]
        group["title"] = group["findings"][0]["summary"]
        group["opens_incident"] = SEVERITY_RANK[group["severity"]] >= SEVERITY_RANK[INCIDENT_MIN_SEVERITY]
        out.append(group)
    out.sort(key=lambda g: -SEVERITY_RANK[g["severity"]])
    return out
