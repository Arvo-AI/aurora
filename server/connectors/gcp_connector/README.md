# GCP Connector

OAuth 2.0 authentication for Google Cloud Platform.

## Setup

### 1. Create OAuth Credentials

1. Go to [GCP Console > Credentials](https://console.cloud.google.com/apis/credentials)
2. Configure **OAuth consent screen** (if first time):
   - User Type: External
   - App name: `Aurora`
   - Add your email as a test user
3. Click **+ CREATE CREDENTIALS** > **OAuth client ID**
   - Type: Web application
   - Redirect URI: `http://localhost:5000/callback`
4. Copy the **Client ID** and **Client Secret**

### 2. Configure `.env`

```bash
CLIENT_ID=your-client-id.apps.googleusercontent.com
CLIENT_SECRET=your-client-secret
```

## Troubleshooting

**"Redirect URI mismatch"** — Ensure `NEXT_PUBLIC_BACKEND_URL` matches exactly what's configured in GCP Console
