import { apiRequest } from '@/lib/services/api-client';

export interface OpenSearchStatus {
  connected: boolean;
  clusterName?: string;
  version?: string;
  endpoint?: string;
  indexPattern?: string;
  error?: string;
}

export interface OpenSearchConnectPayload {
  endpoint: string;
  username: string;
  password: string;
  indexPattern?: string;
  verifySsl?: boolean;
  maxRetries?: number;
}

export interface OpenSearchSearchPayload {
  query: string;
  index?: string;
  startTime?: string;
  endTime?: string;
  size?: number;
  timestampField?: string;
}

export interface OpenSearchSearchResult {
  total: number;
  hits: Record<string, unknown>[];
  index: string;
  query: string;
}

const API_BASE = '/api/opensearch';

export const openSearchService = {
  async getStatus(): Promise<OpenSearchStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      if (!raw) return null;
      return {
        connected: Boolean(raw.connected),
        clusterName: raw.clusterName as string | undefined,
        version: raw.version as string | undefined,
        endpoint: raw.endpoint as string | undefined,
        indexPattern: raw.indexPattern as string | undefined,
        error: raw.error as string | undefined,
      };
    } catch (error) {
      console.error('[openSearchService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: OpenSearchConnectPayload): Promise<OpenSearchStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success),
      clusterName: raw?.clusterName as string | undefined,
      version: raw?.version as string | undefined,
      endpoint: (raw?.endpoint ?? payload.endpoint) as string,
      indexPattern: (raw?.indexPattern ?? payload.indexPattern ?? '*') as string,
    };
  },

  async search(payload: OpenSearchSearchPayload): Promise<OpenSearchSearchResult> {
    const raw = await apiRequest<OpenSearchSearchResult>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return raw ?? { total: 0, hits: [], index: '*', query: payload.query };
  },
};
