import { apiRequest, apiDelete } from '@/lib/services/api-client';

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

export const bigpandaService = {
  async getStatus(): Promise<BigPandaStatus | null> {
    try {
      return await apiRequest<BigPandaStatus>(`${API_BASE}/status`, { cache: 'no-store' });
    } catch (err) {
      console.error('[bigpandaService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(apiToken: string): Promise<BigPandaStatus> {
    const raw = await apiRequest<{ success: boolean; connected: boolean; error?: string; environmentCount?: number }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify({ apiToken }), cache: 'no-store' },
    );
    if (!raw.success && !raw.connected) {
      throw new Error(raw.error || 'Connection failed');
    }
    return { connected: true, environmentCount: raw.environmentCount };
  },

  async getWebhookUrl(): Promise<BigPandaWebhookUrlResponse | null> {
    try {
      return await apiRequest<BigPandaWebhookUrlResponse>(`${API_BASE}/webhook-url`, { cache: 'no-store' });
    } catch (err) {
      console.error('[bigpandaService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async disconnect(): Promise<void> {
    await apiDelete('/api/connected-accounts/bigpanda');
  },
};
