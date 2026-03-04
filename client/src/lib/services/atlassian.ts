export interface AtlassianProductStatus {
  connected: boolean;
  authType?: 'oauth' | 'pat';
  baseUrl?: string;
  cloudId?: string | null;
  agentTier?: 'read' | 'write';
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
  agentTier?: 'read' | 'write';
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
  return JSON.parse(text) as T;
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
    const response = await fetch(`${API_BASE}/disconnect`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product }),
      credentials: 'include',
      cache: 'no-store',
    });
    if (!response.ok && response.status !== 204) {
      const text = await response.text();
      throw new Error(text || 'Failed to disconnect');
    }
  },
};
