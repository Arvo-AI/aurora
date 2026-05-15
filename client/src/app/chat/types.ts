// Multi-agent dispatch tool name. Centralized to avoid magic-string drift.
export const DISPATCH_SUBAGENT_TOOL_NAME = "dispatch_subagent";

// Message type for deploy page chat
export interface PolicyChange {
  action: 'disable_deny_rule' | 'add_allow_rule';
  rule_id?: number | null;
  pattern?: string | null;
  description?: string | null;
  editable: boolean;
}

export interface YesAlwaysEffect {
  summary: string;
  changes: PolicyChange[];
}

export interface ToolCall {
  id: string;
  tool_name: string;
  input: string;
  output?: any;
  error?: string | null;
  status: "pending" | "running" | "completed" | "error" | "awaiting_confirmation" | "cancelled";
  timestamp: string;
  confirmation_id?: string;
  confirmation_message?: string;
  // Set when the confirmation originates from the command gate. Drives the
  // Yes-Always button visibility and the policy-change disclosure.
  block_layer?: string;
  yes_always_effect?: YesAlwaysEffect;
  command?: string; // Add command field to store final_command
  isExpanded?: boolean; // Track whether the tool output is expanded
  // Multi-agent dispatch fields (populated when tool_name === DISPATCH_SUBAGENT_TOOL_NAME)
  agent_id?: string;
  role_name?: string;
  purpose?: string;
  child_session_id?: string;
  wave?: number;
  self_assessed_strength?: "strong" | "moderate" | "weak" | "inconclusive";
}

export type MessageContentPart =
  | { type: 'text'; text: string }
  | { type: 'tool_call'; toolCall: ToolCall };

export type Message = {
  id: number;
  text: string;
  sender: "user" | "bot";
  isStreaming?: boolean;
  isThinking?: boolean;
  severity?: "info" | "error" | "success";
  isDeploymentStatus?: boolean;
  isCompleted?: boolean;
  isOptionsMessage?: boolean;
  options?: Array<{ text: string; value: any }>;
  optionCallback?: (value: any) => void;
  // Images for multimodal messages
  images?: Array<{
    data: string; // base64-only data for backend
    displayData?: string; // full data URL for frontend display
    name?: string; // original filename if available
    type?: string; // MIME type
  }>;
  // Tool calls array for agentic messages
  toolCalls?: ToolCall[];
  // New content array for chronological rendering
  content?: MessageContentPart[];
};

export interface ParsedDispatchCall {
  agent_id: string;
  role_name: string;
  purpose: string;
  child_session_id: string;
  wave: number;
  self_assessed_strength?: "strong" | "moderate" | "weak" | "inconclusive";
}

const ALLOWED_STRENGTHS = new Set<ParsedDispatchCall["self_assessed_strength"]>([
  "strong",
  "moderate",
  "weak",
  "inconclusive",
]);

function coerceWave(raw: unknown): number {
  const n = typeof raw === "number" ? raw : Number(raw);
  if (Number.isFinite(n) && Number.isInteger(n) && n >= 1) return n;
  return 1;
}

function coerceStrength(
  raw: unknown,
): ParsedDispatchCall["self_assessed_strength"] {
  return typeof raw === "string" && ALLOWED_STRENGTHS.has(raw as ParsedDispatchCall["self_assessed_strength"])
    ? (raw as ParsedDispatchCall["self_assessed_strength"])
    : undefined;
}

export function parseDispatchToolCall(tc: ToolCall): ParsedDispatchCall | null {
  if (tc.tool_name !== DISPATCH_SUBAGENT_TOOL_NAME) return null;
  try {
    const parsed = typeof tc.input === "string" ? JSON.parse(tc.input) : tc.input;
    if (!parsed?.agent_id || !parsed?.role_name || !parsed?.purpose) return null;
    const outputStrength =
      tc.output && typeof tc.output === "object"
        ? coerceStrength((tc.output as { self_assessed_strength?: unknown }).self_assessed_strength)
        : undefined;
    return {
      agent_id: String(parsed.agent_id),
      role_name: String(parsed.role_name),
      purpose: String(parsed.purpose),
      child_session_id: String(parsed.child_session_id ?? ""),
      wave: coerceWave(parsed.wave),
      self_assessed_strength: outputStrength ?? coerceStrength(parsed.self_assessed_strength),
    };
  } catch {
    return null;
  }
}
