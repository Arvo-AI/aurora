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
from routes.slack.slack_backfill_config import BACKFILL_INTERVAL_SECONDS
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
# Descriptions are normally enqueued by a reconcile pass (Slack connect, a manage
# page load, an explicit refresh), which is bounded to MAX_AUTO_CHANNELS per
# pass. The beat task below is the safety net that finishes the tail; these caps
# bound its LLM spend and queue depth per run.
BACKFILL_MAX_PER_ORG = 25
BACKFILL_MAX_TOTAL = 200

# A 'pending' row younger than this probably has a description task still in
# flight, so re-enqueueing it would only duplicate the LLM call. Intentionally
# NOT the same as the reconcile pass's 10-minute window: reconcile runs only when
# a user is looking at the page (so a slightly eager retry is cheap and visible),
# whereas this runs unattended across every org, so it waits longer to be sure a
# row is genuinely stuck rather than merely slow.
BACKFILL_STALE_MINUTES = 15


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


def _build_context(client, channel_id: str) -> tuple[str, str, dict]:
    """Return (context_text, channel_name, raw_info) for the LLM prompt."""
    info = client.get_channel_info(channel_id) or {}
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
        # Inside the try so a DB/connection failure on this initial status write
        # is retried (not left silently stuck as 'pending').
        _update_metadata(user_id, channel_id, None, "generating")

        from connectors.slack_connector.client import get_slack_client_for_user
        client = get_slack_client_for_user(user_id)
        if not client:
            _update_metadata(user_id, channel_id, None, "error")
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
            _update_metadata(user_id, channel_id, None, "error")


_BACKFILL_LOG = "[SlackDescBackfill]"


def _users_by_org() -> dict[str, list[str]]:
    """Map org_id -> [user_id, ...]. ``users`` is NOT RLS-protected, so it can be
    read before any org context is set — which is how a cross-org task discovers
    the orgs it has to iterate (see the RLS notes in AGENTS.md)."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT org_id, id FROM users WHERE org_id IS NOT NULL ORDER BY org_id, id"
        )
        by_org: dict[str, list[str]] = {}
        for org_id, user_id in cur.fetchall():
            by_org.setdefault(org_id, []).append(user_id)
        return by_org


def _rotate_orgs(org_ids: list[str], now: float | None = None) -> list[str]:
    """Rotate the org list so a different org leads each run.

    The sweep stops at a global cap, so a fixed iteration order would let a few
    large-backlog orgs consume the whole cap every run and starve the rest
    indefinitely (the longest-waiting-first ordering in the query is only fair
    *within* one org).

    The offset is derived from wall-clock time rather than stored state: the beat
    interval advances it by one each run, so every org takes a turn at the front
    without needing a cursor in Redis or the DB (which would be one more thing to
    migrate, and would reset anyway). Being a pure function of the clock also
    means concurrent beat workers agree on the order.
    """
    if not org_ids:
        return []
    if now is None:
        now = time.time()
    offset = int(now // BACKFILL_INTERVAL_SECONDS) % len(org_ids)
    return org_ids[offset:] + org_ids[:offset]


def _select_undescribed_channels(cur, limit: int) -> list[tuple[str, str]]:
    """Return [(channel_id, owner_user_id)] for member channels still awaiting a
    description. Expects the caller to have set the org's RLS context already.

    Longest-waiting first, so repeated bounded runs drain the backlog fairly
    instead of re-picking the same head of the list.

    ``DISTINCT ON (channel_id)`` because the table is unique per
    ``(user_id, provider, channel_id)``: when two org members each connected
    Slack, the same channel has a row per member. Both rows describe one Slack
    channel and the metadata write covers them all (it's keyed on channel_id), so
    without the de-dupe we'd pay for the same LLM call twice.

    'error'/'limit_reached' rows are deliberately excluded: the metadata task's
    own retries already bound transient failures, and auto-retrying a hard
    failure or a cost-capped org on a timer would just burn quota in a loop.
    Those need an explicit regenerate.
    """
    cur.execute(
        """SELECT channel_id, user_id FROM (
               SELECT DISTINCT ON (channel_id) channel_id, user_id, updated_at
                 FROM slack_channels
                WHERE provider = 'slack' AND is_member
                  AND (
                        metadata_status = 'skipped'
                     OR (metadata_status = 'pending'
                         AND updated_at < NOW() - make_interval(mins => %s))
                  )
                ORDER BY channel_id, updated_at ASC
           ) AS d
           ORDER BY d.updated_at ASC
           LIMIT %s""",
        (BACKFILL_STALE_MINUTES, limit),
    )
    return [(row[0], row[1]) for row in cur.fetchall()]


class _SlackCredProbe:
    """Memoized "can this user reach Slack?" check.

    Each probe is a DB + Vault round trip, and a sweep asks about the same users
    repeatedly, so answers are cached for the lifetime of one run.
    """

    def __init__(self):
        self._cache: dict[str, bool] = {}

    def __call__(self, user_id: str) -> bool:
        if user_id not in self._cache:
            from connectors.slack_connector.client import get_slack_client_for_user
            try:
                self._cache[user_id] = get_slack_client_for_user(user_id) is not None
            except Exception:
                self._cache[user_id] = False
        return self._cache[user_id]


def _assign_actors(rows: list[tuple[str, str]], org_users: list[str],
                   has_slack) -> list[tuple[str, str]]:
    """Pair each channel with the user_id whose credentials should describe it.

    Returns [(actor_user_id, channel_id)], skipping channels nobody can reach.
    """
    # Prefer the row's owner — the identity whose token registered the channel.
    # Only when an owner has since disconnected Slack do we need a stand-in, so
    # the (DB + Vault) lookup for one is done lazily.
    fallback = None
    if any(not has_slack(owner_id) for _cid, owner_id in rows):
        fallback = next((uid for uid in org_users if has_slack(uid)), None)

    assigned = []
    for channel_id, owner_id in rows:
        actor = owner_id if has_slack(owner_id) else fallback
        # Nobody in the org can reach Slack any more — leave the row pending
        # rather than queueing a task that would only mark it 'error' (which is
        # never auto-retried).
        if actor:
            assigned.append((actor, channel_id))
    return assigned


def _claim_org_channels(org_users: list[str], limit: int,
                        has_slack) -> list[tuple[str, str]]:
    """Claim up to ``limit`` of one org's undescribed channels for description.

    "Claim" = select them, then mark them 'pending' with a fresh ``updated_at``
    and COMMIT, so the rows no longer look unqueued to a concurrent sweep. The
    caller enqueues the returned work afterwards.

    Returns [(actor_user_id, channel_id)] — empty when there's nothing to do.
    """
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context
    from routes.slack.slack_channels import _mark_pending

    # Read pass, in its own short-lived block so the pooled connection isn't held
    # across the Vault round trips the credential probe below makes.
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        # No org context resolvable (e.g. org deleted mid-sweep) — don't query
        # with the wrong context.
        if not set_rls_context(cur, conn, org_users[0], log_prefix=_BACKFILL_LOG):
            return []
        rows = _select_undescribed_channels(cur, limit)
    if not rows:
        return []

    to_enqueue = _assign_actors(rows, org_users, has_slack)
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
    """Beat entry point — thin wrapper so the logic stays directly callable.

    The decorated object is the Celery task, not the function, so keeping the
    body in :func:`_backfill_channel_descriptions` is what lets tests (and any
    other caller) invoke it without a broker. Mirrors
    :func:`auto_register_channels_task` wrapping ``auto_register_channels``.
    """
    return _backfill_channel_descriptions()


def _backfill_channel_descriptions():
    """Make sure EVERY member channel eventually gets described.

    Agent routing (``get_connected_slack_channels``) only offers channels whose
    ``metadata_status`` is 'ready'. Descriptions are otherwise only enqueued by a
    reconcile pass — Slack connect, a manage-page load, or an explicit refresh —
    and each pass enqueues at most ``MAX_AUTO_CHANNELS``. In a workspace where
    Aurora is a member of hundreds of channels, the tail past that cap only got
    picked up if a human kept reloading the manage page, so those channels stayed
    'pending' forever: active in Slack, but invisible to routing.

    This closes that gap. Each run sweeps every org for the longest-waiting
    member channels still 'pending'/'skipped' and enqueues a bounded batch, so
    coverage converges on its own with no user action.

    Deliberately DB-only (no Slack API calls), so running it across all orgs on a
    timer can't trip Slack's rate limits; membership reconciliation stays with
    :func:`~routes.slack.slack_channels.auto_register_channels`.
    """
    from routes.slack.slack_channels import _enqueue_metadata

    try:
        users_by_org = _users_by_org()
    except Exception:
        logger.warning("%s could not enumerate orgs", _BACKFILL_LOG, exc_info=True)
        return {"orgs": 0, "enqueued": 0}

    has_slack = _SlackCredProbe()
    total = 0
    orgs_touched = 0

    # Rotate the starting org so the global cap can't permanently starve the orgs
    # that happen to sort last.
    for org_id in _rotate_orgs(list(users_by_org)):
        org_users = users_by_org[org_id]
        # Global cap reached — remaining orgs get their turn next run (the
        # rotation above is what makes that fair across orgs; the
        # longest-waiting-first ordering in the query is fair within one).
        if total >= BACKFILL_MAX_TOTAL:
            break
        try:
            to_enqueue = _claim_org_channels(
                org_users,
                min(BACKFILL_MAX_PER_ORG, BACKFILL_MAX_TOTAL - total),
                has_slack,
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
