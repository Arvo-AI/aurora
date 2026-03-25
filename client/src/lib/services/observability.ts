import { apiGet, apiPost } from './api-client';

// ─── Types ──────────────────────────────────────────────────────────

export interface ObservabilitySummary {
  total_resources: number;
  by_provider: Record<string, number>;
  by_category: Record<string, number>;
  by_status: Record<string, number>;
}

export interface Resource {
  id: string;
  name: string;
  display_name: string;
  resource_type: string;
  sub_type: string;
  category: string;
  provider: string;
  region: string;
  status: string;
  cloud_resource_id: string;
  endpoint: string;
  metadata: Record<string, unknown>;
  updated_at: string;
}

export interface ResourcesResponse {
  resources: Resource[];
  total: number;
  page: number;
  limit: number;
  total_pages: number;
}

export interface ResourceDetail extends Resource {
  upstream: Array<{ name: string; type: string; confidence: number }>;
  downstream: Array<{ name: string; type: string; confidence: number }>;
  impact: Record<string, string[]>;
  total_affected: number;
  incidents: Array<{
    id: number;
    title: string;
    severity: string;
    status: string;
    created_at: string;
  }>;
  alerts: Array<{
    title: string;
    state: string;
    triggered_at: string;
    source: string;
  }>;
  k8s_workloads?: {
    pods: Array<{ name: string; namespace: string; status: string }>;
    deployments: Array<{ name: string; namespace: string; replicas: number }>;
    services: Array<{ name: string; namespace: string; type: string }>;
  };
}

export interface OnPremResourceInput {
  name: string;
  resource_type?: string;
  sub_type?: string;
  ip_address?: string;
  port?: number;
  metadata?: Record<string, unknown>;
  status?: string;
}

// ─── API Functions ──────────────────────────────────────────────────

export function fetchSummary(): Promise<ObservabilitySummary> {
  return apiGet<ObservabilitySummary>('/api/observability/summary');
}

export function fetchResources(params?: {
  provider?: string;
  category?: string;
  resource_type?: string;
  status?: string;
  search?: string;
  page?: number;
  limit?: number;
}): Promise<ResourcesResponse> {
  const searchParams = new URLSearchParams();
  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== '') {
        searchParams.set(key, String(value));
      }
    });
  }
  const qs = searchParams.toString();
  return apiGet<ResourcesResponse>(`/api/observability/resources${qs ? `?${qs}` : ''}`);
}

export function fetchResourceDetail(resourceId: string): Promise<ResourceDetail> {
  return apiGet<ResourceDetail>(`/api/observability/resources/${encodeURIComponent(resourceId)}`);
}

export function registerOnPremResource(data: OnPremResourceInput): Promise<{ id: number; name: string; status: string }> {
  return apiPost('/api/observability/onprem', data);
}
