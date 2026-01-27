export interface ConfluenceStatus {
  connected: boolean;
  authType?: 'oauth' | 'pat';
  baseUrl?: string;
  cloudId?: string | null;
  userDisplayName?: string | null;
  userEmail?: string | null;
  error?: string;
}

export interface ConfluenceConnectPayload {
  authType: 'oauth' | 'pat';
  baseUrl?: string;
  patToken?: string;
  code?: string;
  state?: string;
}

export interface ConfluenceConnectResponse extends ConfluenceStatus {
  success?: boolean;
  authUrl?: string;
}

export interface ConfluenceFetchResponse {
  id?: string;
  title?: string;
  body?: Record<string, unknown>;
  [key: string]: unknown;
}

const API_BASE = '/api/confluence';

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

export const confluenceService = {
  async getStatus(init?: RequestInit): Promise<ConfluenceStatus | null> {
    return handleJsonFetch<ConfluenceStatus>(`${API_BASE}/status`, init);
  },

  async connect(payload: ConfluenceConnectPayload): Promise<ConfluenceConnectResponse | null> {
    return handleJsonFetch<ConfluenceConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async disconnect(): Promise<void> {
    const response = await fetch('/api/connected-accounts/confluence', {
      method: 'DELETE',
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok && response.status !== 204) {
      const text = await response.text();
      throw new Error(text || 'Failed to disconnect Confluence');
    }
  },

  async fetchPage(url: string): Promise<ConfluenceFetchResponse | null> {
    return handleJsonFetch<ConfluenceFetchResponse>(`${API_BASE}/fetch`, {
      method: 'POST',
      body: JSON.stringify({ url }),
    });
  },
};
