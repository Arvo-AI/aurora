'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface CorootProject {
  id: string;
  name?: string;
}

export interface CorootStatus {
  connected: boolean;
  url?: string;
  email?: string;
  projects?: CorootProject[];
  error?: string;
  validatedAt?: string;
}

export interface CorootConnectPayload {
  url: string;
  email: string;
  password: string;
}

export interface CorootConnectResponse {
  success: boolean;
  url: string;
  projects: CorootProject[];
}

const API_BASE = '/api/coroot';

export const corootService = {
  async getStatus(): Promise<CorootStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        url: data?.url as string | undefined,
        email: data?.email as string | undefined,
        projects: (data?.projects as CorootProject[] | undefined) ?? [],
        error: data?.error as string | undefined,
        validatedAt: ((data?.validatedAt ?? data?.validated_at) as string | undefined) ?? undefined,
      };
    } catch (error) {
      console.error('[corootService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: CorootConnectPayload): Promise<CorootConnectResponse> {
    try {
      const result = await apiRequest<CorootConnectResponse>(`${API_BASE}/connect`, {
        method: 'POST',
        body: JSON.stringify(payload),
        cache: 'no-store',
      });
      return result ?? ({} as CorootConnectResponse);
    } catch (error) {
      console.error('[corootService] Connect failed:', error);
      return { success: false, url: '', projects: [] };
    }
  },
};
