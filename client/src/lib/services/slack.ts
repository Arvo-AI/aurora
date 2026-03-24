import { apiRequest } from '@/lib/services/api-client';

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

export const slackService = {
  async getStatus(): Promise<SlackStatus | null> {
    try {
      const data = await apiRequest<Record<string, any>>(`${API_BASE}`, {
        cache: 'no-store',
      });
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
    const data = await apiRequest<SlackConnectResponse>(`${API_BASE}`, {
      method: 'POST',
      cache: 'no-store',
    });
    return data;
  },

  async disconnect(): Promise<void> {
    await apiRequest(`${API_BASE}`, {
      method: 'DELETE',
      cache: 'no-store',
    });
  },

  async listChannels(): Promise<SlackChannel[]> {
    const data = await apiRequest<{ channels: SlackChannel[] }>(`${API_BASE}/channels`, {
      cache: 'no-store',
    });
    return data.channels || [];
  },
};
