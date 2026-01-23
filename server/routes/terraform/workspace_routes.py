import logging
from typing import Any, Dict, List, Optional

from flask import Blueprint, jsonify, request

from chat.backend.agent.tools.iac.iac_write_tool import get_terraform_directory
from utils.auth.stateless_auth import get_user_id_from_request
from utils.storage.storage import get_storage_manager

logger = logging.getLogger(__name__)

terraform_workspace_bp = Blueprint("terraform_workspace", __name__, url_prefix="/terraform")

_MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


def _list_storage_files(user_id: str, session_id: str) -> List[Dict[str, Any]]:
    """List terraform files from storage for a given session."""
    try:
        storage = get_storage_manager(user_id=user_id)
        prefix = f"{session_id}/terraform_dir/"
        full_prefix = f"users/{user_id}/{prefix}"

        file_infos = storage.list_files(prefix=prefix)
        files = []

        for file_info in file_infos:
            name = file_info["path"].replace(full_prefix, "")
            if name and not name.startswith('.terraform'):
                files.append({
                    "name": name,
                    "path": name,
                    "type": "file",
                    "size": file_info.get("size"),
                    "updated_at": file_info.get("modified"),
                })

        return files
    except Exception as e:
        logger.warning(f"Failed to list storage files for session {session_id}: {e}")
        return []


def _read_storage_file(user_id: str, session_id: str, filename: str) -> Optional[str]:
    """Read a terraform file from storage."""
    try:
        storage = get_storage_manager(user_id=user_id)
        
        file_path = f"{session_id}/terraform_dir/{filename}"

        
        file_info = storage.get_file_info(file_path)
        if file_info:
            size = file_info.get("size")
            if size and size > _MAX_FILE_SIZE_BYTES:
                logger.warning(f"File {filename} too large: {size} bytes")
                return None

        
        content_bytes = storage.download_bytes(file_path)
        return content_bytes.decode('utf-8')

    except UnicodeDecodeError:
        logger.warning(f"File {filename} is not valid UTF-8")
        return None
    except Exception as e:
        logger.warning(f"Failed to read storage file {filename} for session {session_id}: {e}")
        return None


@terraform_workspace_bp.route("/workspace/files", methods=["GET"])
def list_workspace_files():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    session_id = request.args.get("session_id")
    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400

    # Read from storage (persistent)
    files = _list_storage_files(user_id, session_id)
    
    workspace_dir = get_terraform_directory(user_id, session_id)
    return jsonify({
        "files": files,
        "workspace_path": str(workspace_dir),
    })


@terraform_workspace_bp.route("/workspace/file", methods=["GET"])
def read_workspace_file():
    user_id = get_user_id_from_request()
    if not user_id:
        return jsonify({"error": "Authentication required"}), 401

    session_id = request.args.get("session_id")
    path_param = request.args.get("path")

    if not session_id:
        return jsonify({"error": "Missing session_id"}), 400
    if not path_param:
        return jsonify({"error": "Missing path"}), 400

    # Read from storage (persistent)
    content = _read_storage_file(user_id, session_id, path_param)
    
    if content is None:
        return jsonify({"error": "File not found or cannot be read"}), 404
    
    return jsonify({
        "path": path_param,
        "content": content,
        "size": len(content),
    })
