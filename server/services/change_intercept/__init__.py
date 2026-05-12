"""Change-Intercept — PR-time risk review pipeline.

Phase 1a of the change-intercept system. Reviews staged PRs for
production-deployment risk (memory leaks, missing timeouts, unbounded
retries, dangerous config edits, unsafe migrations, etc.) and posts a
single GitHub PR Review combining a short top-level summary with up to
three inline per-hunk comments on HIGH-severity, HIGH-confidence findings.

The package is split into three layers:

    services.change_intercept.adapters.base
        Vendor-neutral dataclasses + ``ChangeAdapter`` protocol. Every
        new git host (GitHub today, GitLab/Bitbucket later) implements
        the protocol; the rest of the pipeline never special-cases by
        vendor.

    services.change_intercept.adapters.github
        The only adapter implemented in Phase 1a. Wraps the existing
        Aurora GitHub App auth + webhook infrastructure (JWT signing,
        installation tokens, HMAC verification) rather than duplicating
        any of it.

    services.change_intercept.adapters.registry
        Lookup table mapping vendor strings to adapter instances.
        ``get_adapter("github")`` is the only call site needed by the
        dispatcher.

Two pure-helper modules sit alongside:

    services.change_intercept.risk_taxonomy
        The fixed 12-category taxonomy the investigator is steered to.
        Findings whose category is not in the taxonomy are dropped by
        the validator (Part 2).

    services.change_intercept.diff_parser
        Parses unified diffs into a ``{path -> hunks}`` index so the
        validator (Part 2) can verify each finding actually points at a
        line in the staged change.

This module exports nothing at package level — callers import the
sub-modules they need directly. That keeps the import graph tight and
avoids circular imports between the adapter and the registry.
"""
