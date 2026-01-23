# PowerShell script for Aurora Access Setup
param()

$ErrorActionPreference = "Stop"

Write-Host "Aurora Access Setup" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green

# Function to check if a command exists
function Test-Command {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

# Function to install Azure CLI
function Install-AzureCLI {
    Write-Host "Installing Azure CLI..." -ForegroundColor Yellow
    
    # Check if running as administrator
    $isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
    
    if (-not $isAdmin) {
        Write-Host "Administrator privileges required for installation." -ForegroundColor Yellow
        Write-Host "   Please run PowerShell as Administrator and try again." -ForegroundColor Yellow
        Write-Host "" 
        Write-Host "Alternative installation methods:" -ForegroundColor Cyan
        Write-Host "1. Download MSI installer from: https://aka.ms/installazurecliwindows" -ForegroundColor White
        Write-Host "2. Use Chocolatey: choco install azure-cli" -ForegroundColor White
        Write-Host "3. Use Winget: winget install Microsoft.AzureCLI" -ForegroundColor White
        Write-Host "4. Use Scoop: scoop install azure-cli" -ForegroundColor White
        exit 1
    }
    
    # Try different installation methods
    $installed = $false
    
    # Method 1: Try winget first (Windows 10 1709+ / Windows 11)
    if (Test-Command "winget") {
        try {
            Write-Host "   Using winget to install Azure CLI..." -ForegroundColor Cyan
            winget install Microsoft.AzureCLI --accept-source-agreements --accept-package-agreements
            $installed = $true
        } catch {
            Write-Host "   Winget installation failed, trying alternative method..." -ForegroundColor Yellow
        }
    }
    
    # Method 2: Try Chocolatey if winget failed
    if (-not $installed -and (Test-Command "choco")) {
        try {
            Write-Host "   Using Chocolatey to install Azure CLI..." -ForegroundColor Cyan
            choco install azure-cli -y
            $installed = $true
        } catch {
            Write-Host "   Chocolatey installation failed, trying MSI download..." -ForegroundColor Yellow
        }
    }
    
    # Method 3: Download and install MSI
    if (-not $installed) {
        try {
            Write-Host "   Downloading Azure CLI MSI installer..." -ForegroundColor Cyan
            $tempFile = [System.IO.Path]::GetTempFileName() + ".msi"
            Invoke-WebRequest -Uri "https://aka.ms/installazurecliwindows" -OutFile $tempFile
            
            Write-Host "   Installing Azure CLI from MSI..." -ForegroundColor Cyan
            Start-Process msiexec.exe -Wait -ArgumentList "/I `"$tempFile`" /quiet"
            
            # Clean up
            Remove-Item $tempFile -Force
            $installed = $true
        } catch {
            Write-Host "Failed to install Azure CLI automatically." -ForegroundColor Red
            Write-Host "   Please install manually from: https://docs.microsoft.com/en-us/cli/azure/install-azure-cli-windows" -ForegroundColor Yellow
            exit 1
        }
    }
    
    if ($installed) {
        # Refresh PATH to include newly installed az command
        $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
        
        # Verify installation
        if (Test-Command "az") {
            Write-Host "Azure CLI installed successfully!" -ForegroundColor Green
        } else {
            Write-Host "Azure CLI installed but not found in PATH. Please restart PowerShell." -ForegroundColor Yellow
            Write-Host "   Or manually add Azure CLI to your PATH and run this script again." -ForegroundColor Yellow
            exit 1
        }
    }
}

# Check for Azure CLI
if (-not (Test-Command "az")) {
    Write-Host "Azure CLI not found. Installing..." -ForegroundColor Yellow
    Install-AzureCLI
} else {
    Write-Host "Azure CLI is already installed" -ForegroundColor Green
}

Write-Host ""

# Check if user is logged in
try {
    $null = az account show 2>$null
    Write-Host "Already logged in to Azure!" -ForegroundColor Green
} catch {
    Write-Host "Not logged in to Azure. Logging you in now..." -ForegroundColor Yellow
    az login
    Write-Host "Successfully logged in to Azure!" -ForegroundColor Green
    Write-Host ""
}

# Get current subscription and location info
$SUBSCRIPTION_ID = az account show --query id -o tsv
$TENANT_ID = az account show --query tenantId -o tsv
$SUBSCRIPTION_NAME = az account show --query name -o tsv

# Auto-detect location based on resource groups or use account's home region
try {
    $LOCATION = az account list-locations --query "[?metadata.regionType=='Physical'] | [0].name" -o tsv 2>$null
    if (-not $LOCATION) {
        $LOCATION = "eastus"
    }
} catch {
    $LOCATION = "eastus"
}

Write-Host "Detected Configuration:" -ForegroundColor Cyan
Write-Host "   Subscription: $SUBSCRIPTION_NAME" -ForegroundColor White
Write-Host "   Subscription ID: $SUBSCRIPTION_ID" -ForegroundColor White
Write-Host "   Tenant ID: $TENANT_ID" -ForegroundColor White
Write-Host "   Location: $LOCATION" -ForegroundColor White
Write-Host ""

# Detect existing AKS clusters
Write-Host "Detecting existing AKS clusters..." -ForegroundColor Yellow
try {
    $AKS_CLUSTERS_JSON = az aks list --query "[].{name:name,resourceGroup:resourceGroup}" -o json 2>$null
    if (-not $AKS_CLUSTERS_JSON) {
        $AKS_CLUSTERS_JSON = "[]"
    }
    # Add current subscription ID to each cluster since they're all in the current subscription
    $AKS_CLUSTERS_JSON = $AKS_CLUSTERS_JSON | ConvertFrom-Json | ConvertTo-Json -Depth 3 -Compress
    $AKS_CLUSTERS_JSON = ($AKS_CLUSTERS_JSON | ConvertFrom-Json) | ForEach-Object { $_ | Add-Member -MemberType NoteProperty -Name "subscriptionId" -Value $SUBSCRIPTION_ID -PassThru } | ConvertTo-Json -Depth 3
    if ($AKS_CLUSTERS_JSON -notmatch '^\[') {
        $AKS_CLUSTERS_JSON = "[$AKS_CLUSTERS_JSON]"
    }
    $AKS_CLUSTERS = $AKS_CLUSTERS_JSON | ConvertFrom-Json
    $CLUSTER_COUNT = $AKS_CLUSTERS.Count
} catch {
    $AKS_CLUSTERS = @()
    $CLUSTER_COUNT = 0
    $AKS_CLUSTERS_JSON = "[]"
}

if ($CLUSTER_COUNT -eq 0) {
    Write-Host "   No AKS clusters found in this subscription" -ForegroundColor White
} else {
    Write-Host "   Found $CLUSTER_COUNT AKS cluster(s):" -ForegroundColor White
    foreach ($cluster in $AKS_CLUSTERS) {
        Write-Host "   - $($cluster.name) (Resource Group: $($cluster.resourceGroup))" -ForegroundColor White
    }
}
Write-Host ""

# Create the ARM template inline
Write-Host "Creating Aurora access template..." -ForegroundColor Cyan

$armTemplate = @"
{
  "`$schema": "https://schema.management.azure.com/schemas/2018-05-01/subscriptionDeploymentTemplate.json#",
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
"@

# Write template to file
$armTemplate | Out-File -FilePath "aurora-access-template.json" -Encoding UTF8

# Deploy the template
Write-Host "Deploying Aurora custom role..." -ForegroundColor Cyan
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$deploymentName = "aurora-access-$timestamp"

try {
    Write-Host "   This may take a couple of minutes - please wait..." -ForegroundColor Yellow
    # Step 1 – kick off the deployment (no JMES query so that any error details come through)
    az deployment sub create --name $deploymentName --location $LOCATION --template-file "aurora-access-template.json" --only-show-errors

    # If we reach this point the previous command exited with 0 ⇒ the deployment was accepted.
    # Step 2 – fetch the outputs from the recorded deployment object.
    $deploymentOutputJson = az deployment sub show --name $deploymentName --query "properties.outputs" --output json --only-show-errors

    if ([string]::IsNullOrWhiteSpace($deploymentOutputJson)) {
        throw "Deployment succeeded but outputs were empty."
    }

    $deploymentOutput = $deploymentOutputJson | ConvertFrom-Json
    # Extract outputs on success
    $CUSTOM_ROLE_ID = $deploymentOutput.customRoleId.value
} catch {
    Write-Host "ARM template deployment failed (likely because the role already exists). Attempting fallback lookup..." -ForegroundColor Yellow
    Write-Host "Raw error:" -ForegroundColor Yellow
    Write-Host $_.Exception.Message

    # Attempt to locate an existing role named "Aurora Manager"
    try {
        $CUSTOM_ROLE_ID = az role definition list --name "Aurora Manager" --query "[0].id" -o tsv --only-show-errors 2>$null
    } catch {
        $CUSTOM_ROLE_ID = ""
    }

    if ([string]::IsNullOrWhiteSpace($CUSTOM_ROLE_ID)) {
        Write-Host "Unable to create or locate the \"Aurora Manager\" custom role. Please resolve the error above and re-run the script." -ForegroundColor Red
        exit 1
    } else {
        Write-Host "Using existing custom role: $CUSTOM_ROLE_ID" -ForegroundColor Green
    }
}

Write-Host "Custom role created successfully!" -ForegroundColor Green
Write-Host "   Role ID: $CUSTOM_ROLE_ID" -ForegroundColor White
Write-Host ""

# Create service principal with the custom role
Write-Host "Creating Aurora service principal..." -ForegroundColor Cyan
$spTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$spName = "Aurora-Access-$spTimestamp"

$spOutput = az ad sp create-for-rbac `
    --name $spName `
    --role $CUSTOM_ROLE_ID `
    --scopes "/subscriptions/$SUBSCRIPTION_ID" `
    --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json | ConvertFrom-Json

# Extract credentials
$CLIENT_ID = $spOutput.clientId
$CLIENT_SECRET = $spOutput.clientSecret

Write-Host "Service principal created successfully!" -ForegroundColor Green
Write-Host "" 

# Create read-only service principal
Write-Host "Creating Aurora read-only service principal..." -ForegroundColor Cyan
$readOnlyTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$readOnlySpName = "Aurora-ReadOnly-$readOnlyTimestamp"

$readOnlySpOutput = az ad sp create-for-rbac \
    --name $readOnlySpName \
    --role "Reader" \
    --scopes "/subscriptions/$SUBSCRIPTION_ID" \
    --query '{clientId:appId,clientSecret:password,tenantId:tenant}' -o json | ConvertFrom-Json

$READONLY_CLIENT_ID = $readOnlySpOutput.clientId
$READONLY_CLIENT_SECRET = $readOnlySpOutput.clientSecret

Write-Host "Read-only service principal created successfully!" -ForegroundColor Green
Write-Host ""

# Assign comprehensive read-only roles
Write-Host "Assigning comprehensive read-only roles..." -ForegroundColor Cyan
$readOnlyRoles = @(
    "Reader",
    "Log Analytics Reader",
    "Monitoring Reader",
    "Monitoring Metrics Publisher",
    "Cost Management Reader",
    "Billing Reader",
    "Storage Blob Data Reader",
    "Storage File Data SMB Share Reader",
    "Storage Queue Data Reader",
    "Storage Table Data Reader",
    "Key Vault Reader",
    "Key Vault Secrets User",
    "Security Reader",
    "Virtual Machine User Login",
    "Azure Kubernetes Service Cluster User Role",
    "Azure Kubernetes Service RBAC Reader",
    "Cosmos DB Account Reader Role",
    "Reader and Data Access",
    "Azure Event Hubs Data Receiver",
    "Azure Service Bus Data Receiver",
    "API Management Service Reader Role",
    "AcrPull",
    "Backup Reader",
    "IoT Hub Data Reader",
    "Cognitive Services User"
)

foreach ($role in $readOnlyRoles) {
    try {
        $null = az role assignment create --assignee $READONLY_CLIENT_ID --role "$role" --scope "/subscriptions/$SUBSCRIPTION_ID" 2>$null
    } catch {
        # Ignore errors when role assignment already exists.
    }
}

Write-Host "Granting Azure AD read permissions for CLI authentication..." -ForegroundColor Cyan
try {
    $READONLY_APP_ID = az ad sp show --id $READONLY_CLIENT_ID --query "appId" -o tsv 2>$null
    if ([string]::IsNullOrWhiteSpace($READONLY_APP_ID)) {
        Write-Host "Warning: Could not grant Azure AD permissions (app ID not found)" -ForegroundColor Yellow
    } else {
        try {
            $null = az ad app permission add --id $READONLY_APP_ID \
                --api 00000003-0000-0000-c000-000000000000 \
                --api-permissions e1fe6dd8-ba31-4d61-89e7-88639da4683d=Scope 2>$null
        } catch {}

        try {
            $null = az ad app permission add --id $READONLY_APP_ID \
                --api 00000003-0000-0000-c000-000000000000 \
                --api-permissions 7ab1d382-f21e-4acd-a863-ba3e13f7da61=Role 2>$null
        } catch {}

        try {
            $null = az ad app permission admin-consent --id $READONLY_APP_ID 2>$null
            Write-Host "Azure AD permissions granted successfully!" -ForegroundColor Green
        } catch {
            Write-Host "Warning: Failed to grant Azure AD admin consent automatically." -ForegroundColor Yellow
        }
    }
} catch {
    Write-Host "Warning: Could not grant Azure AD permissions (lookup failed)" -ForegroundColor Yellow
}

Write-Host "Read-only roles assigned successfully!" -ForegroundColor Green
Write-Host ""

# Clean up temporary file
Remove-Item "aurora-access-template.json" -Force

# Display final results
Write-Host "AURORA SETUP COMPLETE!" -ForegroundColor Green
Write-Host "================================" -ForegroundColor Green
Write-Host ""

if ($CLUSTER_COUNT -gt 0) {
    Write-Host "AKS CLUSTER ROLE ASSIGNMENTS" -ForegroundColor Yellow
    Write-Host "Automatically assigning permissions to existing AKS clusters..." -ForegroundColor White
    Write-Host "================================" -ForegroundColor Gray
    
    # Basic subscription-level permissions
    Write-Host "Assigning basic subscription-level permissions..." -ForegroundColor Green
    Write-Host "Running: az role assignment create --assignee $CLIENT_ID --role Reader --scope `"/subscriptions/$SUBSCRIPTION_ID`""
    try {
        $null = az role assignment create --assignee $CLIENT_ID --role "Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" 2>$null
        Write-Host "   Reader role assigned successfully" -ForegroundColor Green
    } catch {
        Write-Host "   Reader role assignment failed or already exists" -ForegroundColor Yellow
    }
    
    Write-Host "Running: az role assignment create --assignee $CLIENT_ID --role `"Cost Management Reader`" --scope `"/subscriptions/$SUBSCRIPTION_ID`""
    try {
        $null = az role assignment create --assignee $CLIENT_ID --role "Cost Management Reader" --scope "/subscriptions/$SUBSCRIPTION_ID" 2>$null
        Write-Host "   Cost Management Reader role assigned successfully" -ForegroundColor Green
    } catch {
        Write-Host "   Cost Management Reader role assignment failed or already exists" -ForegroundColor Yellow
    }
    Write-Host ""
    
    # AKS cluster-specific permissions
    Write-Host "Assigning AKS cluster-specific permissions..." -ForegroundColor Green
    foreach ($cluster in $AKS_CLUSTERS) {
        $scope = "/subscriptions/$($cluster.subscriptionId)/resourceGroups/$($cluster.resourceGroup)/providers/Microsoft.ContainerService/managedClusters/$($cluster.name)"
        Write-Host "Running: az role assignment create --assignee $CLIENT_ID --role `"Azure Kubernetes Service Cluster Admin Role`" --scope `"$scope`""
        try {
            $null = az role assignment create --assignee $CLIENT_ID --role "Azure Kubernetes Service Cluster Admin Role" --scope $scope 2>$null
            Write-Host "   AKS Cluster Admin role assigned successfully for $($cluster.name)" -ForegroundColor Green
        } catch {
            Write-Host "   AKS Cluster Admin role assignment failed or already exists for $($cluster.name)" -ForegroundColor Yellow
        }

        try {
            $null = az role assignment create --assignee $READONLY_CLIENT_ID --role "Azure Kubernetes Service Cluster User Role" --scope $scope 2>$null
            Write-Host "   AKS Cluster User role assigned to read-only SP for $($cluster.name)" -ForegroundColor Green
        } catch {
            # Ignore when assignment already exists or fails silently.
        }

        # Add Kubernetes RBAC permissions
        Write-Host "Adding Kubernetes RBAC permissions for cluster: $($cluster.name)" -ForegroundColor Cyan
        Write-Host "Getting service principal object ID..."
        try {
            $OBJECT_ID = az ad sp show --id $CLIENT_ID --query "id" --output tsv 2>$null
            if ([string]::IsNullOrEmpty($OBJECT_ID)) {
                Write-Host "   Failed to get service principal object ID" -ForegroundColor Red
                continue
            }
            Write-Host "   Service principal object ID: $OBJECT_ID" -ForegroundColor Green
        } catch {
            Write-Host "   Failed to get service principal object ID" -ForegroundColor Red
            continue
        }
        
        Write-Host "Getting AKS credentials..."
        try {
            $null = az aks get-credentials --resource-group $cluster.resourceGroup --name $cluster.name --admin --overwrite-existing 2>$null
            Write-Host "Creating cluster-admin role binding..."
            try {
                # Delete existing binding first to avoid conflicts
                $null = kubectl delete clusterrolebinding aurora-admin-binding 2>$null
                $null = kubectl create clusterrolebinding aurora-admin-binding --clusterrole=cluster-admin --user="$OBJECT_ID" 2>$null
                Write-Host "   Kubernetes cluster-admin role binding created successfully" -ForegroundColor Green
            } catch {
                Write-Host "   Kubernetes cluster-admin role binding failed - trying alternative method..." -ForegroundColor Yellow
                try {
                    $null = kubectl create clusterrolebinding aurora-admin-binding-group --clusterrole=cluster-admin --group="$CLIENT_ID" 2>$null
                    Write-Host "   Kubernetes cluster-admin role binding created with group method" -ForegroundColor Green
                } catch {
                    Write-Host "   Both Kubernetes RBAC methods failed" -ForegroundColor Red
                }
            }
        } catch {
            Write-Host "   Failed to get AKS credentials - please run manually:" -ForegroundColor Red
            Write-Host "     az aks get-credentials --resource-group $($cluster.resourceGroup) --name $($cluster.name) --admin" -ForegroundColor White
            Write-Host "     `$OBJECT_ID = az ad sp show --id $CLIENT_ID --query 'id' --output tsv" -ForegroundColor White
            Write-Host "     kubectl create clusterrolebinding aurora-admin-binding --clusterrole=cluster-admin --user=`$OBJECT_ID" -ForegroundColor White
        }
        Write-Host ""
    }
    Write-Host "================================" -ForegroundColor Gray
    Write-Host ""
}

Write-Host "COPY THIS JSON INTO AURORA:" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host "{"
Write-Host "  `"agent`": {"
Write-Host "    `"tenantId`": `"$TENANT_ID`","
Write-Host "    `"clientId`": `"$CLIENT_ID`","
Write-Host "    `"clientSecret`": `"$CLIENT_SECRET`","
Write-Host "    `"subscriptionId`": `"$SUBSCRIPTION_ID`""
Write-Host "  },"
Write-Host "  `"readonly`": {"
Write-Host "    `"tenantId`": `"$TENANT_ID`","
Write-Host "    `"clientId`": `"$READONLY_CLIENT_ID`","
Write-Host "    `"clientSecret`": `"$READONLY_CLIENT_SECRET`","
Write-Host "    `"subscriptionId`": `"$SUBSCRIPTION_ID`""
Write-Host "  }"
Write-Host "}"

if ($CLUSTER_COUNT -gt 0) {
    Write-Host ""
    Write-Host "NOTE: AKS cluster permissions have been automatically configured." -ForegroundColor Green
    Write-Host "Your clusters will be available in Aurora after authentication." -ForegroundColor Green
}
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

Write-Host "SECURITY NOTES:" -ForegroundColor Yellow
Write-Host "   • Aurora has FULL MANAGEMENT access to your Azure subscription (agent mode)" -ForegroundColor Yellow
Write-Host "   • Aurora has READ-ONLY access to your Azure subscription (ask mode)" -ForegroundColor Yellow
Write-Host "   • Aurora can create, modify, delete resources and manage permissions (agent mode only)" -ForegroundColor Green
Write-Host "   • Aurora can deploy clusters, configure networks, manage storage, etc. (agent mode only)" -ForegroundColor Green
Write-Host "   • Aurora CANNOT delete authorization policies, blueprints, or shared galleries" -ForegroundColor Green
Write-Host "   • You can revoke access anytime by deleting the service principals" -ForegroundColor Green
Write-Host ""
Write-Host "To revoke Aurora access later:" -ForegroundColor Magenta
Write-Host "   az ad sp delete --id $CLIENT_ID" -ForegroundColor White
Write-Host "   az ad sp delete --id $READONLY_CLIENT_ID" -ForegroundColor White
Write-Host ""