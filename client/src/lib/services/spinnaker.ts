const API_BASE = '/api/spinnaker';

async function fetchJson<T>(input: RequestInfo, init?: RequestInit): Promise<T> {
  const response = await fetch(input, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    cache: 'no-store',
  });

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as { error?: string } | null;
    throw new Error(body?.error || response.statusText || `Request failed (${response.status})`);
  }

  return response.json();
}

export interface SpinnakerStatus {
  connected: boolean;
  baseUrl?: string;
  authType?: string;
  applications?: number;
  cloudAccounts?: string[];
  error?: string;
}

export interface SpinnakerDeploymentEvent {
  id: number;
  application: string;
  pipelineName: string;
  executionId: string;
  status: string;
  triggerType: string;
  triggerUser: string;
  receivedAt: string | null;
}

export interface SpinnakerWebhookInfo {
  webhookUrl: string;
  echoConfig: string;
  instructions: string[];
}

export interface SpinnakerRcaSettings {
  rcaEnabled: boolean;
}

export const spinnakerService = {
  async getStatus(): Promise<SpinnakerStatus | null> {
    try {
      return await fetchJson<SpinnakerStatus>(`${API_BASE}/status`);
    } catch {
      return null;
    }
  },

  async connect(payload: Record<string, string>): Promise<SpinnakerStatus> {
    return fetchJson<SpinnakerStatus>(`${API_BASE}/connect`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
  },

  async getWebhookUrl(): Promise<SpinnakerWebhookInfo | null> {
    try {
      return await fetchJson<SpinnakerWebhookInfo>(`${API_BASE}/webhook-url`);
    } catch {
      return null;
    }
  },

  async getDeployments(limit = 10): Promise<{ deployments: SpinnakerDeploymentEvent[]; total: number } | null> {
    try {
      return await fetchJson<{ deployments: SpinnakerDeploymentEvent[]; total: number }>(
        `${API_BASE}/deployments?limit=${limit}`
      );
    } catch {
      return null;
    }
  },

  async getRcaSettings(): Promise<SpinnakerRcaSettings | null> {
    try {
      return await fetchJson<SpinnakerRcaSettings>(`${API_BASE}/rca-settings`);
    } catch {
      return null;
    }
  },

  async updateRcaSettings(settings: SpinnakerRcaSettings): Promise<SpinnakerRcaSettings | null> {
    try {
      return await fetchJson<SpinnakerRcaSettings>(`${API_BASE}/rca-settings`, {
        method: 'PUT',
        body: JSON.stringify(settings),
      });
    } catch {
      return null;
    }
  },
};
