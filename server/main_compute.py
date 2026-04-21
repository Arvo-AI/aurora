"""
Aurora Flask Application - Main Entry Point
This file initializes the Flask app, registers blueprints, and starts the server.
All business logic is contained in blueprint modules under routes/
"""
# Import dotenv early and load env vars before other imports rely on them
from dotenv import load_dotenv 

# Load environment variables from the project root .env file
load_dotenv()

import logging
import os
import hmac
import secrets
import hmac
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from utils.db.db_utils import ensure_database_exists, initialize_tables

# Configure logging first, before importing any modules
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# Silence verbose loggers
logging.getLogger('werkzeug').setLevel(logging.INFO)
logging.getLogger('utils.auth.stateless_auth').setLevel(logging.INFO)
 
import os
import secrets  # For generating a secure random key
import flask
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from utils.db.db_utils import (
    ensure_database_exists,
    initialize_tables,
)


# Initialize Flask application
from jinja2 import ChoiceLoader, FileSystemLoader
github_template_path = os.path.join(os.path.dirname(__file__), "connectors/github_templates")
bitbucket_template_path = os.path.join(os.path.dirname(__file__), "connectors/bitbucket_templates")
app = Flask(__name__, template_folder=github_template_path)
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(github_template_path),
    FileSystemLoader(bitbucket_template_path),
])

# Ensure correct scheme (http/https) behind reverse proxy or load balancer
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(24)
app.config["SESSION_TYPE"] = "filesystem"
app.config["SESSION_PERMANENT"] = False  
app.config["SESSION_FILE_DIR"] = "/tmp/flask_session"  
app.config['MAX_CONTENT_LENGTH'] = 1000 * 1024 * 1024  # 1 GB max file size

# Start MCP preloader service for faster chat responses
try:
    from chat.backend.agent.tools.mcp_preloader import start_mcp_preloader
    mcp_preloader = start_mcp_preloader()
    logging.info("MCP Preloader service started successfully")
except Exception as e:
    logging.warning(f"Failed to start MCP preloader service: {e}")

# Initialize rate limiter for API protection
from utils.web.limiter_ext import limiter, register_rate_limit_handlers
limiter.init_app(app)
logging.info("Rate limiter initialized successfully")
register_rate_limit_handlers(app)

FRONTEND_URL = os.getenv("FRONTEND_URL")

# Configure CORS
CORS(app, origins=FRONTEND_URL, supports_credentials=True, 
     methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
     resources={
         r"/aws/*": {"origins": FRONTEND_URL, "supports_credentials": True, 
                    "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID", 
                                    "Authorization", "X-Provider-Preference"], 
                    "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/azure/*": {"origins": FRONTEND_URL, "supports_credentials": True, 
                      "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID", 
                                      "Authorization", "X-Provider-Preference"], 
                      "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/github/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                       "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/bitbucket/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                          "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                            "Authorization", "X-Provider-Preference"],
                          "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/slack/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/google-chat/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                             "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                               "Authorization", "X-Provider-Preference"],
                             "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
         r"/grafana/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                         "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                            "Authorization", "X-Provider-Preference"],
                         "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
        r"/datadog/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "DELETE", "OPTIONS", "PATCH"]},
        r"/splunk/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
         r"/bigpanda/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                          "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                            "Authorization", "X-Provider-Preference"],
                          "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
        r"/pagerduty/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                         "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                           "Authorization", "X-Provider-Preference"],
                         "methods": ["GET", "POST", "DELETE", "OPTIONS", "PATCH"]},
        r"/opsgenie/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                         "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With",
                                           "X-User-ID", "Authorization", "X-Provider-Preference"],
                         "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
        r"/jenkins/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                        "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                          "Authorization", "X-Provider-Preference"],
                        "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
        r"/spinnaker/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                        "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                          "Authorization", "X-Provider-Preference"],
                        "methods": ["GET", "POST", "DELETE", "OPTIONS", "PATCH"]},
        r"/ovh_api/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
        r"/scaleway_api/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                            "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                              "Authorization", "X-Provider-Preference"],
                            "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
       r"/tailscale_api/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                             "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                               "Authorization", "X-Provider-Preference"],
                             "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
       r"/cloudflare_api/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                              "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                                "Authorization", "X-Provider-Preference"],
                              "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
        r"/api/ssh-keys*": {"origins": FRONTEND_URL, "supports_credentials": True,
                            "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                              "Authorization", "X-Provider-Preference"],
                            "methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]},
       r"/api/vms/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
        r"/api/graph/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                          "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                            "Authorization", "X-Provider-Preference"],
                          "methods": ["GET", "POST", "DELETE", "OPTIONS"]},
        r"/api/prediscovery/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                                 "allow_headers": ["Content-Type", "X-Requested-With", "X-User-ID",
                                                   "Authorization"],
                                 "methods": ["GET", "POST", "OPTIONS"]},
        r"/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID", 
                                "Authorization", "X-Provider-Preference", "X-Org-ID", "X-Internal-Secret"], 
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}
     }
)

# ============================================================================
# Internal API Secret Verification
# ============================================================================
# Ensures requests originate from the Next.js frontend (or another trusted
# internal service) rather than from an unauthenticated external caller.

_INTERNAL_API_SECRET = os.getenv("INTERNAL_API_SECRET", "")
_AURORA_ENV = os.getenv("AURORA_ENV", "production")

if _AURORA_ENV != "dev" and not _INTERNAL_API_SECRET:
    raise RuntimeError(
        "FATAL: INTERNAL_API_SECRET is not set and AURORA_ENV='%s' (non-dev). "
        "Refusing to start without authentication secrets in production." % _AURORA_ENV
    )

_OPEN_PATHS = frozenset(("/api/auth/login", "/api/auth/register"))

_HEALTH_PATH = "/health"

_OPEN_PREFIXES = (
    _HEALTH_PATH,
    "/callback",
    "/github/callback",
    "/bitbucket/callback",
    "/slack/callback",
    "/slack/events",
    "/pagerduty/oauth/callback",
    "/google-chat/callback",
    "/google-chat/events",
    "/datadog/webhook/",
    "/grafana/alerts/webhook/",
    "/splunk/alerts/webhook/",
    "/netdata/alerts/webhook/",
    "/bigpanda/webhook/",
    "/dynatrace/webhook/",
    "/newrelic/webhook/",
    "/pagerduty/webhook/",
    "/opsgenie/webhook/",
    "/jenkins/webhook/",
    "/cloudbees/webhook/",
    "/spinnaker/webhook/",
    "/ovh_api/ovh/oauth2/callback",
    "/azure/callback",
    "/azure/setup-script",
    "/azure/setup-script-ps1",
    "/aws/setup-script",
    "/aws/setup-role",
    "/aws/setup-script-ps1",
    "/aws/setup-role-ps1",
)

@app.before_request
def verify_internal_api_secret():
    """Reject requests that don't carry a valid INTERNAL_API_SECRET header.

    Skipped when the secret is not configured (backward-compatible default)
    and for open endpoints (login, register, health) and external-facing
    webhook/callback/event endpoints called by third-party services.
    """
    if not _INTERNAL_API_SECRET:
        return None

    if request.method == "OPTIONS":
        return None

    if request.path in _OPEN_PATHS:
        return None

    path = request.path
    if any(
        path == prefix or path.startswith(prefix + '/') or (prefix.endswith('/') and path.startswith(prefix))
        for prefix in _OPEN_PREFIXES
    ):
        return None

    provided = request.headers.get("X-Internal-Secret", "")
    if not provided or not hmac.compare_digest(provided, _INTERNAL_API_SECRET):
        logger.warning(
            "Rejected request missing/invalid X-Internal-Secret: %s %s from %s",
            request.method, request.path, request.remote_addr,
        )
        return jsonify({"error": "Forbidden: invalid or missing X-Internal-Secret header"}), 403

    return None

# ============================================================================
# Tenant Isolation Middleware - Validates X-User-ID / X-Org-ID Pairing
# ============================================================================

# Separate from _OPEN_PREFIXES: this only skips routes that carry identity headers before auth completes (login/register); webhook routes don't need listing here because they lack X-User-ID/X-Org-ID and the check below short-circuits on missing headers.
_TENANT_OPEN_PREFIXES = ("/api/auth/login", "/api/auth/register", _HEALTH_PATH)

@app.before_request
def enforce_user_org_binding():
    """Reject requests where X-Org-ID doesn't match the user's actual org."""

    # Skip verification for OPTIONS requests (CORS preflight)
    if request.method == "OPTIONS":
        return None

    if any(request.path.startswith(p) for p in _TENANT_OPEN_PREFIXES):
        return None

    user_id = request.headers.get("X-User-ID")
    claimed_org = request.headers.get("X-Org-ID")

    if not user_id or not claimed_org:
        return None

    from utils.auth.stateless_auth import resolve_org_id
    actual_org = resolve_org_id(user_id)

    if not actual_org:
        return jsonify({"error": "Unauthorized - unknown user"}), 401

    if actual_org != claimed_org:
        logging.getLogger(__name__).warning(
            "Tenant mismatch: user=%s claimed_org=%s actual_org=%s",
            user_id, claimed_org, actual_org,
        )
        return jsonify({"error": "Forbidden - organization mismatch"}), 403

    return None

# ============================================================================
# Register Blueprints - Organized by Domain
# ============================================================================

# --- Core Service Routes ---
from routes.llm_config import llm_config_bp
from routes.auth_routes import auth_bp
from routes.admin_routes import admin_bp

app.register_blueprint(llm_config_bp)  # LLM provider configuration routes
app.register_blueprint(auth_bp)  # Auth.js authentication routes
app.register_blueprint(admin_bp)  # RBAC admin routes

# --- Organization Management Routes ---
from routes.org_routes import org_bp
app.register_blueprint(org_bp)

# --- Command Policy Routes ---
from routes.command_policies import command_policies_bp
app.register_blueprint(command_policies_bp)

# --- GitHub Integration Routes ---
from routes.github.github import github_bp
from routes.github.github_user_repos import github_user_repos_bp
from routes.github.github_repo_selection import github_repo_selection_bp
app.register_blueprint(github_bp, url_prefix="/github")
app.register_blueprint(github_user_repos_bp, url_prefix="/github")
app.register_blueprint(github_repo_selection_bp, url_prefix="/github")

# --- kubectl Agent Token Routes ---
from routes.kubectl_token_routes import kubectl_token_bp
app.register_blueprint(kubectl_token_bp)

# --- MCP API Token Routes ---
from routes.mcp_token_routes import mcp_token_bp
app.register_blueprint(mcp_token_bp)

# --- Slack Integration Routes ---
from routes.slack.slack_routes import slack_bp
from routes.slack.slack_events import slack_events_bp
app.register_blueprint(slack_bp, url_prefix="/slack")
app.register_blueprint(slack_events_bp, url_prefix="/slack")

# --- Google Chat Integration Routes ---
from routes.google_chat.google_chat_routes import google_chat_bp
from routes.google_chat.google_chat_events import google_chat_events_bp
app.register_blueprint(google_chat_bp, url_prefix="/google-chat")
app.register_blueprint(google_chat_events_bp, url_prefix="/google-chat")

# --- Jenkins Integration Routes ---
from routes.jenkins import bp as jenkins_bp  # noqa: F401
import routes.jenkins.tasks  # noqa: F401
app.register_blueprint(jenkins_bp, url_prefix="/jenkins")

# --- CloudBees CI Integration Routes (reuses Jenkins connector) ---
from routes.cloudbees import bp as cloudbees_bp  # noqa: F401
app.register_blueprint(cloudbees_bp, url_prefix="/cloudbees")

# --- Spinnaker Integration Routes ---
from utils.flags.feature_flags import is_spinnaker_enabled
if is_spinnaker_enabled():
    from routes.spinnaker import bp as spinnaker_bp
    import routes.spinnaker.tasks  # noqa: F401
    app.register_blueprint(spinnaker_bp, url_prefix="/spinnaker")

# --- Grafana Integration Routes ---
from routes.grafana import bp as grafana_bp  # noqa: F401
# Import Grafana tasks for Celery registration
import routes.grafana.tasks  # noqa: F401
app.register_blueprint(grafana_bp, url_prefix="/grafana")

# --- Datadog Integration Routes ---
from routes.datadog import bp as datadog_bp  # noqa: F401
import routes.datadog.tasks  # noqa: F401
app.register_blueprint(datadog_bp, url_prefix="/datadog")

# --- Netdata Integration Routes ---
from routes.netdata import bp as netdata_bp  # noqa: F401
import routes.netdata.tasks  # noqa: F401
app.register_blueprint(netdata_bp, url_prefix="/netdata")

# --- Splunk Integration Routes ---
from routes.splunk import bp as splunk_bp, search_bp as splunk_search_bp  # noqa: F401
import routes.splunk.tasks  # noqa: F401
app.register_blueprint(splunk_bp, url_prefix="/splunk")
app.register_blueprint(splunk_search_bp, url_prefix="/splunk")

# --- Coroot Integration Routes ---
from routes.coroot import bp as coroot_bp  # noqa: F401
app.register_blueprint(coroot_bp, url_prefix="/coroot")

# --- ThousandEyes Integration Routes ---
from routes.thousandeyes import bp as thousandeyes_bp  # noqa: F401
app.register_blueprint(thousandeyes_bp, url_prefix="/thousandeyes")

# --- Dynatrace Integration Routes ---
from routes.dynatrace import bp as dynatrace_bp  # noqa: F401
import routes.dynatrace.tasks  # noqa: F401
app.register_blueprint(dynatrace_bp, url_prefix="/dynatrace")

# --- BigPanda Integration Routes ---
from routes.bigpanda import bp as bigpanda_bp  # noqa: F401
import routes.bigpanda.tasks  # noqa: F401
app.register_blueprint(bigpanda_bp, url_prefix="/bigpanda")

# --- New Relic Integration Routes ---
from routes.newrelic import bp as newrelic_bp  # noqa: F401
app.register_blueprint(newrelic_bp, url_prefix="/newrelic")
import routes.newrelic.tasks  # noqa: F401

# --- PagerDuty Integration Routes ---
from routes.pagerduty.pagerduty_routes import pagerduty_bp  # noqa: F401
app.register_blueprint(pagerduty_bp, url_prefix="/pagerduty")

# --- OpsGenie Integration Routes ---
from routes.opsgenie import bp as opsgenie_bp  # noqa: F401
import routes.opsgenie.tasks  # noqa: F401
app.register_blueprint(opsgenie_bp, url_prefix="/opsgenie")

# --- Knowledge Base Routes ---
from routes.knowledge_base import bp as knowledge_base_bp  # noqa: F401
app.register_blueprint(knowledge_base_bp, url_prefix="/api/knowledge-base")


# --- Confluence Integration Routes ---
from routes.confluence import bp as confluence_bp  # noqa: F401
app.register_blueprint(confluence_bp, url_prefix="/confluence")

# --- Unified Atlassian Routes (Confluence + Jira OAuth) ---
from utils.flags.feature_flags import is_jira_enabled, is_confluence_enabled
if is_confluence_enabled() or is_jira_enabled():
    from routes.atlassian import bp as atlassian_bp  # noqa: F401
    app.register_blueprint(atlassian_bp, url_prefix="/atlassian")

# --- Jira Integration Routes ---
if is_jira_enabled():
    from routes.jira import bp as jira_bp  # noqa: F401
    app.register_blueprint(jira_bp, url_prefix="/jira")

# --- SharePoint Integration Routes ---
from utils.flags.feature_flags import is_sharepoint_enabled
if is_sharepoint_enabled():
    from routes.sharepoint import bp as sharepoint_bp  # noqa: F401
    app.register_blueprint(sharepoint_bp, url_prefix="/sharepoint")

# --- Bitbucket Integration Routes ---
from routes.bitbucket.bitbucket import bitbucket_bp
from routes.bitbucket.bitbucket_browsing import bitbucket_browsing_bp
from routes.bitbucket.bitbucket_selection import bitbucket_selection_bp
app.register_blueprint(bitbucket_bp, url_prefix="/bitbucket")
app.register_blueprint(bitbucket_browsing_bp, url_prefix="/bitbucket")
app.register_blueprint(bitbucket_selection_bp, url_prefix="/bitbucket")

# --- Incidents Routes ---
from routes.incidents_routes import incidents_bp
from routes.incidents_sse import incidents_sse_bp
from routes.incident_feedback import incident_feedback_bp
app.register_blueprint(incidents_bp)
app.register_blueprint(incidents_sse_bp)
app.register_blueprint(incident_feedback_bp)

from routes.postmortem_routes import postmortem_bp
app.register_blueprint(postmortem_bp)

# --- SRE Metrics Routes ---
from routes.metrics_routes import metrics_bp
app.register_blueprint(metrics_bp)

# --- Visualization Streaming Routes ---
from routes.visualization_stream import visualization_bp
app.register_blueprint(visualization_bp)

# --- Monitor Routes (Agent Fleet / Waterfall) ---
from routes.monitor.fleet_routes import fleet_bp
from routes.monitor.waterfall_routes import waterfall_bp
app.register_blueprint(fleet_bp)
app.register_blueprint(waterfall_bp)

# --- Audit Log Routes ---
from routes.audit_routes import audit_bp
app.register_blueprint(audit_bp)

# --- User & Auth Routes ---
from routes.user_preferences import user_preferences_bp
from routes.user_connections import user_connections_bp
from routes.account_management import account_management_bp
from routes.health_routes import health_bp
from routes.llm_usage_routes import llm_usage_bp
from routes.aws import bp as aws_bp
from routes.rca_emails import rca_emails_bp
from routes.ssh_keys import bp as ssh_keys_bp
from routes.vms import bp as vms_bp

app.register_blueprint(user_preferences_bp)
app.register_blueprint(health_bp, url_prefix=_HEALTH_PATH) # NEW: Health check endpoint
app.register_blueprint(llm_usage_bp)
app.register_blueprint(aws_bp)  # Primary AWS routes at root
app.register_blueprint(rca_emails_bp)  # RCA email management routes
app.register_blueprint(ssh_keys_bp)  # SSH key management routes
app.register_blueprint(vms_bp)  # VM management routes

app.register_blueprint(user_connections_bp)
app.register_blueprint(account_management_bp)

# --- Unified Connector Status ---
from routes.connector_status import connector_status_bp
app.register_blueprint(connector_status_bp)

# --- Monitoring & Logging Routes ---
from routes.chat_routes import chat_bp

app.register_blueprint(chat_bp, url_prefix="/chat_api")

# ============================================================================
# Register Cloud Provider Blueprints (Organized Subpackages)
# ============================================================================

# --- GCP Routes ---
from routes.gcp import bp as gcp_auth_bp
from routes.gcp.projects import gcp_projects_bp
from routes.gcp.billing import gcp_billing_bp
from routes.gcp.root_project import root_project_bp

app.register_blueprint(gcp_auth_bp)
app.register_blueprint(gcp_projects_bp)
app.register_blueprint(gcp_billing_bp)
app.register_blueprint(root_project_bp)

# --- AWS Routes ---
# AWS blueprint already registered above with url_prefix="/aws_api"

# --- Azure Routes ---
from routes.azure import bp as azure_bp
app.register_blueprint(azure_bp)

# --- OVH Routes ---
from utils.flags.feature_flags import is_ovh_enabled
if is_ovh_enabled():
    from routes.ovh import ovh_bp
    app.register_blueprint(ovh_bp, url_prefix="/ovh_api")

# --- Scaleway Routes ---
from routes.scaleway import scaleway_bp
app.register_blueprint(scaleway_bp, url_prefix="/scaleway_api")

# --- Tailscale Routes ---
from routes.tailscale import tailscale_bp
app.register_blueprint(tailscale_bp, url_prefix="/tailscale_api")

# --- Cloudflare Routes ---
from routes.cloudflare import cloudflare_bp
app.register_blueprint(cloudflare_bp, url_prefix="/cloudflare_api")

from routes.terraform import terraform_workspace_bp
app.register_blueprint(terraform_workspace_bp)

# --- Health & Monitoring Routes ---
# health_bp already imported and registered above

# --- Graph / Service Discovery Routes ---
from routes.graph_routes import graph_bp
app.register_blueprint(graph_bp)

# --- Prediscovery Routes ---
from routes.prediscovery import bp as prediscovery_bp
app.register_blueprint(prediscovery_bp, url_prefix="/api/prediscovery")

# ---- Debug Routes ----
from routes.debug import bp as debug_bp
app.register_blueprint(debug_bp)

# ============================================================================
# Global Error Handlers
# ============================================================================

logger = logging.getLogger(__name__)

@app.errorhandler(404)
def handle_not_found(error):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def handle_internal_error(error):
    logger.error(f"Unhandled server error: {error}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


@app.route('/api/routes', methods=['GET'])
def list_api_routes():
    """Auto-generated catalog of all API endpoints (used by MCP server)."""
    if not request.headers.get("X-User-ID"):
        return jsonify({"error": "Unauthorized"}), 401
    routes = []
    for rule in app.url_map.iter_rules():
        methods = sorted(rule.methods - {'HEAD', 'OPTIONS'})
        if methods:
            routes.append({'path': rule.rule, 'methods': methods})
    routes.sort(key=lambda r: r['path'])
    return jsonify(routes)

# ============================================================================
# Main Application Runner
# ============================================================================

def initialize_app():
    # Initialize database
    ensure_database_exists()
    initialize_tables()

    # Initialize Casbin RBAC enforcer (seeds default policies on first run)
    try:
        from utils.auth.enforcer import get_enforcer
        get_enforcer()
        logging.getLogger(__name__).info("Casbin RBAC enforcer initialized.")
    except Exception as e:
        logging.getLogger(__name__).warning("Casbin enforcer init deferred: %s", e)

# Always run initialization when module is imported (for Gunicorn and direct execution)
initialize_app()

if __name__ == "__main__":
    # Development mode: run Flask's built-in server
    # Port configurable via FLASK_PORT env var (set in .env file)
    # Note: Default is 5080 to avoid conflict with macOS AirPlay Receiver (port 5000)
    port = int(os.getenv("FLASK_PORT"))
    app.run(host="0.0.0.0", port=port, debug=False)
