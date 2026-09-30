"""
Celery task to generate LLM-powered descriptions for Slack channels.

Mirrors :mod:`routes.github.github_repo_metadata`. Fetches a channel's
topic/purpose + a small sample of recent messages, then asks an LLM for a
2-3 sentence description of what the channel is for and which team/service it
serves. The result feeds the agent's channel-routing decisions.
"""
import logging

from celery_config import celery_app
from chat.backend.agent.utils.message_content import extract_text_from_content
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
# flight, so re-enqueueing it would only duplicate the LLM call. Matches the
# staleness window the reconcile pass uses.
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


def _users_by_org() -> dict[str, list[str]]:
    """Map org_id -> [user_id, ...]. ``users`` is NOT RLS-protected, so it can be
    read before any org context is set — which is how a cross-org task discovers
    the orgs it has to iterate (see the RLS notes in AGENTS.md)."""
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id, id FROM users WHERE org_id IS NOT NULL ORDER BY org_id, id"
            )
            by_org: dict[str, list[str]] = {}
            for org_id, user_id in cur.fetchall():
                by_org.setdefault(org_id, []).append(user_id)
            return by_org


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
    from utils.db.connection_pool import db_pool
    from utils.auth.stateless_auth import set_rls_context
    from connectors.slack_connector.client import get_slack_client_for_user
    from routes.slack.slack_channels import _enqueue_metadata, _mark_pending

    log_prefix = "[SlackDescBackfill]"

    # Per-run memo: probe each user's Slack credentials at most once (each probe
    # is a DB + Vault round trip).
    connected: dict[str, bool] = {}

    def _has_slack(uid: str) -> bool:
        if uid not in connected:
            try:
                connected[uid] = get_slack_client_for_user(uid) is not None
            except Exception:
                connected[uid] = False
        return connected[uid]

    try:
        users_by_org = _users_by_org()
    except Exception:
        logger.warning("%s could not enumerate orgs", log_prefix, exc_info=True)
        return {"orgs": 0, "enqueued": 0}

    total = 0
    orgs_touched = 0
    for _org_id, org_users in users_by_org.items():
        # Global cap reached — the remaining orgs get their turn next run (the
        # ORDER BY updated_at in the query above keeps that fair).
        if total >= BACKFILL_MAX_TOTAL:
            break
        try:
            # Read pass. Kept in its own short-lived block so the pooled
            # connection isn't held across the Vault round trips the credential
            # probe below makes.
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    # No org context resolvable for this user (e.g. org deleted
                    # mid-sweep) — skip rather than query with the wrong context.
                    if not set_rls_context(cur, conn, org_users[0], log_prefix=log_prefix):
                        continue
                    rows = _select_undescribed_channels(
                        cur, min(BACKFILL_MAX_PER_ORG, BACKFILL_MAX_TOTAL - total)
                    )
            if not rows:
                continue

            # Enqueue under the row's owner — the identity whose token registered
            # the channel. Only if they've since disconnected Slack do we look for
            # another connected org member (credentials are org-shared), so a
            # channel isn't stranded by one person leaving.
            owners = {owner_id for _cid, owner_id in rows}
            fallback = None
            if any(not _has_slack(owner_id) for owner_id in owners):
                fallback = next((uid for uid in org_users if _has_slack(uid)), None)

            to_enqueue = []
            for channel_id, owner_id in rows:
                actor = owner_id if _has_slack(owner_id) else fallback
                # Nobody in the org can reach Slack any more — leave the rows
                # pending rather than queueing a task that would only mark them
                # 'error' (which is never auto-retried).
                if actor:
                    to_enqueue.append((actor, channel_id))
            if not to_enqueue:
                continue

            # Write pass. Committed BEFORE enqueueing so the worker can never pick
            # a task up against a row that still looks unqueued (or, for a
            # 'skipped' row, one whose status would reject the description write).
            with db_pool.get_admin_connection() as conn:
                with conn.cursor() as cur:
                    if not set_rls_context(cur, conn, org_users[0], log_prefix=log_prefix):
                        continue
                    _mark_pending(cur, [cid for _actor, cid in to_enqueue])
                    conn.commit()
        except Exception:
            logger.warning("%s sweep failed for one org", log_prefix, exc_info=True)
            continue

        for actor, channel_id in to_enqueue:
            _enqueue_metadata(actor, channel_id)
        total += len(to_enqueue)
        orgs_touched += 1

    if total:
        logger.info("%s enqueued %d description(s) across %d org(s)",
                    log_prefix, total, orgs_touched)
    return {"orgs": orgs_touched, "enqueued": total}
