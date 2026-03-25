import { apiDelete } from '@/lib/services/api-client';

export interface AtlassianProductStatus {
  connected: boolean;
  authType?: 'oauth' | 'pat';
  baseUrl?: string;
  cloudId?: string | null;
  error?: string;
}

export interface AtlassianStatus {
  confluence: AtlassianProductStatus;
  jira: AtlassianProductStatus;
}

export interface AtlassianConnectPayload {
  products: string[];
  authType: 'oauth' | 'pat';
  baseUrl?: string;
  patToken?: string;
  confluenceBaseUrl?: string;
  confluencePatToken?: string;
  jiraBaseUrl?: string;
  jiraPatToken?: string;
  code?: string;
  state?: string;
}

export interface AtlassianConnectResponse {
  success?: boolean;
  connected?: boolean;
  authUrl?: string;
  results?: Record<string, AtlassianProductStatus>;
}

const API_BASE = '/api/atlassian';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text) as T;
  } catch {
    return null;
  }
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T | null> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
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

export const atlassianService = {
  async getStatus(init?: RequestInit): Promise<AtlassianStatus | null> {
    return handleJsonFetch<AtlassianStatus>(`${API_BASE}/status`, init);
  },

  async connect(payload: AtlassianConnectPayload): Promise<AtlassianConnectResponse | null> {
    return handleJsonFetch<AtlassianConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async disconnect(product: 'confluence' | 'jira' | 'all'): Promise<void> {
    if (product === 'all') {
      const results = await Promise.allSettled([
        apiDelete('/api/connected-accounts/confluence', { cache: 'no-store' }),
        apiDelete('/api/connected-accounts/jira', { cache: 'no-store' }),
      ]);
      const failed = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected');
      if (failed.length === results.length) {
        throw new Error('Failed to disconnect all Atlassian products');
      }
    } else {
      await apiDelete(`/api/connected-accounts/${product}`, { cache: 'no-store' });
    }
  },
};
