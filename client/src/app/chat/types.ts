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