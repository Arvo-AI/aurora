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
  active_template_id: string | null;
}

export interface TestResult {
  allowed: boolean;
  rule_description: string | null;
  command: string;
}

export interface PolicyTemplateRule {
  pattern: string;
  description: string;
  priority: number;
}

export interface PolicyTemplate {
  id: string;
  name: string;
  description: string;
  allow_count: number;
  deny_count: number;
  allow: PolicyTemplateRule[];
  deny: PolicyTemplateRule[];
}

export const commandPolicyService = {
  getPolicies: () => apiGet<PoliciesResponse>("/api/org/command-policies"),

  createPolicy: (rule: Pick<CommandPolicyRule, "mode" | "pattern" | "description" | "priority">) =>
    apiPost<{ id: number }>("/api/org/command-policies", rule),

  updatePolicy: (id: number, fields: Partial<CommandPolicyRule>) =>
    apiPut<{ status: string }>(`/api/org/command-policies/${id}`, fields),

  deletePolicy: (id: number) =>
    apiDelete<{ status: string }>(`/api/org/command-policies/${id}`),

  testCommand: (command: string) =>
    apiPost<TestResult>("/api/org/command-policies/test", { command }),

  toggleList: (list: "allowlist" | "denylist", enabled: boolean) =>
    apiPut<{ status: string; allowlist_enabled: boolean; denylist_enabled: boolean }>(
      "/api/org/command-policy-toggle", { list, enabled }
    ),

  getTemplates: () => apiGet<PolicyTemplate[]>("/api/org/command-policy-templates"),

  applyTemplate: (templateId: string) =>
    apiPost<{ status: string; template_id: string; allowlist_enabled: boolean; denylist_enabled: boolean; active_template_id: string | null }>(
      "/api/org/command-policy-templates/apply", { template_id: templateId }
    ),

  clearActiveTemplate: () =>
    apiDelete<{ status: string; active_template_id: null }>("/api/org/command-policy-templates/active"),
};
