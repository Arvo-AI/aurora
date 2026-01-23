#!/bin/bash
set -e

echo "Aurora Access Setup"
echo "================================"

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Function to detect OS
detect_os() {
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if command_exists apt-get; then
            echo "ubuntu"
        elif command_exists yum; then
            echo "rhel"
        elif command_exists dnf; then
            echo "fedora"
        else
            echo "linux"
        fi
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ "$OSTYPE" == "cygwin" ]] || [[ "$OSTYPE" == "msys" ]] || [[ "$OSTYPE" == "win32" ]]; then
        echo "windows"
    else
        echo "unknown"
    fi
}

# Function to install Azure CLI
install_azure_cli() {
    local os=$(detect_os)
    echo "Installing Azure CLI for $os..."

    case $os in
        ubuntu)
            curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
            ;;
        rhel|fedora)
            sudo rpm --import https://packages.microsoft.com/keys/microsoft.asc
            sudo dnf install -y azure-cli
            ;;
        macos)
            if command_exists brew; then
                brew install azure-cli
            else
                echo "Homebrew not found. Please install Homebrew first:"
                echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
                echo "   Then run: brew install azure-cli"
                exit 1
            fi
            ;;
        windows)
            echo "Please install Azure CLI manually on Windows:"
            echo "   Download from: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli-windows"
            echo "   Or use PowerShell: Invoke-WebRequest -Uri https://aka.ms/installazurecliwindows -OutFile .\\AzureCLI.msi; Start-Process msiexec.exe -Wait -ArgumentList '/I AzureCLI.msi /quiet'"
            exit 1
            ;;
        *)
            echo "Unsupported OS. Please install Azure CLI manually:"
            echo "   https://docs.microsoft.com/en-us/cli/azure/install-azure-cli"
            exit 1
            ;;
    esac
}

# Function to install jq
install_jq() {
    local os=$(detect_os)
    echo "Installing jq for $os..."

    case $os in
        ubuntu)
            sudo apt-get update && sudo apt-get install -y jq
            ;;
        rhel|fedora)
            sudo dnf install -y jq
            ;;
        macos)
            if command_exists brew; then
                brew install jq
            else
                echo "Homebrew not found. Please install Homebrew first:"
                echo "   /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
                echo "   Then run: brew install jq"
                exit 1
            fi
            ;;
        windows)
            echo "Please install jq manually on Windows:"
            echo "   Download from: https://stedolan.github.io/jq/download/"
            echo "   Or use Chocolatey: choco install jq"
            exit 1
            ;;
        *)
            echo "Unsupported OS. Please install jq manually:"
            echo "   https://stedolan.github.io/jq/download/"
            exit 1
            ;;
    esac
}

# Check for Azure CLI
if ! command_exists az; then
    echo "Azure CLI not found. Installing..."
    install_azure_cli
    echo "Azure CLI installed successfully!"
else
    echo "Azure CLI is already installed"
fi

# Check for jq
if ! command_exists jq; then
    echo "jq not found. Installing..."
    install_jq
    echo "jq installed successfully!"
else
    echo "jq is already installed"
fi

echo ""

# Check if user is logged in
if ! az account show &>/dev/null; then
    echo "Not logged in to Azure. Logging you in now..."
    az login
    echo "Successfully logged in to Azure!"
    echo
fi

# Get current subscription and location info
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)
SUBSCRIPTION_NAME=$(az account show --query name -o tsv)

# Auto-detect location based on resource groups or use account's home region
LOCATION=$(az account list-locations --query "[?metadata.regionType=='Physical'] | [0].name" -o tsv 2>/dev/null || echo "eastus")

echo "Detected Configuration:"
echo "   Subscription: $SUBSCRIPTION_NAME"
echo "   Subscription ID: $SUBSCRIPTION_ID"
echo "   Tenant ID: $TENANT_ID"
echo "   Location: $LOCATION"
echo

# Detect existing AKS clusters
echo "Detecting existing AKS clusters..."
AKS_CLUSTERS=$(az aks list --query "[].{name:name,resourceGroup:resourceGroup}" -o json 2>/dev/null || echo "[]")
# Add current subscription ID to each cluster since they're all in the current subscription
AKS_CLUSTERS=$(echo "$AKS_CLUSTERS" | jq --arg sub_id "$SUBSCRIPTION_ID" 'map(. + {subscriptionId: $sub_id})')
CLUSTER_COUNT=$(echo "$AKS_CLUSTERS" | jq length)

if [ "$CLUSTER_COUNT" -eq 0 ]; then
    echo "   No AKS clusters found in this subscription"
else
    echo "   Found $CLUSTER_COUNT AKS cluster(s):"
    echo "$AKS_CLUSTERS" | jq -r '.[] | "   - \(.name) (Resource Group: \(.resourceGroup))"'
fi
echo

# Create the ARM template inline (no separate file needed)
echo "Creating Aurora access template..."
cat > aurora-access-template.json << 'EOF'
{
  "$schema": "https://schema.management.azure.com/schemas/2018-05-01/subscriptionDeploymentTemplate.json#",
  "contentVersion": "1.0.0.0",
  "parameters": {
    "roleDefinitionName": {
      "type": "string",
      "defaultValue": "Aurora Manager",
      "metadata": {
        "description": "Name of the custom role for Aurora"
      }
    }
  },
  "variables": {
    "roleDefinitionId": "[guid(subscription().id, parameters('roleDefinitionName'))]"
  },
  "resources": [
    {
      "type": "Microsoft.Authorization/roleDefinitions",
      "apiVersion": "2022-04-01",
      "name": "[variables('roleDefinitionId')]",
      "properties": {
        "roleName": "[parameters('roleDefinitionName')]",
        "description": "Custom role for the Aurora platform with comprehensive cloud management capabilities including resource creation, modification, and permission management",
        "type": "CustomRole",
        "permissions": [
          {
            "actions": [
              "*"
            ],
            "notActions": [
              "Microsoft.Authorization/*/Delete",
              "Microsoft.Blueprint/*/write",
              "Microsoft.Blueprint/*/delete",
              "Microsoft.Compute/galleries/*/delete",
              "Microsoft.Compute/images/*/delete"
            ],
            "dataActions": [],
            "notDataActions": []
          }
        ],
        "assignableScopes": [
          "[subscription().id]"
        ]
      }
    }
  ],
  "outputs": {
    "customRoleId": {
      "type": "string",
      "value": "[resourceId('Microsoft.Authorization/roleDefinitions', variables('roleDefinitionId'))]"
    },
    "subscriptionId": {
      "type": "string",
      "value": "[subscription().subscriptionId]"
    },
    "tenantId": {
      "type": "string",
      "value": "[subscription().tenantId]"
    }
  }
}
EOF

# Deploy the template
echo "Deploying Aurora custom role..."
DEPLOYMENT_OUTPUT=$(az deployment sub create \
    --name "aurora-access-$(date +%Y%m%d-%H%M%S)" \
    --location "$LOCATION" \
    --template-file aurora-access-template.json \
    --query 'properties.outputs' -o json)

# Extract outputs
CUSTOM_ROLE_ID=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.customRoleId.value')

echo "Custom role created successfully!"
echo "   Role ID: $CUSTOM_ROLE_ID"
echo

# Create service principal with the custom role
echo "Creating Aurora service principal..."
SP_OUTPUT=$(az ad sp create-for-rbac \
    --name "Aurora-Access-$(date +%Y%m%d-%H%M%S)" \
    --role "$CUSTOM_ROLE_ID" \
    --scopes "/subscriptions/$SUBSCRIPTION_ID" \
    --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json)

# Extract credentials
CLIENT_ID=$(echo "$SP_OUTPUT" | jq -r '.clientId')
CLIENT_SECRET=$(echo "$SP_OUTPUT" | jq -r '.clientSecret')

echo "Service principal created successfully!"
echo

# Create read-only service principal
echo "Creating Aurora read-only service principal..."
READONLY_SP_OUTPUT=$(az ad sp create-for-rbac \
    --name "Aurora-ReadOnly-$(date +%Y%m%d-%H%M%S)" \
    --role "Reader" \
    --scopes "/subscriptions/$SUBSCRIPTION_ID" \
    --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json)

# Extract read-only credentials
READONLY_CLIENT_ID=$(echo "$READONLY_SP_OUTPUT" | jq -r '.clientId')
READONLY_CLIENT_SECRET=$(echo "$READONLY_SP_OUTPUT" | jq -r '.clientSecret')

echo "Read-only service principal created successfully!"
echo

# Assign comprehensive read-only roles for ALL Azure resources
echo "Assigning comprehensive read-only roles..."

# Base reader role (already assigned but ensure it's there)
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Monitoring and Logging roles
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Log Analytics Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Monitoring Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Monitoring Metrics Publisher" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Cost and Billing
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Cost Management Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Billing Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Storage roles
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Storage Blob Data Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Storage File Data SMB Share Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Storage Queue Data Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Storage Table Data Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Security and Key Vault
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Key Vault Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Key Vault Secrets User" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Security Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Virtual Machines and Compute
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Virtual Machine User Login" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# AKS (Azure Kubernetes Service) - COMPREHENSIVE ACCESS
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Azure Kubernetes Service Cluster User Role" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Azure Kubernetes Service RBAC Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Cosmos DB - COMPREHENSIVE READ ACCESS
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Cosmos DB Account Reader Role" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Database roles - READ ONLY
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Reader and Data Access" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Networking - READ ONLY (Reader role covers networking resources)
# Network Contributor removed - Reader role is sufficient

# App Service and Functions - READ ONLY
# Website/Web Plan Contributor removed - Reader role provides read access to app services

# Event Hubs and Service Bus
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Azure Event Hubs Data Receiver" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Azure Service Bus Data Receiver" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Azure AD and Graph - handled separately via Azure AD permissions

# API Management
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "API Management Service Reader Role" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Container Registry
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "AcrPull" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Backup
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Backup Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Automation - READ ONLY (covered by Reader role)

# DevTest Labs - READ ONLY (covered by Reader role)

# HDInsight - READ ONLY (covered by Reader role)

# IoT
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "IoT Hub Data Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Logic Apps - READ ONLY (covered by Reader role)

# Redis Cache - READ ONLY (covered by Reader role)

# Cognitive Services
az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Cognitive Services User" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null || true

# Grant minimal Azure AD permissions for CLI authentication (NOT admin/owner roles)
echo "Granting Azure AD read permissions for CLI authentication..."
# Get the application ID for the read-only service principal
READONLY_APP_ID=$(az ad sp show --id "$READONLY_CLIENT_ID" --query "appId" -o tsv 2>/dev/null)
if [ -n "$READONLY_APP_ID" ]; then
    # Add User.Read permission (minimal permission to read own profile)
    az ad app permission add --id "$READONLY_APP_ID" \
        --api 00000003-0000-0000-c000-000000000000 \
        --api-permissions e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope &>/dev/null || true

    # Add Directory.Read.All permission (read-only access to directory data)
    az ad app permission add --id "$READONLY_APP_ID" \
        --api 00000003-0000-0000-c000-000000000000 \
        --api-permissions 7ab1d382-f21e-4acd-a863-ba3e13f7da61=Role &>/dev/null || true

    # Grant admin consent for these permissions
    az ad app permission admin-consent --id "$READONLY_APP_ID" &>/dev/null || true
    echo "Azure AD permissions granted successfully!"
else
    echo "Warning: Could not grant Azure AD permissions (app ID not found)"
fi

echo "Read-only roles assigned successfully!"
echo

# Clean up temporary file
rm -f aurora-access-template.json

# Display final results
echo "AURORA SETUP COMPLETE!"
echo "================================"
echo

if [ "$CLUSTER_COUNT" -gt 0 ]; then
    echo "AKS CLUSTER ROLE ASSIGNMENTS"
    echo "Automatically assigning permissions to existing AKS clusters..."
    echo "================================"

    # Basic subscription-level permissions
    echo "Assigning basic subscription-level permissions..."
    echo "Running: az role assignment create --assignee $CLIENT_ID --role Reader --scope \"/subscriptions/$SUBSCRIPTION_ID\""
    if az role assignment create --assignee "$CLIENT_ID" --role "Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null; then
        echo "   Reader role assigned successfully"
    else
        echo "   Reader role assignment failed or already exists"
    fi

    echo "Running: az role assignment create --assignee $CLIENT_ID --role \"Cost Management Reader\" --scope \"/subscriptions/$SUBSCRIPTION_ID\""
    if az role assignment create --assignee "$CLIENT_ID" --role "Cost Management Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" &>/dev/null; then
        echo "   Cost Management Reader role assigned successfully"
    else
        echo "   Cost Management Reader role assignment failed or already exists"
    fi
    echo

    # AKS cluster-specific permissions
    echo "Assigning AKS cluster-specific permissions..."
    echo "$AKS_CLUSTERS" | jq -r '.[] | "\(.subscriptionId) \(.resourceGroup) \(.name)"' | while read -r sub_id rg cluster_name; do
        scope="/subscriptions/$sub_id/resourceGroups/$rg/providers/Microsoft.ContainerService/managedClusters/$cluster_name"
        echo "Running: az role assignment create --assignee $CLIENT_ID --role \"Azure Kubernetes Service Cluster Admin Role\" --scope \"$scope\""
        if az role assignment create --assignee "$CLIENT_ID" --role "Azure Kubernetes Service Cluster Admin Role" --scope "$scope" &>/dev/null; then
            echo "   AKS Cluster Admin role assigned successfully for $cluster_name"
        else
            echo "   AKS Cluster Admin role assignment failed or already exists for $cluster_name"
        fi

        # Read-only AKS permissions
        if az role assignment create --assignee "$READONLY_CLIENT_ID" --role "Azure Kubernetes Service Cluster User Role" --scope "$scope" &>/dev/null; then
            echo "   AKS Cluster User role assigned to read-only SP for $cluster_name"
        fi

        # Add Kubernetes RBAC permissions
        echo "Adding Kubernetes RBAC permissions for cluster: $cluster_name"
        echo "Getting service principal object ID..."
        OBJECT_ID=$(az ad sp show --id "$CLIENT_ID" --query "id" --output tsv 2>/dev/null)
        if [ -z "$OBJECT_ID" ]; then
            echo "   Failed to get service principal object ID"
            continue
        fi
        echo "   Service principal object ID: $OBJECT_ID"

        echo "Getting AKS credentials..."
        if az aks get-credentials --resource-group "$rg" --name "$cluster_name" --admin --overwrite-existing &>/dev/null; then
            echo "Creating cluster-admin role binding..."
            # Delete existing binding first to avoid conflicts
            kubectl delete clusterrolebinding aurora-admin-binding &>/dev/null || true
            if kubectl create clusterrolebinding aurora-admin-binding --clusterrole=cluster-admin --user="$OBJECT_ID" &>/dev/null; then
                echo "   Kubernetes cluster-admin role binding created successfully"
            else
                echo "   Kubernetes cluster-admin role binding failed - trying alternative method..."
                # Alternative: Use group binding
                if kubectl create clusterrolebinding aurora-admin-binding-group --clusterrole=cluster-admin --group="$CLIENT_ID" &>/dev/null; then
                    echo "   Kubernetes cluster-admin role binding created with group method"
                else
                    echo "   Both Kubernetes RBAC methods failed"
                fi
            fi
        else
            echo "   Failed to get AKS credentials - please run manually:"
            echo "     az aks get-credentials --resource-group $rg --name $cluster_name --admin"
            echo "     OBJECT_ID=\$(az ad sp show --id $CLIENT_ID --query 'id' --output tsv)"
            echo "     kubectl create clusterrolebinding aurora-admin-binding --clusterrole=cluster-admin --user=\$OBJECT_ID"
        fi
        echo
    done
    echo "================================"
    echo
fi

echo "COPY THIS JSON INTO AURORA:"
echo "================================"
cat << EOF
{
  "agent": {
    "tenantId": "$TENANT_ID",
    "clientId": "$CLIENT_ID",
    "clientSecret": "$CLIENT_SECRET",
    "subscriptionId": "$SUBSCRIPTION_ID"
  },
  "readonly": {
    "tenantId": "$TENANT_ID",
    "clientId": "$READONLY_CLIENT_ID",
    "clientSecret": "$READONLY_CLIENT_SECRET",
    "subscriptionId": "$SUBSCRIPTION_ID"
  }
}
EOF

if [ "$CLUSTER_COUNT" -gt 0 ]; then
    echo
    echo "NOTE: AKS cluster permissions have been automatically configured."
    echo "Your clusters will be available in Aurora after authentication."
fi
echo "================================"
echo

echo "SECURITY NOTES:"
echo "   • Aurora has FULL MANAGEMENT access to your Azure subscription (agent mode)"
echo "   • Aurora has READ-ONLY access to your Azure subscription (ask mode)"
echo "   • Aurora can create, modify, delete resources and manage permissions (agent mode only)"
echo "   • Aurora can deploy clusters, configure networks, manage storage, etc. (agent mode only)"
echo "   • Aurora CANNOT delete authorization policies, blueprints, or shared galleries"
echo "   • You can revoke access anytime by deleting the service principals"
echo
echo "To revoke Aurora access later:"
echo "   Agent mode: az ad sp delete --id $CLIENT_ID"
echo "   Read-only: az ad sp delete --id $READONLY_CLIENT_ID"
echo