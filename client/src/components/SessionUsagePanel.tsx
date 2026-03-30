"use client";

import React, { useState } from "react";
import { ChevronDown, ChevronUp, BarChart3, Zap, DollarSign, Clock } from "lucide-react";
import { SessionUsageState, RequestUsage } from "@/hooks/useSessionUsage";

interface SessionUsagePanelProps {
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

function RequestRow({ request, index }: { request: RequestUsage; index: number }) {
  const modelShort = request.model.includes("/")
    ? request.model.split("/")[1]
    : request.model;

  return (
    <div className="flex items-center justify-between text-xs py-1.5 border-b border-gray-700/50 last:border-0">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-gray-500 font-mono w-4 text-right shrink-0">{index + 1}</span>
        <span className="text-gray-300 truncate" title={request.model}>
          {modelShort}
        </span>
      </div>
      <div className="flex items-center gap-3 shrink-0 ml-2">
        <span className="font-mono tabular-nums text-gray-400">
          {formatTokens(request.input_tokens)}/{formatTokens(request.output_tokens)}
        </span>
        <span className="font-mono tabular-nums text-gray-300 w-16 text-right">
          {formatCost(request.estimated_cost)}
        </span>
        {request.response_time_ms > 0 && (
          <span className="font-mono tabular-nums text-gray-500 w-12 text-right">
            {request.response_time_ms >= 1000
              ? `${(request.response_time_ms / 1000).toFixed(1)}s`
              : `${request.response_time_ms}ms`}
          </span>
        )}
      </div>
    </div>
  );
}

export default function SessionUsagePanel({ sessionUsage }: SessionUsagePanelProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const { sessionUsage: totals, requestHistory } = sessionUsage;

  if (totals.request_count === 0) return null;

  const modelBreakdown = requestHistory.reduce<
    Record<string, { count: number; input: number; output: number; cost: number }>
  >((acc, r) => {
    const key = r.model;
    if (!acc[key]) acc[key] = { count: 0, input: 0, output: 0, cost: 0 };
    acc[key].count += 1;
    acc[key].input += r.input_tokens;
    acc[key].output += r.output_tokens;
    acc[key].cost += r.estimated_cost;
    return acc;
  }, {});

  return (
    <div className="rounded-lg border border-gray-700 bg-gray-800/60 text-sm overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-gray-700/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <BarChart3 className="h-3.5 w-3.5 text-gray-400" />
          <span className="text-gray-300 font-medium text-xs">Session Usage</span>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs text-gray-400">
            <Zap className="h-3 w-3" />
            <span className="font-mono tabular-nums">
              {formatTokens(totals.total_input_tokens + totals.total_output_tokens)}
            </span>
          </div>
          <div className="flex items-center gap-1 text-xs text-gray-400">
            <DollarSign className="h-3 w-3" />
            <span className="font-mono tabular-nums">{formatCost(totals.total_cost)}</span>
          </div>
          {isExpanded ? (
            <ChevronUp className="h-3.5 w-3.5 text-gray-500" />
          ) : (
            <ChevronDown className="h-3.5 w-3.5 text-gray-500" />
          )}
        </div>
      </button>

      {isExpanded && (
        <div className="border-t border-gray-700 px-3 py-2 space-y-3">
          {/* Summary stats */}
          <div className="grid grid-cols-4 gap-2 text-xs">
            <div>
              <div className="text-gray-500">Input</div>
              <div className="font-mono tabular-nums text-gray-300">
                {formatTokens(totals.total_input_tokens)}
              </div>
            </div>
            <div>
              <div className="text-gray-500">Output</div>
              <div className="font-mono tabular-nums text-gray-300">
                {formatTokens(totals.total_output_tokens)}
              </div>
            </div>
            <div>
              <div className="text-gray-500">Cost</div>
              <div className="font-mono tabular-nums text-gray-300">
                {formatCost(totals.total_cost)}
              </div>
            </div>
            <div>
              <div className="text-gray-500">Requests</div>
              <div className="font-mono tabular-nums text-gray-300">
                {totals.request_count}
              </div>
            </div>
          </div>

          {/* Per-model breakdown */}
          {Object.keys(modelBreakdown).length > 1 && (
            <div>
              <div className="text-xs text-gray-500 mb-1">By model</div>
              {Object.entries(modelBreakdown).map(([model, stats]) => {
                const modelShort = model.includes("/") ? model.split("/")[1] : model;
                return (
                  <div
                    key={model}
                    className="flex items-center justify-between text-xs py-1 border-b border-gray-700/30 last:border-0"
                  >
                    <span className="text-gray-300 truncate" title={model}>
                      {modelShort}
                      <span className="text-gray-500 ml-1">x{stats.count}</span>
                    </span>
                    <div className="flex items-center gap-3 shrink-0 ml-2">
                      <span className="font-mono tabular-nums text-gray-400">
                        {formatTokens(stats.input + stats.output)}
                      </span>
                      <span className="font-mono tabular-nums text-gray-300 w-16 text-right">
                        {formatCost(stats.cost)}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* Request history */}
          <div>
            <div className="flex items-center justify-between text-xs text-gray-500 mb-1">
              <span>Request history</span>
              <span className="font-mono">in/out</span>
            </div>
            <div className="max-h-48 overflow-y-auto">
              {requestHistory.map((r, i) => (
                <RequestRow key={i} request={r} index={i} />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
