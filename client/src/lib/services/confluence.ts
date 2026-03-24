import { apiRequest, apiDelete } from '@/lib/services/api-client';

export interface ConfluenceStatus {
  connected: boolean;
  authType?: 'oauth' | 'pat';
  baseUrl?: string;
  cloudId?: string | null;
  userDisplayName?: string | null;
  userEmail?: string | null;
  error?: string;
}

export interface ConfluenceConnectPayload {
  authType: 'oauth' | 'pat';
  baseUrl?: string;
  patToken?: string;
  code?: string;
  state?: string;
}

export interface ConfluenceConnectResponse extends ConfluenceStatus {
  success?: boolean;
  authUrl?: string;
}

export interface ConfluenceFetchResponse {
  id?: string;
  title?: string;
  body?: Record<string, unknown>;
  [key: string]: unknown;
}

const API_BASE = '/api/confluence';

export const confluenceService = {
  async getStatus(init?: RequestInit): Promise<ConfluenceStatus | null> {
    return apiRequest<ConfluenceStatus | null>(`${API_BASE}/status`, {
      ...init,
      cache: 'no-store',
    });
  },

  async connect(payload: ConfluenceConnectPayload): Promise<ConfluenceConnectResponse | null> {
    return apiRequest<ConfluenceConnectResponse | null>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
  },

  async disconnect(): Promise<void> {
    await apiDelete('/api/connected-accounts/confluence', { cache: 'no-store' });
  },

  async fetchPage(url: string): Promise<ConfluenceFetchResponse | null> {
    return apiRequest<ConfluenceFetchResponse | null>(`${API_BASE}/fetch`, {
      method: 'POST',
      body: JSON.stringify({ url }),
      cache: 'no-store',
    });
  },
};
