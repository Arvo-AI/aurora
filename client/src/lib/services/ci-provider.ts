import { apiRequest } from '@/lib/services/api-client';

export type CIProviderSlug = "jenkins" | "cloudbees";

interface CIServer {
  version?: string;
  mode?: string;
  numExecutors?: number;
}

interface CIJobHealth {
  healthy: number;
  unstable: number;
  failing: number;
  disabled: number;
  other: number;
}

interface CISummary {
  jobCount: number;
  jobHealth: CIJobHealth;
  queueSize: number;
  nodesOnline: number;
  nodesOffline: number;
  totalExecutors: number;
  busyExecutors: number;
}

export interface CIProviderStatus {
  connected: boolean;
  baseUrl?: string;
  username?: string;
  server?: CIServer | null;
  summary?: CISummary | null;
  error?: string;
}

export interface CIConnectPayload {
  baseUrl: string;
  username: string;
  apiToken: string;
}

export interface CIDeploymentEvent {
  id: number;
  service: string;
  environment: string;
  result: string;
  buildNumber: number;
  buildUrl: string;
  commitSha: string;
  branch: string;
  repository: string;
  deployer: string;
  durationMs: number | null;
  jobName: string;
  traceId: string | null;
  receivedAt: string | null;
}

export interface CIWebhookInfo {
  webhookUrl: string;
  jenkinsfileBasic: string;
  jenkinsfileOtel: string;
  jenkinsfileCurl: string;
  instructions: string[];
}

export interface CIRcaSettings {
  rcaEnabled: boolean;
}

export interface CIProviderService {
  getStatus(): Promise<CIProviderStatus | null>;
  connect(payload: CIConnectPayload): Promise<CIProviderStatus>;
  getWebhookUrl(): Promise<CIWebhookInfo | null>;
  getDeployments(limit?: number): Promise<{ deployments: CIDeploymentEvent[]; total: number } | null>;
  getRcaSettings(): Promise<CIRcaSettings | null>;
  updateRcaSettings(settings: CIRcaSettings): Promise<CIRcaSettings | null>;
}

export function createCIProviderService(slug: CIProviderSlug): CIProviderService {
  const apiBase = `/api/${slug}`;

  return {
    async getStatus(): Promise<CIProviderStatus | null> {
      try {
        const raw = await apiRequest<Record<string, unknown>>(`${apiBase}/status?full=true`, { cache: 'no-store' });
        return {
          connected: Boolean(raw?.connected),
          baseUrl: (raw?.baseUrl ?? raw?.base_url) as string | undefined,
          username: raw?.username as string | undefined,
          server: (raw?.server as CIServer) ?? null,
          summary: (raw?.summary as CISummary) ?? null,
          error: raw?.error as string | undefined,
        };
      } catch (error) {
        console.error(`[${slug}Service] Failed to fetch status:`, error);
        return null;
      }
    },

    async connect(payload: CIConnectPayload): Promise<CIProviderStatus> {
      const raw = await apiRequest<Record<string, unknown>>(`${apiBase}/connect`, {
        method: "POST",
        body: JSON.stringify(payload),
        cache: 'no-store',
      });
      return {
        connected: Boolean(raw?.success ?? true),
        baseUrl: (raw?.baseUrl as string) ?? payload.baseUrl,
        username: (raw?.username as string) ?? payload.username,
        server: (raw?.server as CIServer) ?? null,
      };
    },

    async getWebhookUrl(): Promise<CIWebhookInfo | null> {
      try {
        return await apiRequest<CIWebhookInfo>(`${apiBase}/webhook-url`, { cache: 'no-store' });
      } catch (error) {
        console.error(`[${slug}Service] Failed to fetch webhook URL:`, error);
        return null;
      }
    },

    async getDeployments(
      limit = 10
    ): Promise<{ deployments: CIDeploymentEvent[]; total: number } | null> {
      try {
        return await apiRequest<{ deployments: CIDeploymentEvent[]; total: number }>(
          `${apiBase}/deployments?limit=${limit}`,
          { cache: 'no-store' },
        );
      } catch (error) {
        console.error(`[${slug}Service] Failed to fetch deployments:`, error);
        return null;
      }
    },

    async getRcaSettings(): Promise<CIRcaSettings | null> {
      try {
        return await apiRequest<CIRcaSettings>(`${apiBase}/rca-settings`, { cache: 'no-store' });
      } catch (error) {
        console.error(`[${slug}Service] Failed to fetch RCA settings:`, error);
        return null;
      }
    },

    async updateRcaSettings(settings: CIRcaSettings): Promise<CIRcaSettings | null> {
      try {
        return await apiRequest<CIRcaSettings>(`${apiBase}/rca-settings`, {
          method: "PUT",
          body: JSON.stringify(settings),
          cache: 'no-store',
        });
      } catch (error) {
        console.error(`[${slug}Service] Failed to update RCA settings:`, error);
        return null;
      }
    },
  };
}

export const jenkinsService = createCIProviderService("jenkins");
export const cloudbeesService = createCIProviderService("cloudbees");

export interface CIProviderConfig {
  slug: CIProviderSlug;
  displayName: string;
  description: string;
  logoPath: string;
  logoAlt: string;
  accentColor: string;
  accentTextColor: string;
  cacheKey: string;
  localStorageConnectedKey: string;
  urlPlaceholder: string;
  urlHelpText: string;
  usernamePlaceholder: string;
  setupStepTitle: string;
  setupStepNavPath: string;
  setupStepInstructions: React.ReactNode[];
  setupStepNote?: React.ReactNode;
  docsUrl: string;
  docsLabel: string;
  service: CIProviderService;
}
