'use client';

type UnknownRecord = Record<string, unknown>;

export interface CorootProject {
  id: string;
  name?: string;
}

export interface CorootStatus {
  connected: boolean;
  url?: string;
  email?: string;
  projects?: CorootProject[];
  error?: string;
  validatedAt?: string;
}

export interface CorootConnectPayload {
  url: string;
  email: string;
  password: string;
}

export interface CorootConnectResponse {
  success: boolean;
  url: string;
  projects: CorootProject[];
}

const API_BASE = '/api/coroot';

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
  return parsed ?? ({} as T);
}

export const corootService = {
  async getStatus(): Promise<CorootStatus | null> {
    try {
      const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/status`);
      return {
        connected: Boolean(data?.connected),
        url: data?.url as string | undefined,
        email: data?.email as string | undefined,
        projects: (data?.projects as CorootProject[] | undefined) ?? [],
        error: data?.error as string | undefined,
        validatedAt: ((data?.validatedAt ?? data?.validated_at) as string | undefined) ?? undefined,
      };
    } catch (error) {
      console.error('[corootService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: CorootConnectPayload): Promise<CorootConnectResponse> {
    return handleJsonFetch<CorootConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },
};
