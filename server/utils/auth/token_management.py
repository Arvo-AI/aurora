"""
Token storage and retrieval for providers (GCP, AWS, Azure).
Manages storing tokens in Vault and database references.
"""

import json
import logging
import time
from typing import Dict, Optional, List, Any
from utils.db.connection_pool import db_pool

logger = logging.getLogger(__name__)


def store_tokens_in_db(user_id: str, token_data: Dict, provider: str, 
                      subscription_name: str = None, subscription_id: str = None) -> None:
    """
    Store token data in Vault and save secret reference in database.
    Legacy token_data column storage has been removed.
    
    Args:
        user_id: User identifier
        token_data: Token data to store
        provider: Provider name (gcp, aws, azure)
        subscription_name: Azure subscription name (optional)
        subscription_id: Azure subscription ID (optional)
    """
    start_time = time.perf_counter()

    try:
        logger.info(f"[STORE-TOKENS] Starting credential storage operation")
        logger.info(f"[STORE-TOKENS] User ID: {user_id}")
        logger.info(f"[STORE-TOKENS] Provider: {provider}")
        logger.info(f"[STORE-TOKENS] Has subscription info: {bool(subscription_name or subscription_id)}")

        from utils.secrets.secret_ref_utils import SecretRefManager

        secret_manager = SecretRefManager()

        # Create secret name
        safe_user_id = ''.join(c for c in user_id if c.isalnum() or c in '-_')
        secret_name = f"aurora-dev-{safe_user_id}-{provider}-token"

        logger.info(f"[STORE-TOKENS] Generated secret name: {secret_name}")
        logger.debug(f"[STORE-TOKENS] Token data keys: {list(token_data.keys()) if isinstance(token_data, dict) else 'string'}")

        # Store credentials in Vault
        token_json = json.dumps(token_data) if isinstance(token_data, dict) else str(token_data)
        logger.info(f"[STORE-TOKENS] Storing credentials in Vault (size: {len(token_json)} bytes)")

        try:
            secret_ref = secret_manager.store_secret(secret_name, token_json)
        except Exception as secret_error:
            logger.error(f"[STORE-TOKENS] Failed to store credentials in Vault: {secret_error}")
            if "not available" in str(secret_error):
                logger.error("[STORE-TOKENS] Please ensure VAULT_ADDR and VAULT_TOKEN environment variables are configured")
            raise Exception(f"Vault storage failed: {secret_error}")
        
        with db_pool.get_admin_connection() as conn:
            cursor = conn.cursor()
            
            # Store only metadata and secret reference in database
            if provider == "azure":
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, subscription_id, tenant_id, client_id, client_secret) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "tenant_id = EXCLUDED.tenant_id, "
                    "client_id = EXCLUDED.client_id, "
                    "client_secret = EXCLUDED.client_secret, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, subscription_name, subscription_id, 
                     token_data.get("tenant_id"), token_data.get("client_id"), token_data.get("client_secret"))
                )
            elif provider == "aws":
                # Store external_id separately in Vault if present
                external_id_secret_ref = None
                if token_data.get("external_id"):
                    external_id_secret_name = f"aws-external-id-{user_id}"
                    try:
                        from utils.secrets.secret_ref_utils import SecretRefManager
                        ext_secret_manager = SecretRefManager()
                        external_id_secret_ref = ext_secret_manager.store_secret(
                            external_id_secret_name,
                            token_data["external_id"]
                        )
                        logger.info(f"Stored external_id in Vault: {external_id_secret_ref}")
                    except Exception as e:
                        logger.error(f"Failed to store external_id in Vault: {e}")
                        # Continue without external_id storage - it's optional
                
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, client_id, client_secret, email) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "client_id = EXCLUDED.client_id, "
                    "client_secret = EXCLUDED.client_secret, "
                    "email = EXCLUDED.email, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, 
                     token_data.get("role_arn"),  # Store role ARN in client_id
                     external_id_secret_ref)      # Store external_id secret ref in client_secret
                )
            elif provider == "gcp":
                # Extract email from token_data before encryption (if available)
                user_email = token_data.get('email') if isinstance(token_data, dict) else None
                
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, client_id, email) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "client_id = EXCLUDED.client_id, "
                    "email = EXCLUDED.email, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, 'N/A', user_email)  # email will be NULL if not present
                )
            elif provider == "grafana":
                # Store Grafana metadata for display (org info + base URL)
                org_name = token_data.get("org_name") if isinstance(token_data, dict) else None
                org_id = token_data.get("org_id") if isinstance(token_data, dict) else None
                base_url = token_data.get("base_url") if isinstance(token_data, dict) else None
                user_email = token_data.get("user_email") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, subscription_id, client_id, email) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "client_id = EXCLUDED.client_id, "
                    "email = EXCLUDED.email, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, org_name, org_id, base_url, user_email)
                )
            elif provider == "datadog":
                org_name = token_data.get("org_name") if isinstance(token_data, dict) else None
                org_id = token_data.get("org_id") if isinstance(token_data, dict) else None
                site = token_data.get("site") if isinstance(token_data, dict) else None
                service_account = token_data.get("service_account_name") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, subscription_id, client_id, email) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "client_id = EXCLUDED.client_id, "
                    "email = EXCLUDED.email, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, org_name, org_id, site, service_account)
                )
            elif provider == "netdata":
                space_name = token_data.get("space_name") if isinstance(token_data, dict) else None
                base_url = token_data.get("base_url") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, client_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "client_id = EXCLUDED.client_id, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, space_name, base_url)
                )
            elif provider == "scaleway":
                # Scaleway: Store access_key as client_id, organization_id as subscription_id
                # Secret key is stored securely in Vault (via secret_ref)
                access_key = token_data.get("access_key") if isinstance(token_data, dict) else None
                organization_id = token_data.get("organization_id") if isinstance(token_data, dict) else None
                project_id = token_data.get("default_project_id") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, client_id, subscription_id, subscription_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "client_id = EXCLUDED.client_id, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, access_key, organization_id, project_id)
                )
            elif provider == "tailscale":
                # Tailscale: Store OAuth client_id, tailnet as subscription_id, tailnet_name as subscription_name
                # OAuth client_secret and token_data stored in Vault (via secret_ref)
                client_id = token_data.get("client_id") if isinstance(token_data, dict) else None
                tailnet = token_data.get("tailnet") if isinstance(token_data, dict) else None
                tailnet_name = token_data.get("tailnet_name") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, client_id, subscription_id, subscription_name) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "client_id = EXCLUDED.client_id, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, client_id, tailnet, tailnet_name)
                )
            elif provider == "splunk":
                # Splunk: Store base_url as client_id, server_name as subscription_name, username as email
                base_url = token_data.get("base_url") if isinstance(token_data, dict) else None
                server_name = token_data.get("server_name") if isinstance(token_data, dict) else None
                username = token_data.get("username") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, client_id, subscription_name, email) "
                    "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "client_id = EXCLUDED.client_id, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "email = EXCLUDED.email, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, base_url, server_name, username)
                )
            elif provider == "slack":
                # Slack: Store team_id in subscription_id column for efficient workspace lookups
                team_id = token_data.get("team_id") if isinstance(token_data, dict) else None
                
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_id) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, team_id)
                )
            elif provider == "bitbucket":
                # Bitbucket: Store workspace slug as subscription_name, workspace UUID as subscription_id,
                # user email as email, auth_type as client_id
                workspace_slug = token_data.get("workspace_slug") if isinstance(token_data, dict) else None
                workspace_uuid = token_data.get("workspace_uuid") if isinstance(token_data, dict) else None
                user_email = token_data.get("email") if isinstance(token_data, dict) else None
                auth_type = token_data.get("auth_type") if isinstance(token_data, dict) else None

                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, subscription_id, email, client_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "email = EXCLUDED.email, "
                    "client_id = EXCLUDED.client_id, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, workspace_slug, workspace_uuid, user_email, auth_type)
                )
            elif provider == "bitbucket_workspace_selection":
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider) "
                    "VALUES (%s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider)
                )
            elif subscription_name is not None and subscription_id is not None:
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider, subscription_name, subscription_id) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "subscription_name = EXCLUDED.subscription_name, "
                    "subscription_id = EXCLUDED.subscription_id, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider, subscription_name, subscription_id)
                )
            else:
                cursor.execute(
                    "INSERT INTO user_tokens (user_id, secret_ref, provider) "
                    "VALUES (%s, %s, %s) ON CONFLICT (user_id, provider) DO UPDATE "
                    "SET secret_ref = EXCLUDED.secret_ref, "
                    "timestamp = CURRENT_TIMESTAMP, "
                    "is_active = TRUE",
                    (user_id, secret_ref, provider)
                )
            
            conn.commit()

        # Clear the secret cache so fresh value is fetched on next retrieval
        try:
            from utils.secrets.secret_cache import clear_secret_cache
            clear_secret_cache(secret_ref)
        except Exception as cache_error:
            logger.warning(f"[STORE-TOKENS] Failed to clear secret cache: {cache_error}")

        elapsed_time = (time.perf_counter() - start_time) * 1000
        logger.info(f"[STORE-TOKENS]Successfully stored credentials for user {user_id}, provider {provider}")
        logger.info(f"[STORE-TOKENS]Secret reference stored in database")
        logger.info(f"[STORE-TOKENS] ⏱️ Total operation completed in {elapsed_time:.2f}ms")

    except Exception as e:
        elapsed_time = (time.perf_counter() - start_time) * 1000
        logger.error(f"[STORE-TOKENS]Failed to store credentials after {elapsed_time:.2f}ms")
        logger.error(f"[STORE-TOKENS] User: {user_id}, Provider: {provider}")
        logger.error(f"[STORE-TOKENS] Error: {e}")
        raise


def get_token_data(user_id: str, provider: str) -> Optional[Dict]:
    """
    Retrieve token data from Vault only.
    Legacy token_data column support has been removed.

    Args:
        user_id: User identifier
        provider: Provider name (gcp, aws, azure) or list of providers

    Returns:
        Token data dictionary or empty dict if not found
    """
    start_time = time.perf_counter()

    try:
        logger.debug(f"[GET-TOKENS] Starting credential retrieval for user {user_id}, provider(s): {provider}")

        # Handle list provider types - get first available provider
        if isinstance(provider, list):
            logger.debug(f"[GET-TOKENS] Searching for credentials across {len(provider)} providers")
            from utils.secret_ref_utils import get_user_token_data

            for i, p in enumerate(provider):
                logger.debug(f"[GET-TOKENS] Trying provider {i+1}/{len(provider)}: {p}")
                token_data = get_user_token_data(user_id, p)
                if token_data:
                    elapsed_time = (time.perf_counter() - start_time) * 1000
                    logger.debug(f"[GET-TOKENS]Found credentials for provider: {p} in {elapsed_time:.2f}ms")
                    return token_data

            elapsed_time = (time.perf_counter() - start_time) * 1000
            logger.debug(f"[GET-TOKENS]️ No credentials found for any provider in list ({elapsed_time:.2f}ms)")
            return {}
        else:
            # Use Vault for single providers
            logger.debug(f"[GET-TOKENS] Single provider credential lookup")
            from utils.secrets.secret_ref_utils import get_user_token_data
            token_data = get_user_token_data(user_id, provider)

            elapsed_time = (time.perf_counter() - start_time) * 1000
            if token_data:
                logger.debug(f"[GET-TOKENS]Successfully retrieved credentials in {elapsed_time:.2f}ms")
            else:
                logger.debug(f"[GET-TOKENS]️ No credentials found for provider: {provider} ({elapsed_time:.2f}ms)")

            return token_data if token_data else {}

    except Exception as e:
        elapsed_time = (time.perf_counter() - start_time) * 1000
        logger.error(f"[GET-TOKENS]Failed to fetch credentials after {elapsed_time:.2f}ms")
        logger.error(f"[GET-TOKENS] User: {user_id}, Provider(s): {provider}")
        logger.error(f"[GET-TOKENS] Error: {e}")
        return {}
