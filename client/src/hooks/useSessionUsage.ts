"use client";

import { useState, useCallback, useRef, useEffect } from "react";

export interface RequestUsage {
  model: string;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost: number;
  response_time_ms: number;
  output_token_details?: Record<string, number>;
}

export interface SessionUsage {
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost: number;
  request_count: number;
}

export interface CurrentStreamingUsage {
  model: string;
  output_tokens: number;
  is_streaming: boolean;
}

export interface SessionUsageState {
  currentStreaming: CurrentStreamingUsage | null;
  sessionUsage: SessionUsage;
  requestHistory: RequestUsage[];
  handleUsageUpdate: (data: Record<string, unknown>) => void;
  handleUsageFinal: (data: Record<string, unknown>) => void;
  reset: () => void;
}

const EMPTY_SESSION: SessionUsage = {
  total_input_tokens: 0,
  total_output_tokens: 0,
  total_cost: 0,
  request_count: 0,
};

export function useSessionUsage(sessionId: string | null): SessionUsageState {
  const [currentStreaming, setCurrentStreaming] = useState<CurrentStreamingUsage | null>(null);
  const [sessionUsage, setSessionUsage] = useState<SessionUsage>(EMPTY_SESSION);
  const [requestHistory, setRequestHistory] = useState<RequestUsage[]>([]);
  const prevSessionRef = useRef<string | null>(null);

  useEffect(() => {
    if (sessionId !== prevSessionRef.current) {
      prevSessionRef.current = sessionId;
      setCurrentStreaming(null);
      setSessionUsage(EMPTY_SESSION);
      setRequestHistory([]);
    }
  }, [sessionId]);

  const handleUsageUpdate = useCallback((data: Record<string, unknown>) => {
    setCurrentStreaming((prev) => ({
      model: (data.model as string) || prev?.model || "",
      output_tokens: (data.output_chunks as number) ?? (data.output_tokens as number) ?? prev?.output_tokens ?? 0,
      is_streaming: true,
    }));
  }, []);

  const handleUsageFinal = useCallback((data: Record<string, unknown>) => {
    setCurrentStreaming(null);

    const request: RequestUsage = {
      model: (data.model as string) || "",
      input_tokens: (data.input_tokens as number) || 0,
      output_tokens: (data.output_tokens as number) || 0,
      total_tokens: (data.total_tokens as number) || 0,
      estimated_cost: (data.estimated_cost as number) || 0,
      response_time_ms: (data.response_time_ms as number) || 0,
      output_token_details: data.output_token_details as Record<string, number> | undefined,
    };
    setRequestHistory(prev => [...prev, request]);

    const totals = data.session_totals as SessionUsage | undefined;
    if (totals) {
      setSessionUsage({
        total_input_tokens: totals.total_input_tokens || 0,
        total_output_tokens: totals.total_output_tokens || 0,
        total_cost: totals.total_cost || 0,
        request_count: totals.request_count || 0,
      });
    }
  }, []);

  const reset = useCallback(() => {
    setCurrentStreaming(null);
    setSessionUsage(EMPTY_SESSION);
    setRequestHistory([]);
  }, []);

  return {
    currentStreaming,
    sessionUsage,
    requestHistory,
    handleUsageUpdate,
    handleUsageFinal,
    reset,
  };
}
