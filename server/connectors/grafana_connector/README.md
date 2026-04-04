# Grafana Connector

Webhook-based connection for Grafana Cloud or self-hosted instances.

## How It Works

Aurora receives alerts from Grafana via webhook. No API key is needed.

1. Open the Grafana integration page in Aurora
2. Copy the webhook URL shown on screen
3. In Grafana: **Alerts & IRM** > **Notification Configuration** > **Contact points** > **New contact point**
   - Type: `Webhook`, URL: the Aurora webhook URL
4. Click **Test** to send a test notification
5. Aurora auto-connects when it receives the first webhook

## Webhook URL Format

`https://your-aurora-domain/grafana/alerts/webhook/{user_id}`

## Notification Policies

After creating the contact point, route alerts to it under **Alerting** > **Notification policies**.

## Disconnect / Reconnect

Disconnecting in Aurora deactivates the connection. Incoming webhooks are
rejected until the user clicks **Reconnect**. The Grafana contact point
does not need to be reconfigured.

## Troubleshooting

**Grafana not connecting** -- Ensure the webhook URL is correct and Aurora is reachable from Grafana. Send a test notification from the contact point.

**Webhooks rejected after disconnect** -- Click Reconnect in Aurora to re-enable the connection.
