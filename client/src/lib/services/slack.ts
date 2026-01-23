export interface SlackStatus {
  connected: boolean;
  team_name?: string;
  user_name?: string;
  team_id?: string;
  team_url?: string;
  connected_at?: number;
  incidents_channel_name?: string;
  error?: string;
}

export interface SlackConnectResponse {
  oauth_url: string;
  message: string;
}

export interface SlackChannel {
  id: string;
  name: string;
  is_member: boolean;
  is_private: boolean;
  num_members?: number;
}

const API_BASE = '/api/slack';

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

export const slackService = {
  async getStatus(): Promise<SlackStatus | null> {
    try {
      const data = await handleJsonFetch<Record<string, any>>(`${API_BASE}`);
      return {
        connected: Boolean(data?.connected),
        team_name: data?.team_name ?? data?.teamName,
        user_name: data?.user_name ?? data?.userName,
        team_id: data?.team_id ?? data?.teamId,
        team_url: data?.team_url ?? data?.teamUrl,
        connected_at: data?.connected_at ?? data?.connectedAt,
        incidents_channel_name: data?.incidents_channel_name,
        error: data?.error,
      };
    } catch (error) {
      console.error('[slackService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(): Promise<SlackConnectResponse> {
    const data = await handleJsonFetch<SlackConnectResponse>(`${API_BASE}`, {
      method: 'POST',
    });
    return data;
  },

  async disconnect(): Promise<void> {
    await handleJsonFetch(`${API_BASE}`, {
      method: 'DELETE',
    });
  },

  async listChannels(): Promise<SlackChannel[]> {
    const data = await handleJsonFetch<{ channels: SlackChannel[] }>(`${API_BASE}/channels`);
    return data.channels || [];
  },
};

