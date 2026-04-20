---
name: gcp
id: gcp
description: "Google Cloud Platform integration for managing Compute Engine, GKE, Cloud SQL, Cloud Storage, Cloud Run, and other services via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "GCP — Compute Engine, GKE, Cloud SQL, Cloud Storage, Cloud Run, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "1.0"
---

# Google Cloud Platform Integration

## Overview
GCP cloud provider for managing compute, Kubernetes, databases, storage, serverless, networking, and monitoring.

## Instructions

### CLI COMMANDS (use cloud_exec with 'gcp')

**CRITICAL: Always use cloud_exec('gcp', 'COMMAND') — NOT terminal_exec!**
Authentication and project setup are auto-configured. The `gcloud` CLI is available.
Additional CLIs: `gsutil`, `bq`, `kubectl`.

**PROJECT SETUP:**
- Get current project: `cloud_exec('gcp', 'config get-value project')`
- Set project: `cloud_exec('gcp', 'config set project <PROJECT_ID>')`
- If the user specifies a project, set it. Otherwise fetch the current one and reuse it.

**Discovery Commands:**
- List projects: `cloud_exec('gcp', 'projects list')`
- List regions: `cloud_exec('gcp', 'compute regions list')`
- List zones: `cloud_exec('gcp', 'compute zones list --filter="region:(us-central1)"')`
- List services: `cloud_exec('gcp', 'services list --enabled')`
- Enable service: `cloud_exec('gcp', 'services enable <SERVICE_API>')`

**Compute Engine:**
- List instances: `cloud_exec('gcp', 'compute instances list')`
- Create instance: `cloud_exec('gcp', 'compute instances create <NAME> --zone=<ZONE> --machine-type=e2-medium --image-family=debian-12 --image-project=debian-cloud')`
- Start/stop: `cloud_exec('gcp', 'compute instances start|stop <NAME> --zone=<ZONE>')`
- Describe: `cloud_exec('gcp', 'compute instances describe <NAME> --zone=<ZONE>')`
- SSH: `cloud_exec('gcp', 'compute ssh <NAME> --zone=<ZONE> --command="<CMD>"')`

**GKE (Kubernetes):**
- List clusters: `cloud_exec('gcp', 'container clusters list')`
- Describe cluster: `cloud_exec('gcp', 'container clusters describe <NAME> --region=<REGION>')`
- Get credentials: `cloud_exec('gcp', 'container clusters get-credentials <NAME> --region=<REGION>')`
- Then kubectl: `cloud_exec('gcp', 'kubectl get pods -n <NAMESPACE> -o wide')`
- Node pools: `cloud_exec('gcp', 'container node-pools list --cluster=<NAME> --region=<REGION>')`

**Cloud Storage (gsutil):**
- List buckets: `cloud_exec('gcp', 'gsutil ls')`
- List objects: `cloud_exec('gcp', 'gsutil ls gs://<BUCKET>/')`
- Copy: `cloud_exec('gcp', 'gsutil cp <SRC> <DST>')`
- Bucket info: `cloud_exec('gcp', 'gsutil du -s gs://<BUCKET>')`

**Cloud SQL (Databases):**
- List instances: `cloud_exec('gcp', 'sql instances list')`
- Describe: `cloud_exec('gcp', 'sql instances describe <NAME>')`
- List databases: `cloud_exec('gcp', 'sql databases list --instance=<NAME>')`

**Cloud Run (Serverless):**
- List services: `cloud_exec('gcp', 'run services list --region=<REGION>')`
- Describe: `cloud_exec('gcp', 'run services describe <NAME> --region=<REGION>')`
- Deploy: `cloud_exec('gcp', 'run deploy <NAME> --image=<IMAGE> --region=<REGION> --allow-unauthenticated')`
- View logs: `cloud_exec('gcp', 'run services logs read <NAME> --region=<REGION> --limit=50')`

**Cloud Logging & Monitoring:**
- Read logs: `cloud_exec('gcp', 'logging read "resource.type=k8s_container AND severity>=ERROR" --limit=50 --freshness=1h')`
- Specific resource: `cloud_exec('gcp', 'logging read "resource.type=gce_instance AND resource.labels.instance_id=<ID>" --limit=50')`
- List metrics: `cloud_exec('gcp', 'monitoring dashboards list')`
- Alert policies: `cloud_exec('gcp', 'alpha monitoring policies list')`

**IAM:**
- List bindings: `cloud_exec('gcp', 'projects get-iam-policy <PROJECT_ID> --flatten="bindings[].members" --format="table(bindings.role,bindings.members)"')`
- Service accounts: `cloud_exec('gcp', 'iam service-accounts list')`

**Networking:**
- List VPCs: `cloud_exec('gcp', 'compute networks list')`
- List subnets: `cloud_exec('gcp', 'compute networks subnets list')`
- Firewall rules: `cloud_exec('gcp', 'compute firewall-rules list')`
- Load balancers: `cloud_exec('gcp', 'compute forwarding-rules list')`

**BigQuery (bq):**
- List datasets: `cloud_exec('gcp', 'bq ls')`
- Query: `cloud_exec('gcp', 'bq query --use_legacy_sql=false "SELECT * FROM dataset.table LIMIT 10"')`

### TERRAFORM FOR GCP
Use iac_tool — provider.tf is AUTO-GENERATED, just write the resource!

**PREREQUISITE:** Get project ID first:
`cloud_exec('gcp', 'config get-value project')`

**COMPUTE INSTANCE EXAMPLE:**
```hcl
resource "google_compute_instance" "vm" {
  name         = "my-vm"
  machine_type = "e2-medium"
  zone         = "us-central1-b"

  boot_disk {
    initialize_params {
      image = "debian-cloud/debian-12"
    }
  }

  network_interface {
    network = "default"
    access_config {}
  }
}
```

**GKE CLUSTER:**
```hcl
resource "google_container_cluster" "cluster" {
  name     = "my-cluster"
  location = "us-central1"

  initial_node_count = 3

  node_config {
    machine_type = "e2-medium"
  }
}
```

**CLOUD SQL:**
```hcl
resource "google_sql_database_instance" "db" {
  name             = "my-db"
  database_version = "POSTGRES_15"
  region           = "us-central1"

  settings {
    tier = "db-f1-micro"
  }

  deletion_protection = false
}
```

**CLOUD STORAGE BUCKET:**
```hcl
resource "google_storage_bucket" "bucket" {
  name     = "my-bucket-unique-name"
  location = "US"
}
```

**Common GCP Terraform resources:**
- `google_compute_instance` — Virtual machines
- `google_compute_firewall` — Firewall rules
- `google_container_cluster`, `google_container_node_pool` — GKE
- `google_storage_bucket` — Cloud Storage
- `google_sql_database_instance` — Cloud SQL
- `google_cloud_run_v2_service` — Cloud Run
- `google_compute_network`, `google_compute_subnetwork` — VPC
- `google_compute_global_forwarding_rule` — Load balancers
- `google_project_iam_member` — IAM bindings

DO NOT write terraform{} or provider{} blocks — they are auto-generated!

### CRITICAL RULES
- Always get the project ID before writing Terraform or running commands
- Use `--region` for regional resources, `--zone` for zonal resources
- GKE: always run `get-credentials` before kubectl commands
- Enable required APIs before creating resources: `services enable <API>`
- Default zone: us-central1-b unless user specifies otherwise
- For beta features, use `cloud_exec('gcp', 'beta <COMMAND>')`

### ON ANY GCP ERROR
1. API not enabled → `cloud_exec('gcp', 'services enable <SERVICE>.googleapis.com')`
2. Permission denied → Check IAM: `cloud_exec('gcp', 'projects get-iam-policy <PROJECT>')`
3. CLI syntax error → `cloud_exec('gcp', '<CATEGORY> --help')`
4. Try beta: `cloud_exec('gcp', 'beta <COMMAND> --help')`
5. Terraform failure → Verify resources with CLI, then fix the manifest
