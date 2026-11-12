"""Each new account's setup journey: who their guide is, what they have
connected, and which setup emails went out.

Tables (created in utils/db/db_utils.py):

* ``onboarding_journeys``: one row per user. Rows made lazily for existing
  users (so the app can show them a guide) never get emails.
* ``onboarding_emails``: one row per email sent, unique per (user, kind).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from services.onboarding import guides as guide_mod
from services.onboarding import schedule
from services.onboarding.emails import EmailContext, compose
from services.onboarding.stack import ALL_PROVIDERS, label, part_of, parts_connected
from utils.db.connection_pool import db_pool
from utils.log_sanitizer import hash_for_log

logger = logging.getLogger(__name__)

# Signup paths that create a brand-new org and someone who can connect tools.
_EMAILED_METHODS = {"password", "github", "slack"}


def _skip_domains() -> set[str]:
    raw = os.getenv("ONBOARDING_SKIP_EMAIL_DOMAINS", "")
    return {d.strip().lower().lstrip("@") for d in raw.split(",") if d.strip()}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _first_name(name: str | None, email: str | None) -> str | None:
    first = (name or "").strip().split(" ")[0]
    if first and "@" not in first and not any(ch.isdigit() for ch in first):
        return first[:1].upper() + first[1:]
    return None


def _assign_guide(cur) -> str | None:
    cur.execute("SELECT guide_key, COUNT(*) FROM onboarding_journeys WHERE guide_key IS NOT NULL GROUP BY guide_key")
    counts = {k: n for k, n in cur.fetchall()}
    g = guide_mod.pick(counts)
    return g.key if g else None


# ── Enrollment ──


def on_user_created(user_id: str, org_id: str | None, email: str, method: str, new_org: bool) -> None:
    """Start a journey for someone who just made a new account. Never raises."""
    try:
        if not (new_org and org_id and method in _EMAILED_METHODS):
            return
        if not guide_mod.load_guides():
            return
        from services.onboarding.sender import emails_enabled, ping_team
        domain = (email or "").rsplit("@", 1)[-1].lower()
        send = emails_enabled() and domain not in _skip_domains()
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                guide_key = _assign_guide(cur)
                cur.execute(
                    """INSERT INTO onboarding_journeys (user_id, org_id, via, guide_key, emails_enabled)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (user_id) DO NOTHING""",
                    (user_id, org_id, method, guide_key, send),
                )
            conn.commit()
        guide = guide_mod.by_key(guide_key)
        ping_team(f"New signup via {method}: {email}. Guide: {guide.name if guide else 'none'}.")
        if send:
            from services.onboarding.tasks import evaluate_journey_task
            evaluate_journey_task.apply_async(args=[user_id], countdown=int(schedule.WELCOME_DELAY.total_seconds()) + 15)
    except Exception:
        logger.exception("[ONBOARDING] could not start a journey for user_hash=%s", hash_for_log(user_id))


def journey_for_app(user_id: str) -> dict:
    """What the setup page needs: the guide, how they signed up, a first name.

    Users from before journeys existed get a row here, with a guide and no emails.
    """
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT u.name, u.email, u.org_id, u.github_user_id, u.slack_team_id,
                          j.via, j.guide_key
                     FROM users u LEFT JOIN onboarding_journeys j ON j.user_id = u.id
                    WHERE u.id = %s""",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                return {"guide": None, "via": None, "first_name": None}
            name, email, org_id, gh_id, slack_team, via, guide_key = row
            if via is None:
                via = "github" if gh_id else "slack" if slack_team else None
            if guide_mod.load_guides() and not guide_mod.by_key(guide_key):
                guide_key = _assign_guide(cur)
                cur.execute(
                    """INSERT INTO onboarding_journeys (user_id, org_id, via, guide_key, emails_enabled)
                       VALUES (%s, %s, %s, %s, FALSE)
                       ON CONFLICT (user_id) DO UPDATE SET guide_key = EXCLUDED.guide_key""",
                    (user_id, org_id, via, guide_key),
                )
                conn.commit()
    guide = guide_mod.by_key(guide_key)
    return {
        "guide": guide.public() if guide else None,
        "via": via,
        "first_name": _first_name(name, email),
    }


def unsubscribe(user_id: str) -> None:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE onboarding_journeys SET unsubscribed_at = COALESCE(unsubscribed_at, NOW()) WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()


# ── The sweep ──


def active_journeys() -> list[str]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id FROM onboarding_journeys
                    WHERE emails_enabled AND unsubscribed_at IS NULL
                      AND created_at > NOW() - (%s * INTERVAL '1 day')
                    ORDER BY created_at""",
                (schedule.JOURNEY_LENGTH.days,),
            )
            return [r[0] for r in cur.fetchall()]


def journeys_in_org(org_id: str) -> list[str]:
    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT user_id FROM onboarding_journeys
                    WHERE org_id = %s AND emails_enabled AND unsubscribed_at IS NULL
                      AND created_at > NOW() - (%s * INTERVAL '1 day')""",
                (org_id, schedule.JOURNEY_LENGTH.days),
            )
            return [r[0] for r in cur.fetchall()]


def _connected_providers(user_id: str, org_id: str) -> frozenset[str] | None:
    """Live connector checks, as the Connectors page does. None if they failed."""
    try:
        from routes.connector_status import _check_all_connectors
        from routes.slack.slack_onboarding import _github_app_ready
        results = _check_all_connectors(user_id, org_id, app_runtime_ready=_github_app_ready())
    except Exception:
        logger.warning("[ONBOARDING] connector check failed for user_hash=%s", hash_for_log(user_id), exc_info=True)
        return None
    return frozenset(p for p, s in results.items() if s.get("connected") and p in ALL_PROVIDERS)


def evaluate(user_id: str) -> str | None:
    """Look at one journey, note new connections, send whatever is due.

    Returns the kind of email sent, if any. Safe to run twice at once: the
    row lock serializes it and onboarding_emails is unique per kind.
    """
    from services.onboarding.sender import emails_enabled, ping_team, send, unsubscribe_url

    if not emails_enabled():
        return None

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT j.org_id, j.via, j.guide_key, j.created_at, j.unsubscribed_at,
                          j.known_connectors, j.pending_parts, j.emails_enabled,
                          u.email, u.name, COALESCE(u.email_verified, FALSE)
                     FROM onboarding_journeys j JOIN users u ON u.id = j.user_id
                    WHERE j.user_id = %s
                    FOR UPDATE OF j""",
                (user_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                return None
            (org_id, via, guide_key, created_at, unsub_at, known, pending,
             enabled, email, name, verified) = row
            guide = guide_mod.by_key(guide_key)
            if not (enabled and guide and org_id and email):
                conn.rollback()
                return None

            providers = _connected_providers(user_id, org_id)
            if providers is None:
                conn.rollback()
                return None

            known_set = set(known or [])
            first_look = not known_set and "welcome" not in _sent_kinds(cur, user_id)
            new = sorted(providers - known_set)
            pending_parts = list(pending or [])
            if new and not first_look:
                for p in new:
                    part = part_of(p)
                    if part and part not in pending_parts:
                        pending_parts.append(part)
                ping_team(f"{email} connected {', '.join(label(p) for p in new)}.")

            sent = _sent(cur, user_id)
            state = schedule.JourneyState(
                enrolled_at=created_at,
                email_verified=bool(verified),
                sent=sent,
                parts_done=parts_connected(providers),
                pending_parts=pending_parts,
                unsubscribed=unsub_at is not None,
            )
            due = schedule.due_email(state, _now())

            kind_sent = None
            if due:
                ctx = EmailContext(
                    guide=guide,
                    first_name=_first_name(name, email),
                    via=via,
                    providers=providers,
                    parts_done=tuple(state.parts_done),
                    setup_url=f"{(os.getenv('FRONTEND_URL') or '').rstrip('/')}/onboarding",
                    unsubscribe_url=unsubscribe_url(user_id),
                )
                msg = compose(due.kind, ctx, due.parts)
                if send(email, guide, msg.subject, msg.text, msg.html, ctx.unsubscribe_url):
                    kinds = [f"connected:{p}" for p in due.parts] if due.kind == "connected" else [due.kind]
                    for k in kinds:
                        cur.execute(
                            """INSERT INTO onboarding_emails (user_id, kind, subject)
                               VALUES (%s, %s, %s) ON CONFLICT (user_id, kind) DO NOTHING""",
                            (user_id, k, msg.subject[:255]),
                        )
                    if due.kind == "connected":
                        pending_parts = [p for p in pending_parts if p not in due.parts]
                    kind_sent = due.kind
                    ping_team(f"Sent \"{msg.subject}\" to {email} from {guide.first_name}.")

            cur.execute(
                """UPDATE onboarding_journeys
                      SET known_connectors = %s, pending_parts = %s, last_checked_at = NOW()
                    WHERE user_id = %s""",
                (sorted(providers | known_set), pending_parts, user_id),
            )
        conn.commit()
    return kind_sent


def _sent(cur, user_id: str) -> dict[str, datetime]:
    cur.execute("SELECT kind, sent_at FROM onboarding_emails WHERE user_id = %s", (user_id,))
    return {k: t for k, t in cur.fetchall()}


def _sent_kinds(cur, user_id: str) -> set[str]:
    return set(_sent(cur, user_id))
