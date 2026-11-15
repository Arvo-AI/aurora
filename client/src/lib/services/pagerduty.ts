'use client';

import { apiRequest } from '@/lib/services/api-client';

export type PagerDutyApiKeyAccess = 'user' | 'account' | 'oauth';

export interface PagerDutyStatus {
  connected: boolean;
  displayName?: string;
  externalUserEmail?: string;
  externalUserName?: string;
  externalUserRole?: string;
  accountSubdomain?: string;
  validatedAt?: string;
  authType?: 'api_token' | 'oauth';
  capabilities?: {
    can_read_incidents: boolean;
    can_write_incidents: boolean;
    api_key_access?: PagerDutyApiKeyAccess;
  };
  /** Set by connect/rotate when the new token cannot post notes and the org toggle was turned off. */
  notesDisabled?: boolean;
  /** Present when can_write_incidents is false: why, and what to do about it. */
  notesUnwritableReason?: string;
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

  /** Re-validates write capability server-side, then turns on RCA notes for the org. Throws with the server's reason. */
  async enableNotes(): Promise<{ enabled: boolean }> {
    return apiRequest<{ enabled: boolean }>(`${API_BASE}/notes/enable`, {
      method: 'POST',
      body: '{}',
      cache: 'no-store',
    });
  },
};
