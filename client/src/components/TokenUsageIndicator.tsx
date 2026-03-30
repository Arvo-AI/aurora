"use client";

import React, { useEffect, useRef, useMemo } from "react";
import { Zap, Clock, TrendingUp, TrendingDown, Minus } from "lucide-react";
import { SessionUsageState } from "@/hooks/useSessionUsage";
import AnimatedNumber from "@/components/AnimatedNumber";

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

type RateTrend = "up" | "down" | "flat";

/**
 * Tracks token rate as a simple moving average over the last N samples.
 * Returns the current trend direction.
 */
function useTokenRate(outputTokens: number, isStreaming: boolean): { tokPerSec: number; trend: RateTrend } {
  const samplesRef = useRef<{ time: number; tokens: number }[]>([]);
  const prevTokPerSecRef = useRef(0);

  const result = useMemo(() => {
    if (!isStreaming || outputTokens === 0) {
      samplesRef.current = [];
      prevTokPerSecRef.current = 0;
      return { tokPerSec: 0, trend: "flat" as RateTrend };
    }

    const now = Date.now();
    const samples = samplesRef.current;
    samples.push({ time: now, tokens: outputTokens });

    // Keep ~3 seconds of samples for the moving average
    const windowMs = 3000;
    const cutoff = now - windowMs;
    while (samples.length > 1 && samples[0].time < cutoff) {
      samples.shift();
    }

    if (samples.length < 2) {
      return { tokPerSec: 0, trend: "flat" as RateTrend };
    }

    const oldest = samples[0];
    const newest = samples[samples.length - 1];
    const dt = (newest.time - oldest.time) / 1000;
    const dTokens = newest.tokens - oldest.tokens;
    const tokPerSec = dt > 0 ? dTokens / dt : 0;

    const prev = prevTokPerSecRef.current;
    prevTokPerSecRef.current = tokPerSec;

    const threshold = 3;
    let trend: RateTrend = "flat";
    if (tokPerSec > prev + threshold) trend = "up";
    else if (tokPerSec < prev - threshold) trend = "down";

    return { tokPerSec, trend };
  }, [outputTokens, isStreaming]);

  return result;
}

function RateArrow({ trend, tokPerSec }: { trend: RateTrend; tokPerSec: number }) {
  if (tokPerSec === 0) return null;

  const Icon = trend === "up" ? TrendingUp : trend === "down" ? TrendingDown : Minus;
  const color =
    trend === "up"
      ? "text-emerald-400"
      : trend === "down"
        ? "text-amber-400"
        : "text-zinc-500";

  return (
    <span className={`inline-flex items-center gap-0.5 ${color}`}>
      <Icon className="h-3 w-3" />
      <span className="text-[10px] font-mono tabular-nums">
        {Math.round(tokPerSec)}t/s
      </span>
    </span>
  );
}

export default function TokenUsageIndicator({ sessionUsage }: TokenUsageIndicatorProps) {
  const { currentStreaming, sessionUsage: totals, requestHistory } = sessionUsage;
  const lastRequest = requestHistory.length > 0 ? requestHistory[requestHistory.length - 1] : null;

  const { tokPerSec, trend } = useTokenRate(
    currentStreaming?.output_tokens ?? 0,
    !!currentStreaming
  );

  // Reset rate samples when streaming starts
  const wasStreamingRef = useRef(false);
  useEffect(() => {
    if (currentStreaming && !wasStreamingRef.current) {
      wasStreamingRef.current = true;
    } else if (!currentStreaming) {
      wasStreamingRef.current = false;
    }
  }, [currentStreaming]);

  if (totals.request_count === 0 && !currentStreaming) return null;

  return (
    <div className="flex items-center gap-3 text-xs text-zinc-400 px-1 py-1">
      {currentStreaming ? (
        <div className="flex items-center gap-2">
          <Zap className="h-3 w-3 text-yellow-400 animate-pulse" />
          <AnimatedNumber
            value={currentStreaming.output_tokens}
            format={formatTokens}
            className="text-yellow-300 text-xs"
          />
          <span className="text-yellow-300/60">tokens</span>
          <RateArrow trend={trend} tokPerSec={tokPerSec} />
        </div>
      ) : lastRequest ? (
        <div className="flex items-center gap-3">
          <div className="flex items-center gap-1">
            <Zap className="h-3 w-3 text-zinc-500" />
            <AnimatedNumber
              value={lastRequest.input_tokens}
              format={formatTokens}
              className="text-zinc-400 text-xs"
            />
            <span className="text-zinc-600">in</span>
            <span className="text-zinc-600">/</span>
            <AnimatedNumber
              value={lastRequest.output_tokens}
              format={formatTokens}
              className="text-zinc-400 text-xs"
            />
            <span className="text-zinc-600">out</span>
          </div>
          <span className="text-zinc-600">|</span>
          <AnimatedNumber
            value={lastRequest.estimated_cost}
            format={formatCost}
            className="text-zinc-400 text-xs"
          />
          {lastRequest.response_time_ms > 0 && (
            <>
              <span className="text-zinc-600">|</span>
              <div className="flex items-center gap-0.5">
                <Clock className="h-3 w-3 text-zinc-500" />
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
        <div className="flex items-center gap-1 ml-auto text-zinc-500">
          <span>Session:</span>
          <AnimatedNumber
            value={totals.total_input_tokens + totals.total_output_tokens}
            format={formatTokens}
            className="text-zinc-400 text-xs"
          />
          <span className="text-zinc-600">tokens</span>
          <span className="text-zinc-700">|</span>
          <AnimatedNumber
            value={totals.total_cost}
            format={formatCost}
            className="text-zinc-400 text-xs"
          />
        </div>
      )}
    </div>
  );
}
