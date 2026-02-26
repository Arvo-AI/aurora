export interface BigPandaStatus {
  connected: boolean;
  environmentCount?: number;
  error?: string;
}

export interface BigPandaWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/bigpanda';

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

  return await response.json().catch(() => null as T);
}

export const bigpandaService = {
  async getStatus(): Promise<BigPandaStatus | null> {
    try {
      return await jsonFetch<BigPandaStatus>(`${API_BASE}/status`);
    } catch (err) {
      console.error('[bigpandaService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(apiToken: string): Promise<BigPandaStatus> {
    const raw = await jsonFetch<{ success: boolean; connected: boolean; environmentCount?: number }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify({ apiToken }) },
    );
    return { connected: raw.success || raw.connected, environmentCount: raw.environmentCount };
  },

  async getWebhookUrl(): Promise<BigPandaWebhookUrlResponse | null> {
    try {
      return await jsonFetch<BigPandaWebhookUrlResponse>(`${API_BASE}/webhook-url`);
    } catch (err) {
      console.error('[bigpandaService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async disconnect(): Promise<void> {
    const response = await fetch('/api/connected-accounts/bigpanda', { method: 'DELETE', credentials: 'include' });
    if (!response.ok && response.status !== 204) {
      throw new Error(await response.text() || 'Failed to disconnect');
    }
  },
};
