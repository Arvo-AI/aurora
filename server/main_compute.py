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
import secrets
from flask import Flask
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from utils.db.db_utils import ensure_database_exists, initialize_tables

# Configure logging first, before importing any modules
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# Silence verbose loggers
logging.getLogger('werkzeug').setLevel(logging.INFO)
logging.getLogger('utils.auth.stateless_auth').setLevel(logging.INFO)
 
import requests
import os, json, base64
import secrets  # For generating a secure random key
import flask
from flask import Flask, redirect, request, session, jsonify
from dotenv import load_dotenv
from utils.db.db_utils import (
    ensure_database_exists,
    initialize_tables,
    connect_to_db_as_admin,
    connect_to_db_as_user,
)
import urllib.parse
import time
import traceback
from datetime import datetime
import subprocess
import shutil

# CORS imports
from flask_cors import CORS
from utils.web.cors_utils import create_cors_response

# Routes imports - organized sections below

# GCP imports
from connectors.gcp_connector.auth import (
    get_credentials,
    get_project_list,
    ensure_aurora_full_access,
    get_aurora_service_account_email,
)
from connectors.gcp_connector.auth.oauth import (
    get_auth_url,
    exchange_code_for_token,
)
from utils.auth.token_management import (
    get_token_data,
    store_tokens_in_db,
)
from connectors.gcp_connector.billing import store_bigquery_data, is_bigquery_enabled, has_active_billing
from connectors.gcp_connector.gcp.projects import list_gke_clusters

# Azure imports
from connectors.azure_connector.k8s_client import get_aks_clusters, extract_resource_group
from azure.identity import ClientSecretCredential

# AWS imports
import boto3, flask
from utils.auth.stateless_auth import get_user_id_from_request

# Google API imports
from googleapiclient.discovery import build  # local import to avoid global dependency



# Initialize Flask application
template_path = os.path.join(os.path.dirname(__file__), "connectors/github_templates")
app = Flask(__name__, template_folder=template_path)

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
         r"/slack/*": {"origins": FRONTEND_URL, "supports_credentials": True,
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
        r"/pagerduty/*": {"origins": FRONTEND_URL, "supports_credentials": True,
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
        r"/api/ssh-keys*": {"origins": FRONTEND_URL, "supports_credentials": True,
                            "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                              "Authorization", "X-Provider-Preference"],
                            "methods": ["GET", "POST", "PATCH", "DELETE", "OPTIONS"]},
       r"/api/vms/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                       "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID",
                                         "Authorization", "X-Provider-Preference"],
                       "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]},
        r"/*": {"origins": FRONTEND_URL, "supports_credentials": True,
                "allow_headers": ["Content-Type", "X-Provider", "X-Requested-With", "X-User-ID", 
                                "Authorization", "X-Provider-Preference"], 
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"]}
     }
)

# ============================================================================
# Register Blueprints - Organized by Domain
# ============================================================================

# --- Core Service Routes ---
from routes.llm_config import llm_config_bp
from routes.auth_routes import auth_bp

app.register_blueprint(llm_config_bp)  # LLM provider configuration routes
app.register_blueprint(auth_bp)  # Auth.js authentication routes

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

# --- Slack Integration Routes ---
from utils.flags.feature_flags import is_slack_enabled
if is_slack_enabled():
    from routes.slack.slack_routes import slack_bp
    from routes.slack.slack_events import slack_events_bp
    app.register_blueprint(slack_bp, url_prefix="/slack")
    app.register_blueprint(slack_events_bp, url_prefix="/slack")

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

# --- PagerDuty Integration Routes ---
from routes.pagerduty.pagerduty_routes import pagerduty_bp  # noqa: F401
app.register_blueprint(pagerduty_bp, url_prefix="/pagerduty")

# --- Knowledge Base Routes ---
from routes.knowledge_base import bp as knowledge_base_bp  # noqa: F401
app.register_blueprint(knowledge_base_bp, url_prefix="/api/knowledge-base")

# --- Incidents Routes ---
from routes.incidents_routes import incidents_bp
from routes.incidents_sse import incidents_sse_bp
app.register_blueprint(incidents_bp)
app.register_blueprint(incidents_sse_bp)

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
app.register_blueprint(health_bp, url_prefix="/health") # NEW: Health check endpoint
app.register_blueprint(llm_usage_bp)
app.register_blueprint(aws_bp)  # Primary AWS routes at root
app.register_blueprint(rca_emails_bp)  # RCA email management routes
app.register_blueprint(ssh_keys_bp)  # SSH key management routes
app.register_blueprint(vms_bp)  # VM management routes
from routes.billing_scheduler_routes import billing_scheduler_bp
app.register_blueprint(billing_scheduler_bp)  # NEW: Automated billing endpoints

app.register_blueprint(user_connections_bp)
app.register_blueprint(account_management_bp)

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

from routes.terraform import terraform_workspace_bp
app.register_blueprint(terraform_workspace_bp)

# --- Health & Monitoring Routes ---
# health_bp already imported and registered above

# ---- Debug Routes ----
from routes.debug import bp as debug_bp
app.register_blueprint(debug_bp)

# ============================================================================
# Main Application Runner
# ============================================================================

def initialize_app():
    # Initialize database
    ensure_database_exists()
    initialize_tables()

# Always run initialization when module is imported (for Gunicorn and direct execution)
initialize_app()

if __name__ == "__main__":
    # Development mode: run Flask's built-in server
    # Port configurable via FLASK_PORT env var (set in .env file)
    # Note: Default is 5080 to avoid conflict with macOS AirPlay Receiver (port 5000)
    port = int(os.getenv("FLASK_PORT"))
    app.run(host="0.0.0.0", port=port, debug=True)
