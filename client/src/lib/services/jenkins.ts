'use client';

interface JenkinsServer {
  version?: string;
  mode?: string;
  numExecutors?: number;
}

interface JenkinsJobHealth {
  healthy: number;
  unstable: number;
  failing: number;
  disabled: number;
  other: number;
}

interface JenkinsSummary {
  jobCount: number;
  jobHealth: JenkinsJobHealth;
  queueSize: number;
  nodesOnline: number;
  nodesOffline: number;
  totalExecutors: number;
  busyExecutors: number;
}

export interface JenkinsStatus {
  connected: boolean;
  baseUrl?: string;
  username?: string;
  server?: JenkinsServer | null;
  summary?: JenkinsSummary | null;
  error?: string;
}

export interface JenkinsConnectPayload {
  baseUrl: string;
  username: string;
  apiToken: string;
}

const API_BASE = '/api/jenkins';

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

export const jenkinsService = {
  async getStatus(): Promise<JenkinsStatus | null> {
    try {
      const raw = await fetchJson<Record<string, unknown>>(`${API_BASE}/status`);
      return {
        connected: Boolean(raw?.connected),
        baseUrl: (raw?.baseUrl ?? raw?.base_url) as string | undefined,
        username: raw?.username as string | undefined,
        server: (raw?.server as JenkinsServer) ?? null,
        summary: (raw?.summary as JenkinsSummary) ?? null,
        error: raw?.error as string | undefined,
      };
    } catch (error) {
      console.error('[jenkinsService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: JenkinsConnectPayload): Promise<JenkinsStatus> {
    const raw = await fetchJson<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success ?? true),
      baseUrl: (raw?.baseUrl as string) ?? payload.baseUrl,
      username: (raw?.username as string) ?? payload.username,
      server: (raw?.server as JenkinsServer) ?? null,
    };
  },
};
