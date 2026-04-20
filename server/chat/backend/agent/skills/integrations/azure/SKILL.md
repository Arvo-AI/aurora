---
name: azure
id: azure
description: "Microsoft Azure integration for managing VMs, AKS, SQL, Storage, App Service, and other services via CLI and Terraform"
category: cloud_provider
connection_check:
  method: provider_in_preference
tools:
  - cloud_exec
  - iac_tool
index: "Azure — VMs, AKS, SQL, Storage, App Service, Terraform IaC"
rca_priority: 10
allowed-tools: cloud_exec, iac_tool
metadata:
  author: aurora
  version: "1.0"
---

# Microsoft Azure Integration

## Overview
Azure cloud provider for managing compute, Kubernetes, databases, storage, serverless, networking, and monitoring.

## Instructions

### CLI COMMANDS (use cloud_exec with 'azure')

**CRITICAL: Always use cloud_exec('azure', 'COMMAND') — NOT terminal_exec!**
Authentication and subscription are auto-configured. The `az` CLI is available.
Additional CLIs: `kubectl`.

**SUBSCRIPTION & RESOURCE GROUP SETUP:**
- Get subscription: `cloud_exec('azure', "account show --query 'id' -o tsv")`
- List resource groups: `cloud_exec('azure', 'group list --output table')`
- Create resource group: `cloud_exec('azure', 'group create --name <NAME> --location <LOCATION>')`
- **MANDATORY:** Every Azure resource lives in a resource group. Check for existing ones before creating new ones.

**Discovery Commands:**
- Account info: `cloud_exec('azure', 'account show')`
- List locations: `cloud_exec('azure', 'account list-locations --output table')`
- List providers: `cloud_exec('azure', 'provider list --output table')`
- Register provider: `cloud_exec('azure', 'provider register --namespace Microsoft.<SERVICE>')`
- List resources in RG: `cloud_exec('azure', 'resource list --resource-group <RG> --output table')`

**Virtual Machines:**
- List VMs: `cloud_exec('azure', 'vm list --output table')`
- Create VM: `cloud_exec('azure', 'vm create --resource-group <RG> --name <NAME> --image Ubuntu2204 --size Standard_B2s --generate-ssh-keys')`
- Start/stop/restart: `cloud_exec('azure', 'vm start|stop|restart --resource-group <RG> --name <NAME>')`
- Describe: `cloud_exec('azure', 'vm show --resource-group <RG> --name <NAME>')`
- List sizes: `cloud_exec('azure', 'vm list-sizes --location <LOCATION> --output table')`

**AKS (Kubernetes):**
- List clusters: `cloud_exec('azure', 'aks list --output table')`
- Describe cluster: `cloud_exec('azure', 'aks show --name <NAME> --resource-group <RG>')`
- Get credentials: `cloud_exec('azure', 'aks get-credentials --name <NAME> --resource-group <RG>')`
- Then kubectl: `cloud_exec('azure', 'kubectl get pods -n <NAMESPACE> -o wide')`
- Node pools: `cloud_exec('azure', 'aks nodepool list --cluster-name <NAME> --resource-group <RG> --output table')`

**Storage:**
- List accounts: `cloud_exec('azure', 'storage account list --output table')`
- Create account: `cloud_exec('azure', 'storage account create --name <NAME> --resource-group <RG> --location <LOC> --sku Standard_LRS')`
- List containers: `cloud_exec('azure', 'storage container list --account-name <ACCOUNT> --output table')`
- List blobs: `cloud_exec('azure', 'storage blob list --account-name <ACCOUNT> --container-name <CONTAINER> --output table')`

**SQL Database:**
- List servers: `cloud_exec('azure', 'sql server list --output table')`
- List databases: `cloud_exec('azure', 'sql db list --server <SERVER> --resource-group <RG> --output table')`
- Create server: `cloud_exec('azure', 'sql server create --name <NAME> --resource-group <RG> --location <LOC> --admin-user <USER> --admin-password <PASS>')`

**App Service (Web Apps):**
- List apps: `cloud_exec('azure', 'webapp list --output table')`
- Create plan: `cloud_exec('azure', 'appservice plan create --name <PLAN> --resource-group <RG> --sku B1 --is-linux')`
- Create app: `cloud_exec('azure', 'webapp create --name <NAME> --resource-group <RG> --plan <PLAN> --runtime "PYTHON:3.11"')`
- View logs: `cloud_exec('azure', 'webapp log tail --name <NAME> --resource-group <RG>')`

**Azure Monitor & Log Analytics:**
- Query logs: `cloud_exec('azure', 'monitor log-analytics query -w <WORKSPACE_ID> --analytics-query "<KQL_QUERY>"')`
- List alerts: `cloud_exec('azure', 'monitor alert list --output table')`
- Metrics: `cloud_exec('azure', 'monitor metrics list --resource <RESOURCE_ID> --metric "<METRIC>" --interval PT1H')`
- Activity log: `cloud_exec('azure', 'monitor activity-log list --start-time <ISO> --end-time <ISO>')`

**Networking:**
- List VNets: `cloud_exec('azure', 'network vnet list --output table')`
- List NSGs: `cloud_exec('azure', 'network nsg list --output table')`
- NSG rules: `cloud_exec('azure', 'network nsg rule list --nsg-name <NSG> --resource-group <RG> --output table')`
- List public IPs: `cloud_exec('azure', 'network public-ip list --output table')`
- List load balancers: `cloud_exec('azure', 'network lb list --output table')`

### TERRAFORM FOR AZURE
Use iac_tool — provider.tf is AUTO-GENERATED, just write the resource!

**PREREQUISITE:** Get subscription ID first:
`cloud_exec('azure', "account show --query 'id' -o tsv")`

**IMPORTANT:** The system auto-generates strong admin passwords using Terraform's `random_password` resource. Do NOT ask users for passwords.

**RESOURCE GROUP (always needed first):**
```hcl
resource "azurerm_resource_group" "rg" {
  name     = "my-rg"
  location = "eastus"
}
```

**VIRTUAL MACHINE:**
```hcl
resource "azurerm_linux_virtual_machine" "vm" {
  name                = "my-vm"
  resource_group_name = azurerm_resource_group.rg.name
  location            = azurerm_resource_group.rg.location
  size                = "Standard_B2s"
  admin_username      = "adminuser"

  admin_ssh_key {
    username   = "adminuser"
    public_key = file("~/.ssh/id_rsa.pub")
  }

  network_interface_ids = [azurerm_network_interface.nic.id]

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

**AKS CLUSTER:**
```hcl
resource "azurerm_kubernetes_cluster" "aks" {
  name                = "my-aks"
  location            = azurerm_resource_group.rg.location
  resource_group_name = azurerm_resource_group.rg.name
  dns_prefix          = "myaks"

  default_node_pool {
    name       = "default"
    node_count = 3
    vm_size    = "Standard_B2s"
  }

  identity {
    type = "SystemAssigned"
  }
}
```

**STORAGE ACCOUNT:**
```hcl
resource "azurerm_storage_account" "storage" {
  name                     = "mystorageaccount"
  resource_group_name      = azurerm_resource_group.rg.name
  location                 = azurerm_resource_group.rg.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
}
```

**Common Azure Terraform resources:**
- `azurerm_resource_group` — Resource groups (required for everything)
- `azurerm_linux_virtual_machine` — Linux VMs
- `azurerm_network_interface`, `azurerm_virtual_network`, `azurerm_subnet` — Networking
- `azurerm_network_security_group` — Firewall rules
- `azurerm_kubernetes_cluster` — AKS
- `azurerm_storage_account`, `azurerm_storage_container` — Blob storage
- `azurerm_mssql_server`, `azurerm_mssql_database` — SQL databases
- `azurerm_service_plan`, `azurerm_linux_web_app` — App Service
- `azurerm_lb` — Load balancers

DO NOT write terraform{} or provider{} blocks — they are auto-generated!

### CRITICAL RULES
- **ALWAYS** specify `--resource-group` for Azure operations
- Check existing resource groups before creating new ones
- Get subscription ID before writing Terraform
- Use `--output table` for readable CLI output
- AKS: always run `get-credentials` before kubectl commands
- VM passwords are auto-generated by Terraform — never ask the user
- Default location: eastus unless user specifies otherwise
- Resource names in Azure must often be globally unique (storage accounts, web apps)

### ON ANY AZURE ERROR
1. Provider not registered → `cloud_exec('azure', 'provider register --namespace Microsoft.<SERVICE>')`
2. Permission denied → Check role assignments: `cloud_exec('azure', 'role assignment list --assignee <PRINCIPAL>')`
3. Resource group missing → Create one: `cloud_exec('azure', 'group create --name <RG> --location <LOC>')`
4. CLI syntax error → `cloud_exec('azure', '<CATEGORY> --help')`
5. Terraform failure → Verify resources with CLI, then fix the manifest
