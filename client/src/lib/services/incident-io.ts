import { apiRequest } from '@/lib/services/api-client';

export interface IncidentIoStatus {
  connected: boolean;
  error?: string;
}

export interface IncidentIoConnectPayload {
  apiKey: string;
}

export interface IncidentIoAlert {
  id: number;
  incidentId?: string;
  name?: string;
  status?: string;
  severity?: string;
  incidentType?: string;
  payload?: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface IncidentIoAlertsResponse {
  alerts: IncidentIoAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface IncidentIoWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

export interface IncidentIoRcaSettings {
  rcaEnabled: boolean;
  postbackEnabled: boolean;
}

const API_BASE = '/api/incident-io';

export const incidentIoService = {
  async getStatus(): Promise<IncidentIoStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      if (!raw) return null;
      return {
        connected: Boolean(raw.connected),
        error: raw.error as string | undefined,
      };
    } catch (error) {
      console.error('[incidentIoService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: IncidentIoConnectPayload): Promise<IncidentIoStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success),
      error: raw?.error as string | undefined,
    };
  },

  async getAlerts(limit = 50, offset = 0, severity?: string): Promise<IncidentIoAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (severity) params.append('severity', severity);

    const raw = await apiRequest<IncidentIoAlertsResponse>(`${API_BASE}/alerts?${params}`, {
      cache: 'no-store',
    });
    return raw ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<IncidentIoWebhookUrlResponse | null> {
    try {
      return await apiRequest<IncidentIoWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, {
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[incidentIoService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async saveWebhookSecret(webhookSecret: string): Promise<boolean> {
    try {
      await apiRequest(`${API_BASE}/webhook-secret`, {
        method: 'PUT',
        body: JSON.stringify({ webhookSecret }),
        cache: 'no-store',
      });
      return true;
    } catch (error) {
      console.error('[incidentIoService] Failed to save webhook secret:', error);
      return false;
    }
  },

  async getRcaSettings(): Promise<IncidentIoRcaSettings | null> {
    try {
      return await apiRequest<IncidentIoRcaSettings>(`${API_BASE}/rca-settings`, {
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[incidentIoService] Failed to fetch RCA settings:', error);
      return null;
    }
  },

  async updateRcaSettings(settings: Partial<IncidentIoRcaSettings>): Promise<IncidentIoRcaSettings | null> {
    try {
      return await apiRequest<IncidentIoRcaSettings>(`${API_BASE}/rca-settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[incidentIoService] Failed to update RCA settings:', error);
      return null;
    }
  },
};
