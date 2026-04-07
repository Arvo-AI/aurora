'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface OpsGenieStatus {
  connected: boolean;
  region?: string;
  accountName?: string | null;
  plan?: string | null;
  error?: string;
}

export interface OpsGenieConnectPayload {
  apiKey: string;
  region: string;
}

export interface OpsGenieWebhookInfo {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/opsgenie';

export const opsgenieService = {
  async getStatus(): Promise<OpsGenieStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        region: (data?.region as string) || undefined,
        accountName: ((data?.accountName ?? data?.account_name) as string | null) ?? null,
        plan: (data?.plan as string | null) ?? null,
        error: data?.error as string | undefined,
      };
    } catch (error) {
      console.error('[opsgenieService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: OpsGenieConnectPayload): Promise<OpsGenieStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(data?.success ?? data?.connected),
      region: (data?.region as string) || undefined,
      accountName: ((data?.accountName ?? data?.account_name) as string | null) ?? null,
      plan: (data?.plan as string | null) ?? null,
    };
  },

  async getWebhookUrl(): Promise<OpsGenieWebhookInfo> {
    return apiRequest<OpsGenieWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },
};
