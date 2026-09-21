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

export interface SlackConnectedChannel {
  channel_id: string;
  channel_name?: string;
  is_private?: boolean;
  is_member?: boolean;
  channel_type?: string;
  detected_platform?: string | null;
  notify_enabled?: boolean;
  metadata_summary?: string | null;
  metadata_status?: string;
  is_dismissed?: boolean;
}

export interface SlackChannelsResponse {
  connected: SlackConnectedChannel[];
  dismissed: SlackConnectedChannel[];
}

const API_BASE = '/api/slack';
const CHANNELS_BASE = '/api/slack/channels';

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

  async getChannels(): Promise<SlackChannelsResponse> {
    const data = await apiRequest<SlackChannelsResponse>(`${CHANNELS_BASE}`, {
      cache: 'no-store',
    });
    return {
      connected: data?.connected ?? [],
      dismissed: data?.dismissed ?? [],
    };
  },

  // Dismiss = hide from routing/UI; does NOT leave the Slack channel.
  async dismissChannel(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/dismiss`, {
      method: 'POST',
      cache: 'no-store',
    });
  },

  async restoreChannel(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/restore`, {
      method: 'POST',
      cache: 'no-store',
    });
  },

  async updateChannelDescription(channelId: string, summary: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/metadata`, {
      method: 'PUT',
      body: JSON.stringify({ metadata_summary: summary }),
      cache: 'no-store',
    });
  },

  async setChannelNotify(channelId: string, enabled: boolean): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/notify`, {
      method: 'PUT',
      body: JSON.stringify({ enabled }),
      cache: 'no-store',
    });
  },

  async regenerateChannelDescription(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/metadata/generate`, {
      method: 'POST',
      body: JSON.stringify({ channel_id: channelId }),
      cache: 'no-store',
    });
  },

  // Bulk-activate indexed channels: describe them + make them routable. Powers
  // the "search a keyword, check matches, activate" flow for large workspaces.
  async activateChannels(channelIds: string[]): Promise<{ activated: number }> {
    const data = await apiRequest<{ activated: number }>(`${CHANNELS_BASE}/activate`, {
      method: 'POST',
      body: JSON.stringify({ channel_ids: channelIds }),
      cache: 'no-store',
    });
    return { activated: data?.activated ?? 0 };
  },

  // Re-scan the workspace and register any newly-visible channels. Used by the
  // "Refresh channels" button so workspaces connected before auto-registration
  // (or with new channels) get updated without reconnecting.
  async refreshChannels(): Promise<{ described: number }> {
    const data = await apiRequest<{ described: number }>(`${CHANNELS_BASE}/refresh`, {
      method: 'POST',
      cache: 'no-store',
    });
    return { described: data?.described ?? 0 };
  },
};
