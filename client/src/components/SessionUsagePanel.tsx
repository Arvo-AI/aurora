"use client";

import React, { useState } from "react";
import { ChevronDown, BarChart3, Zap, DollarSign, Clock } from "lucide-react";
import { SessionUsageState, RequestUsage } from "@/hooks/useSessionUsage";
import AnimatedNumber from "@/components/AnimatedNumber";

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
    <div className="flex items-center justify-between text-xs py-1.5 border-b border-zinc-700/50 last:border-0">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-zinc-500 font-mono w-4 text-right shrink-0">{index + 1}</span>
        <span className="text-zinc-300 truncate" title={request.model}>
          {modelShort}
        </span>
      </div>
      <div className="flex items-center gap-3 shrink-0 ml-2">
        <span className="font-mono tabular-nums text-zinc-400">
          {formatTokens(request.input_tokens)}/{formatTokens(request.output_tokens)}
        </span>
        <span className="font-mono tabular-nums text-zinc-300 w-16 text-right">
          {formatCost(request.estimated_cost)}
        </span>
        {request.response_time_ms > 0 && (
          <span className="font-mono tabular-nums text-zinc-500 w-12 text-right">
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

  if (totals.request_count === 0) {
    return (
      <div className="rounded-lg border border-zinc-700/50 bg-zinc-800/40 text-sm overflow-hidden">
        <div className="flex items-center gap-2 px-3 py-2">
          <BarChart3 className="h-3.5 w-3.5 text-zinc-600" />
          <span className="text-zinc-500 text-xs">Token usage will appear here once the session starts</span>
        </div>
      </div>
    );
  }

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
    <div className="rounded-lg border border-zinc-700 bg-zinc-800/60 text-sm overflow-hidden">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-3 py-2 hover:bg-zinc-700/40 transition-colors"
      >
        <div className="flex items-center gap-2">
          <BarChart3 className="h-3.5 w-3.5 text-zinc-400" />
          <span className="text-zinc-300 font-medium text-xs">Session Usage</span>
        </div>
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1.5 text-xs text-zinc-400">
            <Zap className="h-3 w-3" />
            <AnimatedNumber
              value={totals.total_input_tokens + totals.total_output_tokens}
              format={formatTokens}
              className="text-zinc-400 text-xs"
            />
          </div>
          <div className="flex items-center gap-1 text-xs text-zinc-400">
            <DollarSign className="h-3 w-3" />
            <AnimatedNumber
              value={totals.total_cost}
              format={formatCost}
              className="text-zinc-400 text-xs"
            />
          </div>
          <ChevronDown
            className={`h-3.5 w-3.5 text-zinc-500 transition-transform duration-250 ${isExpanded ? "rotate-180" : ""}`}
          />
        </div>
      </button>

      <div className="collapsible-panel" data-open={isExpanded}>
        <div>
          <div className="border-t border-zinc-700 px-3 py-2 space-y-3">
            {/* Summary stats */}
            <div className="grid grid-cols-4 gap-2 text-xs">
              <div>
                <div className="text-zinc-500">Input</div>
                <AnimatedNumber
                  value={totals.total_input_tokens}
                  format={formatTokens}
                  className="text-zinc-300 text-xs"
                />
              </div>
              <div>
                <div className="text-zinc-500">Output</div>
                <AnimatedNumber
                  value={totals.total_output_tokens}
                  format={formatTokens}
                  className="text-zinc-300 text-xs"
                />
              </div>
              <div>
                <div className="text-zinc-500">Cost</div>
                <AnimatedNumber
                  value={totals.total_cost}
                  format={formatCost}
                  className="text-zinc-300 text-xs"
                />
              </div>
              <div>
                <div className="text-zinc-500">Requests</div>
                <AnimatedNumber
                  value={totals.request_count}
                  className="text-zinc-300 text-xs"
                />
              </div>
            </div>

            {/* Per-model breakdown */}
            {Object.keys(modelBreakdown).length > 1 && (
              <div>
                <div className="text-xs text-zinc-500 mb-1">By model</div>
                {Object.entries(modelBreakdown).map(([model, stats]) => {
                  const modelShort = model.includes("/") ? model.split("/")[1] : model;
                  return (
                    <div
                      key={model}
                      className="flex items-center justify-between text-xs py-1 border-b border-zinc-700/30 last:border-0"
                    >
                      <span className="text-zinc-300 truncate" title={model}>
                        {modelShort}
                        <span className="text-zinc-500 ml-1">x{stats.count}</span>
                      </span>
                      <div className="flex items-center gap-3 shrink-0 ml-2">
                        <span className="font-mono tabular-nums text-zinc-400">
                          {formatTokens(stats.input + stats.output)}
                        </span>
                        <span className="font-mono tabular-nums text-zinc-300 w-16 text-right">
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
              <div className="flex items-center justify-between text-xs text-zinc-500 mb-1">
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
        </div>
      </div>
    </div>
  );
}
