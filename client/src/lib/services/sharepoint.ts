export interface SharePointStatus {
  connected: boolean;
  siteUrl?: string;
  siteName?: string;
  userDisplayName?: string | null;
  userEmail?: string | null;
  error?: string;
}

export interface SharePointConnectPayload {
  code?: string;
  state?: string;
}

export interface SharePointConnectResponse extends SharePointStatus {
  success?: boolean;
  authUrl?: string;
}

export interface SharePointSearchResult {
  id?: string;
  name?: string;
  webUrl?: string;
  [key: string]: unknown;
}

export interface SharePointFetchResponse {
  id?: string;
  title?: string;
  content?: string;
  webUrl?: string;
  [key: string]: unknown;
}

export interface SharePointSite {
  id: string;
  name: string;
  webUrl: string;
  displayName?: string;
  [key: string]: unknown;
}

const API_BASE = '/api/sharepoint';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) {
    return null;
  }
  try {
    return JSON.parse(text) as T;
  } catch {
    throw new Error('Invalid JSON response from server');
  }
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T | null> {
  const headers = new Headers(init?.headers);
  if (!headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }

  const response = await fetch(input, {
    ...init,
    headers,
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

export const sharepointService = {
  async getStatus(init?: RequestInit): Promise<SharePointStatus | null> {
    return handleJsonFetch<SharePointStatus>(`${API_BASE}/status`, init);
  },

  async connect(payload: SharePointConnectPayload): Promise<SharePointConnectResponse | null> {
    return handleJsonFetch<SharePointConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async disconnect(): Promise<void> {
    const response = await fetch('/api/connected-accounts/sharepoint', {
      method: 'DELETE',
      credentials: 'include',
      cache: 'no-store',
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(text || 'Failed to disconnect SharePoint');
    }
  },

  async search(query: string, siteId?: string): Promise<SharePointSearchResult[] | null> {
    return handleJsonFetch<SharePointSearchResult[]>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify({ query, siteId }),
    });
  },

  async fetchPage(siteId: string, pageId: string): Promise<SharePointFetchResponse | null> {
    return handleJsonFetch<SharePointFetchResponse>(`${API_BASE}/fetch-page`, {
      method: 'POST',
      body: JSON.stringify({ siteId, pageId }),
    });
  },

  async fetchDocument(siteId: string, driveId: string, itemId: string): Promise<SharePointFetchResponse | null> {
    return handleJsonFetch<SharePointFetchResponse>(`${API_BASE}/fetch-document`, {
      method: 'POST',
      body: JSON.stringify({ siteId, driveId, itemId }),
    });
  },

  async createPage(siteId: string, title: string, content: string): Promise<SharePointFetchResponse | null> {
    return handleJsonFetch<SharePointFetchResponse>(`${API_BASE}/create-page`, {
      method: 'POST',
      body: JSON.stringify({ siteId, title, content }),
    });
  },

  async getSites(): Promise<SharePointSite[] | null> {
    return handleJsonFetch<SharePointSite[]>(`${API_BASE}/sites`);
  },
};
