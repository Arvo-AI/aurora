"""Pass or fail a connection check from the reply after SSH login."""

from typing import Optional, Tuple

CHANNEL_FAILED = "SSH login succeeded but the session channel failed."
SESSION_DROPPED = "SSH login succeeded but the session dropped."


def session_reply_result(
    result: str,
    error_output: str,
    target_user: str,
    *,
    transport_active: bool,
    channel_error: bool = False,
    exec_rejected: bool = False,
) -> Tuple[bool, Optional[str], Optional[str]]:
    # connect() already authenticated. A server may refuse the exec channel
    # after that, and the login is still a passed check. A dead transport is not.
    if exec_rejected and not result and not error_output and transport_active:
        return True, None, target_user

    # A timeout or a dropped read, with nothing in hand, is still a failed check.
    if channel_error and not result and not error_output:
        return False, CHANNEL_FAILED, None

    # Nothing came back and the session is gone
    if not result and not error_output and not transport_active:
        return False, SESSION_DROPPED, None

    # Error text from a network OS is not the login name
    connected_as = result if result == target_user else target_user
    return True, None, connected_as
