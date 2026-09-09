'use client';

import { apiRequest } from '@/lib/services/api-client';

export interface VictorOpsStatus {
  connected: boolean;
  displayName?: string;
  externalUserName?: string;
  accountName?: string;
  validatedAt?: string;
  capabilities?: {
    can_read_incidents: boolean;
  };
}

const API_BASE = '/api/victorops';

export const victoropsService = {
  async getStatus(): Promise<VictorOpsStatus | null> {
    try {
      return await apiRequest<VictorOpsStatus>(API_BASE, { cache: 'no-store' });
    } catch {
      return null;
    }
  },

  async connect(
    apiId: string,
    apiKey: string,
    displayName = 'Splunk On-Call'
  ): Promise<VictorOpsStatus> {
    return apiRequest<VictorOpsStatus>(API_BASE, {
      method: 'POST',
      body: JSON.stringify({ apiId, apiKey, displayName }),
      cache: 'no-store',
    });
  },

  async disconnect(): Promise<void> {
    await apiRequest(API_BASE, { method: 'DELETE', cache: 'no-store' });
  },
};
