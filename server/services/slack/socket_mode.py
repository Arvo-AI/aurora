"""
Slack Socket Mode listener for Aurora.

Socket Mode is an alternative to the HTTP Events API: instead of Slack POSTing
events to a public HTTPS "Request URL", the app opens an *outbound* WebSocket to
Slack and Slack pushes events (mentions, interactions) down that persistent
connection. No inbound ingress, public hostname or TLS-terminated endpoint is
required — the app only needs outbound HTTPS to Slack.

This makes Slack work for private / self-hosted deployments where Aurora's
backend cannot be reached from the public internet (behind a firewall, in a
private VPC/cluster, air-gapped, on a laptop, etc.). The HTTP Events API path in
``routes/slack/slack_events.py`` is unaffected and remains the default; this
listener reuses the same transport-agnostic handlers
(``process_event_callback`` / ``process_interaction``) so behaviour is identical
across both transports.

Enablement:
- Set ``SLACK_APP_TOKEN`` (an app-level ``xapp-...`` token with the
  ``connections:write`` scope) to turn the listener on; leave it unset to stay
  on the HTTP Events API. Socket Mode must also be enabled in the Slack app
  config, and event subscriptions + interactivity turned on there.

One app-level token authenticates the whole Slack app, so a single connection
carries events from *every* workspace the app is installed in. Per-workspace
identity resolution still happens downstream via ``team_id`` in each payload
(``get_user_id_from_slack_team``), exactly like the webhook path.
"""

import logging
# Configure logging first, before importing app modules, to match main_chatbot.py.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Quiet the noisy transport loggers Socket Mode pulls in.
logging.getLogger("slack_sdk").setLevel(logging.WARNING)
logging.getLogger("websocket").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

import os
import signal
import threading

from dotenv import load_dotenv

load_dotenv()


def _dispatch(client, req) -> None:
    """Route one Socket Mode request to the shared Slack handlers.

    ``req`` is a slack_sdk ``SocketModeRequest``. We ack every envelope first
    (Slack disconnects a client that does not ack within ~3s), then process.
    For ``interactive`` requests the handler returns the same response body the
    HTTP interactivity endpoint would send, which we include in the ack so
    Block Kit message updates behave identically to the webhook path.
    """
    from slack_sdk.socket_mode.response import SocketModeResponse

    # Imported lazily so importing this module never drags in the Flask app
    # unless the listener actually runs.
    from routes.slack.slack_events import process_event_callback, process_interaction

    try:
        payload = req.payload or {}

        # Slack Events API delivery (app_mention, member_joined_channel, ...).
        if req.type == "events_api":
            # Ack immediately; the event is processed synchronously after, and
            # the handlers themselves never raise.
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            process_event_callback(payload)
            return

        # Interactivity (button clicks: Run/Details/Dismiss/View PR).
        if req.type == "interactive":
            # process_interaction returns the body Slack expects. An empty-text
            # body is the "do not replace the message" ack; a non-empty text
            # updates the originating message, same as the HTTP path.
            response_payload = process_interaction(payload)
            client.send_socket_mode_response(
                SocketModeResponse(envelope_id=req.envelope_id, payload=response_payload)
            )
            return

        # slash_commands / other request types are not used by Aurora today —
        # ack them so Slack does not retry, but do nothing else.
        client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        logger.debug("Ignoring unhandled Socket Mode request type: %s", req.type)

    except Exception:
        # Never let a handler error kill the listener thread. Best-effort ack so
        # Slack does not immediately retry a request we already logged.
        logger.exception("Error dispatching Socket Mode request (type=%s)", getattr(req, "type", "?"))
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.debug("Failed to ack Socket Mode envelope after dispatch error", exc_info=True)


def build_client():
    """Construct (but do not connect) the Socket Mode client.

    Socket Mode is enabled purely by the presence of an app-level token:
    set ``SLACK_APP_TOKEN`` (an ``xapp-...`` token with the
    ``connections:write`` scope) to turn the listener on. Returns None when the
    token is absent or malformed, so callers can log and skip.
    """
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    if not app_token:
        logger.info(
            "Slack Socket Mode not configured (SLACK_APP_TOKEN unset); skipping listener. "
            "Set an app-level token (xapp-...) with the connections:write scope to enable it."
        )
        return None

    # An app-level token always starts with xapp-; a bot token (xoxb-) here is a
    # common misconfiguration that fails with a confusing handshake error.
    if not app_token.startswith("xapp-"):
        logger.error(
            "SLACK_APP_TOKEN does not look like an app-level token (expected 'xapp-' prefix). "
            "Socket Mode needs the App-Level Token, not the bot/user OAuth token."
        )
        return None

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.web import WebClient

    # The WebClient here is only used by the SDK for the connection handshake
    # (apps.connections.open). Per-workspace message sending still goes through
    # Aurora's own SlackClient with the stored per-workspace bot token, so we do
    # NOT put a workspace bot token on this client.
    client = SocketModeClient(
        app_token=app_token,
        web_client=WebClient(),
    )
    client.socket_mode_request_listeners.append(_dispatch)
    return client


def run() -> None:
    """Blocking entrypoint: connect and serve until interrupted.

    Safe to call unconditionally — it returns immediately if Socket Mode is not
    enabled, so the container can always run this command.
    """
    client = build_client()
    if client is None:
        # Enabled-but-broken already logged; keep the process alive so a
        # restart loop does not hammer the scheduler, but do nothing.
        logger.info("Slack Socket Mode listener not started.")
        _sleep_forever()
        return

    logger.info("Starting Slack Socket Mode listener (outbound WebSocket to Slack)...")
    client.connect()
    logger.info("Slack Socket Mode listener connected. Waiting for events.")

    # SocketModeClient.connect() is non-blocking (runs its own background
    # threads), so block the main thread until a termination signal arrives.
    _sleep_forever()


_stop = threading.Event()


def _sleep_forever() -> None:
    """Block until SIGTERM/SIGINT, so the container exits cleanly on stop."""
    def _handle(signum, _frame):
        logger.info("Received signal %s; shutting down Slack Socket Mode listener", signum)
        _stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle)
        except ValueError:
            # signal only works in the main thread; if we are not there, just
            # block on the event and let the parent handle termination.
            pass

    _stop.wait()


if __name__ == "__main__":
    run()
