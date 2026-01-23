'use client';

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

/**
 * Fetch helper that parses JSON response.
 * Returns empty object as T if response body is empty (e.g., 204 No Content).
 * Throws Error with message from response on non-ok status.
 */
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
    const text = await response.text();
    let message = 'Request failed';
    try {
      const parsed = JSON.parse(text);
      message = parsed.error || parsed.details || message;
    } catch {
      message = text || message;
    }
    throw new Error(message);
  }

  const text = await response.text();
  return text ? JSON.parse(text) : ({} as T);
}

export const netdataService = {
  async getStatus(): Promise<NetdataStatus | null> {
    try {
      const raw = await handleJsonFetch<Record<string, unknown>>(`${API_BASE}/status`);
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
    const raw = await handleJsonFetch<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      // Default to false - only trust explicit success from backend
      connected: Boolean(raw?.success),
      baseUrl: (raw?.baseUrl ?? payload.spaceUrl) as string | undefined,
      spaceName: (raw?.spaceName ?? payload.spaceName) as string | undefined,
    };
  },

  async getWebhookUrl(): Promise<WebhookUrlResponse> {
    return handleJsonFetch<WebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`);
  },

  async getAlerts(limit = 50, offset = 0, status?: string): Promise<NetdataAlertsResponse> {
    let url = `${API_BASE}/alerts?limit=${limit}&offset=${offset}`;
    if (status) {
      url += `&status=${encodeURIComponent(status)}`;
    }
    return handleJsonFetch<NetdataAlertsResponse>(url);
  },
};
