"""Vendor-neutral dataclasses + ``ChangeAdapter`` protocol.

Every git-host adapter (GitHub today, GitLab/Bitbucket later) implements
the six-method ``ChangeAdapter`` protocol. The rest of the pipeline —
dispatcher, Celery tasks, investigator, validator, review poster — works
exclusively against these dataclasses, so adding a new vendor never
requires changes to the core.

Method coverage by phase (Phase 1a):

    Part 1 (plumbing, this commit):
        - ``verify_signature``        — usually delegated to existing util
        - ``parse``                   — webhook → NormalizedChangeEvent
        - ``fetch_snapshot``          — one-shot diff/files/commits fetch
        - ``is_reply_to_us``          — classify replies to Aurora

    Part 2 (investigator + review posting in dry-run):
        No adapter changes — pure investigator + validator work using
        the snapshot already fetched in Part 1.

    Part 3 (live reviews):
        - ``post_verdict``            — submit Review with inline comments
        - ``dismiss_prior``           — clean up prior Review on re-run

The Part 1 build leaves ``post_verdict`` and ``dismiss_prior`` defined
on the protocol but the GitHub implementation raises ``NotImplementedError``
until Part 3 wires them.

Frozen dataclasses are used throughout because the pipeline persists
each value as JSONB and never mutates after construction. ``asdict()``
from the stdlib gives clean serialisation; ``replace()`` covers the
rare case (followups) where we need a near-copy with a few overrides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


# ─── Event + Snapshot ────────────────────────────────────────────────


@dataclass(frozen=True)
class NormalizedChangeEvent:
    """Vendor-neutral event produced by ``ChangeAdapter.parse``.

    Returned for both ``pull_request`` (kind=``code_change``) and reply
    events (kind=``code_change_followup``). Followups carry the original
    PR's external id in ``parent_external_id`` plus the engineer's
    comment in ``follow_up_comment``.

    Attributes:
        vendor: lowercase vendor string (``github``). Used by the
            registry to dispatch persistence/posting.
        kind: ``code_change`` for PR open/synchronize/reopened/ready,
            ``code_change_followup`` for engineer replies addressed to
            Aurora.
        org_id: Aurora org_id resolved from the installation. Required
            because ``change_events`` is RLS-protected. The adapter
            resolves this from ``user_github_installations``.
        installation_id: vendor-native install id (GitHub installation_id).
            Used to mint outbound tokens via the existing
            ``get_installation_token`` helper.
        external_id: vendor-native unique id for the event. For PRs this
            is the PR number scoped to the repo (e.g. ``"42"``); for
            comments it is the comment id. The validator joins on this
            when reconciling re-runs.
        dedup_key: stable string identifying the PR across re-runs.
            Format: ``<vendor>:<repo>:<pr_number>``. Used by the
            dispatcher to look up the prior investigation when a
            followup arrives.
        repo: ``owner/repo`` (vendor-neutral; GitHub already uses this
            convention).
        ref: head branch name. ``None`` for comment events.
        base_ref: base branch name (merge target). ``None`` for comment
            events.
        commit_sha: HEAD SHA of the change at webhook time. ``None``
            for comment events.
        actor: vendor-native username of the actor. For PRs this is
            the PR author; for comments it is the commenter.
        target_env: ``"prod"`` / ``"non-prod"`` / ``None``. Set by a
            cheap heuristic on branch name (``main|master|prod*`` →
            prod). Not used for gating in Phase 1a per the resolved
            open question — every PR is reviewed regardless of env.
            Persisted for analytics only.
        follow_up_comment: engineer's reply text verbatim. Populated
            iff ``kind == 'code_change_followup'``.
        parent_external_id: original PR's ``external_id`` (e.g. PR
            number) when this is a followup. ``None`` for PR events.
        action: vendor-native action that produced this event
            (``opened`` / ``synchronize`` / ``reopened`` /
            ``ready_for_review`` / ``reply``). Used by the dispatcher
            to decide whether to (re-)investigate or merely persist.
        raw_payload: full webhook body. Persisted to ``change_events.payload``
            for audit. Not used by the investigator (snapshot has
            everything it needs).
    """

    vendor: str
    kind: str
    org_id: str
    installation_id: int | None
    external_id: str
    dedup_key: str
    repo: str | None
    ref: str | None
    base_ref: str | None
    commit_sha: str | None
    actor: str | None
    target_env: str | None
    action: str
    raw_payload: dict[str, Any] = field(default_factory=dict)
    follow_up_comment: str | None = None
    parent_external_id: str | None = None


@dataclass(frozen=True)
class ChangeSnapshot:
    """Vendor-neutral snapshot fetched by ``ChangeAdapter.fetch_snapshot``.

    Everything the investigator needs about the PR at webhook time —
    persisted to ``change_events`` so the investigator never has to
    call back to the vendor mid-run.

    Attributes:
        body: PR body / MR description verbatim. The investigator
            treats this as "the engineer's stated reason" for the
            change and flags intent mismatches against the actual diff.
        diff: full unified diff (text). May be empty for some webhook
            edge cases (e.g. PR opened with no commits yet) — the
            investigator treats an empty diff as "approve, nothing to
            review."
        files: list of ``{path, status, additions, deletions}`` per file.
            Lets the investigator prioritise (a 1-line config change
            vs. a 500-line refactor warrant different depths).
        commits: list of ``{sha, message, author}`` per commit in the
            PR. Commit messages often explain intent beyond the body.
        comments: list of ``{id, user_login, body, in_reply_to_id,
            created_at}``. Fetched even for the initial investigation
            so the validator can reconcile re-runs against prior
            Aurora-authored comments without an extra API round-trip.
    """

    body: str
    diff: str
    files: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)


# ─── Reply classification ────────────────────────────────────────────


@dataclass(frozen=True)
class ReplyMatch:
    """Returned by ``ChangeAdapter.is_reply_to_us`` when a comment event
    is classified as a reply addressed to Aurora.

    The dispatcher uses ``repo`` + ``parent_pr_external_id`` to look up
    the most recent ``change_investigations`` row for the PR, and
    enqueues a follow-up investigation that takes ``comment_body`` as
    additional context.

    Attributes:
        repo: ``owner/repo`` for parent lookup.
        parent_pr_external_id: the PR number (as string) the comment is
            attached to.
        comment_id: vendor-native id of the engineer's comment. Used
            for thread continuity if Aurora's response is another
            inline comment.
        comment_body: full text of the engineer's comment, passed to
            the follow-up investigator verbatim.
        replier: vendor-native username of the engineer.
        match_kind: ``threaded`` (reply to Aurora's inline / review
            comment via ``in_reply_to_id``) or ``mention`` (top-level
            comment containing the App's @-handle) or ``re_review``
            (explicit recheck command — see ``RE_REVIEW_COMMANDS``).
    """

    repo: str
    parent_pr_external_id: str
    comment_id: str
    comment_body: str
    replier: str
    match_kind: str


# Phrases that explicitly request a re-review when @-mentioning the App.
# Tight allowlist on purpose — broader matching invites false positives
# from engineers casually @-ing Aurora about something unrelated. Case-
# insensitive substring match in ``is_reply_to_us``.
RE_REVIEW_COMMANDS: tuple[str, ...] = (
    "re-review",
    "recheck",
    "review again",
    "investigate again",
)


# ─── Verdict (Part 3 output of post_verdict) ─────────────────────────


@dataclass(frozen=True)
class PostedVerdict:
    """Returned by ``ChangeAdapter.post_verdict`` after a verdict lands.

    The vendor-native id of the Review (or equivalent) is persisted to
    ``change_investigations.external_verdict_id`` and used by
    ``dismiss_prior`` on a subsequent re-run.

    Attributes:
        verdict_id: vendor-native id of the Review (GitHub) / MR
            approval state (GitLab) / pullrequest action (Bitbucket).
        inline_comment_ids: vendor-native ids of the per-finding
            inline comments. Persisted to
            ``change_investigations.inline_comment_ids`` so a future
            ``synchronize`` can compare and potentially reuse them
            instead of re-posting.
    """

    verdict_id: str
    inline_comment_ids: list[str] = field(default_factory=list)


# ─── Finding (Part 2 investigator output) ────────────────────────────


@dataclass(frozen=True)
class Finding:
    """A single risk finding emitted by the investigator.

    Defined here (vendor-neutral) so the validator and review-poster
    work uniformly regardless of which vendor is hosting the PR. The
    adapter is responsible only for translating the list of findings
    into the vendor-native inline-comment format inside ``post_verdict``.

    Attributes:
        severity: ``HIGH`` / ``MEDIUM`` / ``LOW``. Only ``HIGH`` +
            ``HIGH`` confidence findings become inline comments per
            the Phase 1a policy.
        confidence: ``HIGH`` / ``MEDIUM`` / ``LOW``. The validator
            downgrades to ``MEDIUM`` if a finding cites no real tool
            call AND no mechanically-verifiable diff anchor.
        category: one of ``risk_taxonomy.CATEGORIES``. Findings outside
            the taxonomy are dropped by the validator.
        file_path: path of the file containing the issue. Must match a
            file in the unified diff or the finding is dropped.
        start_line: first line of the affected hunk (1-indexed,
            post-change line number).
        end_line: last line of the affected hunk. ``None`` for
            single-line findings.
        title: one-liner used as the inline comment header and as the
            top-level-body bullet.
        rationale: 2-3 sentence explanation of the concrete production
            failure mode this change could cause.
        cited_tool_calls: list of ``{tool, call_id, summary}`` entries
            referencing the investigator's transcript. Empty list is
            allowed only if the rationale contains an explicit
            ``[diff]`` reference (mechanically-verifiable claim).
    """

    severity: str
    confidence: str
    category: str
    file_path: str
    start_line: int
    title: str
    rationale: str
    end_line: int | None = None
    cited_tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ─── Protocol ────────────────────────────────────────────────────────


class ChangeAdapter(Protocol):
    """Vendor adapter protocol. See module docstring for phase-by-phase
    method coverage. Implementations live under ``adapters/<vendor>.py``
    and are registered in ``adapters.registry``.

    The ``vendor`` class attribute is the lowercase string used by the
    registry; it must match the ``vendor`` field on
    ``NormalizedChangeEvent`` instances returned by ``parse``.
    """

    vendor: str

    def verify_signature(
        self,
        raw_body: bytes,
        headers: dict[str, str],
    ) -> bool:
        """Verify the webhook signature against the vendor's spec.

        Args:
            raw_body: exact bytes of the request body — caller MUST
                pass the un-re-serialised payload, as HMAC is byte-
                exact.
            headers: incoming HTTP headers as a plain dict.

        Returns:
            ``True`` if the signature validates, ``False`` otherwise.
            Implementations should NEVER raise on a signature
            mismatch; raise only on malformed headers (which the
            caller treats as 401).
        """
        ...

    def parse(
        self,
        event_type: str,
        payload: dict[str, Any],
        org_id: str,
    ) -> NormalizedChangeEvent | None:
        """Translate a webhook payload into a ``NormalizedChangeEvent``.

        Returns ``None`` for events we accept but do not act on (e.g.
        a ``pull_request.closed`` action — we don't review closed PRs).
        The caller treats ``None`` as "no-op, mark webhook delivery
        processed."

        Args:
            event_type: vendor's event type header value
                (``pull_request``, ``issue_comment``,
                ``pull_request_review_comment``).
            payload: parsed JSON body.
            org_id: Aurora org_id resolved upstream by the dispatcher
                from ``user_github_installations``. Threaded through
                here so the returned event can be persisted under the
                correct RLS context.
        """
        ...

    def fetch_snapshot(
        self,
        event: NormalizedChangeEvent,
    ) -> ChangeSnapshot:
        """One-shot fetch of diff + files + commits + body + comments.

        Called immediately after ``parse`` so the investigator never
        has to call back to the vendor mid-run. Implementations use
        their vendor-native auth (GitHub installation token, GitLab
        PAT/App, Bitbucket App Password) — none of that surfaces to
        the caller.

        Args:
            event: the parsed event. ``installation_id`` + ``repo`` +
                ``external_id`` (PR number for ``code_change`` kinds)
                are the fields the adapter needs.
        """
        ...

    def is_reply_to_us(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> ReplyMatch | None:
        """Classify a comment event as a reply addressed to Aurora.

        Returns a ``ReplyMatch`` for replies the dispatcher should
        treat as a follow-up trigger; ``None`` otherwise. Self-filter
        is mandatory: events where the sender is Aurora's own App user
        MUST return ``None`` to prevent feedback loops.

        Args:
            event_type: ``issue_comment`` or
                ``pull_request_review_comment``.
            payload: parsed JSON body.
        """
        ...

    def post_verdict(
        self,
        event: NormalizedChangeEvent,
        investigation: dict[str, Any],
    ) -> PostedVerdict:
        """Submit the verdict + inline comments to the vendor.

        Wired in Part 3. The GitHub implementation builds a single
        ``POST /pulls/{n}/reviews`` call with
        ``event=APPROVE|REQUEST_CHANGES``, ``body=<rendered>``, and
        a ``comments`` array of up to three HIGH+HIGH findings.

        Args:
            event: the parsed event the verdict is for.
            investigation: a ``change_investigations`` row dict with
                ``verdict``, ``summary``, ``findings``,
                ``intent_alignment``, ``intent_notes`` populated.

        Returns:
            ``PostedVerdict`` capturing the vendor-native review id
            and inline-comment ids for future dismissal.
        """
        ...

    def dismiss_prior(
        self,
        event: NormalizedChangeEvent,
        prior_verdict: PostedVerdict,
    ) -> None:
        """Dismiss a prior verdict before re-posting.

        Wired in Part 3. GitHub's
        ``PUT /pulls/{n}/reviews/{id}/dismissals`` clears the
        request-changes state; inline comments are left in place but
        automatically marked outdated by GitHub when their line moves.

        Args:
            event: the parsed event we're re-running for.
            prior_verdict: the ``PostedVerdict`` we previously stored.
        """
        ...
