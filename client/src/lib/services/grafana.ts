'use client';

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
  baseUrl?: string;
  stackSlug?: string;
  org?: GrafanaOrg | null;
  user?: GrafanaUser | null;
  error?: string;
}

export interface GrafanaConnectPayload {
  baseUrl: string;
  apiToken: string;
  stackSlug?: string;
}

const API_BASE = '/api/grafana';

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
  return (parsed ?? ({} as T));
}

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
      const raw = await handleJsonFetch<Record<string, any>>(`${API_BASE}/status`);
      return {
        connected: Boolean(raw?.connected),
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

  async connect(payload: GrafanaConnectPayload): Promise<GrafanaStatus> {
    const raw = await handleJsonFetch<Record<string, any>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success ?? true),
      baseUrl: raw?.baseUrl ?? payload.baseUrl,
      stackSlug: raw?.stackSlug ?? payload.stackSlug,
      org: raw?.org ?? null,
      user: raw?.user ?? (raw?.userEmail ? { email: raw.userEmail } : null),
    };
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<GrafanaAlertsResponse> {
    let url = `${API_BASE}/alerts?limit=${limit}&offset=${offset}`;
    if (state) {
      url += `&state=${encodeURIComponent(state)}`;
    }

    const data = await handleJsonFetch<GrafanaAlertsResponse>(url);
    return data;
  },

  async getWebhookUrl(): Promise<WebhookUrlResponse> {
    const data = await handleJsonFetch<WebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`);
    return data;
  },
};
