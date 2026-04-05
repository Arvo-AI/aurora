import { apiGet } from '@/lib/services/api-client';

// ============================================================================
// Types
// ============================================================================

export interface MetricsSummary {
  totalIncidents: number;
  activeIncidents: number;
  resolvedIncidents: number;
  avgMttrSeconds: number;
  avgMttdSeconds: number;
  changeFailureRate: number;
  totalDeployments: number;
  topServices: { service: string; count: number }[];
}

export interface MttrBySeverity {
  severity: string;
  count: number;
  avgMttrSeconds: number;
  p50MttrSeconds: number;
  p95MttrSeconds: number;
  avgDetectionToRcaSeconds: number;
  avgRcaToResolveSeconds: number;
}

export interface MttrTrendPoint {
  date: string;
  avgMttrSeconds: number;
  count: number;
}

export interface MttrResponse {
  bySeverity: MttrBySeverity[];
  trend: MttrTrendPoint[];
}

export interface IncidentFrequencyPoint {
  date: string;
  group: string;
  count: number;
}

export interface IncidentFrequencyResponse {
  data: IncidentFrequencyPoint[];
  groupBy: string;
}

export interface ChangeFailureByService {
  service: string;
  totalDeployments: number;
  failureLinked: number;
  rate: number;
}

export interface ChangeFailureRateResponse {
  totalDeployments: number;
  failureLinked: number;
  changeFailureRate: number;
  windowHours: number;
  byService: ChangeFailureByService[];
}

export interface ToolStat {
  toolName: string;
  totalCalls: number;
  incidentsUsed: number;
}

export interface AgentExecutionResponse {
  toolStats: ToolStat[];
  avgStepsPerRca: number;
  totalRcasCompleted: number;
}

export type Period = '7d' | '30d' | '90d';

// ============================================================================
// Service
// ============================================================================

export const metricsService = {
  async getSummary(): Promise<MetricsSummary> {
    return apiGet<MetricsSummary>('/api/metrics/summary');
  },

  async getMttr(period: Period): Promise<MttrResponse> {
    return apiGet<MttrResponse>(`/api/metrics/mttr?period=${period}`);
  },

  async getIncidentFrequency(
    period: Period,
    groupBy: string = 'severity',
  ): Promise<IncidentFrequencyResponse> {
    return apiGet<IncidentFrequencyResponse>(
      `/api/metrics/incident-frequency?period=${period}&group_by=${groupBy}`,
    );
  },

  async getChangeFailureRate(
    period: Period,
    windowHours: number = 4,
  ): Promise<ChangeFailureRateResponse> {
    return apiGet<ChangeFailureRateResponse>(
      `/api/metrics/change-failure-rate?period=${period}&window_hours=${windowHours}`,
    );
  },

  async getAgentExecution(period: Period): Promise<AgentExecutionResponse> {
    return apiGet<AgentExecutionResponse>(
      `/api/metrics/agent-execution?period=${period}`,
    );
  },
};
