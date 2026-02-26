interface CloudBeesServer {
  version?: string;
  mode?: string;
  numExecutors?: number;
}

interface CloudBeesJobHealth {
  healthy: number;
  unstable: number;
  failing: number;
  disabled: number;
  other: number;
}

interface CloudBeesSummary {
  jobCount: number;
  jobHealth: CloudBeesJobHealth;
  queueSize: number;
  nodesOnline: number;
  nodesOffline: number;
  totalExecutors: number;
  busyExecutors: number;
}

export interface CloudBeesStatus {
  connected: boolean;
  baseUrl?: string;
  username?: string;
  server?: CloudBeesServer | null;
  summary?: CloudBeesSummary | null;
  error?: string;
}

export interface CloudBeesConnectPayload {
  baseUrl: string;
  username: string;
  apiToken: string;
}

export interface CloudBeesDeploymentEvent {
  id: number;
  service: string;
  environment: string;
  result: string;
  buildNumber: number;
  buildUrl: string;
  commitSha: string;
  branch: string;
  repository: string;
  deployer: string;
  durationMs: number | null;
  jobName: string;
  traceId: string | null;
  receivedAt: string | null;
}

export interface CloudBeesWebhookInfo {
  webhookUrl: string;
  jenkinsfileBasic: string;
  jenkinsfileOtel: string;
  jenkinsfileCurl: string;
  instructions: string[];
}

const API_BASE = '/api/cloudbees';

async function fetchJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });

  if (!response.ok) {
    const body = await response.json().catch(() => null) as { error?: string; details?: string } | null;
    throw new Error(body?.error || body?.details || response.statusText || `Request failed (${response.status})`);
  }

  return response.json();
}

export const cloudbeesService = {
  async getStatus(): Promise<CloudBeesStatus | null> {
    try {
      const raw = await fetchJson<Record<string, unknown>>(`${API_BASE}/status?full=true`);
      return {
        connected: Boolean(raw?.connected),
        baseUrl: (raw?.baseUrl ?? raw?.base_url) as string | undefined,
        username: raw?.username as string | undefined,
        server: (raw?.server as CloudBeesServer) ?? null,
        summary: (raw?.summary as CloudBeesSummary) ?? null,
        error: raw?.error as string | undefined,
      };
    } catch (error) {
      console.error('[cloudbeesService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: CloudBeesConnectPayload): Promise<CloudBeesStatus> {
    const raw = await fetchJson<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success ?? true),
      baseUrl: (raw?.baseUrl as string) ?? payload.baseUrl,
      username: (raw?.username as string) ?? payload.username,
      server: (raw?.server as CloudBeesServer) ?? null,
    };
  },

  async getWebhookUrl(): Promise<CloudBeesWebhookInfo | null> {
    try {
      return await fetchJson<CloudBeesWebhookInfo>(`${API_BASE}/webhook-url`);
    } catch (error) {
      console.error('[cloudbeesService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async getDeployments(limit = 10): Promise<{ deployments: CloudBeesDeploymentEvent[]; total: number } | null> {
    try {
      return await fetchJson<{ deployments: CloudBeesDeploymentEvent[]; total: number }>(
        `${API_BASE}/deployments?limit=${limit}`
      );
    } catch (error) {
      console.error('[cloudbeesService] Failed to fetch deployments:', error);
      return null;
    }
  },
};
