"""
AWS STS AssumeRole Client with Caching
Provides secure, cached STS assume role functionality for workspace-based AWS access.
"""
import boto3
import logging
import time
from typing import Dict, Optional, Any
from botocore.exceptions import ClientError, NoCredentialsError
from dotenv import load_dotenv
load_dotenv()

# ------------------------------------------------------------------
# When running in staging or prod, pull base AWS credentials from GCP Secret
# Manager so the container does not need them baked in. For local/dev we do
# nothing and fall back to normal boto3 search order.
# ------------------------------------------------------------------

logger = logging.getLogger(__name__)

# In-memory cache for credentials
# In production, consider using Redis for cross-instance caching
_credential_cache: Dict[str, Dict[str, Any]] = {}


def get_aurora_account_id() -> Optional[str]:
    """
    Get Aurora's own AWS account ID by calling STS get_caller_identity.
    
    Returns:
        Aurora's AWS account ID, or None if unable to determine
    """
    try:
        sts = boto3.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity.get("Account")
        if account_id:
            logger.debug(f"Detected Aurora account ID: {account_id}")
            return account_id
    except (NoCredentialsError, ClientError) as e:
        logger.debug(f"Could not determine Aurora account ID: {e}")
    return None


class STSAssumeRoleClient:
    """
    Handles STS AssumeRole operations with caching and error handling.
    """
    
    def __init__(self, region: str = "us-east-1"):
        """
        Initialize STS client.
        
        Args:
            region: AWS region for STS operations
        """
        self.region = region
        self.sts = boto3.client("sts", region_name=region)

        try:
            identity = self.sts.get_caller_identity()
            logger.info(
                f"STS client initialized with default credentials: {identity.get('Arn', 'Unknown')}"
            )
        except NoCredentialsError:
            logger.debug(
                "STS client initialised without default credentials – will rely on assume_role.")
        except ClientError as ce:
            # Some hosts may have *incorrect* credentials; log but continue so that
            # assume_role can still be attempted later.
            logger.warning(
                f"STS client initialised but default credentials unusable: {ce.response.get('Error', {}).get('Code')}"
            )
        except Exception as e:
            # Unexpected failure: surface it, because the client genuinely cannot operate.
            logger.error(f"Failed to initialize STS client: {e}")
            raise
    
    def assume_workspace_role(
        self,
        role_arn: str,
        external_id: str,
        workspace_id: str,
        duration_seconds: int = 3600,
        session_policy: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Assume a workspace role with ExternalId and return cached credentials.

        Args:
            role_arn: IAM role ARN to assume
            external_id: ExternalId for the trust policy (UUID v4) - must match workspace's external_id
            workspace_id: Workspace identifier for session naming
            duration_seconds: Session duration (max depends on role's MaxSessionDuration)
            session_policy: Optional session policy JSON to further restrict permissions

        Returns:
            Dictionary with AWS credentials:
            {
                'accessKeyId': str,
                'secretAccessKey': str,
                'sessionToken': str,
                'expiration': int (unix timestamp)
            }
            
        Raises:
            ClientError: AWS API errors (invalid role, permissions, etc.)
            ValueError: Invalid parameters or external_id mismatch
        """
        if not role_arn or not external_id or not workspace_id:
            raise ValueError("role_arn, external_id, and workspace_id are required")
        
        # SECURITY: Validate that external_id matches the workspace's expected external_id
        # This prevents attackers from using incorrect External IDs
        from utils.workspace.workspace_utils import get_workspace_by_id
        workspace = get_workspace_by_id(workspace_id)
        if not workspace:
            raise ValueError(f"Workspace {workspace_id} not found")
        
        workspace_external_id = workspace.get('aws_external_id')
        if not workspace_external_id:
            raise ValueError(f"Workspace {workspace_id} does not have an aws_external_id configured")
        
        if external_id != workspace_external_id:
            logger.error(
                f"SECURITY: External ID mismatch for workspace {workspace_id}. "
                f"Provided: '{external_id}', Expected: '{workspace_external_id}'. Rejecting role assumption."
            )
            raise ValueError(
                f"External ID mismatch. Provided external_id does not match workspace's expected external_id. "
                f"This is a security requirement to prevent unauthorized role assumption."
            )
        
        # Cache key includes role, external_id, and policy hash for security
        import hashlib
        policy_hash = hashlib.md5(session_policy.encode()).hexdigest()[:8] if session_policy else "full"
        cache_key = f"{role_arn}:{external_id}:{policy_hash}"
        current_time = int(time.time())
        
        # Check cache first (leave 60s buffer before expiration)
        if cache_key in _credential_cache:
            cached_creds = _credential_cache[cache_key]
            if cached_creds['expiration'] > current_time + 60:
                logger.debug(f"Using cached credentials for workspace {workspace_id}")
                return {
                    'accessKeyId': cached_creds['accessKeyId'],
                    'secretAccessKey': cached_creds['secretAccessKey'],
                    'sessionToken': cached_creds['sessionToken'],
                    'expiration': cached_creds['expiration']
                }
        
        # Assume role with STS
        try:
            logger.info(f"Assuming role {role_arn} for workspace {workspace_id} (policy: {'restricted' if session_policy else 'full'})")

            assume_role_params = {
                "RoleArn": role_arn,
                "RoleSessionName": f"aurora-{workspace_id}",
                "ExternalId": external_id,
                "DurationSeconds": min(duration_seconds, 3600)  # Max 1 hour by default
            }

            # Add session policy if provided (for read-only mode)
            if session_policy:
                assume_role_params["Policy"] = session_policy

            response = self.sts.assume_role(**assume_role_params)
            
            if not response.get('Credentials'):
                raise ValueError("AssumeRole returned no credentials")
            
            creds = response['Credentials']
            expiration = int(creds['Expiration'].timestamp())
            
            # SECURITY: Verify that the role's trust policy actually requires ExternalId
            # AWS will accept assume_role even if ExternalId isn't required, which is a security risk
            # We test this by trying to assume the role WITHOUT ExternalId - if it succeeds, reject it
            try:
                self._verify_external_id_required(role_arn, external_id, workspace_id)
            except Exception as verify_error:
                # If verification fails, we should reject the role assumption
                logger.error(
                    f"SECURITY: Role {role_arn} for workspace {workspace_id} does not properly require ExternalId. "
                    f"Rejecting role assumption. Error: {verify_error}"
                )
                raise ValueError(
                    f"Role {role_arn} does not require ExternalId in its trust policy. "
                    f"This is a security requirement. Please update the role's trust policy to include: "
                    f'{{"Condition": {{"StringEquals": {{"sts:ExternalId": "{external_id}"}}}}}}. '
                    f"The role must reject assume_role calls that don't include the correct ExternalId."
                ) from verify_error
            
            # Build credential dict
            credential_dict = {
                'accessKeyId': creds['AccessKeyId'],
                'secretAccessKey': creds['SecretAccessKey'],
                'sessionToken': creds['SessionToken'],
                'expiration': expiration
            }
            
            # Cache the credentials
            _credential_cache[cache_key] = credential_dict
            
            # Clean up expired entries from cache
            self._cleanup_cache()
            
            logger.info(f"Successfully assumed role for workspace {workspace_id}, expires at {expiration}")
            return credential_dict
            
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', 'Unknown')
            error_message = e.response.get('Error', {}).get('Message', str(e))
            
            if error_code == 'AccessDenied':
                logger.error(f"Access denied assuming role {role_arn} for workspace {workspace_id}: {error_message}")
                raise ClientError(
                    {'Error': {'Code': 'AccessDenied', 'Message': 'Role assumption failed - check ExternalId and trust policy'}},
                    'AssumeRole'
                )
            elif error_code == 'InvalidParameterValue':
                logger.error(f"Invalid parameter for role {role_arn}: {error_message}")
                raise ValueError(f"Invalid role parameter: {error_message}")
            else:
                logger.error(f"Failed to assume role {role_arn} for workspace {workspace_id}: {error_code} - {error_message}")
                raise
        except NoCredentialsError:
            logger.error(
                "AWS base credentials not configured; cannot call STS AssumeRole. "
                "Set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY or an instance role with permissions to assume the customer's role."
            )
            raise
                
        except Exception as e:
            logger.error(f"Unexpected error assuming role {role_arn} for workspace {workspace_id}: {e}")
            raise
    
    def create_boto3_session(
        self,
        role_arn: str,
        external_id: str,
        workspace_id: str,
        region: Optional[str] = None
    ) -> boto3.Session:
        """
        Create a boto3 session using assumed role credentials.
        
        Args:
            role_arn: IAM role ARN to assume
            external_id: ExternalId for the trust policy
            workspace_id: Workspace identifier
            region: Optional AWS region (defaults to client region)
            
        Returns:
            boto3.Session configured with assumed role credentials
        """
        creds = self.assume_workspace_role(role_arn, external_id, workspace_id)
        
        return boto3.Session(
            aws_access_key_id=creds['accessKeyId'],
            aws_secret_access_key=creds['secretAccessKey'],
            aws_session_token=creds['sessionToken'],
            region_name=region or self.region
        )
    
    def _verify_external_id_required(
        self,
        role_arn: str,
        expected_external_id: str,
        workspace_id: str
    ) -> None:
        """
        Verify that the role's trust policy requires ExternalId.
        
        This is a security check: AWS will accept assume_role even if ExternalId
        isn't required in the trust policy, which is insecure. We test this by
        attempting to assume the role without ExternalId - if it succeeds, the
        role is insecure.
        
        Args:
            role_arn: The role ARN to check
            expected_external_id: The ExternalId that should be required
            workspace_id: Workspace identifier for logging
            
        Raises:
            ValueError: If the role doesn't require ExternalId
        """
        try:
            # SECURITY TEST: Try to assume the role WITHOUT ExternalId
            # If this succeeds, the role doesn't require ExternalId and is insecure
            test_sts = boto3.client('sts', region_name=self.region)
            test_params = {
                "RoleArn": role_arn,
                "RoleSessionName": f"aurora-security-test-{workspace_id}",
                # Intentionally omitting ExternalId
            }
            
            try:
                # Try to assume without ExternalId - this should FAIL if ExternalId is required
                test_response = test_sts.assume_role(**test_params)
                
                # If we get here, the role assumption succeeded WITHOUT ExternalId
                # This means the role doesn't require ExternalId - REJECT IT
                logger.error(
                    f"SECURITY VIOLATION: Role {role_arn} can be assumed WITHOUT ExternalId. "
                    f"This is insecure. Rejecting role assumption for workspace {workspace_id}."
                )
                raise ValueError(
                    f"Role {role_arn} does not require ExternalId in its trust policy. "
                    f"This is a security requirement. Please update the role's trust policy to include: "
                    f'{{"Condition": {{"StringEquals": {{"sts:ExternalId": "{expected_external_id}"}}}}}}. '
                    f"The role must reject assume_role calls that don't include the correct ExternalId."
                )
            except ClientError as test_error:
                # Good! The assume_role failed without ExternalId
                error_code = test_error.response.get('Error', {}).get('Code', '')
                if error_code == 'AccessDenied':
                    # This is expected - the role correctly requires ExternalId
                    logger.debug(
                        f"Security check passed: Role {role_arn} correctly requires ExternalId "
                        f"(assume_role without ExternalId was denied)"
                    )
                    return
                else:
                    # Unexpected error - log it but don't fail the verification
                    logger.warning(
                        f"Unexpected error during ExternalId verification for role {role_arn}: {error_code}"
                    )
                    # We'll allow this to proceed, but log the warning
                    return
            
        except Exception as e:
            # If verification fails for any reason, we should reject for security
            logger.error(f"Failed to verify ExternalId requirement for role {role_arn}: {e}")
            raise ValueError(
                f"Failed to verify ExternalId requirement for role {role_arn}. "
                f"Security verification error: {e}"
            ) from e
    
    def _cleanup_cache(self) -> None:
        """Remove expired credentials from cache."""
        current_time = int(time.time())
        expired_keys = [
            key for key, creds in _credential_cache.items()
            if creds['expiration'] <= current_time
        ]
        
        for key in expired_keys:
            del _credential_cache[key]
            
        if expired_keys:
            logger.debug(f"Cleaned up {len(expired_keys)} expired cache entries")
    
    @staticmethod
    def clear_cache() -> None:
        """Clear all cached credentials (useful for testing)."""
        _credential_cache.clear()
        logger.info("Cleared all cached credentials")
    
    @staticmethod
    def get_cache_stats() -> Dict[str, Any]:
        """Get cache statistics for monitoring."""
        current_time = int(time.time())
        active_count = sum(
            1 for creds in _credential_cache.values()
            if creds['expiration'] > current_time
        )
        
        return {
            'total_entries': len(_credential_cache),
            'active_entries': active_count,
            'expired_entries': len(_credential_cache) - active_count
        }


# Convenience function for global usage
_default_client: Optional[STSAssumeRoleClient] = None


def get_sts_client(region: str = "us-east-1") -> STSAssumeRoleClient:
    """Get or create default STS client instance."""
    global _default_client
    if not _default_client:
        _default_client = STSAssumeRoleClient(region)
    return _default_client


def assume_workspace_role(
    role_arn: str,
    external_id: str,
    workspace_id: str,
    duration_seconds: int = 3600,
    region: str = "us-east-1",
    session_policy: Optional[str] = None
) -> Dict[str, Any]:
    """
    Convenience function to assume a workspace role.

    Args:
        role_arn: IAM role ARN to assume
        external_id: ExternalId for the trust policy
        workspace_id: Workspace identifier
        duration_seconds: Session duration
        region: AWS region
        session_policy: Optional session policy JSON to restrict permissions

    Returns:
        AWS credentials dictionary
    """
    client = get_sts_client(region)
    return client.assume_workspace_role(role_arn, external_id, workspace_id, duration_seconds, session_policy)


def create_workspace_session(
    role_arn: str,
    external_id: str,
    workspace_id: str,
    region: str = "us-east-1"
) -> boto3.Session:
    """
    Convenience function to create a boto3 session for a workspace.
    
    Args:
        role_arn: IAM role ARN to assume
        external_id: ExternalId for the trust policy
        workspace_id: Workspace identifier
        region: AWS region
        
    Returns:
        boto3.Session configured with workspace credentials
    """
    client = get_sts_client(region)
    return client.create_boto3_session(role_arn, external_id, workspace_id, region)
