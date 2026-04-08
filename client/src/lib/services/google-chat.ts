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
};
