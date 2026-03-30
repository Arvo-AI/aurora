'use client';

import { apiRequest } from '@/lib/services/api-client';

export interface LokiStatus {
  connected: boolean;
  baseUrl?: string;
  authType?: string;
  tenantId?: string;
  error?: string;
}

export interface LokiConnectPayload {
  baseUrl: string;
  authType: 'bearer' | 'basic' | 'none';
  token?: string;
  username?: string;
  password?: string;
  tenantId?: string;
}

export interface WebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/loki';

export const lokiService = {
  async getStatus(): Promise<LokiStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(raw?.connected),
        baseUrl: (raw?.baseUrl ?? raw?.base_url) as string | undefined,
        authType: (raw?.authType ?? raw?.auth_type) as string | undefined,
        tenantId: (raw?.tenantId ?? raw?.tenant_id) as string | undefined,
        error: raw?.error as string | undefined,
      };
    } catch (error) {
      console.error('[lokiService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: LokiConnectPayload): Promise<LokiStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success),
      baseUrl: (raw?.baseUrl ?? raw?.base_url ?? payload.baseUrl) as string | undefined,
      authType: (raw?.authType ?? raw?.auth_type ?? payload.authType) as string | undefined,
      tenantId: (raw?.tenantId ?? raw?.tenant_id ?? payload.tenantId) as string | undefined,
    };
  },

  async getWebhookUrl(): Promise<WebhookUrlResponse> {
    return apiRequest<WebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, {
      cache: 'no-store',
    });
  },
};
