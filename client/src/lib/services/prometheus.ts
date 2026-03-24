import { apiRequest, apiDelete } from '@/lib/services/api-client';

export interface PrometheusStatus {
  connected: boolean;
  version?: string;
  alertmanagerConnected?: boolean;
  error?: string;
}

const API_BASE = '/api/prometheus';

export const prometheusService = {
  async getStatus(): Promise<PrometheusStatus | null> {
    try {
      return await apiRequest<PrometheusStatus>(`${API_BASE}/status`, { cache: 'no-store' });
    } catch (err) {
      console.error('[prometheusService] Failed to fetch status:', err);
      return null;
    }
  },

  async connect(
    prometheusUrl: string,
    alertmanagerUrl?: string,
    bearerToken?: string,
    username?: string,
    password?: string,
  ): Promise<PrometheusStatus> {
    const raw = await apiRequest<{
      success: boolean;
      connected: boolean;
      error?: string;
      version?: string;
      alertmanagerConnected?: boolean;
    }>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify({
        prometheusUrl,
        alertmanagerUrl: alertmanagerUrl || undefined,
        bearerToken: bearerToken || undefined,
        username: username || undefined,
        password: password || undefined,
      }),
      cache: 'no-store',
    });
    if (!raw.success && !raw.connected) {
      throw new Error(raw.error || 'Connection failed');
    }
    return {
      connected: true,
      version: raw.version,
      alertmanagerConnected: raw.alertmanagerConnected,
    };
  },

  async disconnect(): Promise<void> {
    await apiDelete('/api/connected-accounts/prometheus');
  },
};
