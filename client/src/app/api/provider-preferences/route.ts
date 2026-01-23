import { NextRequest, NextResponse } from 'next/server';
import { getAuthenticatedUser } from '@/lib/auth-helper';
import { isOvhEnabled } from '@/lib/feature-flags';

const API_BASE_URL = process.env.BACKEND_URL

// ---------------------------------------------------------------------------
// GET /api/provider-preferences
// Gets the user's cloud provider preferences from database
// Returns: { providers: string[] }
// ---------------------------------------------------------------------------
export async function GET() {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      return authResult; // Return the error response
    }

    const { userId, headers: authHeaders } = authResult;

    // Get provider preferences from backend database
    const response = await fetch(`${API_BASE_URL}/api/user-preferences?key=provider_preferences`, {
      method: 'GET',
      headers: authHeaders,
    });

    if (!response.ok) {
      console.error('Backend error fetching provider preferences:', await response.text());
      return NextResponse.json(
        { providers: [] }, // Return empty array as fallback
        { status: 200 }
      );
    }

    const data = await response.json();
    let providers: string[] = [];

    // Parse the provider preferences
    if (data.value) {
      try {
        if (typeof data.value === 'string') {
          providers = JSON.parse(data.value);
        } else if (Array.isArray(data.value)) {
          providers = data.value;
        }
      } catch (e) {
        console.error('Error parsing provider preferences:', e);
      }
    }

    // Ensure it's an array of valid providers
    const validProviders = ['gcp', 'azure', 'aws', 'scaleway', 'tailscale', 'grafana', 'datadog'];
    if (isOvhEnabled()) {
      validProviders.push('ovh');
    }
    providers = providers.filter(p => validProviders.includes(p));

    return NextResponse.json({ 
      providers,
      source: 'database'
    });
  } catch (err) {
    console.error('Error fetching provider preferences:', err);
    return NextResponse.json(
      { providers: [] },
      { status: 200 } // Return 200 with empty array rather than error
    );
  }
}

// ---------------------------------------------------------------------------
// POST /api/provider-preferences
// Sets the user's cloud provider preferences in database
// Body: { 
//   providers: string[], 
//   action?: 'set' | 'add' | 'remove',
//   provider?: string // for add/remove actions
// }
// ---------------------------------------------------------------------------
export async function POST(request: NextRequest) {
  try {
    const authResult = await getAuthenticatedUser();

    if (authResult instanceof NextResponse) {
      // No authentication - return error for write operations
      return NextResponse.json({ error: 'Authentication required' }, { status: 401 });
    }

    const { userId, headers: authHeaders } = authResult;

    const body = await request.json();
    const { providers, action = 'set', provider } = body;

    // Validate input
    const validProviders = ['gcp', 'azure', 'aws', 'scaleway', 'tailscale', 'grafana', 'datadog'];
    if (isOvhEnabled()) {
      validProviders.push('ovh');
    }

    if (action === 'set') {
      if (!Array.isArray(providers)) {
        return NextResponse.json(
          { error: 'providers must be an array for set action' },
          { status: 400 }
        );
      }
      
      const invalidProviders = providers.filter(p => !validProviders.includes(p));
      if (invalidProviders.length > 0) {
        return NextResponse.json(
          { error: `Invalid providers: ${invalidProviders.join(', ')}` },
          { status: 400 }
        );
      }
    } else if (action === 'add' || action === 'remove') {
      if (!provider || !validProviders.includes(provider)) {
        return NextResponse.json(
          { error: `Invalid provider for ${action} action: ${provider}` },
          { status: 400 }
        );
      }
    }

    let finalProviders: string[];

    if (action === 'set') {
      finalProviders = providers;
    } else {
      // Get current preferences first
      const currentResponse = await fetch(`${API_BASE_URL}/api/user-preferences?key=provider_preferences`, {
        method: 'GET',
        headers: authHeaders,
      });

      let currentProviders: string[] = [];
      if (currentResponse.ok) {
        const currentData = await currentResponse.json();
        if (currentData.value) {
          try {
            if (typeof currentData.value === 'string') {
              currentProviders = JSON.parse(currentData.value);
            } else if (Array.isArray(currentData.value)) {
              currentProviders = currentData.value;
            }
          } catch (e) {
            console.error('Error parsing current provider preferences:', e);
          }
        }
      }

      if (action === 'add') {
        finalProviders = currentProviders.includes(provider) 
          ? currentProviders 
          : [...currentProviders, provider];
      } else { // remove
        finalProviders = currentProviders.filter(p => p !== provider);
      }
    }

    // Store preferences in backend database
    const response = await fetch(`${API_BASE_URL}/api/user-preferences`, {
      method: 'POST',
      headers: {
        ...authHeaders,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({
        key: 'provider_preferences',
        value: finalProviders
      }),
    });

    if (!response.ok) {
      const errorText = await response.text();
      console.error('Backend error storing provider preferences:', errorText);
      return NextResponse.json(
        { error: 'Failed to store provider preferences in database' },
        { status: 500 }
      );
    }

    // Also track unselected providers for smart auto-select
    const allProviders = ['gcp', 'azure', 'aws', 'scaleway', 'tailscale', 'grafana', 'datadog'];
    if (isOvhEnabled()) {
      allProviders.push('ovh');
    }
    if (action === 'remove' || (action === 'set' && providers.length < allProviders.length)) {
      const unselectedProviders = allProviders.filter(p => !finalProviders.includes(p));
      
      if (unselectedProviders.length > 0) {
        await fetch(`${API_BASE_URL}/api/user-preferences`, {
          method: 'POST',
          headers: {
            ...authHeaders,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            key: 'unselected_providers',
            value: unselectedProviders
          }),
        });
      }
    }
    
    return NextResponse.json({ 
      success: true, 
      providers: finalProviders,
      action,
      message: `Provider preferences ${action === 'set' ? 'updated' : action === 'add' ? 'added' : 'removed'} in database`,
      source: 'database'
    });
  } catch (err) {
    console.error('Error setting provider preferences:', err);
    return NextResponse.json(
      { error: 'Failed to set provider preferences' },
      { status: 500 },
    );
  }
}
