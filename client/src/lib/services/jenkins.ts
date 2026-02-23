'use client';

interface JenkinsServer {
  version?: string;
  mode?: string;
  numExecutors?: number;
}

export interface JenkinsStatus {
  connected: boolean;
  baseUrl?: string;
  username?: string;
  server?: JenkinsServer | null;
  error?: string;
}

export interface JenkinsConnectPayload {
  baseUrl: string;
  username: string;
  apiToken: string;
}

const API_BASE = '/api/jenkins';

async function parseJsonResponse<T>(response: Response): Promise<T | null> {
  const text = await response.text();
  if (!text) {
    return null;
  }
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
  return (parsed ?? ({} as T));
}

export const jenkinsService = {
  async getStatus(): Promise<JenkinsStatus | null> {
    try {
      const raw = await handleJsonFetch<Record<string, any>>(`${API_BASE}/status`);
      return {
        connected: Boolean(raw?.connected),
        baseUrl: raw?.baseUrl ?? raw?.base_url,
        username: raw?.username,
        server: raw?.server ?? null,
        error: raw?.error,
      };
    } catch (error) {
      console.error('[jenkinsService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: JenkinsConnectPayload): Promise<JenkinsStatus> {
    const raw = await handleJsonFetch<Record<string, any>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    return {
      connected: Boolean(raw?.success ?? true),
      baseUrl: raw?.baseUrl ?? payload.baseUrl,
      username: raw?.username ?? payload.username,
      server: raw?.server ?? null,
    };
  },
};
