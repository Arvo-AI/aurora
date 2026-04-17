import { apiRequest } from '@/lib/services/api-client';

export interface GoogleChatStatus {
  connected: boolean;
  has_service_account?: boolean;
  connected_by?: string;
  incidents_space_display_name?: string;
  error?: string;
}

export interface GoogleChatConnectResponse {
  oauth_url: string;
  error?: string;
  error_code?: string;
}

export interface BotSpace {
  name: string;
  displayName: string;
  description: string;
}

export interface TeamMapping {
  id: number;
  team_name: string;
  space_name: string;
  space_display_name: string | null;
  description: string | null;
  created_by: string;
  created_at: string | null;
}

const API_BASE = '/api/google-chat';

export const googleChatService = {
  async getStatus(): Promise<GoogleChatStatus | null> {
    try {
      const data = await apiRequest<Record<string, any>>(`${API_BASE}`, {
        cache: 'no-store',
      });
      return {
        connected: Boolean(data?.connected),
        has_service_account: data?.has_service_account,
        connected_by: data?.connected_by,
        incidents_space_display_name: data?.incidents_space_display_name,
        error: data?.error,
      };
    } catch (error) {
      console.error('[googleChatService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(): Promise<GoogleChatConnectResponse> {
    const data = await apiRequest<GoogleChatConnectResponse>(`${API_BASE}`, {
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

  async listBotSpaces(): Promise<BotSpace[]> {
    const data = await apiRequest<{ spaces: BotSpace[] }>(`${API_BASE}/spaces/bot`, {
      cache: 'no-store',
    });
    return data.spaces ?? [];
  },

  async getTeamMappings(): Promise<TeamMapping[]> {
    const data = await apiRequest<{ mappings: TeamMapping[] }>(`${API_BASE}/team-mappings`, {
      cache: 'no-store',
    });
    return data.mappings ?? [];
  },

  async upsertTeamMapping(mapping: {
    team_name: string;
    space_name: string;
    space_display_name?: string;
    description?: string;
  }): Promise<{ id: number; message: string }> {
    return apiRequest(`${API_BASE}/team-mappings`, {
      method: 'POST',
      body: JSON.stringify(mapping),
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
    });
  },

  async deleteTeamMapping(id: number): Promise<void> {
    await apiRequest(`${API_BASE}/team-mappings/${id}`, {
      method: 'DELETE',
      cache: 'no-store',
    });
  },

  async getRoutingInstructions(): Promise<string> {
    const data = await apiRequest<{ routing_instructions: string }>(`${API_BASE}/routing-instructions`, {
      cache: 'no-store',
    });
    return data.routing_instructions ?? '';
  },

  async updateRoutingInstructions(instructions: string): Promise<void> {
    await apiRequest(`${API_BASE}/routing-instructions`, {
      method: 'PUT',
      body: JSON.stringify({ routing_instructions: instructions }),
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
    });
  },
};
