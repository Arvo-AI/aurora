'use client';

import { apiRequest } from '@/lib/services/api-client';

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

export const pagerdutyService = {
  async getStatus(): Promise<PagerDutyStatus | null> {
    try {
      return await apiRequest<PagerDutyStatus>(API_BASE, { cache: 'no-store' });
    } catch {
      return null;
    }
  },

  async connect(token: string, displayName = 'PagerDuty'): Promise<PagerDutyStatus> {
    return apiRequest<PagerDutyStatus>(API_BASE, {
      method: 'POST',
      body: JSON.stringify({ token, displayName }),
      cache: 'no-store',
    });
  },

  async oauthLogin(): Promise<{ oauth_url: string }> {
    return apiRequest<{ oauth_url: string }>(`${API_BASE}/oauth/login`, {
      method: 'POST',
      body: '{}',
      cache: 'no-store',
    });
  },

  async changeToken(token: string): Promise<PagerDutyStatus> {
    return apiRequest<PagerDutyStatus>(API_BASE, {
      method: 'PATCH',
      body: JSON.stringify({ token }),
      cache: 'no-store',
    });
  },

  async disconnect(): Promise<void> {
    await apiRequest(API_BASE, { method: 'DELETE', cache: 'no-store' });
  },
};
