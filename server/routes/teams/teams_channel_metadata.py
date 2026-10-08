"""LLM channel descriptions for Microsoft Teams (shared store + backfill pattern)."""

import logging

from celery_config import celery_app
from routes.slack.slack_backfill_config import BACKFILL_STALE_MINUTES
from services.channels import metadata_store
from services.channels.metadata_llm import generate_summary
from utils.log_sanitizer import sanitize

logger = logging.getLogger(__name__)

_PROVIDER = "teams"
_PLATFORM_DISPLAY = "Microsoft Teams"
_FIELDS_HINT = "name, description"
_REQUEST_TYPE = "teams_channel_metadata"

BACKFILL_MAX_PER_ORG = 25
BACKFILL_MAX_TOTAL = 200
BACKFILL_MAX_SCAN_PER_ORG = 500


class ChannelUnreadable(RuntimeError):
    pass


def _build_context(client, team_id: str, channel_id: str) -> tuple[str, str, dict]:
    info = client.get_channel(team_id, channel_id)
    if not info:
        raise ChannelUnreadable("channel not found")
    name = info.get("displayName") or ""
    desc = info.get("description") or ""
    parts = [f"Channel name: {name}"]
    if desc:
        parts.append(f"Description: {desc}")
    try:
        messages = client.list_channel_messages(team_id, channel_id, limit=15)
        texts = []
        for m in messages:
            body = (m.get("body") or {}).get("content") or ""
            if body.strip():
                texts.append(body.strip()[:200])
        if texts:
            parts.append("Recent messages:\n" + "\n".join(f"- {t}" for t in texts[:15]))
    except Exception as e:
        logger.warning("[TeamsChannelMeta] history failed for %s: %s", sanitize(channel_id), e)
    ch = {"name": name, "description": desc, "team_id": team_id, "id": channel_id}
    return "\n".join(parts), name, ch


def _record_exhausted_retry(user_id: str, channel_id: str, exc: BaseException) -> None:
    status = "pending" if isinstance(exc, ChannelUnreadable) else "error"
    metadata_store.update_metadata(user_id, _PROVIDER, channel_id, None, status)


@celery_app.task(
    name="routes.teams.teams_channel_metadata.generate_channel_metadata",
    bind=True,
    max_retries=2,
)
def generate_channel_metadata(self, user_id: str, channel_id: str, team_id: str | None = None):
    from utils.hooks import get_hook
    from utils.auth.stateless_auth import get_org_id_for_user
    from connectors.teams_connector.client import get_teams_client_for_user
    from routes.teams.teams_channels import _classify_channel, _team_id_for_channel

    org_id = get_org_id_for_user(user_id) if user_id else None
    allowed, message = get_hook("before_llm_call")(org_id, user_id)
    if not allowed:
        metadata_store.update_metadata(user_id, _PROVIDER, channel_id, None, "limit_reached")
        return

    try:
        if not metadata_store.claim_for_generation(
            user_id, _PROVIDER, channel_id, allow_generating=bool(self.request.retries),
        ):
            return
        client = get_teams_client_for_user(user_id)
        if not client:
            metadata_store.update_metadata(user_id, _PROVIDER, channel_id, None, "pending")
            return
        tid = team_id or _team_id_for_channel(user_id, channel_id)
        if not tid:
            metadata_store.update_metadata(user_id, _PROVIDER, channel_id, None, "pending")
            return
        context_text, _, info = _build_context(client, tid, channel_id)
        channel_type, platform = _classify_channel(
            info.get("name") or "", info.get("description") or "",
        )
        summary = generate_summary(
            user_id,
            platform_display=_PLATFORM_DISPLAY,
            fields_hint=_FIELDS_HINT,
            context_text=context_text,
            request_type=_REQUEST_TYPE,
        )
        metadata_store.update_metadata(
            user_id, _PROVIDER, channel_id, summary, "ready", channel_type, platform,
        )
    except Exception as e:
        logger.exception("[TeamsChannelMeta] failed for %s", sanitize(channel_id))
        try:
            self.retry(countdown=30)
        except self.MaxRetriesExceededError:
            _record_exhausted_retry(user_id, channel_id, e)


@celery_app.task(
    name="routes.teams.teams_channel_metadata.auto_register_channels_task",
    bind=True,
    max_retries=2,
)
def auto_register_channels_task(self, user_id: str, team_id: str | None = None):
    try:
        from routes.teams.teams_channels import auto_register_channels
        auto_register_channels(user_id, team_id=team_id)
    except Exception as exc:
        try:
            self.retry(countdown=30, exc=exc)
        except self.MaxRetriesExceededError:
            logger.warning("[TeamsAutoRegister] gave up for user %s", sanitize(user_id))


@celery_app.task(
    name="routes.teams.teams_channel_metadata.bulk_activate_channels_task",
    bind=True,
    max_retries=2,
)
def bulk_activate_channels_task(self, user_id: str, channel_ids: list[str]):
    try:
        from routes.teams.teams_channels import _activate_channels

        activated = _activate_channels(user_id, channel_ids)
        logger.info("[TeamsBulkActivate] activated %d/%d for user %s",
                    activated, len(channel_ids), sanitize(user_id))
    except Exception as exc:
        try:
            self.retry(countdown=30, exc=exc)
        except self.MaxRetriesExceededError:
            logger.warning("[TeamsBulkActivate] gave up for user %s", sanitize(user_id))


_BACKFILL_LOG = "[TeamsChannelBackfill]"


def _teams_connected(user_id: str) -> bool:
    from chat.backend.agent.tools.teams_tool import is_teams_connected
    return is_teams_connected(user_id)


@celery_app.task(name="routes.teams.teams_channel_metadata.backfill_channel_descriptions")
def backfill_channel_descriptions():
    return _backfill_channel_descriptions()


def _backfill_channel_descriptions():
    from routes.teams.teams_channels import _enqueue_metadata, _mark_pending, _team_id_for_channel
    from utils.auth.stateless_auth import set_rls_context, users_by_org
    from utils.db.connection_pool import db_pool

    try:
        org_users_map = users_by_org()
    except Exception:
        logger.warning("%s could not enumerate orgs", _BACKFILL_LOG, exc_info=True)
        return {"orgs": 0, "enqueued": 0}

    total = 0
    orgs_touched = 0
    for org_id, org_users in org_users_map.items():
        if total >= BACKFILL_MAX_TOTAL:
            break
        actor = next((u for u in org_users if _teams_connected(u)), None)
        if not actor:
            continue
        try:
            with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
                if not set_rls_context(cur, conn, actor, log_prefix=_BACKFILL_LOG):
                    continue
                cur.execute(
                    """SELECT channel_id, user_id FROM (
                           SELECT DISTINCT ON (channel_id) channel_id, user_id, updated_at
                             FROM slack_channels
                            WHERE provider = %s AND is_member
                              AND (
                                    metadata_status = 'skipped'
                                 OR (metadata_status IN ('pending', 'generating')
                                     AND updated_at < NOW() - make_interval(mins => %s))
                              )
                            ORDER BY channel_id, updated_at ASC
                       ) d
                       ORDER BY d.updated_at ASC
                       LIMIT %s""",
                    (_PROVIDER, BACKFILL_STALE_MINUTES, min(BACKFILL_MAX_PER_ORG, BACKFILL_MAX_TOTAL - total)),
                )
                rows = cur.fetchall()
                if not rows:
                    continue
                channel_ids = [r[0] for r in rows]
                claimed = _mark_pending(cur, channel_ids, stale_minutes=BACKFILL_STALE_MINUTES)
                conn.commit()
            for channel_id in claimed:
                owner = next((r[1] for r in rows if r[0] == channel_id), actor)
                _enqueue_metadata(owner, channel_id, _team_id_for_channel(owner, channel_id))
            total += len(claimed)
            if claimed:
                orgs_touched += 1
        except Exception:
            logger.warning("%s sweep failed for org", _BACKFILL_LOG, exc_info=True)
    return {"orgs": orgs_touched, "enqueued": total}
