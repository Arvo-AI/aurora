'use client';

import { apiRequest } from '@/lib/services/api-client';

export interface NetdataStatus {
  connected: boolean;
  baseUrl?: string;
  spaceName?: string;
  error?: string;
}

export interface NetdataConnectPayload {
  apiToken: string;
  spaceUrl?: string;
  spaceName?: string;
}

export interface NetdataAlert {
  id: number;
  alertName?: string;
  status?: string;
  chart?: string;
  host?: string;
  space?: string;
  room?: string;
  value?: string;
  message?: string;
  payload: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface NetdataAlertsResponse {
  alerts: NetdataAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface WebhookUrlResponse {
  webhookUrl: string;
  verificationToken?: string;
}

const API_BASE = '/api/netdata';

export const netdataService = {
  async getStatus(): Promise<NetdataStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(raw?.connected),
        baseUrl: (raw?.baseUrl ?? raw?.base_url) as string | undefined,
        spaceName: (raw?.spaceName ?? raw?.space_name) as string | undefined,
        error: raw?.error as string | undefined,
      };
    } catch (error) {
      console.error('[netdataService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: NetdataConnectPayload): Promise<NetdataStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success),
      baseUrl: (raw?.baseUrl ?? payload.spaceUrl) as string | undefined,
      spaceName: (raw?.spaceName ?? payload.spaceName) as string | undefined,
    };
  },

  async getWebhookUrl(): Promise<WebhookUrlResponse> {
    return apiRequest<WebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, {
      cache: 'no-store',
    });
  },

  async getAlerts(limit = 50, offset = 0, status?: string): Promise<NetdataAlertsResponse> {
    let url = `${API_BASE}/alerts?limit=${limit}&offset=${offset}`;
    if (status) {
      url += `&status=${encodeURIComponent(status)}`;
    }
    return apiRequest<NetdataAlertsResponse>(url, { cache: 'no-store' });
  },
};
