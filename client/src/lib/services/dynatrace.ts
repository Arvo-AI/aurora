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

async function jsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T | null> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });

  if (!response.ok) {
    const parsed = await response.json().catch(() => null) as { error?: string } | null;
    throw new Error(parsed?.error || response.statusText || `Request failed (${response.status})`);
  }

  const text = await response.text();
  return text ? (JSON.parse(text) as T) : null;
}

export const dynatraceService = {
  async getStatus(): Promise<DynatraceStatus | null> {
    try {
      const raw = await jsonFetch<Record<string, unknown>>(`${API_BASE}/status`);
      if (!raw) return null;
      return {
        connected: Boolean(raw.connected),
        environmentUrl: raw.environmentUrl as string | undefined,
        version: raw.version as string | undefined,
        error: raw.error as string | undefined,
      };
    } catch (error) {
      console.error('[dynatraceService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: DynatraceConnectPayload): Promise<DynatraceStatus> {
    const raw = await jsonFetch<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success),
      environmentUrl: (raw?.environmentUrl ?? payload.environmentUrl) as string,
      version: raw?.version as string | undefined,
    };
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<DynatraceAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.append('state', state);
    const raw = await jsonFetch<DynatraceAlertsResponse>(`${API_BASE}/alerts?${params}`);
    return raw ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<DynatraceWebhookUrlResponse | null> {
    try {
      return await jsonFetch<DynatraceWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`);
    } catch (error) {
      console.error('[dynatraceService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async getRcaSettings(): Promise<DynatraceRcaSettings> {
    try {
      const raw = await jsonFetch<DynatraceRcaSettings>(`${API_BASE}/rca-settings`);
      return raw ?? { rcaEnabled: false };
    } catch (error) {
      console.error('[dynatraceService] Failed to fetch RCA settings:', error);
      return { rcaEnabled: false };
    }
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<DynatraceRcaSettings> {
    const raw = await jsonFetch<DynatraceRcaSettings>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
    });
    return raw ?? { rcaEnabled };
  },
};
