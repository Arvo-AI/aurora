import { apiRequest } from '@/lib/services/api-client';

export interface SplunkOnCallStatus {
  connected: boolean;
  routingKeyContains?: string;
}

export interface SplunkOnCallWebhookInfo {
  webhookUrl: string;
  webhookSecret: string;
  secretHeader: string;
  instructions?: string[];
}

const API_BASE = '/api/splunk-on-call';

export const splunkOnCallService = {
  async getStatus(): Promise<SplunkOnCallStatus | null> {
    try {
      return await apiRequest<SplunkOnCallStatus>(`${API_BASE}/status`, { cache: 'no-store' });
    } catch (err) {
      console.error('[splunkOnCallService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(apiId: string, apiKey: string, routingKeyContains: string): Promise<SplunkOnCallStatus> {
    return apiRequest<SplunkOnCallStatus>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify({ apiId, apiKey, routingKeyContains }),
      cache: 'no-store',
    });
  },

  async getWebhookUrl(): Promise<SplunkOnCallWebhookInfo | null> {
    try {
      return await apiRequest<SplunkOnCallWebhookInfo>(`${API_BASE}/webhook-url`, { cache: 'no-store' });
    } catch (err) {
      console.error('[splunkOnCallService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async disconnect(): Promise<void> {
    await apiRequest(`${API_BASE}/disconnect`, { method: 'DELETE', cache: 'no-store' });
  },
};
