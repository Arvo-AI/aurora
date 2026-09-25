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
  metadata_summary?: string | null;
  metadata_status?: string;
  is_dismissed?: boolean;
}

export interface SlackChannelsResponse {
  connected: SlackConnectedChannel[];
  dismissed: SlackConnectedChannel[];
  // The single channel that receives the structured incident card, or null.
  card_channel_id?: string | null;
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

  // Fetch the org's channels. `pollMode` (?live=0) serves stored rows straight
  // from the DB with NO Slack calls — used by the status poll, which only needs
  // to watch metadata_status while descriptions generate and must not trigger
  // the rate-limited workspace re-list on every tick. A normal load omits it so
  // the backend reconciles membership + live-lists available channels.
  async getChannels(pollMode = false): Promise<SlackChannelsResponse> {
    const url = pollMode ? `${CHANNELS_BASE}?live=0` : `${CHANNELS_BASE}`;
    const data = await apiRequest<SlackChannelsResponse>(url, {
      cache: 'no-store',
    });
    return {
      connected: data?.connected ?? [],
      dismissed: data?.dismissed ?? [],
      card_channel_id: data?.card_channel_id ?? null,
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

  // Designate the single channel that receives the structured incident card.
  // Must be an active channel; the backend 409s otherwise.
  async setCardChannel(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/card-channel`, {
      method: 'PUT',
      body: JSON.stringify({ channel_id: channelId }),
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

  // Bulk-activate indexed channels: joins + describes them on the worker (so a
  // large batch can't rate-limit/timeout the request). Returns how many were
  // queued; rows/descriptions land asynchronously and the manage page polls.
  async activateChannels(channelIds: string[]): Promise<{ queued: number }> {
    const data = await apiRequest<{ queued: number }>(`${CHANNELS_BASE}/activate`, {
      method: 'POST',
      body: JSON.stringify({ channel_ids: channelIds }),
      cache: 'no-store',
    });
    return { queued: data?.queued ?? 0 };
  },
};
