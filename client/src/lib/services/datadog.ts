'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface DatadogStatus {
  connected: boolean;
  site?: string;
  baseUrl?: string;
  org?: UnknownRecord | null;
  serviceAccountName?: string | null;
  error?: string;
  validatedAt?: string;
}

export interface DatadogConnectPayload {
  apiKey: string;
  appKey: string;
  site?: string;
  serviceAccountName?: string;
}

export interface DatadogWebhookInfo {
  webhookUrl: string;
  instructions: string[];
}

export interface DatadogIngestedEvent {
  id: number;
  eventType?: string;
  title?: string;
  status?: string;
  scope?: string;
  payload: UnknownRecord;
  receivedAt?: string;
  createdAt?: string;
}

export interface DatadogIngestedEventsResponse {
  events: DatadogIngestedEvent[];
  total: number;
  limit: number;
  offset: number;
}

const API_BASE = '/api/datadog';

export const datadogService = {
  async getStatus(): Promise<DatadogStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        site: data?.site,
        baseUrl: data?.baseUrl ?? data?.base_url,
        org: (data?.org as UnknownRecord | undefined) ?? null,
        serviceAccountName: ((data?.serviceAccountName ?? data?.service_account_name) as string | undefined | null) ?? null,
        error: data?.error as string | undefined,
        validatedAt: ((data?.validatedAt ?? data?.validated_at) as string | undefined) ?? undefined,
      };
    } catch (error) {
      console.error('[datadogService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: DatadogConnectPayload): Promise<DatadogStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(data?.success ?? true),
      site: data?.site ?? payload.site,
      baseUrl: data?.baseUrl as string | undefined,
      org: (data?.org as UnknownRecord | undefined) ?? null,
      serviceAccountName: ((data?.serviceAccountName as string | undefined) ?? payload.serviceAccountName) ?? null,
      validatedAt: (data?.validatedAt as string | undefined) ?? undefined,
    };
  },

  async searchLogs(body: { query?: string; from?: string; to?: string; limit?: number; cursor?: string }): Promise<UnknownRecord> {
    return apiRequest<UnknownRecord>(`${API_BASE}/logs/search`, {
      method: 'POST',
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  },

  async queryMetrics(body: { query: string; fromMs?: number; toMs?: number; interval?: number }): Promise<UnknownRecord> {
    return apiRequest<UnknownRecord>(`${API_BASE}/metrics/query`, {
      method: 'POST',
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  },

  async getEvents(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events?${qs}` : `${API_BASE}/events`;
    return apiRequest<UnknownRecord>(url, { cache: 'no-store' });
  },

  async getMonitors(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/monitors?${qs}` : `${API_BASE}/monitors`;
    return apiRequest<UnknownRecord>(url, { cache: 'no-store' });
  },

  async getWebhookUrl(): Promise<DatadogWebhookInfo> {
    return apiRequest<DatadogWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },

  async getIngestedEvents(params: URLSearchParams): Promise<DatadogIngestedEventsResponse> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events/ingested?${qs}` : `${API_BASE}/events/ingested`;
    return apiRequest<DatadogIngestedEventsResponse>(url, { cache: 'no-store' });
  },
};
