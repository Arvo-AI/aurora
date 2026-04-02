'use client';

import { apiRequest } from '@/lib/services/api-client';

interface GrafanaOrg {
  id?: string | number;
  name?: string;
}

interface GrafanaUser {
  email?: string;
  name?: string;
  login?: string;
}

export interface GrafanaStatus {
  connected: boolean;
  hasConnection?: boolean;
  baseUrl?: string;
  stackSlug?: string;
  org?: GrafanaOrg | null;
  user?: GrafanaUser | null;
  error?: string;
}

const API_BASE = '/api/grafana';

export interface GrafanaAlert {
  id: number;
  alertUid?: string;
  title?: string;
  state?: string;
  ruleName?: string;
  ruleUrl?: string;
  dashboardUrl?: string;
  panelUrl?: string;
  payload: Record<string, any>;
  receivedAt?: string;
  createdAt?: string;
}

export interface GrafanaAlertsResponse {
  alerts: GrafanaAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface WebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

export const grafanaService = {
  async getStatus(): Promise<GrafanaStatus | null> {
    try {
      const raw = await apiRequest<Record<string, any>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(raw?.connected),
        hasConnection: Boolean(raw?.hasConnection),
        baseUrl: raw?.baseUrl ?? raw?.base_url,
        stackSlug: raw?.stackSlug ?? raw?.stack_slug,
        org: raw?.org ?? null,
        user: raw?.user ?? (raw?.userEmail ? { email: raw.userEmail } : null),
        error: raw?.error,
      };
    } catch (error) {
      console.error('[grafanaService] Failed to fetch status:', error);
      return null;
    }
  },

  async reconnect(): Promise<{ success: boolean }> {
    return apiRequest<{ success: boolean }>(`${API_BASE}/reconnect`, {
      method: 'POST',
      cache: 'no-store',
    });
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<GrafanaAlertsResponse> {
    let url = `${API_BASE}/alerts?limit=${limit}&offset=${offset}`;
    if (state) {
      url += `&state=${encodeURIComponent(state)}`;
    }

    const data = await apiRequest<GrafanaAlertsResponse>(url, { cache: 'no-store' });
    return data;
  },

  async getWebhookUrl(): Promise<WebhookUrlResponse> {
    const data = await apiRequest<WebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, {
      cache: 'no-store',
    });
    return data;
  },
};
