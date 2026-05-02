// AI SDK 5 UIMessage parts shape — what `chat_messages.parts[]` carries.
//
// Used by useChatStream to maintain a live in-memory mirror of parts[] per
// message_id. The reducer is O(1) for streaming text/reasoning extension —
// it appends to the trailing streaming part rather than rewriting the array.

export type TextPart = {
  type: 'text';
  text: string;
  state?: 'streaming' | 'done';
};

export type ReasoningPart = {
  type: 'reasoning';
  text: string;
  state?: 'streaming' | 'done';
};

export type ToolPartState =
  | 'input-streaming'
  | 'input-available'
  | 'output-available'
  | 'output-error'
  | 'awaiting-confirmation'
  | 'setting-up-environment';

export type ToolPart = {
  // AI SDK 5 convention: `tool-<TOOLNAME>`
  type: `tool-${string}`;
  toolCallId: string;
  state: ToolPartState;
  input?: unknown;
  output?: unknown;
  errorText?: string;
  confirmationId?: string;
  confirmationMessage?: string;
};

export type DataSubAgentPart = {
  type: 'data-subagent';
  id?: string;
  data: {
    agent_id: string;
    status?: 'dispatched' | 'running' | 'finished' | 'failed';
    purpose?: string;
    ui_label?: string;
    model?: string;
    summary?: string;
    tools_used?: string[];
    [k: string]: unknown;
  };
};

export type DataPlanPart = {
  type: 'data-plan';
  id?: string;
  data: {
    selected?: unknown;
    rationale?: string;
    memory_hints_used?: unknown;
    [k: string]: unknown;
  };
};

export type MessagePart =
  | TextPart
  | ReasoningPart
  | ToolPart
  | DataSubAgentPart
  | DataPlanPart;

// SSE chat_event envelope.
export interface ChatStreamEvent {
  seq: number;
  session_id: string;
  message_id: string;
  agent_id?: string;
  parent_agent_id?: string | null;
  type: string;
  payload: Record<string, unknown>;
}

// Find the trailing streaming part of a given type, if any.
function findTrailingStreaming(
  parts: MessagePart[],
  type: 'text' | 'reasoning',
): number {
  if (parts.length === 0) return -1;
  const last = parts[parts.length - 1];
  if (last.type !== type) return -1;
  const streamable = last as TextPart | ReasoningPart;
  if (streamable.state === 'streaming' || streamable.state === undefined) {
    return parts.length - 1;
  }
  return -1;
}

// `Array.prototype.with(idx, value)` returns a new array with one slot replaced —
// single allocation, no head/tail reconstruction. Trailing streaming-text
// extension is the hot path during token streaming, so saving the slice/spread
// here matters at production scale.
function appendToText(parts: MessagePart[], delta: string): MessagePart[] {
  if (!delta) return parts;
  const idx = findTrailingStreaming(parts, 'text');
  if (idx >= 0) {
    const cur = parts[idx] as TextPart;
    return parts.with(idx, { ...cur, text: cur.text + delta, state: 'streaming' });
  }
  return [...parts, { type: 'text', text: delta, state: 'streaming' }];
}

// finalText carries the LLM's first AIMessage text. In tool-using flows the
// trailing streaming text comes from a later iteration, so only upgrade to
// finalText when no tool part precedes it (otherwise it'd clobber post-tool
// content with the pre-tool intro and surface as a duplicate segment).
function finalizeStreamingText(parts: MessagePart[], finalText?: string): MessagePart[] {
  const idx = findTrailingStreaming(parts, 'text');
  if (idx < 0) {
    if (finalText) {
      return [...parts, { type: 'text', text: finalText, state: 'done' }];
    }
    return parts;
  }
  const hasToolBefore = parts.slice(0, idx).some((p) => p.type.startsWith('tool-'));
  const cur = parts[idx] as TextPart;
  const canUpgrade = !hasToolBefore && finalText && finalText.length > cur.text.length;
  const text = canUpgrade ? (finalText as string) : cur.text;
  return parts.with(idx, { ...cur, text, state: 'done' });
}

function upsertToolPart(
  parts: MessagePart[],
  toolCallId: string,
  toolName: string,
  patch: Partial<ToolPart>,
): MessagePart[] {
  const idx = parts.findIndex(
    (p) => p.type.startsWith('tool-') && (p as ToolPart).toolCallId === toolCallId,
  );
  if (idx < 0) {
    const created: ToolPart = {
      type: `tool-${toolName}`,
      toolCallId,
      state: patch.state ?? 'input-streaming',
      ...patch,
    };
    return [...parts, created];
  }
  const cur = parts[idx] as ToolPart;
  return parts.with(idx, { ...cur, ...patch });
}

function upsertSubAgent(parts: MessagePart[], data: DataSubAgentPart['data']): MessagePart[] {
  const idx = parts.findIndex(
    (p) => p.type === 'data-subagent' && (p as DataSubAgentPart).data.agent_id === data.agent_id,
  );
  if (idx < 0) {
    return [...parts, { type: 'data-subagent', data }];
  }
  const cur = parts[idx] as DataSubAgentPart;
  return parts.with(idx, { ...cur, data: { ...cur.data, ...data } });
}

// Reducer: (currentParts, sseEvent) -> updated parts[].
// Forward-compatible: unknown event types pass through as a no-op.
export function reduceParts(parts: MessagePart[], evt: ChatStreamEvent): MessagePart[] {
  const p = evt.payload || {};
  switch (evt.type) {
    case 'assistant_started':
      return parts;

    case 'assistant_chunk': {
      const delta = (p.delta as string) ?? (p.text as string) ?? '';
      return appendToText(parts, delta);
    }

    case 'assistant_finalized':
    case 'assistant_interrupted':
    case 'assistant_failed':
      return finalizeStreamingText(parts, typeof p.text === 'string' ? p.text : undefined);

    case 'tool_call_started': {
      const toolCallId = (p.tool_call_id as string) || (p.id as string) || '';
      const toolName = (p.tool_name as string) || 'unknown';
      if (!toolCallId) return parts;
      return upsertToolPart(parts, toolCallId, toolName, {
        type: `tool-${toolName}`,
        state: 'input-streaming',
        input: p.input,
      });
    }

    case 'tool_call_chunk': {
      const toolCallId = (p.tool_call_id as string) || '';
      const toolName = (p.tool_name as string) || 'unknown';
      if (!toolCallId) return parts;
      // input-available once the input payload is fully assembled
      const state: ToolPartState = p.input_complete ? 'input-available' : 'input-streaming';
      return upsertToolPart(parts, toolCallId, toolName, {
        state,
        input: p.input ?? undefined,
      });
    }

    case 'tool_call_result': {
      const toolCallId = (p.tool_call_id as string) || '';
      const toolName = (p.tool_name as string) || 'unknown';
      if (!toolCallId) return parts;
      const isError = Boolean(p.error) || p.status === 'error';
      return upsertToolPart(parts, toolCallId, toolName, {
        state: isError ? 'output-error' : 'output-available',
        output: p.output ?? p.result,
        errorText: isError ? String(p.error ?? p.error_text ?? 'Tool error') : undefined,
      });
    }

    case 'plan_committed': {
      const newPlan = {
        type: 'data-plan' as const,
        data: {
          selected: p.selected,
          rationale: p.rationale as string | undefined,
          memory_hints_used: p.memory_hints_used,
        },
      };
      // Upsert so event replay doesn't accumulate duplicate plan parts.
      const existingIdx = parts.findIndex((part) => part.type === 'data-plan');
      if (existingIdx === -1) return [...parts, newPlan];
      const next = parts.slice();
      next[existingIdx] = newPlan;
      return next;
    }

    case 'subagent_dispatched':
    case 'subagent_finished':
    case 'subagent_failed': {
      const agent_id = (p.agent_id as string) || '';
      if (!agent_id) return parts;
      const status: DataSubAgentPart['data']['status'] =
        evt.type === 'subagent_dispatched'
          ? 'dispatched'
          : evt.type === 'subagent_finished'
            ? 'finished'
            : 'failed';
      return upsertSubAgent(parts, {
        agent_id,
        status,
        purpose: p.purpose as string | undefined,
        ui_label: p.ui_label as string | undefined,
        model: p.model as string | undefined,
        summary: p.summary as string | undefined,
        tools_used: p.tools_used as string[] | undefined,
      });
    }

    case 'user_message':
      // user_message events seed a user-row's parts; consumers normally
      // create the row at send time, so this is a no-op for the live mirror.
      return parts;

    case 'tool_status': {
      // Only `setting_up_environment` has UI today; web_search progress
      // statuses are recorded for durability but render no-op.
      if ((p.status as string) !== 'setting_up_environment') return parts;
      // Mirrors the WS handler walk-back: the spinner attaches to the most
      // recent in-flight tool call.
      for (let i = parts.length - 1; i >= 0; i--) {
        const pp = parts[i];
        if (!pp.type.startsWith('tool-')) continue;
        const tool = pp as ToolPart;
        if (tool.state !== 'input-streaming' && tool.state !== 'input-available') {
          continue;
        }
        return parts.with(i, { ...tool, state: 'setting-up-environment' });
      }
      return parts;
    }

    case 'execution_confirmation': {
      // Match by tool_call_id; fall back to the last tool part with the same name.
      const explicitId = (p.tool_call_id as string) || '';
      const toolName = (p.tool_name as string) || '';
      const confirmationId = (p.confirmation_id as string) || '';
      const confirmationMessage =
        typeof p.message === 'string' ? (p.message as string) : undefined;
      if (!confirmationId) return parts;

      let idx = -1;
      if (explicitId) {
        idx = parts.findIndex(
          (pp) =>
            pp.type.startsWith('tool-') &&
            (pp as ToolPart).toolCallId === explicitId,
        );
      }
      if (idx < 0 && toolName) {
        const expectedType = `tool-${toolName}`;
        for (let i = parts.length - 1; i >= 0; i--) {
          const pp = parts[i];
          if (pp.type === expectedType) {
            idx = i;
            break;
          }
        }
      }
      if (idx < 0) return parts;
      const cur = parts[idx] as ToolPart;
      return parts.with(idx, {
        ...cur,
        state: 'awaiting-confirmation',
        confirmationId,
        confirmationMessage,
      });
    }

    default:
      return parts;
  }
}
