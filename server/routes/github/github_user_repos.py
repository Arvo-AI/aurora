"""
GitHub user repositories endpoint that works with user ID authentication
"""
import logging
import requests
from flask import Blueprint, jsonify, request
from utils.auth.stateless_auth import get_credentials_from_db

github_user_repos_bp = Blueprint('github_user_repos', __name__)
logger = logging.getLogger(__name__)

def create_cors_response(data=None, status=200):
    """Create a response with CORS headers"""
    response = jsonify(data) if data else jsonify({})
    response.status_code = status
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-User-ID, Authorization'
    return response

def get_user_id_from_request():
    """Extract user ID from request headers"""
    return request.headers.get('X-User-ID') or request.headers.get('X-User-ID')

@github_user_repos_bp.route("/user-repos", methods=["GET", "OPTIONS"])
def get_user_repos():
    """Fetch repositories for a user using their stored GitHub credentials"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return create_cors_response({"error": "User ID required"}, 400)
        
        # Get stored GitHub credentials for this user
        github_creds = get_credentials_from_db(user_id, "github")
        if not github_creds or not github_creds.get("access_token"):
            # This should return 401 instead of 200 btw
            return create_cors_response({"error": "No GitHub credentials found", "repos": []}, 200)
        
        token = github_creds["access_token"]
        
        # Fetch repositories from GitHub API with pagination
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        all_repos = []
        page = 1
        per_page = 100  # Maximum per page allowed by GitHub API
        
        while True:
            response = requests.get(
                "https://api.github.com/user/repos",
                headers=headers,
                params={"sort": "updated", "per_page": per_page, "page": page}
            )
            
            if response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code}")
                return create_cors_response({"error": "Failed to fetch repositories", "repos": []}, 200)
            
            repos = response.json()
            
            # If no repos returned, we've reached the end
            if not repos:
                break
                
            all_repos.extend(repos)
            
            # If we got fewer repos than requested, we've reached the end
            if len(repos) < per_page:
                break
                
            page += 1
            
            # Safety check to prevent infinite loops
            if page > 50:  # This would be 5,000 repos, which is unrealistic for most users
                logger.warning(f"Hit pagination safety limit for user repositories")
                break
        
        logger.info(f"Fetched {len(all_repos)} repositories for user")
        
        # Filter and simplify repository data
        simplified_repos = []
        for repo in all_repos:
            # Only include repositories the user can push to
            if repo.get("permissions", {}).get("push", False):
                simplified_repos.append({
                    "id": repo["id"],
                    "name": repo["name"],
                    "full_name": repo["full_name"],
                    "private": repo["private"],
                    "html_url": repo.get("html_url", ""),
                    "description": repo.get("description"),
                    "default_branch": repo.get("default_branch", "main"),
                    "updated_at": repo.get("updated_at", ""),
                    "permissions": repo.get("permissions", {}),
                    "owner": {
                        "login": repo["owner"]["login"] if "owner" in repo else "",
                        "avatar_url": repo["owner"]["avatar_url"] if "owner" in repo else ""
                    }
                })
        
        return create_cors_response({"repos": simplified_repos})
        
    except Exception as e:
        logger.error(f"Error fetching user repositories: {e}")
        return create_cors_response({"error": str(e), "repos": []}, 500)

@github_user_repos_bp.route("/user-branches/<path:repo_full_name>", methods=["GET", "OPTIONS"])
def get_user_branches(repo_full_name):
    """Fetch branches for a repository using stored GitHub credentials"""
    if request.method == 'OPTIONS':
        return create_cors_response()
    
    try:
        user_id = get_user_id_from_request()
        if not user_id:
            return create_cors_response({"error": "User ID required"}, 400)
        
        # Get stored GitHub credentials for this user
        github_creds = get_credentials_from_db(user_id, "github")
        if not github_creds or not github_creds.get("access_token"):
            # This should return 401 instead of 200 btw
            return create_cors_response({"error": "No GitHub credentials found", "branches": []}, 200)
        
        token = github_creds["access_token"]
        
        # Fetch branches from GitHub API with pagination
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        all_branches = []
        page = 1
        per_page = 100  # Maximum per page allowed by GitHub API
        
        while True:
            response = requests.get(
                f"https://api.github.com/repos/{repo_full_name}/branches",
                headers=headers,
                params={"per_page": per_page, "page": page}
            )
            
            if response.status_code != 200:
                logger.error(f"GitHub API error: {response.status_code}")
                return create_cors_response({"error": "Failed to fetch branches", "branches": []}, 200)
            
            branches = response.json()
            
            # If no branches returned, we've reached the end
            if not branches:
                break
                
            all_branches.extend(branches)
            
            # If we got fewer branches than requested, we've reached the end
            if len(branches) < per_page:
                break
                
            page += 1
            
            # Safety check to prevent infinite loops
            if page > 100:  # This would be 10,000 branches, which is unrealistic
                logger.warning(f"Hit pagination safety limit for repo {repo_full_name}")
                break
        
        logger.info(f"Fetched {len(all_branches)} branches for repo {repo_full_name}")
        return create_cors_response({"branches": all_branches})
        
    except Exception as e:
        logger.error(f"Error fetching branches: {e}")
        return create_cors_response({"error": str(e), "branches": []}, 500)
