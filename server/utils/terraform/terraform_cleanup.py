"""Terraform cleanup utilities for maintaining clean workspace state."""

import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def cleanup_terraform_directory():
    """Clean up terraform configuration files on startup while preserving state files for all users."""
    try:
        terraform_dir = Path("/app/terraform_workdir")
        
        if not terraform_dir.exists():
            terraform_dir.mkdir(exist_ok=True)
            logger.info("Created terraform_workdir directory")
            return
            
        # Clean up configuration files but preserve state files across all user directories
        cleanup_count = 0
        
        # Handle files in the root terraform_workdir (legacy files)
        for item in terraform_dir.iterdir():
            try:
                if item.is_file():
                    cleanup_count += _cleanup_terraform_file(item, "root")
                elif item.is_dir():
                    cleanup_count += _cleanup_terraform_dir(item)
            except Exception as e:
                logger.warning(f"Could not process item {item.name}: {e}")
                
        logger.info(f"Startup terraform cleanup completed. Cleaned {cleanup_count} items total.")
        
    except Exception as e:
        logger.error(f"Error during terraform directory cleanup: {e}")


def _cleanup_terraform_file(file_path: Path, context: str = ""):
    """Clean up a single terraform file if it's not a preserve-worthy file."""
    # Preserve state files, plan files, lock files, and .tf configuration files
    if (file_path.name.startswith('terraform.tfstate') or 
        file_path.name.endswith('.tfplan') or 
        file_path.name == '.terraform.lock.hcl' or
        file_path.name.endswith('.tf')):  # Preserve all .tf files (main.tf, provider.tf, etc.)
        logger.info(f"Preserving {context} file: {file_path.name}")
        return 0
    else:
        file_path.unlink()
        logger.info(f"Removed {context} file: {file_path.name}")
        return 1


def _cleanup_terraform_dir(dir_path: Path): # TODO: This is not always called, and deletes everything in the directory
    """Clean up a terraform directory recursively while preserving important files."""
    if dir_path.name == '.terraform':
        logger.info(f"Preserving .terraform directory: {dir_path.name}")
        return 0
    
    # This is likely a user directory, clean it selectively
    user_cleanup_count = 0
    
    for user_item in dir_path.iterdir():
        try:
            if user_item.is_file():
                user_cleanup_count += _cleanup_terraform_file(user_item, f"{dir_path.name}")
            elif user_item.is_dir():
                if user_item.name == '.terraform':
                    # Preserve .terraform directories
                    logger.info(f"Preserving user .terraform directory: {dir_path.name}/{user_item.name}")
                    continue
                elif user_item.name.startswith('session_'):
                    # This is a session directory, clean it selectively
                    session_cleanup_count = _cleanup_session_dir(user_item, dir_path.name)
                    user_cleanup_count += session_cleanup_count
                else:
                    # Remove other non-session, non-.terraform directories
                    shutil.rmtree(user_item)
                    user_cleanup_count += 1
                    logger.info(f"Removed user directory: {dir_path.name}/{user_item.name}")
        except Exception as e:
            logger.warning(f"Could not remove user item {dir_path.name}/{user_item.name}: {e}")
    
    logger.info(f"Cleaned {user_cleanup_count} items from user directory: {dir_path.name}")
    return user_cleanup_count


def _cleanup_session_dir(session_dir: Path, user_dir_name: str):
    """Clean up a session directory while preserving state files."""
    session_cleanup_count = 0
    
    for session_item in session_dir.iterdir():
        try:
            if session_item.is_file():
                if (session_item.name.startswith('terraform.tfstate') or 
                    session_item.name.endswith('.tfplan') or 
                    session_item.name == '.terraform.lock.hcl' or
                    session_item.name.endswith('.tf')):  # Preserve all .tf files
                    logger.info(f"Preserving session file: {user_dir_name}/{session_dir.name}/{session_item.name}")
                    continue
                else:
                    session_item.unlink()
                    session_cleanup_count += 1
                    logger.info(f"Removed session file: {user_dir_name}/{session_dir.name}/{session_item.name}")
            elif session_item.is_dir() and session_item.name != '.terraform':
                # Remove non-.terraform directories
                shutil.rmtree(session_item)
                session_cleanup_count += 1
                logger.info(f"Removed session directory: {user_dir_name}/{session_dir.name}/{session_item.name}")
        except Exception as e:
            logger.warning(f"Could not remove session item {user_dir_name}/{session_dir.name}/{session_item.name}: {e}")
    
    logger.info(f"Cleaned {session_cleanup_count} items from session directory: {user_dir_name}/{session_dir.name}")
    return session_cleanup_count