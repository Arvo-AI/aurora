'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface NewRelicStatus {
  connected: boolean;
  region?: string;
  accountId?: string;
  accountName?: string;
  userEmail?: string;
  userName?: string;
  validatedAt?: string;
  hasLicenseKey?: boolean;
  accessibleAccounts?: Array<{ id: number; name: string }>;
  error?: string;
}

export interface NewRelicConnectPayload {
  apiKey: string;
  accountId: string;
  region?: string;
  licenseKey?: string;
}

export interface NewRelicWebhookInfo {
  webhookUrl: string;
  instructions: string[];
}

const API_BASE = '/api/newrelic';

export const newrelicService = {
  async getStatus(): Promise<NewRelicStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        region: data?.region as string | undefined,
        accountId: (data?.accountId ?? data?.account_id) as string | undefined,
        accountName: (data?.accountName ?? data?.account_name) as string | undefined,
        userEmail: (data?.userEmail ?? data?.user_email) as string | undefined,
        userName: (data?.userName ?? data?.user_name) as string | undefined,
        validatedAt: (data?.validatedAt ?? data?.validated_at) as string | undefined,
        hasLicenseKey: Boolean(data?.hasLicenseKey ?? data?.has_license_key),
        accessibleAccounts: (data?.accessibleAccounts ?? data?.accessible_accounts) as Array<{ id: number; name: string }> | undefined,
        error: data?.error as string | undefined,
      };
    } catch (error) {
      console.error('[newrelicService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: NewRelicConnectPayload): Promise<NewRelicStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(data?.success ?? true),
      region: (data?.region ?? payload.region) as string | undefined,
      accountId: (data?.accountId ?? payload.accountId) as string | undefined,
      accountName: data?.accountName as string | undefined,
      userEmail: data?.userEmail as string | undefined,
      userName: data?.userName as string | undefined,
      validatedAt: data?.validatedAt as string | undefined,
      accessibleAccounts: data?.accessibleAccounts as Array<{ id: number; name: string }> | undefined,
    };
  },

  async getWebhookUrl(): Promise<NewRelicWebhookInfo> {
    return apiRequest<NewRelicWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },
};
