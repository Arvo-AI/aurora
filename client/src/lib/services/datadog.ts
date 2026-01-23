'use client';

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

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) {
    return null;
  }

  return JSON.parse(text) as T;
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    cache: 'no-store',
  });

  if (!response.ok) {
    type ErrorBody = { error?: string; details?: string };
    const parsed = await parseJsonResponse<ErrorBody>(response).catch(() => null);
    const message = parsed?.error || parsed?.details || response.statusText || `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  const parsed = await parseJsonResponse<T>(response);
  return parsed ?? ({} as T);
}

export const datadogService = {
  async getStatus(): Promise<DatadogStatus | null> {
    try {
      const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/status`);
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
    const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
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
    return handleJsonFetch<UnknownRecord>(`${API_BASE}/logs/search`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  async queryMetrics(body: { query: string; fromMs?: number; toMs?: number; interval?: number }): Promise<UnknownRecord> {
    return handleJsonFetch<UnknownRecord>(`${API_BASE}/metrics/query`, {
      method: 'POST',
      body: JSON.stringify(body),
    });
  },

  async getEvents(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events?${qs}` : `${API_BASE}/events`;
    return handleJsonFetch<UnknownRecord>(url);
  },

  async getMonitors(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/monitors?${qs}` : `${API_BASE}/monitors`;
    return handleJsonFetch<UnknownRecord>(url);
  },

  async getWebhookUrl(): Promise<DatadogWebhookInfo> {
    return handleJsonFetch<DatadogWebhookInfo>(`${API_BASE}/webhook-url`);
  },

  async getIngestedEvents(params: URLSearchParams): Promise<DatadogIngestedEventsResponse> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events/ingested?${qs}` : `${API_BASE}/events/ingested`;
    return handleJsonFetch<DatadogIngestedEventsResponse>(url);
  },
};
