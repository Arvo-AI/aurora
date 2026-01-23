# Azure Connector

Service Principal authentication for Microsoft Azure.

## Setup

### 1. Create App Registration

1. Go to [Azure Portal > App registrations](https://portal.azure.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)
2. Click **+ New registration**
   - Name: `Aurora`
   - Redirect URI: Web > `http://localhost:5000/azure/callback`
3. Copy the **Application (client) ID** and **Directory (tenant) ID**
4. Go to **Certificates & secrets** > **+ New client secret**
5. Copy the secret **Value** immediately

### 2. Grant Permissions

1. Go to **API permissions** > **+ Add a permission**
2. Select **Azure Service Management** > **user_impersonation**
3. Click **Grant admin consent**

### 3. Assign Role to Subscription

1. Go to [Subscriptions](https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBlade)
2. Select subscription > **Access control (IAM)** > **+ Add role assignment**
3. Role: **Contributor** (or Reader for read-only)
4. Assign to your `Aurora` app

## Troubleshooting

**"No enabled subscription found"** — Assign Contributor/Reader role to the app in your subscription's IAM
