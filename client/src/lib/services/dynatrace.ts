import { apiRequest, apiDelete } from '@/lib/services/api-client';

export interface DynatraceStatus {
  connected: boolean;
  environmentUrl?: string;
  version?: string;
  error?: string;
}

export interface DynatraceConnectPayload {
  environmentUrl: string;
  apiToken: string;
}

export interface DynatraceAlert {
  id: number;
  problemId?: string;
  title?: string;
  state?: string;
  severity?: string;
  impact?: string;
  impactedEntity?: string;
  problemUrl?: string;
  tags?: string;
  payload?: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface DynatraceAlertsResponse {
  alerts: DynatraceAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface DynatraceWebhookUrlResponse {
  webhookUrl: string;
  suggestedPayload: string;
  instructions: string[];
}

export interface DynatraceRcaSettings {
  rcaEnabled: boolean;
}

const API_BASE = '/api/dynatrace';

export const dynatraceService = {
  async getStatus(): Promise<DynatraceStatus | null> {
    try {
      return await apiRequest<DynatraceStatus>(`${API_BASE}/status`, { cache: 'no-store' });
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(payload: DynatraceConnectPayload): Promise<DynatraceStatus> {
    const raw = await apiRequest<{ success: boolean; environmentUrl: string; version?: string }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify(payload), cache: 'no-store' },
    );
    return {
      connected: raw.success,
      environmentUrl: raw.environmentUrl ?? payload.environmentUrl,
      version: raw.version,
    };
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<DynatraceAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.append('state', state);
    return await apiRequest<DynatraceAlertsResponse>(`${API_BASE}/alerts?${params}`, { cache: 'no-store' })
      ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<DynatraceWebhookUrlResponse | null> {
    try {
      return await apiRequest<DynatraceWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, { cache: 'no-store' });
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async getRcaSettings(): Promise<DynatraceRcaSettings> {
    try {
      return await apiRequest<DynatraceRcaSettings>(`${API_BASE}/rca-settings`, { cache: 'no-store' })
        ?? { rcaEnabled: false };
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch RCA settings:', err);
      return { rcaEnabled: false };
    }
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<DynatraceRcaSettings> {
    return await apiRequest<DynatraceRcaSettings>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
      cache: 'no-store',
    }) ?? { rcaEnabled };
  },

  async disconnect(): Promise<void> {
    await apiDelete('/api/connected-accounts/dynatrace');
  },
};
