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

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });

  if (!response.ok) {
    const parsed = await response.json().catch(() => null) as { error?: string } | null;
    throw new Error(parsed?.error || response.statusText || `Request failed (${response.status})`);
  }

  return await response.json().catch(() => null as T);
}

export const dynatraceService = {
  async getStatus(): Promise<DynatraceStatus | null> {
    try {
      return await jsonFetch<DynatraceStatus>(`${API_BASE}/status`);
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(payload: DynatraceConnectPayload): Promise<DynatraceStatus> {
    const raw = await jsonFetch<{ success: boolean; environmentUrl: string; version?: string }>(
      `${API_BASE}/connect`,
      { method: 'POST', body: JSON.stringify(payload) },
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
    return await jsonFetch<DynatraceAlertsResponse>(`${API_BASE}/alerts?${params}`)
      ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<DynatraceWebhookUrlResponse | null> {
    try {
      return await jsonFetch<DynatraceWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`);
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch webhook URL:', err);
      return null;
    }
  },

  async getRcaSettings(): Promise<DynatraceRcaSettings> {
    try {
      return await jsonFetch<DynatraceRcaSettings>(`${API_BASE}/rca-settings`)
        ?? { rcaEnabled: false };
    } catch (err) {
      console.error('[dynatraceService] Failed to fetch RCA settings:', err);
      return { rcaEnabled: false };
    }
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<DynatraceRcaSettings> {
    return await jsonFetch<DynatraceRcaSettings>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
    }) ?? { rcaEnabled };
  },

  async disconnect(): Promise<void> {
    const response = await fetch('/api/connected-accounts/dynatrace', { method: 'DELETE', credentials: 'include' });
    if (!response.ok && response.status !== 204) {
      throw new Error(await response.text() || 'Failed to disconnect');
    }
  },
};
