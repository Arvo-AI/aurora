import { NextRequest, NextResponse } from 'next/server'
import { getAuthenticatedUser } from '@/lib/auth-helper'

// Backend base URL
const API_BASE_URL = process.env.BACKEND_URL

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
      return authResult // Return the error response
    }

    const { userId, headers: authHeaders } = authResult
    // In Next.js 15 `params` is a Promise; await it before accessing its properties
    const { provider } = await context.params

    // Validate provider
    if (!['gcp', 'azure', 'aws', 'github', 'grafana', 'datadog', 'netdata', 'ovh', 'scaleway', 'tailscale', 'slack', 'splunk', 'dynatrace', 'confluence', 'coroot', 'jenkins'].includes(provider)) {
      return NextResponse.json(
        { error: 'Invalid provider' },
        { status: 400 }
      )
    }

    // Slack (uses unified /api/slack endpoint)
    if (provider === 'slack') {
      const response = await fetch(`${API_BASE_URL}/slack`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Slack:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Slack' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for OVH (uses POST instead of DELETE)
    if (provider === 'ovh') {
      const response = await fetch(`${API_BASE_URL}/ovh_api/ovh/disconnect`, {
        method: 'POST',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting OVH:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect OVH' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Scaleway (uses POST instead of DELETE)
    if (provider === 'scaleway') {
      const response = await fetch(`${API_BASE_URL}/scaleway_api/scaleway/disconnect`, {
        method: 'POST',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Scaleway:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Scaleway' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Tailscale (uses POST instead of DELETE)
    if (provider === 'tailscale') {
      const response = await fetch(`${API_BASE_URL}/tailscale_api/tailscale/disconnect`, {
        method: 'POST',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Tailscale:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Tailscale' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for GitHub
    if (provider === 'github') {
      const response = await fetch(`${API_BASE_URL}/github/disconnect`, {
        method: 'POST',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting GitHub:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect GitHub' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Grafana
    if (provider === 'grafana') {
      const response = await fetch(`${API_BASE_URL}/grafana/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Grafana:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Grafana' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Datadog
    if (provider === 'datadog') {
      const response = await fetch(`${API_BASE_URL}/datadog/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Datadog:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Datadog' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Netdata
    if (provider === 'netdata') {
      const response = await fetch(`${API_BASE_URL}/netdata/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Netdata:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Netdata' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Splunk
    if (provider === 'splunk') {
      const response = await fetch(`${API_BASE_URL}/splunk/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Splunk:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Splunk' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Dynatrace
    if (provider === 'dynatrace') {
      const response = await fetch(`${API_BASE_URL}/dynatrace/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Dynatrace:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Dynatrace' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Confluence
    if (provider === 'confluence') {
      const response = await fetch(`${API_BASE_URL}/confluence/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Confluence:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Confluence' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Jenkins
    if (provider === 'jenkins') {
      const response = await fetch(`${API_BASE_URL}/jenkins/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Jenkins:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Jenkins' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // Special handling for Coroot
    if (provider === 'coroot') {
      const response = await fetch(`${API_BASE_URL}/coroot/disconnect`, {
        method: 'DELETE',
        headers: authHeaders,
      })

      if (!response.ok) {
        const errorText = await response.text()
        console.error('Backend error disconnecting Coroot:', errorText)
        return NextResponse.json(
          { error: 'Failed to disconnect Coroot' },
          { status: response.status }
        )
      }

      const data = await response.json()
      return NextResponse.json(data)
    }

    // For other providers, use the general disconnect endpoint
    const response = await fetch(`${API_BASE_URL}/api/connected-accounts/${userId}/${provider}`, {
      method: 'DELETE',
      headers: authHeaders,
    })

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
