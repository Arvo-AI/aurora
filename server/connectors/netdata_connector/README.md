# Netdata Connector

API Token authentication for Netdata Cloud.

## Setup

### 1. Create API Token

1. Go to [Netdata Cloud](https://app.netdata.cloud/) > avatar > **Account Settings** > **API Tokens**
2. Click **Create Token**, name it `Aurora`, copy the token

### 2. Configure `.env`

> API tokens are entered by users via the UI. No environment variables required.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/netdata/alerts/webhook/{user_id}`

In Netdata Cloud: **Space settings** > **Alert notifications** > **Add configuration**
- Method: `Webhook`, URL: Aurora webhook URL

## Troubleshooting

**Netdata connector not enabled** — Restart Aurora and verify the connector appears in the UI.
