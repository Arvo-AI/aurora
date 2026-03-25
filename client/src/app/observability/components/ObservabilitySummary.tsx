"use client";

import React from "react";
import {
  PieChart,
  Pie,
  Cell,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
} from "recharts";
import type { ObservabilitySummary as SummaryData } from "@/lib/services/observability";
import { STATUS_HEX_COLORS, STATUS_DOT_CLASSES } from "./StatusBadge";

interface Props {
  data: SummaryData | undefined;
  isLoading: boolean;
}

const CATEGORY_COLOR = "#6366f1";

const PROVIDER_COLORS: Record<string, string> = {
  gcp: "#4285F4",
  aws: "#FF9900",
  azure: "#0078D4",
  tailscale: "#6366f1",
  ovh: "#123F6D",
  scaleway: "#4F0599",
  kubectl: "#326CE5",
  onprem: "#6b7280",
};

function LoadingSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {[1, 2, 3].map((i) => (
          <div key={i} className="rounded-lg border border-border bg-card p-5 shadow-sm animate-pulse">
            <div className="h-4 w-24 bg-muted rounded mb-3" />
            <div className="h-8 w-16 bg-muted rounded mb-2" />
            <div className="h-32 w-full bg-muted rounded" />
          </div>
        ))}
      </div>
    </div>
  );
}

function CustomTooltipContent({ active, payload, label }: { active?: boolean; payload?: Array<{ value: number; payload: Record<string, unknown> }>; label?: string }) {
  if (!active || !payload?.length) return null;
  const displayName = label || (payload[0].payload.name as string) || "";
  return (
    <div className="rounded-md border border-border bg-[#1a1a1a] px-3 py-2 shadow-lg text-sm">
      <span className="text-neutral-100 font-medium">{displayName}: </span>
      <span className="text-neutral-400">{payload[0].value}</span>
    </div>
  );
}

export default function ObservabilitySummary({ data, isLoading }: Props) {
  if (isLoading) return <LoadingSkeleton />;

  const statusData = Object.entries(data?.by_status || {}).map(([name, value]) => ({
    name,
    value,
    fill: STATUS_HEX_COLORS[name] || STATUS_HEX_COLORS.Unknown,
  }));

  const categoryData = Object.entries(data?.by_category || {})
    .sort(([, a], [, b]) => b - a)
    .map(([name, value]) => ({ name, value }));

  const providerData = Object.entries(data?.by_provider || {})
    .sort(([, a], [, b]) => b - a)
    .map(([name, value]) => ({
      name: name.toUpperCase(),
      value,
      fill: PROVIDER_COLORS[name] || "#6b7280",
    }));

  const totalResources = data?.total_resources ?? 0;
  const runningCount = data?.by_status?.Running ?? 0;
  const healthPercent = totalResources > 0 ? Math.round((runningCount / totalResources) * 100) : 0;

  return (
    <div className="space-y-4">
      {/* Top row: 3 cards */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Health Donut */}
        <div className="rounded-lg border border-border bg-card p-5 shadow-sm">
          <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">Health Overview</h3>
          <div className="flex items-center gap-4">
            <div className="relative w-32 h-32 flex-shrink-0">
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={statusData.length > 0 ? statusData : [{ name: "No data", value: 1, fill: "#374151" }]}
                    cx="50%"
                    cy="50%"
                    innerRadius={36}
                    outerRadius={56}
                    paddingAngle={statusData.length > 1 ? 3 : 0}
                    dataKey="value"
                    stroke="none"
                  >
                    {statusData.map((entry, index) => (
                      <Cell key={index} fill={entry.fill} />
                    ))}
                  </Pie>
                  <Tooltip content={<CustomTooltipContent />} wrapperStyle={{ outline: "none" }} cursor={false} />
                </PieChart>
              </ResponsiveContainer>
              {/* Center text */}
              <div className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none">
                <span className="text-lg font-bold text-foreground">{healthPercent}%</span>
                <span className="text-[10px] text-muted-foreground">healthy</span>
              </div>
            </div>
            <div className="space-y-1.5 flex-1 min-w-0">
              {statusData.map((entry) => (
                <div key={entry.name} className="flex items-center gap-2 text-sm">
                  <span className={`inline-block h-2.5 w-2.5 rounded-full flex-shrink-0 ${STATUS_DOT_CLASSES[entry.name] || "bg-gray-400"}`} />
                  <span className="text-muted-foreground truncate">{entry.name}</span>
                  <span className="ml-auto font-medium text-foreground tabular-nums">{entry.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        {/* Provider Distribution */}
        <div className="rounded-lg border border-border bg-card p-5 shadow-sm">
          <div className="flex items-baseline justify-between mb-3">
            <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider">By Provider</h3>
            <span className="text-2xl font-bold text-foreground">{totalResources}</span>
          </div>
          {providerData.length > 0 ? (
            <ResponsiveContainer width="100%" height={Math.max(80, providerData.length * 32)}>
              <BarChart data={providerData} layout="vertical" margin={{ left: 0, right: 8, top: 0, bottom: 0 }}>
                <XAxis type="number" hide />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={70}
                  axisLine={false}
                  tickLine={false}
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                />
                <Tooltip content={<CustomTooltipContent />} wrapperStyle={{ outline: "none" }} cursor={{ fill: "rgba(255,255,255,0.05)" }} />
                <Bar dataKey="value" radius={[0, 4, 4, 0]} barSize={16}>
                  {providerData.map((entry, index) => (
                    <Cell key={index} fill={entry.fill} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-muted-foreground">No providers connected</p>
          )}
        </div>

        {/* Category Distribution */}
        <div className="rounded-lg border border-border bg-card p-5 shadow-sm">
          <h3 className="text-xs font-medium text-muted-foreground uppercase tracking-wider mb-3">By Category</h3>
          {categoryData.length > 0 ? (
            <ResponsiveContainer width="100%" height={Math.max(80, categoryData.length * 28)}>
              <BarChart data={categoryData} layout="vertical" margin={{ left: 0, right: 8, top: 0, bottom: 0 }}>
                <XAxis type="number" hide />
                <YAxis
                  type="category"
                  dataKey="name"
                  width={80}
                  axisLine={false}
                  tickLine={false}
                  tick={{ fontSize: 11, fill: "#9ca3af" }}
                />
                <Tooltip content={<CustomTooltipContent />} wrapperStyle={{ outline: "none" }} cursor={{ fill: "rgba(255,255,255,0.05)" }} />
                <Bar dataKey="value" fill={CATEGORY_COLOR} radius={[0, 4, 4, 0]} barSize={14} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-muted-foreground">No resources discovered</p>
          )}
        </div>
      </div>
    </div>
  );
}
