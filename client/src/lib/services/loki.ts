'use client';

import { apiRequest } from '@/lib/services/api-client';

export interface LokiStatus {
  connected: boolean;
  baseUrl?: string;
  username?: string;
  labelCount?: number;
  error?: string;
}

export interface LokiConnectPayload {
  baseUrl: string;
  apiToken: string;
  username?: string;
}

const API_BASE = '/api/loki';

export const lokiService = {
  async getStatus(): Promise<LokiStatus | null> {
    try {
      const raw = await apiRequest<Record<string, any>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(raw?.connected),
        baseUrl: raw?.baseUrl ?? raw?.base_url,
        username: raw?.username,
        labelCount: raw?.labelCount ?? raw?.label_count,
        error: raw?.error,
      };
    } catch (error) {
      console.error('[lokiService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: LokiConnectPayload): Promise<LokiStatus> {
    const raw = await apiRequest<Record<string, any>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success ?? true),
      baseUrl: raw?.baseUrl ?? payload.baseUrl,
      username: raw?.username ?? payload.username,
      labelCount: raw?.labelCount,
    };
  },
};
