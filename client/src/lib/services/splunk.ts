import { apiRequest } from '@/lib/services/api-client';

interface SplunkServer {
  name?: string;
  version?: string;
  instanceType?: string;
}

export interface SplunkAccount {
  accountId: string;
  baseUrl?: string;
  serverName?: string;
  version?: string;
  instanceType?: string;
  username?: string;
  allowed?: boolean;
  valid?: boolean;
  error?: string;
}

export interface SplunkStatus {
  connected: boolean;
  baseUrl?: string;
  server?: SplunkServer | null;
  username?: string;
  error?: string;
  accounts?: SplunkAccount[];
}

export interface SplunkConnectPayload {
  baseUrl: string;
  apiToken: string;
}

export interface SplunkAlert {
  id: number;
  alertId?: string;
  title?: string;
  state?: string;
  searchName?: string;
  searchQuery?: string;
  resultCount?: number;
  severity?: string;
  payload?: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface SplunkAlertsResponse {
  alerts: SplunkAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface SplunkWebhookUrlResponse {
  webhookUrl: string;
  instructions: string[];
}

export interface SplunkSearchPayload {
  query: string;
  earliestTime?: string;
  latestTime?: string;
  maxCount?: number;
  accountId?: string;
}

export interface SplunkSearchResult {
  success: boolean;
  results: Record<string, unknown>[];
  count: number;
}

export interface SplunkJobStatus {
  sid: string;
  dispatchState?: string;
  isDone: boolean;
  isFailed: boolean;
  resultCount: number;
  scanCount?: number;
  eventCount?: number;
  doneProgress?: number;
  runDuration?: number;
}

export interface SplunkJobResponse {
  success: boolean;
  sid?: string;
  message?: string;
}

export interface SplunkJobResultsResponse {
  results: Record<string, unknown>[];
  count: number;
  offset: number;
}

export interface SplunkRcaSettings {
  rcaEnabled: boolean;
}

function mapAccount(raw: unknown): SplunkAccount {
  const account = (raw ?? {}) as Record<string, unknown>;
  return {
    accountId: typeof account.accountId === 'string' ? account.accountId : '',
    baseUrl: account.baseUrl as string | undefined,
    serverName: account.serverName as string | undefined,
    version: account.version as string | undefined,
    instanceType: account.instanceType as string | undefined,
    username: account.username as string | undefined,
    allowed: account.allowed as boolean | undefined,
    valid: account.valid as boolean | undefined,
    error: account.error as string | undefined,
  };
}

function withAccount(path: string, accountId?: string): string {
  if (!accountId) return path;
  const joiner = path.includes('?') ? '&' : '?';
  return `${path}${joiner}account=${encodeURIComponent(accountId)}`;
}

const API_BASE = '/api/splunk';

export const splunkService = {
  async getStatus(): Promise<SplunkStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      if (!raw) {
        return null;
      }
      return {
        connected: Boolean(raw.connected),
        baseUrl: raw.baseUrl as string | undefined,
        server: raw.server as SplunkServer | null,
        username: raw.username as string | undefined,
        error: raw.error as string | undefined,
        accounts: Array.isArray(raw.accounts) ? raw.accounts.map(mapAccount) : [],
      };
    } catch (error) {
      console.error('[splunkService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: SplunkConnectPayload): Promise<SplunkStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(raw?.success ?? raw?.connected),
      baseUrl: (raw?.baseUrl ?? payload.baseUrl) as string,
      server: raw?.server as SplunkServer | null,
      username: raw?.username as string | undefined,
      accounts: Array.isArray(raw?.accounts) ? raw.accounts.map(mapAccount) : [],
      error: raw?.error as string | undefined,
    };
  },

  async getAlerts(limit = 50, offset = 0, state?: string): Promise<SplunkAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.append('state', state);

    const raw = await apiRequest<SplunkAlertsResponse>(`${API_BASE}/alerts?${params}`, {
      cache: 'no-store',
    });
    return raw ?? { alerts: [], total: 0, limit, offset };
  },

  async getWebhookUrl(): Promise<SplunkWebhookUrlResponse | null> {
    try {
      return await apiRequest<SplunkWebhookUrlResponse>(`${API_BASE}/alerts/webhook-url`, {
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[splunkService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async search(payload: SplunkSearchPayload): Promise<SplunkSearchResult> {
    const raw = await apiRequest<SplunkSearchResult>(`${API_BASE}/search`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return raw ?? { success: false, results: [], count: 0 };
  },

  async createSearchJob(payload: SplunkSearchPayload): Promise<SplunkJobResponse> {
    const raw = await apiRequest<SplunkJobResponse>(`${API_BASE}/search/jobs`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return raw ?? { success: false };
  },

  async getJobStatus(sid: string, accountId?: string): Promise<SplunkJobStatus> {
    const raw = await apiRequest<SplunkJobStatus>(withAccount(`${API_BASE}/search/jobs/${sid}`, accountId), {
      cache: 'no-store',
    });
    return raw ?? { sid, isDone: false, isFailed: true, resultCount: 0 };
  },

  async getJobResults(sid: string, options?: { offset?: number; count?: number; accountId?: string }): Promise<SplunkJobResultsResponse> {
    const offset = options?.offset ?? 0;
    const count = options?.count ?? 1000;
    const raw = await apiRequest<SplunkJobResultsResponse>(
      withAccount(
        `${API_BASE}/search/jobs/${sid}?results=true&offset=${offset}&count=${count}`,
        options?.accountId,
      ),
      { cache: 'no-store' },
    );
    return raw ?? { results: [], count: 0, offset };
  },

  async cancelJob(sid: string, accountId?: string): Promise<void> {
    await apiRequest(withAccount(`${API_BASE}/search/jobs/${sid}`, accountId), {
      method: 'DELETE',
      cache: 'no-store',
    });
  },

  async getRcaSettings(): Promise<SplunkRcaSettings> {
    try {
      const raw = await apiRequest<SplunkRcaSettings>(`${API_BASE}/rca-settings`, {
        cache: 'no-store',
      });
      return raw ?? { rcaEnabled: false };
    } catch (error) {
      console.error('[splunkService] Failed to fetch RCA settings:', error);
      return { rcaEnabled: false };
    }
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<SplunkRcaSettings> {
    const raw = await apiRequest<SplunkRcaSettings>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
      cache: 'no-store',
    });
    return raw ?? { rcaEnabled };
  },
};
