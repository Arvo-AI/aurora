import { apiRequest } from '@/lib/services/api-client';

const API_BASE = '/api/spinnaker';

export interface SpinnakerStatus {
  connected: boolean;
  baseUrl?: string;
  authType?: string;
  applications?: number;
  cloudAccounts?: string[];
  error?: string;
}

export interface SpinnakerDeploymentEvent {
  id: number;
  application: string;
  pipelineName: string;
  executionId: string;
  status: string;
  triggerType: string;
  triggerUser: string;
  receivedAt: string | null;
}

export interface SpinnakerWebhookInfo {
  webhookUrl: string;
  echoConfig: string;
  instructions: string[];
}

export interface SpinnakerRcaSettings {
  rcaEnabled: boolean;
}

export const spinnakerService = {
  async getStatus(): Promise<SpinnakerStatus | null> {
    try {
      return await apiRequest<SpinnakerStatus>(`${API_BASE}/status`, { cache: 'no-store' });
    } catch {
      return null;
    }
  },

  async connect(payload: Record<string, string>): Promise<SpinnakerStatus> {
    return apiRequest<SpinnakerStatus>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
  },

  async getWebhookUrl(): Promise<SpinnakerWebhookInfo | null> {
    try {
      return await apiRequest<SpinnakerWebhookInfo>(`${API_BASE}/webhook-url`, { cache: 'no-store' });
    } catch {
      return null;
    }
  },

  async getDeployments(limit = 10): Promise<{ deployments: SpinnakerDeploymentEvent[]; total: number } | null> {
    try {
      return await apiRequest<{ deployments: SpinnakerDeploymentEvent[]; total: number }>(
        `${API_BASE}/deployments?limit=${limit}`,
        { cache: 'no-store' },
      );
    } catch {
      return null;
    }
  },

  async getRcaSettings(): Promise<SpinnakerRcaSettings | null> {
    try {
      return await apiRequest<SpinnakerRcaSettings>(`${API_BASE}/rca-settings`, { cache: 'no-store' });
    } catch {
      return null;
    }
  },

  async updateRcaSettings(settings: SpinnakerRcaSettings): Promise<SpinnakerRcaSettings | null> {
    try {
      return await apiRequest<SpinnakerRcaSettings>(`${API_BASE}/rca-settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
        cache: 'no-store',
      });
    } catch {
      return null;
    }
  },
};
