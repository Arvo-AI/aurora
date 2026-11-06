import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'
import { forwardRequest } from '@/lib/backend-proxy'

// DELETE /{provider}/disconnect. A new connector only needs its name here.
const DELETE_DISCONNECT_PROVIDERS = new Set([
  'bigpanda',
  'cloudbees',
  'confluence',
  'coroot',
  'datadog',
  'dynatrace',
  'elastic',
  'grafana',
  'incidentio',
  'jenkins',
  'jira',
  'netdata',
  'opsgenie',
  'rabbitmq',
  'sentry',
  'servicenow',
  'sharepoint',
  'spinnaker',
  'splunk',
  'thousandeyes',
])

// Same path, but the backend route is POST-only.
const POST_DISCONNECT_PROVIDERS = new Set(['github', 'gitlab'])

// SharePoint's Graph cleanup can outlast the default proxy timeout.
const DISCONNECT_TIMEOUT_MS: Record<string, number> = {
  sharepoint: 20_000,
}

// Backend route is not /{provider}/disconnect.
const CUSTOM_DISCONNECTS: Record<string, { method: 'DELETE' | 'POST'; path: string }> = {
  google_chat: { method: 'DELETE', path: '/google-chat' },
  ovh: { method: 'POST', path: '/ovh_api/ovh/disconnect' },
  scaleway: { method: 'POST', path: '/scaleway_api/scaleway/disconnect' },
  slack: { method: 'DELETE', path: '/slack' },
  tailscale: { method: 'POST', path: '/tailscale_api/tailscale/disconnect' },
}

// Generic delete also clears provider caches and graph nodes.
const ACCOUNT_PROVIDERS = new Set(['gcp', 'azure', 'aws', 'newrelic'])

const VALID_PROVIDERS = new Set<string>([
  ...DELETE_DISCONNECT_PROVIDERS,
  ...POST_DISCONNECT_PROVIDERS,
  ...Object.keys(CUSTOM_DISCONNECTS),
  ...ACCOUNT_PROVIDERS,
])

// ---------------------------------------------------------------------------
// DELETE /api/connected-accounts/[provider]
// Disconnects a provider by removing tokens from database
// ---------------------------------------------------------------------------
export async function DELETE(
  request: NextRequest,
  context: { params: Promise<{ provider: string }> }
) {
  try {
    const authResult = await getAuthenticatedUser()

    if (authResult instanceof NextResponse) {
      return authResult
    }

    const { userId } = authResult
    const { provider } = await context.params

    if (!VALID_PROVIDERS.has(provider)) {
      return NextResponse.json(
        { error: 'Invalid provider' },
        { status: 400 }
      )
    }

    // Path doesn't match /{provider}/disconnect.
    const custom = CUSTOM_DISCONNECTS[provider]
    if (custom) {
      return forwardRequest(request, custom.method, custom.path, `${provider}-disconnect`)
    }

    // GitHub and GitLab register disconnect as POST only.
    if (POST_DISCONNECT_PROVIDERS.has(provider)) {
      return forwardRequest(request, 'POST', `/${provider}/disconnect`, `${provider}-disconnect`)
    }

    // Query string is forwarded as-is, including Splunk's account selector.
    if (DELETE_DISCONNECT_PROVIDERS.has(provider)) {
      const timeoutMs = DISCONNECT_TIMEOUT_MS[provider]
      return forwardRequest(
        request,
        'DELETE',
        `/${provider}/disconnect`,
        `${provider}-disconnect`,
        timeoutMs ? { timeoutMs } : {},
      )
    }

    return forwardRequest(
      request,
      'DELETE',
      `/api/connected-accounts/${userId}/${provider}`,
      `${provider}-disconnect`,
    )
  } catch (err) {
    console.error('Error disconnecting provider:', err)
    return NextResponse.json(
      { error: 'Failed to disconnect provider' },
      { status: 500 },
    )
  }
}
