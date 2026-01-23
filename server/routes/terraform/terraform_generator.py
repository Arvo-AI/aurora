import os
import zipfile
import tempfile
import shutil
import re
import uuid
from typing import Dict, Any, Optional, Tuple, List
import logging

logger = logging.getLogger(__name__)

class TerraformGenerator:
    def __init__(self, working_dir: str):
        """
        Initialize the TerraformGenerator with a working directory.
        
        Args:
            working_dir: Directory to store temporary files and generated Terraform code
        """
        self.working_dir = working_dir
        self.upload_folder = os.path.join(working_dir, 'uploads')
        self.extract_folder = os.path.join(working_dir, 'extracted')
        self.terraform_folder = os.path.join(working_dir, 'terraform')
        
        # Create required directories
        os.makedirs(self.upload_folder, exist_ok=True)
        os.makedirs(self.extract_folder, exist_ok=True)
        os.makedirs(self.terraform_folder, exist_ok=True)

    def generate_terraform_from_zip(self, zip_file_path: str, cloud_provider: str = "gcp") -> Tuple[bool, Dict[str, Any]]:
        """
        Generate comprehensive Terraform code from a zip file based on the specified cloud provider.
        ALWAYS uses the exhaustive multi-container aware Terraform generator - NO FALLBACKS.
        
        Args:
            zip_file_path: Path to the zip file containing source code
            cloud_provider: Target cloud provider ("gcp", "aws", "azure")
            
        Returns:
            Tuple of (success_boolean, result_dict)
        """
        logger.info("Generating Terraform from zip file")
        try:
            # Create a unique session ID for this generation
            session_id = str(uuid.uuid4())[:8]
            
            # Extract the zip file
            extract_path = os.path.join(self.extract_folder, session_id)
            os.makedirs(extract_path, exist_ok=True)
            
            with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
                zip_ref.extractall(extract_path)
            
            logger.info(f"Extracted zip to {extract_path}, provider: {cloud_provider}")
            
            # Try to find a Dockerfile (optional - comprehensive logic can handle without it)
            dockerfile_path = self._find_dockerfile(extract_path)
            if dockerfile_path:
                logger.info(f" Found Dockerfile at: {dockerfile_path}")
            else:
                logger.info(" No Dockerfile found - proceeding with comprehensive logic")
                # Use extract_path as the base path for comprehensive detection
                dockerfile_path = extract_path
            
            # Generate comprehensive Terraform code based on cloud provider
            if cloud_provider.lower() == 'aws':
                terraform_code = self._generate_aws_terraform(dockerfile_path, session_id)
            elif cloud_provider.lower() == 'azure':
                terraform_code = self._generate_azure_terraform(dockerfile_path, session_id)
            elif cloud_provider.lower() == 'ovh':
                terraform_code = self._generate_ovh_terraform(dockerfile_path, session_id)
            else:  # Default to GCP
                terraform_code = self._generate_gcp_terraform(dockerfile_path, session_id)
            
            if not terraform_code:
                logger.error(" Comprehensive Terraform generation returned empty result")
                return False, {'error': 'Comprehensive Terraform generation failed to produce output'}
            
            logger.info(f" Successfully generated comprehensive Terraform code ({len(terraform_code)} characters)")
            
            # Save Terraform code to files
            tf_dir = os.path.join(self.terraform_folder, session_id)
            os.makedirs(tf_dir, exist_ok=True)
            
            tf_file_path = os.path.join(tf_dir, 'main.tf')
            with open(tf_file_path, 'w') as f:
                f.write(terraform_code)
            
            # Create terraform.tfvars.example based on cloud provider
            tfvars_file_path = os.path.join(tf_dir, 'terraform.tfvars.example')
            with open(tfvars_file_path, 'w') as f:
                if cloud_provider.lower() == 'aws':
                    f.write("""# Copy this file to terraform.tfvars and fill in your values
region = "us-east-1"
# Single flat ECR repository – put your unique tag here
container_image = "123456789012.dkr.ecr.us-east-1.amazonaws.com/aurora-images:your-app-latest"
# app_name = "my-app"  # Optional - default from zip file name
""")
                elif cloud_provider.lower() == 'azure':
                    f.write("""# Copy this file to terraform.tfvars and fill in your values
resource_group_name = "your-resource-group"
location = "East US"
container_image = "yourregistry.azurecr.io/your-app:latest"
# app_name = "my-app"  # Optional - default from zip file name
""")
                elif cloud_provider.lower() == 'ovh':
                    f.write("""# Copy this file to terraform.tfvars and fill in your values
service_name = "your-ovh-public-cloud-project-id"
region = "GRA7"  # OVH region: GRA7, SBG5, BHS5, etc.
# cluster_name = "my-k8s-cluster"  # Optional
# node_pool_flavor = "b2-7"  # Optional - default is b2-7
""")
                else:  # GCP
                    f.write("""# Copy this file to terraform.tfvars and fill in your values
project_id = "your-gcp-project-id"
container_image = "gcr.io/your-project/your-image:latest"
# region = "us-central1"  # Optional - default is us-central1
""")
            
            return True, {
                'terraform_code': terraform_code,
                'session_id': session_id,
                'cloud_provider': cloud_provider,
                'files': {
                    'main_tf': tf_file_path,
                    'tfvars_example': tfvars_file_path
                }
            }
            
        except Exception as e:
            logger.error(f" Error in comprehensive Terraform generation: {str(e)}")
            return False, {'error': str(e)}

    def _find_dockerfile(self, extract_path: str) -> Optional[str]:
        """Find Dockerfile in the extracted source code."""
        for root, dirs, files in os.walk(extract_path):
            for file in files:
                if file.lower() in ['dockerfile', 'dockerfile.txt']:
                    return os.path.join(root, file)
        return None

    def _find_project_root(self, dockerfile_path: str) -> str:
        """
        Find the project root directory for multi-container detection.
        
        Strategy:
        1. Check if docker-compose.yml exists in the same directory as the Dockerfile
        2. Check parent directories up to 3 levels for docker-compose.yml
        3. Look for a directory that contains multiple service subdirectories
        4. Fall back to the directory containing the Dockerfile
        """
        dockerfile_dir = os.path.dirname(dockerfile_path)
        current_dir = dockerfile_dir
        
        # Strategy 1: Look for docker-compose.yml in current and parent directories
        for _ in range(4):  # Check current + 3 parent levels
            if current_dir and os.path.exists(current_dir):
                for compose_name in ['docker-compose.yml', 'docker-compose.yaml']:
                    compose_path = os.path.join(current_dir, compose_name)
                    if os.path.exists(compose_path):
                        logger.info(f"Found docker-compose.yml at: {compose_path}")
                        return current_dir
                
                # Move to parent directory
                parent_dir = os.path.dirname(current_dir)
                if parent_dir == current_dir:  # Reached filesystem root
                    break
                current_dir = parent_dir
            else:
                break
        
        # Strategy 2: Look for multi-service structure (multiple subdirectories with Dockerfiles)
        potential_roots = set()
        dockerfile_dir = os.path.dirname(dockerfile_path)
        
        # Check if the parent directory contains multiple service-like subdirectories
        for _ in range(3):  # Check up to 3 parent levels
            parent_dir = os.path.dirname(dockerfile_dir)
            if parent_dir == dockerfile_dir:  # Reached root
                break
                
            if os.path.exists(parent_dir):
                service_dirs = []
                try:
                    for item in os.listdir(parent_dir):
                        item_path = os.path.join(parent_dir, item)
                        if os.path.isdir(item_path):
                            # Check if this looks like a service directory
                            dockerfile_in_service = any(
                                os.path.exists(os.path.join(item_path, df))
                                for df in ['Dockerfile', 'dockerfile']
                            )
                            if dockerfile_in_service or item.lower() in [
                                'frontend', 'backend', 'api', 'web', 'server', 'client',
                                'app', 'service', 'api-service', 'data-service', 'worker'
                            ]:
                                service_dirs.append(item)
                    
                    # If we found multiple service-like directories, this is likely the project root
                    if len(service_dirs) >= 2:
                        logger.info(f"Found multi-service structure in {parent_dir}: {service_dirs}")
                        return parent_dir
                        
                except OSError:
                    pass
                    
            dockerfile_dir = parent_dir
        
        # Strategy 3: Fall back to the original Dockerfile directory
        logger.info(f"No project root detected, using Dockerfile directory: {os.path.dirname(dockerfile_path)}")
        return os.path.dirname(dockerfile_path)

    def _parse_dockerfile(self, dockerfile_path: str) -> Dict[str, Any]:
        """Parse Dockerfile to extract configuration information."""
        exposed_ports = []
        
        try:
            with open(dockerfile_path, 'r') as f:
                content = f.read()
        
            # Extract EXPOSE statements
            expose_pattern = r'EXPOSE\s+(\d+)'
            matches = re.findall(expose_pattern, content, re.IGNORECASE)
            exposed_ports = [int(port) for port in matches]
            
        except Exception as e:
            # If parsing fails, use defaults
            pass
        
        return {
            'exposed_ports': exposed_ports
        }

    def _generate_gcp_terraform(self, dockerfile_path: str, app_id: str) -> str:
        """
        Generate Terraform configuration for GCP Cloud Run based on project structure.
        
        Args:
            dockerfile_path: Either a path to a Dockerfile OR the extraction directory
            app_id: Application ID for naming
        """
        # Determine dockerfile info intelligently
        if os.path.isfile(dockerfile_path):
            # dockerfile_path is an actual Dockerfile
            dockerfile_info = self._parse_dockerfile(dockerfile_path)
            logger.info(f" Using Dockerfile at: {dockerfile_path}")
        else:
            # dockerfile_path is the extraction directory - no Dockerfile found
            dockerfile_info = {'exposed_ports': []}  # Default empty dockerfile info
            logger.info(f" No Dockerfile found, using default configuration")
        
        # Determine container port (default to 8080 if not specified)
        container_port = 8080
        if dockerfile_info['exposed_ports']:
            container_port = dockerfile_info['exposed_ports'][0]
        
        # Create a sanitized name for the Cloud Run service
        app_name = f"cloudrun-{app_id[:8]}"
        logger.info(f"  Application name: {app_name}")
        logger.info(f" Container port: {container_port}")
        
        # Generate the Terraform code
        tf_code = f"""# Aurora Comprehensive GCP Cloud Run Terraform Configuration
terraform {{
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 4.0"
    }}
  }}
}}

provider "google" {{
  project = var.project_id
  region  = var.region
}}

# Variables
variable "project_id" {{
  description = "The GCP project ID"
  type        = string
}}

variable "region" {{
  description = "The GCP region to deploy to"
  type        = string
  default     = "us-central1"
}}

variable "container_image" {{
  description = "Container image to deploy (after you push to GCR/Artifact Registry)"
  type        = string
  default     = "gcr.io/cloudrun/hello"  # Default for destroy operations
}}

# Cloud Run Service
resource "google_cloud_run_service" "app" {{
  name     = "{app_name}"
  location = var.region

  template {{
    spec {{
      containers {{
        image = var.container_image
        
        # Set container port
        ports {{
          container_port = {container_port}
        }}
        
        # Resource limits
        resources {{
          limits = {{
            memory = "512Mi"
            cpu    = "1"
          }}
        }}
      }}
    }}
  }}

  traffic {{
    percent         = 100
    latest_revision = true
  }}
}}

# Allow completely public access
resource "google_cloud_run_service_iam_member" "public_access" {{
  service  = google_cloud_run_service.app.name
  location = google_cloud_run_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}}

# Output the service URL
output "service_url" {{
  value = google_cloud_run_service.app.status[0].url
}}
"""
        return tf_code

    def _generate_ovh_terraform(self, dockerfile_path: str, app_id: str) -> str:
        """
        Generate Terraform configuration for OVH Managed Kubernetes.
        
        Uses the OVH provider with OAuth2 access token authentication.
        Creates a managed Kubernetes cluster with a node pool.
        
        Args:
            dockerfile_path: Either a path to a Dockerfile OR the extraction directory
            app_id: Application ID for naming
        """
        # Create a sanitized name for the cluster
        cluster_name = f"k8s-{app_id[:8]}"
        logger.info(f"  OVH Kubernetes cluster name: {cluster_name}")
        
        tf_code = f"""# Aurora OVH Managed Kubernetes Terraform Configuration
# Uses OAuth2 access token authentication (OVH_ACCESS_TOKEN env var)

terraform {{
  required_providers {{
    ovh = {{
      source  = "ovh/ovh"
      version = ">= 0.36.0"
    }}
  }}
}}

# OVH Provider - uses OVH_ENDPOINT and OVH_ACCESS_TOKEN from environment
# Credentials are injected via environment variables (no hardcoded values)
provider "ovh" {{
}}

# Variables
variable "service_name" {{
  description = "OVH Public Cloud project ID (service_name)"
  type        = string
}}

variable "region" {{
  description = "OVH region for the Kubernetes cluster"
  type        = string
  default     = "GRA7"
}}

variable "cluster_name" {{
  description = "Name of the Kubernetes cluster"
  type        = string
  default     = "{cluster_name}"
}}

variable "node_pool_flavor" {{
  description = "Flavor (instance type) for node pool"
  type        = string
  default     = "b2-7"  # 2 vCPU, 7GB RAM
}}

variable "node_pool_size" {{
  description = "Number of nodes in the pool"
  type        = number
  default     = 3
}}

# OVH Managed Kubernetes Cluster
resource "ovh_cloud_project_kube" "cluster" {{
  service_name = var.service_name
  name         = var.cluster_name
  region       = var.region
  
  # Use latest stable Kubernetes version
  version = "1.28"
  
  # Private network configuration (optional, commented out)
  # private_network_id = ovh_cloud_project_network_private.network.id
}}

# Node Pool for the Kubernetes cluster
resource "ovh_cloud_project_kube_nodepool" "pool" {{
  service_name  = var.service_name
  kube_id       = ovh_cloud_project_kube.cluster.id
  name          = "default-pool"
  flavor_name   = var.node_pool_flavor
  desired_nodes = var.node_pool_size
  min_nodes     = 1
  max_nodes     = 10
  
  # Autoscaling enabled
  autoscale = true
}}

# Outputs
output "cluster_id" {{
  description = "ID of the Kubernetes cluster"
  value       = ovh_cloud_project_kube.cluster.id
}}

output "cluster_name" {{
  description = "Name of the Kubernetes cluster"
  value       = ovh_cloud_project_kube.cluster.name
}}

output "cluster_version" {{
  description = "Kubernetes version"
  value       = ovh_cloud_project_kube.cluster.version
}}

output "kubeconfig" {{
  description = "Kubeconfig file content (sensitive)"
  value       = ovh_cloud_project_kube.cluster.kubeconfig
  sensitive   = true
}}

output "cluster_api_url" {{
  description = "Kubernetes API server URL"
  value       = ovh_cloud_project_kube.cluster.control_plane_is_up_to_date ? ovh_cloud_project_kube.cluster.url : "Cluster not ready yet"
}}
"""
        return tf_code

    def _generate_aws_terraform(self, dockerfile_path: str, app_id: str) -> str:
        """
        Intelligently generate AWS Terraform configuration.
        
        NEW LOGIC:
        1. Detect K8s manifests → Use EKS (matches GCP approach)
        2. Detect multi-service docker-compose → Use EKS for better orchestration
        3. Single service → Use ECS Fargate
        
        Args:
            dockerfile_path: Either a path to a Dockerfile OR the extraction directory
            app_id: Application ID for naming
        """
        try:
            # Determine project root and dockerfile info intelligently
            if os.path.isfile(dockerfile_path):
                # dockerfile_path is an actual Dockerfile
                dockerfile_dir = os.path.dirname(dockerfile_path)
                dockerfile_info = self._parse_dockerfile(dockerfile_path)
                project_root = self._find_project_root(dockerfile_path)
                logger.info(f" Using Dockerfile at: {dockerfile_path}")
                logger.info(f" Project root: {project_root}")
            else:
                # dockerfile_path is the extraction directory - no Dockerfile found
                project_root = dockerfile_path
                dockerfile_dir = dockerfile_path
                dockerfile_info = {'exposed_ports': []}  # Default empty dockerfile info
                logger.info(f" No Dockerfile found, using project root: {project_root}")
            
            app_name = f"aws-{app_id[:8]}"
            logger.info(f"  Application name: {app_name}")
            
            # Phase 1: Check for Kubernetes manifests (highest priority - matches GCP)
            k8s_manifests = self._find_kubernetes_manifests(project_root)
            
            if k8s_manifests:
                logger.info(f" Kubernetes manifests detected! Using EKS for AWS deployment")
                logger.info(f" Found {len(k8s_manifests)} K8s manifest files")
                
                # Parse K8s manifests to extract service metadata
                services_metadata = self._parse_kubernetes_to_services_metadata(k8s_manifests)
                
                if not services_metadata:
                    logger.warning("  K8s manifests found but failed to parse services - generating default K8s service")
                    services_metadata = {
                        'app': {
                            'image_variable': 'app_docker_image',
                            'port': 80,
                            'environment': {},
                            'depends_on': [],
                            'cpu': 256,
                            'memory': 512,
                            'is_load_balanced': True,
                            'health_check_path': '/',
                            'service_type': 'generic',
                            'needs_persistence': False
                        }
                    }
                
                logger.info(f" Generating EKS deployment for {len(services_metadata)} services: {list(services_metadata.keys())}")
                return self.generate_aws_eks_terraform(services_metadata, app_name)
            
            # Phase 2: Check for docker-compose (multi-service detection)
            docker_compose_path = self._find_docker_compose_file(project_root)
            
            if docker_compose_path:
                logger.info(f" Docker-compose detected: {docker_compose_path}")
                services_metadata = self._parse_docker_compose_to_services_metadata(docker_compose_path)
                
                if services_metadata and len(services_metadata) > 1:
                    logger.info(f" Multi-service docker-compose detected! Using EKS for better orchestration")
                    logger.info(f" Services: {list(services_metadata.keys())}")
                    logger.info("   Rationale: EKS provides better multi-service networking, scaling, and service discovery")
                    return self.generate_aws_eks_terraform(services_metadata, app_name)
                elif services_metadata and len(services_metadata) == 1:
                    logger.info(f" Single-service docker-compose detected! Using ECS Fargate for simplicity")
                    logger.info(f" Service: {list(services_metadata.keys())[0]}")
                    return self.generate_aws_fargate_terraform(services_metadata, app_name)
                else:
                    logger.warning("  Docker-compose found but no services parsed - falling back to single service")
            
            # Phase 3: Single Dockerfile or no containerization - use ECS Fargate
            logger.info(f" Single service deployment detected! Using ECS Fargate")
            
            # Create single service metadata from dockerfile info
            services_metadata = self._parse_dockerfile_to_services_metadata(dockerfile_info, app_name)
            
            if not services_metadata:
                logger.warning("  No services detected from any source - generating default single service")
                # Generate a default service
                services_metadata = {
                    'app': {
                        'image_variable': 'container_image',
                        'port': 80,
                        'environment': {},
                        'depends_on': [],
                        'cpu': 256,
                        'memory': 512,
                        'is_load_balanced': True,
                        'health_check_path': '/',
                        'service_type': 'generic',
                        'needs_persistence': False
                    }
                }
            
            logger.info(f" Generating ECS Fargate deployment for single service: {list(services_metadata.keys())[0]}")
            return self.generate_aws_fargate_terraform(services_metadata, app_name)
                
        except Exception as e:
            logger.error(f" Error generating AWS Terraform: {e}")
            # Return a basic terraform configuration instead of failing completely
            return 0
            

    def _parse_deployment_to_services_metadata(self, source_dir: str, dockerfile_info: Dict, app_name: str) -> Dict[str, Dict[str, Any]]:
        """
        Parse deployment structure into normalized services metadata.
        Supports docker-compose.yml, Kubernetes manifests, or single Dockerfile.
        """
        # Try to find docker-compose file first
        docker_compose_path = self._find_docker_compose_file(source_dir)
        
        if docker_compose_path:
            return self._parse_docker_compose_to_services_metadata(docker_compose_path)
        
        # Try to find Kubernetes manifests
        k8s_manifests = self._find_kubernetes_manifests(source_dir)
        if k8s_manifests:
            return self._parse_kubernetes_to_services_metadata(k8s_manifests)
        
        # Fallback to single Dockerfile service
        return self._parse_dockerfile_to_services_metadata(dockerfile_info, app_name)

    def _find_docker_compose_file(self, source_dir: str) -> Optional[str]:
        """Find docker-compose file in the source directory."""
        try:
            for file in os.listdir(source_dir):
                if file.lower().startswith('docker-compose') and file.endswith(('.yml', '.yaml')):
                    compose_path = os.path.join(source_dir, file)
                    logger.info(f"Found docker-compose file: {compose_path}")
                    return compose_path
        except Exception as e:
            logger.warning(f"Error searching for docker-compose file: {e}")
        return None

    def _find_kubernetes_manifests(self, source_dir: str) -> List[str]:
        """Find Kubernetes manifest files in the source directory with comprehensive detection."""
        manifests = []
        try:
            logger.info(f" Scanning for Kubernetes manifests in: {source_dir}")
            
            # Get ALL YAML files first (like GCP strategy)
            all_yaml_files = []
            for root, dirs, files in os.walk(source_dir):
                # Skip hidden directories and __MACOSX
                dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__MACOSX']
                
                for file in files:
                    if file.endswith(('.yaml', '.yml')) and not file.startswith('.'):
                        all_yaml_files.append(os.path.join(root, file))
            
            logger.info(f" Found {len(all_yaml_files)} YAML files total")
            
            # Validate which ones are actually Kubernetes manifests
            for yaml_file in all_yaml_files:
                try:
                    import yaml
                    with open(yaml_file, 'r', encoding='utf-8') as f:
                        docs = list(yaml.safe_load_all(f))
                    
                    # Check if any document in the file is a K8s resource
                    for doc in docs:
                        if (doc and isinstance(doc, dict) and 
                            'apiVersion' in doc and 'kind' in doc):
                            
                            # Common Kubernetes resource types
                            k8s_kinds = {
                                'Deployment', 'Service', 'Pod', 'ReplicaSet', 'StatefulSet',
                                'DaemonSet', 'Job', 'CronJob', 'ConfigMap', 'Secret',
                                'Ingress', 'ServiceAccount', 'Role', 'RoleBinding',
                                'ClusterRole', 'ClusterRoleBinding', 'PersistentVolume',
                                'PersistentVolumeClaim', 'Namespace', 'HorizontalPodAutoscaler'
                            }
                            
                            if doc['kind'] in k8s_kinds:
                                manifests.append(yaml_file)
                                logger.info(f" Detected K8s {doc['kind']}: {os.path.basename(yaml_file)}")
                                break  # Found at least one K8s resource in this file
                                
                except Exception as e:
                    logger.debug(f"  Could not parse YAML file {yaml_file}: {e}")
                    # If filename suggests it's K8s related, include it anyway
                    if any(keyword in os.path.basename(yaml_file).lower() 
                           for keyword in ['deployment', 'service', 'k8s', 'kubernetes', 'manifest']):
                        manifests.append(yaml_file)
                        logger.info(f" Including K8s-named file: {os.path.basename(yaml_file)}")
            
            if manifests:
                logger.info(f" Successfully detected {len(manifests)} Kubernetes manifest files")
                for manifest in manifests:
                    logger.info(f"    {os.path.relpath(manifest, source_dir)}")
            else:
                logger.info(" No Kubernetes manifests detected")
                
        except Exception as e:
            logger.warning(f"Error searching for Kubernetes manifests: {e}")
            
        return manifests

    def _parse_docker_compose_to_services_metadata(self, compose_path: str) -> Dict[str, Dict[str, Any]]:
        """Parse docker-compose.yml into normalized services metadata with automatic issue detection and fixes."""
        try:
            import yaml
            
            with open(compose_path, 'r') as f:
                compose_data = yaml.safe_load(f)
            
            if not compose_data or 'services' not in compose_data:
                logger.warning("No services found in docker-compose file")
                return {}
            
            services_metadata = {}
            compose_services = compose_data['services']
            
            logger.info(f"Found docker-compose with {len(compose_services)} services: {list(compose_services.keys())}")
            
            # Auto-detect application name for service discovery
            app_name_for_discovery = self._generate_service_discovery_name(compose_path)
            
            # First pass: collect all service information
            for service_name, service_config in compose_services.items():
                # Parse ports with intelligent defaults
                container_port = self._parse_service_port(service_name, service_config)
                
                # Parse and fix environment variables
                environment = self._parse_and_fix_environment_variables(
                    service_config.get('environment', {}), 
                    service_name, 
                    compose_services, 
                    app_name_for_discovery
                )
                
                # Detect service type and apply intelligent defaults
                service_type = self._detect_service_type(service_name, service_config, environment)
                
                # Determine if service should be load balanced
                is_load_balanced = self._should_service_be_load_balanced(service_name, service_type, container_port)
                
                # Get intelligent resource allocation
                cpu, memory = self._get_intelligent_resource_allocation(service_type, service_name)
                
                # Detect health check path
                health_check_path = self._detect_health_check_path(service_type, service_name, environment)
                
                # Check for persistence requirements
                needs_persistence = self._detect_persistence_needs(service_name, service_config, service_type)
                
                # Create clean variable name for terraform
                clean_service_name = service_name.replace('-', '_').replace(' ', '_').lower()
                
                services_metadata[service_name] = {
                    'image_variable': f"{clean_service_name}_docker_image",
                    'port': container_port,
                    'environment': environment,
                    'depends_on': service_config.get('depends_on', []),
                    'cpu': cpu,
                    'memory': memory,
                    'is_load_balanced': is_load_balanced,
                    'health_check_path': health_check_path,
                    'service_type': service_type,
                    'needs_persistence': needs_persistence,
                    'build_context': service_config.get('build'),  # Track build contexts for warnings
                }
            
            # Post-processing: Log fixes and warnings
            self._log_deployment_warnings_and_fixes(services_metadata, compose_services)
            
            return services_metadata
            
        except Exception as e:
            logger.error(f"Error parsing docker-compose file: {e}")
            return {}

    def _generate_service_discovery_name(self, compose_path: str) -> str:
        """Generate a consistent service discovery namespace name."""
        # Use the directory name of the docker-compose file
        compose_dir = os.path.dirname(compose_path)
        dir_name = os.path.basename(compose_dir) if compose_dir else "app"
        
        # Clean the name for DNS compatibility
        clean_name = dir_name.lower().replace(' ', '-').replace('_', '-')
        clean_name = ''.join(c for c in clean_name if c.isalnum() or c == '-')
        return clean_name

    def _parse_service_port(self, service_name: str, service_config: Dict) -> int:
        """Parse service port with intelligent defaults."""
        ports = service_config.get('ports', [])
        if ports:
            port_entry = ports[0]
            if isinstance(port_entry, str):
                if ':' in port_entry:
                    return int(port_entry.split(':')[-1])
                else:
                    return int(port_entry)
            elif isinstance(port_entry, int):
                return port_entry
            elif isinstance(port_entry, dict):
                return port_entry.get('target', 80)
        
        # Intelligent port defaults based on service name/type
        service_lower = service_name.lower()
        if 'frontend' in service_lower or 'web' in service_lower or 'ui' in service_lower:
            return 3000  # Common frontend port
        elif 'api' in service_lower or 'backend' in service_lower:
            return 4000  # Common API port
        elif 'data' in service_lower or 'analytics' in service_lower:
            return 5000  # Common data service port
        elif 'redis' in service_lower:
            return 6379  # Redis default
        elif 'postgres' in service_lower or 'mysql' in service_lower:
            return 5432 if 'postgres' in service_lower else 3306
        else:
            return 80  # Generic default

    def _parse_and_fix_environment_variables(self, env_raw: Any, service_name: str, 
                                           all_services: Dict, app_name: str) -> Dict[str, str]:
        """Parse environment variables and automatically fix common deployment issues."""
        environment = {}
        
        # Parse environment variables (support both list and dict formats)
        if isinstance(env_raw, list):
            for env_item in env_raw:
                if isinstance(env_item, str) and '=' in env_item:
                    key, value = env_item.split('=', 1)
                    environment[key] = value
        elif isinstance(env_raw, dict):
            environment = env_raw
        
        # Auto-fix common issues
        fixed_environment = {}
        fixes_applied = []
        
        for key, value in environment.items():
            fixed_value = value
            
            # Fix 1: Replace localhost URLs with service discovery DNS
            if isinstance(value, str):
                if 'localhost:' in value or '127.0.0.1:' in value:
                    fixed_value = self._fix_localhost_urls(value, all_services, app_name)
                    if fixed_value != value:
                        fixes_applied.append(f"Fixed localhost URL in {key}: {value} → {fixed_value}")
                
                # Fix 2: Replace service names with fully qualified DNS names
                elif any(svc_name in value for svc_name in all_services.keys() if svc_name != service_name):
                    fixed_value = self._fix_service_references(value, all_services, app_name)
                    if fixed_value != value:
                        fixes_applied.append(f"Enhanced service reference in {key}: {value} → {fixed_value}")
            
            fixed_environment[key] = fixed_value
        
        # Add service discovery environment variables for all services
        fixed_environment['SERVICE_NAME'] = service_name
        fixed_environment['APP_NAME'] = app_name
        fixed_environment['SERVICE_DISCOVERY_NAMESPACE'] = f"{app_name}.local"
        
        # Log fixes if any were applied
        if fixes_applied:
            logger.info(f"Applied environment variable fixes for {service_name}:")
            for fix in fixes_applied:
                logger.info(f"  - {fix}")
        
        return fixed_environment

    def _fix_localhost_urls(self, value: str, all_services: Dict, app_name: str) -> str:
        """Fix localhost URLs to use service discovery DNS."""
        import re
        
        # Pattern to match localhost or 127.0.0.1 with port
        localhost_pattern = r'(https?://)?(localhost|127\.0\.0\.1):(\d+)'
        
        def replace_localhost(match):
            protocol = match.group(1) or 'http://'
            port = match.group(3)
            
            # Find which service uses this port
            for svc_name, svc_config in all_services.items():
                svc_ports = svc_config.get('ports', [])
                for port_entry in svc_ports:
                    svc_port = None
                    if isinstance(port_entry, str) and ':' in port_entry:
                        svc_port = port_entry.split(':')[-1]
                    elif isinstance(port_entry, int):
                        svc_port = str(port_entry)
                    elif isinstance(port_entry, dict):
                        svc_port = str(port_entry.get('target', ''))
                    
                    if svc_port == port:
                        return f"{protocol}{svc_name}.{app_name}.local:{port}"
            
            # If no service found for this port, use generic format
            return f"{protocol}service-{port}.{app_name}.local:{port}"
        
        return re.sub(localhost_pattern, replace_localhost, value)

    def _fix_service_references(self, value: str, all_services: Dict, app_name: str) -> str:
        """Fix bare service name references to use fully qualified DNS names."""
        fixed_value = value
        
        for svc_name in all_services.keys():
            # Look for service names in URLs or connection strings
            patterns = [
                f"://{svc_name}:",  # redis://redis:6379
                f"http://{svc_name}:",  # http://api-service:4000
                f"https://{svc_name}:",  # https://api-service:4000
            ]
            
            for pattern in patterns:
                if pattern in fixed_value:
                    fixed_pattern = pattern.replace(f"://{svc_name}:", f"://{svc_name}.{app_name}.local:")
                    fixed_value = fixed_value.replace(pattern, fixed_pattern)
        
        return fixed_value

    def _detect_service_type(self, service_name: str, service_config: Dict, environment: Dict) -> str:
        """Detect the type of service for intelligent configuration."""
        service_lower = service_name.lower()
        image = service_config.get('image', '').lower()
        
        # Database services
        if any(db in service_lower for db in ['redis', 'postgres', 'mysql', 'mongo', 'elasticsearch']):
            return 'database'
        if any(db in image for db in ['redis', 'postgres', 'mysql', 'mongo', 'elasticsearch']):
            return 'database'
        
        # Frontend services
        if any(fe in service_lower for fe in ['frontend', 'web', 'ui', 'client', 'app']):
            return 'frontend'
        if any(fe_env in environment for fe_env in ['REACT_APP_', 'VUE_APP_', 'NEXT_']):
            return 'frontend'
        
        # API services
        if any(api in service_lower for api in ['api', 'backend', 'server']):
            return 'api'
        
        # Data/Analytics services
        if any(data in service_lower for data in ['data', 'analytics', 'etl', 'worker']):
            return 'data'
        
        # Message brokers
        if any(broker in service_lower for broker in ['kafka', 'rabbitmq', 'nats']):
            return 'broker'
        if any(broker in image for broker in ['kafka', 'rabbitmq', 'nats']):
            return 'broker'
        
        return 'generic'

    def _should_service_be_load_balanced(self, service_name: str, service_type: str, port: int) -> bool:
        """Determine if a service should be load balanced."""
        # Frontend services should always be load balanced
        if service_type == 'frontend':
            return True
        
        # API services on common web ports
        if service_type == 'api' and port in [80, 443, 8080, 3000, 4000]:
            return True
        
        # Services with "web" or "ui" in name
        if any(web in service_name.lower() for web in ['web', 'ui', 'app', 'client']):
            return True
        
        # Common web ports
        if port in [80, 443, 8080, 3000]:
            return True
        
        return False

    def _get_intelligent_resource_allocation(self, service_type: str, service_name: str) -> tuple[int, int]:
        """Get intelligent CPU and memory allocation based on service type."""
        # Resource allocation (CPU in vCPU units * 1024, Memory in MB)
        
        if service_type == 'database':
            # Databases need more memory and moderate CPU
            if 'redis' in service_name.lower():
                return 256, 512  # Redis is memory-efficient
            else:
                return 512, 1024  # PostgreSQL, MySQL need more resources
        
        elif service_type == 'frontend':
            # Frontend services are typically lightweight
            return 256, 512
        
        elif service_type == 'api':
            # API services need balanced resources
            return 512, 1024
        
        elif service_type == 'data':
            # Data services may need more resources for processing
            return 512, 1024
        
        elif service_type == 'broker':
            # Message brokers need more memory
            return 512, 1024
        
        else:
            # Generic services get moderate allocation
            return 256, 512

    def _detect_health_check_path(self, service_type: str, service_name: str, environment: Dict) -> str:
        """Detect appropriate health check path for the service with intelligent fallbacks."""
        # Check environment variables for health check hints
        for key, value in environment.items():
            if any(health_key in key.lower() for health_key in ['health', 'status', 'ping']):
                if isinstance(value, str) and value.startswith('/'):
                    return value
        
        # Database services - use TCP health checks (no HTTP endpoint)
        if service_type == 'database':
            return None  # Will trigger TCP health check instead
        
        # Frontend services - try common frontend health check paths
        if service_type == 'frontend':
            # Common frontend health check paths in order of preference
            return '/health'  # Most modern frontends have this
        
        # API services - look for common API health check patterns
        if service_type == 'api':
            # Common API health check paths in order of preference
            return '/health'  # Most modern APIs have this endpoint
        
        # Data services
        if service_type == 'data':
            return '/health'
        
        # Message brokers - typically don't have HTTP health endpoints
        if service_type == 'broker':
            return None  # Will trigger TCP health check
        
        # Default to root for unknown services, but prefer /health
        return '/health'

    def _detect_persistence_needs(self, service_name: str, service_config: Dict, service_type: str) -> bool:
        """Detect if a service needs persistent storage."""
        # Check for volumes in docker-compose
        if service_config.get('volumes'):
            return True
        
        # Database services typically need persistence
        if service_type == 'database':
            return True
        
        # Services with "data" in name likely need persistence
        if 'data' in service_name.lower():
            return True
        
        return False

    def _get_intelligent_health_check_config(self, service_type: str, service_name: str, port: int) -> Dict[str, Any]:
        """Get intelligent health check configuration based on service type and common patterns."""
        config = {
            'container_start_period': 45,  # Optimized: Reduced from 60s
            'container_interval': 20,      # Optimized: Reduced from 30s  
            'container_timeout': 5,        # Default timeout
            'container_retries': 3,        # Default retries
            'alb_healthy_threshold': 2,    # ALB healthy threshold
            'alb_unhealthy_threshold': 3,  # ALB unhealthy threshold
            'alb_timeout': 5,              # ALB timeout
            'alb_interval': 10,            # Optimized: Reduced from 15s
            'grace_period': 120,           # Optimized: Significantly reduced from 300s (2 min vs 5 min)
            'use_tcp_check': False         # Whether to use TCP instead of HTTP
        }
        
        # Service-type specific optimizations
        if service_type == 'database':
            # Databases typically take longer to start and may not have HTTP endpoints
            config.update({
                'container_start_period': 90,   # Optimized: Reduced from 120s
                'container_interval': 25,       # Optimized: Reduced from 30s
                'container_timeout': 8,         # Optimized: Reduced from 10s
                'container_retries': 4,         # Optimized: Reduced from 5
                'alb_timeout': 8,               # Optimized: Reduced from 10s
                'alb_interval': 20,             # Optimized: Reduced from 30s
                'grace_period': 240,            # Optimized: Drastically reduced from 600s (4 min vs 10 min)
                'use_tcp_check': True           # TCP health check for databases
            })
            
        elif service_type == 'frontend':
            # Frontend apps usually start quickly but need time for assets to load
            config.update({
                'container_start_period': 60,   # Optimized: Reduced from 90s
                'container_interval': 15,       # Optimized: Reduced from 20s
                'container_timeout': 6,         # Optimized: Reduced from 8s
                'container_retries': 3,
                'alb_healthy_threshold': 2,     # Quick promotion to healthy
                'alb_timeout': 6,               # Optimized: Reduced from 8s
                'alb_interval': 8,              # Optimized: Reduced from 10s
                'grace_period': 90              # Optimized: Reduced from 180s (1.5 min vs 3 min)
            })
            
        elif service_type == 'api':
            # API services may have dependencies and need initialization time
            config.update({
                'container_start_period': 75,   # Optimized: Reduced from 120s
                'container_interval': 20,       # Optimized: Reduced from 25s
                'container_timeout': 8,         # Optimized: Reduced from 10s
                'container_retries': 3,         # Optimized: Reduced from 4
                'alb_timeout': 8,               # Optimized: Reduced from 10s
                'alb_interval': 12,             # Optimized: Reduced from 15s
                'grace_period': 150             # Optimized: Reduced from 300s (2.5 min vs 5 min)
            })
            
        elif service_type == 'data':
            # Data services often have heavy initialization and processing
            config.update({
                'container_start_period': 120,  # Optimized: Reduced from 180s
                'container_interval': 30,       # Optimized: Reduced from 45s
                'container_timeout': 12,        # Optimized: Reduced from 15s
                'container_retries': 4,         # Optimized: Reduced from 5
                'alb_timeout': 12,
                'alb_interval': 20,             # Optimized: Reduced from 30s
                'grace_period': 300             # Optimized: Drastically reduced from 900s (5 min vs 15 min)
            })
            
        elif service_type == 'broker':
            # Message brokers need time to establish connections and sync
            config.update({
                'container_start_period': 60,   # Optimized: Reduced from 90s
                'container_interval': 25,       # Optimized: Reduced from 30s
                'container_timeout': 8,         # Optimized: Reduced from 10s
                'container_retries': 3,         # Optimized: Reduced from 4
                'alb_timeout': 8,               # Optimized: Reduced from 10s
                'alb_interval': 15,             # Optimized: Reduced from 20s
                'grace_period': 120,            # Optimized: Reduced from 300s (2 min vs 5 min)
                'use_tcp_check': True           # Most brokers use TCP
            })
        
        # Port-based adjustments (some services are slower on certain ports)
        if port in [5432, 3306, 27017]:  # Database ports
            config['grace_period'] = max(config['grace_period'], 240)  # Optimized: Reduced from 600s
            config['use_tcp_check'] = True
        elif port in [6379]:  # Redis
            config['grace_period'] = max(config['grace_period'], 90)   # Optimized: Reduced from 120s
            config['use_tcp_check'] = True
        elif port in [9200, 9300]:  # Elasticsearch
            config['grace_period'] = max(config['grace_period'], 300)  # Optimized: Reduced from 600s
            
        # Service name based adjustments
        service_lower = service_name.lower()
        if any(db in service_lower for db in ['postgres', 'mysql', 'mongo', 'redis', 'elastic']):
            config['grace_period'] = max(config['grace_period'], 240)  # Optimized: Reduced from 600s
            config['use_tcp_check'] = True
        elif any(heavy in service_lower for heavy in ['kafka', 'spark', 'hadoop', 'analytics']):
            config['grace_period'] = max(config['grace_period'], 360)  # Optimized: Drastically reduced from 900s (6 min vs 15 min)
            config['container_start_period'] = max(config['container_start_period'], 120)  # Optimized: Reduced from 240s
            
        return config

    def _log_deployment_warnings_and_fixes(self, services_metadata: Dict, compose_services: Dict):
        """Log warnings and fixes applied for deployment readiness."""
        logger.info(" Deployment Readiness Analysis:")
        
        # Check for build contexts
        build_warnings = []
        for service_name, metadata in services_metadata.items():
            if metadata.get('build_context'):
                build_warnings.append(service_name)
        
        if build_warnings:
            logger.warning(f"  Build contexts detected for services: {build_warnings}")
            logger.warning("   These services need to be built and pushed to ECR before deployment")
            logger.warning("   Consider using a CI/CD pipeline or ECR build scripts")
        
        # Check for persistence needs
        persistence_services = [
            name for name, metadata in services_metadata.items() 
            if metadata.get('needs_persistence')
        ]
        
        if persistence_services:
            logger.info(f" Services requiring persistent storage: {persistence_services}")
            logger.info("   EFS volumes will be automatically configured for these services")
        
        # Check for load balanced services
        lb_services = [
            name for name, metadata in services_metadata.items()
            if metadata.get('is_load_balanced')
        ]
        
        if lb_services:
            logger.info(f" Services configured for load balancing: {lb_services}")
        
        # Summary
        logger.info(f" Successfully processed {len(services_metadata)} services with automatic optimizations")

    def _parse_dockerfile_to_services_metadata(self, dockerfile_info: Dict, app_name: str) -> Dict[str, Dict[str, Any]]:
        """Parse single Dockerfile into normalized services metadata."""
        container_port = 80
        if dockerfile_info['exposed_ports']:
            container_port = dockerfile_info['exposed_ports'][0]
        
        return {
            'app': {
                'image_variable': 'container_image',
                'port': container_port,
                'environment': {},
                'depends_on': [],
                'cpu': 256,
                'memory': 512,
                'is_load_balanced': True,
                'health_check_path': '/'
            }
        }

    def generate_aws_fargate_terraform(self, services_metadata: Dict[str, Dict[str, Any]], app_name: str, 
                                     fargate_config: Optional[Dict[str, Any]] = None) -> str:
        """
        Generate unified AWS ECS Fargate Terraform for multi-service deployments.
        
        Args:
            services_metadata: Normalized service metadata
            app_name: Application name
            fargate_config: Optional Fargate configuration overrides
            
        Returns:
            Complete Terraform configuration as string
        """
        if not services_metadata:
            raise ValueError("No services metadata provided")
        
        # Detect persistence needs automatically
        has_persistent_services = any(svc.get('needs_persistence', False) for svc in services_metadata.values())
        
        # Apply default fargate config
        config = {
            'region': 'us-east-1',
            'enable_service_discovery': len(services_metadata) > 1,
            'enable_load_balancer': any(svc.get('is_load_balanced', False) for svc in services_metadata.values()),
            'enable_efs': has_persistent_services,
            'log_retention_days': 7,
            'services_metadata': services_metadata  # Pass services metadata to config
        }
        if fargate_config:
            config.update(fargate_config)
        
        # Generate Terraform sections
        terraform_sections = [
            self._generate_terraform_header("ecs"),
            self._generate_terraform_variables(services_metadata, app_name),
            self._generate_terraform_networking(app_name, config),
            self._generate_terraform_efs(app_name, config),
            self._generate_terraform_iam_roles(app_name, config),
            self._generate_terraform_ecs_cluster(app_name),
            self._generate_terraform_service_discovery(app_name, config),
            self._generate_terraform_services(services_metadata, app_name, config),
            self._generate_terraform_load_balancer(services_metadata, app_name, config),
            self._generate_terraform_outputs(services_metadata, app_name, config)
        ]
        
        return '\n\n'.join(filter(None, terraform_sections))

    def _generate_terraform_header(self, deployment_type: str = "ecs") -> str:
        """Generate Terraform provider configuration based on deployment type."""
        if deployment_type == "eks":
            return """# Aurora Multi-Service AWS EKS Terraform Configuration
terraform {
  required_version = ">= 1.0"
  
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.23"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.11"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Kubernetes provider configuration - will use cluster endpoint after creation
provider "kubernetes" {
  host                   = try(data.aws_eks_cluster.this.endpoint, "")
  cluster_ca_certificate = try(base64decode(data.aws_eks_cluster.this.certificate_authority[0].data), "")
  token                  = try(data.aws_eks_cluster_auth.this.token, "")
}

# Helm provider configuration
provider "helm" {
  kubernetes {
    host                   = try(data.aws_eks_cluster.this.endpoint, "")
    cluster_ca_certificate = try(base64decode(data.aws_eks_cluster.this.certificate_authority[0].data), "")
    token                  = try(data.aws_eks_cluster_auth.this.token, "")
  }
}

# Get current AWS account and region
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}"""
        else:
            return """# Aurora Multi-Service AWS ECS Fargate Terraform Configuration
terraform {
  required_version = ">= 1.0"
  
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# Get current AWS account and region
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}"""

    def _generate_terraform_variables(self, services_metadata: Dict[str, Dict[str, Any]], app_name: str) -> str:
        """Generate Terraform variables section."""
        variables = [
            """# Variables
variable "region" {
  description = "The AWS region to deploy to"
  type        = string
  default     = "us-east-1"
}

variable "app_name" {
  description = "Application name"
  type        = string
  default     = \"""" + app_name + """\"
}"""
        ]
        
        # Generate service-specific image variables
        variables.append("\n# Service-specific container image variables")
        for service_name, metadata in services_metadata.items():
            image_var = metadata['image_variable']
            default_image = metadata.get('container_image', f"{service_name}:latest")
            
            variables.extend([
                '',
                f'variable "{image_var}" {{',
                f'  description = "Docker image for {service_name}"',
                '  type        = string',
                f'  default     = "{default_image}"',
                '}',
            ])
        
        return '\n'.join(variables)

    def _generate_terraform_networking(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate networking resources (VPC, subnets, security groups)."""
        networking = [
            """############################################
# Networking                               #
############################################

# Use the default VPC in the selected region
data "aws_vpc" "default" {
  default = true
}

# Fetch all default subnets for the VPC
data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Pick two subnets for ALB / Fargate placement
locals {
  public_subnets = slice(data.aws_subnets.default.ids, 0, 2)
}"""
        ]
        
        # ECS Tasks Security Group (no circular dependencies)
        networking.append(f"""# ECS Tasks Security Group
resource "aws_security_group" "ecs_inter_service" {{
  name        = "${{var.app_name}}-ecs-tasks"
  description = "Security group for ECS tasks"
  vpc_id      = data.aws_vpc.default.id

  # Allow all traffic between services in the same security group
  ingress {{
    description = "Inter-service communication"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }}

  # Allow HTTP traffic from VPC (includes ALB)
  ingress {{
    description = "HTTP from VPC"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  # Allow HTTPS traffic from VPC (includes ALB)  
  ingress {{
    description = "HTTPS from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  # Allow high port traffic from VPC (for custom app ports)
  ingress {{
    description = "Custom app ports from VPC"
    from_port   = 3000
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  # Restricted outbound traffic - only necessary services
  egress {{
    description = "HTTPS outbound"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "HTTP outbound"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "DNS outbound"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "Inter-service communication outbound"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  tags = {{
    Name = "${{var.app_name}}-ecs-tasks-sg"
  }}
}}""")
        
        # Load balancer security group (if needed)
        if config['enable_load_balancer']:
            networking.append("""# ALB Security Group (no circular dependencies)
resource "aws_security_group" "alb" {
  name        = "${var.app_name}-alb"
  description = "Security group for Application Load Balancer"
  vpc_id      = data.aws_vpc.default.id

  # Allow HTTP inbound from internet
  ingress {
    description = "HTTP from internet"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Allow HTTPS inbound from internet  
  ingress {
    description = "HTTPS from internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Restricted outbound - only to ECS tasks in VPC
  egress {
    description = "HTTP to ECS tasks"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  egress {
    description = "HTTPS to ECS tasks"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  egress {
    description = "Custom app ports to ECS tasks"
    from_port   = 3000
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }

  tags = {
    Name = "${var.app_name}-alb-sg"
  }
}""")
        
        return '\n\n'.join(networking)

    def _generate_terraform_efs(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate EFS resources for persistent storage if needed."""
        if not config.get('enable_efs', False):
            return ""
        
        # Get list of services that need persistence
        persistent_services = []
        if 'services_metadata' in config:
            persistent_services = [
                name for name, metadata in config['services_metadata'].items()
                if metadata.get('needs_persistence', False)
            ]
        
        if not persistent_services:
            return ""
        
        logger.info(f" Adding EFS support for persistent services: {persistent_services}")
        
        return f"""############################################
# EFS for Persistent Storage              #
############################################

# EFS File System for persistent data
resource "aws_efs_file_system" "app_data" {{
  creation_token = "${{var.app_name}}-persistent-data"
  
  # Enable encryption
  encrypted = true
  
  # Performance mode (generalPurpose is suitable for most use cases)
  performance_mode = "generalPurpose"
  
  # Throughput mode (provisioned allows for higher throughput if needed)
  throughput_mode = "bursting"
  
  tags = {{
    Name = "${{var.app_name}}-persistent-data"
    Services = "{', '.join(persistent_services)}"
  }}
}}

# EFS Mount Targets (one per subnet for high availability)
resource "aws_efs_mount_target" "app_data" {{
  count           = length(data.aws_subnets.default.ids)
  file_system_id  = aws_efs_file_system.app_data.id
  subnet_id       = data.aws_subnets.default.ids[count.index]
  security_groups = [aws_security_group.efs.id]
}}

# Security Group for EFS
resource "aws_security_group" "efs" {{
  name        = "${{var.app_name}}-efs"
  description = "Security group for EFS mount targets"
  vpc_id      = data.aws_vpc.default.id

  # Allow NFS traffic from EKS nodes (when using EKS)
  ingress {{
    description     = "NFS from EKS nodes"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.eks_nodes.id]
  }}

  # Allow NFS traffic from VPC (covers ECS tasks and other services)
  ingress {{
    description = "NFS from VPC"
    from_port   = 2049
    to_port     = 2049
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  # No outbound rules needed for EFS mount targets
  # EFS mount targets don't initiate outbound connections

  tags = {{
    Name = "${{var.app_name}}-efs-sg"
  }}
}}

{self._generate_efs_access_points(persistent_services)}"""

    def _generate_efs_access_points(self, persistent_services: List[str]) -> str:
        """Generate EFS access points for each persistent service."""
        if not persistent_services:
            return ""
        
        access_points = []
        for service_name in persistent_services:
            clean_service_name = service_name.replace('-', '_')
            access_points.append(f"""# EFS Access Point for {service_name}
resource "aws_efs_access_point" "{clean_service_name}" {{
  file_system_id = aws_efs_file_system.app_data.id
  
  root_directory {{
    path = "/{service_name}"
    creation_info {{
      owner_gid   = 1000
      owner_uid   = 1000
      permissions = "755"
    }}
  }}
  
  posix_user {{
    gid = 1000
    uid = 1000
  }}
  
  tags = {{
    Name = "${{var.app_name}}-{service_name}-access-point"
  }}
}}""")
        
        return '\n\n'.join(access_points)

    def _generate_terraform_iam_roles(self, app_name: str, config: Optional[Dict[str, Any]] = None) -> str:
        """Generate IAM roles for ECS tasks."""
        return f"""############################################
# IAM Roles                                #
############################################

# ECS Task Execution Role
resource "aws_iam_role" "ecs_execution_role" {{
  name = "${{var.app_name}}-ecs-execution-role"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {{
          Service = "ecs-tasks.amazonaws.com"
        }}
      }}
    ]
  }})

  tags = {{
    Name = "${{var.app_name}}-ecs-execution-role"
  }}
}}

resource "aws_iam_role_policy_attachment" "ecs_execution_role_policy" {{
  role       = aws_iam_role.ecs_execution_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}}

# ECS Task Role (for application permissions)
resource "aws_iam_role" "ecs_task_role" {{
  name = "${{var.app_name}}-ecs-task-role"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {{
          Service = "ecs-tasks.amazonaws.com"
        }}
      }}
    ]
  }})

  tags = {{
    Name = "${{var.app_name}}-ecs-task-role"
  }}
}}

# Add permissions for service discovery and EFS
resource "aws_iam_role_policy" "ecs_task_policy" {{
  name = "${{var.app_name}}-ecs-task-policy"
  role = aws_iam_role.ecs_task_role.id

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Effect = "Allow"
        Action = [
          "servicediscovery:DiscoverInstances",
          "servicediscovery:GetService",
          "servicediscovery:ListServices"
        ]
        Resource = "*"
      }}{"," + '''
      {
        Effect = "Allow"
        Action = [
          "elasticfilesystem:CreateAccessPoint",
          "elasticfilesystem:TagResource", 
          "elasticfilesystem:DescribeMountTargets",
          "elasticfilesystem:DescribeFileSystems"
        ]
        Resource = "*"
      }''' if config and config.get('enable_efs', False) else ''}
    ]
  }})
}}"""

    def _generate_terraform_ecs_cluster(self, app_name: str) -> str:
        """Generate ECS cluster configuration."""
        return """############################################
# ECS Cluster                              #
############################################

resource "aws_ecs_cluster" "main" {
  name = var.app_name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = var.app_name
  }
}

# Cluster Capacity Providers
resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name = aws_ecs_cluster.main.name

  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    base              = 1
    weight            = 100
    capacity_provider = "FARGATE"
  }
}"""

    def _generate_terraform_service_discovery(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate service discovery configuration if enabled."""
        if not config['enable_service_discovery']:
            return ""
        
        return """############################################
# Service Discovery                        #
############################################

# Service Discovery Namespace
resource "aws_service_discovery_private_dns_namespace" "app" {
  name        = "${var.app_name}.local"
  description = "Service discovery namespace for ${var.app_name}"
  vpc         = data.aws_vpc.default.id

  tags = {
    Name = "${var.app_name}-discovery"
  }
}"""

    def _generate_terraform_services(self, services_metadata: Dict[str, Dict[str, Any]], 
                                   app_name: str, config: Dict[str, Any]) -> str:
        """Generate ECS services for all services."""
        services_tf = ["############################################\n# ECS Services                           #\n############################################"]
        
        for service_name, metadata in services_metadata.items():
            services_tf.append(self._generate_single_service_terraform(service_name, metadata, config))
        
        return '\n\n'.join(services_tf)

    def _generate_single_service_terraform(self, service_name: str, metadata: Dict[str, Any], 
                                         config: Dict[str, Any]) -> str:
        """Generate Terraform for a single ECS service."""
        # Prepare environment variables
        env_vars = []
        for key, value in metadata.get('environment', {}).items():
            # Convert None to empty string so join() never sees a NoneType
            env_vars.append(f'        {{"name": "{key}", "value": "{"" if value is None else value}"}}')
        
        # Add service discovery environment variables
        service_discovery_env = [
            f'        {{"name": "SERVICE_NAME", "value": "{service_name}"}}',
            f'        {{"name": "APP_NAME", "value": "${{var.app_name}}"}}'
        ]
        
        all_env_vars = env_vars + service_discovery_env
        env_vars_json = ',\n'.join(all_env_vars) if all_env_vars else ''
        
        # Build depends_on clause - optimize for parallel deployment
        depends_on_services = []
        essential_deps = []
        
        # Only include essential runtime dependencies (databases, brokers)
        # Skip frontend->api dependencies for parallel deployment
        for dep in metadata.get('depends_on', []):
            dep_metadata = config['services_metadata'].get(dep, {})
            dep_service_type = dep_metadata.get('service_type', 'generic')
            
            # Only add dependencies for essential services that must be up first
            if dep_service_type in ['database', 'broker', 'cache']:
                essential_deps.append(f'aws_ecs_service.{dep.replace("-", "_")}')
            # Allow frontend/api services to start in parallel for faster deployment
        
        depends_on_clause = ''
        if essential_deps:
            depends_on_clause = f'  depends_on = [{", ".join(essential_deps)}]\n'
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Service {service_name} will wait for essential services: {[dep.split('.')[-1] for dep in essential_deps]}")
        else:
            import logging
            logger = logging.getLogger(__name__)
            logger.info(f"Service {service_name} optimized for parallel deployment (no blocking dependencies)")
        
        # Service discovery registration
        service_registry = ""
        if config['enable_service_discovery']:
            service_registry = f"""
  service_registries {{
    registry_arn = aws_service_discovery_service.{service_name.replace("-", "_")}.arn
  }}"""
        
        # Clean service name for terraform resources
        clean_service_name = service_name.replace('-', '_')
        
        terraform_blocks = []
        
        # CloudWatch Log Group
        terraform_blocks.append(f"""# CloudWatch Log Group for {service_name}
resource "aws_cloudwatch_log_group" "{clean_service_name}" {{
  name              = "/ecs/${{var.app_name}}-{service_name}"
  retention_in_days = {config['log_retention_days']}

  tags = {{
    Name = "${{var.app_name}}-{service_name}-logs"
  }}
}}""")
        
        # Service Discovery Service (if enabled)
        if config['enable_service_discovery']:
            terraform_blocks.append(f"""# Service Discovery Service for {service_name}
resource "aws_service_discovery_service" "{clean_service_name}" {{
  name = "{service_name}"

  dns_config {{
    namespace_id = aws_service_discovery_private_dns_namespace.app.id

    dns_records {{
      ttl  = 10
      type = "A"
    }}

    routing_policy = "MULTIVALUE"
  }}
}}""")
        
        # Task Definition with intelligent health check configuration
        health_check_config = self._get_intelligent_health_check_config(
            metadata.get('service_type', 'generic'), 
            service_name, 
            metadata['port']
        )
        
        health_check = ""
        health_check_path = metadata.get('health_check_path')
        
        if health_check_path and not health_check_config['use_tcp_check']:
            # HTTP health check
            health_check = f'''
      healthCheck = {{
        command     = ["CMD-SHELL", "curl -f http://localhost:{metadata['port']}{health_check_path} || exit 1"]
        interval    = {health_check_config['container_interval']}
        timeout     = {health_check_config['container_timeout']}
        retries     = {health_check_config['container_retries']}
        startPeriod = {health_check_config['container_start_period']}
      }}'''
        elif health_check_config['use_tcp_check']:
            # TCP health check for databases and brokers
            health_check = f'''
      healthCheck = {{
        command     = ["CMD-SHELL", "nc -z localhost {metadata['port']} || exit 1"]
        interval    = {health_check_config['container_interval']}
        timeout     = {health_check_config['container_timeout']}
        retries     = {health_check_config['container_retries']}
        startPeriod = {health_check_config['container_start_period']}
      }}'''
        
        # Add EFS mount points for persistent services
        mount_points = ""
        if metadata.get('needs_persistence', False) and config.get('enable_efs', False):
            # Determine mount path based on service type
            mount_path = "/data"
            if metadata.get('service_type') == 'database':
                if 'redis' in service_name.lower():
                    mount_path = "/data"
                elif 'postgres' in service_name.lower():
                    mount_path = "/var/lib/postgresql/data"
                elif 'mysql' in service_name.lower():
                    mount_path = "/var/lib/mysql"
                elif 'mongo' in service_name.lower():
                    mount_path = "/data/db"
            
            mount_points = f'''
      mountPoints = [
        {{
          sourceVolume  = "efs-volume"
          containerPath = "{mount_path}"
          readOnly      = false
        }}
      ]'''
        
        # Add EFS volume definition if persistence is needed
        volume_definition = ""
        if metadata.get('needs_persistence', False) and config.get('enable_efs', False):
            volume_definition = f'''

  volume {{
    name = "efs-volume"
    
    efs_volume_configuration {{
      file_system_id     = aws_efs_file_system.app_data.id
      root_directory     = "/{service_name}"
      transit_encryption = "ENABLED"
      
      authorization_config {{
        access_point_id = aws_efs_access_point.{clean_service_name}.id
        iam             = "ENABLED"
      }}
    }}
  }}'''
        
        terraform_blocks.append(f"""# Task Definition for {service_name}
resource "aws_ecs_task_definition" "{clean_service_name}" {{
  family                   = "${{var.app_name}}-{service_name}"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                     = "{metadata.get('cpu', 256)}"
  memory                  = "{metadata.get('memory', 512)}"
  execution_role_arn      = aws_iam_role.ecs_execution_role.arn
  task_role_arn           = aws_iam_role.ecs_task_role.arn

  container_definitions = jsonencode([
    {{
      name      = "{service_name}"
      image     = var.{metadata['image_variable']}
      essential = true

      portMappings = [
        {{
          containerPort = {metadata['port']}
          protocol      = "tcp"
        }}
      ]

      environment = [
{env_vars_json}
      ]{mount_points}

      logConfiguration = {{
        logDriver = "awslogs"
        options = {{
          awslogs-group         = aws_cloudwatch_log_group.{clean_service_name}.name
          awslogs-region        = var.region
          awslogs-stream-prefix = "ecs"
        }}
      }}{health_check}
    }}
  ]){volume_definition}

  tags = {{
    Name = "${{var.app_name}}-{service_name}"
  }}
}}""")
        
        # Generate load balancer block and security groups
        load_balancer_block = ""
        security_groups = "[aws_security_group.ecs_inter_service.id]"
        
        if metadata.get('is_load_balanced', False) and config.get('enable_load_balancer', False):
            load_balancer_block = f"""

  load_balancer {{
    target_group_arn = aws_lb_target_group.{clean_service_name}.arn
    container_name   = "{service_name}"
    container_port   = {metadata['port']}
  }}"""
            # Load balanced services only need their own security group (ALB has separate rules)
            security_groups = "[aws_security_group.ecs_inter_service.id]"

        # ECS Service with intelligent grace period
        grace_period_config = ""
        if metadata.get('is_load_balanced', False) and config.get('enable_load_balancer', False):
            grace_period_config = f"\n  health_check_grace_period_seconds = {health_check_config['grace_period']}"
        
        terraform_blocks.append(f"""# ECS Service for {service_name}
resource "aws_ecs_service" "{clean_service_name}" {{
  name            = "${{var.app_name}}-{service_name}"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.{clean_service_name}.arn
  launch_type     = "FARGATE"
  desired_count   = 1{grace_period_config}

  network_configuration {{
    security_groups  = {security_groups}
    subnets          = local.public_subnets
    assign_public_ip = true
  }}{service_registry}{load_balancer_block}

{depends_on_clause}  tags = {{
    Name = "${{var.app_name}}-{service_name}"
  }}
}}""")
        
        return '\n\n'.join(terraform_blocks)

    def _generate_terraform_load_balancer(self, services_metadata: Dict[str, Dict[str, Any]], 
                                        app_name: str, config: Dict[str, Any]) -> str:
        """Generate load balancer configuration if needed."""
        if not config['enable_load_balancer']:
            return ""
        
        # Find load balanced services
        load_balanced_services = [
            name for name, metadata in services_metadata.items() 
            if metadata.get('is_load_balanced', False)
        ]
        
        if not load_balanced_services:
            return ""
        
        lb_terraform = [
            """############################################
# Application Load Balancer               #
############################################

resource "aws_lb" "app" {
  name               = "${var.app_name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets           = local.public_subnets

  enable_deletion_protection = false

  tags = {
    Name = "${var.app_name}-alb"
  }
}"""
        ]
        
        # Generate target groups and listeners
        main_service = load_balanced_services[0]
        main_metadata = services_metadata[main_service]
        clean_main_service = main_service.replace('-', '_')
        
        # Main target group with intelligent health check config
        main_health_config = self._get_intelligent_health_check_config(
            main_metadata.get('service_type', 'generic'), 
            main_service, 
            main_metadata['port']
        )
        
        lb_terraform.append(f"""# Target Group for {main_service}
resource "aws_lb_target_group" "{clean_main_service}" {{
  name        = "${{var.app_name}}-{main_service}-tg"
  port        = {main_metadata['port']}
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"

  health_check {{
    enabled             = true
    healthy_threshold   = {main_health_config['alb_healthy_threshold']}
    unhealthy_threshold = {main_health_config['alb_unhealthy_threshold']}
    timeout             = {main_health_config['alb_timeout']}
    interval            = {main_health_config['alb_interval']}
    path                = "{main_metadata.get('health_check_path', '/')}"
    matcher             = "200-499"
    port                = "traffic-port"
  }}

  tags = {{
    Name = "${{var.app_name}}-{main_service}-tg"
  }}
}}""")
        
        # Additional target groups for other load-balanced services
        for service_name in load_balanced_services[1:]:  # Skip main service
            metadata = services_metadata[service_name]
            clean_service_name = service_name.replace('-', '_')
            
            # Get intelligent health check config for this service
            health_config = self._get_intelligent_health_check_config(
                metadata.get('service_type', 'generic'), 
                service_name, 
                metadata['port']
            )
            
            lb_terraform.append(f"""# Target Group for {service_name}
resource "aws_lb_target_group" "{clean_service_name}" {{
  name        = "${{var.app_name}}-{service_name}-tg"
  port        = {metadata['port']}
  protocol    = "HTTP"
  vpc_id      = data.aws_vpc.default.id
  target_type = "ip"

  health_check {{
    enabled             = true
    healthy_threshold   = {health_config['alb_healthy_threshold']}
    unhealthy_threshold = {health_config['alb_unhealthy_threshold']}
    timeout             = {health_config['alb_timeout']}
    interval            = {health_config['alb_interval']}
    path                = "{metadata.get('health_check_path', '/')}"
    matcher             = "200-499"
    port                = "traffic-port"
  }}

  tags = {{
    Name = "${{var.app_name}}-{service_name}-tg"
  }}
}}""")

        # Main listener
        lb_terraform.append(f"""# Main Listener (port 80) -> {main_service}
resource "aws_lb_listener" "main" {{
  load_balancer_arn = aws_lb.app.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {{
    type             = "forward"
    target_group_arn = aws_lb_target_group.{clean_main_service}.arn
  }}
}}""")
        
        # Additional listeners for other services (on different ports)
        port_counter = 8080
        for service_name in load_balanced_services[1:]:  # Skip main service
            clean_service_name = service_name.replace('-', '_')
            
            lb_terraform.append(f"""# Listener for {service_name} (port {port_counter})
resource "aws_lb_listener" "{clean_service_name}" {{
  load_balancer_arn = aws_lb.app.arn
  port              = "{port_counter}"
  protocol          = "HTTP"

  default_action {{
    type             = "forward"
    target_group_arn = aws_lb_target_group.{clean_service_name}.arn
  }}
}}""")
            port_counter += 1
        
        return '\n\n'.join(lb_terraform)

    def _generate_terraform_outputs(self, services_metadata: Dict[str, Dict[str, Any]], 
                                  app_name: str, config: Dict[str, Any]) -> str:
        """Generate Terraform outputs."""
        outputs = [
            """############################################
# Outputs                                  #
############################################"""
        ]
        
        # Load balancer outputs (if enabled)
        if config['enable_load_balancer']:
            # Find load balanced services
            load_balanced_services = [
                name for name, metadata in services_metadata.items() 
                if metadata.get('is_load_balanced', False)
            ]
            
            outputs.append("""output "load_balancer_dns" {
  description = "DNS name of the load balancer"
  value       = aws_lb.app.dns_name
}""")
            
            # Main service URL (port 80)
            if load_balanced_services:
                main_service = load_balanced_services[0]
                outputs.append(f"""output "{main_service}_url" {{
  description = "URL of the {main_service} service (main port 80)"
  value       = "http://${{aws_lb.app.dns_name}}"
}}""")
                
                # Additional service URLs (ports 8080+)
                port_counter = 8080
                for service_name in load_balanced_services[1:]:
                    outputs.append(f"""output "{service_name}_url" {{
  description = "URL of the {service_name} service (port {port_counter})"
  value       = "http://${{aws_lb.app.dns_name}}:{port_counter}"
}}""")
                    port_counter += 1
        
        # Service ARN outputs
        for service_name in services_metadata.keys():
            clean_service_name = service_name.replace('-', '_')
            outputs.append(f"""output "{service_name}_service_arn" {{
  description = "ARN of the {service_name} ECS service"
  value       = aws_ecs_service.{clean_service_name}.id
}}""")
        
        return '\n\n'.join(outputs)

    def _generate_azure_terraform(self, dockerfile_path: str, app_id: str) -> str:
        """
        Generate Terraform configuration for Azure Container Apps based on project structure.
        
        Args:
            dockerfile_path: Either a path to a Dockerfile OR the extraction directory
            app_id: Application ID for naming
        """
        # Determine dockerfile info intelligently
        if os.path.isfile(dockerfile_path):
            # dockerfile_path is an actual Dockerfile
            dockerfile_info = self._parse_dockerfile(dockerfile_path)
            logger.info(f" Using Dockerfile at: {dockerfile_path}")
        else:
            # dockerfile_path is the extraction directory - no Dockerfile found
            dockerfile_info = {'exposed_ports': []}  # Default empty dockerfile info
            logger.info(f" No Dockerfile found, using default configuration")
        
        # Determine container port (default to 80 if not specified)
        container_port = 80
        if dockerfile_info['exposed_ports']:
            container_port = dockerfile_info['exposed_ports'][0]
        
        # Create a sanitized name for the Container App
        app_name = f"containerapp-{app_id[:8]}"
        logger.info(f"  Application name: {app_name}")
        logger.info(f" Container port: {container_port}")
        
        # Generate the Terraform code
        tf_code = f"""# Aurora Comprehensive Azure Container Apps Terraform Configuration
terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
}}

# Variables
variable "resource_group_name" {{
  description = "The Azure resource group name"
  type        = string
}}

variable "location" {{
  description = "The Azure location to deploy to"
  type        = string
  default     = "East US"
}}

variable "container_image" {{
  description = "Container image to deploy (after you push to ACR)"
  type        = string
  default     = "nginx:latest"  # Default for destroy operations
}}

variable "app_name" {{
  description = "Application name"
  type        = string
  default     = "{app_name}"
}}

# Resource Group (using existing)
data "azurerm_resource_group" "main" {{
  name = var.resource_group_name
}}

# Log Analytics Workspace
resource "azurerm_log_analytics_workspace" "app" {{
  name                = "${{var.app_name}}-logs"
  location            = data.azurerm_resource_group.main.location
  resource_group_name = data.azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30

  tags = {{
    Name = "${{var.app_name}}-logs"
  }}
}}

# Container Apps Environment
resource "azurerm_container_app_environment" "app" {{
  name                       = "${{var.app_name}}-env"
  location                   = data.azurerm_resource_group.main.location
  resource_group_name        = data.azurerm_resource_group.main.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.app.id

  tags = {{
    Name = "${{var.app_name}}-env"
  }}
}}

# Container App
resource "azurerm_container_app" "app" {{
  name                         = var.app_name
  container_app_environment_id = azurerm_container_app_environment.app.id
  resource_group_name          = data.azurerm_resource_group.main.name
  revision_mode                = "Single"

  template {{
    container {{
      name   = var.app_name
      image  = var.container_image
      cpu    = 0.25
      memory = "0.5Gi"
    }}

    max_replicas = 1
    min_replicas = 1
  }}

  ingress {{
    allow_insecure_connections = false
    external_enabled           = true
    target_port                = {container_port}

    traffic_weight {{
      percentage      = 100
      latest_revision = true
    }}
  }}

  tags = {{
    Name = var.app_name
  }}
}}

# Output the service URL
output "service_url" {{
  value = "https://${{azurerm_container_app.app.latest_revision_fqdn}}"
}}
"""
        return tf_code

    def _parse_kubernetes_to_services_metadata(self, k8s_manifests: List[str]) -> Dict[str, Dict[str, Any]]:
        """Parse Kubernetes manifests into normalized services metadata with intelligent detection."""
        try:
            import yaml
            
            services_metadata = {}
            deployments = {}
            services = {}
            
            logger.info(f"Found {len(k8s_manifests)} Kubernetes manifest files: {k8s_manifests}")
            
            # First pass: parse all manifests and categorize
            for manifest_path in k8s_manifests:
                try:
                    with open(manifest_path, 'r') as f:
                        docs = list(yaml.safe_load_all(f))
                    
                    for doc in docs:
                        if not doc or 'kind' not in doc:
                            continue
                            
                        kind = doc['kind']
                        name = doc.get('metadata', {}).get('name', 'unknown')
                        
                        if kind == 'Deployment':
                            deployments[name] = doc
                            logger.info(f"Found Deployment: {name}")
                        elif kind == 'Service':
                            services[name] = doc
                            logger.info(f"Found Service: {name}")
                        
                except Exception as e:
                    logger.warning(f"Error parsing manifest {manifest_path}: {e}")
            
            # Auto-detect application name for service discovery
            app_name_for_discovery = self._generate_k8s_app_name(k8s_manifests)
            
            # Second pass: extract service metadata from deployments
            for deployment_name, deployment_doc in deployments.items():
                try:
                    container_spec = self._extract_k8s_container_spec(deployment_doc)
                    if not container_spec:
                        continue
                    
                    # Parse ports
                    container_port = self._parse_k8s_service_port(deployment_name, container_spec, services)
                    
                    # Parse environment variables
                    environment = self._parse_k8s_environment_variables(
                        container_spec, deployment_name, deployments, app_name_for_discovery
                    )
                    
                    # Detect service type
                    service_type = self._detect_service_type(deployment_name, container_spec, environment)
                    
                    # Check if service should be load balanced (has a LoadBalancer service)
                    is_load_balanced = self._is_k8s_service_load_balanced(deployment_name, services)
                    
                    # Get intelligent resource allocation
                    cpu, memory = self._get_k8s_resource_allocation(container_spec, service_type, deployment_name)
                    
                    # Detect health check path
                    health_check_path = self._detect_health_check_path(service_type, deployment_name, environment)
                    
                    # Check for persistence requirements
                    needs_persistence = self._detect_k8s_persistence_needs(deployment_name, deployment_doc, service_type)
                    
                    # Create clean variable name for terraform
                    clean_service_name = deployment_name.replace('-', '_').replace(' ', '_').lower()
                    
                    services_metadata[deployment_name] = {
                        'image_variable': f"{clean_service_name}_docker_image",
                        'port': container_port,
                        'environment': environment,
                        'depends_on': [],  # K8s handles dependencies differently
                        'cpu': cpu,
                        'memory': memory,
                        'is_load_balanced': is_load_balanced,
                        'health_check_path': health_check_path,
                        'service_type': service_type,
                        'needs_persistence': needs_persistence,
                        'k8s_deployment': deployment_doc,  # Store original K8s spec
                        'k8s_service': services.get(deployment_name),  # Associated service
                        'container_image': container_spec.get('image', f"{deployment_name}:latest"),
                    }
                    
                except Exception as e:
                    logger.warning(f"Error processing deployment {deployment_name}: {e}")
            
            if services_metadata:
                logger.info(f"Successfully parsed {len(services_metadata)} services from Kubernetes manifests")
                logger.info(f"Services: {list(services_metadata.keys())}")
            
            return services_metadata
            
        except Exception as e:
            logger.error(f"Error parsing Kubernetes manifests: {e}")
            return {}

    def _generate_k8s_app_name(self, k8s_manifests: List[str]) -> str:
        """Generate a consistent application name from K8s manifests."""
        if k8s_manifests:
            # Use the directory name of the first manifest
            manifest_dir = os.path.dirname(k8s_manifests[0])
            dir_name = os.path.basename(manifest_dir) if manifest_dir else "k8s-app"
        else:
            dir_name = "k8s-app"
        
        # Clean the name for DNS compatibility
        clean_name = dir_name.lower().replace(' ', '-').replace('_', '-')
        clean_name = ''.join(c for c in clean_name if c.isalnum() or c == '-')
        return clean_name

    def _extract_k8s_container_spec(self, deployment_doc: Dict) -> Dict:
        """Extract the main container spec from a Kubernetes Deployment."""
        try:
            containers = deployment_doc.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
            if containers:
                return containers[0]  # Return first container
        except Exception as e:
            logger.warning(f"Error extracting container spec: {e}")
        return {}

    def _parse_k8s_service_port(self, deployment_name: str, container_spec: Dict, services: Dict) -> int:
        """Parse service port from K8s container spec and associated services."""
        # First, try to find port from associated service
        if deployment_name in services:
            service_spec = services[deployment_name].get('spec', {})
            service_ports = service_spec.get('ports', [])
            if service_ports:
                return service_ports[0].get('targetPort', service_ports[0].get('port', 80))
        
        # Next, try container ports
        container_ports = container_spec.get('ports', [])
        if container_ports:
            return container_ports[0].get('containerPort', 80)
        
        # Fallback to intelligent defaults based on service name
        return self._parse_service_port(deployment_name, {})

    def _parse_k8s_environment_variables(self, container_spec: Dict, service_name: str, 
                                       all_deployments: Dict, app_name: str) -> Dict[str, str]:
        """Parse environment variables from K8s container spec and apply fixes."""
        environment = {}
        
        # Extract environment variables from container spec
        env_vars = container_spec.get('env', [])
        for env_var in env_vars:
            if 'name' in env_var and 'value' in env_var:
                environment[env_var['name']] = str(env_var['value'])
        
        # Apply the same auto-fixes as docker-compose
        return self._parse_and_fix_environment_variables(
            environment, service_name, all_deployments, app_name
        )

    def _is_k8s_service_load_balanced(self, deployment_name: str, services: Dict) -> bool:
        """Check if a K8s deployment has a LoadBalancer service."""
        if deployment_name in services:
            service_spec = services[deployment_name].get('spec', {})
            return service_spec.get('type') == 'LoadBalancer'
        return False

    def _get_k8s_resource_allocation(self, container_spec: Dict, service_type: str, service_name: str) -> tuple[int, int]:
        """Get resource allocation from K8s container spec or apply intelligent defaults."""
        resources = container_spec.get('resources', {})
        
        # Try to parse existing resource requests/limits
        requests = resources.get('requests', {})
        limits = resources.get('limits', {})
        
        cpu_str = requests.get('cpu') or limits.get('cpu')
        memory_str = requests.get('memory') or limits.get('memory')
        
        cpu = 0
        memory = 0
        
        # Parse CPU (e.g., "100m", "0.5", "1")
        if cpu_str:
            try:
                if cpu_str.endswith('m'):
                    cpu = int(cpu_str[:-1])
                else:
                    cpu = int(float(cpu_str) * 1000)
            except:
                pass
        
        # Parse Memory (e.g., "128Mi", "1Gi", "512M")
        if memory_str:
            try:
                if memory_str.endswith('Mi'):
                    memory = int(memory_str[:-2])
                elif memory_str.endswith('Gi'):
                    memory = int(float(memory_str[:-2]) * 1024)
                elif memory_str.endswith('M'):
                    memory = int(memory_str[:-1])
                elif memory_str.endswith('G'):
                    memory = int(float(memory_str[:-1]) * 1024)
            except:
                pass
        
        # Apply intelligent defaults if not specified
        if cpu == 0 or memory == 0:
            default_cpu, default_memory = self._get_intelligent_resource_allocation(service_type, service_name)
            cpu = cpu or default_cpu
            memory = memory or default_memory
        
        return cpu, memory

    def _detect_k8s_persistence_needs(self, deployment_name: str, deployment_doc: Dict, service_type: str) -> bool:
        """Detect if a K8s deployment needs persistent storage."""
        # Check for volume mounts in the deployment
        try:
            containers = deployment_doc.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
            for container in containers:
                volume_mounts = container.get('volumeMounts', [])
                if volume_mounts:
                    return True
            
            # Check for volumes in the pod spec
            volumes = deployment_doc.get('spec', {}).get('template', {}).get('spec', {}).get('volumes', [])
            persistent_volumes = [v for v in volumes if 'persistentVolumeClaim' in v]
            if persistent_volumes:
                return True
        except:
            pass
        
        # Fallback to service type detection
        return self._detect_persistence_needs(deployment_name, {}, service_type)

    def generate_aws_eks_terraform(self, services_metadata: Dict[str, Dict[str, Any]], app_name: str, 
                                  eks_config: Optional[Dict[str, Any]] = None) -> str:
        """Generate comprehensive AWS EKS Terraform configuration for Kubernetes deployments."""
        try:
            logger.info(f"Generating AWS EKS Terraform for {len(services_metadata)} services")
            
            # Default EKS configuration
            config = {
                'region': 'us-east-1',
                'availability_zones': ['us-east-1a', 'us-east-1b'],
                'node_instance_types': ['t3.medium'],
                'desired_capacity': 2,
                'min_capacity': 1,
                'max_capacity': 4,
                'k8s_version': '1.28',
                **(eks_config or {})
            }
            
            # Check for persistent services to enable EFS
            persistent_services = [name for name, metadata in services_metadata.items() 
                                 if metadata.get('needs_persistence', False)]
            config['has_persistent_services'] = len(persistent_services) > 0
            config['persistent_services'] = persistent_services
            
            # Generate all terraform components
            terraform_content = []
            terraform_content.append(self._generate_terraform_header("eks"))
            terraform_content.append(self._generate_eks_terraform_variables(services_metadata, app_name))
            terraform_content.append(self._generate_eks_terraform_networking(app_name, config))
            
            if config['has_persistent_services']:
                terraform_content.append(self._generate_terraform_efs(app_name, config))
            
            terraform_content.append(self._generate_eks_terraform_iam_roles(app_name, config))
            terraform_content.append(self._generate_eks_terraform_cluster(app_name, config))
            terraform_content.append(self._generate_eks_terraform_node_group(app_name, config))
            
            # Inject aws-auth ConfigMap so Terraform role & node role are recognized by the cluster
            terraform_content.append(self._generate_eks_aws_auth_config_map())
            
            
            
            terraform_content.append(self._generate_eks_terraform_outputs(services_metadata, app_name, config))
            
            final_terraform = '\n\n'.join(terraform_content)
            logger.info("Successfully generated EKS Terraform configuration")
            return final_terraform
            
        except Exception as e:
            logger.error(f"Error generating EKS Terraform: {e}")
            raise

    def _generate_eks_terraform_variables(self, services_metadata: Dict[str, Dict[str, Any]], app_name: str) -> str:
        """Generate Terraform variables for EKS deployment."""
        variables = [
            'variable "aws_region" {',
            '  description = "AWS region"',
            '  type        = string',
            '  default     = "us-east-1"',
            '}',
            '',
            'variable "cluster_name" {',
            '  description = "EKS cluster name"',
            '  type        = string',
            f'  default     = "{app_name}"',
            '}',
            '',
            'variable "k8s_version" {',
            '  description = "Kubernetes version"',
            '  type        = string',
            '  default     = "1.28"',
            '}',
            '',
            'variable "node_instance_types" {',
            '  description = "Instance types for EKS node group"',
            '  type        = list(string)',
            '  default     = ["t3.medium"]',
            '}',
            '',
            'variable "desired_capacity" {',
            '  description = "Desired number of nodes"',
            '  type        = number',
            '  default     = 2',
            '}',
            '',
            'variable "min_capacity" {',
            '  description = "Minimum number of nodes"',
            '  type        = number',
            '  default     = 1',
            '}',
            '',
            'variable "max_capacity" {',
            '  description = "Maximum number of nodes"',
            '  type        = number',
            '  default     = 4',
            '}',
            '',
            'variable "capacity_type" {',
            '  description = "Capacity type for EKS node group (ON_DEMAND or SPOT)"',
            '  type        = string',
            '  default     = "ON_DEMAND"',
            '}',
            '',
            'variable "enable_cluster_logging" {',
            '  description = "Enable EKS cluster logging (can be disabled for faster deployment)"',
            '  type        = bool',
            '  default     = true',
            '}',
        ]
        
        # Add container image variables for each service
        for service_name, metadata in services_metadata.items():
            image_var = metadata['image_variable']
            default_image = metadata.get('container_image', f"{service_name}:latest")
            
            variables.extend([
                '',
                f'variable "{image_var}" {{',
                f'  description = "Docker image for {service_name}"',
                '  type        = string',
                f'  default     = "{default_image}"',
                '}',
            ])
        
        return '\n'.join(variables)

    def _generate_eks_terraform_networking(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate networking configuration for EKS using default VPC to avoid VPC limits."""
        return f"""############################################
# Networking (Using Default VPC)          #
############################################

# Use the default VPC in the selected region (avoids VPC limits)
data "aws_vpc" "default" {{
  default = true
}}

# Get all default subnets for the VPC
data "aws_subnets" "default" {{
  filter {{
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }}
}}

# Get subnet details for availability zone distribution
data "aws_subnet" "default_subnets" {{
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}}

# Select subnets across different availability zones for EKS
locals {{
  # Get unique availability zones from default subnets
  available_azs = distinct([for subnet in data.aws_subnet.default_subnets : subnet.availability_zone])
  
  # Select first 2 AZs for EKS cluster (minimum requirement)
  selected_azs = slice(local.available_azs, 0, min(2, length(local.available_azs)))
  
  # Select one subnet per selected AZ for EKS
  eks_subnet_ids = [
    for az in local.selected_azs : [
      for subnet_id, subnet in data.aws_subnet.default_subnets : subnet_id
      if subnet.availability_zone == az
    ][0]
  ]
}}

# Security Group for EKS Cluster
resource "aws_security_group" "eks_cluster" {{
  name_prefix = "${{var.cluster_name}}-cluster-"
  vpc_id      = data.aws_vpc.default.id

  # Restricted egress - only necessary outbound traffic
  egress {{
    description = "HTTPS outbound for AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "DNS outbound"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "Communication to node groups"
    from_port   = 1025
    to_port     = 65535
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  lifecycle {{
    create_before_destroy = true
  }}

  tags = {{
    Name = "${{var.cluster_name}}-cluster-sg"
    "kubernetes.io/cluster/${{var.cluster_name}}" = "shared"
  }}
}}

# Security Group for EKS Node Groups
resource "aws_security_group" "eks_nodes" {{
  name_prefix = "${{var.cluster_name}}-nodes-"
  vpc_id      = data.aws_vpc.default.id

  # Allow nodes to communicate with each other
  ingress {{
    description = "Node to node communication"
    from_port   = 0
    to_port     = 65535
    protocol    = "tcp"
    self        = true
  }}

  # Restricted egress - only necessary outbound traffic
  egress {{
    description = "HTTPS outbound for AWS APIs and container registries"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "HTTP outbound for package downloads"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "DNS outbound"
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "NTP outbound"
    from_port   = 123
    to_port     = 123
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }}

  egress {{
    description = "Node to cluster communication"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.default.cidr_block]
  }}

  lifecycle {{
    create_before_destroy = true
  }}

  tags = {{
    Name = "${{var.cluster_name}}-nodes-sg"
    "kubernetes.io/cluster/${{var.cluster_name}}" = "shared"
  }}
}}

# Separate Security Group Rules to avoid circular dependencies

# Allow cluster to communicate with node groups  
resource "aws_security_group_rule" "cluster_to_nodes_443" {{
  description              = "Allow cluster to receive HTTPS from node groups"
  from_port                = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.eks_cluster.id
  source_security_group_id = aws_security_group.eks_nodes.id
  to_port                  = 443
  type                     = "ingress"
}}

# Allow cluster control plane to communicate with worker nodes
resource "aws_security_group_rule" "cluster_to_nodes_1025_65535" {{
  description              = "Allow cluster control plane to communicate with node kubelet"
  from_port                = 1025
  protocol                 = "tcp"
  security_group_id        = aws_security_group.eks_nodes.id
  source_security_group_id = aws_security_group.eks_cluster.id
  to_port                  = 65535
  type                     = "ingress"
}}

# Allow nodes to communicate with cluster API Server
resource "aws_security_group_rule" "nodes_to_cluster_443" {{
  description              = "Allow nodes to communicate with cluster API Server"
  from_port                = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.eks_nodes.id
  source_security_group_id = aws_security_group.eks_cluster.id
  to_port                  = 443
  type                     = "ingress"
}}"""

    def _generate_eks_terraform_iam_roles(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate IAM roles for EKS cluster and node groups."""
        return f"""# EKS Cluster IAM Role
resource "aws_iam_role" "eks_cluster_role" {{
  name = "{app_name}-eks-cluster-role"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {{
          Service = "eks.amazonaws.com"
        }}
      }}
    ]
  }})
}}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {{
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster_role.name
}}

# CloudWatch Logs policy for EKS cluster
resource "aws_iam_role_policy" "eks_cluster_cloudwatch_logs" {{
  name = "${{var.cluster_name}}-eks-cluster-cloudwatch-logs"
  role = aws_iam_role.eks_cluster_role.id

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }}
    ]
  }})
}}

# EKS Node Group IAM Role
resource "aws_iam_role" "eks_node_group_role" {{
  name = "{app_name}-eks-node-group-role"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {{
          Service = "ec2.amazonaws.com"
        }}
      }}
    ]
  }})
}}

resource "aws_iam_role_policy_attachment" "eks_worker_node_policy" {{
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.eks_node_group_role.name
}}

resource "aws_iam_role_policy_attachment" "eks_cni_policy" {{
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.eks_node_group_role.name
}}

resource "aws_iam_role_policy_attachment" "eks_container_registry_policy" {{
  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.eks_node_group_role.name
}}

# AWS Load Balancer Controller IAM Role
resource "aws_iam_role" "aws_load_balancer_controller_role" {{
  name = "{app_name}-aws-load-balancer-controller-role"

  assume_role_policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Effect = "Allow"
        Principal = {{
          Federated = aws_iam_openid_connect_provider.eks_oidc_provider.arn
        }}
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {{
          StringEquals = {{
            "${{replace(aws_iam_openid_connect_provider.eks_oidc_provider.url, "https://", "")}}:sub": "system:serviceaccount:kube-system:aws-load-balancer-controller"
            "${{replace(aws_iam_openid_connect_provider.eks_oidc_provider.url, "https://", "")}}:aud": "sts.amazonaws.com"
          }}
        }}
      }}
    ]
  }})
}}

resource "aws_iam_role_policy" "aws_load_balancer_controller_policy" {{
  name = "{app_name}-aws-load-balancer-controller-policy"
  role = aws_iam_role.aws_load_balancer_controller_role.id

  policy = jsonencode({{
    Version = "2012-10-17"
    Statement = [
      {{
        Effect = "Allow"
        Action = [
          "iam:CreateServiceLinkedRole",
          "ec2:DescribeAccountAttributes",
          "ec2:DescribeAddresses",
          "ec2:DescribeAvailabilityZones",
          "ec2:DescribeInternetGateways",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeInstances",
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeTags",
          "ec2:GetCoipPoolUsage",
          "ec2:DescribeCoipPools",
          "elasticloadbalancing:DescribeLoadBalancers",
          "elasticloadbalancing:DescribeLoadBalancerAttributes",
          "elasticloadbalancing:DescribeListeners",
          "elasticloadbalancing:DescribeListenerCertificates",
          "elasticloadbalancing:DescribeSSLPolicies",
          "elasticloadbalancing:DescribeRules",
          "elasticloadbalancing:DescribeTargetGroups",
          "elasticloadbalancing:DescribeTargetGroupAttributes",
          "elasticloadbalancing:DescribeTargetHealth",
          "elasticloadbalancing:DescribeTags"
        ]
        Resource = "*"
      }},
      {{
        Effect = "Allow"
        Action = [
          "cognito-idp:DescribeUserPoolClient",
          "acm:ListCertificates",
          "acm:DescribeCertificate",
          "iam:ListServerCertificates",
          "iam:GetServerCertificate",
          "waf-regional:GetWebACL",
          "waf-regional:GetWebACLForResource",
          "waf-regional:AssociateWebACL",
          "waf-regional:DisassociateWebACL",
          "wafv2:GetWebACL",
          "wafv2:GetWebACLForResource",
          "wafv2:AssociateWebACL",
          "wafv2:DisassociateWebACL",
          "shield:DescribeProtection",
          "shield:GetSubscriptionState",
          "shield:DescribeSubscription",
          "shield:CreateProtection",
          "shield:DeleteProtection"
        ]
        Resource = "*"
      }},
      {{
        Effect = "Allow"
        Action = [
          "ec2:AuthorizeSecurityGroupIngress",
          "ec2:RevokeSecurityGroupIngress",
          "ec2:CreateSecurityGroup",
          "elasticloadbalancing:CreateListener",
          "elasticloadbalancing:DeleteListener",
          "elasticloadbalancing:CreateRule",
          "elasticloadbalancing:DeleteRule",
          "elasticloadbalancing:SetWebAcl",
          "elasticloadbalancing:ModifyListener",
          "elasticloadbalancing:AddListenerCertificates",
          "elasticloadbalancing:RemoveListenerCertificates",
          "elasticloadbalancing:ModifyRule"
        ]
        Resource = "*"
      }},
      {{
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:CreateLoadBalancer",
          "elasticloadbalancing:CreateTargetGroup"
        ]
        Resource = "*"
        Condition = {{
          StringEquals = {{
            "elasticloadbalancing:CreateLoadBalancer/internet-facing": "true"
          }}
        }}
      }},
      {{
        Effect = "Allow"
        Action = [
          "elasticloadbalancing:AddTags",
          "elasticloadbalancing:RemoveTags"
        ]
        Resource = [
          "arn:aws:elasticloadbalancing:*:*:targetgroup/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/net/*/*",
          "arn:aws:elasticloadbalancing:*:*:loadbalancer/app/*/*"
        ]
        Condition = {{
          StringEquals = {{
            "elasticloadbalancing:CreateAction": [
              "CreateTargetGroup",
              "CreateLoadBalancer"
            ]
          }}
        }}
      }}
    ]
  }})
}}"""

    def _generate_eks_terraform_cluster(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate EKS cluster configuration using default VPC."""
        return f"""# EKS Cluster (using default VPC subnets)
resource "aws_eks_cluster" "eks_cluster" {{
  name     = var.cluster_name
  role_arn = aws_iam_role.eks_cluster_role.arn
  version  = var.k8s_version

  vpc_config {{
    subnet_ids              = local.eks_subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = true
    security_group_ids      = [aws_security_group.eks_cluster.id]
  }}

  # Enable control plane logging for monitoring (conditional)
  enabled_cluster_log_types = var.enable_cluster_logging ? ["api", "audit", "authenticator", "controllerManager", "scheduler"] : []

  timeouts {{
    create = "30m"
    update = "30m"
    delete = "30m"
  }}

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_cloudwatch_log_group.eks_cluster
  ]

  tags = {{
    Name = "{app_name}-eks-cluster"
  }}
}}

# OIDC Provider for Service Accounts
data "tls_certificate" "eks_oidc_root_ca" {{
  url = aws_eks_cluster.eks_cluster.identity[0].oidc[0].issuer
}}

resource "aws_iam_openid_connect_provider" "eks_oidc_provider" {{
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc_root_ca.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.eks_cluster.identity[0].oidc[0].issuer

  tags = {{
    Name = "{app_name}-eks-oidc-provider"
  }}
}}"""

    def _generate_eks_terraform_node_group(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate EKS node group configuration using default VPC."""
        return f"""# EKS Node Group (using default VPC subnets)
resource "aws_eks_node_group" "eks_node_group" {{
  cluster_name    = aws_eks_cluster.eks_cluster.name
  node_group_name = "{app_name}-node-group"
  node_role_arn   = aws_iam_role.eks_node_group_role.arn
  subnet_ids      = local.eks_subnet_ids

  instance_types = var.node_instance_types
  capacity_type  = var.capacity_type

  scaling_config {{
    desired_size = var.desired_capacity
    max_size     = var.max_capacity
    min_size     = var.min_capacity
  }}

  update_config {{
    max_unavailable = 1
  }}

  timeouts {{
    create = "20m"
    update = "20m"
    delete = "20m"
  }}

  # Remote access disabled for security - SSH not needed for containerized workloads
  # If SSH access to worker nodes is required, add an EC2 key pair and uncomment:
  # remote_access {{
  #   ec2_ssh_key = "your-ec2-key-name"
  # }}

  depends_on = [
    aws_iam_role_policy_attachment.eks_worker_node_policy,
    aws_iam_role_policy_attachment.eks_cni_policy,
    aws_iam_role_policy_attachment.eks_container_registry_policy,
  ]

  tags = {{
    Name = "{app_name}-eks-node-group"
  }}
}}"""


    # NEW: Generate aws-auth ConfigMap to map Terraform and node roles into EKS RBAC
    def _generate_eks_aws_auth_config_map(self) -> str:
        """Generate Terraform code that injects IAM roles into aws-auth ConfigMap."""
        return """############################################
# aws-auth ConfigMap – RBAC bootstrap   #
############################################

data "aws_eks_cluster" "this" {
  name = aws_eks_cluster.eks_cluster.name
}

data "aws_eks_cluster_auth" "this" {
  name = data.aws_eks_cluster.this.name
}

locals {
  map_roles = [
    {
      rolearn  = data.aws_caller_identity.current.arn
      username = "terraform"
      groups   = ["system:masters"]
    },
    {
      rolearn  = aws_iam_role.eks_node_group_role.arn
      username = "system:node:{{EC2PrivateDNSName}}"
      groups   = ["system:bootstrappers", "system:nodes"]
    }
  ]
}

# Patch existing aws-auth ConfigMap instead of creating new one
# If this still fails, run: terraform import kubernetes_config_map_v1_data.aws_auth kube-system/aws-auth
resource "kubernetes_config_map_v1_data" "aws_auth" {
  metadata {
    name      = "aws-auth"
    namespace = "kube-system"
  }

  data = {
    mapRoles = yamlencode(local.map_roles)
  }

  force = true

  depends_on = [
    aws_eks_node_group.eks_node_group
  ]
}"""

    def _generate_eks_terraform_outputs(self, services_metadata: Dict[str, Dict[str, Any]], 
                                      app_name: str, config: Dict[str, Any]) -> str:
        """Generate Terraform outputs for EKS deployment."""
        # Add monitoring resources first
        monitoring_section = self._generate_eks_monitoring_resources(app_name, config)
        
        outputs = [
            monitoring_section,
            '',
            '############################################',
            '# Terraform Outputs                     #',
            '############################################',
            
            'output "cluster_id" {',
            '  description = "EKS cluster ID"',
            '  value       = aws_eks_cluster.eks_cluster.id',
            '}',
            '',
            'output "cluster_arn" {',
            '  description = "EKS cluster ARN"',
            '  value       = aws_eks_cluster.eks_cluster.arn',
            '}',
            '',
            'output "cluster_endpoint" {',
            '  description = "EKS cluster endpoint"',
            '  value       = aws_eks_cluster.eks_cluster.endpoint',
            '}',
            '',
            'output "cluster_security_group_id" {',
            '  description = "Security group ID attached to the EKS cluster"',
            '  value       = aws_eks_cluster.eks_cluster.vpc_config[0].cluster_security_group_id',
            '}',
            '',
            'output "cluster_certificate_authority_data" {',
            '  description = "Base64 encoded certificate data required to communicate with the cluster"',
            '  value       = aws_eks_cluster.eks_cluster.certificate_authority[0].data',
            '}',
            '',
            'output "node_group_arn" {',
            '  description = "EKS node group ARN"',
            '  value       = aws_eks_node_group.eks_node_group.arn',
            '}',
            '',
            'output "oidc_provider_arn" {',
            '  description = "The ARN of the OIDC Provider"',
            '  value       = aws_iam_openid_connect_provider.eks_oidc_provider.arn',
            '}',
            '',
            'output "kubectl_config_command" {',
            '  description = "Command to configure kubectl"',
            f'  value       = "aws eks update-kubeconfig --region ${{var.aws_region}} --name {app_name}"',
            '}',
            '',
            'output "cloudwatch_log_group" {',
            '  description = "CloudWatch log group for EKS cluster logs (if enabled)"',
            '  value       = aws_cloudwatch_log_group.eks_cluster.name',
            '}',
        ]
        
        # Add load balanced service outputs
        load_balanced_services = [name for name, metadata in services_metadata.items() 
                                if metadata.get('is_load_balanced', False)]
        
        if load_balanced_services:
            # Format list for Terraform HCL syntax with double quotes
            terraform_list = '[' + ', '.join(f'"{service}"' for service in load_balanced_services) + ']'
            outputs.extend([
                '',
                'output "load_balanced_services" {',
                '  description = "Services configured with load balancers"',
                f'  value       = {terraform_list}',
                '}',
            ])
        
        return '\n'.join(outputs)

    def _generate_eks_monitoring_resources(self, app_name: str, config: Dict[str, Any]) -> str:
        """Generate basic monitoring resources for EKS cluster."""
        return f"""############################################
# Basic Monitoring Resources              #
############################################

# CloudWatch Log Group for EKS Cluster Logs
resource "aws_cloudwatch_log_group" "eks_cluster" {{
  name              = "/aws/eks/{app_name}/cluster"
  retention_in_days = 14

  tags = {{
    Name = "{app_name}-eks-cluster-logs"
  }}
}}"""

    