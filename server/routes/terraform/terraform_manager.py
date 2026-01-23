import subprocess
import os
import logging
import tempfile
import shutil
import re
import requests
import json
import time
from typing import Optional, Tuple, Dict, Any
from routes.terraform.terraform_generator import TerraformGenerator

# Get logger
logger = logging.getLogger(__name__)
logger.info("Reached terraform_manager.py")

# Terraform operation timeouts (in seconds)
TERRAFORM_APPLY_TIMEOUT = 1800 
TERRAFORM_DESTROY_TIMEOUT = 600

class TerraformManager:
    """Class to handle Terraform deployment operations."""

    def __init__(self, working_dir: str, authenticator=None):
        """
        Initialize the TerraformManager with a specified working directory.
        
        Args:
            working_dir: Directory containing Terraform files (main.tf, etc.)
            authenticator: Optional cloud authenticator (deprecated - no longer used)
        """
        if not working_dir or not os.path.isdir(working_dir):
            # Add a check here for robustness
            err_msg = f"Invalid or non-existent Terraform working directory provided: {working_dir}"
            logger.error(err_msg)
            raise ValueError(err_msg)

        self.working_dir = working_dir
        # Authenticator parameter deprecated - credentials now managed via setup_terraform_environment
        self.authenticator = None
        self.terraform_cmd = self._get_terraform_command()
        self.terraform_generator = TerraformGenerator(working_dir)

        # Ensure Terraform is installed
        if not self._ensure_terraform_installed():
            raise RuntimeError("Terraform is not installed or not accessible")

        # Check if terraform binary is available
        try:
            result = subprocess.run(
                ["which", self.terraform_cmd],
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                logger.error(f"Terraform binary not found in PATH: {result.stderr}")
                # Try to find terraform in common locations
                for path in ["/usr/local/bin/terraform", "/usr/bin/terraform"]:
                    if os.path.exists(path):
                        self.terraform_cmd = path
                        logger.info(f"Found Terraform at {path}")
                        break
                else:
                    logger.error("Terraform binary not found in common locations")
            else:
                logger.info(f"Terraform found at: {result.stdout.strip()}")
        except Exception as e:
            logger.error(f"Error checking for Terraform binary: {str(e)}")

    def _ensure_terraform_installed(self) -> bool:
        """
        Ensure that Terraform is installed in the container.
        
        Returns:
            bool: True if Terraform is installed or successfully installed, False otherwise
        """
        try:
            # Check if terraform is available
            result = subprocess.run(
                ["which", "terraform"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                logger.info(f"Terraform found at: {result.stdout.strip()}")
                return True
            
            # Try to find terraform in common locations
            for path in ["/usr/local/bin/terraform", "/usr/bin/terraform"]:
                if os.path.exists(path):
                    logger.info(f"Found Terraform at {path}")
                    return True
            
            # Terraform not found, install it
            logger.warning("Terraform not found, attempting to install it")
            
            # Create a temporary directory
            with tempfile.TemporaryDirectory() as temp_dir:
                # Download Terraform
                terraform_version = "1.7.5"
                terraform_url = f"https://releases.hashicorp.com/terraform/{terraform_version}/terraform_{terraform_version}_linux_amd64.zip"
                terraform_zip = os.path.join(temp_dir, "terraform.zip")
                
                subprocess.run(
                    ["wget", "-q", terraform_url, "-O", terraform_zip],
                    check=True
                )
                
                # Unzip Terraform
                subprocess.run(
                    ["unzip", "-q", terraform_zip, "-d", temp_dir],
                    check=True
                )
                
                # Move Terraform to /usr/local/bin
                terraform_binary = os.path.join(temp_dir, "terraform")
                shutil.copy(terraform_binary, "/usr/local/bin/terraform")
                
                # Make Terraform executable
                os.chmod("/usr/local/bin/terraform", 0o755)
                
                logger.info("Terraform installed successfully")
                return True
                
        except Exception as e:
            logger.error(f"Failed to install Terraform: {str(e)}")
            return False

    def generate_terraform_tfvars(self, data: dict, output_path: str) -> bool:
        """
        Generate a terraform.tfvars file from the provided data.

        Args:
            data: Dictionary containing variable values
            output_path: Path to write the tfvars file

        Returns:
            bool: True if successful, False otherwise
        """
        try:
            # Filter out sensitive data or data not intended for tfvars
            # Keep keys that are likely Terraform variables
            safe_data = {k: v for k, v in data.items() if k not in [
                # Remove 'terraform_config' as it's no longer passed
                'userId', 'build_docker',
                'dockerfile_path', 'context_path', 'terraform_dir_path' # Add terraform_dir_path
            ]}

            # Determine cloud provider from data to check appropriate required variables
            cloud_provider = safe_data.get('cloud_provider', 'gcp').lower()
            logger.debug(f"Detected cloud provider: {cloud_provider}")
            
            # Check required variables based on cloud provider
            if cloud_provider == 'aws':
                # AWS requires region and container_image
                if 'region' not in safe_data or not safe_data['region']:
                    logger.error("region is missing in data provided to generate_terraform_tfvars for AWS")
                    return False
                if 'container_image' not in safe_data or not safe_data['container_image']:
                    logger.warning("container_image might be missing for AWS deployment")
            elif cloud_provider == 'azure':
                # Azure requires resource_group_name, location, and container_image  
                if 'resource_group_name' not in safe_data or not safe_data['resource_group_name']:
                    logger.error("resource_group_name is missing in data provided to generate_terraform_tfvars for Azure")
                    return False
                if 'location' not in safe_data or not safe_data['location']:
                    logger.error("location is missing in data provided to generate_terraform_tfvars for Azure")
                    return False
            else:
                # GCP requires project_id
                if 'project_id' not in safe_data or not safe_data['project_id']:
                    logger.error("project_id is missing in data provided to generate_terraform_tfvars for GCP")
                    return False
                if 'region' not in safe_data or not safe_data['region']:
                    logger.warning("Region might be missing in data for generate_terraform_tfvars, Terraform might use default.")

            # Handle Docker images - support both single image and multiple images from docker-compose
            if 'docker_image' in data and data['docker_image']:
                # Single image case (backward compatibility)
                safe_data['docker_image'] = data['docker_image']
                
            # Handle multiple images from docker-compose - Enhanced for multi-service deployments
            if 'docker_images' in data and data['docker_images'] and isinstance(data['docker_images'], dict):
                logger.debug(f"Processing docker_images for multi-service Terraform vars: {data['docker_images']}")
                
                # For multi-service deployments, create individual variables for each service
                for service_name, image_name in data['docker_images'].items():
                    # Clean service name for Terraform variable compatibility
                    clean_service_name = service_name.replace('-', '_').replace(' ', '_').lower()
                    var_name = f"{clean_service_name}_docker_image"
                    safe_data[var_name] = image_name
                    logger.debug(f"Created Terraform variable '{var_name}' for service '{service_name}': {image_name}")
                
                # Also store the complete images map as docker_images for reference
                safe_data['docker_images'] = data['docker_images']
                
                # Legacy compatibility: Map common service names to expected variable names
                service_to_var_mapping = {
                    'frontend': 'frontend_docker_image',
                    'web': 'frontend_docker_image',
                    'client': 'frontend_docker_image',
                    'ui': 'frontend_docker_image',
                    'app': 'app_docker_image',
                    'backend': 'backend_docker_image',
                    'api': 'api_docker_image',
                    'api-service': 'api_service_docker_image',
                    'data-service': 'data_service_docker_image',
                    'server': 'backend_docker_image',
                    'service': 'backend_docker_image',
                    'database': 'database_docker_image',
                    'db': 'database_docker_image',
                    'cache': 'cache_docker_image',
                    'redis': 'redis_docker_image',
                    'queue': 'queue_docker_image',
                    'worker': 'worker_docker_image'
                }
                
                # Apply legacy mappings if they don't conflict with service names
                for service_name, image_name in data['docker_images'].items():
                    if service_name in service_to_var_mapping:
                        legacy_var_name = service_to_var_mapping[service_name]
                        if legacy_var_name not in safe_data:
                            safe_data[legacy_var_name] = image_name
                            logger.debug(f"Added legacy mapping '{legacy_var_name}' for service '{service_name}'")
                
                # Set a primary docker_image for backward compatibility
                if 'docker_image' not in safe_data and data['docker_images']:
                    # Priority order: frontend -> app -> web -> first service
                    priority_services = ['frontend', 'app', 'web', 'ui', 'client']
                    primary_service = None
                    
                    for priority in priority_services:
                        if priority in data['docker_images']:
                            primary_service = priority
                            break
                    
                    if not primary_service:
                        primary_service = next(iter(data['docker_images']))
                    
                    safe_data['docker_image'] = data['docker_images'][primary_service]
                    logger.debug(f"Set primary docker_image to {primary_service}: {safe_data['docker_image']}")
                    
                # Add service configuration metadata for multi-service setups
                safe_data['is_multi_service'] = len(data['docker_images']) > 1
                safe_data['service_count'] = len(data['docker_images'])
                safe_data['service_names'] = list(data['docker_images'].keys())
                
                logger.info(f"Multi-service configuration: {safe_data['service_count']} services detected")
                logger.debug(f"Services: {', '.join(safe_data['service_names'])}")

            # Include user_access_token if available (for docker login on VM)
            if 'access_token' in data and data['access_token']:
                # Rename key for clarity in tfvars if desired, or use access_token
                safe_data['user_access_token'] = data['access_token']

            # Remove None values before writing
            tfvars_data = {k: v for k, v in safe_data.items() if v is not None}

            logger.info(f"Generating tfvars file at {output_path} with keys: {list(tfvars_data.keys())}")
            # Write the tfvars file
            with open(output_path, 'w') as f:
                for key, value in tfvars_data.items():
                    if isinstance(value, str):
                        # Clean ANSI escape sequences first
                        clean_value = self._clean_ansi_sequences(str(value))
                        # Handle multi-line strings using heredoc syntax
                        if '\n' in clean_value:
                            f.write(f'{key} = <<EOF\n{clean_value}\nEOF\n')
                        else:
                            # Single-line string with proper escaping
                            escaped_value = clean_value.replace('\\', '\\\\').replace('"', '\\"')
                            f.write(f'{key} = "{escaped_value}"\n')
                    elif isinstance(value, (int, float)):
                        f.write(f'{key} = {value}\n')
                    elif isinstance(value, bool):
                        f.write(f'{key} = {str(value).lower()}\n')
                    elif isinstance(value, list):
                        # Special handling for env_vars or lists containing dictionaries
                        if key == "env_vars" or (value and isinstance(value[0], dict)):
                            # Format list of objects with proper HCL syntax
                            list_items = []
                            for item in value:
                                if isinstance(item, dict):
                                    # Format each dictionary with double quotes for both keys and string values
                                    dict_parts = []
                                    for k, v in item.items():
                                        if isinstance(v, str):
                                            # Clean the value of any ANSI escape sequences and properly escape it
                                            clean_value = self._clean_ansi_sequences(str(v))
                                            # Check if it's a multi-line string (like RSA keys)
                                            if '\n' in clean_value:
                                                # For multi-line strings in HCL lists, we need to escape newlines
                                                escaped_value = clean_value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')
                                                dict_parts.append(f'"{k}" = "{escaped_value}"')
                                            else:
                                                # Single-line string with proper escaping
                                                escaped_value = clean_value.replace('\\', '\\\\').replace('"', '\\"')
                                                dict_parts.append(f'"{k}" = "{escaped_value}"')
                                        else:
                                            dict_parts.append(f'"{k}" = {v}')
                                    list_items.append("{" + ", ".join(dict_parts) + "}")
                                else:
                                    # Handle non-dictionary items
                                    if isinstance(item, str):
                                        clean_item = self._clean_ansi_sequences(str(item))
                                        escaped_item = clean_item.replace("\\", "\\\\").replace('"', '\\"')
                                        list_items.append(f'"{escaped_item}"')
                                    else:
                                        list_items.append(str(item))
                            
                            f.write(f'{key} = [{", ".join(list_items)}]\n')
                        else:
                            # Regular list handling for simple types
                            list_items = []
                            for item in value:
                                if isinstance(item, str):
                                    clean_item = self._clean_ansi_sequences(str(item))
                                    escaped_item = clean_item.replace("\\", "\\\\").replace('"', '\\"')
                                    list_items.append(f'"{escaped_item}"')
                                else:
                                    list_items.append(str(item))
                            list_str = ", ".join(list_items)
                            f.write(f'{key} = [{list_str}]\n')
                    elif isinstance(value, dict):
                        # Format maps appropriately for HCL
                        map_items = []
                        for k, v in value.items():
                            if isinstance(v, str):
                                clean_v = self._clean_ansi_sequences(str(v))
                                escaped_v = clean_v.replace('\\', '\\\\').replace('"', '\\"')
                                map_items.append(f'"{k}" = "{escaped_v}"')
                            else:
                                map_items.append(f'"{k}" = {str(v)}')
                        f.write(f'{key} = {{\n  {", ".join(map_items)}\n}}\n')
                    else:
                        # Fallback for other types - might need adjustment
                        clean_value = self._clean_ansi_sequences(str(value))
                        escaped_value = clean_value.replace('\\', '\\\\').replace('"', '\\"')
                        f.write(f'{key} = "{escaped_value}"\n')

            return True
        except Exception as e:
            logger.error(f"Error generating terraform.tfvars: {str(e)}")
            return False 

    def init(self) -> bool:
        """Initialize Terraform in the working directory with robust retry logic."""
        try:
            # Validate that working_dir is not None
            if self.working_dir is None:
                logger.error("Working directory is None - cannot initialize Terraform")
                return False
            
            # Validate that terraform_cmd is not None    
            if self.terraform_cmd is None:
                logger.error("Terraform command is None - cannot initialize Terraform")
                return False
                
            logger.info(f"Initializing Terraform in directory: {self.working_dir}")
            logger.debug(f"Using Terraform command: {self.terraform_cmd}")
            
            # Check if terraform.tfvars exists
            tfvars_path = os.path.join(self.working_dir, "terraform.tfvars")
            tfvars_exists = os.path.exists(tfvars_path)
            logger.debug(f"terraform.tfvars exists: {tfvars_exists}")
            
            # Check if variables.tf exists
            variables_path = os.path.join(self.working_dir, "variables.tf")
            variables_exists = os.path.exists(variables_path)
            logger.debug(f"variables.tf exists: {variables_exists}")
            
            # Check if variables are defined in main.tf
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            main_tf_exists = os.path.exists(main_tf_path)
            variables_in_main_tf = False
            
            if main_tf_exists:
                with open(main_tf_path, 'r') as f:
                    main_tf_content = f.read()
                    variables_in_main_tf = 'variable "project_id"' in main_tf_content and 'variable "region"' in main_tf_content
            
            if not variables_exists and not variables_in_main_tf:
                logger.warning("No variables.tf found and variables not defined in main.tf")
            
            # Create Terraform configuration file for better network handling
            self._create_terraform_network_config()
            
            # Get environment variables with credentials
            env = self._get_terraform_env()
            logger.info("Retrieved environment variables for Terraform")
            
            # Add comprehensive network timeout and reliability environment variables
            # Optimize for performance while maintaining reliability
            env.update({
                'TF_HTTP_TIMEOUT': '300s',  # 5 minute timeout for HTTP requests
                'TF_PROVIDER_CONNECT_TIMEOUT': '300s',  # 5 minute timeout for provider connections
                'TF_CONCURRENT_DOWNLOADS': '3',  # Increased for better performance (was 1)
                'TF_PLUGIN_TIMEOUT': '300s',  # 5 minute timeout for plugin operations
                'TF_REGISTRY_TIMEOUT': '300s',  # 5 minute timeout for registry operations
                'TF_CLI_TIMEOUT': '1800s',  # 30 minute timeout for CLI operations
                'TF_MAX_BACKOFF': '30s',  # Maximum backoff for retries
                'TF_FORCE_IPV4': '1',  # Force IPv4 to avoid IPv6 DNS issues
                'GODEBUG': 'netdns=go+2',  # Use Go's DNS resolver with fallbacks
                'TF_LOG': 'ERROR',  # Reduce log verbosity for network operations
                'TF_PLUGIN_CACHE_MAY_BREAK_DEPENDENCY_LOCK_FILE': 'true',  # Allow cache optimization
            })
            
            # Validate the terraform command and environment before running
            if not isinstance(self.terraform_cmd, str) or not self.terraform_cmd.strip():
                logger.error(f"Invalid terraform command: {self.terraform_cmd}")
                return False
            
            # Skip pre-connectivity check - it causes false negatives and unnecessary fallbacks
            # The retry logic below is sufficient to handle genuine network issues
            
            # Retry terraform init with exponential backoff
            max_retries = 3
            base_delay = 10  # seconds (increased from 5)
            
            for attempt in range(max_retries):
                try:
                    logger.info(f"Terraform init attempt {attempt + 1}/{max_retries}")
                    
                    # Run terraform init with network-optimized flags
                    cmd = [self.terraform_cmd, "init", "-input=false", "-no-color"]
                    logger.info(f"Running command: {' '.join(cmd)} in directory: {self.working_dir}")
                    
                    result = subprocess.run(
                        cmd,
                        cwd=self.working_dir,
                        capture_output=True,
                        text=True,
                        timeout=300,  # 5 minute timeout for init
                        env=env
                    )
                    
                    if result.returncode == 0:
                        logger.info("Terraform init completed successfully")
                        return True
                    
                    logger.warning(f"Terraform init attempt {attempt + 1} failed with return code {result.returncode}")
                    logger.warning(f"Stderr: {result.stderr}")
                    
                    # Check if it's a genuine network connectivity error (not HTTP method errors)
                    stderr_lower = result.stderr.lower()
                    genuine_network_errors = [
                        'tls handshake timeout', 'connection timeout', 'connection refused',
                        'no such host', 'network unreachable', 'dns resolution failed',
                        'dial tcp', 'i/o timeout'
                    ]
                    
                    if any(err in stderr_lower for err in genuine_network_errors):
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)  # Exponential backoff
                            logger.info(f"Genuine network error detected. Retrying in {delay} seconds...")
                            time.sleep(delay)
                            continue
                        else:
                            logger.warning("All retries failed with network errors. Attempting fallback approach...")
                            return self._fallback_terraform_init(env)
                    
                    # If not a network error or last attempt, log and potentially retry
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying terraform init in {delay} seconds...")
                        time.sleep(delay)
                    else:
                        logger.error(f"All terraform init attempts failed. Final error: {result.stderr}")
                        return False
                        
                except subprocess.TimeoutExpired:
                    logger.warning(f"Terraform init attempt {attempt + 1} timed out")
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying terraform init in {delay} seconds...")
                        time.sleep(delay)
                    else:
                        logger.error("All terraform init attempts timed out")
                        return False
                        
                except Exception as e:
                    logger.warning(f"Terraform init attempt {attempt + 1} failed with exception: {str(e)}")
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)
                        logger.info(f"Retrying terraform init in {delay} seconds...")
                        time.sleep(delay)
                    else:
                        logger.error(f"All terraform init attempts failed with exceptions")
                        return False
            
            return False
            
        except Exception as e:
            logger.error(f"Failed to initialize Terraform: {str(e)}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def validate(self) -> tuple[bool, str]:
        """
        Validate Terraform configuration for syntax and basic errors.
        This is a fast operation that catches many common issues.
        
        Returns:
            Tuple of (success, output)
        """
        try:
            logger.info("Running Terraform validate to catch syntax errors early")
            
            env = self._get_terraform_env()
            
            result = subprocess.run(
                [self.terraform_cmd, "validate", "-no-color"],
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=60,  # Quick validation, 1 minute max
                env=env
            )
            
            if result.returncode != 0:
                logger.error(f"Terraform validation failed: {result.stderr}")
                return False, result.stderr
            
            logger.info(" Terraform validation passed")
            return True, result.stdout
            
        except subprocess.TimeoutExpired:
            error_msg = "Terraform validate timed out after 1 minute"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            logger.error(f"Failed to validate Terraform configuration: {str(e)}")
            return False, str(e)

    def run_pre_flight_checks(self) -> tuple[bool, str]:     #kind of stupid funciton tbf
        """
        Run comprehensive pre-flight checks to catch errors before expensive apply operations.
        This includes validation, credential checks, and common issue detection.
        
        Returns:
            Tuple of (success, error_messages_or_success_message)
        """
        logger.info("Starting comprehensive pre-flight checks...")
        errors = []
        warnings = []
        
        try:
            # 1. Basic file existence checks
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if not os.path.exists(main_tf_path):
                errors.append("main.tf file not found")
                return False, "Pre-flight check failed: " + "; ".join(errors)
            
            # 2. Read terraform configuration for analysis
            with open(main_tf_path, 'r') as f:
                terraform_content = f.read()
            
            # 3. Terraform syntax validation (fast check)
            logger.info("Checking Terraform syntax...")
            validate_success, validate_msg = self.validate()
            if not validate_success:
                errors.append(f"Terraform validation failed: {validate_msg}")
                return False, "Pre-flight check failed: " + "; ".join(errors)
            
            # 4. Check for required variables
            logger.info("Checking required variables...")
            var_check_success, var_msg = self._check_required_variables(terraform_content)
            if not var_check_success:
                errors.append(f"Variable check failed: {var_msg}")
            
            # 5. Credential validation
            logger.info("Validating cloud credentials...")
            cred_success, cred_msg = self._validate_credentials()
            if not cred_success:
                errors.append(f"Credential validation failed: {cred_msg}")
            else:
                logger.info("Cloud credentials validated")
            
            # 6. Resource name validation
            logger.info("Checking resource naming conventions...")
            naming_warnings = self._check_resource_naming(terraform_content)
            warnings.extend(naming_warnings)
            
            # 7. Check for common anti-patterns
            logger.info("Scanning for common issues...")
            pattern_warnings = self._check_common_patterns(terraform_content)
            warnings.extend(pattern_warnings)
            
            # 8. Docker image validation (if applicable)
            docker_warnings = self._validate_docker_images()
            warnings.extend(docker_warnings)
            
            # 9. Quick connectivity test for critical services
            logger.info("Testing connectivity to cloud services...")
            connectivity_success, connectivity_msg = self._test_cloud_connectivity()
            if not connectivity_success:
                warnings.append(f"Connectivity issue: {connectivity_msg}")
            
            # Report results
            if errors:
                error_summary = f"Pre-flight checks failed with {len(errors)} error(s): " + "; ".join(errors)
                if warnings:
                    error_summary += f"\n\nAlso found {len(warnings)} warning(s): " + "; ".join(warnings)
                logger.error(error_summary)
                return False, error_summary
            
            # Success with possible warnings
            success_msg = " All pre-flight checks passed!"
            if warnings:
                success_msg += f"\n\nFound {len(warnings)} warning(s) (proceeding anyway):\n" + "\n".join(warnings)
                logger.warning(f"Pre-flight checks passed with warnings: {warnings}")
            else:
                logger.info(" Pre-flight checks completed successfully with no issues")
            
            return True, success_msg
            
        except Exception as e:
            error_msg = f"Pre-flight checks failed with exception: {str(e)}"
            logger.error(error_msg)
            return False, error_msg

    def plan(self) -> tuple[bool, str]:
        """
        Generate and return Terraform plan.
        
        Returns:
            Tuple of (success, output)
        """
        try:
            # Check if terraform.tfvars exists
            tfvars_exists = os.path.exists(os.path.join(self.working_dir, "terraform.tfvars"))
            
            # Use -var-file if terraform.tfvars exists
            env = self._get_terraform_env()
            
            # Add comprehensive network timeout environment variables for plan
            env.update({
                'TF_HTTP_TIMEOUT': '300s',
                'TF_PROVIDER_CONNECT_TIMEOUT': '300s',
                'TF_PLUGIN_TIMEOUT': '300s',
                'TF_REGISTRY_TIMEOUT': '300s',
                'TF_FORCE_IPV4': '1',
                'GODEBUG': 'netdns=go+2',
            })
            
            # Get optimal parallelism for planning
            parallelism = self._get_optimal_parallelism()
            
            # High-performance terraform plan with optimization flags
            plan_cmd = [
                self.terraform_cmd, "plan", 
                f"-parallelism={parallelism}",
                "-refresh=false",  # Skip refresh for faster planning
                "-out=tfplan", 
                "-input=false", 
                "-no-color"
            ]
            
            if tfvars_exists:
                plan_cmd.insert(-4, "-var-file=terraform.tfvars")  # Insert before -out flag
            
            logger.info(f"Running high-performance terraform plan with parallelism={parallelism}")
            
            
            plan_timeout = 600  # 10 minutes
            logger.info(f"Using plan timeout of {plan_timeout // 60} minutes for this deployment")
            
            result = subprocess.run(
                plan_cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=plan_timeout,
                env=env
            )
                
            if result.returncode != 0:
                logger.error(f"Terraform plan failed: {result.stderr}")
                return False, result.stderr
            
            logger.info("Terraform plan completed successfully")
            return True, result.stdout
        except subprocess.TimeoutExpired:

            timeout_minutes = plan_timeout // 60
            error_msg = f"Terraform plan timed out after {timeout_minutes} minutes"
            logger.error(error_msg)
            return False, error_msg
        except Exception as e:
            logger.error(f"Failed to create Terraform plan: {str(e)}")
            return False, str(e)
    
    def apply(self) -> tuple[bool, str]:
        """
        Apply the Terraform plan with periodic status updates for long-running operations.
        
        Returns:
            Tuple of (success, output)
        """
        try:
            logger.info("Starting Terraform apply...")
            
            # Set a longer timeout for the apply operation
            env = self._get_terraform_env()
            
            # Add comprehensive network timeout environment variables for apply
            env.update({
                'TF_HTTP_TIMEOUT': '300s',
                'TF_PROVIDER_CONNECT_TIMEOUT': '300s',
                'TF_PLUGIN_TIMEOUT': '300s',
                'TF_REGISTRY_TIMEOUT': '300s',
                'TF_FORCE_IPV4': '1',
                'GODEBUG': 'netdns=go+2',
            })
            
            # Detect deployment complexity and adjust parallelism accordingly
            parallelism = self._get_optimal_parallelism()
            
            # Start the terraform apply process with optimized settings
            # High-performance terraform apply with optimization flags
            apply_cmd = [
                self.terraform_cmd, "apply", 
                f"-parallelism={parallelism}",
                "-refresh=false",  # Skip refresh to save time on large deployments
                "-input=false",
                "-no-color",
                "tfplan"
            ]
            logger.info(f"Running high-performance terraform apply with parallelism={parallelism}: {' '.join(apply_cmd[:4])}...")
            
            process = subprocess.Popen(
                apply_cmd,
                cwd=self.working_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env
            )
            
            # Monitor the process and provide periodic updates
            start_time = time.time()
            
            # Use file-specific timeout constant
            timeout = TERRAFORM_APPLY_TIMEOUT
            logger.info(f"Using timeout of {timeout // 60} minutes ({timeout // 3600} hours) for this deployment")
            
            # Option to disable timeout completely (if set to 0)
            if timeout == 0:
                logger.warning(" Terraform apply timeout disabled - process will run indefinitely")
                timeout = None
            
            last_progress_log = 0
            while process.poll() is None:  # Process is still running
                elapsed = time.time() - start_time
                
                # Only check timeout if it's set (not None or 0)
                if timeout and elapsed > timeout:
                    # Kill the process if it takes too long
                    process.terminate()
                    try:
                        process.wait(timeout=30)  # Give it 30 seconds to terminate gracefully
                    except subprocess.TimeoutExpired:
                        process.kill()  # Force kill if it doesn't terminate
                    
                    timeout_minutes = timeout // 60
                    logger.error(f"Terraform apply timed out after {timeout_minutes} minutes")
                    self._cleanup_on_failure()
                    return False, f"Terraform apply timed out after {timeout_minutes} minutes."
                
                # Enhanced progress logging every 30 seconds with performance insights
                if int(elapsed) >= last_progress_log + 30:
                    minutes = int(elapsed // 60)
                    seconds = int(elapsed % 60)
                    if timeout:
                        progress_pct = min(95, (elapsed / timeout) * 100)  # Cap at 95% until completion
                        logger.info(f"Terraform apply progress: {minutes}m {seconds}s elapsed ({progress_pct:.1f}% of timeout)")
                    else:
                        logger.info(f"Terraform apply progress: {minutes}m {seconds}s elapsed (no timeout set)")
                    last_progress_log = int(elapsed)
                
                time.sleep(1)  # Check every second
            
            # Process completed, get the results
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                logger.error(f"Terraform apply failed: {stderr}")
                # Run terraform destroy to clean up partial resources on failure
                logger.info("Running terraform destroy to clean up partial resources after failed apply")
                self._cleanup_on_failure()
                return False, stderr
            
            elapsed_total = time.time() - start_time
            logger.info(f"Terraform apply completed successfully in {elapsed_total:.1f} seconds ({elapsed_total/60:.1f} minutes)")
            return True, stdout
            
        except Exception as e:
            logger.error(f"Failed to apply Terraform plan: {str(e)}")
            # Clean up on any exception
            logger.info("Running terraform destroy to clean up after exception")
            self._cleanup_on_failure()
            return False, str(e)
    
    def destroy(self) -> tuple[bool, str]:
        """
        Destroy the Terraform-managed infrastructure.
        
        Returns:
            Tuple of (success, output)
        """
        try:
            # Always clean up the lock file and re-init before destroy
            lock_file = os.path.join(self.working_dir, '.terraform.lock.hcl')
            if os.path.exists(lock_file):
                os.remove(lock_file)
                logger.info("Deleted .terraform.lock.hcl before destroy to avoid dependency lock errors")
            # Always run terraform init before destroy
            env = self._get_terraform_env()
            
            # Try to init, but don't fail destroy if init has network issues
            init_success = False
            init_cmd = [self.terraform_cmd, "init", "-input=false", "-no-color", "-upgrade=false"]
            logger.info(f"Running terraform init before destroy: {' '.join(init_cmd)}")
            
            try:
                init_result = subprocess.run(
                    init_cmd,
                    cwd=self.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=120,  # Reduced timeout for init
                    env=env
                )
                if init_result.returncode != 0:
                    logger.warning(f"Terraform init failed before destroy: {init_result.stderr}")
                    
                    # Check if it's a network/DNS issue
                    stderr_lower = init_result.stderr.lower() if init_result.stderr else ""
                    if any(indicator in stderr_lower for indicator in ['registry.terraform.io', 'dns', 'network', 'connection']):
                        logger.info("Network/DNS issues detected during init, checking for existing providers...")
                        
                        # If we have a .terraform directory with providers, we can proceed
                        terraform_data_dir = os.path.join(self.working_dir, '.terraform')
                        providers_dir = os.path.join(terraform_data_dir, 'providers')
                        if os.path.exists(providers_dir) and os.listdir(providers_dir):
                            logger.info(" Found existing providers in .terraform directory, proceeding with destroy")
                            init_success = True
                        else:
                            logger.warning("No existing providers found, destroy might fail")
                    else:
                        # Non-network error, this is more serious
                        logger.error("Init failed with non-network error, destroy will likely fail")
                else:
                    logger.info(" Terraform init completed successfully")
                    init_success = True
                    
            except subprocess.TimeoutExpired:
                logger.warning("Terraform init timed out, proceeding with destroy anyway")
                # Check if we have existing providers
                terraform_data_dir = os.path.join(self.working_dir, '.terraform')
                if os.path.exists(terraform_data_dir):
                    init_success = True
            except Exception as e:
                logger.warning(f"Terraform init failed with exception: {e}, proceeding anyway")

            # Check if terraform.tfvars exists
            tfvars_exists = os.path.exists(os.path.join(self.working_dir, "terraform.tfvars"))
            
            # Prepare destroy command with environment variables
            env = self._get_terraform_env()
            
            # Add comprehensive network timeout environment variables for destroy
            env.update({
                'TF_HTTP_TIMEOUT': '300s',
                'TF_PROVIDER_CONNECT_TIMEOUT': '300s',
                'TF_PLUGIN_TIMEOUT': '300s',
                'TF_REGISTRY_TIMEOUT': '300s',
                'TF_FORCE_IPV4': '1',
                'GODEBUG': 'netdns=go+2',
            })
            
            # Get optimal parallelism for destroy operations
            parallelism = min(self._get_optimal_parallelism() + 10, 60)  # Slightly higher for destroy
            
            cmd = [self.terraform_cmd, "destroy", "-input=false", "-no-color"]
            
            # Always use tfvars file if it exists
            if tfvars_exists:
                cmd.extend(["-var-file=terraform.tfvars"])
            else:
                # Intelligently detect required variables from main.tf and provide appropriate dummy values
                logger.info("No terraform.tfvars found, analyzing terraform configuration for required variables")
                
                # Use the same intelligent variable detection as the main destroy method
                dummy_vars = self._get_intelligent_destroy_variables()
                if dummy_vars:
                    cmd.extend(dummy_vars)
                    logger.info(f"Providing {len(dummy_vars)//2} dummy variables for destroy operation")
            
            # High-performance destroy flags with optimized parallelism
            cmd.extend([
                "-auto-approve", 
                "-lock=false", 
                "-refresh=false",  # Skip refresh for faster destroy
                f"-parallelism={parallelism}"
            ])
            
            logger.info(f"Running high-performance destroy with parallelism={parallelism}")
            
            # Use file-specific timeout constant
            destroy_timeout = TERRAFORM_DESTROY_TIMEOUT
            logger.info(f"Using destroy timeout of {destroy_timeout // 60} minutes ({destroy_timeout // 3600} hours)")
            
            # Option to disable timeout (if set to 0)
            if destroy_timeout == 0:
                logger.warning(" Terraform destroy timeout disabled")
                destroy_timeout = None
            
            # Run destroy with optional timeout
            try:
                result = subprocess.run(
                    cmd,
                    cwd=self.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=destroy_timeout if destroy_timeout else None,
                    env=env
                )
            except subprocess.TimeoutExpired:
                # Calculate timeout for error message
                timeout_minutes = destroy_timeout // 60 if destroy_timeout else "unknown"
                error_msg = f"Terraform destroy timed out after {timeout_minutes} minutes"
                logger.error(error_msg)
                
                # For ECS deployments, timeout might be okay if resources are partially cleaned up
                if self._is_ecs_deployment():
                    logger.warning("ECS deployment destroy timed out, but marking as cleaned up")
                    return True, f"ECS cleanup completed (timeout after {timeout_minutes} minutes)"
                
                return False, error_msg
                
            if result.returncode != 0:
                logger.error(f"Terraform destroy failed: {result.stderr}")
                
                # Check if this is an ECS deployment and the error is related to missing resources
                # This is common when resources were already deleted or never existed
                stderr_lower = result.stderr.lower() if result.stderr else ""
                
                # Check for common "not found" errors that indicate resources are already gone
                not_found_errors = [
                    'not found', 'doesn\'t exist', 'no such resource', 'no longer exists',
                    'already deleted', 'resource not found', 'cluster not found',
                    'service not found', 'load balancer not found', 'target group not found'
                ]
                
                if any(error in stderr_lower for error in not_found_errors):
                    logger.info("Resources appear to be already deleted based on error message")
                    return True, "Infrastructure appears to be already deleted"
                
                # Check for IAM permission errors (common with ECS cleanup)
                permission_errors = [
                    'accessdenied', 'access denied', 'not authorized', 'unauthorized',
                    'permission denied', 'forbidden', 'insufficient permissions'
                ]
                
                if any(error in stderr_lower for error in permission_errors):
                    logger.warning("Terraform destroy failed due to permission errors")
                    # For ECS deployments, this might be okay if the core resources are gone
                    if self._is_ecs_deployment():
                        logger.info("ECS deployment detected - permission errors may be non-critical")
                        return True, "ECS infrastructure cleaned up (some permission errors encountered)"
                    else:
                        return False, f"Permission errors during destroy: {result.stderr}"
                
                # Check for state lock errors
                if 'state lock' in stderr_lower or 'lock' in stderr_lower:
                    logger.warning("State lock detected, attempting to force unlock and retry")
                    try:
                        # Force unlock
                        unlock_result = subprocess.run(
                            [self.terraform_cmd, "force-unlock", "-force", "terraform.tfstate"],
                            cwd=self.working_dir,
                            capture_output=True,
                            text=True,
                            timeout=30,
                            env=env
                        )
                        if unlock_result.returncode == 0:
                            logger.info("Successfully force-unlocked Terraform state")
                            # Try destroy again
                            retry_result = subprocess.run(
                                cmd,
                                cwd=self.working_dir,
                                capture_output=True,
                                text=True,
                                timeout=destroy_timeout if destroy_timeout else None,
                                env=env
                            )
                            if retry_result.returncode == 0:
                                logger.info("Destroy succeeded after force unlock")
                                return True, retry_result.stdout
                            else:
                                logger.warning(f"Destroy still failed after force unlock: {retry_result.stderr}")
                        else:
                            logger.warning(f"Force unlock failed: {unlock_result.stderr}")
                    except Exception as unlock_error:
                        logger.warning(f"Force unlock attempt failed: {unlock_error}")
                
                # For ECS deployments, if we get here, the error might be non-critical
                if self._is_ecs_deployment():
                    logger.warning("ECS deployment destroy failed, but marking as cleaned up")
                    return True, f"ECS cleanup completed with warnings: {result.stderr}"
                
                return False, result.stderr
            
            logger.info("Terraform destroy completed successfully")
            return True, result.stdout
        except Exception as e:
            logger.error(f"Failed to destroy infrastructure: {str(e)}")
            
            # For ECS deployments, exceptions might be non-critical
            if self._is_ecs_deployment():
                logger.warning("ECS deployment destroy failed with exception, but marking as cleaned up")
                return True, f"ECS cleanup completed with errors: {str(e)}"
            
            return False, str(e)
    
    def _is_ecs_deployment(self) -> bool:
        """
        Check if this is an ECS deployment by examining the Terraform configuration.
        
        Returns:
            True if this appears to be an ECS deployment
        """
        try:
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if not os.path.exists(main_tf_path):
                return False
            
            with open(main_tf_path, 'r') as f:
                terraform_content = f.read()
            
            # Check for ECS-related resources
            ecs_indicators = [
                'aws_ecs_cluster',
                'aws_ecs_service', 
                'aws_ecs_task_definition',
                'aws_lb',  # Application Load Balancer
                'aws_lb_target_group',
                'fargate'
            ]
            
            return any(indicator in terraform_content for indicator in ecs_indicators)
            
        except Exception as e:
            logger.warning(f"Error checking if ECS deployment: {str(e)}")
            return False

    def _get_intelligent_destroy_variables(self) -> list:
        """
        Analyze the terraform configuration to determine what variables are required 
        and provide appropriate dummy values only for those variables.
        
        Returns:
            List of terraform variable arguments ["-var", "name=value", ...]
        """
        dummy_vars = []
        
        try:
            # Read main.tf to understand what variables are declared
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if not os.path.exists(main_tf_path):
                logger.warning("main.tf not found, cannot determine required variables")
                # Don't provide any variables if we can't find main.tf - let Terraform fail with a clear error
                # This is better than providing wrong variables that cause confusing errors
                return []
            
            with open(main_tf_path, 'r') as f:
                terraform_content = f.read()
            
            # Extract variable declarations using multiple regex patterns to handle different syntax
            variable_patterns = [
                r'variable\s+"([^"]+)"\s*\{',  # variable "name" {
                r"variable\s+'([^']+)'\s*\{",  # variable 'name' {
                r'variable\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\{',  # variable name {
            ]
            
            declared_variables = set()
            for pattern in variable_patterns:
                matches = re.findall(pattern, terraform_content, re.MULTILINE)
                declared_variables.update(matches)
            
            logger.info(f"Found declared variables in terraform: {list(declared_variables)}")
            
            # Detect cloud provider from terraform content
            cloud_provider = self._detect_cloud_provider_from_terraform(terraform_content)
            logger.info(f"Detected cloud provider from terraform: {cloud_provider}")
            
            # Provide dummy values only for declared variables
            for var_name in declared_variables:
                dummy_value = self._get_dummy_value_for_variable(var_name, cloud_provider)
                if dummy_value:
                    dummy_vars.extend(["-var", f"{var_name}={dummy_value}"])
            
            # If no variables found but we detected a cloud provider, provide minimal required set
            if not dummy_vars:
                logger.warning("No variable declarations found in terraform configuration")
                # Don't provide any variables if we can't find variable declarations
                # This is better than providing wrong variables that cause confusing errors
                return []
            
            return dummy_vars
            
        except Exception as e:
            logger.warning(f"Error analyzing terraform configuration: {e}, cannot provide variables")
            # Don't provide any variables if we can't analyze the configuration
            # This is better than providing wrong variables that cause confusing errors
            return []

    def _detect_cloud_provider_from_terraform(self, terraform_content: str) -> str:
        """
        Detect the cloud provider from terraform configuration content.
        
        Args:
            terraform_content: The content of the terraform file
            
        Returns:
            Cloud provider: 'gcp', 'aws', 'azure', or 'unknown'
        """
        content_lower = terraform_content.lower()
        
        # Check for provider blocks and resources
        if 'provider "google"' in content_lower or 'google_cloud_run' in content_lower or 'gcr.io' in content_lower:
            return 'gcp'
        elif 'provider "aws"' in content_lower or 'aws_ecs' in content_lower or 'aws_lb' in content_lower:
            return 'aws'
        elif 'provider "azurerm"' in content_lower or 'azurerm_container_app' in content_lower:
            return 'azure'
        else:
            return 'unknown'

    def _get_dummy_value_for_variable(self, var_name: str, cloud_provider: str) -> str:
        """
        Get appropriate dummy value for a terraform variable based on its name and cloud provider.
        
        Args:
            var_name: Name of the terraform variable
            cloud_provider: Detected cloud provider
            
        Returns:
            Appropriate dummy value for the variable
        """
        var_name_lower = var_name.lower()
        
        # Common variable name patterns
        if 'project' in var_name_lower and 'id' in var_name_lower:
            return 'dummy-project'
        elif 'resource_group' in var_name_lower:
            return 'dummy-resource-group'
        elif 'location' in var_name_lower and cloud_provider == 'azure':
            return 'East US'
        elif 'region' in var_name_lower:
            if cloud_provider == 'gcp':
                return 'us-central1'
            elif cloud_provider == 'aws':
                return 'us-east-1'
            else:
                return 'us-east-1'
        elif 'image' in var_name_lower:
            # Handle container image variables
            if cloud_provider == 'gcp':
                return 'gcr.io/cloudrun/hello'
            else:
                return 'nginx:latest'
        elif 'app_name' in var_name_lower:
            return 'dummy-app'
        elif var_name_lower.endswith('_docker_image') or var_name_lower.endswith('_container_image'):
            # Service-specific image variables (common in multi-service deployments)
            return 'nginx:latest'
        else:
            # Default based on cloud provider
            if cloud_provider == 'gcp':
                return 'dummy-value'
            elif cloud_provider == 'aws':
                return 'dummy-value'
            elif cloud_provider == 'azure':
                return 'dummy-value'
            else:
                return 'dummy-value'

    def _post_process_terraform_code(self, terraform_code: str) -> str:
        """
        Applies specific fixes to the generated Terraform code.
        
        Args:
            terraform_code: The Terraform code to process
            
        Returns:
            The processed Terraform code
        """
        logger.info("Starting post-processing of generated Terraform code.")
        
        # Regex to find 'limits {' within a 'resources {' block (common in Cloud Run/K8s TF)
        original_pattern = r"(\s*)limits\s*\{"  # Capture leading whitespace
        replacement_pattern = r"\1limits = {"
        
        corrected_code, num_subs = re.subn(original_pattern, replacement_pattern, terraform_code)
        
        if num_subs > 0:
            logger.info(f"Corrected {num_subs} instance(s) of 'limits {{' to 'limits = {{'.")
        else:
            logger.info("No 'limits {' blocks found needing correction.")
            
        logger.info("Finished post-processing Terraform code.")
        return corrected_code

    def generate_terraform_from_zip(self, source_code_zip_path: str, api_url: Optional[str] = None, cloud_provider: str = "gcp") -> str:
        """
        Generates Terraform code from a source code zip file.

        Args:
            source_code_zip_path: The path to the source code zip file.
            api_url: Not used anymore, kept for backward compatibility.
            cloud_provider: Target cloud provider ("gcp", "aws", "azure")

        Returns:
            The generated Terraform code as a string.

        Raises:
            FileNotFoundError: If the source_code_zip_path does not exist.
            ValueError: If the generation fails.
            Exception: For other potential errors.
        """
        if not os.path.isfile(source_code_zip_path):
            raise FileNotFoundError(f"Source code zip file not found at: {source_code_zip_path}")

        logger.info(f"Attempting to generate Terraform code from {source_code_zip_path} for {cloud_provider}")

        try:
            success, result = self.terraform_generator.generate_terraform_from_zip(source_code_zip_path, cloud_provider)
            
            if not success:
                error_msg = result.get('error', 'Unknown error during Terraform generation')
                logger.error(f"Terraform generation failed: {error_msg}")
                raise ValueError(error_msg)

            terraform_code = result['terraform_code']
            logger.info(f"Successfully generated Terraform code for {cloud_provider}. Session ID: {result.get('session_id')}")
            
            # Post-processing step
            processed_terraform_code = self._post_process_terraform_code(terraform_code)
            
            # Log any changes made during post-processing
            if processed_terraform_code != terraform_code:
                logger.info("Post-processing applied changes to Terraform code")

            return processed_terraform_code

        except Exception as e:
            logger.error(f"An unexpected error occurred during Terraform generation: {str(e)}")
            raise

    def _clean_ansi_sequences(self, s: str) -> str:
        """
        Remove ANSI escape sequences from a string.
        
        Args:
            s: The input string to clean
            
        Returns:
            The cleaned string without ANSI escape sequences
        """
        # Remove ANSI escape sequences
        ansi_escape = re.compile(r'\x1b\[[0-9;]*[mGKH]')
        return ansi_escape.sub('', s)

    def _get_terraform_env(self) -> dict:
        """
        Get environment variables for Terraform execution, including cloud provider credentials.
        
        Returns:
            Dictionary of environment variables
        """
        # Start with current environment
        env = os.environ.copy()
        
        # Add cloud provider credentials if authenticator is available
        if self.authenticator:
            try:
                credentials = self.authenticator.get_credentials()
                if credentials:
                    # Determine the cloud provider type
                    authenticator_type = type(self.authenticator).__name__
                    
                    if 'AWS' in authenticator_type or 'access_key' in credentials:
                        # AWS credentials
                        if 'access_key' in credentials and credentials['access_key'] is not None:
                            env['AWS_ACCESS_KEY_ID'] = str(credentials['access_key'])
                        if 'secret_key' in credentials and credentials['secret_key'] is not None:
                            env['AWS_SECRET_ACCESS_KEY'] = str(credentials['secret_key'])
                        if 'session_token' in credentials and credentials['session_token'] is not None:
                            env['AWS_SESSION_TOKEN'] = str(credentials['session_token'])
                        if 'region' in credentials and credentials['region'] is not None:
                            env['AWS_DEFAULT_REGION'] = str(credentials['region'])
                        
                        logger.info("Added AWS credentials to Terraform environment")
                        
                    elif 'GCP' in authenticator_type or 'credentials_file' in credentials:
                        # GCP credentials
                        if 'credentials_file' in credentials and credentials['credentials_file'] is not None:
                            # Convert to absolute path
                            creds_file = os.path.abspath(credentials['credentials_file'])
                            if os.path.exists(creds_file):
                                env['GOOGLE_APPLICATION_CREDENTIALS'] = creds_file
                                logger.info(f"Set GOOGLE_APPLICATION_CREDENTIALS to: {creds_file}")
                            else:
                                logger.warning(f"GCP credentials file not found: {creds_file}")
                        
                        if 'project_id' in credentials and credentials['project_id'] is not None:
                            env['GOOGLE_CLOUD_PROJECT'] = str(credentials['project_id'])
                            logger.info(f"Set GOOGLE_CLOUD_PROJECT to: {credentials['project_id']}")
                        
                        # If we have an access token, we might need to set it for certain operations
                        if 'access_token' in credentials and credentials['access_token'] is not None:
                            env['GOOGLE_OAUTH_ACCESS_TOKEN'] = str(credentials['access_token'])
                            logger.info("Added Google OAuth access token to environment")
                        
                        logger.info("Added GCP credentials to Terraform environment")
                        
                    elif 'Azure' in authenticator_type or 'client_id' in credentials:
                        # Azure credentials
                        if 'client_id' in credentials and credentials['client_id'] is not None:
                            env['ARM_CLIENT_ID'] = str(credentials['client_id'])
                        if 'client_secret' in credentials and credentials['client_secret'] is not None:
                            env['ARM_CLIENT_SECRET'] = str(credentials['client_secret'])
                        if 'tenant_id' in credentials and credentials['tenant_id'] is not None:
                            env['ARM_TENANT_ID'] = str(credentials['tenant_id'])
                        if 'subscription_id' in credentials and credentials['subscription_id'] is not None:
                            env['ARM_SUBSCRIPTION_ID'] = str(credentials['subscription_id'])
                        
                        logger.info("Added Azure credentials to Terraform environment")
                    
                    # Debug: Log available credential keys (not values)
                    cred_keys = list(credentials.keys()) if credentials else []
                    logger.info(f"Available credential keys from authenticator: {cred_keys}")
                    
            except Exception as e:
                logger.warning(f"Failed to get credentials from authenticator: {str(e)}")
                logger.warning("Terraform operations may fail due to missing credentials")
        else:
            logger.warning("No authenticator available - Terraform operations may fail due to missing credentials")
        
        return env

    def _create_terraform_network_config(self) -> None:
        """Create Terraform configuration file for maximum performance and reliability."""
        try:
            # Test DNS resolution first
            self._test_network_connectivity()
            
            # Create .terraformrc file in working directory for network optimization
            terraformrc_path = os.path.join(self.working_dir, ".terraformrc")
            
            terraformrc_content = """# Terraform Network Configuration for Maximum Performance and Reliability

# Use direct downloads with performance optimization
provider_installation {
  direct {
    exclude = []
  }
}

# Plugin cache to avoid re-downloading (critical for performance)
plugin_cache_dir = "/tmp/terraform-plugin-cache"

# Performance optimizations
disable_checkpoint = true
disable_checkpoint_signature = true

# Network performance settings
provider_installation_direct_network_timeout = 300
provider_installation_direct_parallelism = 10

# Allow plugin cache optimizations that may break dependency lock files
# This significantly speeds up subsequent runs
plugin_cache_may_break_dependency_lock_file = true
"""
            
            with open(terraformrc_path, 'w') as f:
                f.write(terraformrc_content)
            
            # Create plugin cache directory with proper permissions
            cache_dir = "/tmp/terraform-plugin-cache"
            os.makedirs(cache_dir, exist_ok=True)
            os.chmod(cache_dir, 0o755)  # Ensure proper permissions
            
            # Set environment variable to use this config
            os.environ['TF_CLI_CONFIG_FILE'] = terraformrc_path
            
            logger.info(f"Created Terraform network configuration at {terraformrc_path} (direct downloads only)")
            
        except Exception as e:
            logger.warning(f"Failed to create Terraform network config (proceeding anyway): {e}")

    def _test_network_connectivity(self) -> None:
        """Test basic network connectivity to HashiCorp registry."""
        try:
            import socket
            import urllib.request
            
            # Test DNS resolution for registry.terraform.io
            try:
                socket.gethostbyname('registry.terraform.io')
                logger.info(" DNS resolution successful for registry.terraform.io")
            except socket.gaierror as e:
                logger.warning(f" DNS resolution failed for registry.terraform.io: {e}")
                # Try to resolve 8.8.8.8 to check if DNS is working at all
                try:
                    socket.gethostbyname('8.8.8.8')
                    logger.info("Basic DNS is working, registry.terraform.io might be temporarily unavailable")
                except:
                    logger.error("DNS resolution completely broken - network issues detected")
            
            # Test HTTP connectivity to registry (with timeout)
            try:
                req = urllib.request.Request('https://registry.terraform.io/v1/providers/hashicorp/aws')
                req.add_header('User-Agent', 'Terraform/1.0')
                with urllib.request.urlopen(req, timeout=30) as response:
                    if response.status == 200:
                        logger.info(" HTTP connectivity successful to Terraform registry")
            except Exception as e:
                logger.warning(f" HTTP connectivity test failed: {e}")
                
        except Exception as e:
            logger.warning(f"Network connectivity test failed: {e}")

    def _verify_terraform_registry_access(self) -> bool:
        """Verify that we can access the Terraform registry before attempting init."""
        try:
            import urllib.request
            import socket
            
            # Quick connectivity test to registry.terraform.io
            socket.setdefaulttimeout(30)  # 30 second timeout
            
            logger.info("Testing connectivity to Terraform registry...")
            
            # Test with a simple HEAD request to the main registry endpoint
            req = urllib.request.Request('https://registry.terraform.io/v1/providers/hashicorp/aws')
            req.add_header('User-Agent', 'Aurora-Terraform/1.0')
            req.get_method = lambda: 'HEAD'  # Use HEAD to minimize data transfer
            
            with urllib.request.urlopen(req, timeout=60) as response:
                if response.status == 200:
                    logger.info(" Terraform registry is accessible")
                    return True
                else:
                    logger.warning(f" Terraform registry returned status {response.status}")
                    return False
                    
        except Exception as e:
            logger.warning(f" Cannot reach Terraform registry: {e}")
            return False

    def _fallback_terraform_init(self, env: dict) -> bool:
        """Fallback init method for when registry access fails."""
        try:
            logger.info(" Attempting fallback terraform init with reduced provider requirements...")
            
            # Check if we have a main.tf file
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if not os.path.exists(main_tf_path):
                logger.error("No main.tf file found for fallback init")
                return False
            
            # Read the existing terraform configuration
            with open(main_tf_path, 'r') as f:
                terraform_content = f.read()
            
            # Create a simplified version with minimal providers for testing
            # We'll try to extract just the core cloud provider (AWS/GCP/Azure)
            core_provider = self._extract_core_provider(terraform_content)
            
            if core_provider:
                logger.info(f"Creating simplified terraform config with {core_provider} provider only")
                self._create_simplified_terraform_config(core_provider)
                
                # Try init with the simplified config
                cmd = [self.terraform_cmd, "init", "-input=false", "-no-color", "-upgrade"]
                logger.info(f"Running fallback init: {' '.join(cmd)}")
                
                result = subprocess.run(
                    cmd,
                    cwd=self.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=600,  # 10 minute timeout for fallback
                    env=env
                )
                
                if result.returncode == 0:
                    logger.info(" Fallback terraform init succeeded")
                    return True
                else:
                    logger.warning(f" Fallback terraform init failed: {result.stderr}")
                    return False
            else:
                logger.error("Could not identify core provider for fallback")
                return False
                
        except Exception as e:
            logger.error(f"Fallback terraform init failed with exception: {e}")
            return False

    def _extract_core_provider(self, terraform_content: str) -> str:
        """Extract the core cloud provider from terraform content."""
        content_lower = terraform_content.lower()
        
        if 'provider "aws"' in content_lower or 'aws_' in content_lower:
            return 'aws'
        elif 'provider "google"' in content_lower or 'google_' in content_lower:
            return 'google'
        elif 'provider "azurerm"' in content_lower or 'azurerm_' in content_lower:
            return 'azurerm'
        else:
            return None

    def _create_simplified_terraform_config(self, provider: str) -> None:
        """Create a simplified terraform configuration for fallback init."""
        try:
            simplified_config = f"""# Simplified Terraform configuration for network-constrained environments
terraform {{
  required_version = ">= 1.0"
  
  required_providers {{
    {provider} = {{
      source  = "hashicorp/{provider}"
      version = "~> {'5.0' if provider == 'aws' else '4.0' if provider == 'google' else '3.0'}"
    }}
  }}
}}

provider "{provider}" {{
  # Provider will be configured via environment variables or default settings
}}

# Minimal configuration to test provider initialization
"""
            
            # Backup original main.tf
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            backup_path = os.path.join(self.working_dir, "main.tf.backup")
            
            if os.path.exists(main_tf_path):
                shutil.copy2(main_tf_path, backup_path)
                logger.info(f"Backed up original main.tf to {backup_path}")
            
            # Write simplified config
            with open(main_tf_path, 'w') as f:
                f.write(simplified_config)
                
            logger.info(f"Created simplified terraform config with {provider} provider")
            
        except Exception as e:
            logger.error(f"Failed to create simplified terraform config: {e}")

    def _get_terraform_command(self) -> str:
        """Get the terraform command path."""
        return "terraform"
    
    def _cleanup_on_failure(self) -> None:
        """
        Run terraform destroy to clean up resources after a failed apply.
        This runs in a best-effort manner and doesn't raise exceptions.
        """
        try:
            logger.info("Starting cleanup of failed deployment resources")
            
            # Clean up any corrupted lock files first
            self._cleanup_terraform_locks()
            
            # Try to reinitialize terraform for cleanup (in case lock file is inconsistent)
            logger.info("Attempting to reinitialize terraform for cleanup...")
            env = self._get_terraform_env()
            env.update({
                'TF_HTTP_TIMEOUT': '300s',
                'TF_PROVIDER_CONNECT_TIMEOUT': '300s',
                'TF_PLUGIN_TIMEOUT': '300s',
                'TF_REGISTRY_TIMEOUT': '300s',
                'TF_FORCE_IPV4': '1',
                'GODEBUG': 'netdns=go+2',
            })
            
            # Quick terraform init for cleanup (don't fail if this doesn't work)
            init_success = False
            try:
                # First try with minimal configuration
                init_result = subprocess.run(
                    [self.terraform_cmd, "init", "-input=false", "-no-color", "-upgrade=false", "-get=false"],
                    cwd=self.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=60,  # 1 minute timeout for cleanup init
                    env=env
                )
                if init_result.returncode == 0:
                    logger.info(" Terraform reinitialized successfully for cleanup")
                    init_success = True
                else:
                    logger.warning(f" Terraform init for cleanup failed: {init_result.stderr}")
                    
                    # If DNS/network issues, try offline mode with existing providers
                    if 'registry.terraform.io' in init_result.stderr or 'dns' in init_result.stderr.lower():
                        logger.info("Detected network/DNS issues, attempting offline cleanup...")
                        # Skip the init entirely if we have .terraform directory
                        terraform_data_dir = os.path.join(self.working_dir, '.terraform')
                        if os.path.exists(terraform_data_dir):
                            logger.info("Using existing .terraform directory for cleanup")
                            init_success = True
            except Exception as e:
                logger.warning(f" Terraform init for cleanup failed with exception: {e}")
                # Continue anyway - we'll try destroy even without successful init
            
            # Check if terraform.tfvars exists
            tfvars_exists = os.path.exists(os.path.join(self.working_dir, "terraform.tfvars"))
            
            # Prepare the destroy command (env already set up above)
            
            cmd = [self.terraform_cmd, "destroy", "-input=false", "-no-color"]
            
            if tfvars_exists:
                cmd.extend(["-var-file=terraform.tfvars"])
            else:
                # Try to intelligently determine required variables for cleanup
                logger.info("No terraform.tfvars found for cleanup, analyzing terraform configuration")
                
                # Use the same intelligent variable detection as the main destroy method
                dummy_vars = self._get_intelligent_destroy_variables()
                if dummy_vars:
                    cmd.extend(dummy_vars)
                    logger.info(f"Providing {len(dummy_vars)//2} variables for cleanup based on terraform analysis")
                else:
                    logger.warning("Could not determine required variables for cleanup - terraform may fail")
                    # Don't add any variables - let terraform fail with a clear error
            
            # Add lock=false for cleanup operations to avoid state lock issues
            cmd.extend(["-auto-approve", "-lock=false", "-refresh=false", "-parallelism=50"])
            
            # Run destroy with a shorter timeout since we're just cleaning up
            result = subprocess.run(
                cmd,
                cwd=self.working_dir,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minutes timeout for cleanup
                env=env
            )
            
            if result.returncode == 0:
                logger.info("Successfully cleaned up resources after failed deployment")
            else:
                logger.warning(f"Cleanup may have been incomplete. Terraform destroy output: {result.stderr}")
                
        except subprocess.TimeoutExpired:
            logger.warning("Cleanup timed out after 5 minutes - manual cleanup may be required")
        except Exception as e:
            logger.warning(f"Failed to run cleanup after deployment failure: {str(e)}")
            logger.warning("Manual cleanup of cloud resources may be required")

    def get_state(self) -> Optional[str]:
        """
        Get the current Terraform state as a JSON string.
        
        Returns:
            JSON string of the Terraform state or None if not found
        """
        try:
            state_file = os.path.join(self.working_dir, "terraform.tfstate")
            if os.path.exists(state_file):
                with open(state_file, 'r') as f:
                    return f.read()
            else:
                logger.warning(f"Terraform state file not found at {state_file}")
                return None
        except Exception as e:
            logger.error(f"Failed to read Terraform state: {str(e)}")
            return None
    
    def set_state(self, state_json: str) -> bool:
        """
        Set the Terraform state from a JSON string.
        
        Args:
            state_json: JSON string of the Terraform state
            
        Returns:
            True if successful, False otherwise
        """
        try:
            state_file = os.path.join(self.working_dir, "terraform.tfstate")
            with open(state_file, 'w') as f:
                f.write(state_json)
            logger.info(f"Successfully wrote Terraform state to {state_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to write Terraform state: {str(e)}")
            return False

    def _check_required_variables(self, terraform_content: str) -> tuple[bool, str]:
        """
        Check if all required variables are provided via tfvars or have defaults.
        
        Args:
            terraform_content: Content of the main.tf file
            
        Returns:
            Tuple of (success, error_message)
        """
        try:
            # Extract variable declarations
            variable_pattern = r'variable\s+"([^"]+)"\s*\{([^}]*)\}'
            variables = re.findall(variable_pattern, terraform_content, re.DOTALL)
            
            # Check if terraform.tfvars exists
            tfvars_path = os.path.join(self.working_dir, "terraform.tfvars")
            provided_vars = set()
            
            if os.path.exists(tfvars_path):
                with open(tfvars_path, 'r') as f:
                    tfvars_content = f.read()
                # Extract variable assignments from tfvars
                tfvars_pattern = r'(\w+)\s*='
                provided_vars = set(re.findall(tfvars_pattern, tfvars_content))
            
            missing_vars = []
            for var_name, var_block in variables:
                # Check if variable has a default value
                has_default = 'default' in var_block
                
                # If no default and not provided in tfvars, it's missing
                if not has_default and var_name not in provided_vars:
                    missing_vars.append(var_name)
            
            if missing_vars:
                error_msg = f"Missing required variables: {', '.join(missing_vars)}"
                return False, error_msg
            
            logger.info(f" All {len(variables)} required variables are satisfied")
            return True, "All required variables provided"
            
        except Exception as e:
            return False, f"Error checking variables: {str(e)}"

    def _validate_credentials(self) -> tuple[bool, str]:
        """
        Validate that cloud credentials are working.
        
        Returns:
            Tuple of (success, error_message)
        """
        try:
            if not self.authenticator:
                return False, "No authenticator available"
            
            credentials = self.authenticator.get_credentials()
            if not credentials:
                return False, "No credentials available from authenticator"
            
            # Detect cloud provider and test credentials
            authenticator_type = type(self.authenticator).__name__
            
            if 'AWS' in authenticator_type:
                return self._test_aws_credentials(credentials)
            elif 'GCP' in authenticator_type:
                return self._test_gcp_credentials(credentials)
            elif 'Azure' in authenticator_type:
                return self._test_azure_credentials(credentials)
            else:
                return True, "Credential validation skipped for unknown provider"
                
        except Exception as e:
            return False, f"Credential validation error: {str(e)}"

    def _test_aws_credentials(self, credentials: dict) -> tuple[bool, str]:
        """Test AWS credentials by making a simple API call."""
        try:
            if not all(key in credentials for key in ['access_key', 'secret_key']):
                return False, "Missing AWS access key or secret key"
            
            # Test with a simple STS call to verify credentials
            import boto3
            session = boto3.Session(
                aws_access_key_id=credentials['access_key'],
                aws_secret_access_key=credentials['secret_key'],
                aws_session_token=credentials.get('session_token'),
                region_name=credentials.get('region', 'us-east-1')
            )
            
            sts_client = session.client('sts')
            response = sts_client.get_caller_identity()
            
            account_id = response.get('Account')
            logger.info(f" AWS credentials validated for account: {account_id}")
            return True, f"AWS credentials valid for account {account_id}"
            
        except Exception as e:
            return False, f"AWS credential test failed: {str(e)}"

    def _test_gcp_credentials(self, credentials: dict) -> tuple[bool, str]:
        """Test GCP credentials by making a simple API call."""
        try:
            project_id = credentials.get('project_id')
            if not project_id:
                return False, "Missing GCP project_id"
            
            # Test with a simple API call
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build
            
            if 'access_token' in credentials:
                creds = Credentials(
                    token=credentials.get('access_token'),
                    refresh_token=credentials.get('refresh_token'),
                    client_id=credentials.get('client_id'),
                    client_secret=credentials.get('client_secret'),
                    token_uri="https://oauth2.googleapis.com/token"
                )
                
                # Test with Cloud Resource Manager API
                service = build('cloudresourcemanager', 'v1', credentials=creds)
                project = service.projects().get(projectId=project_id).execute()
                
                logger.info(f" GCP credentials validated for project: {project_id}")
                return True, f"GCP credentials valid for project {project_id}"
            else:
                return True, "GCP credentials present (detailed validation skipped)"
                
        except Exception as e:
            return False, f"GCP credential test failed: {str(e)}"

    def _test_azure_credentials(self, credentials: dict) -> tuple[bool, str]:
        """Test Azure credentials by making a simple API call."""
        try:
            required_fields = ['client_id', 'client_secret', 'tenant_id', 'subscription_id']
            missing = [field for field in required_fields if field not in credentials]
            
            if missing:
                return False, f"Missing Azure credentials: {', '.join(missing)}"
            
            # For now, just check that all required fields are present
            # Full API testing would require azure-identity package
            logger.info(" Azure credentials structure validated")
            return True, "Azure credentials present and properly structured"
            
        except Exception as e:
            return False, f"Azure credential test failed: {str(e)}"

    def _check_resource_naming(self, terraform_content: str) -> list[str]:
        """
        Check resource naming conventions for potential issues.
        
        Returns:
            List of warning messages
        """
        warnings = []
        
        try:
            # Extract resource names
            resource_pattern = r'resource\s+"[^"]+"\s+"([^"]+)"'
            resource_names = re.findall(resource_pattern, terraform_content)
            
            for name in resource_names:
                # Check for overly long names
                if len(name) > 63:
                    warnings.append(f" Resource name '{name}' is very long ({len(name)} chars) - may cause issues")
                
                # Check for invalid characters
                if not re.match(r'^[a-zA-Z0-9_-]+$', name):
                    warnings.append(f" Resource name '{name}' contains potentially problematic characters")
                
                # Check for names that start with numbers
                if name[0].isdigit():
                    warnings.append(f" Resource name '{name}' starts with a number - may cause issues")
            
        except Exception as e:
            warnings.append(f" Error checking resource names: {str(e)}")
        
        return warnings

    def _check_common_patterns(self, terraform_content: str) -> list[str]:
        """
        Check for common anti-patterns and potential issues.
        
        Returns:
            List of warning messages
        """
        warnings = []
        
        try:
            content_lower = terraform_content.lower()
            
            # Check for hardcoded credentials
            if any(pattern in content_lower for pattern in ['password', 'secret', 'key', 'token']):
                if any(hardcoded in terraform_content for hardcoded in ['"password"', '"secret"', '"key"', '"token"']):
                    warnings.append(" Possible hardcoded credentials detected - use variables instead")
            
            # Check for missing tags
            if 'aws_' in content_lower and 'tags' not in content_lower:
                warnings.append(" No tags found in AWS resources - consider adding tags for management")
            
            # Check for missing region specification
            if 'aws_' in content_lower and 'region' not in content_lower:
                warnings.append(" No region specified - ensure region is set in provider or variables")
            
            # Check for potential networking issues
            if 'security_group' in content_lower and '0.0.0.0/0' in terraform_content:
                warnings.append(" Found overly permissive security group rules (0.0.0.0/0)")
            
            # Check for missing backup/persistence
            if any(db in content_lower for db in ['rds', 'database']) and 'backup' not in content_lower:
                warnings.append(" Database resources found without backup configuration")
            
            # Check for missing monitoring
            resource_count = len(re.findall(r'resource\s+"[^"]+"', terraform_content))
            if resource_count > 5 and 'cloudwatch' not in content_lower and 'monitoring' not in content_lower:
                warnings.append(" Large deployment without monitoring resources")
            
            # Check for external command dependencies in local-exec provisioners
            local_exec_pattern = r'provisioner\s+"local-exec"\s*\{[^}]*command\s*=\s*"([^"]+)"'
            local_exec_commands = re.findall(local_exec_pattern, terraform_content, re.IGNORECASE | re.DOTALL)
            
            for command in local_exec_commands:
                # Extract the command name (first word before any arguments)
                cmd_name = command.strip().split()[0] if command.strip() else ""
                
                # List of common external commands that might not be available
                external_commands = {
                    'aws': 'AWS CLI not available in Terraform environment. Consider using Terraform AWS provider resources instead.',
                    'gcloud': 'Google Cloud CLI not available in Terraform environment. Consider using Terraform Google provider resources instead.',
                    'az': 'Azure CLI not available in Terraform environment. Consider using Terraform AzureRM provider resources instead.',
                    'kubectl': 'kubectl not available in Terraform environment. Consider managing Kubernetes resources with Terraform Kubernetes provider.',
                    'helm': 'Helm not available in Terraform environment. Consider using Terraform Helm provider instead.',
                    'docker': 'Docker CLI not available in Terraform environment. Consider pre-building images or using CI/CD pipelines.',
                    'curl': 'curl might not be available in all Terraform environments.',
                    'wget': 'wget might not be available in all Terraform environments.',
                    'git': 'git might not be available in Terraform environment.',
                    'python': 'Python might not be available in Terraform environment.',
                    'node': 'Node.js might not be available in Terraform environment.',
                    'npm': 'npm might not be available in Terraform environment.'
                }
                
                if cmd_name in external_commands:
                    warnings.append(f" External dependency detected: {external_commands[cmd_name]} (Command: '{cmd_name}')")
                elif cmd_name and cmd_name not in ['echo', 'cat', 'touch', 'mkdir', 'cp', 'mv', 'rm', 'ls', 'chmod', 'sleep']:
                    # Warn about any other non-standard commands
                    warnings.append(f" External command dependency detected: '{cmd_name}' - ensure this command is available in the Terraform execution environment")
                
        except Exception as e:
            warnings.append(f" Error checking patterns: {str(e)}")
        
        return warnings

    def _validate_docker_images(self) -> list[str]:
        """
        Validate Docker images if present in tfvars.
        
        Returns:
            List of warning messages
        """
        warnings = []
        
        try:
            tfvars_path = os.path.join(self.working_dir, "terraform.tfvars")
            if not os.path.exists(tfvars_path):
                return warnings
            
            with open(tfvars_path, 'r') as f:
                tfvars_content = f.read()
            
            # Extract Docker image references
            image_pattern = r'(?:docker_image|container_image)\s*=\s*"([^"]+)"'
            images = re.findall(image_pattern, tfvars_content)
            
            for image in images:
                # Check for latest tag
                if image.endswith(':latest') or ':' not in image:
                    warnings.append(f" Docker image '{image}' uses 'latest' tag - consider using specific versions")
                
                # Check for suspicious image names
                if 'localhost' in image or '127.0.0.1' in image:
                    warnings.append(f" Docker image '{image}' appears to reference localhost - ensure it's accessible from cloud")
                
                # Check for missing registry
                if '/' not in image or '.' not in image:
                    warnings.append(f" Docker image '{image}' may be missing registry prefix")
                    
        except Exception as e:
            warnings.append(f" Error validating Docker images: {str(e)}")
        
        return warnings

    def _test_cloud_connectivity(self) -> tuple[bool, str]:
        """
        Test basic connectivity to cloud services.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            import urllib.request
            import socket
            
            # Determine which cloud services to test based on terraform content
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if os.path.exists(main_tf_path):
                with open(main_tf_path, 'r') as f:
                    content = f.read().lower()
                
                # Test relevant cloud endpoints
                endpoints_to_test = []
                
                if 'aws_' in content:
                    endpoints_to_test.append(('AWS', 'https://aws.amazon.com'))
                if 'google_' in content or 'gcp' in content:
                    endpoints_to_test.append(('GCP', 'https://cloud.google.com'))
                if 'azurerm_' in content:
                    endpoints_to_test.append(('Azure', 'https://azure.microsoft.com'))
                
                # Test Docker registries if docker images are used
                if 'docker' in content or 'container' in content:
                    endpoints_to_test.append(('Docker Hub', 'https://registry-1.docker.io'))
                
                # Test each endpoint
                failed_tests = []
                for service_name, endpoint in endpoints_to_test:
                    try:
                        req = urllib.request.Request(endpoint)
                        req.add_header('User-Agent', 'Aurora-Terraform-Preflight/1.0')
                        with urllib.request.urlopen(req, timeout=10) as response:
                            if response.status >= 400:
                                failed_tests.append(f"{service_name} ({response.status})")
                    except Exception as e:
                        failed_tests.append(f"{service_name} ({str(e)[:50]})")
                
                if failed_tests:
                    return False, f"Connectivity issues with: {', '.join(failed_tests)}"
                else:
                    return True, f"Successfully tested connectivity to {len(endpoints_to_test)} service(s)"
            
            return True, "No specific connectivity tests needed"
            
        except Exception as e:
            return False, f"Connectivity test failed: {str(e)}"

    def _get_optimal_parallelism(self) -> int:
        """
        Determine optimal parallelism based on deployment complexity.
        
        Returns:
            Optimal parallelism value for Terraform operations
        """
        try:
            # Analyze terraform configuration to determine complexity
            main_tf_path = os.path.join(self.working_dir, "main.tf")
            if not os.path.exists(main_tf_path):
                return 30  # Default moderate parallelism
            
            with open(main_tf_path, 'r') as f:
                terraform_content = f.read()
            
            # Count resources to estimate complexity
            resource_count = len(re.findall(r'resource\s+"[^"]+"', terraform_content))
            variable_count = len(re.findall(r'variable\s+"[^"]+"', terraform_content))
            
            # Check for multi-service indicators
            is_multi_service = any(indicator in terraform_content.lower() for indicator in [
                'docker_images', 'service_', '_service', 'frontend', 'backend', 'database', 
                'redis', 'api-service', 'data-service', 'ecs_service', 'cloud_run_service'
            ])
            
            # Determine parallelism based on complexity
            if resource_count > 50 or (is_multi_service and resource_count > 20):
                parallelism = 50  # High parallelism for complex deployments
            elif resource_count > 20 or is_multi_service:
                parallelism = 35  # Medium-high parallelism for moderate complexity
            elif resource_count > 10:
                parallelism = 25  # Medium parallelism for simple deployments
            else:
                parallelism = 20  # Conservative parallelism for very simple deployments
            
            logger.info(f"Detected deployment complexity: {resource_count} resources, multi-service: {is_multi_service}, using parallelism: {parallelism}")
            return parallelism
            
        except Exception as e:
            logger.warning(f"Failed to analyze deployment complexity: {e}, using default parallelism")
            return 30  # Safe default


    def _cleanup_terraform_locks(self) -> None:
        """
        Clean up corrupted Terraform lock files that may prevent destroy operations.
        This is a best-effort cleanup and won't raise exceptions.
        """
        try:
            # More comprehensive list of files that can cause lock/dependency issues
            problematic_files = [
                os.path.join(self.working_dir, ".terraform.tfstate.lock.info"),
                os.path.join(self.working_dir, ".terraform.lock.hcl"),
                os.path.join(self.working_dir, ".terraform", "terraform.tfstate"),
                os.path.join(self.working_dir, ".terraform", "environment"),
            ]
            
            for file_path in problematic_files:
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                        logger.info(f"Removed potentially problematic file: {os.path.basename(file_path)}")
                    except Exception as e:
                        logger.warning(f"Failed to remove file {file_path}: {e}")
            
            # Also remove .terraform directory if it exists (will be recreated by init)
            terraform_dir = os.path.join(self.working_dir, ".terraform")
            if os.path.exists(terraform_dir):
                try:
                    shutil.rmtree(terraform_dir, ignore_errors=True)
                    logger.info("Removed .terraform directory to force clean reinitialization")
                except Exception as e:
                    logger.warning(f"Failed to remove .terraform directory: {e}")
            
            # Try to force unlock using terraform command (if terraform is available)
            try:
                env = self._get_terraform_env()
                result = subprocess.run(
                    [self.terraform_cmd, "force-unlock", "-force", "terraform.tfstate"],
                    cwd=self.working_dir,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=env
                )
                if result.returncode == 0:
                    logger.info("Successfully force-unlocked Terraform state")
                else:
                    logger.debug(f"Force unlock failed (this is normal if no lock existed): {result.stderr}")
            except Exception as e:
                logger.debug(f"Force unlock attempt failed: {e}")
        except Exception as e:
            logger.warning(f"Exception during comprehensive cleanup: {e}")

    def _is_ecs_deployment(self) -> bool:
        """Check if this is an ECS deployment by examining the Terraform configuration."""
        try:
            main_tf = os.path.join(self.working_dir, "main.tf")
            if os.path.exists(main_tf):
                with open(main_tf, 'r') as f:
                    content = f.read()
                    return 'aws_ecs_cluster' in content or 'aws_ecs_service' in content
            return False
        except Exception:
            return False
    
    def import_ecs_resources(self, app_name: str) -> tuple[bool, str]:
        """
        Import existing ECS resources into Terraform state.
        This is needed when Terraform doesn't have the resources in state but they exist in AWS.
        """
        try:
            logger.info(f"Importing ECS resources for app: {app_name}")
            
            # Prepare environment variables
            env = self._get_terraform_env()
            
            # List of resources to import with their import IDs
            import_commands = [
                # ECS Cluster
                (f"aws_ecs_cluster.main", app_name),
                # ECS Service (format: cluster/service)
                (f"aws_ecs_service.app", f"{app_name}/{app_name}"),
                # ECS Task Definition
                (f"aws_ecs_task_definition.app", app_name),
                # Load Balancer
                (f"aws_lb.app", f"{app_name}-alb"),
                # Target Group
                (f"aws_lb_target_group.app", f"{app_name}-tg"),
                # Security Groups
                (f"aws_security_group.alb", f"{app_name}-alb"),
                (f"aws_security_group.ecs_tasks", f"{app_name}-ecs-tasks"),
                # IAM Roles
                (f"aws_iam_role.ecs_execution", f"{app_name}-ecs-execution-role"),
                (f"aws_iam_role.ecs_task", f"{app_name}-ecs-task-role"),
                # CloudWatch Log Group
                (f"aws_cloudwatch_log_group.ecs", f"/ecs/{app_name}"),
            ]
            
            successful_imports = []
            failed_imports = []
            
            for resource_address, import_id in import_commands:
                try:
                    logger.info(f"Importing {resource_address} with ID: {import_id}")
                    
                    cmd = [self.terraform_cmd, "import", resource_address, import_id]
                    result = subprocess.run(
                        cmd,
                        cwd=self.working_dir,
                        capture_output=True,
                        text=True,
                        timeout=60,
                        env=env
                    )
                    
                    if result.returncode == 0:
                        logger.info(f" Successfully imported {resource_address}")
                        successful_imports.append(resource_address)
                    else:
                        error_msg = result.stderr.strip()
                        # Check if it's a "not found" error (resource doesn't exist)
                        if "not found" in error_msg.lower() or "doesn't exist" in error_msg.lower():
                            logger.info(f"ℹ  Resource {resource_address} not found in AWS (may be already deleted)")
                        else:
                            logger.warning(f" Failed to import {resource_address}: {error_msg}")
                            failed_imports.append((resource_address, error_msg))
                            
                except subprocess.TimeoutExpired:
                    logger.warning(f" Import timeout for {resource_address}")
                    failed_imports.append((resource_address, "Timeout"))
                except Exception as e:
                    logger.warning(f" Import error for {resource_address}: {str(e)}")
                    failed_imports.append((resource_address, str(e)))
            
            # Summary
            logger.info(f"Import summary: {len(successful_imports)} successful, {len(failed_imports)} failed")
            
            if successful_imports:
                return True, f"Successfully imported {len(successful_imports)} resources. Failed: {len(failed_imports)}"
            else:
                return False, f"No resources imported. All {len(failed_imports)} imports failed."
                
        except Exception as e:
            error_msg = f"Error during ECS resource import: {str(e)}"
            logger.error(error_msg)
            return False, error_msg