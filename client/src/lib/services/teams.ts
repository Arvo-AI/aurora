import { apiRequest } from "@/lib/services/api-client";

export interface TeamsStatus {
  connected: boolean;
  tenant_id?: string;
  team_name?: string;
  user_name?: string;
  connected_at?: number;
  incidents_channel_name?: string;
  error?: string;
}

export interface TeamsConnectedChannel {
  channel_id: string;
  team_id?: string;
  team_name?: string;
  channel_name?: string;
  is_private?: boolean;
  is_member?: boolean;
  channel_type?: string;
  detected_platform?: string | null;
  metadata_summary?: string | null;
  metadata_status?: string;
  is_dismissed?: boolean;
}

export interface TeamsChannelsResponse {
  connected: TeamsConnectedChannel[];
  dismissed: TeamsConnectedChannel[];
  card_channel_id?: string | null;
}

const API_BASE = "/api/teams";
const CHANNELS_BASE = "/api/teams/channels";

export const teamsService = {
  async getStatus(): Promise<TeamsStatus | null> {
    try {
      const data = await apiRequest<Record<string, unknown>>(API_BASE, { cache: "no-store" });
      return {
        connected: Boolean(data?.connected),
        tenant_id: data?.tenant_id as string | undefined,
        team_name: data?.team_name as string | undefined,
        user_name: data?.user_name as string | undefined,
        connected_at: data?.connected_at as number | undefined,
        incidents_channel_name: data?.incidents_channel_name as string | undefined,
        error: data?.error as string | undefined,
      };
    } catch (error) {
      console.error("[teamsService] Failed to fetch status:", error);
      return null;
    }
  },

  async connect(): Promise<{ oauth_url: string; message: string }> {
    return apiRequest(`${API_BASE}`, { method: "POST", cache: "no-store" });
  },

  async disconnect(): Promise<void> {
    await apiRequest(API_BASE, { method: "DELETE", cache: "no-store" });
  },

  async getChannels(pollMode = false): Promise<TeamsChannelsResponse> {
    const url = pollMode ? `${CHANNELS_BASE}?live=0` : CHANNELS_BASE;
    const data = await apiRequest<TeamsChannelsResponse>(url, { cache: "no-store" });
    return {
      connected: data?.connected ?? [],
      dismissed: data?.dismissed ?? [],
      card_channel_id: data?.card_channel_id ?? null,
    };
  },

  async dismissChannel(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/dismiss`, { method: "POST", cache: "no-store" });
  },

  async restoreChannel(channelId: string, teamId?: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/restore`, {
      method: "POST",
      body: JSON.stringify(teamId ? { team_id: teamId } : {}),
      cache: "no-store",
    });
  },

  async updateChannelDescription(channelId: string, summary: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${channelId}/metadata`, {
      method: "PUT",
      body: JSON.stringify({ metadata_summary: summary }),
      cache: "no-store",
    });
  },

  async setCardChannel(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/card-channel`, {
      method: "PUT",
      body: JSON.stringify({ channel_id: channelId }),
      cache: "no-store",
    });
  },

  async regenerateChannelDescription(channelId: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/metadata/generate`, {
      method: "POST",
      body: JSON.stringify({ channel_id: channelId }),
      cache: "no-store",
    });
  },

  async activateChannels(channelIds: string[]): Promise<{ queued: number }> {
    const data = await apiRequest<{ queued: number }>(`${CHANNELS_BASE}/activate`, {
      method: "POST",
      body: JSON.stringify({ channel_ids: channelIds }),
      cache: "no-store",
    });
    return { queued: data?.queued ?? 0 };
  },

  async refreshChannels(): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/refresh`, { method: "POST", cache: "no-store" });
  },
};
