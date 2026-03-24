'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface ThousandEyesAccountGroup {
  aid: string;
  accountGroupName?: string;
}

export interface ThousandEyesStatus {
  connected: boolean;
  account_group_id?: string;
  account_groups?: ThousandEyesAccountGroup[];
  error?: string;
  validatedAt?: string;
}

export interface ThousandEyesConnectPayload {
  api_token: string;
  account_group_id?: string;
}

export interface ThousandEyesConnectResponse {
  success: boolean;
  account_groups: ThousandEyesAccountGroup[];
}

const API_BASE = '/api/thousandeyes';

export const thousandEyesService = {
  async getStatus(): Promise<ThousandEyesStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        account_group_id: data?.account_group_id as string | undefined,
        account_groups: (data?.account_groups as ThousandEyesAccountGroup[] | undefined) ?? [],
        error: data?.error as string | undefined,
        validatedAt: (data?.validatedAt ?? data?.validated_at) as string | undefined,
      };
    } catch (error) {
      console.error('[thousandEyesService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: ThousandEyesConnectPayload): Promise<ThousandEyesConnectResponse> {
    return await apiRequest<ThousandEyesConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
  },
};
