---
name: ovh
id: ovh
description: "OVHcloud integration — instances, MKS Kubernetes, managed databases, object storage, private networks via CLI"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
index: "OVHcloud — instances, MKS Kubernetes, managed databases, object storage, private networks"
rca_priority: 10
allowed-tools: cloud_exec
metadata:
  author: aurora
  version: "2.0"
---

# OVHcloud Integration

## Overview
Full OVHcloud Public Cloud access via `cloud_exec('ovh', 'COMMAND')`.
Available CLI: `ovhcloud` (aliased through cloud_exec).
Authentication is automatic — never ask users for credentials.

## Project Context (CRITICAL)
Most OVH commands require `--cloud-project <PROJECT_ID>`:
```python
cloud_exec('ovh', 'cloud project list --json')
```
Get the project ID first, then use it on every subsequent command.

## CLI Reference

### Discovery
```python
cloud_exec('ovh', 'cloud project list --json')
cloud_exec('ovh', 'cloud region list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud reference list-flavors --cloud-project <PROJECT_ID> --region <REGION> --json')
cloud_exec('ovh', 'cloud reference list-images --cloud-project <PROJECT_ID> --region <REGION> --json')
```

### Compute Instances
```python
# List all instances:
cloud_exec('ovh', 'cloud instance list --cloud-project <PROJECT_ID> --json')
# Get instance details:
cloud_exec('ovh', 'cloud instance get <INSTANCE_ID> --cloud-project <PROJECT_ID> --json')
# Create instance (region is POSITIONAL):
cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --network.public --wait --json')
# Create with SSH key:
cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --ssh-key.create.name my-key --ssh-key.create.public-key "<PUBKEY>" --network.public --wait --json')
# Create on private network:
cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --network.private <NETWORK_ID> --wait --json')
# With auto-backup:
cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --auto-backup.cron "0 1 * * *" --network.public --wait --json')
# With user data:
cloud_exec('ovh', 'cloud instance create <REGION> --cloud-project <PROJECT_ID> --name <NAME> --boot-from.image <IMAGE_UUID> --flavor <FLAVOR_UUID> --user-data @/path/to/cloud-init.yaml --network.public --wait --json')
# Start/Stop/Reboot:
cloud_exec('ovh', 'cloud instance start <INSTANCE_ID> --cloud-project <PROJECT_ID>')
cloud_exec('ovh', 'cloud instance stop <INSTANCE_ID> --cloud-project <PROJECT_ID>')
cloud_exec('ovh', 'cloud instance reboot <INSTANCE_ID> --cloud-project <PROJECT_ID>')
# Soft vs hard reboot:
cloud_exec('ovh', 'cloud instance reboot <INSTANCE_ID> --cloud-project <PROJECT_ID> --type soft')
cloud_exec('ovh', 'cloud instance reboot <INSTANCE_ID> --cloud-project <PROJECT_ID> --type hard')
# Resize:
cloud_exec('ovh', 'cloud instance resize <INSTANCE_ID> --cloud-project <PROJECT_ID> --flavor <NEW_FLAVOR_UUID>')
# Rebuild (reinstall OS):
cloud_exec('ovh', 'cloud instance rebuild <INSTANCE_ID> --cloud-project <PROJECT_ID> --image <IMAGE_UUID>')
# Delete:
cloud_exec('ovh', 'cloud instance delete <INSTANCE_ID> --cloud-project <PROJECT_ID>')
# Console URL (for debugging boot issues):
cloud_exec('ovh', 'cloud instance vnc <INSTANCE_ID> --cloud-project <PROJECT_ID> --json')
# Instance logs (serial console output):
cloud_exec('ovh', 'cloud instance logs <INSTANCE_ID> --cloud-project <PROJECT_ID>')
```

Common flavors (query first — UUIDs are region-specific):
- General: `b2-7` (2 vCPU, 7GB), `b2-15` (4 vCPU, 15GB), `b2-30` (8 vCPU, 30GB), `b2-60` (16 vCPU, 60GB)
- CPU: `c2-7`, `c2-15`, `c2-30`, `c2-60`
- RAM: `r2-15`, `r2-30`, `r2-60`, `r2-120`
- GPU: `t1-45`, `t1-90`, `t2-45`, `t2-90`

### SSH Keys
```python
cloud_exec('ovh', 'cloud ssh-key list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud ssh-key create --cloud-project <PROJECT_ID> --name <NAME> --public-key "<PUBKEY>"')
cloud_exec('ovh', 'cloud ssh-key get <KEY_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud ssh-key delete <KEY_ID> --cloud-project <PROJECT_ID>')
```

### Managed Kubernetes Service (MKS)
```python
# List clusters:
cloud_exec('ovh', 'cloud kube list --cloud-project <PROJECT_ID> --json')
# Get cluster details:
cloud_exec('ovh', 'cloud kube get <CLUSTER_ID> --cloud-project <PROJECT_ID> --json')
# Create cluster:
cloud_exec('ovh', 'cloud kube create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --version 1.31')
# Get kubeconfig (CRITICAL: use output_file to avoid shell escaping):
cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>', output_file='/tmp/kubeconfig.yaml')
# Node pools:
cloud_exec('ovh', 'cloud kube nodepool list <CLUSTER_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud kube nodepool get <CLUSTER_ID> <NODEPOOL_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud kube nodepool create <CLUSTER_ID> --cloud-project <PROJECT_ID> --name worker-pool --flavor b2-7 --desired-nodes 3 --autoscale true')
# With min/max autoscaling:
cloud_exec('ovh', 'cloud kube nodepool create <CLUSTER_ID> --cloud-project <PROJECT_ID> --name worker-pool --flavor b2-7 --desired-nodes 3 --min-nodes 1 --max-nodes 10 --autoscale true')
cloud_exec('ovh', 'cloud kube nodepool scale <CLUSTER_ID> <NODEPOOL_ID> --cloud-project <PROJECT_ID> --desired-nodes 5')
cloud_exec('ovh', 'cloud kube nodepool delete <CLUSTER_ID> <NODEPOOL_ID> --cloud-project <PROJECT_ID>')
# Update cluster version:
cloud_exec('ovh', 'cloud kube update <CLUSTER_ID> --cloud-project <PROJECT_ID> --version 1.31')
# Reset kubeconfig:
cloud_exec('ovh', 'cloud kube kubeconfig reset <CLUSTER_ID> --cloud-project <PROJECT_ID>')
```

**KUBECTL WORKFLOW (for OVH MKS clusters):**
1. Save kubeconfig to file: `cloud_exec('ovh', 'cloud kube kubeconfig generate <CLUSTER_ID> --cloud-project <PROJECT_ID>', output_file='/tmp/kubeconfig.yaml')`
2. Run kubectl: `cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -A')`
3. CRITICAL: Use output_file parameter to save kubeconfig — avoids shell escaping issues with YAML
4. Do NOT try to embed kubeconfig YAML in echo commands — it will break

```python
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get pods -n <NS> -o wide')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml describe pod <POD> -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml logs <POD> -n <NS> --since=1h --tail=200')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml logs <POD> -n <NS> -c <CONTAINER> --previous')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get events -n <NS> --sort-by=.lastTimestamp')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml top pods -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml top nodes')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get hpa -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get deployments -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml rollout history deployment/<DEPLOY> -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get pvc -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get svc -n <NS>')
cloud_exec('ovh', 'kubectl --kubeconfig=/tmp/kubeconfig.yaml get ingress -n <NS>')
```

### Managed Databases
```python
# List database services (all engines):
cloud_exec('ovh', 'cloud database list --cloud-project <PROJECT_ID> --json')
# Supported engines: postgresql, mysql, mongodb, redis, kafka, cassandra, opensearch
# Create database:
cloud_exec('ovh', 'cloud database create <ENGINE> --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --version <VER> --plan <PLAN> --flavor <FLAVOR> --json')
# Example PostgreSQL:
cloud_exec('ovh', 'cloud database create postgresql --cloud-project <PROJECT_ID> --name my-pg --region GRA --version 15 --plan essential --flavor db1-4 --json')
# Example Kafka:
cloud_exec('ovh', 'cloud database create kafka --cloud-project <PROJECT_ID> --name my-kafka --region GRA --version 3.4 --plan business --flavor db1-7 --json')
# Get database details:
cloud_exec('ovh', 'cloud database get <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
# List nodes:
cloud_exec('ovh', 'cloud database node list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
# Users:
cloud_exec('ovh', 'cloud database user list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud database user create <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --name <USER> --json')
# IP restrictions (IMPORTANT for connectivity issues):
cloud_exec('ovh', 'cloud database ip-restriction list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud database ip-restriction add <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --ip <CIDR>')
cloud_exec('ovh', 'cloud database ip-restriction delete <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --ip <CIDR>')
# Backups:
cloud_exec('ovh', 'cloud database backup list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
# Logs:
cloud_exec('ovh', 'cloud database log list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
# Metrics:
cloud_exec('ovh', 'cloud database metric list <ENGINE> <DB_ID> --cloud-project <PROJECT_ID> --json')
# Delete:
cloud_exec('ovh', 'cloud database delete <ENGINE> <DB_ID> --cloud-project <PROJECT_ID>')
```

Plans: `essential` (single node), `business` (HA, 2+ nodes), `enterprise` (dedicated, HA)
Common flavors: `db1-4` (4GB RAM), `db1-7` (7GB RAM), `db1-15` (15GB RAM), `db1-30` (30GB RAM)

### Object Storage (S3-compatible)
```python
# S3 users:
cloud_exec('ovh', 'cloud storage-s3 list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud storage-s3 create --cloud-project <PROJECT_ID> --region <REGION>')
cloud_exec('ovh', 'cloud storage-s3 get <USER_ID> --cloud-project <PROJECT_ID> --json')
# Legacy object storage containers:
cloud_exec('ovh', 'cloud storage container list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud storage container create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION>')
cloud_exec('ovh', 'cloud storage container get --cloud-project <PROJECT_ID> --name <NAME> --json')
cloud_exec('ovh', 'cloud storage container delete --cloud-project <PROJECT_ID> --name <NAME>')
# Object operations:
cloud_exec('ovh', 'cloud storage container object list --cloud-project <PROJECT_ID> --name <CONTAINER>')
```

### Block Storage (Volumes)
```python
cloud_exec('ovh', 'cloud volume list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud volume get <VOLUME_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud volume create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --size <GB> --type high-speed')
cloud_exec('ovh', 'cloud volume attach <VOLUME_ID> --cloud-project <PROJECT_ID> --instance-id <INSTANCE_ID>')
cloud_exec('ovh', 'cloud volume detach <VOLUME_ID> --cloud-project <PROJECT_ID> --instance-id <INSTANCE_ID>')
cloud_exec('ovh', 'cloud volume upsize <VOLUME_ID> --cloud-project <PROJECT_ID> --size <NEW_GB>')
cloud_exec('ovh', 'cloud volume delete <VOLUME_ID> --cloud-project <PROJECT_ID>')
# Snapshots:
cloud_exec('ovh', 'cloud volume snapshot list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud volume snapshot create <VOLUME_ID> --cloud-project <PROJECT_ID> --name my-snap')
```

Volume types: `classic` (HDD), `high-speed` (SSD), `high-speed-gen2` (NVMe)

### Private Networks
```python
cloud_exec('ovh', 'cloud network list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud network get <NETWORK_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud network create --cloud-project <PROJECT_ID> --name <NAME> --vlan-id <ID> --regions <REGION>')
cloud_exec('ovh', 'cloud network subnet list <NETWORK_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud network subnet create <NETWORK_ID> --cloud-project <PROJECT_ID> --region <REGION> --start <START_IP> --end <END_IP> --network <CIDR>')
cloud_exec('ovh', 'cloud network delete <NETWORK_ID> --cloud-project <PROJECT_ID>')
```

### Load Balancers
```python
cloud_exec('ovh', 'cloud loadbalancer list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud loadbalancer get <LB_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud loadbalancer create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --flavor small --json')
cloud_exec('ovh', 'cloud loadbalancer delete <LB_ID> --cloud-project <PROJECT_ID>')
```

### DNS Zones
```python
cloud_exec('ovh', 'domain zone list --json')
cloud_exec('ovh', 'domain zone record list <ZONE> --json')
cloud_exec('ovh', 'domain zone record create <ZONE> --type A --target <IP> --subdomain <SUB> --ttl 3600')
cloud_exec('ovh', 'domain zone record delete <ZONE> <RECORD_ID>')
cloud_exec('ovh', 'domain zone refresh <ZONE>')
```

### Container Registry
```python
cloud_exec('ovh', 'cloud registry list --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud registry get <REGISTRY_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud registry create --cloud-project <PROJECT_ID> --name <NAME> --region <REGION> --plan small')
cloud_exec('ovh', 'cloud registry user list <REGISTRY_ID> --cloud-project <PROJECT_ID> --json')
cloud_exec('ovh', 'cloud registry user create <REGISTRY_ID> --cloud-project <PROJECT_ID> --login <USER> --json')
```

### Instance Logs & Debugging
OVH does not have centralized logging like CloudWatch or Cloud Logging.
- For instances: check serial console via `cloud instance logs <ID>`, or SSH in and check syslog/journalctl
- For Kubernetes pods: use kubectl logs
- For databases: use `cloud database log list`
- For application logs: check Logs Data Platform if configured, or pod logs

## RCA / Investigation Workflow

When investigating an OVH incident:

1. **Get project context**: `cloud project list --json` — identify the project
2. **Check instance state**: `cloud instance list --cloud-project <ID> --json` — look for ERROR/STOPPED status
3. **Get instance details**: `cloud instance get <INSTANCE_ID> --cloud-project <ID> --json` — check flavor, image, network
4. **Check serial console**: `cloud instance logs <INSTANCE_ID> --cloud-project <ID>` — boot issues, kernel panics
5. **Get MKS credentials** (if K8s): Save kubeconfig via `output_file`, then use kubectl
6. **Check pods/containers**: `kubectl get pods -o wide`, `kubectl describe pod`, `kubectl logs`
7. **Check K8s events**: `kubectl get events --sort-by=.lastTimestamp`
8. **Check node health**: `kubectl top nodes`, `kubectl describe node`
9. **Check node pools**: `cloud kube nodepool list <CLUSTER_ID>` — verify nodes are READY
10. **Check databases**: `cloud database get <ENGINE> <DB_ID>` — status, node health
11. **Check DB logs**: `cloud database log list <ENGINE> <DB_ID>` — recent errors
12. **Check DB IP restrictions**: `cloud database ip-restriction list` — connectivity issues often caused by missing IP allowlist
13. **Check networking**: `cloud network list` — verify private network connectivity
14. **Check volumes**: `cloud volume list` — verify attached volumes, check for full disks
15. **Check recent changes**: `kubectl rollout history`, database version upgrades
16. **Compare healthy vs unhealthy**: Pod metrics and logs side-by-side

## Critical Rules
- Use **UUID** from `id` field for flavor/image, NOT names — always query flavors/images first
- Use `--cloud-project <ID>` on EVERY command (NOT `--project-id`)
- Region is POSITIONAL in create commands: `cloud instance create <REGION> ...`
- Use `kube` NOT `kubernetes` subcommand
- Use `--network.public` for public IP (NOT `--network <ID>`)
- Kubeconfig MUST use `output_file` parameter — NEVER echo kubeconfig YAML in shell
- Always query flavors/images/regions BEFORE creating resources — they are region-specific

### Dynamic Data
Context7 docs do NOT contain runtime data. For dynamic values, always use CLI:
- **K8s versions**: `cloud kube create --help`
- **Flavors**: `cloud reference list-flavors --cloud-project <ID> --region <REGION> --json`
- **Images**: `cloud reference list-images --cloud-project <ID> --region <REGION> --json`
- **Regions**: `cloud region list --cloud-project <ID> --json`
- **DB plans/flavors**: Check engine-specific availability per region

## Error Recovery

1. **Project not found** → Verify project ID: `cloud project list --json`
2. **Resource not found** → Check region — resources are region-specific
3. **Flavor/image UUID wrong** → Re-query: `cloud reference list-flavors`, `cloud reference list-images`
4. **Database connection refused** → Check IP restrictions: `cloud database ip-restriction list`
5. **Network connectivity** → Verify private network and subnet configuration
6. **CLI syntax** → `cloud_exec('ovh', '<COMMAND> --help')` for subcommand reference

### Context7 lookup on failure
For CLI errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/ovh/ovhcloud-cli', topic='cloud instance create')`

## Region Mapping
- EU/France (default): GRA (Gravelines), SBG (Strasbourg), RBX (Roubaix)
- EU/Germany: DE1 (Frankfurt)
- EU/UK: UK1 (London)
- EU/Poland: WAW1 (Warsaw)
- US East: US-EAST-VA-1 (Virginia)
- US West: US-WEST-OR-1 (Oregon/Hillsboro)
- Canada: CA-EAST-BHS-1 (Beauharnois)
- Asia-Pacific: AP-SOUTH-SGP-1 (Singapore), AP-SOUTH-SYD-1 (Sydney)
