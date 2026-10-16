"""One-time login handoff tokens, redeemed by the frontend at /api/auth/handoff.

Used by flows that authenticate the user outside Auth.js (GitHub one-click
signup, SAML SSO) and then need to establish a NextAuth session.
"""

import hashlib
import secrets

HANDOFF_TTL_SEC = 120


def mint_handoff(cur, user_id: str) -> str:
    """Store a hashed one-time login token on the user row; return the raw token.

    Expiry uses Postgres ``NOW()`` — the clock redemption compares against —
    so app-vs-DB clock skew can't shrink or stretch the window.
    """
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    cur.execute(
        """UPDATE users
              SET signup_handoff_hash = %s,
                  signup_handoff_expires_at = NOW() + make_interval(secs => %s)
            WHERE id = %s""",
        (token_hash, HANDOFF_TTL_SEC, user_id),
    )
    return token
