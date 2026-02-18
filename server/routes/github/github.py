import logging
import requests
import flask
from flask import Blueprint, request, jsonify, Response
import os
from utils.web.cors_utils import create_cors_response
from utils.auth.stateless_auth import get_user_id_from_request

github_bp = Blueprint("github", __name__)

# Get frontend URL from environment with fallback
FRONTEND_URL = os.getenv("FRONTEND_URL")

@github_bp.route("/login", methods=["POST", "OPTIONS"])
def github_login():
    """Handle GitHub OAuth login initiation and manual token storage"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        data = request.get_json()
        user_id = data.get('userId') or get_user_id_from_request()
        
        if not user_id:
            return jsonify({"error": "User ID is required"}), 400
        
        # Check if this is a manual token submission or OAuth initiation
        access_token = data.get('access_token')
        
        if access_token:
            # Manual token submission - store directly
            try:
                # Validate the token by getting user info
                headers = {"Authorization": f"token {access_token}"}
                user_response = requests.get("https://api.github.com/user", headers=headers)
                
                if user_response.status_code != 200:
                    return jsonify({"error": "Invalid GitHub access token"}), 400
                
                user_data = user_response.json()
                github_username = user_data.get("login")
                github_user_id = user_data.get("id")
                
                # Store in database using existing pattern
                from utils.auth.token_management import store_tokens_in_db
                
                github_token_data = {
                    "access_token": access_token,
                    "username": github_username,
                    "user_id": github_user_id,
                    "api_url": "https://api.github.com"
                }
                
                store_tokens_in_db(user_id, github_token_data, "github")
                logging.info(f"Stored GitHub credentials for user {user_id} (manual)")
                
                # Clear MCP tools cache so new GitHub tools are loaded
                try:
                    from chat.backend.agent.tools.mcp_tools import clear_credentials_cache
                    clear_credentials_cache(user_id)
                    logging.info(f"Cleared MCP cache for user {user_id} after GitHub login")
                except Exception as cache_err:
                    logging.warning(f"Failed to clear MCP cache: {cache_err}")
                
                return jsonify({
                    "success": True,
                    "message": f"Successfully connected to GitHub as {github_username}",
                    "username": github_username
                })
                
            except Exception as e:
                logging.error(f"Error storing GitHub credentials: {e}", exc_info=True)
                return jsonify({"error": "Failed to store GitHub credentials"}), 500
        else:
            # Check if GitHub OAuth environment variables are configured
            github_client_id = os.getenv("GH_OAUTH_CLIENT_ID")
            github_client_secret = os.getenv("GH_OAUTH_CLIENT_SECRET")
            
            if not github_client_id or not github_client_secret:
                logging.error("GitHub OAuth client ID or secret not configured")
                return jsonify({
                    "error": "GitHub OAuth is not configured",
                    "error_code": "GITHUB_NOT_CONFIGURED",
                    "message": "GitHub OAuth environment variables (GH_OAUTH_CLIENT_ID and GH_OAUTH_CLIENT_SECRET) are not configured. Please configure them as described in the GitHub connector README."
                }), 400
            
            env = os.environ.get('AURORA_ENV', '').lower()
            if env in ['prod', 'staging']:
                redirect_uri = f"{request.host_url}backend/github/callback"
            elif env in ['dev']:
                redirect_uri = f"{request.host_url}github/callback"

                
            # Use user_id as state parameter to identify user after OAuth
            oauth_url = (
                f"https://github.com/login/oauth/authorize"
                f"?client_id={github_client_id}"
                f"&redirect_uri={redirect_uri}"
                f"&scope=repo,user"
                f"&state={user_id}"
            )
            
            return jsonify({
                "oauth_url": oauth_url,
                "message": "Redirect to GitHub for authentication"
            })
    
    except Exception as e:
        logging.error(f"Error in GitHub login: {e}", exc_info=True)
        return jsonify({"error": "Failed to process GitHub login"}), 500

@github_bp.route("/status", methods=["GET", "OPTIONS"])
def github_status():
    """Check GitHub connection status for a user"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"connected": False, "error": "User ID required"}), 400
        
        # Check if user has GitHub credentials stored
        from utils.auth.stateless_auth import get_credentials_from_db
        
        github_creds = get_credentials_from_db(user_id, "github")
        if not github_creds or not github_creds.get("access_token"):
            return jsonify({"connected": False})
        
        # Validate the stored token by making a test API call
        headers = {"Authorization": f"token {github_creds['access_token']}"}
        user_response = requests.get("https://api.github.com/user", headers=headers)
        
        if user_response.status_code == 200:
            user_data = user_response.json()
            return jsonify({
                "connected": True,
                "username": user_data.get("login"),
                "name": user_data.get("name"),
                "avatar_url": user_data.get("avatar_url")
            })
        else:
            # Token is invalid, remove it
            return jsonify({"connected": False, "error": "Invalid or expired token"})
    
    except Exception as e:
        logging.error(f"Error checking GitHub status: {e}", exc_info=True)
        return jsonify({"connected": False, "error": "Failed to check GitHub status"}), 500

@github_bp.route("/disconnect", methods=["POST", "OPTIONS"])
def github_disconnect():
    """Disconnect GitHub account for a user"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400
        
        # Remove GitHub credentials from database and Vault
        from utils.secrets.secret_ref_utils import delete_user_secret
        
        delete_success = delete_user_secret(user_id, "github")
        
        if delete_success:
            logging.info(f"Disconnected GitHub for user {user_id}")
            return jsonify({"success": True, "message": "GitHub account disconnected"})
        else:
            logging.error(f"Failed to disconnect GitHub for user {user_id}")
            return jsonify({"error": "Failed to disconnect GitHub account"}), 500
    
    except Exception as e:
        logging.error(f"Error disconnecting GitHub: {e}", exc_info=True)
        return jsonify({"error": "Failed to disconnect GitHub"}), 500

@github_bp.route("/callback", methods=["GET", "POST"])
def github_callback():
    """ Handles the callback from GitHub """
    try:
        # Get the authorization code from the query string
        code = request.args.get("code")
        if not code:
            logging.error("No code provided in GitHub callback")
            return flask.render_template("github_callback_error.html", 
                                        error="No authorization code provided",
                                        frontend_url=FRONTEND_URL)
        
        logging.info(f"Received GitHub code: {code[:5]}...")
        

        # Check environment variables
        # Exchange the code for an access token
        github_client_id = os.getenv("GH_OAUTH_CLIENT_ID")
        github_client_secret = os.getenv("GH_OAUTH_CLIENT_SECRET")
        
        if not github_client_id or not github_client_secret:
            logging.error("GitHub client ID or secret not configured")
            return flask.render_template("github_callback_error.html", 
                                        error="GitHub integration not properly configured",
                                        frontend_url=FRONTEND_URL)
        
        # Make the request to GitHub
        token_url = "https://github.com/login/oauth/access_token"
        payload = {
            "client_id": github_client_id,
            "client_secret": github_client_secret,
            "code": code
        }
        headers = {"Accept": "application/json"}
        
        logging.info(f"Requesting token with payload: {payload}")
        response = requests.post(token_url, json=payload, headers=headers)
        logging.info(f"Token response status: {response.status_code}")
        
        if response.status_code != 200:
            logging.error(f"GitHub token exchange failed: {response.text}")
            return flask.render_template("github_callback_error.html", 
                                        error="Failed to authenticate with GitHub",
                                        frontend_url=FRONTEND_URL)
        
        # Extract the access token
        token_data = response.json()
        logging.info(f"Token response data keys: {list(token_data.keys())}")
        
        access_token = token_data.get("access_token")
        
        if not access_token:
            logging.error(f"No access token in GitHub response: {token_data}")
            return flask.render_template("github_callback_error.html", 
                                        error="Invalid response from GitHub",
                                        frontend_url=FRONTEND_URL)
        
        logging.info(f"Received access token: {access_token[:5]}...")
        
        # Get user information to identify the user
        user_response = requests.get("https://api.github.com/user", 
                                    headers={"Authorization": f"token {access_token}"})
        
        logging.info(f"User info response status: {user_response.status_code}")
        
        if user_response.status_code != 200:
            logging.error(f"Failed to get GitHub user info: {user_response.text}")
            return flask.render_template("github_callback_error.html", 
                                        error="Failed to get user information",
                                        frontend_url=FRONTEND_URL)
        
        user_data = user_response.json()
        github_user_id = user_data.get("id")
        github_username = user_data.get("login")
        
        logging.info(f"Authenticated as GitHub user: {github_username}")
        
        # Store the token in the database
        try:
            # Extract user ID from query params (passed from frontend)
            user_id = request.args.get("state")  # GitHub OAuth state parameter
            if user_id:
                from utils.auth.token_management import store_tokens_in_db
                
                # Store GitHub credentials in the same format as other providers
                github_token_data = {
                    "access_token": access_token,
                    "username": github_username,
                    "user_id": github_user_id,
                    "api_url": "https://api.github.com"
                }
                
                # Store in user_tokens table
                store_tokens_in_db(user_id, github_token_data, "github")
                logging.info(f"Stored GitHub credentials for user {user_id}")
                
                # Clear MCP tools cache so new GitHub tools are loaded
                try:
                    from chat.backend.agent.tools.mcp_tools import clear_credentials_cache
                    clear_credentials_cache(user_id)
                    logging.info(f"Cleared MCP cache for user {user_id} after GitHub OAuth")
                except Exception as cache_err:
                    logging.warning(f"Failed to clear MCP cache: {cache_err}")
            else:
                logging.warning("No user_id provided in GitHub OAuth state parameter")
        except Exception as e:
            logging.error(f"Failed to store GitHub credentials: {e}")
            # Continue without failing the auth flow
        
        # Return an HTML page that sends a message to the opener window and closes itself
        return flask.render_template("github_callback_success.html", 
                              token=access_token,
                              github_username=github_username,
                              frontend_url=FRONTEND_URL)
    
    except Exception as e:
        logging.error(f"Error during GitHub callback: {e}")
        return flask.render_template("github_callback_error.html",
                                    error="An unexpected error occurred during GitHub authentication",
                                    frontend_url=FRONTEND_URL)

@github_bp.route("/repos", methods=["GET", "OPTIONS"])
def get_github_repos():
    """Fetch repositories for an authenticated GitHub user"""
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        return create_cors_response()
        
    try:
        # Get the GitHub token from Authorization header
        auth_header = request.headers.get("Authorization", "")
        logging.info(f"Received Authorization header: {auth_header[:10]}...")
        
        # Extract token from Authorization header - handle both 'Bearer' and 'token' formats
        token = None
        if auth_header.startswith("Bearer "):
            token = auth_header.split(" ")[1]
        elif auth_header.startswith("token "):
            token = auth_header.split(" ")[1]
        else:
            # Try to get the token directly
            token = auth_header
        
        if not token:
            logging.error("Invalid or missing token in Authorization header")
            return jsonify({"error": "Invalid authorization header"}), 401
            
        logging.info(f"Extracted token for repository fetch: {token[:4]}...")
        
        # Use the token to fetch repos from GitHub
        headers = {
            "Authorization": f"token {token}",  # GitHub expects 'token', not 'Bearer'
            "Accept": "application/vnd.github.v3+json"
        }
        
        # Log the headers we're sending (without the full token)
        logging.info(f"Sending GitHub API request with headers: {{'Authorization': 'token {token[:4]}...', 'Accept': '{headers['Accept']}'}}")
        
        # Make the API request
        # Get both user repos and orgs the user belongs to
        api_url = "https://api.github.com/user/repos?sort=updated&per_page=100"
        logging.info(f"Fetching repositories from: {api_url}")
        response = requests.get(api_url, headers=headers)
        
        # Log the response details
        logging.info(f"GitHub API response status: {response.status_code}")
        if response.status_code != 200:
            response_text = response.text
            logging.error(f"GitHub API error: {response_text}")
            return jsonify({
                "error": "Failed to fetch repositories", 
                "status": response.status_code,
                "details": response_text
            }), response.status_code
        
        # Log the response content
        repos = response.json()
        logging.info(f"Received {len(repos)} repositories from GitHub")
        
        # Extract essential repo information - keep all useful fields
        simplified_repos = []
        for repo in repos:
            try:
                # Only include repositories the user can push to
                if not repo.get("permissions", {}).get("push", False):
                    continue
                    
                simplified_repos.append({
                    "id": repo["id"],
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "private": repo["private"],
                    "html_url": repo["html_url"],
                    "description": repo["description"],
                    "default_branch": repo.get("default_branch", "main"),
                    "updated_at": repo.get("updated_at", ""),
                    "language": repo.get("language"),
                    "owner": {
                        "login": repo["owner"]["login"],
                        "avatar_url": repo["owner"]["avatar_url"]
                    },
                    "permissions": repo.get("permissions", {})
                })
            except KeyError as e:
                logging.error(f"Missing key in repository data: {e}")
        
        # Get user info to return with repos
        user_response = requests.get("https://api.github.com/user", headers=headers)
        user_info = {}
        if user_response.status_code == 200:
            user_data = user_response.json()
            user_info = {
                "login": user_data.get("login"),
                "name": user_data.get("name"),
                "avatar_url": user_data.get("avatar_url")
            }
        
        logging.info(f"Returning {len(simplified_repos)} processed repositories")
        return jsonify({
            "repos": simplified_repos,
            "user": user_info
        })
        
    except Exception as e:
        logging.error(f"Error fetching GitHub repos: {e}", exc_info=True)
        return jsonify({"error": "Failed to fetch GitHub repositories"}), 500

@github_bp.route("/token-info", methods=["GET", "OPTIONS"])
def github_token_info():
    """Debug endpoint to check token information"""
    if request.method == 'OPTIONS':
        return create_cors_response()
        
    try:
        # Get the GitHub token from Authorization header
        auth_header = request.headers.get("Authorization", "")
        headers = dict(request.headers)
        
        # Remove any tokens from the log for security
        if auth_header:
            sanitized_header = auth_header[:10] + "..." if len(auth_header) > 10 else auth_header
            headers["Authorization"] = sanitized_header
            
            # Extract token
            token = None
            if auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            elif auth_header.startswith("token "):
                token = auth_header.split(" ")[1]
            else:
                # Try to get the token directly
                token = auth_header
                
            # Get user info if token is available
            user_info = {}
            if token:
                user_response = requests.get(
                    "https://api.github.com/user", 
                    headers={"Authorization": f"token {token}"}
                )
                if user_response.status_code == 200:
                    user_data = user_response.json()
                    user_info = {
                        "login": user_data.get("login"),
                        "name": user_data.get("name"),
                        "avatar_url": user_data.get("avatar_url")
                    }
            
            return jsonify({
                "message": "Token info request received",
                "headers_received": headers,
                "has_auth_header": bool(auth_header),
                "auth_header_starts_with": sanitized_header,
                "username": user_info.get("login", ""),
                "name": user_info.get("name", ""),
                "is_valid_token": bool(user_info)
            })
        
        return jsonify({
            "message": "Token info request received",
            "headers_received": headers,
            "has_auth_header": False,
            "is_valid_token": False
        })
    except Exception as e:
        logging.error(f"Error in token info endpoint: {e}", exc_info=True)
        return jsonify({"error": "Failed to retrieve token info"}), 500

@github_bp.route("/download-repo", methods=["POST", "OPTIONS"])
def download_github_repo():
    """Download a GitHub repository as a zip file and return it"""
    if request.method == 'OPTIONS':
        return create_cors_response()
        
    try:
        # Get request data
        data = request.get_json()
        repo_full_name = data.get('repo_full_name')
        branch = data.get('branch', 'main')
        
        if not repo_full_name:
            return jsonify({"error": "Missing repo_full_name parameter"}), 400
            
        # Get user ID and fetch stored GitHub credentials (reusing provider selector pattern)
        user_id = get_user_id_from_request()
        if not user_id:
            return jsonify({"error": "User ID required"}), 400
        
        # Get GitHub credentials from database
        from utils.auth.stateless_auth import get_credentials_from_db
        
        github_creds = get_credentials_from_db(user_id, "github")
        if not github_creds or not github_creds.get("access_token"):
            return jsonify({"error": "GitHub not connected"}), 401
        
        token = github_creds['access_token']
        
        if not token:
            return jsonify({"error": "Missing GitHub token in Authorization header"}), 401
            
        logging.info(f"Downloading repository: {repo_full_name}, branch: {branch}")
        
        # Download the repository from GitHub
        download_url = f"https://api.github.com/repos/{repo_full_name}/zipball/{branch}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        response = requests.get(download_url, headers=headers, stream=True)
        
        if response.status_code != 200:
            logging.error(f"Failed to download repository: {response.status_code}, {response.text}")
            return jsonify({
                "error": f"Failed to download repository: {response.status_code}",
                "details": response.text
            }), response.status_code
            
        # Return the zip file as a response with appropriate headers
        repo_name = repo_full_name.replace('/', '-')
        
        # Create a response with the zip file content
        response_data = Response(
            response.iter_content(chunk_size=4096),
            content_type='application/zip'
        )
        
        # Add headers for file download
        response_data.headers['Content-Disposition'] = f'attachment; filename="{repo_name}.zip"'
        
        # Allow requests from frontend URL
        origin = request.headers.get('Origin', '')
        allowed_origins = os.getenv("FRONTEND_URL")
        
        if origin in allowed_origins:
            response_data.headers['Access-Control-Allow-Origin'] = origin
        else:
            # Default to the frontend URL
            response_data.headers['Access-Control-Allow-Origin'] = os.getenv("FRONTEND_URL")
            
        response_data.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response_data.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response_data.headers['Access-Control-Allow-Credentials'] = 'true'
        
        return response_data
        
    except Exception as e:
        logging.error(f"Error downloading GitHub repository: {e}", exc_info=True)
        return jsonify({"error": "Failed to download GitHub repository"}), 500
