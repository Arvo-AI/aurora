export interface PrometheusStatus {
  connected: boolean;
  baseUrl?: string;
  version?: string;
  error?: string;
}

export interface PrometheusWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

export interface PrometheusAlert {
  labels: Record<string, string>;
  annotations: Record<string, string>;
  status: string;
  startsAt: string;
  endsAt: string;
  generatorURL: string;
  fingerprint: string;
}

const API_BASE = '/api/prometheus';

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });

  if (!response.ok) {
    const parsed = await response.json().catch(() => null) as { error?: string } | null;
    throw new Error(parsed?.error || response.statusText || `Request failed (${response.status})`);
  }

  return await response.json();
}

export const prometheusService = {
  async getStatus(): Promise<PrometheusStatus | null> {
    try {
      return await jsonFetch<PrometheusStatus>(`${API_BASE}/status`);
    } catch (err) {
      console.error('[prometheusService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(baseUrl: string, apiToken?: string): Promise<PrometheusStatus> {
    const raw = await jsonFetch<{ success: boolean; connected: boolean; error?: string; version?: string; baseUrl?: string }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify({ baseUrl, apiToken: apiToken || undefined }) },
    );
    if (!raw.success && !raw.connected) {
      throw new Error(raw.error || 'Connection failed');
    }
    return { connected: true, version: raw.version, baseUrl: raw.baseUrl };
  },

  async getWebhookUrl(): Promise<PrometheusWebhookUrlResponse | null> {
    try {
      return await jsonFetch<PrometheusWebhookUrlResponse>(`${API_BASE}/webhook-url`);
    } catch (err) {
      console.error('[prometheusService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async getAlerts(): Promise<{ alerts: PrometheusAlert[]; count: number }> {
    return await jsonFetch<{ alerts: PrometheusAlert[]; count: number }>(`${API_BASE}/alerts`);
  },

  async disconnect(): Promise<void> {
    const response = await fetch('/api/connected-accounts/prometheus', { method: 'DELETE', credentials: 'include' });
    if (!response.ok) {
      throw new Error(await response.text() || 'Failed to disconnect');
    }
  },
};
