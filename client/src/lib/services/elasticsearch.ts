interface ElasticsearchCluster {
  name?: string;
  version?: string;
  distribution?: string;
  health?: string;
}

export interface ElasticsearchStatus {
  connected: boolean;
  baseUrl?: string;
  cluster?: ElasticsearchCluster | null;
  username?: string;
  error?: string;
}

export interface ElasticsearchConnectPayload {
  baseUrl: string;
  authMethod: 'apiKey' | 'basic';
  apiKey?: string;
  username?: string;
  password?: string;
}

export interface ElasticsearchAlert {
  id: number;
  alertId?: string;
  title?: string;
  state?: string;
  watchId?: string;
  query?: string;
  resultCount?: number;
  severity?: string;
  payload?: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface ElasticsearchAlertsResponse {
  alerts: ElasticsearchAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface ElasticsearchWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

export interface ElasticsearchSearchPayload {
  index?: string;
  queryString?: string;
  query?: Record<string, unknown>;
  from?: number;
  size?: number;
  sort?: Record<string, unknown>[];
  timeField?: string;
  earliestTime?: string;
  latestTime?: string;
}

export interface ElasticsearchSearchResult {
  success: boolean;
  results: Record<string, unknown>[];
  total: number;
  took: number;
  timedOut: boolean;
}

export interface ElasticsearchIndex {
  index: string;
  health: string;
  status: string;
  'docs.count': string;
  'store.size': string;
}

export interface ElasticsearchRcaSettings {
  rcaEnabled: boolean;
}

const API_BASE = '/api/elasticsearch';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  return JSON.parse(text) as T;
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T | null> {
  const response = await fetch(input, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(init?.headers ?? {}),
    },
    cache: 'no-store',
  });

  if (!response.ok) {
    type ErrorBody = { error?: string; details?: string };
    const parsed = await parseJsonResponse<ErrorBody>(response).catch(() => null);
    const message = parsed?.error || parsed?.details || response.statusText || `Request failed with status ${response.status}`;
    throw new Error(message);
  }

  return parseJsonResponse<T>(response);
}

export const elasticsearchService = {
  async getStatus(): Promise<ElasticsearchStatus | null> {
    try {
      const raw = await handleJsonFetch<Record<string, unknown>>(`${API_BASE}/status`);
      if (!raw) {
        return null;
      }
      return {
        connected: Boolean(raw.connected),
        baseUrl: raw.baseUrl as string | undefined,
        cluster: raw.cluster as ElasticsearchCluster | null,
        username: raw.username as string | undefined,
        error: raw.error as string | undefined,
      };
    } catch (error) {
      console.error('[elasticsearchService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: ElasticsearchConnectPayload): Promise<ElasticsearchStatus> {
    const raw = await handleJsonFetch<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success),
      baseUrl: (raw?.baseUrl ?? payload.baseUrl) as string,
      cluster: raw?.cluster as ElasticsearchCluster | null,
      username: raw?.username as string | undefined,
    };
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<ElasticsearchAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.append('state', state);

    const raw = await handleJsonFetch<ElasticsearchAlertsResponse>(`${API_BASE}/alerts?${params}`);
    return raw ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<ElasticsearchWebhookUrlResponse | null> {
    try {
      return await handleJsonFetch<ElasticsearchWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`);
    } catch (error) {
      console.error('[elasticsearchService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async search(payload: ElasticsearchSearchPayload): Promise<ElasticsearchSearchResult> {
    const raw = await handleJsonFetch<ElasticsearchSearchResult>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return raw ?? { success: false, results: [], total: 0, took: 0, timedOut: false };
  },

  async getIndices(): Promise<ElasticsearchIndex[]> {
    try {
      const raw = await handleJsonFetch<{ success: boolean; indices: ElasticsearchIndex[] }>(`${API_BASE}/indices`);
      return raw?.indices ?? [];
    } catch (error) {
      console.error('[elasticsearchService] Failed to fetch indices:', error);
      return [];
    }
  },

  async getClusterHealth(): Promise<Record<string, unknown> | null> {
    try {
      return await handleJsonFetch<Record<string, unknown>>(`${API_BASE}/cluster/health`);
    } catch (error) {
      console.error('[elasticsearchService] Failed to fetch cluster health:', error);
      return null;
    }
  },

  async getRcaSettings(): Promise<ElasticsearchRcaSettings> {
    try {
      const raw = await handleJsonFetch<ElasticsearchRcaSettings>(`${API_BASE}/rca-settings`);
      return raw ?? { rcaEnabled: false };
    } catch (error) {
      console.error('[elasticsearchService] Failed to fetch RCA settings:', error);
      return { rcaEnabled: false };
    }
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<ElasticsearchRcaSettings> {
    const raw = await handleJsonFetch<ElasticsearchRcaSettings>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
    });
    return raw ?? { rcaEnabled };
  },
};
