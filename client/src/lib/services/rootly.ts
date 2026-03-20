export interface RootlyStatus {
  connected: boolean;
  userEmail?: string;
  userName?: string;
  error?: string;
}

export interface RootlyWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/rootly';

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

export const rootlyService = {
  async getStatus(): Promise<RootlyStatus | null> {
    try {
      return await jsonFetch<RootlyStatus>(`${API_BASE}/status`);
    } catch (err) {
      console.error('[rootlyService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(apiToken: string): Promise<RootlyStatus> {
    const raw = await jsonFetch<{ success: boolean; connected: boolean; error?: string; userEmail?: string; userName?: string }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify({ apiToken }) },
    );
    if (!raw.success && !raw.connected) {
      throw new Error(raw.error || 'Connection failed');
    }
    return { connected: true, userEmail: raw.userEmail, userName: raw.userName };
  },

  async getWebhookUrl(): Promise<RootlyWebhookUrlResponse | null> {
    try {
      return await jsonFetch<RootlyWebhookUrlResponse>(`${API_BASE}/webhook-url`);
    } catch (err) {
      console.error('[rootlyService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async disconnect(): Promise<void> {
    await jsonFetch(`${API_BASE}/disconnect`, { method: 'DELETE' });
  },
};
