---
name: ovh
id: ovh
description: "OVHcloud infrastructure integration for managing instances, Kubernetes clusters, networks, and object storage via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "OVHcloud — instances, MKS Kubernetes, networks, S3 storage, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "1.0"
---

# OVHcloud Integration

## Overview
OVHcloud infrastructure provider for managing compute instances, Managed Kubernetes Service (MKS), networks, and object storage.

## Instructions

### CLI COMMANDS (use cloud_exec with 'ovh')

**Discovery Commands:**
- List projects: `cloud_exec('ovh', 'cloud project list --json')`
- List regions: `cloud_exec('ovh', 'cloud region list --cloud-project <PROJECT_ID> --json')`
- List flavors: `cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <PROJECT_ID> --region <REGION> --json')`
- List images: `cloud_exec('ovh', 'cloud reference list-images --cloud-project <PROJECT_ID> --region <REGION> --json')`

**Instance Management:**
- List instances: `cloud_exec('ovh', 'cloud instance list --cloud-project <PROJECT_ID> --json')`
- Create instance: `cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --network.public --wait --json')`
- With SSH key: `cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --ssh-key.create.name my-key --ssh-key.create.public-key "<PUBKEY>" --network.public --wait --json')`
- Stop/Start/Reboot: `cloud_exec('ovh', 'cloud instance stop|start|reboot <INSTANCE_ID> --cloud-project <PROJECT_ID>')`
- Delete: `cloud_exec('ovh', 'cloud instance delete <INSTANCE_ID> --cloud-project <PROJECT_ID>')`

**Kubernetes (MKS):**
- List clusters: `cloud_exec('ovh', 'cloud kube list --cloud-project <PROJECT_ID> --json')`
- Create cluster: `cloud_exec('ovh', 'cloud kube create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --version 1.28')`
- Get kubeconfig: `cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>')`
- Create nodepool: `cloud_exec('ovh', 'cloud kube nodepool create <CLUSTER_ID> --cloud-project <PROJECT_ID> --name worker-pool --flavor b2-7 --desired-nodes 3 --autoscale true')`

**KUBECTL WORKFLOW (for OVH clusters):**
1. Save kubeconfig to file: `cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>', output_file='/tmp/kubeconfig.yaml')`
2. Run kubectl: `terminal_exec('kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -A')`
3. CRITICAL: Use output_file parameter to save kubeconfig directly - avoids shell escaping issues
4. Do NOT try to embed kubeconfig YAML in echo commands - it will break due to special characters

**Networks:**
- List networks: `cloud_exec('ovh', 'cloud network list --cloud-project <PROJECT_ID> --json')`
- Create network: `cloud_exec('ovh', 'cloud network create --cloud-project <PROJECT_ID> --name <NAME> --vlan-id <ID> --regions <REGION>')`

**Object Storage (S3):**
- List S3 users: `cloud_exec('ovh', 'cloud storage-s3 list --cloud-project <PROJECT_ID> --json')`
- Create S3 user: `cloud_exec('ovh', 'cloud storage-s3 create --cloud-project <PROJECT_ID> --region <REGION>')`

### TERRAFORM FOR OVH
Use iac_tool - provider.tf is AUTO-GENERATED, just write the resource!

**INSTANCE EXAMPLE (MUST use nested blocks, NOT flat attributes):**
```hcl
resource "ovh_cloud_project_instance" "vm" {
  service_name   = "<PROJECT_ID>"
  region         = "US-EAST-VA-1"
  billing_period = "hourly"
  name           = "my-vm"
  flavor {
    flavor_id = "<FLAVOR_UUID>"
  }
  boot_from {
    image_id = "<IMAGE_UUID>"
  }
  network {
    public = true
  }
  # SSH key options (use ONE):
  # Option 1: Reference existing SSH key by name
  ssh_key {
    name = "my-ssh-key"  # Must exist in OVH first
  }
  # Option 2: Create new SSH key inline
  # ssh_key_create {
  #   name = "my-new-key"
  #   public_key = "ssh-rsa AAAA..."
  # }
}
```

**SSH KEY IMPORTANT:** Use `ssh_key` to reference existing key, or `ssh_key_create` to create new one inline. If unsure, query Context7 with topic='ovh_cloud_project_instance ssh_key'.

**Other resources:** `ovh_cloud_project_kube`, `ovh_cloud_project_kube_nodepool`, `ovh_cloud_project_database`

DO NOT write terraform{} or provider{} blocks - they are auto-generated!

### CRITICAL RULES
- Use **UUID** from 'id' field for flavor/image, NOT names!
- Use `--cloud-project <ID>` NOT `--project-id`
- Region is POSITIONAL in create commands: `cloud instance create <REGION> ...`
- Use `kube` NOT `kubernetes` subcommand
- Use `--network.public` for public IP (not `--network <ID>`)

### DYNAMIC/RUNTIME DATA (versions, flavors, images)
Context7 docs do NOT contain runtime data. For dynamic values, use CLI:
- **K8s versions**: For Terraform, omit `version` to use latest stable, or use `1.31`, `1.32` (check `cloud kube create --help` for valid versions)
- **Flavors**: `cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <ID> --region <REGION> --json')`
- **Images**: `cloud_exec('ovh', 'cloud reference list-images --cloud-project <ID> --region <REGION> --json')`
- **Regions**: `cloud_exec('ovh', 'cloud region list --cloud-project <ID> --json')`
- Always query flavors/images/regions BEFORE creating resources.

### MANDATORY: ON ANY OVH ERROR OR FAILURE
**YOU MUST** use Context7 MCP to look up correct syntax BEFORE retrying. Choose the RIGHT library:

**If `iac_tool` (Terraform) fails** -- Use TERRAFORM docs:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/terraform-provider-ovh', topic='ovh_cloud_project_instance')`
Topic should be the **resource type** (e.g., 'ovh_cloud_project_instance', 'ovh_cloud_project_kube', 'ssh_key block')

**If `cloud_exec` (CLI) fails** -- Use CLI docs:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/ovhcloud-cli', topic='cloud instance create')`
Topic should be the **CLI command** (e.g., 'cloud instance create', 'cloud kube list')

Do NOT mix them up! Terraform errors need Terraform docs, CLI errors need CLI docs.
