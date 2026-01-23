"""Setup SSH keys locally for development mode."""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def ensure_local_ssh_keys(user_id: str) -> bool:
    """
    Setup SSH keys in local ~/.ssh/ for development mode.
    Called before subprocess.run() commands that might need SSH.
    """
    try:
        from utils.auth.token_management import get_token_data
        from utils.db.connection_pool import db_pool
        
        # Get user's SSH keys
        ssh_keys = {}
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT provider FROM user_tokens WHERE user_id = %s AND provider LIKE %s",
                (user_id, '%_ssh_%')
            )
            
            for row in cursor.fetchall():
                provider = row[0] if isinstance(row, tuple) else row
                try:
                    token_data = get_token_data(user_id, provider)
                    if token_data and 'private_key' in token_data:
                        vm_id = provider.replace('_ssh_', '_')
                        ssh_keys[vm_id] = token_data['private_key']
                except Exception as e:
                    logger.debug(f"Failed to fetch SSH key for {provider}: {e}")
        
        if not ssh_keys:
            logger.debug(f"No SSH keys found for user {user_id}")
            return True
        
        # Determine SSH directory - use HOME from environment or default
        home_dir = os.environ.get('HOME', str(Path.home()))
        ssh_dir = Path(home_dir) / '.ssh'
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        
        # Write each key
        for vm_id, private_key in ssh_keys.items():
            # Normalize the private key to fix line ending issues and ensure trailing newline
            private_key_normalized = private_key.strip().replace('\r\n', '\n').replace('\r', '\n')
            if not private_key_normalized.endswith('\n'):
                private_key_normalized += '\n'
            
            key_file = ssh_dir / f"id_{vm_id}"
            key_file.write_text(private_key_normalized)
            key_file.chmod(0o600)
        
        # Create/update SSH config
        config_file = ssh_dir / 'config'
        config_content = """Host *
    StrictHostKeyChecking no
    UserKnownHostsFile=/dev/null
    LogLevel ERROR
"""
        if not config_file.exists():
            config_file.write_text(config_content)
            config_file.chmod(0o600)
        
        logger.debug(f"Successfully setup {len(ssh_keys)} SSH keys locally")
        return True
        
    except Exception as e:
        logger.error(f"Failed to setup local SSH keys: {e}")
        return False

