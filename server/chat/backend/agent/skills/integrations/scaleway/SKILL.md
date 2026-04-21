---
name: scaleway
id: scaleway
description: "Scaleway cloud integration for managing instances, Kapsule Kubernetes clusters, object storage, and managed databases via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "Scaleway — instances, Kapsule Kubernetes, object storage, managed databases, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "1.0"
---

# Scaleway Integration

## Overview
Scaleway cloud provider for managing compute instances, Kapsule Kubernetes clusters, object storage, and managed databases.

## Instructions

### CLI COMMANDS (use cloud_exec with 'scaleway')

**CRITICAL: Always use cloud_exec('scaleway', 'command') for Scaleway commands, NOT terminal_exec!**
The cloud_exec tool has your Scaleway credentials configured.

**Discovery Commands:**
- List projects: `cloud_exec('scaleway', 'account project list')`
- List zones: `cloud_exec('scaleway', 'instance zone list')`
- List instance types: `cloud_exec('scaleway', 'instance server-type list')`
- List images: `cloud_exec('scaleway', 'instance image list')`

**Instance Management:**
- List instances: `cloud_exec('scaleway', 'instance server list')`
- Create instance: `cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm')`
- With zone: `cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm zone=fr-par-1')`
- Start/Stop/Reboot: `cloud_exec('scaleway', 'instance server start|stop|reboot <SERVER_ID>')`
- Delete: `cloud_exec('scaleway', 'instance server delete <SERVER_ID>')`
- SSH into server: `cloud_exec('scaleway', 'instance server ssh <SERVER_ID>')`

**Kubernetes (Kapsule):**
- List clusters: `cloud_exec('scaleway', 'k8s cluster list')`
- Create cluster: `cloud_exec('scaleway', 'k8s cluster create name=my-cluster version=1.28 cni=cilium')`
- Get kubeconfig: `cloud_exec('scaleway', 'k8s kubeconfig get <CLUSTER_ID>')`
- List pools: `cloud_exec('scaleway', 'k8s pool list cluster-id=<CLUSTER_ID>')`
- Create pool: `cloud_exec('scaleway', 'k8s pool create cluster-id=<CLUSTER_ID> name=worker-pool node-type=DEV1-M size=3')`

**Object Storage:**
- List buckets: `cloud_exec('scaleway', 'object bucket list')`
- Create bucket: `cloud_exec('scaleway', 'object bucket create name=my-bucket')`

**Databases:**
- List instances: `cloud_exec('scaleway', 'rdb instance list')`
- Create instance: `cloud_exec('scaleway', 'rdb instance create name=my-db engine=PostgreSQL-15 node-type=DB-DEV-S')`

### TERRAFORM FOR SCALEWAY
Use iac_tool - provider.tf is AUTO-GENERATED, just write the resource!
Scaleway Terraform provider: https://registry.terraform.io/providers/scaleway/scaleway/latest/docs

**INSTANCE EXAMPLE:**
```hcl
resource "scaleway_instance_server" "vm" {
  name  = "my-vm"
  type  = "DEV1-S"
  image = "ubuntu_jammy"
  # Optional: specify zone (defaults to fr-par-1)
  # zone = "fr-par-1"
}
```

**KUBERNETES (KAPSULE) CLUSTER:**
```hcl
resource "scaleway_k8s_cluster" "cluster" {
  name    = "my-cluster"
  version = "1.28"
  cni     = "cilium"
}

resource "scaleway_k8s_pool" "pool" {
  cluster_id = scaleway_k8s_cluster.cluster.id
  name       = "worker-pool"
  node_type  = "DEV1-M"
  size       = 3
}
```

**OBJECT STORAGE BUCKET:**
```hcl
resource "scaleway_object_bucket" "bucket" {
  name = "my-bucket"
}
```

**DATABASE (RDB) INSTANCE:**
```hcl
resource "scaleway_rdb_instance" "db" {
  name           = "my-database"
  engine         = "PostgreSQL-15"
  node_type      = "DB-DEV-S"
  is_ha_cluster  = false
  disable_backup = false
}
```

**Common Scaleway Terraform resources:**
- `scaleway_instance_server` - Virtual machines
- `scaleway_instance_ip` - Public IP addresses
- `scaleway_instance_security_group` - Firewall rules
- `scaleway_k8s_cluster` - Kubernetes clusters
- `scaleway_k8s_pool` - Kubernetes node pools
- `scaleway_object_bucket` - Object storage buckets
- `scaleway_rdb_instance` - Managed databases
- `scaleway_vpc_private_network` - Private networks
- `scaleway_lb` - Load balancers

DO NOT write terraform{} or provider{} blocks - they are auto-generated!

**When to use Terraform vs CLI:**
- **CLI (cloud_exec)**: Quick single resource ops, listing, inspection
- **Terraform (iac_tool)**: Complex deployments, multi-resource setups, user explicitly requests 'terraform' or 'IaC'

### CRITICAL RULES
- **ALWAYS** use `cloud_exec('scaleway', ...)` NOT `terminal_exec` for Scaleway commands!
- Scaleway CLI uses `key=value` syntax, NOT `--key value` for most parameters
- Common instance types: DEV1-S, DEV1-M, DEV1-L, GP1-XS, GP1-S, GP1-M
- Common images: ubuntu_jammy, ubuntu_focal, debian_bookworm, debian_bullseye
- Default region: fr-par, zones: fr-par-1, fr-par-2, fr-par-3
- Default SSH username for instances: `root`
