# Grafana Connector

API Token authentication for Grafana Cloud or self-hosted.

## Setup

### 1. Create API Token

**Grafana Cloud:**
1. Go to [Grafana Cloud](https://grafana.com/) > your stack
2. **Administration** > **Service accounts** > **Add service account**
   - Name: `Aurora`, Role: `Viewer`
3. **Add service account token** > copy the token


> API tokens are entered by users via the UI.

## Webhook Configuration

Webhook URL format: `https://your-aurora-domain/grafana/alerts/webhook/{user_id}`

In Grafana: **Alerting** > **Contact points** > **+ Add contact point**
- Type: `Webhook`, URL: Aurora webhook URL

Then in **Notification policies**, route alerts to the Aurora contact point.

## Troubleshooting

**Grafana connector not working** — Check that the API token is correctly configured in the UI
