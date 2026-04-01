"use client";

import React, { useState, useEffect, useRef, useMemo } from "react";
import { ChevronDown, Zap, TrendingUp, TrendingDown, Minus } from "lucide-react";
import { SessionUsageState, RequestUsage } from "@/hooks/useSessionUsage";
import AnimatedNumber from "@/components/AnimatedNumber";

function formatCost(cost: number): string {
  if (cost < 0.01) return `$${cost.toFixed(4)}`;
  return `$${cost.toFixed(2)}`;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

type RateTrend = "up" | "down" | "flat";

function useTokenRate(outputTokens: number, isStreaming: boolean): { tokPerSec: number; trend: RateTrend } {
  const samplesRef = useRef<{ time: number; tokens: number }[]>([]);
  const prevRef = useRef(0);

  return useMemo(() => {
    if (!isStreaming || outputTokens === 0) {
      samplesRef.current = [];
      prevRef.current = 0;
      return { tokPerSec: 0, trend: "flat" as RateTrend };
    }
    const now = Date.now();
    const samples = samplesRef.current;
    samples.push({ time: now, tokens: outputTokens });
    while (samples.length > 1 && samples[0].time < now - 3000) samples.shift();
    if (samples.length < 2) return { tokPerSec: 0, trend: "flat" as RateTrend };

    const dt = (samples[samples.length - 1].time - samples[0].time) / 1000;
    const dT = samples[samples.length - 1].tokens - samples[0].tokens;
    const tokPerSec = dt > 0 ? dT / dt : 0;
    const prev = prevRef.current;
    prevRef.current = tokPerSec;
    const trend: RateTrend = tokPerSec > prev + 3 ? "up" : tokPerSec < prev - 3 ? "down" : "flat";
    return { tokPerSec, trend };
  }, [outputTokens, isStreaming]);
}

function RequestRow({ request }: { request: RequestUsage }) {
  const modelShort = request.model.includes("/")
    ? request.model.split("/")[1]
    : request.model;

  return (
    <div className="flex items-center justify-between text-xs py-1.5 border-b border-zinc-800/60 last:border-0">
      <div className="flex items-center gap-2 min-w-0">
        <span className="text-zinc-500 font-mono text-[11px] shrink-0 w-[72px]">
          {formatTime(request.timestamp)}
        </span>
        <span className="text-zinc-400 truncate" title={request.model}>
          {modelShort}
        </span>
      </div>
      <div className="flex items-center gap-3 shrink-0 ml-2 font-mono tabular-nums">
        <span className="text-zinc-500">
          {formatTokens(request.input_tokens)}<span className="text-zinc-600">/</span>{formatTokens(request.output_tokens)}
        </span>
        <span className="text-zinc-400 w-16 text-right">{formatCost(request.estimated_cost)}</span>
        <span className="text-zinc-600 w-12 text-right">
          {request.response_time_ms >= 1000
            ? `${(request.response_time_ms / 1000).toFixed(1)}s`
            : `${request.response_time_ms}ms`}
        </span>
      </div>
    </div>
  );
}

interface SessionUsagePanelProps {
  sessionUsage: SessionUsageState;
}

export default function SessionUsagePanel({ sessionUsage }: SessionUsagePanelProps) {
  const [isExpanded, setIsExpanded] = useState(false);
  const { currentStreaming, sessionUsage: totals, requestHistory } = sessionUsage;

  const { tokPerSec, trend } = useTokenRate(
    currentStreaming?.output_tokens ?? 0,
    !!currentStreaming
  );

  const wasStreamingRef = useRef(false);
  useEffect(() => {
    if (currentStreaming && !wasStreamingRef.current) wasStreamingRef.current = true;
    else if (!currentStreaming) wasStreamingRef.current = false;
  }, [currentStreaming]);

  if (totals.request_count === 0 && !currentStreaming) {
    return (
      <div className="flex items-center gap-2 px-2 py-2 text-sm text-zinc-500">
        <Zap className="h-3.5 w-3.5 text-zinc-600" />
        <span>Waiting for LLM activity...</span>
      </div>
    );
  }

  const TrendIcon = trend === "up" ? TrendingUp : trend === "down" ? TrendingDown : Minus;
  const trendColor = trend === "up" ? "text-emerald-400" : trend === "down" ? "text-amber-400" : "text-zinc-500";

  return (
    <div className="text-sm">
      <button
        onClick={() => setIsExpanded(!isExpanded)}
        className="w-full flex items-center justify-between px-2 py-2 hover:bg-zinc-800/50 rounded transition-colors"
      >
        {/* Left: streaming indicator or idle */}
        <div className="flex items-center gap-2 text-zinc-400">
          {currentStreaming ? (
            <>
              <Zap className="h-3.5 w-3.5 text-yellow-400 animate-pulse" />
              <AnimatedNumber
                value={currentStreaming.output_tokens}
                format={formatTokens}
                className="text-yellow-300 text-sm"
              />
              <span className="text-yellow-300/50 text-xs">chunks</span>
              {tokPerSec > 0 && (
                <span className={`inline-flex items-center gap-0.5 text-xs ${trendColor}`}>
                  <TrendIcon className="h-3 w-3" />
                  {Math.round(tokPerSec)}c/s
                </span>
              )}
            </>
          ) : (
            <Zap className="h-3.5 w-3.5 text-zinc-600" />
          )}
        </div>

        {/* Right: session totals */}
        <div className="flex items-center gap-3 font-mono tabular-nums text-zinc-400">
          <AnimatedNumber
            value={totals.total_input_tokens + totals.total_output_tokens}
            format={(n) => `${formatTokens(n)} tok`}
            className="text-zinc-400 text-sm"
          />
          <AnimatedNumber
            value={totals.total_cost}
            format={formatCost}
            className="text-zinc-300 text-sm"
          />
          <span className="text-zinc-500 text-xs">{totals.request_count} req</span>
          <ChevronDown
            className={`h-3.5 w-3.5 text-zinc-500 transition-transform duration-200 ${isExpanded ? "rotate-180" : ""}`}
          />
        </div>
      </button>

      {/* Expandable: just the request history */}
      <div className="collapsible-panel" data-open={isExpanded}>
        <div>
          <div className="px-2 pb-2 pt-1">
            {requestHistory.length > 0 ? (
              <div className="max-h-48 overflow-y-auto">
                {requestHistory.map((r, i) => (
                  <RequestRow key={i} request={r} />
                ))}
              </div>
            ) : (
              <div className="text-zinc-500 text-xs py-1">No requests yet</div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
