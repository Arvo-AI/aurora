import { apiRequest } from '@/lib/services/api-client';

export type ServiceNowAuthType = 'basic' | 'oauth_client_credentials';

export interface ServiceNowStatus {
  connected: boolean;
  instanceUrl?: string;
  authType?: ServiceNowAuthType;
  displayName?: string;
  username?: string;
  validatedAt?: string;
  hasWebhookSecret?: boolean;
  error?: string;
}

export interface ServiceNowConnectPayload {
  instanceUrl: string;
  authType: ServiceNowAuthType;
  username?: string;
  password?: string;
  clientId?: string;
  clientSecret?: string;
  displayName?: string;
}

export interface ServiceNowWebhookUrlResponse {
  webhookUrl: string;
  webhookSecret: string;
  headerName: string;
  instructions: string[];
  businessRuleScript: string;
}

export type ServiceNowMinPriority = 1 | 2 | 3 | 4 | 5;

export interface ServiceNowRcaSettings {
  rcaEnabled: boolean;
  minPriority: ServiceNowMinPriority;
  workNotesEnabled: boolean;
}

export const SERVICENOW_PRIORITY_OPTIONS: ReadonlyArray<{ value: ServiceNowMinPriority; label: string }> = [
  { value: 1, label: '1 - Critical' },
  { value: 2, label: '2 - High' },
  { value: 3, label: '3 - Moderate' },
  { value: 4, label: '4 - Low' },
  { value: 5, label: '5 - Planning (all incidents)' },
];

const API_BASE = '/api/servicenow';

export const serviceNowService = {
  async getStatus(): Promise<ServiceNowStatus | null> {
    try {
      const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/status`, {
        cache: 'no-store',
      });
      if (!raw) return null;
      return {
        connected: Boolean(raw.connected),
        instanceUrl: raw.instanceUrl as string | undefined,
        authType: raw.authType as ServiceNowAuthType | undefined,
        displayName: raw.displayName as string | undefined,
        username: raw.username as string | undefined,
        validatedAt: raw.validatedAt as string | undefined,
        hasWebhookSecret: raw.hasWebhookSecret as boolean | undefined,
        error: raw.error as string | undefined,
      };
    } catch (error) {
      console.error('[serviceNowService] Failed to fetch status:', error);
      return null;
    }
  },

  async connect(payload: ServiceNowConnectPayload): Promise<ServiceNowStatus> {
    const raw = await apiRequest<Record<string, unknown>>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
      retries: 0,
    });
    return {
      connected: Boolean(raw?.connected ?? raw?.success),
      instanceUrl: raw?.instanceUrl as string | undefined,
      authType: raw?.authType as ServiceNowAuthType | undefined,
      displayName: raw?.displayName as string | undefined,
      username: raw?.username as string | undefined,
      validatedAt: raw?.validatedAt as string | undefined,
      hasWebhookSecret: raw?.hasWebhookSecret as boolean | undefined,
      error: raw?.error as string | undefined,
    };
  },

  async disconnect(): Promise<void> {
    await apiRequest(`${API_BASE}/disconnect`, {
      method: 'POST',
      cache: 'no-store',
      retries: 0,
    });
  },

  async getWebhookUrl(): Promise<ServiceNowWebhookUrlResponse | null> {
    try {
      return await apiRequest<ServiceNowWebhookUrlResponse>(`${API_BASE}/webhook-url`, {
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[serviceNowService] Failed to fetch webhook URL:', error);
      return null;
    }
  },

  async rotateWebhookSecret(): Promise<string> {
    const raw = await apiRequest<{ success: boolean; webhookSecret: string }>(
      `${API_BASE}/webhook-secret/rotate`,
      { method: 'POST', cache: 'no-store', retries: 0 },
    );
    return raw.webhookSecret;
  },

  async getRcaSettings(): Promise<ServiceNowRcaSettings | null> {
    try {
      return await apiRequest<ServiceNowRcaSettings>(`${API_BASE}/rca-settings`, {
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[serviceNowService] Failed to fetch RCA settings:', error);
      return null;
    }
  },

  async updateRcaSettings(settings: Partial<ServiceNowRcaSettings>): Promise<ServiceNowRcaSettings | null> {
    try {
      return await apiRequest<ServiceNowRcaSettings>(`${API_BASE}/rca-settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
        cache: 'no-store',
      });
    } catch (error) {
      console.error('[serviceNowService] Failed to update RCA settings:', error);
      return null;
    }
  },
};
