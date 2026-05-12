"""Fixed taxonomy of production-deployment risks Aurora flags on PRs.

This module is the single source of truth for the risk categories the
investigator is allowed to emit findings under. Anything not in this
list is by definition outside Phase 1a's scope — the validator drops
findings whose ``category`` is unknown, the prompt builder injects the
descriptions into the investigator prompt, and the review-poster uses
the human-readable labels in the PR review body.

The taxonomy is intentionally short (12 categories) so the
investigator stays focused on what we promise the customer: "if it
wouldn't show up in a postmortem, we don't comment on it."

Adding a category requires:
    1. New entry in ``_CATEGORIES`` (slug + label + description + at
       least one positive example and one anti-example).
    2. Refreshing the calibration corpus — adding a category mid-flight
       skews the severity distribution and breaks comparability with
       prior dry-runs.
    3. Updating the prompt builder's frozen example set so the
       investigator sees the new category in-context.

The slug strings are stable contracts; renaming a category requires a
data migration on ``change_investigations.findings``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RiskCategory:
    """A single production-deployment risk category.

    Attributes:
        slug: stable string used in ``Finding.category`` and persisted
            to JSONB. Lowercase snake_case.
        label: human-readable label rendered in PR review bodies.
        description: one-sentence definition used in the investigator
            prompt to anchor what counts vs. doesn't.
        examples: 1-3 short, concrete positive examples (the kind of
            change that legitimately falls under this category).
        anti_examples: 1-2 short, concrete negative examples (changes
            that look superficially similar but are NOT what we want
            to flag — these go into the prompt as "do NOT flag" lines).
    """

    slug: str
    label: str
    description: str
    examples: tuple[str, ...]
    anti_examples: tuple[str, ...] = ()


_CATEGORIES: tuple[RiskCategory, ...] = (
    RiskCategory(
        slug="memory_leak",
        label="Memory leak",
        description=(
            "Code change that causes unbounded memory growth over the "
            "lifetime of a long-running process."
        ),
        examples=(
            "An in-memory cache or list that grows without eviction",
            "An event listener / subscription registered but never removed",
            "A goroutine / asyncio task spawned in a loop with no shutdown path",
            "A file handle / socket opened but not closed on the error path",
        ),
        anti_examples=(
            "A short-lived script that accumulates state then exits",
            "A test fixture that intentionally builds a large in-memory object",
        ),
    ),
    RiskCategory(
        slug="unbounded_retry",
        label="Unbounded retry",
        description=(
            "Retry logic that lacks a max-attempts cap, lacks backoff, "
            "or retries operations that are not safely idempotent."
        ),
        examples=(
            "A while-True retry loop with no break / max-attempts",
            "Retry without exponential backoff on a downstream that may rate-limit",
            "Retry on a POST/PUT that mutates state and isn't idempotent",
        ),
        anti_examples=(
            "A library default retry (e.g. urllib3) with reasonable caps already in place",
        ),
    ),
    RiskCategory(
        slug="missing_timeout",
        label="Missing timeout",
        description=(
            "Outbound call, lock acquisition, or DB query without an "
            "explicit timeout / deadline."
        ),
        examples=(
            "`requests.get(url)` (no `timeout=` kwarg) — hangs forever if peer is slow",
            "DB query without statement_timeout on a hot path",
            "RPC stub created without a per-call deadline",
            "`Lock.acquire()` without a timeout argument",
        ),
        anti_examples=(
            "A CLI script or local dev tool where blocking is acceptable",
        ),
    ),
    RiskCategory(
        slug="blocking_in_hot_path",
        label="Blocking call in hot path",
        description=(
            "Synchronous I/O / CPU-bound work introduced into an async "
            "handler, event loop, or request thread."
        ),
        examples=(
            "`time.sleep()` in an asyncio coroutine",
            "Synchronous file read inside an async request handler",
            "CPU-heavy work in a Flask request without offloading to Celery",
        ),
        anti_examples=(
            "Blocking I/O inside a background worker designed for it",
        ),
    ),
    RiskCategory(
        slug="concurrency",
        label="Concurrency / race condition",
        description=(
            "Shared mutable state accessed without synchronisation, or "
            "an ordering assumption that doesn't hold under contention."
        ),
        examples=(
            "Module-level dict mutated by multiple threads without a lock",
            "Cache-fill that races (two readers both mint the same expensive value)",
            "Read-modify-write across separate transactions without SELECT FOR UPDATE",
        ),
        anti_examples=(
            "Single-threaded code where concurrency is structurally impossible",
        ),
    ),
    RiskCategory(
        slug="n_plus_one",
        label="N+1 query",
        description=(
            "Loop that triggers one DB query per iteration instead of "
            "a single batched query."
        ),
        examples=(
            "ORM lazy-load triggered inside a list iteration",
            "`for user in users: cur.execute('SELECT … WHERE id=%s', user.id)`",
        ),
        anti_examples=(
            "Loops that fan out to a small (≤3) bounded set",
            "Explicit batched chunking already in place",
        ),
    ),
    RiskCategory(
        slug="dangerous_config",
        label="Dangerous config change",
        description=(
            "Configuration edit that materially weakens reliability or "
            "capacity in production."
        ),
        examples=(
            "Timeout reduction on a downstream that occasionally exceeds the new value",
            "Replica count decrease without justification",
            "Kubernetes resource-limit drop (memory / CPU / connections)",
            "Health-check threshold tightening that could trigger restart storms",
            "Worker / thread pool shrink",
        ),
        anti_examples=(
            "Documentation-only YAML edits (no live impact)",
            "Config additions that are gated behind a flag and not yet enabled",
        ),
    ),
    RiskCategory(
        slug="unsafe_migration",
        label="Unsafe migration",
        description=(
            "Schema or data migration that is not backward-compatible "
            "with the running version, or that locks a hot table."
        ),
        examples=(
            "Dropping a column still referenced by the deployed code",
            "Renaming a column without a two-phase rollout",
            "Type change that requires a full table rewrite under a lock",
            "Irreversible data backfill without a staging step",
        ),
        anti_examples=(
            "Adding a nullable column (safe, online)",
            "Creating a new table that no deployed code references yet",
        ),
    ),
    RiskCategory(
        slug="secret_handling",
        label="Secret handling regression",
        description=(
            "Change that exposes secrets, weakens credential isolation, "
            "or removes guards around sensitive values."
        ),
        examples=(
            "Logging a token / API key / password at any level",
            "Adding a secret to a hardcoded string literal",
            "Removing redaction from an error path",
            "Reading a credential into a shared env var (per-process leak)",
        ),
        anti_examples=(
            "Adding redaction to a path that previously logged sensitive data",
        ),
    ),
    RiskCategory(
        slug="error_swallowing",
        label="Error swallowing on critical path",
        description=(
            "Exception caught and dropped on a path where the caller "
            "needs to know the operation failed."
        ),
        examples=(
            "`except: pass` wrapping a DB write or external API call",
            "`catch (e) {}` after a network call whose failure invalidates downstream state",
            "Retry-and-ignore that masks a permanent failure",
        ),
        anti_examples=(
            "Best-effort metric / log emission where failure is non-critical",
            "Explicit fall-back path that logs the swallow",
        ),
    ),
    RiskCategory(
        slug="breaking_api_change",
        label="Breaking API change",
        description=(
            "Change to a response shape, status code, or request "
            "contract that would break a current consumer."
        ),
        examples=(
            "Removing or renaming a response field",
            "Changing a 200 response to 204 (or vice versa)",
            "Tightening request validation in a way that rejects today's inputs",
            "Renaming an internal RPC method without versioning",
        ),
        anti_examples=(
            "Additive changes (new optional field, new endpoint)",
            "Changes to a contract that is not yet consumed by anyone",
        ),
    ),
    RiskCategory(
        slug="dependency_risk",
        label="Dependency risk",
        description=(
            "Dependency change that introduces supply-chain or "
            "compatibility risk to production."
        ),
        examples=(
            "Major-version bump of a dependency on the request hot path",
            "Adding a new transitive dependency from an unfamiliar publisher",
            "Removing a dep that other code still imports (build/runtime break)",
        ),
        anti_examples=(
            "Patch-version bump of a well-known dependency",
            "Lockfile-only refresh with no semantic changes",
        ),
    ),
)


# Map keyed by slug for O(1) lookups. Built once at import.
CATEGORIES_BY_SLUG: dict[str, RiskCategory] = {c.slug: c for c in _CATEGORIES}


CATEGORIES: tuple[str, ...] = tuple(c.slug for c in _CATEGORIES)
"""Stable tuple of category slugs. Used by the validator to reject
findings whose ``category`` isn't in the taxonomy."""


def get_category(slug: str) -> RiskCategory | None:
    """Return the ``RiskCategory`` for ``slug``, or ``None`` if unknown.

    The validator uses this to fail-closed on unknown categories. The
    prompt builder uses ``CATEGORIES_BY_SLUG`` directly to enumerate
    every category for the investigator prompt.
    """
    return CATEGORIES_BY_SLUG.get(slug)


def all_categories() -> tuple[RiskCategory, ...]:
    """Return the immutable tuple of all registered categories.

    Used by the prompt builder to inject the taxonomy block into the
    investigator prompt and by the calibration report to enumerate
    per-category severity distributions.
    """
    return _CATEGORIES
