import { apiGet, apiPost, apiPut, apiDelete } from "./api-client";

export interface CommandPolicyRule {
  id: number;
  mode: "allow" | "deny";
  pattern: string;
  description: string;
  priority: number;
  enabled: boolean;
  created_at?: string;
  updated_at?: string;
  updated_by?: string;
}

export interface PoliciesResponse {
  allow_rules: CommandPolicyRule[];
  deny_rules: CommandPolicyRule[];
  allowlist_enabled: boolean;
  denylist_enabled: boolean;
}

export interface TestResult {
  allowed: boolean;
  rule_description: string | null;
  command: string;
}

export const commandPolicyService = {
  getPolicies: () => apiGet<PoliciesResponse>("/api/org/command-policies"),

  createPolicy: (rule: Pick<CommandPolicyRule, "mode" | "pattern" | "description" | "priority">) =>
    apiPost<{ id: number }>("/api/org/command-policies", rule),

  updatePolicy: (id: number, fields: Partial<CommandPolicyRule>) =>
    apiPut<{ ok: boolean }>(`/api/org/command-policies/${id}`, fields),

  deletePolicy: (id: number) =>
    apiDelete<{ ok: boolean }>(`/api/org/command-policies/${id}`),

  testCommand: (command: string) =>
    apiPost<TestResult>("/api/org/command-policies/test", { command }),

  toggleList: (list: "allowlist" | "denylist", enabled: boolean) =>
    apiPut<{ ok: boolean; allowlist_enabled: boolean; denylist_enabled: boolean }>(
      "/api/org/command-policy-toggle", { list, enabled }
    ),
};
