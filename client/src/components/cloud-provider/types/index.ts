// Shared types for provider components (server-side safe)

export interface Provider {
  id: string;
  name: string;
  icon: string;
  isConnected: boolean;
  isRefreshing?: boolean;
  status?: 'connected' | 'disconnected' | 'unavailable';
  projects?: Project[];
  projectsLoaded?: boolean;
  email?: string;
  description?: string;
  badgeText?: string;
  baseUrl?: string;
  stackSlug?: string;
  metadata?: Record<string, unknown>;
  setupStatus?: {
    message: string;
    progress: number;
    step: number;
    totalSteps: number;
    propagation?: {
      current: number;
      total: number;
    };
  };
}

export interface Project {
  projectId: string;
  name: string;
  enabled: boolean;
  hasPermission?: boolean;
  isRootProject?: boolean;
}

export interface ProviderTokens {
  [providerId: string]: any;
}

export interface ProviderPreferences {
  preference: string | string[] | null;
  available: string[];
  source: string;
}
