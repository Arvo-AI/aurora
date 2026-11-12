"""
Onboarding routes for connector selection and setup flow.
"""
import html
import logging
from flask import Blueprint, Response, request, jsonify
from werkzeug.exceptions import BadRequest
from utils.auth.rbac_decorators import require_auth_only
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)

onboarding_bp = Blueprint('onboarding', __name__)
# Reached from email links, without a session.
onboarding_public_bp = Blueprint('onboarding_public', __name__)


@onboarding_bp.route('/journey', methods=['GET'])
@require_auth_only
def get_journey(user_id):
    """The setup guide for this account, and how the account signed up."""
    from services.onboarding.journey import journey_for_app
    return jsonify(journey_for_app(user_id))


_UNSUB_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">
<title>Aurora setup emails</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0a0a;color:#f2f4f3;
font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif}}
main{{max-width:420px;padding:32px 24px}}h1{{font-size:22px;font-weight:600;margin:0 0 8px}}
p{{color:rgba(255,255,255,.6);margin:0 0 24px}}button{{font:inherit;font-weight:500;background:#fff;color:#000;
border:0;border-radius:8px;padding:10px 18px;cursor:pointer}}button:focus-visible{{outline:2px solid #26e680;outline-offset:3px}}</style>
</head><body><main>{body}</main></body></html>"""


def _unsub_html(body: str, status: int = 200) -> Response:
    return Response(_UNSUB_PAGE.format(body=body), status=status, mimetype="text/html")


@onboarding_public_bp.route('/unsubscribe', methods=['GET', 'POST'])
def unsubscribe_setup_emails():
    """Stop setup emails. GET asks first, so link scanners don't unsubscribe
    anyone; POST (the button, or a mail client's one-click) does it."""
    from services.onboarding.sender import unsubscribe_token_ok
    user_id = (request.args.get("u") or "")[:255]
    token = (request.args.get("t") or "")[:128]
    if not unsubscribe_token_ok(user_id, token):
        return _unsub_html("<h1>This link doesn't work</h1><p>It may have been cut off. Reply to any setup email and we'll stop them by hand.</p>", 400)
    if request.method == "GET":
        action = html.escape(request.full_path, quote=True)
        return _unsub_html(
            "<h1>Stop Aurora setup emails?</h1><p>You'll still get account emails, like password resets.</p>"
            f'<form method="post" action="{action}"><button type="submit">Stop setup emails</button></form>'
        )
    from services.onboarding.journey import unsubscribe
    unsubscribe(user_id)
    return _unsub_html("<h1>Done</h1><p>You won't get any more setup emails from Aurora.</p>")


@onboarding_bp.route('/complete', methods=['POST'])
@require_auth_only
def complete_onboarding(user_id):
    """Save connector selections and mark onboarding complete."""
    try:
        try:
            data = request.get_json()
        except BadRequest:
            return jsonify({"error": "Invalid JSON body"}), 400
        if not isinstance(data, dict):
            return jsonify({"error": "Invalid request body"}), 400

        selected_connectors = data.get("selected_connectors", [])

        if not isinstance(selected_connectors, list) or len(selected_connectors) > 50:
            return jsonify({"error": "Invalid connector selections"}), 400
        selected_connectors = [
            str(c)[:100] for c in selected_connectors if isinstance(c, str)
        ]

        with db_pool.get_admin_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "User not found"}), 404
                org_id = row[0]

                if not org_id:
                    return jsonify({"error": "User is not associated with an organization"}), 400

                cur.execute(
                    """INSERT INTO onboarding_selections (org_id, user_id, selected_connectors)
                       VALUES (%s, %s, %s)
                       ON CONFLICT (org_id) DO UPDATE
                       SET selected_connectors = EXCLUDED.selected_connectors,
                           user_id = EXCLUDED.user_id""",
                    (org_id, user_id, selected_connectors),
                )

                cur.execute(
                    "UPDATE organizations SET onboarding_completed = TRUE WHERE id = %s",
                    (org_id,),
                )

                conn.commit()

        return jsonify({"success": True, "selected": selected_connectors})
    except Exception:
        logger.exception("Error completing onboarding")
        return jsonify({"error": "Internal server error"}), 500
