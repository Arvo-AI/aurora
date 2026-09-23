'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface DatadogAccount {
  label: string;
  site?: string;
  orgName?: string | null;
  serviceAccountName?: string | null;
  validatedAt?: string | null;
  valid?: boolean;
  error?: string;
}

export interface DatadogStatus {
  connected: boolean;
  site?: string;
  baseUrl?: string;
  org?: UnknownRecord | null;
  serviceAccountName?: string | null;
  error?: string;
  validatedAt?: string;
  label?: string;
  accounts?: DatadogAccount[];
  /** True when the connect updated an existing organization's keys in place. */
  replaced?: boolean;
}

export interface DatadogConnectPayload {
  apiKey: string;
  appKey: string;
  site?: string;
  serviceAccountName?: string;
  label?: string;
}

export interface DatadogWebhookInfo {
  webhookUrl: string;
  instructions: string[];
  perOrganization?: boolean;
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

/** Appends ?account=<label> when an org is selected. */
const withAccount = (path: string, account?: string): string => {
  if (!account) return path;
  const separator = path.includes('?') ? '&' : '?';
  return `${path}${separator}account=${encodeURIComponent(account)}`;
};

export const datadogService = {
  async getStatus(): Promise<DatadogStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        site: data?.site as string | undefined,
        baseUrl: (data?.baseUrl ?? data?.base_url) as string | undefined,
        org: (data?.org as UnknownRecord | undefined) ?? null,
        serviceAccountName: ((data?.serviceAccountName ?? data?.service_account_name) as string | undefined | null) ?? null,
        error: data?.error as string | undefined,
        validatedAt: ((data?.validatedAt ?? data?.validated_at) as string | undefined) ?? undefined,
        label: data?.label as string | undefined,
        accounts: (data?.accounts as DatadogAccount[] | undefined) ?? [],
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
      site: (data?.site as string | undefined) ?? payload.site,
      baseUrl: data?.baseUrl as string | undefined,
      org: (data?.org as UnknownRecord | undefined) ?? null,
      serviceAccountName: ((data?.serviceAccountName as string | undefined) ?? payload.serviceAccountName) ?? null,
      validatedAt: (data?.validatedAt as string | undefined) ?? undefined,
      label: (data?.label as string | undefined) ?? payload.label,
      accounts: (data?.accounts as DatadogAccount[] | undefined) ?? [],
      replaced: Boolean(data?.replaced),
    };
  },

  /** Removes one organization, or every organization when label is omitted. */
  async disconnect(label?: string): Promise<{ accounts: DatadogAccount[] }> {
    const data = await apiRequest<UnknownRecord>(withAccount(`${API_BASE}/disconnect`, label), {
      method: 'DELETE',
      cache: 'no-store',
    });
    return { accounts: (data?.accounts as DatadogAccount[] | undefined) ?? [] };
  },

  async searchLogs(body: { query?: string; from?: string; to?: string; limit?: number; cursor?: string }, account?: string): Promise<UnknownRecord> {
    return apiRequest<UnknownRecord>(withAccount(`${API_BASE}/logs/search`, account), {
      method: 'POST',
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  },

  async queryMetrics(body: { query: string; fromMs?: number; toMs?: number; interval?: number }, account?: string): Promise<UnknownRecord> {
    return apiRequest<UnknownRecord>(withAccount(`${API_BASE}/metrics/query`, account), {
      method: 'POST',
      body: JSON.stringify(body),
      cache: 'no-store',
    });
  },

  async getEvents(params: URLSearchParams, account?: string): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events?${qs}` : `${API_BASE}/events`;
    return apiRequest<UnknownRecord>(withAccount(url, account), { cache: 'no-store' });
  },

  async getMonitors(params: URLSearchParams, account?: string): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/monitors?${qs}` : `${API_BASE}/monitors`;
    return apiRequest<UnknownRecord>(withAccount(url, account), { cache: 'no-store' });
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
