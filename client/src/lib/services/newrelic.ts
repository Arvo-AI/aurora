'use client';

type UnknownRecord = Record<string, unknown>;

export interface NewRelicStatus {
  connected: boolean;
  region?: string;
  accountId?: string;
  accountName?: string;
  userEmail?: string;
  userName?: string;
  validatedAt?: string;
  hasLicenseKey?: boolean;
  accessibleAccounts?: Array<{ id: number; name: string }>;
  error?: string;
}

export interface NewRelicConnectPayload {
  apiKey: string;
  accountId: string;
  region?: string;
  licenseKey?: string;
}

export interface NewRelicWebhookInfo {
  webhookUrl: string;
  instructions: string[];
}

export interface NewRelicIssue {
  issueId: string;
  title: string;
  priority: string;
  state: string;
  activatedAt?: string;
  closedAt?: string;
  createdAt?: string;
  entityNames?: string[];
  conditionName?: string;
  policyName?: string;
  totalIncidents?: number;
}

export interface NewRelicIngestedEvent {
  id: number;
  issueId?: string;
  title?: string;
  priority?: string;
  state?: string;
  entityNames?: string;
  payload: UnknownRecord;
  receivedAt?: string;
  createdAt?: string;
}

export interface NewRelicIngestedEventsResponse {
  events: NewRelicIngestedEvent[];
  total: number;
  limit: number;
  offset: number;
}

const API_BASE = '/api/newrelic';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) return null;
  return JSON.parse(text) as T;
}

async function handleJsonFetch<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
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

  const parsed = await parseJsonResponse<T>(response);
  return parsed ?? ({} as T);
}

export const newrelicService = {
  async getStatus(): Promise<NewRelicStatus | null> {
    try {
      const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/status`);
      return {
        connected: Boolean(data?.connected),
        region: data?.region as string | undefined,
        accountId: (data?.accountId ?? data?.account_id) as string | undefined,
        accountName: (data?.accountName ?? data?.account_name) as string | undefined,
        userEmail: (data?.userEmail ?? data?.user_email) as string | undefined,
        userName: (data?.userName ?? data?.user_name) as string | undefined,
        validatedAt: (data?.validatedAt ?? data?.validated_at) as string | undefined,
        hasLicenseKey: Boolean(data?.hasLicenseKey ?? data?.has_license_key),
        accessibleAccounts: (data?.accessibleAccounts ?? data?.accessible_accounts) as Array<{ id: number; name: string }> | undefined,
        error: data?.error as string | undefined,
      };
    } catch (error) {
      console.error('[newrelicService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: NewRelicConnectPayload): Promise<NewRelicStatus> {
    const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(data?.success ?? true),
      region: (data?.region ?? payload.region) as string | undefined,
      accountId: (data?.accountId ?? payload.accountId) as string | undefined,
      accountName: data?.accountName as string | undefined,
      userEmail: data?.userEmail as string | undefined,
      userName: data?.userName as string | undefined,
      validatedAt: data?.validatedAt as string | undefined,
      accessibleAccounts: data?.accessibleAccounts as Array<{ id: number; name: string }> | undefined,
    };
  },

  async getWebhookUrl(): Promise<NewRelicWebhookInfo> {
    return handleJsonFetch<NewRelicWebhookInfo>(`${API_BASE}/webhook-url`);
  },

  async getIssues(params: URLSearchParams): Promise<{ issues: NewRelicIssue[]; nextCursor?: string }> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/issues?${qs}` : `${API_BASE}/issues`;
    return handleJsonFetch(url);
  },

  async getIngestedEvents(params: URLSearchParams): Promise<NewRelicIngestedEventsResponse> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events/ingested?${qs}` : `${API_BASE}/events/ingested`;
    return handleJsonFetch<NewRelicIngestedEventsResponse>(url);
  },

  async executeNrql(query: string, accountId?: string): Promise<UnknownRecord> {
    return handleJsonFetch<UnknownRecord>(`${API_BASE}/nrql`, {
      method: 'POST',
      body: JSON.stringify({ query, accountId }),
    });
  },

  async getEntities(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/entities?${qs}` : `${API_BASE}/entities`;
    return handleJsonFetch<UnknownRecord>(url);
  },

  async getAccounts(): Promise<{ accounts: Array<{ id: number; name: string }> }> {
    return handleJsonFetch(`${API_BASE}/accounts`);
  },

  async pollIssues(): Promise<UnknownRecord> {
    return handleJsonFetch<UnknownRecord>(`${API_BASE}/poll-issues`, { method: 'POST' });
  },
};
