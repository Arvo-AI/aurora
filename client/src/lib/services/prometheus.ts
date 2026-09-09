'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface PrometheusStatus {
  connected: boolean;
  prometheusUrl?: string;
  instanceLabel?: string;
  alertmanagerUrl?: string;
  authType?: string;
  version?: string;
  validatedAt?: string;
  error?: string;
}

export interface PrometheusConnectPayload {
  prometheusUrl: string;
  instanceLabel?: string;
  alertmanagerUrl?: string;
  authType: string;
  username?: string;
  password?: string;
  bearerToken?: string;
  customHeaders?: Record<string, string>;
  verifySsl?: boolean;
}

export interface PrometheusWebhookInfo {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/proxy/prometheus';

export const prometheusService = {
  async getStatus(): Promise<PrometheusStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      if (!data) return null;
      return {
        connected: Boolean(data.connected),
        ...data,
      } as PrometheusStatus;
    } catch (error) {
      console.error('[prometheusService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: PrometheusConnectPayload): Promise<PrometheusStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(data?.success),
      ...data,
    } as PrometheusStatus;
  },

  async getWebhookUrl(): Promise<PrometheusWebhookInfo> {
    return apiRequest<PrometheusWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },
};
