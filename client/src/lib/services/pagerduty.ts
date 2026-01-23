'use client';

export interface PagerDutyStatus {
  connected: boolean;
  displayName?: string;
  externalUserEmail?: string;
  externalUserName?: string;
  accountSubdomain?: string;
  validatedAt?: string;
  authType?: 'api_token' | 'oauth';
  capabilities?: {
    can_read_incidents: boolean;
    can_write_incidents: boolean;
  };
}

const API_BASE = '/api/pagerduty';

async function apiCall<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
    cache: 'no-store',
  });

  if (!response.ok) {
    const err = await response.json().catch(() => ({}));
    throw new Error(err.error || err.message || response.statusText);
  }

  return response.json();
}

export const pagerdutyService = {
  async getStatus(): Promise<PagerDutyStatus | null> {
    try {
      return await apiCall<PagerDutyStatus>(API_BASE);
    } catch {
      return null;
    }
  },

  async connect(token: string, displayName = 'PagerDuty'): Promise<PagerDutyStatus> {
    return apiCall<PagerDutyStatus>(API_BASE, {
      method: 'POST',
      body: JSON.stringify({ token, displayName }),
    });
  },

  async oauthLogin(): Promise<{ oauth_url: string }> {
    return apiCall(`${API_BASE}/oauth/login`, { method: 'POST', body: '{}' });
  },

  async changeToken(token: string): Promise<PagerDutyStatus> {
    return apiCall<PagerDutyStatus>(API_BASE, {
      method: 'PATCH',
      body: JSON.stringify({ token }),
    });
  },

  async disconnect(): Promise<void> {
    await apiCall(API_BASE, { method: 'DELETE' });
  },
};
