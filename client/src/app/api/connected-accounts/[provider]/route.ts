import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

const API_BASE_URL = process.env.BACKEND_URL

const VALID_PROVIDERS = new Set([
  'gcp', 'azure', 'aws', 'github', 'grafana', 'datadog', 'netdata',
  'ovh', 'scaleway', 'tailscale', 'slack', 'splunk', 'dynatrace',
  'confluence', 'sharepoint', 'coroot', 'thousandeyes', 'jenkins',
  'cloudbees', 'bigpanda', 'spinnaker', 'newrelic',
])

// Providers with non-standard disconnect endpoints or methods.
// Everything else uses DELETE /{provider}/disconnect.
const PROVIDER_OVERRIDES: Record<string, { url: string; method: string }> = {
  slack:     { url: '/slack',                           method: 'DELETE' },
  ovh:       { url: '/ovh_api/ovh/disconnect',          method: 'POST' },
  scaleway:  { url: '/scaleway_api/scaleway/disconnect', method: 'POST' },
  tailscale: { url: '/tailscale_api/tailscale/disconnect', method: 'POST' },
  github:    { url: '/github/disconnect',               method: 'POST' },
}

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

    const { userId, headers: authHeaders } = authResult
    const { provider } = await context.params

    if (!VALID_PROVIDERS.has(provider)) {
      return NextResponse.json(
        { error: 'Invalid provider' },
        { status: 400 }
      )
    }

    // SharePoint needs a longer timeout
    if (provider === 'sharepoint') {
      const controller = new AbortController()
      const timeoutId = setTimeout(() => controller.abort(), 20000)
      let response: Response
      try {
        response = await fetch(`${API_BASE_URL}/sharepoint/disconnect`, {
          method: 'DELETE',
          headers: authHeaders,
          signal: controller.signal,
        })
      } finally {
        clearTimeout(timeoutId)
      }

      if (!response.ok) {
        console.error('Backend error disconnecting SharePoint: status=%d', response.status)
        return NextResponse.json(
          { error: 'Failed to disconnect SharePoint' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Resolve endpoint: use override if one exists, otherwise standard pattern
    const override = PROVIDER_OVERRIDES[provider]
    const url = override
      ? `${API_BASE_URL}${override.url}`
      : `${API_BASE_URL}/${provider}/disconnect`
    const method = override?.method ?? 'DELETE'

    const response = await fetch(url, { method, headers: authHeaders })

    if (!response.ok) {
      const errorText = await response.text()
      console.error(`Backend error disconnecting ${provider}:`, errorText)
      return NextResponse.json(
        { error: `Failed to disconnect ${provider}` },
        { status: response.status }
      )
    }

    const data = await response.json()
    return NextResponse.json(data)
  } catch (err) {
    console.error('Error disconnecting provider:', err)
    return NextResponse.json(
      { error: 'Failed to disconnect provider' },
      { status: 500 },
    )
  }
}
