"""
Celery task to generate LLM-powered descriptions for Slack channels.

Mirrors :mod:`routes.github.github_repo_metadata`. Fetches a channel's
topic/purpose + a small sample of recent messages, then asks an LLM for a
2-3 sentence description of what the channel is for and which team/service it
serves. The result feeds the agent's channel-routing decisions.
"""
import logging
import time

from celery_config import celery_app
from chat.backend.agent.utils.message_content import extract_text_from_content
from routes.slack.slack_backfill_config import (
    BACKFILL_INTERVAL_SECONDS,
    BACKFILL_STALE_MINUTES,
)
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

METADATA_PROMPT = (
    "Write a 2-3 sentence description of this Slack channel for an AI SRE "
    "teammate. State what the channel is used for, which team or service it "
    "serves, and whether it's an incident/alerting channel (and from which "
    "platform if apparent, e.g. incident.io/PagerDuty/Opsgenie). Infer from the "
    "name, topic, purpose, and recent messages. Output ONLY the description — "
    "no notes, caveats, or markdown headers.\n\n"
    "{context}"
)

# --- Periodic backfill bounds ----------------------------------------------
# Descriptions are normally enqueued by a user-triggered reconcile pass, which is
# capped per pass. This beat task is the safety net for the tail; these caps bound
# its LLM spend and queue depth per run.
BACKFILL_MAX_PER_ORG = 25
BACKFILL_MAX_TOTAL = 200

# How many rows one org's read pass may scan before giving up for this run. The
# pass re-reads past workspaces nobody can describe (see _claim_org_channels), so
# without a ceiling an org whose backlog is mostly unreachable would page through
# all of it every 15 minutes.
BACKFILL_MAX_SCAN_PER_ORG = 500

# BACKFILL_STALE_MINUTES lives in slack_backfill_config: slack_channels backdates
# the rows it registers without describing against the same window read here, and
# a drift strands those rows for the difference.


def _update_metadata(user_id: str, channel_id: str, summary, status: str,
                     channel_type: str | None = None, platform: str | None = None):
    """Persist a description write, only advancing rows still pending/generating."""
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            if not set_rls_context(cur, conn, user_id, log_prefix="[SlackChannelMeta]"):
                return
            # Status-only update (generating / error) — don't clobber a summary.
            # Scoped by the RLS org context (set above), NOT user_id: the row may
            # be owned by a different org member, so a user_id predicate would
            # match zero rows and silently drop the write.
            if summary is None:
                cur.execute(
                    """UPDATE slack_channels
                       SET metadata_status = %s, updated_at = NOW()
                       WHERE provider = 'slack' AND channel_id = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (status, channel_id),
                )
            else:
                cur.execute(
                    """UPDATE slack_channels
                       SET metadata_summary = %s, metadata_status = %s,
                           channel_type = COALESCE(%s, channel_type),
                           detected_platform = COALESCE(%s, detected_platform),
                           updated_at = NOW()
                       WHERE provider = 'slack' AND channel_id = %s
                         AND metadata_status IN ('pending', 'generating')""",
                    (summary, status, channel_type, platform, channel_id),
                )
            conn.commit()


def _claim_for_generation(user_id: str, channel_id: str,
                          allow_generating: bool = False) -> bool:
    """Flip pending -> generating, returning False if someone else got there first.

    The sweep re-enqueues rows that look stuck, so the same channel can be queued
    twice; without this the second task would pay for a full LLM call whose write
    ``_update_metadata`` then discards. ``allow_generating`` is for our own retry,
    which re-enters with the row already flipped by its first attempt.
    """
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context

    statuses = ['pending', 'skipped'] + (['generating'] if allow_generating else [])
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if not set_rls_context(cur, conn, user_id, log_prefix="[SlackChannelMeta]"):
            return False
        cur.execute(
            """UPDATE slack_channels
               SET metadata_status = 'generating', updated_at = NOW()
               WHERE provider = 'slack' AND channel_id = %s
                 AND metadata_status = ANY(%s)""",
            (channel_id, statuses),
        )
        claimed = cur.rowcount > 0
        conn.commit()
    return claimed


class ChannelUnreadable(RuntimeError):
    """conversations.info returned nothing for the channel.

    Means the token can't see it (wrong workspace, archived, revoked scope) or
    Slack failed. Either way there's nothing to describe, and an empty context
    would just make the LLM invent a summary — so this fails into the retry path,
    and on exhaustion back to 'pending' rather than 'error' (see
    :func:`_record_exhausted_retry`).
    """


def _record_exhausted_retry(user_id: str, channel_id: str, exc: BaseException) -> str:
    """Land a task that ran out of retries on a status, returning which one.

    'error' is terminal — neither the sweep nor reconcile ever retries it, only an
    explicit regenerate. That's right once LLM quota is spent, but ChannelUnreadable
    fires before the LLM call, and get_channel_info swallows timeouts and 429s into
    None, so marking 'error' lets a Slack blip outlasting two 30s retries strand
    every channel being described at that moment. Those go back to 'pending'.
    """
    # Nothing was spent, so the sweep can afford to try again in a few hours.
    status = "pending" if isinstance(exc, ChannelUnreadable) else "error"
    _update_metadata(user_id, channel_id, None, status)
    return status


def _build_context(client, channel_id: str) -> tuple[str, str, dict]:
    """Return (context_text, channel_name, raw_info) for the LLM prompt."""
    info = client.get_channel_info(channel_id) or {}
    # Nothing came back, so we never actually read the channel. Describing it
    # anyway would persist a fabricated summary as 'ready'.
    if not info:
        raise ChannelUnreadable("conversations.info returned no channel")
    name = info.get("name", "")
    topic = (info.get("topic") or {}).get("value", "")
    purpose = (info.get("purpose") or {}).get("value", "")

    parts = [f"Channel name: #{name}"]
    if topic:
        parts.append(f"Topic: {topic}")
    if purpose:
        parts.append(f"Purpose: {purpose}")

    # A few recent messages give the LLM a sense of the channel's actual use.
    # Best-effort: the bot may not be a member, which is fine.
    try:
        result = client._make_request(
            "GET", "conversations.history", {"channel": channel_id, "limit": 15}
        )
        raw_messages = result.get("messages", [])
        texts = [
            (m.get("text") or "").strip()
            for m in result.get("messages", [])
            if (m.get("text") or "").strip()
        ]
        logger.info(
            "[SlackChannelMeta] history for #%s (%s): api_ok=%s returned=%d with_text=%d",
            sanitize(name), sanitize(channel_id), result.get("ok"),
            len(raw_messages), len(texts),
        )
        if texts:
            sample = "\n".join(f"- {t[:200]}" for t in texts[:15])
            parts.append(f"Recent messages:\n{sample}")
        else:
            # No usable text — often the bot isn't a member of the channel.
            logger.info(
                "[SlackChannelMeta] no message text for #%s (%s) — bot may not be a member",
                sanitize(name), sanitize(channel_id),
            )
    except Exception as e:
        logger.warning(
            "[SlackChannelMeta] history fetch failed for #%s (%s): %s",
            sanitize(name), sanitize(channel_id), sanitize(e),
        )

    return "\n".join(parts), name, info


@celery_app.task(
    name="routes.slack.slack_channel_metadata.auto_register_channels_task",
    bind=True,
    max_retries=2,
)
def auto_register_channels_task(self, user_id: str, team_id: str | None = None):
    """Background wrapper for auto_register_channels.

    Slack OAuth stores credentials then needs to enumerate + describe the
    workspace's channels. For large workspaces that paginated Slack I/O plus DB
    writes can exceed the OAuth callback's request budget, timing out the
    redirect after the connection was already saved. Running it here keeps the
    callback fast; the manage page polls for the channels as descriptions land.
    """
    try:
        from routes.slack.slack_channels import auto_register_channels
        auto_register_channels(user_id, team_id=team_id)
    except Exception as exc:
        logger.warning("[SlackAutoRegister] task failed for user %s; retrying", sanitize(user_id))
        # Retry a couple of times (e.g. transient Slack 429/5xx); give up quietly
        # after that — the user can hit "Refresh channels" on the manage page.
        try:
            self.retry(countdown=30, exc=exc)
        except self.MaxRetriesExceededError:
            logger.warning("[SlackAutoRegister] gave up after retries for user %s", sanitize(user_id))


@celery_app.task(
    name="routes.slack.slack_channel_metadata.bulk_activate_channels_task",
    bind=True,
    max_retries=2,
)
def bulk_activate_channels_task(self, user_id: str, channel_ids: list[str]):
    """Join + register/describe many channels in the background.

    Bulk-activate joins each channel via conversations.join (Tier 3, rate
    limited) and enqueues a description. Doing that inline in the HTTP request
    means a large batch (e.g. 50+ channels) serially hits Slack and can
    rate-limit/timeout the request. Running it here keeps the endpoint instant;
    the manage page polls the channel list as rows/descriptions land.
    """
    try:
        from routes.slack.slack_channels import _activate_channels
        activated = _activate_channels(user_id, channel_ids)
        logger.info("[SlackBulkActivate] activated %d/%d channel(s) for user %s",
                    activated, len(channel_ids), sanitize(user_id))
    except Exception as exc:
        logger.warning("[SlackBulkActivate] task failed for user %s; retrying", sanitize(user_id))
        # Retry on transient Slack 429/5xx; _activate_channels is idempotent
        # (join + describe both no-op on already-member channels).
        try:
            self.retry(countdown=30, exc=exc)
        except self.MaxRetriesExceededError:
            logger.warning("[SlackBulkActivate] gave up after retries for user %s", sanitize(user_id))


@celery_app.task(
    name="routes.slack.slack_channel_metadata.generate_channel_metadata",
    bind=True,
    max_retries=2,
)
def generate_channel_metadata(self, user_id: str, channel_id: str):
    """Fetch channel info + recent messages and generate an LLM description."""
    logger.info("Generating metadata for Slack channel %s (user %s)", sanitize(channel_id), sanitize(user_id))

    # Respect the LLM-usage hook (cost gating), like the GitHub metadata task.
    from utils.hooks import get_hook
    from utils.auth.stateless_auth import get_org_id_for_user
    _hook_org_id = get_org_id_for_user(user_id) if user_id else None
    hook_allowed, hook_message = get_hook("before_llm_call")(_hook_org_id, user_id)
    if not hook_allowed:
        logger.warning("Hook blocked channel metadata for user %s: %s", user_id, hook_message)
        _update_metadata(user_id, channel_id, None, "limit_reached")
        return

    try:
        # Claim the row before spending anything. The sweep re-enqueues rows that
        # look stuck, so the same channel can be queued twice; losing the claim
        # means another task owns it and this one would only pay for an LLM call
        # whose write _update_metadata discards. Inside the try so a DB failure
        # here is retried rather than leaving the row silently 'pending'.
        if not _claim_for_generation(user_id, channel_id,
                                     allow_generating=bool(self.request.retries)):
            logger.info("[SlackChannelMeta] %s already claimed elsewhere — skipping",
                        sanitize(channel_id))
            return

        from connectors.slack_connector.client import get_slack_client_for_user
        client = get_slack_client_for_user(user_id)
        # Creds vanished between the sweep picking this actor and now (disconnect,
        # or a transient Vault read). Also pre-LLM, so leave it to the sweep — and
        # if nobody in the org can reach Slack, _assign_actors won't re-enqueue it
        # at all, so 'pending' costs nothing while 'error' would be permanent.
        if not client:
            _update_metadata(user_id, channel_id, None, "pending")
            return

        context_text, channel_name, info = _build_context(client, channel_id)
        logger.info(
            "[SlackChannelMeta] context for #%s (%s): %d chars, includes_recent_messages=%s",
            sanitize(channel_name), sanitize(channel_id),
            len(context_text), "Recent messages:" in context_text,
        )

        # Re-classify with the fuller info dict (topic/purpose now available).
        from routes.slack.slack_channels import _classify_channel
        channel_type, platform = _classify_channel(info)

        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
        from langchain_core.messages import HumanMessage

        llm = create_chat_model(
            ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            temperature=0.2,
            streaming=False,
        )
        prompt = METADATA_PROMPT.format(context=context_text)
        response = tracked_invoke(
            llm,
            [HumanMessage(content=prompt)],
            user_id=user_id,
            model_name=ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL,
            request_type="slack_channel_metadata",
        )
        summary = extract_text_from_content(response.content).strip() or "No description generated"
        _update_metadata(user_id, channel_id, summary, "ready", channel_type, platform)
        logger.info("Metadata generated for Slack channel %s", sanitize(channel_id))

    except Exception as e:
        logger.exception("Channel metadata generation failed for %s: %s", sanitize(channel_id), e)
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _record_exhausted_retry(user_id, channel_id, e)


_BACKFILL_LOG = "[SlackDescBackfill]"


def _rotate_orgs(org_ids: list[str], now: float | None = None) -> list[str]:
    """Rotate the org list so a different org leads each run.

    The sweep stops at a global cap, so a fixed order would let a few
    large-backlog orgs consume it every run and starve the rest. Clock-derived so
    there's no cursor to migrate and concurrent workers agree on the order.
    """
    if not org_ids:
        return []
    if now is None:
        now = time.time()
    offset = int(now // BACKFILL_INTERVAL_SECONDS) % len(org_ids)
    return org_ids[offset:] + org_ids[:offset]


def _select_undescribed_channels(cur, limit: int,
                                 exclude_teams: list[str] | None = None,
                                 exclude_untagged: bool = False,
                                 ) -> list[tuple[str, str, str | None]]:
    """Return [(channel_id, owner_user_id, team_id)] for member channels still
    awaiting a description. Caller must have set the org's RLS context.

    ``team_id`` is the Slack workspace the row was registered against; the actor
    picker needs it because an org can connect more than one workspace.
    ``exclude_teams`` / ``exclude_untagged`` drop workspaces the caller has
    already proven unreachable, so the oldest undescribable rows can't wedge the
    window and hide the rest of the backlog.

    Longest-waiting first so bounded runs drain the backlog; DISTINCT ON de-dupes
    the row-per-member. 'error'/'limit_reached' excluded — retrying burns quota.
    A stale 'generating' row is included: it's crash recovery, not a retry.
    """
    cur.execute(
        """SELECT channel_id, user_id, team_id FROM (
               SELECT DISTINCT ON (channel_id) channel_id, user_id, team_id, updated_at
                 FROM slack_channels
                WHERE provider = 'slack' AND is_member
                  AND (
                        metadata_status = 'skipped'
                     -- 'generating' is set by the task itself, so a stale one
                     -- means the worker died mid-flight (eviction/OOM) and no
                     -- retry will fire. Without this the row is stranded for
                     -- good: a spinner in the UI, and invisible to the agent,
                     -- which only lists 'ready'. Generation takes seconds, so
                     -- anything this old is not still running.
                     OR (metadata_status IN ('pending', 'generating')
                         AND updated_at < NOW() - make_interval(mins => %s))
                  )
                  -- Workspaces already proven unreachable this run. Guarded on
                  -- NULL because `NULL = ANY(...)` is unknown, not false, and
                  -- would silently drop every untagged row.
                  AND (team_id IS NULL OR team_id <> ALL(%s::text[]))
                  AND (team_id IS NOT NULL OR NOT %s)
                ORDER BY channel_id, updated_at ASC
           ) AS d
           ORDER BY d.updated_at ASC
           LIMIT %s""",
        (BACKFILL_STALE_MINUTES, exclude_teams or [], exclude_untagged, limit),
    )
    return [(row[0], row[1], row[2]) for row in cur.fetchall()]


class _SlackCredProbe:
    """Memoized "can this user reach Slack, and which workspace?" lookup.

    Each probe is a DB + Vault round trip, and a sweep asks about the same users
    repeatedly, so answers are cached for the lifetime of one run. The workspace
    matters as much as the yes/no: an org can hold Slack connections for several
    workspaces, and a token from the wrong one can't read the channel.
    """

    def __init__(self):
        self._cache: dict[str, dict | None] = {}

    def _creds(self, user_id: str) -> dict | None:
        if user_id not in self._cache:
            from utils.auth.stateless_auth import get_credentials_from_db
            try:
                creds = get_credentials_from_db(user_id, "slack") or {}
            except Exception:
                creds = {}
            self._cache[user_id] = creds if creds.get("access_token") else None
        return self._cache[user_id]

    def __call__(self, user_id: str) -> bool:
        """True when the user still has usable Slack credentials."""
        return self._creds(user_id) is not None

    def team(self, user_id: str) -> str | None:
        """The Slack workspace the user's connection belongs to, if recorded."""
        return (self._creds(user_id) or {}).get("team_id")


def _can_reach(probe: "_SlackCredProbe", user_id: str, team_id: str | None) -> bool:
    """True when the user's Slack token belongs to the channel's workspace.

    ``team_id`` must be the row's recorded workspace: an untagged row has nothing
    to compare against, so it is never resolved through here (see
    :func:`_untagged_actor`).
    """
    if not probe(user_id) or team_id is None:
        return False
    return probe.team(user_id) == team_id


def _fallback_actor(org_users: list[str], team_id: str,
                    probe: "_SlackCredProbe") -> str | None:
    """Pick an org member whose Slack token belongs to ``team_id``.

    Credentials are org-shared, but an org can connect several workspaces, so a
    stand-in for a disconnected owner is only safe if its token belongs to the
    channel's workspace. A foreign token makes conversations.info return nothing
    and we'd persist an invented description as 'ready'.
    """
    return next((uid for uid in org_users
                 if probe(uid) and probe.team(uid) == team_id), None)


def _untagged_actor(org_users: list[str], owner_id: str,
                    probe: "_SlackCredProbe") -> str | None:
    """Pick an actor for a row with no recorded workspace.

    Rows registered before workspace tagging (or where Slack never returned a
    team) can't be matched against a token, and the owner's current token may
    well point at a different workspace now. Only safe when the org has exactly
    one connected workspace; otherwise it's a guess, so leave the row pending.
    """
    connected = [uid for uid in org_users if probe(uid)]
    if len({probe.team(uid) for uid in connected}) != 1:
        return None
    # Single workspace: every connected member's token reads the same one, so
    # prefer the owner to keep the actor stable across runs.
    return owner_id if owner_id in connected else connected[0]


def _assign_actors(rows: list[tuple[str, str, str | None]], org_users: list[str],
                   probe: "_SlackCredProbe") -> tuple[list[tuple[str, str]], set[str | None]]:
    """Pair each channel with the user_id whose credentials should describe it.

    ``rows`` is [(channel_id, owner_user_id, team_id)] as returned by
    :func:`_select_undescribed_channels`. Returns
    ``([(actor_user_id, channel_id)], unreachable_team_ids)`` — the second element
    lets the caller skip those workspaces and keep scanning the backlog.
    """
    # Memoized per workspace: resolving a stand-in walks the org's members and
    # each probe is a DB + Vault round trip.
    actors: dict[str | None, str | None] = {}

    assigned: list[tuple[str, str]] = []
    unreachable: set[str | None] = set()
    for channel_id, owner_id, team_id in rows:
        # Prefer the row's owner — the identity whose token registered it. Still
        # workspace-checked: an owner who disconnected and reconnected to a
        # different workspace can no longer read this channel.
        if _can_reach(probe, owner_id, team_id):
            assigned.append((owner_id, channel_id))
            continue

        # Owner is gone (or moved workspace), so look for a stand-in. Untagged
        # rows have no workspace to match on and are resolved separately.
        if team_id is None:
            actor = _untagged_actor(org_users, owner_id, probe)
        else:
            if team_id not in actors:
                actors[team_id] = _fallback_actor(org_users, team_id, probe)
            actor = actors[team_id]

        # No compatible connection left in the org — leave the row pending rather
        # than queueing a task that would only mark it 'error' (which is never
        # auto-retried) or describe it from an empty, wrong-workspace context.
        if actor:
            assigned.append((actor, channel_id))
        else:
            # Nothing in the org can read this workspace, so every other row on it
            # is unreachable too — report it so the caller stops re-reading them.
            unreachable.add(team_id)
    return assigned, unreachable


def _claim_org_channels(org_users: list[str], limit: int,
                        probe: "_SlackCredProbe") -> list[tuple[str, str]]:
    """Claim up to ``limit`` of one org's undescribed channels for description.

    "Claim" = mark them 'pending' with a fresh ``updated_at`` and COMMIT, so a
    concurrent sweep won't pick them up again. Returns [(actor_user_id,
    channel_id)] for the caller to enqueue.
    """
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context
    from routes.slack.slack_channels import _mark_pending

    # Reads happen in short-lived blocks so the pooled connection isn't held
    # across the Vault round trips the credential probe makes.
    def read(exclude_teams, exclude_untagged):
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
            # No org context resolvable (e.g. org deleted mid-sweep) — don't query
            # with the wrong context.
            if not set_rls_context(cur, conn, org_users[0], log_prefix=_BACKFILL_LOG):
                return None
            return _select_undescribed_channels(
                cur, limit, exclude_teams=exclude_teams,
                exclude_untagged=exclude_untagged,
            )

    to_enqueue: list[tuple[str, str]] = []
    dead_teams: set[str] = set()
    skip_untagged = False
    scanned = 0
    # The oldest rows may belong to a workspace nobody is connected to; those never
    # become describable, so a single page would hand back the same dead rows every
    # sweep and the channels behind them would never be reached. Re-read past each
    # workspace we've just proven unreachable.
    while True:
        rows = read(sorted(dead_teams), skip_untagged)
        if not rows:
            return []
        scanned += len(rows)
        # Each page is the same oldest-first prefix minus the excluded workspaces,
        # so a later page re-assigns everything an earlier one did — replace rather
        # than accumulate.
        to_enqueue, unreachable = _assign_actors(rows, org_users, probe)
        new_dead = unreachable - dead_teams - ({None} if skip_untagged else set())
        # Budget filled, nothing new to skip past, or we've scanned far enough for
        # one run — any of those means the next page adds nothing.
        if (len(to_enqueue) >= limit or not new_dead
                or scanned >= BACKFILL_MAX_SCAN_PER_ORG):
            break
        dead_teams |= {t for t in new_dead if t is not None}
        skip_untagged = skip_untagged or None in new_dead
    if not to_enqueue:
        return []

    # Write pass, committed BEFORE the caller enqueues, so a worker can never pick
    # a task up against a row that still looks unqueued (or, for a 'skipped' row,
    # one whose status would make the description write a no-op). Passing the
    # staleness window makes select-then-claim atomic: only rows the UPDATE
    # actually claimed are returned, so two overlapping sweeps racing on the same
    # row can't both enqueue it.
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        if not set_rls_context(cur, conn, org_users[0], log_prefix=_BACKFILL_LOG):
            return []
        claimed = set(_mark_pending(
            cur, [cid for _actor, cid in to_enqueue],
            stale_minutes=BACKFILL_STALE_MINUTES,
        ))
        conn.commit()
    return [(actor, cid) for actor, cid in to_enqueue if cid in claimed]


@celery_app.task(name="routes.slack.slack_channel_metadata.backfill_channel_descriptions")
def backfill_channel_descriptions():
    """Beat entry point. The body lives in the undecorated function below so tests
    and other callers can invoke it without a broker."""
    return _backfill_channel_descriptions()


def _backfill_channel_descriptions():
    """Make sure EVERY member channel eventually gets described.

    Routing only offers 'ready' channels, but descriptions were otherwise only
    enqueued by capped, user-triggered reconcile passes, so a large workspace's
    tail stayed 'pending' forever. DB-only, so a timer can't hit Slack's limits.
    """
    from routes.slack.slack_channels import _enqueue_metadata
    from utils.auth.stateless_auth import users_by_org

    try:
        org_users_map = users_by_org()
    except Exception:
        logger.warning("%s could not enumerate orgs", _BACKFILL_LOG, exc_info=True)
        return {"orgs": 0, "enqueued": 0}

    probe = _SlackCredProbe()
    total = 0
    orgs_touched = 0

    # Rotate the starting org so the global cap can't permanently starve the orgs
    # that happen to sort last.
    for org_id in _rotate_orgs(list(org_users_map)):
        org_users = org_users_map[org_id]
        # Global cap reached — remaining orgs get their turn next run (the
        # rotation above is what makes that fair across orgs; the
        # longest-waiting-first ordering in the query is fair within one).
        if total >= BACKFILL_MAX_TOTAL:
            break
        try:
            to_enqueue = _claim_org_channels(
                org_users,
                min(BACKFILL_MAX_PER_ORG, BACKFILL_MAX_TOTAL - total),
                probe,
            )
        except Exception:
            # One org's failure must not starve the rest of the sweep.
            logger.warning("%s sweep failed for one org", _BACKFILL_LOG, exc_info=True)
            continue
        if not to_enqueue:
            continue

        for actor, channel_id in to_enqueue:
            _enqueue_metadata(actor, channel_id)
        total += len(to_enqueue)
        orgs_touched += 1

    if total:
        logger.info("%s enqueued %d description(s) across %d org(s)",
                    _BACKFILL_LOG, total, orgs_touched)
    return {"orgs": orgs_touched, "enqueued": total}
