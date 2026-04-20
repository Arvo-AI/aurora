---
name: azure
id: azure
description: "Azure integration — VMs, AKS, SQL, Storage, App Service, Monitor, NSG, VNet via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "Azure — VMs, AKS, SQL, Storage, App Service, Monitor, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "2.0"
---

# Microsoft Azure Integration

## Overview
Full Azure access via `cloud_exec('azure', 'COMMAND')`.
Available CLIs: `az`, `kubectl`.
Authentication and subscription are auto-configured — never ask users for credentials.

## Resource Group Requirement (CRITICAL)
**Every Azure resource lives in a resource group.** Before creating anything:
1. List existing: `cloud_exec('azure', 'group list --output table')`
2. Reuse a suitable one, or create a new one: `cloud_exec('azure', 'group create --name <RG> --location <LOC>')`
3. Always specify `--resource-group` on subsequent commands.

## Subscription Setup
- Get subscription ID: `cloud_exec('azure', "account show --query 'id' -o tsv")`
- ALWAYS get this before writing Terraform.

## CLI Reference

### Discovery
```python
cloud_exec('azure', 'account show')
cloud_exec('azure', "account show --query 'id' -o tsv")
cloud_exec('azure', 'account list-locations --output table')
cloud_exec('azure', 'group list --output table')
cloud_exec('azure', 'resource list --resource-group <RG> --output table')
cloud_exec('azure', 'provider list --output table')
cloud_exec('azure', 'provider register --namespace Microsoft.<SERVICE>')
```

### Virtual Machines
```python
cloud_exec('azure', 'vm list --output table')
cloud_exec('azure', 'vm list -d --output table')  # includes power state
cloud_exec('azure', 'vm show --resource-group <RG> --name <VM> --show-details')
cloud_exec('azure', 'vm create --resource-group <RG> --name <VM> --image Ubuntu2204 --size Standard_B2s --generate-ssh-keys --output json')
cloud_exec('azure', 'vm start --resource-group <RG> --name <VM>')
cloud_exec('azure', 'vm stop --resource-group <RG> --name <VM>')
cloud_exec('azure', 'vm restart --resource-group <RG> --name <VM>')
cloud_exec('azure', 'vm delete --resource-group <RG> --name <VM> --yes')
cloud_exec('azure', 'vm list-sizes --location <LOC> --output table')
# Diagnostics:
cloud_exec('azure', 'vm get-instance-view --resource-group <RG> --name <VM> --query "instanceView.statuses"')
cloud_exec('azure', 'vm boot-diagnostics get-boot-log --resource-group <RG> --name <VM>')
# Disks:
cloud_exec('azure', 'disk list --resource-group <RG> --output table')
```

### AKS (Kubernetes)
```python
cloud_exec('azure', 'aks list --output table')
cloud_exec('azure', 'aks show --name <CLUSTER> --resource-group <RG>')
# MANDATORY before any kubectl:
cloud_exec('azure', 'aks get-credentials --name <CLUSTER> --resource-group <RG>')
# Then kubectl works:
cloud_exec('azure', 'kubectl get pods -n <NS> -o wide')
cloud_exec('azure', 'kubectl describe pod <POD> -n <NS>')
cloud_exec('azure', 'kubectl logs <POD> -n <NS> --since=1h --tail=200')
cloud_exec('azure', 'kubectl logs <POD> -n <NS> -c <CONTAINER> --previous')
cloud_exec('azure', 'kubectl get events -n <NS> --sort-by=.lastTimestamp')
cloud_exec('azure', 'kubectl top pods -n <NS>')
cloud_exec('azure', 'kubectl top nodes')
cloud_exec('azure', 'kubectl get hpa -n <NS>')
cloud_exec('azure', 'kubectl get deployments -n <NS>')
cloud_exec('azure', 'kubectl rollout history deployment/<DEPLOY> -n <NS>')
cloud_exec('azure', 'kubectl get pvc -n <NS>')
cloud_exec('azure', 'kubectl get svc -n <NS>')
cloud_exec('azure', 'kubectl get ingress -n <NS>')
# Node pools:
cloud_exec('azure', 'aks nodepool list --cluster-name <CLUSTER> --resource-group <RG> --output table')
cloud_exec('azure', 'aks nodepool show --cluster-name <CLUSTER> --resource-group <RG> --nodepool-name <POOL>')
# Scale:
cloud_exec('azure', 'aks nodepool scale --cluster-name <CLUSTER> --resource-group <RG> --nodepool-name <POOL> --node-count 5')
# Create cluster:
cloud_exec('azure', 'aks create --resource-group <RG> --name <CLUSTER> --node-count 3 --node-vm-size Standard_DS2_v2 --generate-ssh-keys --enable-cluster-autoscaler --min-count 1 --max-count 10')
```

### Storage
```python
cloud_exec('azure', 'storage account list --output table')
cloud_exec('azure', 'storage account show --resource-group <RG> --name <ACCT> --query "primaryEndpoints"')
cloud_exec('azure', 'storage account create --name <ACCT> --resource-group <RG> --location <LOC> --sku Standard_LRS')
# Containers:
cloud_exec('azure', 'storage container list --account-name <ACCT> --output table')
cloud_exec('azure', 'storage container create --account-name <ACCT> --name <CONTAINER>')
# Blobs:
cloud_exec('azure', 'storage blob list --account-name <ACCT> --container-name <CONTAINER> --output table')
cloud_exec('azure', 'storage blob upload --account-name <ACCT> --container-name <CONTAINER> --file <LOCAL> --name <BLOB>')
# Keys (for troubleshooting access):
cloud_exec('azure', 'storage account keys list --account-name <ACCT> --resource-group <RG>')
cloud_exec('azure', 'storage account keys renew --account-name <ACCT> --resource-group <RG> --key primary')
```

### SQL Database
```python
cloud_exec('azure', 'sql server list --output table')
cloud_exec('azure', 'sql server show --name <SERVER> --resource-group <RG>')
cloud_exec('azure', 'sql db list --server <SERVER> --resource-group <RG> --output table')
cloud_exec('azure', 'sql db show --server <SERVER> --resource-group <RG> --name <DB>')
# Create:
cloud_exec('azure', 'sql server create --name <SERVER> --resource-group <RG> --location <LOC> --admin-user <USER> --admin-password <PASS>')
cloud_exec('azure', 'sql db create --server <SERVER> --resource-group <RG> --name <DB> --service-objective S0')
# Firewall (allow Azure services):
cloud_exec('azure', 'sql server firewall-rule create --server <SERVER> --resource-group <RG> --name AllowAzure --start-ip-address 0.0.0.0 --end-ip-address 0.0.0.0')
# Auditing and threat detection:
cloud_exec('azure', 'sql server audit-policy show --name <SERVER> --resource-group <RG>')
```

### App Service (Web Apps)
```python
cloud_exec('azure', 'webapp list --output table')
cloud_exec('azure', 'webapp show --name <APP> --resource-group <RG>')
cloud_exec('azure', 'appservice plan list --output table')
cloud_exec('azure', 'appservice plan create --name <PLAN> --resource-group <RG> --sku B1 --is-linux')
cloud_exec('azure', 'webapp create --name <APP> --resource-group <RG> --plan <PLAN> --runtime "PYTHON:3.11"')
cloud_exec('azure', 'webapp log tail --name <APP> --resource-group <RG>')
cloud_exec('azure', 'webapp deployment list-publishing-profiles --name <APP> --resource-group <RG>')
# Configuration:
cloud_exec('azure', 'webapp config show --name <APP> --resource-group <RG>')
cloud_exec('azure', 'webapp config appsettings list --name <APP> --resource-group <RG>')
```

### Azure Monitor & Log Analytics (CRITICAL for investigations)

#### Log Analytics Queries (KQL)
```python
# First, find workspace ID:
cloud_exec('azure', 'monitor log-analytics workspace list --output table')
cloud_exec('azure', 'monitor log-analytics workspace show --resource-group <RG> --workspace-name <WS> --query "customerId" -o tsv')

# Query with KQL:
cloud_exec('azure', 'monitor log-analytics query -w <WORKSPACE_ID> --analytics-query "ContainerLog | where LogEntry contains \'error\' | take 50"')
cloud_exec('azure', 'monitor log-analytics query -w <WORKSPACE_ID> --analytics-query "KubeEvents | where Reason == \'BackOff\' | order by TimeGenerated desc | take 30"')
cloud_exec('azure', 'monitor log-analytics query -w <WORKSPACE_ID> --analytics-query "Perf | where ObjectName == \'K8SContainer\' and CounterName == \'memoryWorkingSetBytes\' | summarize avg(CounterValue) by bin(TimeGenerated, 5m), InstanceName | order by TimeGenerated desc"')
cloud_exec('azure', 'monitor log-analytics query -w <WORKSPACE_ID> --analytics-query "AzureActivity | where OperationNameValue contains \'Microsoft.ContainerService\' | order by TimeGenerated desc | take 20"')
```

Common KQL patterns:
- Container errors: `ContainerLog | where LogEntry contains "error" | order by TimeGenerated desc`
- Pod events: `KubeEvents | where Reason in ("Failed", "BackOff", "Unhealthy") | order by TimeGenerated desc`
- Performance: `Perf | where ObjectName == "K8SContainer" and CounterName == "cpuUsageNanoCores" | summarize avg(CounterValue) by bin(TimeGenerated, 5m), InstanceName`
- Node conditions: `KubeNodeInventory | where Status != "Ready" | order by TimeGenerated desc`
- Deployment changes: `AzureActivity | where OperationNameValue contains "deployments" | order by TimeGenerated desc`

#### Metrics & Alerts
```python
cloud_exec('azure', 'monitor alert list --output table')
cloud_exec('azure', 'monitor metrics list --resource <RESOURCE_ID> --metric "Percentage CPU" --interval PT1H --output table')
cloud_exec('azure', 'monitor metrics list --resource <RESOURCE_ID> --metric "Available Memory Bytes" --interval PT5M --aggregation Average')
cloud_exec('azure', 'monitor metrics list-definitions --resource <RESOURCE_ID> --output table')
```

#### Activity Log
```python
cloud_exec('azure', 'monitor activity-log list --start-time <ISO> --end-time <ISO> --output table')
cloud_exec('azure', 'monitor activity-log list --resource-group <RG> --start-time <ISO>')
cloud_exec('azure', 'monitor activity-log list --caller <EMAIL> --start-time <ISO>')
```

#### Diagnostic Settings
```python
cloud_exec('azure', 'monitor diagnostic-settings list --resource <RESOURCE_ID>')
cloud_exec('azure', 'monitor diagnostic-settings create --name <NAME> --resource <RESOURCE_ID> --workspace <WS_ID> --logs "[{category:kube-audit,enabled:true}]"')
```

### Networking
```python
cloud_exec('azure', 'network vnet list --output table')
cloud_exec('azure', 'network vnet show --resource-group <RG> --name <VNET>')
cloud_exec('azure', 'network vnet subnet list --resource-group <RG> --vnet-name <VNET> --output table')
cloud_exec('azure', 'network nsg list --output table')
cloud_exec('azure', 'network nsg rule list --nsg-name <NSG> --resource-group <RG> --output table')
cloud_exec('azure', 'network nsg rule create --nsg-name <NSG> --resource-group <RG> --name AllowHTTPS --protocol Tcp --direction Inbound --priority 100 --destination-port-ranges 443 --access Allow')
cloud_exec('azure', 'network public-ip list --output table')
cloud_exec('azure', 'network lb list --output table')
cloud_exec('azure', 'network lb show --resource-group <RG> --name <LB>')
# DNS:
cloud_exec('azure', 'network dns zone list --output table')
cloud_exec('azure', 'network dns record-set list --resource-group <RG> --zone-name <ZONE> --output table')
```

### Other Services
```python
# Container Registry:
cloud_exec('azure', 'acr list --output table')
cloud_exec('azure', 'acr repository list --name <REGISTRY>')
cloud_exec('azure', 'acr repository show-tags --name <REGISTRY> --repository <REPO> --orderby time_desc --top 10')
# Key Vault:
cloud_exec('azure', 'keyvault list --output table')
cloud_exec('azure', 'keyvault secret list --vault-name <VAULT> --output table')
# Azure Functions:
cloud_exec('azure', 'functionapp list --output table')
cloud_exec('azure', 'functionapp show --name <APP> --resource-group <RG>')
# Service Bus:
cloud_exec('azure', 'servicebus namespace list --output table')
cloud_exec('azure', 'servicebus queue list --namespace-name <NS> --resource-group <RG>')
```

## RCA / Investigation Workflow

When investigating an Azure incident:

1. **Get subscription context**: `account show`
2. **Find resources**: `resource list --resource-group <RG> --output table`
3. **Get AKS credentials** (if K8s): `aks get-credentials --name <CLUSTER> --resource-group <RG>`
4. **Check resource state**: `vm show --show-details`, `aks show`, `sql server show`
5. **Check pods/containers** (if K8s): `kubectl get pods -o wide`, `kubectl describe pod`, `kubectl logs`
6. **Check Kubernetes events**: `kubectl get events --sort-by=.lastTimestamp`
7. **Query Log Analytics**: KQL queries for ContainerLog, KubeEvents, Perf counters
8. **Check metrics**: `monitor metrics list` for CPU, memory, connections
9. **Check alerts**: `monitor alert list`
10. **Check activity log**: `monitor activity-log list` for recent changes/deployments
11. **Check networking**: NSG rules, load balancer health, DNS resolution
12. **Check node health**: `kubectl describe node`, `kubectl top nodes`, node pool status
13. **Compare healthy vs unhealthy**: Pod metrics, log patterns side-by-side

## Terraform

Use `iac_tool` — provider.tf is AUTO-GENERATED. Never write terraform{} or provider{} blocks.

**PREREQUISITE:** Get subscription ID first:
```python
cloud_exec('azure', "account show --query 'id' -o tsv")
```

**VM passwords are auto-generated** by the system using Terraform's `random_password` resource. Never ask users for passwords.

### Resource Group (always first)
```hcl
resource "azurerm_resource_group" "rg" {
  name     = "my-rg"
  location = "eastus"
}
```

### Linux Virtual Machine
```hcl
resource "azurerm_virtual_network" "vnet" {
  name                = "my-vnet"
  address_space       = ["10.0.0.0/16"]
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
}

resource "azurerm_subnet" "subnet" {
  name                 = "my-subnet"
  resource_group_name  = azurerm_resource_group.rg.name
  virtual_network_name = azurerm_virtual_network.vnet.name
  address_prefixes     = ["10.0.1.0/24"]
}

resource "azurerm_network_interface" "nic" {
  name                = "my-nic"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = azurerm_subnet.subnet.id
    private_ip_address_allocation = "Dynamic"
  }
}

resource "azurerm_linux_virtual_machine" "vm" {
  name                = "my-vm"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  size                = "Standard_B2s"
  admin_username      = "adminuser"
  network_interface_ids = [azurerm_network_interface.nic.id]

  admin_ssh_key {
    username   = "adminuser"
    public_key = file("~/.ssh/id_rsa.pub")
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts"
    version   = "latest"
  }
}
```

### AKS Cluster
```hcl
resource "azurerm_kubernetes_cluster" "aks" {
  name                = "my-aks"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = "myaks"

  default_node_pool {
    name       = "default"
    node_count = 3
    vm_size    = "Standard_DS2_v2"
  }

  identity {
    type = "SystemAssigned"
  }

  tags = {
    Environment = "Production"
  }
}
```

### Storage Account with Network Rules
```hcl
resource "azurerm_storage_account" "storage" {
  name                     = "mystorageaccount"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"

  network_rules {
    default_action = "Deny"
    ip_rules       = ["100.0.0.1"]
  }
}
```

### SQL Server + Database
```hcl
resource "azurerm_mssql_server" "sql" {
  name                         = "my-sql-server"
  resource_group_name          = azurerm_resource_group.rg.name
  location                     = azurerm_resource_group.rg.location
  version                      = "12.0"
  administrator_login          = "sqladmin"
  administrator_login_password = random_password.sql.result
}

resource "azurerm_mssql_database" "db" {
  name      = "my-database"
  server_id = azurerm_mssql_server.sql.id
  sku_name  = "S0"
}
```

### Network Security Group
```hcl
resource "azurerm_network_security_group" "nsg" {
  name                = "my-nsg"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name

  security_rule {
    name                       = "AllowHTTPS"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }
}
```

### Common Terraform resources
`azurerm_resource_group`, `azurerm_virtual_network`, `azurerm_subnet`,
`azurerm_network_interface`, `azurerm_network_security_group`,
`azurerm_linux_virtual_machine`, `azurerm_windows_virtual_machine`,
`azurerm_kubernetes_cluster`, `azurerm_kubernetes_cluster_node_pool`,
`azurerm_storage_account`, `azurerm_storage_container`,
`azurerm_mssql_server`, `azurerm_mssql_database`,
`azurerm_service_plan`, `azurerm_linux_web_app`,
`azurerm_lb`, `azurerm_lb_backend_address_pool`, `azurerm_lb_rule`,
`azurerm_public_ip`, `azurerm_container_registry`,
`azurerm_key_vault`, `azurerm_key_vault_secret`,
`azurerm_dns_zone`, `azurerm_dns_a_record`

## Error Recovery

1. **Provider not registered** → `cloud_exec('azure', 'provider register --namespace Microsoft.ContainerService')` — common: Microsoft.ContainerService, Microsoft.Sql, Microsoft.Storage, Microsoft.Web, Microsoft.Network
2. **Resource group missing** → Create one: `group create --name <RG> --location <LOC>`
3. **Permission denied** → Check role: `cloud_exec('azure', 'role assignment list --assignee <PRINCIPAL> --output table')`
4. **CLI syntax** → `cloud_exec('azure', '<CATEGORY> --help')` for subcommand reference
5. **Storage name taken** → Storage account names must be globally unique, 3-24 chars, lowercase alphanumeric only
6. **Terraform failure** → Verify resources with CLI, then fix manifest

### Context7 lookup on failure
For Terraform errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/hashicorp/terraform-provider-azurerm', topic='azurerm_kubernetes_cluster')`
For CLI errors:
`mcp_context7_get_library_docs(context7CompatibleLibraryID='/microsoftdocs/azure-docs-cli', topic='aks get-credentials')`

## Region Mapping
- US (default): eastus
- Canada: canadacentral
- EU/Belgium: westeurope
- UK/London: uksouth
- Singapore/SEA: southeastasia
- Tokyo/Japan: japaneast
