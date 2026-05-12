import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface SentryStatus {
  connected: boolean;
  orgSlug?: string;
  baseUrl?: string;
  orgName?: string | null;
  error?: string;
  validatedAt?: string;
}

export interface SentryConnectPayload {
  authToken: string;
  clientSecret: string;
  orgSlug: string;
  baseUrl?: string;
}

export interface SentryWebhookInfo {
  webhookUrl: string;
}

export interface SentryIngestedEvent {
  id: number;
  eventType?: string;
  title?: string;
  status?: string;
  scope?: string;
  payload: UnknownRecord;
  receivedAt?: string;
  createdAt?: string;
}

export interface SentryIngestedEventsResponse {
  events: SentryIngestedEvent[];
  total: number;
  limit: number;
  offset: number;
}

const API_BASE = '/api/sentry';

export const sentryService = {
  async getStatus(): Promise<SentryStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        orgSlug: (data?.orgSlug ?? data?.org_slug) as string | undefined,
        baseUrl: (data?.baseUrl ?? data?.base_url) as string | undefined,
        orgName: ((data?.orgName ?? data?.org_name) as string | undefined | null) ?? null,
        error: data?.error as string | undefined,
        validatedAt: ((data?.validatedAt ?? data?.validated_at) as string | undefined) ?? undefined,
      };
    } catch (error) {
      console.error('[sentryService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: SentryConnectPayload): Promise<SentryStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    return {
      connected: Boolean(data?.success ?? true),
      orgSlug: (data?.orgSlug ?? payload.orgSlug) as string | undefined,
      baseUrl: (data?.baseUrl ?? payload.baseUrl) as string | undefined,
      orgName: (data?.orgName as string | undefined) ?? null,
      validatedAt: (data?.validatedAt as string | undefined) ?? undefined,
    };
  },

  async getWebhookUrl(): Promise<SentryWebhookInfo> {
    return apiRequest<SentryWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },

  async getIssues(params: URLSearchParams): Promise<UnknownRecord> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/issues?${qs}` : `${API_BASE}/issues`;
    return apiRequest<UnknownRecord>(url, { cache: 'no-store' });
  },

  async getIngestedEvents(params: URLSearchParams): Promise<SentryIngestedEventsResponse> {
    const qs = params.toString();
    const url = qs ? `${API_BASE}/events/ingested?${qs}` : `${API_BASE}/events/ingested`;
    return apiRequest<SentryIngestedEventsResponse>(url, { cache: 'no-store' });
  },
};
