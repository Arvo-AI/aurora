"""Provider configuration templates used by the IaC tooling."""

from __future__ import annotations


def generate_gcp_provider_config(project_id: str) -> str:
    """Generate minimal Terraform provider configuration for GCP."""
    return f'''terraform {{
  required_providers {{
    google = {{
      source  = "hashicorp/google"
      version = "~> 4.0"
    }}
  }}
  required_version = ">= 1.0"
}}

provider "google" {{
  project = "{project_id}"
  region  = "us-central1"
}}

variable "project_id" {{
  description = "The GCP project ID"
  type        = string
  default     = "{project_id}"
}}
'''


def generate_aws_provider_config(region: str = "us-east-1") -> str:
    """Generate minimal Terraform provider configuration for AWS."""
    return f'''terraform {{
  required_providers {{
    aws = {{
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }}
  }}
  required_version = ">= 1.0"
}}

provider "aws" {{
  region = "{region}"
}}

variable "region" {{
  description = "The AWS region"
  type        = string
  default     = "{region}"
}}

# Get the default VPC
data "aws_vpc" "default" {{
  default = true
}}

# Get the default subnet
data "aws_subnet" "default" {{
  vpc_id            = data.aws_vpc.default.id
  availability_zone = "{region}a"
  default_for_az    = true
}}
'''


def generate_azure_provider_config(subscription_id: str) -> str:
    """Generate minimal Terraform provider configuration for Azure."""
    return f'''terraform {{
  required_providers {{
    azurerm = {{
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }}
    random = {{
      source  = "hashicorp/random"
      version = "~> 3.0"
    }}
  }}
}}

provider "azurerm" {{
  features {{}}
  subscription_id = "{subscription_id}"
}}
'''


def generate_ovh_provider_config(service_name: str) -> str:
    """Generate minimal Terraform provider configuration for OVH.
    
    Uses OAuth2 access token authentication via OVH_ACCESS_TOKEN env var.
    Endpoint is read from OVH_ENDPOINT env var (set by setup_terraform_environment).
    """
    return f'''terraform {{
  required_providers {{
    ovh = {{
      source  = "ovh/ovh"
      version = ">= 0.36.0"
    }}
  }}
  required_version = ">= 1.0"
}}

# OVH Provider - uses OVH_ENDPOINT and OVH_ACCESS_TOKEN from environment
# Do NOT specify endpoint/access_token here - they come from env vars
provider "ovh" {{
}}

variable "service_name" {{
  description = "OVH Public Cloud project ID"
  type        = string
  default     = "{service_name}"
}}
'''


def generate_scaleway_provider_config(project_id: str, region: str = "fr-par") -> str:
    """Generate minimal Terraform provider configuration for Scaleway.
    
    Uses API key authentication via SCW_ACCESS_KEY and SCW_SECRET_KEY env vars.
    See: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs
    
    Args:
        project_id: Scaleway project ID
        region: Scaleway region (default: fr-par)
    """
    return f'''terraform {{
  required_providers {{
    scaleway = {{
      source  = "scaleway/scaleway"
      version = ">= 2.0"
    }}
  }}
  required_version = ">= 1.0"
}}

# Scaleway Provider - uses SCW_ACCESS_KEY and SCW_SECRET_KEY from environment
# See: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs
provider "scaleway" {{
  region     = var.region
  zone       = var.zone
  project_id = var.project_id
}}

variable "project_id" {{
  description = "The Scaleway project ID"
  type        = string
  default     = "{project_id}"
}}

variable "region" {{
  description = "The Scaleway region (e.g., fr-par, nl-ams, pl-waw)"
  type        = string
  default     = "{region}"
}}

variable "zone" {{
  description = "The Scaleway zone (e.g., fr-par-1, nl-ams-1)"
  type        = string
  default     = "{region}-1"
}}
'''

