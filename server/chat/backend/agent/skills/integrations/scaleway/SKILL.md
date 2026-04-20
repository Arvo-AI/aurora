---
name: scaleway
id: scaleway
description: "Scaleway integration — instances, Kapsule Kubernetes, managed databases, object storage, VPC, serverless, load balancers via CLI"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
index: "Scaleway — instances, Kapsule Kubernetes, managed databases, object storage, VPC, serverless"
rca_priority: 10
allowed-tools: cloud_exec
metadata:
  author: aurora
  version: "2.0"
---

# Scaleway Integration

## Overview
Full Scaleway access via `cloud_exec('scaleway', 'COMMAND')`.
Available CLI: `scw` (aliased through cloud_exec).
Authentication is automatic — never ask users for credentials.

## CLI Syntax (CRITICAL)
Scaleway CLI uses `key=value` syntax for most parameters (NOT `--key value`):
```python
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm')
```

## CLI Reference

### Discovery
```python
cloud_exec('scaleway', 'account project list')
cloud_exec('scaleway', 'instance zone list')
cloud_exec('scaleway', 'instance server-type list')
cloud_exec('scaleway', 'instance server-type list zone=fr-par-1')
cloud_exec('scaleway', 'instance image list')
cloud_exec('scaleway', 'marketplace image list')
```

### Instances (Compute)
```python
# List instances:
cloud_exec('scaleway', 'instance server list')
cloud_exec('scaleway', 'instance server list zone=fr-par-1')
# Get instance details:
cloud_exec('scaleway', 'instance server get <SERVER_ID>')
# Create instance:
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm')
# Create with zone:
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm zone=fr-par-1')
# Create with cloud-init:
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm cloud-init=@/path/to/init.sh')
# Create with additional block volume:
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm additional-volumes.0=block:20G')
# Create with specific root volume size:
cloud_exec('scaleway', 'instance server create type=DEV1-S image=ubuntu_jammy name=my-vm root-volume=local:20G')
# Start/Stop/Reboot:
cloud_exec('scaleway', 'instance server start <SERVER_ID>')
cloud_exec('scaleway', 'instance server stop <SERVER_ID>')
cloud_exec('scaleway', 'instance server reboot <SERVER_ID>')
# Standby (hibernate):
cloud_exec('scaleway', 'instance server standby <SERVER_ID>')
# Terminate (delete server + IP + volumes):
cloud_exec('scaleway', 'instance server terminate <SERVER_ID> with-ip=true with-block=true')
# Delete (just the server, keeps IP and volumes):
cloud_exec('scaleway', 'instance server delete <SERVER_ID>')
# SSH into server:
cloud_exec('scaleway', 'instance server ssh <SERVER_ID>')
# Serial console (for boot debugging):
cloud_exec('scaleway', 'instance server console <SERVER_ID>')
# User data:
cloud_exec('scaleway', 'instance server get-user-data <SERVER_ID> key=cloud-init')
```

Instance types:
- Development: `DEV1-S` (2 vCPU, 2GB), `DEV1-M` (3 vCPU, 4GB), `DEV1-L` (4 vCPU, 8GB), `DEV1-XL` (4 vCPU, 12GB)
- General Purpose: `GP1-XS` (4 vCPU, 16GB), `GP1-S` (8 vCPU, 32GB), `GP1-M` (16 vCPU, 64GB), `GP1-L` (32 vCPU, 128GB)
- Production Optimized: `PRO2-XXS`, `PRO2-XS`, `PRO2-S`, `PRO2-M`
- Enterprise: `ENT1-XXS`, `ENT1-XS`, `ENT1-S`, `ENT1-M`
- Default SSH username: `root`

### IP Addresses
```python
cloud_exec('scaleway', 'instance ip list')
cloud_exec('scaleway', 'instance ip create')
cloud_exec('scaleway', 'instance ip attach <IP_ID> server-id=<SERVER_ID>')
cloud_exec('scaleway', 'instance ip detach <IP_ID>')
cloud_exec('scaleway', 'instance ip delete <IP_ID>')
```

### Security Groups (Firewall)
```python
cloud_exec('scaleway', 'instance security-group list')
cloud_exec('scaleway', 'instance security-group get <SG_ID>')
cloud_exec('scaleway', 'instance security-group create name=my-sg inbound-default-policy=drop outbound-default-policy=accept')
cloud_exec('scaleway', 'instance security-group-rule create security-group-id=<SG_ID> protocol=TCP direction=inbound action=accept dest-port-from=443')
cloud_exec('scaleway', 'instance security-group-rule create security-group-id=<SG_ID> protocol=TCP direction=inbound action=accept dest-port-from=80')
cloud_exec('scaleway', 'instance security-group-rule list security-group-id=<SG_ID>')
cloud_exec('scaleway', 'instance security-group-rule delete <RULE_ID>')
```

### Block Storage (Volumes)
```python
cloud_exec('scaleway', 'instance volume list')
cloud_exec('scaleway', 'instance volume get <VOLUME_ID>')
cloud_exec('scaleway', 'instance volume create name=data-vol size=50GB volume-type=b_ssd')
cloud_exec('scaleway', 'instance volume delete <VOLUME_ID>')
# Snapshots:
cloud_exec('scaleway', 'instance snapshot list')
cloud_exec('scaleway', 'instance snapshot create volume-id=<VOLUME_ID> name=my-snap')
cloud_exec('scaleway', 'instance snapshot delete <SNAPSHOT_ID>')
```

Volume types: `l_ssd` (local SSD), `b_ssd` (block SSD)

### Kapsule (Managed Kubernetes)
```python
# List clusters:
cloud_exec('scaleway', 'k8s cluster list')
# Get cluster details:
cloud_exec('scaleway', 'k8s cluster get <CLUSTER_ID>')
# Create cluster:
cloud_exec('scaleway', 'k8s cluster create name=my-cluster version=1.31 cni=cilium')
# Create with auto-upgrade:
cloud_exec('scaleway', 'k8s cluster create name=my-cluster version=1.31 cni=cilium auto-upgrade.enable=true auto-upgrade.maintenance-window.day=sunday auto-upgrade.maintenance-window.start-hour=3')
# Create in VPC:
cloud_exec('scaleway', 'k8s cluster create name=my-cluster version=1.31 cni=cilium private-network-id=<PN_ID>')
# Get kubeconfig:
cloud_exec('scaleway', 'k8s kubeconfig get <CLUSTER_ID>')
# Install kubeconfig (writes to ~/.kube/config):
cloud_exec('scaleway', 'k8s kubeconfig install <CLUSTER_ID>')
# Node pools:
cloud_exec('scaleway', 'k8s pool list cluster-id=<CLUSTER_ID>')
cloud_exec('scaleway', 'k8s pool get <POOL_ID>')
cloud_exec('scaleway', 'k8s pool create cluster-id=<CLUSTER_ID> name=worker-pool node-type=DEV1-M size=3')
# With autoscaling:
cloud_exec('scaleway', 'k8s pool create cluster-id=<CLUSTER_ID> name=worker-pool node-type=GP1-XS size=3 min-size=1 max-size=10 autoscaling=true autohealing=true')
# Scale pool:
cloud_exec('scaleway', 'k8s pool update <POOL_ID> size=5')
# Delete pool:
cloud_exec('scaleway', 'k8s pool delete <POOL_ID>')
# List nodes:
cloud_exec('scaleway', 'k8s node list cluster-id=<CLUSTER_ID>')
# Upgrade cluster:
cloud_exec('scaleway', 'k8s cluster update <CLUSTER_ID> version=1.31')
# Delete cluster:
cloud_exec('scaleway', 'k8s cluster delete <CLUSTER_ID>')
```

**KUBECTL WORKFLOW:** After `k8s kubeconfig install <CLUSTER_ID>`, kubectl works directly:
```python
cloud_exec('scaleway', 'kubectl get pods -n <NS> -o wide')
cloud_exec('scaleway', 'kubectl describe pod <POD> -n <NS>')
cloud_exec('scaleway', 'kubectl logs <POD> -n <NS> --since=1h --tail=200')
cloud_exec('scaleway', 'kubectl logs <POD> -n <NS> -c <CONTAINER> --previous')
cloud_exec('scaleway', 'kubectl get events -n <NS> --sort-by=.lastTimestamp')
cloud_exec('scaleway', 'kubectl top pods -n <NS>')
cloud_exec('scaleway', 'kubectl top nodes')
cloud_exec('scaleway', 'kubectl get hpa -n <NS>')
cloud_exec('scaleway', 'kubectl get deployments -n <NS>')
cloud_exec('scaleway', 'kubectl rollout history deployment/<DEPLOY> -n <NS>')
cloud_exec('scaleway', 'kubectl get pvc -n <NS>')
cloud_exec('scaleway', 'kubectl get svc -n <NS>')
cloud_exec('scaleway', 'kubectl get ingress -n <NS>')
```

### Object Storage
```python
cloud_exec('scaleway', 'object bucket list')
cloud_exec('scaleway', 'object bucket get name=<BUCKET>')
cloud_exec('scaleway', 'object bucket create name=my-bucket')
cloud_exec('scaleway', 'object bucket delete name=<BUCKET>')
# ACL:
cloud_exec('scaleway', 'object bucket get-acl name=<BUCKET>')
```

### Managed Databases (RDB)
```python
# List instances:
cloud_exec('scaleway', 'rdb instance list')
# Get instance details:
cloud_exec('scaleway', 'rdb instance get <INSTANCE_ID>')
# Create PostgreSQL:
cloud_exec('scaleway', 'rdb instance create name=my-db engine=PostgreSQL-15 node-type=DB-DEV-S is-ha-cluster=false')
# Create MySQL:
cloud_exec('scaleway', 'rdb instance create name=my-db engine=MySQL-8 node-type=DB-DEV-S')
# Create with HA:
cloud_exec('scaleway', 'rdb instance create name=my-db engine=PostgreSQL-15 node-type=DB-GP-XS is-ha-cluster=true')
# Databases:
cloud_exec('scaleway', 'rdb database list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb database create instance-id=<INSTANCE_ID> name=mydb')
# Users:
cloud_exec('scaleway', 'rdb user list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb user create instance-id=<INSTANCE_ID> name=myuser password=<PASS>')
# ACL / Network access (CRITICAL for connectivity):
cloud_exec('scaleway', 'rdb acl list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb acl add instance-id=<INSTANCE_ID> rules.0.ip=<CIDR> rules.0.description=office')
cloud_exec('scaleway', 'rdb acl delete instance-id=<INSTANCE_ID> acl-rule-ips.0=<CIDR>')
# Backups:
cloud_exec('scaleway', 'rdb backup list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb backup create instance-id=<INSTANCE_ID> name=my-backup database-name=mydb')
cloud_exec('scaleway', 'rdb backup restore <BACKUP_ID> instance-id=<INSTANCE_ID> database-name=mydb')
# Logs:
cloud_exec('scaleway', 'rdb log list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb log prepare instance-id=<INSTANCE_ID> start-date=<ISO> end-date=<ISO>')
# Read replicas:
cloud_exec('scaleway', 'rdb read-replica list instance-id=<INSTANCE_ID>')
cloud_exec('scaleway', 'rdb read-replica create instance-id=<INSTANCE_ID>')
# Upgrade instance:
cloud_exec('scaleway', 'rdb instance upgrade <INSTANCE_ID> node-type=DB-GP-XS')
# Metrics:
cloud_exec('scaleway', 'rdb instance get-metrics <INSTANCE_ID>')
# Delete:
cloud_exec('scaleway', 'rdb instance delete <INSTANCE_ID>')
```

Database node types: `DB-DEV-S` (1 vCPU, 2GB), `DB-DEV-M` (2 vCPU, 4GB), `DB-GP-XS` (4 vCPU, 16GB), `DB-GP-S` (8 vCPU, 32GB), `DB-GP-M` (16 vCPU, 64GB)

### VPC / Private Networks
```python
# VPC (parent container):
cloud_exec('scaleway', 'vpc list')
cloud_exec('scaleway', 'vpc get <VPC_ID>')
cloud_exec('scaleway', 'vpc create name=my-vpc')
# Private networks:
cloud_exec('scaleway', 'vpc private-network list')
cloud_exec('scaleway', 'vpc private-network get <PN_ID>')
cloud_exec('scaleway', 'vpc private-network create name=my-network')
# With CIDR:
cloud_exec('scaleway', 'vpc private-network create name=my-network subnets.0=192.168.1.0/24')
cloud_exec('scaleway', 'vpc private-network delete <PN_ID>')
# Attach instance to private network:
cloud_exec('scaleway', 'instance private-nic create server-id=<SERVER_ID> private-network-id=<PN_ID>')
cloud_exec('scaleway', 'instance private-nic list server-id=<SERVER_ID>')
cloud_exec('scaleway', 'instance private-nic delete server-id=<SERVER_ID> private-nic-id=<NIC_ID>')
```

### Load Balancers
```python
cloud_exec('scaleway', 'lb list')
cloud_exec('scaleway', 'lb get <LB_ID>')
cloud_exec('scaleway', 'lb create name=my-lb type=LB-S')
# Backends:
cloud_exec('scaleway', 'lb backend list lb-id=<LB_ID>')
cloud_exec('scaleway', 'lb backend create lb-id=<LB_ID> name=web-backend forward-port=80 forward-protocol=tcp health-check.port=80')
cloud_exec('scaleway', 'lb backend create lb-id=<LB_ID> name=web-backend forward-port=443 forward-protocol=tcp health-check.port=443 health-check.tcp-config={}')
# Add servers to backend:
cloud_exec('scaleway', 'lb backend add-servers <BACKEND_ID> server-ip.0=<IP>')
cloud_exec('scaleway', 'lb backend remove-servers <BACKEND_ID> server-ip.0=<IP>')
# Frontends:
cloud_exec('scaleway', 'lb frontend list lb-id=<LB_ID>')
cloud_exec('scaleway', 'lb frontend create lb-id=<LB_ID> name=web-frontend inbound-port=443 backend-id=<BACKEND_ID>')
# Health checks:
cloud_exec('scaleway', 'lb backend get-healthcheck <BACKEND_ID>')
# Stats:
cloud_exec('scaleway', 'lb get-stats <LB_ID>')
# Delete:
cloud_exec('scaleway', 'lb delete <LB_ID>')
```

LB types: `LB-S` (small), `LB-GP-M` (medium), `LB-GP-L` (large)

### Container Registry
```python
cloud_exec('scaleway', 'registry namespace list')
cloud_exec('scaleway', 'registry namespace get <NS_ID>')
cloud_exec('scaleway', 'registry namespace create name=my-registry')
cloud_exec('scaleway', 'registry image list namespace-id=<NS_ID>')
cloud_exec('scaleway', 'registry tag list image-id=<IMAGE_ID>')
cloud_exec('scaleway', 'registry namespace delete <NS_ID>')
```

### Serverless Functions
```python
cloud_exec('scaleway', 'function namespace list')
cloud_exec('scaleway', 'function namespace get <NS_ID>')
cloud_exec('scaleway', 'function namespace create name=my-funcs')
cloud_exec('scaleway', 'function function list namespace-id=<NS_ID>')
cloud_exec('scaleway', 'function function get <FUNCTION_ID>')
cloud_exec('scaleway', 'function function deploy <FUNCTION_ID>')
cloud_exec('scaleway', 'function log list function-id=<FUNCTION_ID>')
```

### Serverless Containers
```python
cloud_exec('scaleway', 'container namespace list')
cloud_exec('scaleway', 'container namespace get <NS_ID>')
cloud_exec('scaleway', 'container namespace create name=my-containers')
cloud_exec('scaleway', 'container container list namespace-id=<NS_ID>')
cloud_exec('scaleway', 'container container get <CONTAINER_ID>')
cloud_exec('scaleway', 'container container deploy <CONTAINER_ID>')
cloud_exec('scaleway', 'container log list container-id=<CONTAINER_ID>')
```

### DNS
```python
cloud_exec('scaleway', 'dns zone list')
cloud_exec('scaleway', 'dns record list <ZONE>')
cloud_exec('scaleway', 'dns record add <ZONE> name=<SUB> type=A data=<IP> ttl=3600')
cloud_exec('scaleway', 'dns record delete <ZONE> name=<SUB> type=A data=<IP>')
```

### Secret Manager
```python
cloud_exec('scaleway', 'secret secret list')
cloud_exec('scaleway', 'secret secret get <SECRET_ID>')
cloud_exec('scaleway', 'secret secret create name=my-secret')
cloud_exec('scaleway', 'secret version list secret-id=<SECRET_ID>')
cloud_exec('scaleway', 'secret version create secret-id=<SECRET_ID> data=@/path/to/secret.txt')
cloud_exec('scaleway', 'secret version access <VERSION_ID>')
```

## RCA / Investigation Workflow

When investigating a Scaleway incident:

1. **List instances**: `instance server list` — check status (running/stopped/stopping)
2. **Get instance details**: `instance server get <ID>` — check type, volumes, security groups
3. **Check serial console**: `instance server console <ID>` — boot issues, kernel panics
4. **Get Kapsule credentials** (if K8s): `k8s kubeconfig install <CLUSTER_ID>`
5. **Check cluster health**: `k8s cluster get <ID>` — version, status, node count
6. **Check pods/containers**: `kubectl get pods -o wide`, `kubectl describe pod`, `kubectl logs`
7. **Check K8s events**: `kubectl get events --sort-by=.lastTimestamp`
8. **Check node health**: `kubectl top nodes`, `kubectl describe node`, `k8s node list cluster-id=<ID>`
9. **Check node pools**: `k8s pool list cluster-id=<ID>` — verify pool status, autoscaling config
10. **Check databases**: `rdb instance get <ID>` — status, engine version, HA status
11. **Check DB logs**: `rdb log list instance-id=<ID>` — recent errors, slow queries
12. **Check DB ACLs**: `rdb acl list instance-id=<ID>` — connectivity issues often caused by missing ACL rules
13. **Check DB backups**: `rdb backup list instance-id=<ID>` — verify backup health
14. **Check networking**: `vpc private-network list`, security group rules, private NIC attachments
15. **Check load balancer**: `lb get-stats <ID>`, `lb backend get-healthcheck <BACKEND_ID>`
16. **Check serverless**: `function log list`, `container log list` — invocation errors
17. **Check recent deployments**: `kubectl rollout history`, function/container deploy history
18. **Compare healthy vs unhealthy**: Pod metrics, logs, instance status side-by-side

## Critical Rules
- **ALWAYS** use `cloud_exec('scaleway', ...)` NOT `terminal_exec` for Scaleway commands
- Scaleway CLI uses `key=value` syntax, NOT `--key value` for most parameters
- Default region: `fr-par`, zones: `fr-par-1`, `fr-par-2`, `fr-par-3`
- Default SSH username for instances: `root`
- Instance IDs are UUIDs — always use the full UUID
- `instance server terminate` deletes server + associated resources (IP, volumes if specified)
- `instance server delete` only removes the server, keeps IP and volumes

## Error Recovery

1. **Resource not found** → Verify zone/region — resources are zone-specific
2. **Quota exceeded** → Check project quotas, try a different zone
3. **Permission denied** → Verify API key has correct project scope
4. **Server type unavailable** → Try different zone: `fr-par-1`, `fr-par-2`, `fr-par-3`, `nl-ams-1`, `nl-ams-2`, `pl-waw-1`, `pl-waw-2`
5. **Database connection refused** → Check ACL rules: `rdb acl list instance-id=<ID>`
6. **CLI syntax** → `cloud_exec('scaleway', '<PRODUCT> <RESOURCE> --help')` for subcommand reference

### Context7 lookup on failure
For resource reference:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/scaleway/terraform-provider-scaleway', topic='scaleway_instance_server')`

## Region Mapping
- France (default): fr-par, zones: fr-par-1, fr-par-2, fr-par-3
- Netherlands: nl-ams, zones: nl-ams-1, nl-ams-2, nl-ams-3
- Poland: pl-waw, zones: pl-waw-1, pl-waw-2, pl-waw-3
