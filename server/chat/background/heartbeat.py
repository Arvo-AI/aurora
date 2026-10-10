"""Keep a background session alive while its workflow is awaiting a model.

Streaming saves report progress, but a healthy model call can be silent for
minutes. This heartbeat reports worker liveness independently of model output.
The caller's workflow timeout still bounds how long the worker may wait.
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress


logger = logging.getLogger(__name__)
HEARTBEAT_INTERVAL_SECONDS = 30


def _touch_session(session_id: str, user_id: str) -> None:
    """Refresh only this user's active session; never reopen a terminal one."""
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    try:
        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cursor:
                if not set_rls_context(
                    cursor, conn, user_id, log_prefix="[BackgroundChat:Heartbeat]",
                ):
                    return
                cursor.execute(
                    """UPDATE chat_sessions
                       SET updated_at = GREATEST(updated_at, NOW())
                       WHERE id = %s AND user_id = %s AND status = 'in_progress'""",
                    (session_id, user_id),
                )
            conn.commit()
    except Exception:
        logger.warning("[BackgroundChat:Heartbeat] Could not refresh session", exc_info=True)


@asynccontextmanager
async def background_session_heartbeat(session_id: str, user_id: str):
    """Pulse during this workflow only, including silent model waits."""
    await asyncio.to_thread(_touch_session, session_id, user_id)

    async def pulse():
        """Refresh session activity at a fixed interval until cancelled."""
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
            await asyncio.to_thread(_touch_session, session_id, user_id)

    heartbeat = asyncio.create_task(pulse(), name="background-session-heartbeat")
    try:
        yield
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
