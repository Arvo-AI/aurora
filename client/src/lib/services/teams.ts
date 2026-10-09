import { apiRequest } from "@/lib/services/api-client";

export interface TeamsPendingSetupStep {
  id: string;
  title: string;
  detail: string;
}

export interface TeamsStatus {
  connected: boolean;
  /** Entra OAuth + Graph token valid. */
  oauth_connected?: boolean;
  /** Recent bot @mention received (Teams app + messaging endpoint). */
  bot_verified?: boolean;
  /** OAuth and bot path verified — same as GitHub “repos connected”. */
  setup_complete?: boolean;
  pending_setup?: TeamsPendingSetupStep[];
  tenant_id?: string;
  team_name?: string;
  user_name?: string;
  connected_at?: number;
  incidents_channel_name?: string;
  /** Unix seconds — last channel message Aurora received from the Teams bot. */
  last_bot_message_at?: number | null;
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
      const connected = Boolean(data?.connected);
      const setupComplete = data?.setup_complete as boolean | undefined;
      return {
        connected,
        oauth_connected: (data?.oauth_connected as boolean | undefined) ?? connected,
        bot_verified: Boolean(data?.bot_verified),
        setup_complete: setupComplete ?? (connected ? Boolean(data?.bot_verified) : false),
        pending_setup: (data?.pending_setup as TeamsPendingSetupStep[] | undefined) ?? [],
        tenant_id: data?.tenant_id as string | undefined,
        team_name: data?.team_name as string | undefined,
        user_name: data?.user_name as string | undefined,
        connected_at: data?.connected_at as number | undefined,
        incidents_channel_name: data?.incidents_channel_name as string | undefined,
        last_bot_message_at: (data?.last_bot_message_at as number | null | undefined) ?? null,
        error: data?.error as string | undefined,
      };
    } catch (error) {
      console.error("[teamsService] Failed to fetch status:", error);
      return null;
    }
  },

  async connect(): Promise<{ oauth_url: string; message: string; redirect_uri?: string }> {
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
    await apiRequest(`${CHANNELS_BASE}/${encodeURIComponent(channelId)}/dismiss`, {
      method: "POST",
      cache: "no-store",
    });
  },

  async restoreChannel(channelId: string, teamId?: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${encodeURIComponent(channelId)}/restore`, {
      method: "POST",
      body: JSON.stringify(teamId ? { team_id: teamId } : {}),
      cache: "no-store",
    });
  },

  async updateChannelDescription(channelId: string, summary: string): Promise<void> {
    await apiRequest(`${CHANNELS_BASE}/${encodeURIComponent(channelId)}/metadata`, {
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
