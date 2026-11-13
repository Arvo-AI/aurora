# OVH Connector

OAuth 2.0 authentication for OVH Cloud (multi-region).

## Setup

### 1. Create OAuth App in OVH

> **Important**: OVH OAuth2 only accepts **HTTPS** callback URLs. For local development, use a tunnel service like [ngrok](https://ngrok.com/) or [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/local/) to expose your local server with HTTPS. For production, use your production HTTPS URL.

1. Go to the API console for your region:
   - EU: https://eu.api.ovh.com/console/
   - CA: https://ca.api.ovh.com/console/
   - US: https://us.api.ovh.com/console/
2. Authenticate with your OVH account
3. Navigate to `/me` > `/me/api/oauth2/client` and use **POST** to create a new OAuth2 client with this format:
   ```json
   {
     "callbackUrls": [
       "https://your-ngrok-url.ngrok-free.dev/ovh_api/ovh/oauth2/callback"
     ],
     "description": "Aurora Cloud Platform",
     "flow": "AUTHORIZATION_CODE",
     "name": "Aurora"
   }
   ```
   - For local dev: Replace with your ngrok tunnel URL (e.g., `https://your-ngrok-url.ngrok-free.dev/ovh_api/ovh/oauth2/callback`)
   - For production: Use your production URL (e.g., `https://aurora.example.com/ovh_api/ovh/oauth2/callback`)
4. Copy the **Client ID** and **Client Secret** from the response

### 2. Configure `.env`

```bash
# Europe region (configure at least one region)
OVH_EU_CLIENT_ID=your-eu-client-id
OVH_EU_CLIENT_SECRET=your-eu-client-secret

# Canada region (optional)
OVH_CA_CLIENT_ID=your-ca-client-id
OVH_CA_CLIENT_SECRET=your-ca-client-secret

# US region (optional)
OVH_US_CLIENT_ID=your-us-client-id
OVH_US_CLIENT_SECRET=your-us-client-secret
```

There is no redirect-URI variable. Aurora builds the callback as
`{NEXT_PUBLIC_BACKEND_URL}/ovh_api/ovh/oauth2/callback`, using `NGROK_URL` instead
when `NEXT_PUBLIC_BACKEND_URL` starts with `http://localhost`.

### Local Development with Ngrok

To run locally with OVH OAuth:

1. Start ngrok tunnel:
   ```bash
   ngrok http 5080
   ```
2. Copy the generated HTTPS URL (e.g., `https://your-ngrok-url.ngrok-free.dev`)
3. Set `NGROK_URL` in `.env` to the tunnel URL, then recreate the server so it picks up the new value (`docker compose up -d --force-recreate aurora-server`, adding `-f docker-compose.prod-local.yml` if you run that file). Editing `.env` alone does not change a running container.
4. Update your OVH OAuth app callback URL via the API console if needed

**Alternative**: You can also use [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-apps/install-and-setup/tunnel-guide/local/) instead of ngrok.

## Troubleshooting

**"OAuth2 credentials not configured for [region]"** — Set `OVH_[REGION]_CLIENT_ID` and `OVH_[REGION]_CLIENT_SECRET`
