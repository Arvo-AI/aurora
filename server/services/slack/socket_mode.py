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

import json
import os
import signal
import threading

from dotenv import load_dotenv

load_dotenv()


def _post_response_url(response_url: str, body: dict) -> None:
    """Deliver an interaction result to Slack via the payload's response_url.

    Slack's documented follow-up mechanism for interactive components: after
    acking the envelope, POST the message body to the ``response_url`` (valid
    for ~30 min). ``replace_original`` makes it update the originating message,
    matching what returning the body in the HTTP interactivity response does.
    """
    import urllib.request

    data = json.dumps({**body, "replace_original": True}).encode("utf-8")
    request = urllib.request.Request(
        response_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as resp:
        resp.read()


def _process_interaction_async(payload: dict) -> None:
    """Run the (potentially slow) interaction handler off the ack path.

    The handlers do DB queries + Slack API calls and queue background work, so
    running them inline would blow Slack's ~3s ack deadline. We already acked
    the envelope in :func:`_dispatch`; here we do the work and, if the handler
    returns a non-empty body, deliver it through the interaction's
    ``response_url`` (Slack's follow-up mechanism) instead of the ack.
    """
    from routes.slack.slack_events import process_interaction

    try:
        response_payload = process_interaction(payload)

        # Only a non-empty text should update the originating message; an empty
        # body is the "leave the message as-is" ack we already sent.
        response_url = payload.get("response_url")
        if response_url and response_payload and response_payload.get("text"):
            _post_response_url(response_url, response_payload)
    except Exception:
        # Never let a handler error kill the worker thread.
        logger.exception("Error processing Socket Mode interaction asynchronously")


def _dispatch(client, req) -> None:
    """Route one Socket Mode request to the shared Slack handlers.

    ``req`` is a slack_sdk ``SocketModeRequest``. We ack every envelope first
    (Slack disconnects a client that does not ack within ~3s), then process.
    Interactions are handed to a background thread and their result is delivered
    via the payload's ``response_url`` so a slow handler never delays the ack.
    """
    from slack_sdk.socket_mode.response import SocketModeResponse

    # Imported lazily so importing this module never drags in the Flask app
    # unless the listener actually runs.
    from routes.slack.slack_events import process_event_callback

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
            # Ack the envelope FIRST — the handler runs DB queries + Slack API
            # calls that can exceed Slack's ~3s deadline. The result is
            # delivered later via response_url, not this ack.
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
            threading.Thread(
                target=_process_interaction_async,
                args=(payload,),
                name="slack-interaction",
                daemon=True,
            ).start()
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
