"use client";

import React from "react";
import { Zap, Clock } from "lucide-react";
import { SessionUsageState } from "@/hooks/useSessionUsage";

interface TokenUsageIndicatorProps {
  sessionUsage: SessionUsageState;
}

function formatCost(cost: number): string {
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}

export default function TokenUsageIndicator({ sessionUsage }: TokenUsageIndicatorProps) {
  const { currentStreaming, sessionUsage: totals, requestHistory } = sessionUsage;
  const lastRequest = requestHistory.length > 0 ? requestHistory[requestHistory.length - 1] : null;

  if (totals.request_count === 0 && !currentStreaming) return null;

  return (
    <div className="flex items-center gap-3 text-xs text-gray-400 px-1 py-1">
      {currentStreaming ? (
        <div className="flex items-center gap-1.5 animate-pulse">
          <Zap className="h-3 w-3 text-yellow-400" />
          <span className="text-yellow-300 font-mono tabular-nums">
            {formatTokens(currentStreaming.output_tokens)} tokens
          </span>
        </div>
      ) : lastRequest ? (
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <Zap className="h-3 w-3 text-gray-500" />
            <span className="font-mono tabular-nums">
              {formatTokens(lastRequest.input_tokens)} in / {formatTokens(lastRequest.output_tokens)} out
            </span>
          </div>
          <span className="text-gray-500">|</span>
          <span className="font-mono tabular-nums">{formatCost(lastRequest.estimated_cost)}</span>
          {lastRequest.response_time_ms > 0 && (
            <>
              <span className="text-gray-500">|</span>
              <div className="flex items-center gap-0.5">
                <Clock className="h-3 w-3 text-gray-500" />
                <span className="font-mono tabular-nums">
                  {lastRequest.response_time_ms >= 1000
                    ? `${(lastRequest.response_time_ms / 1000).toFixed(1)}s`
                    : `${lastRequest.response_time_ms}ms`}
                </span>
              </div>
            </>
          )}
        </div>
      ) : null}

      {totals.request_count > 0 && (
        <div className="flex items-center gap-1 ml-auto text-gray-500">
          <span>Session:</span>
          <span className="font-mono tabular-nums text-gray-400">
            {formatTokens(totals.total_input_tokens + totals.total_output_tokens)} tokens
          </span>
          <span className="text-gray-600">|</span>
          <span className="font-mono tabular-nums text-gray-400">
            {formatCost(totals.total_cost)}
          </span>
        </div>
      )}
    </div>
  );
}
