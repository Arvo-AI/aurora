import { apiRequest, apiDelete } from '@/lib/services/api-client';

export interface SharePointStatus {
  connected: boolean;
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

export const sharepointService = {
  async getStatus(init?: RequestInit): Promise<SharePointStatus | null> {
    return apiRequest<SharePointStatus | null>(`${API_BASE}/status`, {
      ...init,
      cache: 'no-store',
    });
  },

  async connect(payload: SharePointConnectPayload): Promise<SharePointConnectResponse | null> {
    return apiRequest<SharePointConnectResponse | null>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
  },

  async disconnect(): Promise<void> {
    await apiDelete('/api/connected-accounts/sharepoint', { cache: 'no-store' });
  },

  async search(query: string, siteId?: string): Promise<SharePointSearchResult[] | null> {
    return apiRequest<SharePointSearchResult[] | null>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify({ query, siteId }),
      cache: 'no-store',
    });
  },

  async fetchPage(siteId: string, pageId: string): Promise<SharePointFetchResponse | null> {
    return apiRequest<SharePointFetchResponse | null>(`${API_BASE}/fetch-page`, {
      method: 'POST',
      body: JSON.stringify({ siteId, pageId }),
      cache: 'no-store',
    });
  },

  async fetchDocument(siteId: string, driveId: string, itemId: string): Promise<SharePointFetchResponse | null> {
    return apiRequest<SharePointFetchResponse | null>(`${API_BASE}/fetch-document`, {
      method: 'POST',
      body: JSON.stringify({ siteId, driveId, itemId }),
      cache: 'no-store',
    });
  },

  async createPage(siteId: string, title: string, content: string): Promise<SharePointFetchResponse | null> {
    return apiRequest<SharePointFetchResponse | null>(`${API_BASE}/create-page`, {
      method: 'POST',
      body: JSON.stringify({ siteId, title, content }),
      cache: 'no-store',
    });
  },

  async getSites(): Promise<SharePointSite[] | null> {
    return apiRequest<SharePointSite[] | null>(`${API_BASE}/sites`, { cache: 'no-store' });
  },
};
