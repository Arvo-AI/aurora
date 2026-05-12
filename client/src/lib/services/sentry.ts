'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export interface SentryProjectSummary {
  id?: string;
  slug?: string;
  name?: string;
  platform?: string;
  isMember?: boolean;
}

export interface SentryStatus {
  connected: boolean;
  region?: string;
  orgSlug?: string;
  orgName?: string;
  validatedAt?: string;
  hasWebhookSecret?: boolean;
  accessibleProjects?: SentryProjectSummary[];
  error?: string;
}

export interface SentryConnectPayload {
  authToken: string;
  orgSlug: string;
  region?: string;
  webhookSecret?: string;
}

export interface SentryWebhookInfo {
  webhookUrl: string;
  instructions: string[];
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
        region: data?.region as string | undefined,
        orgSlug: (data?.orgSlug ?? data?.org_slug) as string | undefined,
        orgName: (data?.orgName ?? data?.org_name) as string | undefined,
        validatedAt: (data?.validatedAt ?? data?.validated_at) as string | undefined,
        hasWebhookSecret: Boolean(data?.hasWebhookSecret ?? data?.has_webhook_secret),
        accessibleProjects: (data?.accessibleProjects ?? data?.accessible_projects) as SentryProjectSummary[] | undefined,
        error: data?.error as string | undefined,
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
      connected: Boolean(data?.success),
      region: (data?.region ?? payload.region) as string | undefined,
      orgSlug: (data?.orgSlug ?? payload.orgSlug) as string | undefined,
      orgName: data?.orgName as string | undefined,
      validatedAt: data?.validatedAt as string | undefined,
      hasWebhookSecret: Boolean(data?.hasWebhookSecret),
      accessibleProjects: data?.accessibleProjects as SentryProjectSummary[] | undefined,
    };
  },

  async getWebhookUrl(): Promise<SentryWebhookInfo> {
    return apiRequest<SentryWebhookInfo>(`${API_BASE}/webhook-url`, {
      cache: 'no-store',
    });
  },
};
