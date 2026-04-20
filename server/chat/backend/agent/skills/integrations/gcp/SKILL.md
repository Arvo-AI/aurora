---
name: gcp
id: gcp
description: "GCP integration — Compute Engine, GKE, Cloud SQL, Cloud Storage, Cloud Run, Cloud Logging, IAM via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "GCP — Compute Engine, GKE, Cloud SQL, Cloud Storage, Cloud Run, Logging, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "2.0"
---

# Google Cloud Platform Integration

## Overview
Full GCP access via `cloud_exec('gcp', 'COMMAND')`.
Available CLIs: `gcloud`, `gsutil`, `bq`, `kubectl`.
Authentication and project are auto-configured — never ask users for credentials.

## Project Setup
- Get current project: `cloud_exec('gcp', 'config get-value project')`
- Set project explicitly: `cloud_exec('gcp', 'config set project <PROJECT_ID>')`
- If user specifies a project, set it. Otherwise fetch the current one and reuse it everywhere.
- ALWAYS get the project ID before writing Terraform.

## CLI Reference

### Discovery
```python
cloud_exec('gcp', 'config get-value project')
cloud_exec('gcp', 'projects list')
cloud_exec('gcp', 'compute regions list --format="table(name,status)"')
cloud_exec('gcp', 'compute zones list --filter="region:(us-central1)"')
cloud_exec('gcp', 'services list --enabled --format="table(name)"')
cloud_exec('gcp', 'services enable <SERVICE>.googleapis.com')
```

### Compute Engine
```python
cloud_exec('gcp', 'compute instances list --format="table(name,zone,machineType,status,networkInterfaces[0].accessConfigs[0].natIP)"')
cloud_exec('gcp', 'compute instances describe <NAME> --zone=<ZONE>')
cloud_exec('gcp', 'compute instances create <NAME> --zone=<ZONE> --machine-type=e2-medium --image-family=debian-12 --image-project=debian-cloud --tags=http-server')
cloud_exec('gcp', 'compute instances start <NAME> --zone=<ZONE>')
cloud_exec('gcp', 'compute instances stop <NAME> --zone=<ZONE>')
cloud_exec('gcp', 'compute instances delete <NAME> --zone=<ZONE> --quiet')
# SSH with command execution:
cloud_exec('gcp', 'compute ssh <NAME> --zone=<ZONE> --command="uptime && free -m && df -h"')
# Serial port output (useful for boot issues):
cloud_exec('gcp', 'compute instances get-serial-port-output <NAME> --zone=<ZONE>')
# Instance metadata:
cloud_exec('gcp', 'compute instances describe <NAME> --zone=<ZONE> --format="json(metadata)"')
# List machine types:
cloud_exec('gcp', 'compute machine-types list --zones=<ZONE> --filter="name:(e2-*)" --format="table(name,guestCpus,memoryMb)"')
```

### GKE (Kubernetes)
```python
cloud_exec('gcp', 'container clusters list --format="table(name,location,currentMasterVersion,status,currentNodeCount)"')
cloud_exec('gcp', 'container clusters describe <CLUSTER> --region=<REGION>')
# MANDATORY before any kubectl:
cloud_exec('gcp', 'container clusters get-credentials <CLUSTER> --region=<REGION>')
# Then kubectl works:
cloud_exec('gcp', 'kubectl get pods -n <NS> -o wide')
cloud_exec('gcp', 'kubectl describe pod <POD> -n <NS>')
cloud_exec('gcp', 'kubectl logs <POD> -n <NS> --since=1h --tail=200')
cloud_exec('gcp', 'kubectl logs <POD> -n <NS> -c <CONTAINER> --previous')
cloud_exec('gcp', 'kubectl get events -n <NS> --sort-by=.lastTimestamp')
cloud_exec('gcp', 'kubectl top pods -n <NS>')
cloud_exec('gcp', 'kubectl top nodes')
cloud_exec('gcp', 'kubectl get hpa -n <NS>')
cloud_exec('gcp', 'kubectl get deployments -n <NS>')
cloud_exec('gcp', 'kubectl rollout history deployment/<DEPLOY> -n <NS>')
cloud_exec('gcp', 'kubectl get pvc -n <NS>')
cloud_exec('gcp', 'kubectl get svc -n <NS>')
cloud_exec('gcp', 'kubectl get ingress -n <NS>')
# Node pools:
cloud_exec('gcp', 'container node-pools list --cluster=<CLUSTER> --region=<REGION>')
cloud_exec('gcp', 'container node-pools describe <POOL> --cluster=<CLUSTER> --region=<REGION>')
# Resize:
cloud_exec('gcp', 'container clusters resize <CLUSTER> --node-pool=<POOL> --num-nodes=5 --region=<REGION> --quiet')
```

### Cloud Storage (gsutil)
```python
cloud_exec('gcp', 'gsutil ls')
cloud_exec('gcp', 'gsutil ls -la gs://<BUCKET>/')
cloud_exec('gcp', 'gsutil du -s gs://<BUCKET>')
cloud_exec('gcp', 'gsutil cp <LOCAL> gs://<BUCKET>/<KEY>')
cloud_exec('gcp', 'gsutil rm gs://<BUCKET>/<KEY>')
cloud_exec('gcp', 'gsutil iam get gs://<BUCKET>')
cloud_exec('gcp', 'gsutil versioning get gs://<BUCKET>')
cloud_exec('gcp', 'gsutil lifecycle get gs://<BUCKET>')
```

### Cloud SQL (Databases)
```python
cloud_exec('gcp', 'sql instances list --format="table(name,databaseVersion,settings.tier,state,region)"')
cloud_exec('gcp', 'sql instances describe <INSTANCE>')
cloud_exec('gcp', 'sql databases list --instance=<INSTANCE>')
cloud_exec('gcp', 'sql operations list --instance=<INSTANCE> --limit=10')
# Connect (via proxy):
cloud_exec('gcp', 'sql connect <INSTANCE> --user=<USER> --quiet')
# Backups:
cloud_exec('gcp', 'sql backups list --instance=<INSTANCE>')
# Patch/resize:
cloud_exec('gcp', 'sql instances patch <INSTANCE> --tier=db-custom-2-7680')
```

### Cloud Run (Serverless)
```python
cloud_exec('gcp', 'run services list --region=<REGION> --format="table(name,status.url,status.conditions.status)"')
cloud_exec('gcp', 'run services describe <NAME> --region=<REGION>')
cloud_exec('gcp', 'run deploy <NAME> --image=<IMAGE> --region=<REGION> --allow-unauthenticated --memory=512Mi --cpu=1')
cloud_exec('gcp', 'run revisions list --service=<NAME> --region=<REGION>')
# Logs:
cloud_exec('gcp', 'run services logs read <NAME> --region=<REGION> --limit=50')
```

### Cloud Logging (CRITICAL for investigations)
```python
# Recent errors across K8s:
cloud_exec('gcp', 'logging read "resource.type=k8s_container AND severity>=ERROR" --limit=50 --freshness=1h --format=json')
# Specific pod logs:
cloud_exec('gcp', 'logging read "resource.type=k8s_container AND resource.labels.pod_name=<POD> AND resource.labels.namespace_name=<NS>" --limit=100 --freshness=2h')
# Compute instance logs:
cloud_exec('gcp', 'logging read "resource.type=gce_instance AND resource.labels.instance_id=<ID>" --limit=50 --freshness=1h')
# Cloud SQL logs:
cloud_exec('gcp', 'logging read "resource.type=cloudsql_database AND resource.labels.database_id=<PROJECT>:<INSTANCE>" --limit=50 --freshness=1h')
# Cloud Run logs:
cloud_exec('gcp', 'logging read "resource.type=cloud_run_revision AND resource.labels.service_name=<SVC>" --limit=50 --freshness=1h')
# GKE audit logs:
cloud_exec('gcp', 'logging read "resource.type=k8s_cluster AND logName:\"cloudaudit.googleapis.com\"" --limit=30 --freshness=6h')
# Custom filter with text search:
cloud_exec('gcp', 'logging read "resource.type=k8s_container AND textPayload:\"OOMKilled\"" --limit=30 --freshness=24h')
```

Cloud Logging filter syntax reference:
- Severity: `severity>=ERROR`, `severity=WARNING`
- Text search: `textPayload:"keyword"`, `jsonPayload.message:"keyword"`
- Resource: `resource.type=k8s_container`, `resource.type=gce_instance`, `resource.type=cloudsql_database`
- Labels: `resource.labels.namespace_name="default"`, `resource.labels.pod_name="my-pod"`
- Time: Use `--freshness=1h` or `--freshness=6h` (simpler than timestamp ranges)
- Combine with AND/OR: `severity>=ERROR AND resource.labels.namespace_name="prod"`

### Cloud Monitoring
```python
cloud_exec('gcp', 'monitoring dashboards list --format="table(displayName,name)"')
cloud_exec('gcp', 'alpha monitoring policies list --format="table(displayName,enabled,conditions.displayName)"')
# Time series (raw metrics):
cloud_exec('gcp', 'monitoring time-series list --filter="metric.type=compute.googleapis.com/instance/cpu/utilization AND resource.labels.instance_id=<ID>" --interval-start-time=<ISO> --interval-end-time=<ISO>')
```

### IAM
```python
cloud_exec('gcp', 'projects get-iam-policy <PROJECT> --flatten="bindings[].members" --format="table(bindings.role,bindings.members)" --filter="bindings.members:<EMAIL>"')
cloud_exec('gcp', 'iam service-accounts list --format="table(email,displayName)"')
cloud_exec('gcp', 'iam service-accounts get-iam-policy <SA_EMAIL>')
cloud_exec('gcp', 'iam roles describe roles/<ROLE>')
# Test permissions:
cloud_exec('gcp', 'iam list-testable-permissions //cloudresourcemanager.googleapis.com/projects/<PROJECT>')
```

### Networking
```python
cloud_exec('gcp', 'compute networks list')
cloud_exec('gcp', 'compute networks subnets list --network=<NETWORK>')
cloud_exec('gcp', 'compute firewall-rules list --format="table(name,network,direction,allowed[].map().firewall_rule().ip_protocol,sourceRanges)"')
cloud_exec('gcp', 'compute firewall-rules describe <RULE>')
cloud_exec('gcp', 'compute forwarding-rules list')
cloud_exec('gcp', 'compute addresses list')
# Health checks:
cloud_exec('gcp', 'compute health-checks list')
cloud_exec('gcp', 'compute backend-services get-health <BACKEND> --global')
```

### BigQuery (bq)
```python
cloud_exec('gcp', 'bq ls')
cloud_exec('gcp', 'bq ls <DATASET>')
cloud_exec('gcp', 'bq show <DATASET>.<TABLE>')
cloud_exec('gcp', 'bq query --use_legacy_sql=false "SELECT * FROM `<PROJECT>.<DATASET>.<TABLE>` LIMIT 10"')
cloud_exec('gcp', 'bq show --job <JOB_ID>')
```

### Other Services
```python
# Cloud Functions:
cloud_exec('gcp', 'functions list --format="table(name,status,runtime)"')
cloud_exec('gcp', 'functions describe <NAME> --region=<REGION>')
cloud_exec('gcp', 'functions logs read <NAME> --region=<REGION> --limit=50')
# Pub/Sub:
cloud_exec('gcp', 'pubsub topics list')
cloud_exec('gcp', 'pubsub subscriptions list')
# Secret Manager:
cloud_exec('gcp', 'secrets list')
cloud_exec('gcp', 'secrets versions access latest --secret=<NAME>')
# Artifact Registry / Container Registry:
cloud_exec('gcp', 'artifacts repositories list --location=<REGION>')
cloud_exec('gcp', 'artifacts docker images list <REGION>-docker.pkg.dev/<PROJECT>/<REPO>')
```

## RCA / Investigation Workflow

When investigating a GCP incident:

1. **Get project context**: `config get-value project`
2. **Get cluster credentials** (if GKE): `container clusters get-credentials <CLUSTER> --region=<REGION>`
3. **Check resource state**: `container clusters list`, `compute instances list`, `sql instances list`
4. **Check pods/containers** (if K8s): `kubectl get pods -o wide`, `kubectl describe pod`, `kubectl logs`
5. **Check Kubernetes events**: `kubectl get events --sort-by=.lastTimestamp`
6. **Query Cloud Logging**: Use filter syntax above — check severity>=ERROR, search for OOMKilled, CrashLoopBackOff
7. **Check metrics**: `kubectl top pods`, Cloud Monitoring time-series for CPU/memory
8. **Check alert policies**: `alpha monitoring policies list`
9. **Check recent deployments**: `kubectl rollout history`, GKE audit logs
10. **Check networking**: Firewall rules, health checks, backend service health
11. **Check node health**: `kubectl describe node`, `kubectl top nodes`
12. **Compare healthy vs unhealthy**: Side-by-side pod metrics and logs

## Terraform

Use `iac_tool` — provider.tf is AUTO-GENERATED. Never write terraform{} or provider{} blocks.

**PREREQUISITE:** Always get project ID first:
```python
cloud_exec('gcp', 'config get-value project')
```

### Compute Instance
```hcl
resource "google_service_account" "default" {
  account_id   = "my-custom-sa"
  display_name = "Custom SA for VM"
}

resource "google_compute_instance" "vm" {
  name         = "my-instance"
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

  service_account {
    email  = google_service_account.default.email
    scopes = ["cloud-platform"]
  }
}
```

### GKE Cluster
```hcl
resource "google_container_cluster" "primary" {
  name     = "my-cluster"
  location = "us-central1"

  initial_node_count       = 1
  remove_default_node_pool = true
}

resource "google_container_node_pool" "primary_nodes" {
  name       = "primary-pool"
  location   = "us-central1"
  cluster    = google_container_cluster.primary.name
  node_count = 3

  node_config {
    machine_type = "e2-medium"
    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  }

  autoscaling {
    min_node_count = 1
    max_node_count = 5
  }
}
```

### Cloud SQL
```hcl
resource "google_sql_database_instance" "db" {
  name             = "my-db"
  database_version = "POSTGRES_15"
  region           = "us-central1"

  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"

    backup_configuration {
      enabled = true
    }
  }

  deletion_protection = false
}

resource "google_sql_database" "database" {
  name     = "mydb"
  instance = google_sql_database_instance.db.name
}
```

### Cloud Storage
```hcl
resource "google_storage_bucket" "bucket" {
  name     = "my-unique-bucket-name"
  location = "US"

  versioning {
    enabled = true
  }

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
}
```

### VPC Network
```hcl
resource "google_compute_network" "vpc" {
  name                    = "my-vpc"
  auto_create_subnetworks = false
}

resource "google_compute_subnetwork" "subnet" {
  name          = "my-subnet"
  ip_cidr_range = "10.0.0.0/24"
  region        = "us-central1"
  network       = google_compute_network.vpc.id
}

resource "google_compute_firewall" "allow_ssh" {
  name    = "allow-ssh"
  network = google_compute_network.vpc.name

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  source_ranges = ["0.0.0.0/0"]
  target_tags   = ["ssh-enabled"]
}
```

### Common Terraform resources
`google_compute_instance`, `google_compute_firewall`, `google_compute_network`, `google_compute_subnetwork`,
`google_container_cluster`, `google_container_node_pool`,
`google_storage_bucket`, `google_sql_database_instance`, `google_sql_database`,
`google_cloud_run_v2_service`, `google_cloudfunctions_function`,
`google_compute_global_forwarding_rule`, `google_compute_health_check`,
`google_project_iam_member`, `google_service_account`,
`google_pubsub_topic`, `google_pubsub_subscription`,
`google_artifact_registry_repository`, `google_secret_manager_secret`

## Error Recovery

1. **API not enabled** → `cloud_exec('gcp', 'services enable <SERVICE>.googleapis.com')` — common: container.googleapis.com, sqladmin.googleapis.com, run.googleapis.com, cloudfunctions.googleapis.com
2. **Permission denied** → Check IAM: `projects get-iam-policy <PROJECT>`, verify service account roles
3. **Beta feature** → Prefix with beta: `cloud_exec('gcp', 'beta <COMMAND>')`
4. **CLI syntax** → `cloud_exec('gcp', '<CATEGORY> --help')` for subcommand reference
5. **Terraform failure** → Verify with CLI, then fix manifest

### Context7 lookup on failure
For Terraform errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/hashicorp/terraform-provider-google', topic='google_container_cluster')`
For CLI errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/websites/cloud_google_sdk', topic='container clusters get-credentials')`

## Region Mapping
- US (default): us-central1-b
- Canada: northamerica-northeast1-a, northamerica-northeast2-a
- EU/Belgium: europe-west1-a
- UK/London: europe-west2-a
- Singapore/SEA: asia-southeast1-a
- Tokyo/Japan: asia-northeast1-a
- If user says "NOT US", prefer Canada (northamerica-northeast1-a)
