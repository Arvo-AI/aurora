import { apiGet, apiPut, apiPost } from "./api-client";

export interface ToolPermission {
  tool_key: string;
  connector: string;
  label: string;
  tier: string;
  enabled: boolean;
}

export interface ToolPermissionsResponse {
  tools_by_connector: Record<string, ToolPermission[]>;
  seeded: boolean;
}

export const toolPermissionService = {
  getPermissions: () => apiGet<ToolPermissionsResponse>("/api/org/tool-permissions"),
  toggleTool: (toolKey: string, enabled: boolean) =>
    apiPut<{ tool_key: string; enabled: boolean }>(`/api/org/tool-permissions/${toolKey}`, { enabled }),
  seedDefaults: () => apiPost<{ seeded: number }>("/api/org/tool-permissions/seed"),
};
