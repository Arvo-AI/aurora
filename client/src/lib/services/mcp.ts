'use client';

import { apiRequest } from '@/lib/services/api-client';

export type McpToolMode = 'auto' | 'always' | 'never';

export interface McpToolSummary {
  name: string;
  write: boolean;
  /** True when the server itself annotated the tool, rather than us guessing. */
  declared: boolean;
  mode: McpToolMode;
}

export interface McpServerSummary {
  label: string;
  url: string;
  transport: 'streamable_http' | 'sse';
  authType: 'bearer' | 'header' | 'oauth' | 'none';
  readOnly: boolean;
  toolCount: number;
  tools: McpToolSummary[];
  allowInBackground: string[];
  validatedAt?: string;
}

export interface McpServerList {
  servers: McpServerSummary[];
  maxServers: number;
  maxToolsPerServer: number;
}

export interface McpRegisterPayload {
  label: string;
  url: string;
  /** Omitted means "detect it" -- the backend tries streamable HTTP then SSE. */
  transport?: 'streamable_http' | 'sse';
  authType: 'bearer' | 'header' | 'oauth' | 'none';
  token?: string;
  headerName?: string;
  readOnly: boolean;
  clientId?: string;
  clientSecret?: string;
}

/** What a URL needs to connect, probed before the user is asked anything. */
export interface McpDetectResult {
  authType: 'none' | 'oauth' | 'token';
  transport?: 'streamable_http' | 'sse';
  supportsDcr?: boolean;
}

const API_BASE = '/api/mcp/servers';
const CONNECTED_FLAG = 'isMcpConnected';

// Registration probes the remote server, so it can legitimately take a while.
// Retries are off: a POST that already stored a server must not be replayed.
const REGISTER_OPTIONS = { timeout: 60_000, retries: 0 } as const;

function setConnectedFlag(connected: boolean): void {
  if (globalThis.window === undefined) return;
  if (connected) localStorage.setItem(CONNECTED_FLAG, 'true');
  else localStorage.removeItem(CONNECTED_FLAG);
  window.dispatchEvent(new Event('mcpStateChanged'));
}

export const mcpService = {
  async listServers(): Promise<McpServerList> {
    const data = await apiRequest<McpServerList>(API_BASE, { cache: 'no-store' });
    const servers = data?.servers ?? [];
    setConnectedFlag(servers.length > 0);
    return {
      servers,
      maxServers: data?.maxServers ?? 10,
      maxToolsPerServer: data?.maxToolsPerServer ?? 25,
    };
  },

  async register(payload: McpRegisterPayload): Promise<{ server: McpServerSummary; warning?: string }> {
    const data = await apiRequest<{ server: McpServerSummary; warning?: string }>(API_BASE, {
      ...REGISTER_OPTIONS,
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
    setConnectedFlag(true);
    return { server: data.server, warning: data.warning };
  },

  async refresh(label: string): Promise<{ server: McpServerSummary; warning?: string }> {
    const data = await apiRequest<{ server: McpServerSummary; warning?: string }>(
      `${API_BASE}/${encodeURIComponent(label)}/refresh`,
      { ...REGISTER_OPTIONS, method: 'POST', cache: 'no-store' },
    );
    return { server: data.server, warning: data.warning };
  },

  /** Probe a URL to learn whether it needs OAuth, a token, or nothing. */
  async detect(url: string): Promise<McpDetectResult> {
    return apiRequest<McpDetectResult>(`${API_BASE}/detect`, {
      ...REGISTER_OPTIONS,
      method: 'POST',
      body: JSON.stringify({ url }),
      cache: 'no-store',
    });
  },

  async setToolMode(label: string, toolName: string, mode: McpToolMode): Promise<void> {
    await apiRequest(
      `${API_BASE}/${encodeURIComponent(label)}/tools/${encodeURIComponent(toolName)}`,
      { method: 'PATCH', body: JSON.stringify({ mode }), retries: 0, cache: 'no-store' },
    );
  },

  async remove(label: string): Promise<void> {
    const data = await apiRequest<{ remaining: number }>(
      `${API_BASE}/${encodeURIComponent(label)}`,
      { method: 'DELETE', retries: 0, cache: 'no-store' },
    );
    setConnectedFlag((data?.remaining ?? 0) > 0);
  },

  /**
   * Begin the OAuth flow. Returns the provider's consent URL plus the state
   * that `completeOAuth` must echo back.
   */
  async startOAuth(payload: McpRegisterPayload): Promise<{ authorizeUrl: string; state: string }> {
    return apiRequest<{ authorizeUrl: string; state: string }>(`${API_BASE}/oauth/start`, {
      ...REGISTER_OPTIONS,
      method: 'POST',
      body: JSON.stringify(payload),
      cache: 'no-store',
    });
  },

  async completeOAuth(code: string, state: string): Promise<{ server: McpServerSummary; warning?: string }> {
    const data = await apiRequest<{ server: McpServerSummary; warning?: string }>(
      `${API_BASE}/oauth/complete`,
      {
        ...REGISTER_OPTIONS,
        method: 'POST',
        body: JSON.stringify({ code, state }),
        cache: 'no-store',
      },
    );
    setConnectedFlag(true);
    return { server: data.server, warning: data.warning };
  },
};
