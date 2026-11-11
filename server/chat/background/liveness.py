"""Liveness heartbeat for a background chat task.

The cleanup sweep has to decide whether the worker running an investigation is
still alive. What it had to go on were side effects of progress: a saved
thought, a stream event, a tool call marked running. None of those move while
the model works through one long step, so a healthy investigation looked dead
after three quiet minutes and its incident got a false "worker died" notice.

This is the missing signal. A timer in the worker process touches the session
every 30 seconds for as long as the task runs, whatever the model, the tools
or the event loop are doing. It runs on its own thread, so a blocking call on
the event loop cannot stop it. When the process dies the beats stop with it,
and the sweep sees a stale session within its usual threshold.

It does not detect a task that is alive but stuck (a model call that never
returns): that is what the investigation time limit is for.
"""

import logging
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

HEARTBEAT_INTERVAL_SECONDS = 30
_LOG = "[BackgroundChat:Heartbeat]"


def touch_session(session_id: str, user_id: str) -> bool:
    """Mark an in-progress session as alive. Never reopens a finished one.

    Returns True when the session row was touched.
    """
    from utils.auth.stateless_auth import set_rls_context
    from utils.db.connection_pool import db_pool

    with db_pool.get_admin_connection() as conn:
        with conn.cursor() as cursor:
            # Without the RLS variables the UPDATE silently matches no row.
            if not set_rls_context(cursor, conn, user_id, log_prefix=_LOG):
                return False
            cursor.execute(
                "UPDATE chat_sessions SET updated_at = NOW() WHERE id = %s AND status = 'in_progress'",
                (session_id,),
            )
            touched = cursor.rowcount == 1
        conn.commit()
    return touched


def _beat_until_stopped(session_id: str, user_id: str, interval: float, stop: threading.Event) -> None:
    # The first beat is immediate: a task that waited in the queue starts with a
    # session that already looks stale.
    while True:
        try:
            touch_session(session_id, user_id)
        except Exception:
            # One missed beat is harmless; the sweep allows several.
            logger.warning("%s could not refresh session %s", _LOG, session_id, exc_info=True)
        if stop.wait(interval):
            return


@contextmanager
def session_heartbeat(
    session_id: Optional[str],
    user_id: Optional[str],
    interval: float = HEARTBEAT_INTERVAL_SECONDS,
) -> Iterator[None]:
    """Beat for the duration of the ``with`` block, then stop.

    The caller's own error, timeout or cancellation all end the block, so the
    beats can never outlive the task they vouch for.
    """
    if not session_id or not user_id:
        yield
        return

    stop = threading.Event()
    thread = threading.Thread(
        target=_beat_until_stopped,
        args=(str(session_id), str(user_id), interval, stop),
        name=f"session-heartbeat-{str(session_id)[:8]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        # A beat caught mid-query finishes on its own; the thread is a daemon.
        thread.join(timeout=5)
