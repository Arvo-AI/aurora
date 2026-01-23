# Netdata Connector

API Token authentication for Netdata Cloud.

## Setup

### 1. Create API Token

1. Go to [Netdata Cloud](https://app.netdata.cloud/) > avatar > **Account Settings** > **API Tokens**
2. Click **Create Token**, name it `Aurora`, copy the token

### 2. Configure `.env`

```bash
NEXT_PUBLIC_ENABLE_NETDATA=true
```

> API tokens are entered by users via the UI.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/netdata/alerts/webhook/{user_id}`

In Netdata Cloud: **Space settings** > **Alert notifications** > **Add configuration**
- Method: `Webhook`, URL: Aurora webhook URL

## Troubleshooting

**Netdata connector not enabled** — Ensure `NEXT_PUBLIC_ENABLE_NETDATA=true` and restart Aurora
