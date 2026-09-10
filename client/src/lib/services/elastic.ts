'use client';

import { apiRequest } from '@/lib/services/api-client';

type UnknownRecord = Record<string, unknown>;

export type ElasticDeploymentType = 'cloud_hosted' | 'serverless' | 'self_managed';

export interface ElasticStatus {
  connected: boolean;
  deploymentType?: ElasticDeploymentType;
  clusterName?: string;
  version?: string;
  elasticsearchUrl?: string;
  kibanaUrl?: string;
  kibanaReachable?: boolean;
  indexPattern?: string;
  username?: string;
  hasWebhookSecret?: boolean;
  error?: string;
}

export interface ElasticConnectPayload {
  apiKey: string;
  cloudId?: string;
  elasticsearchUrl?: string;
  kibanaUrl?: string;
  indexPattern?: string;
}

export interface ElasticWebhookInfo {
  webhookUrl: string;
  webhookSecret: string;
  headerName: string;
  basicAuthUsername: string;
  actionBodyTemplate: string;
  instructions: string[];
}

export interface ElasticAlert {
  id: number;
  alertId?: string;
  alertUuid?: string;
  title?: string;
  state?: string;
  ruleId?: string;
  ruleName?: string;
  ruleType?: string;
  actionGroup?: string;
  reason?: string;
  severity?: string;
  viewInAppUrl?: string;
  payload?: Record<string, unknown>;
  receivedAt?: string;
  createdAt?: string;
}

export interface ElasticAlertsResponse {
  alerts: ElasticAlert[];
  total: number;
  limit: number;
  offset: number;
}

export interface ElasticRcaSettings {
  rcaEnabled: boolean;
}

const API_BASE = '/api/elastic';
const CACHE_KEY = 'elastic_connection_status';
const CONNECTED_FLAG = 'isElasticConnected';

export type CachedElasticStatus = Pick<ElasticStatus, 'connected' | 'deploymentType' | 'clusterName' | 'kibanaUrl'>;

function toStatus(data: UnknownRecord | null | undefined, fallbackConnected: boolean): ElasticStatus {
  return {
    connected: fallbackConnected,
    deploymentType: (data?.deploymentType ?? data?.deployment_type) as ElasticDeploymentType | undefined,
    clusterName: (data?.clusterName ?? data?.cluster_name) as string | undefined,
    version: data?.version as string | undefined,
    elasticsearchUrl: (data?.elasticsearchUrl ?? data?.elasticsearch_url) as string | undefined,
    kibanaUrl: (data?.kibanaUrl ?? data?.kibana_url) as string | undefined,
    kibanaReachable: Boolean(data?.kibanaReachable ?? data?.kibana_reachable),
    indexPattern: (data?.indexPattern ?? data?.index_pattern) as string | undefined,
    username: data?.username as string | undefined,
    hasWebhookSecret: Boolean(data?.hasWebhookSecret ?? data?.has_webhook_secret),
    error: data?.error as string | undefined,
  };
}

export const elasticService = {
  async getStatus(): Promise<ElasticStatus | null> {
    try {
      const data = await apiRequest<UnknownRecord>(`${API_BASE}/status`, { cache: 'no-store' });
      return toStatus(data, Boolean(data?.connected));
    } catch (error) {
      console.error('[elasticService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: ElasticConnectPayload): Promise<ElasticStatus> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
      retries: 0,
      timeout: 45_000,
    });
    return toStatus(data, Boolean(data?.success));
  },

  async getWebhookUrl(): Promise<ElasticWebhookInfo> {
    return apiRequest<ElasticWebhookInfo>(`${API_BASE}/webhook-url`, { cache: 'no-store' });
  },

  async getAlerts(limit = 20, offset = 0, state?: string): Promise<ElasticAlertsResponse> {
    const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
    if (state) params.set('state', state);
    return apiRequest<ElasticAlertsResponse>(`${API_BASE}/alerts?${params.toString()}`, { cache: 'no-store' });
  },

  async getRcaSettings(): Promise<ElasticRcaSettings> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/rca-settings`, { cache: 'no-store' });
    return { rcaEnabled: Boolean(data?.rcaEnabled) };
  },

  async updateRcaSettings(rcaEnabled: boolean): Promise<ElasticRcaSettings> {
    const data = await apiRequest<UnknownRecord>(`${API_BASE}/rca-settings`, {
      method: 'PUT',
      body: JSON.stringify({ rcaEnabled }),
      cache: 'no-store',
    });
    return { rcaEnabled: Boolean(data?.rcaEnabled) };
  },

  loadCachedStatus(): CachedElasticStatus | null {
    if (globalThis.window === undefined) return null;
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      if (!raw) return null;
      return JSON.parse(raw) as CachedElasticStatus;
    } catch {
      return null;
    }
  },

  cacheStatus(status: ElasticStatus): void {
    if (globalThis.window === undefined) return;
    const slim: CachedElasticStatus = {
      connected: status.connected,
      deploymentType: status.deploymentType,
      clusterName: status.clusterName,
      kibanaUrl: status.kibanaUrl,
    };
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify(slim));
      if (status.connected) localStorage.setItem(CONNECTED_FLAG, 'true');
      else localStorage.removeItem(CONNECTED_FLAG);
    } catch {
      /* storage unavailable */
    }
  },

  clearCachedStatus(): void {
    if (globalThis.window === undefined) return;
    try {
      localStorage.removeItem(CACHE_KEY);
      localStorage.removeItem(CONNECTED_FLAG);
    } catch {
      /* storage unavailable */
    }
  },
};
