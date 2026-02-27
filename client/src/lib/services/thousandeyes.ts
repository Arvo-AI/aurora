'use client';

type UnknownRecord = Record<string, unknown>;

export interface ThousandEyesAccountGroup {
  aid: string;
  accountGroupName?: string;
}

export interface ThousandEyesStatus {
  connected: boolean;
  account_group_id?: string;
  account_groups?: ThousandEyesAccountGroup[];
  error?: string;
  validatedAt?: string;
}

export interface ThousandEyesConnectPayload {
  api_token: string;
  account_group_id?: string;
}

export interface ThousandEyesConnectResponse {
  success: boolean;
  account_groups: ThousandEyesAccountGroup[];
}

const API_BASE = '/api/thousandeyes';

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
  if (parsed === null) {
    throw new Error(`Empty response body from ${typeof input === 'string' ? input : 'request'}`);
  }
  return parsed;
}

export const thousandEyesService = {
  async getStatus(): Promise<ThousandEyesStatus | null> {
    try {
      const data = await handleJsonFetch<UnknownRecord>(`${API_BASE}/status`);
      return {
        connected: Boolean(data?.connected),
        account_group_id: data?.account_group_id as string | undefined,
        account_groups: (data?.account_groups as ThousandEyesAccountGroup[] | undefined) ?? [],
        error: data?.error as string | undefined,
        validatedAt: (data?.validatedAt ?? data?.validated_at) as string | undefined,
      };
    } catch (error) {
      console.error('[thousandEyesService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: ThousandEyesConnectPayload): Promise<ThousandEyesConnectResponse> {
    return await handleJsonFetch<ThousandEyesConnectResponse>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },
};
