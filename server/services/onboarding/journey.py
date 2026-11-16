"""Each new account's setup journey: who their guide is, what they have
connected, and which setup emails went out.

Tables (created in utils/db/db_utils.py):

* ``onboarding_journeys``: one row per user. Rows made lazily for existing
  users (so the app can show them a guide) never get emails.
* ``onboarding_emails``: one row per email sent, unique per (user, kind).
  The row is written before the email goes out, so a crash at the wrong
  moment can lose an email but never sends one twice.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
    return datetime.now(timezone.utc)


def _first_name(name: str | None) -> str | None:
    first = (name or "").strip().split(" ")[0]
    if first and "@" not in first and not any(ch.isdigit() for ch in first):
        return first[:1].upper() + first[1:]
    return None


def _assign_guide(cur) -> str | None:
    # Balance on the accounts guides actually look after (the ones that get
    # emails), not everyone who has opened the app.
    cur.execute(
        """SELECT guide_key, COUNT(*) FROM onboarding_journeys
            WHERE guide_key IS NOT NULL AND emails_enabled GROUP BY guide_key"""
    )
    g = guide_mod.pick(dict(cur.fetchall()))
    return g.key if g else None


def _via_from_user(gh_id, slack_team) -> str | None:
    if gh_id:
        return "github"
    if slack_team:
        return "slack"
    return None


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
        with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
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
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT u.name, u.org_id, u.github_user_id, u.slack_team_id, j.via, j.guide_key,
                      COALESCE(u.created_at > NOW() - (%s * INTERVAL '1 day'), FALSE)
                 FROM users u LEFT JOIN onboarding_journeys j ON j.user_id = u.id
                WHERE u.id = %s""",
            (schedule.JOURNEY_LENGTH.days, user_id),
        )
        row = cur.fetchone()
        if not row:
            return {"guide": None, "via": None, "first_name": None, "team": guide_mod.team_name(), "recent": False}
        name, org_id, gh_id, slack_team, via, guide_key, recent = row
        via = via or _via_from_user(gh_id, slack_team)
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
    # recent: still in the first weeks, when the in-app setup companion shows.
    return {"guide": guide.public() if guide else None, "via": via, "first_name": _first_name(name),
            "team": guide_mod.team_name(), "recent": bool(recent)}


def unsubscribe(user_id: str) -> None:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE onboarding_journeys SET unsubscribed_at = COALESCE(unsubscribed_at, NOW()) WHERE user_id = %s",
            (user_id,),
        )
        conn.commit()


# ── The sweep ──


def active_journeys() -> list[str]:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT user_id FROM onboarding_journeys
                WHERE emails_enabled AND unsubscribed_at IS NULL
                  AND created_at > NOW() - (%s * INTERVAL '1 day')
                ORDER BY created_at""",
            (schedule.JOURNEY_LENGTH.days,),
        )
        return [r[0] for r in cur.fetchall()]


def journeys_in_org(org_id: str) -> list[str]:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT user_id FROM onboarding_journeys
                WHERE org_id = %s AND emails_enabled AND unsubscribed_at IS NULL
                  AND created_at > NOW() - (%s * INTERVAL '1 day')""",
            (org_id, schedule.JOURNEY_LENGTH.days),
        )
        return [r[0] for r in cur.fetchall()]


@dataclass(frozen=True)
class _Checked:
    connected: frozenset[str]
    # Tools whose own check failed or timed out: unknown, not disconnected.
    unknown: frozenset[str]


def _connected_providers(user_id: str, org_id: str) -> _Checked | None:
    """Live connector checks, as the Connectors page does. None if they all failed."""
    try:
        from routes.connector_status import _check_all_connectors
        from routes.slack.slack_onboarding import _github_app_ready
        results = _check_all_connectors(user_id, org_id, app_runtime_ready=_github_app_ready())
    except Exception:
        logger.warning("[ONBOARDING] connector check failed for user_hash=%s", hash_for_log(user_id), exc_info=True)
        return None
    ours = {p: st for p, st in results.items() if p in ALL_PROVIDERS}
    return _Checked(
        connected=frozenset(p for p, st in ours.items() if st.get("connected")),
        unknown=frozenset(p for p, st in ours.items() if st.get("check_failed")),
    )


@dataclass
class _Row:
    org_id: str
    via: str | None
    guide: guide_mod.Guide
    created_at: datetime
    unsubscribed: bool
    known: set[str]
    pending: list[str]
    pending_since: datetime | None
    email: str
    name: str | None
    verified: bool


def _load_locked(cur, user_id: str) -> _Row | None:
    cur.execute(
        """SELECT j.org_id, j.via, j.guide_key, j.created_at, j.unsubscribed_at,
                  j.known_connectors, j.pending_parts, j.pending_since, j.emails_enabled,
                  u.email, u.name, COALESCE(u.email_verified, FALSE)
             FROM onboarding_journeys j JOIN users u ON u.id = j.user_id
            WHERE j.user_id = %s
            FOR UPDATE OF j""",
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        return None
    (org_id, via, guide_key, created_at, unsub_at, known, pending, pending_since,
     enabled, email, name, verified) = row
    guide = guide_mod.by_key(guide_key)
    if not (enabled and guide and org_id and email):
        return None
    return _Row(org_id, via, guide, created_at, unsub_at is not None, set(known or []),
                list(pending or []), pending_since, email, name, bool(verified))


def _has_stored(cur, org_id: str) -> bool:
    """Any connector credentials or links saved for the org, checked or not."""
    cur.execute(
        """SELECT EXISTS (SELECT 1 FROM user_tokens
                           WHERE org_id = %s AND is_active AND secret_ref IS NOT NULL AND provider = ANY(%s))
               OR EXISTS (SELECT 1 FROM user_connections WHERE org_id = %s AND status = 'active' AND provider = ANY(%s))
               OR EXISTS (SELECT 1 FROM user_github_installations WHERE org_id = %s AND disconnected_at IS NULL)
               OR EXISTS (SELECT 1 FROM kubeconfig_clusters WHERE org_id = %s AND is_active)
               OR EXISTS (SELECT 1 FROM kubectl_agent_tokens WHERE org_id = %s AND status = 'active')
               OR EXISTS (SELECT 1 FROM user_manual_vms WHERE org_id = %s)""",
        (org_id, list(ALL_PROVIDERS), org_id, list(ALL_PROVIDERS), org_id, org_id, org_id, org_id),
    )
    return bool(cur.fetchone()[0])


def _sent(cur, user_id: str) -> dict[str, datetime]:
    cur.execute("SELECT kind, sent_at FROM onboarding_emails WHERE user_id = %s", (user_id,))
    return dict(cur.fetchall())


def _note_new_connections(r: _Row, providers: frozenset[str], sent: dict) -> None:
    """Queue newly connected parts for a "connected" email.

    What is connected at the first look, before the welcome goes out, is the
    starting point the welcome already describes, not news.
    """
    new = sorted(providers - r.known)
    if not new or (not r.known and "welcome" not in sent):
        return
    from services.onboarding.sender import ping_team
    ping_team(f"{r.email} connected {', '.join(label(p) for p in new)}.")
    # A second tool in a part that was already connected (or announced) isn't news.
    already = set(parts_connected(frozenset(r.known))) | {k.split(":", 1)[1] for k in sent if k.startswith("connected:")}
    for part in dict.fromkeys(part_of(p) for p in new):
        if part and part not in already and part not in r.pending:
            r.pending.append(part)
            r.pending_since = r.pending_since or _now()


def _journey_org(user_id: str) -> str | None:
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT org_id FROM onboarding_journeys WHERE user_id = %s AND emails_enabled", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None


@dataclass
class _Claim:
    kind: str
    kinds: list[str]
    to: str
    guide: guide_mod.Guide
    subject: str
    text: str
    html: str
    unsubscribe_url: str


def _claim_due(cur, user_id: str, r: _Row, providers: frozenset[str], state: schedule.JourneyState) -> _Claim | None:
    """Pick the email that is due and record it as sent, before sending.

    Committed with the rest of the journey update, so a crash between here
    and the provider loses this email instead of sending it twice.
    """
    from services.onboarding.sender import unsubscribe_url
    due = schedule.due_email(state, _now())
    if not due:
        return None
    ctx = EmailContext(
        guide=r.guide,
        first_name=_first_name(r.name),
        via=r.via,
        providers=providers,
        parts_done=tuple(state.parts_done),
        setup_url=f"{(os.getenv('FRONTEND_URL') or '').rstrip('/')}/onboarding",
        unsubscribe_url=unsubscribe_url(user_id),
    )
    msg = compose(due.kind, ctx, due.parts)
    kinds = [f"connected:{p}" for p in due.parts] if due.kind == "connected" else [due.kind]
    cur.execute(
        """INSERT INTO onboarding_emails (user_id, kind, subject)
           SELECT %s, k, %s FROM unnest(%s::text[]) AS k
           ON CONFLICT (user_id, kind) DO NOTHING RETURNING kind""",
        (user_id, msg.subject[:255], kinds),
    )
    if len(cur.fetchall()) != len(kinds):
        return None  # another run already recorded it
    return _Claim(due.kind, kinds, r.email, r.guide, msg.subject, msg.text, msg.html, ctx.unsubscribe_url)


def _release(user_id: str, kinds: list[str]) -> None:
    """The provider refused the email: drop the claim so a later run tries again."""
    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM onboarding_emails WHERE user_id = %s AND kind = ANY(%s)", (user_id, kinds))
        conn.commit()


def evaluate(user_id: str) -> str | None:
    """Look at one journey, note new connections, send whatever is due.

    Returns the kind of email sent, if any. Safe to run twice at once: the
    row lock serializes the decision and onboarding_emails is unique per
    kind. The live checks and the send both happen outside the lock.
    """
    from services.onboarding.sender import emails_enabled, ping_team, send
    from utils.auth.stateless_auth import set_rls_context
    if not emails_enabled():
        return None
    org_id = _journey_org(user_id)
    if not org_id:
        return None
    checked = _connected_providers(user_id, org_id)

    with db_pool.get_admin_connection() as conn, conn.cursor() as cur:
        # The stored-credential tables are row-level secured, so _has_stored
        # sees nothing without this outside a request. It commits, so it goes
        # before the lock.
        if not set_rls_context(cur, conn, user_id, org_id=org_id, log_prefix="[ONBOARDING]"):
            return None
        r = _load_locked(cur, user_id)
        if r is None:
            conn.rollback()
            return None
        sent = _sent(cur, user_id)
        # A part announced by an earlier run that stopped before clearing it.
        r.pending = [p for p in r.pending if f"connected:{p}" not in sent]
        r.pending_since = r.pending_since if r.pending else None
        if checked is None:
            # Nothing is known about connections, but the welcome doesn't need them.
            if "welcome" in sent:
                conn.rollback()
                return None
            providers, unknown = frozenset(r.known), ALL_PROVIDERS
        else:
            providers, unknown = checked.connected, checked.unknown
            _note_new_connections(r, providers, sent)
        ever = providers | r.known
        state = schedule.JourneyState(
            enrolled_at=r.created_at,
            email_verified=r.verified,
            sent=sent,
            parts_done=parts_connected(providers),
            parts_ever=parts_connected(ever),
            pending_parts=r.pending,
            pending_since=r.pending_since,
            has_stored=_has_stored(cur, r.org_id),
            parts_unknown=[p for p in dict.fromkeys(part_of(x) for x in sorted(unknown)) if p],
            unsubscribed=r.unsubscribed,
        )
        claim = _claim_due(cur, user_id, r, providers, state)
        cur.execute(
            """UPDATE onboarding_journeys
                  SET known_connectors = %s, pending_parts = %s, pending_since = %s, last_checked_at = NOW()
                WHERE user_id = %s""",
            (sorted(ever), r.pending, r.pending_since, user_id),
        )
        conn.commit()

    if claim is None:
        return None
    if not send(claim.to, claim.guide, claim.subject, claim.text, claim.html, claim.unsubscribe_url):
        _release(user_id, claim.kinds)
        return None
    ping_team(f"Sent \"{claim.subject}\" to {claim.to} from {claim.guide.first_name}.")
    return claim.kind
