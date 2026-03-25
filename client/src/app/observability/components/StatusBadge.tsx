"use client";

import React from "react";

interface Props {
  status: string;
  showLabel?: boolean;
}

const STATUS_CONFIG: Record<string, { color: string; label: string }> = {
  Running: { color: "bg-emerald-500", label: "Running" },
  Stopped: { color: "bg-gray-400", label: "Stopped" },
  Error: { color: "bg-red-500", label: "Error" },
  Provisioning: { color: "bg-yellow-500", label: "Provisioning" },
  Unknown: { color: "bg-gray-300 ring-1 ring-gray-400", label: "Unknown" },
};

export const STATUS_HEX_COLORS: Record<string, string> = {
  Running: "#10b981",
  Stopped: "#6b7280",
  Error: "#ef4444",
  Provisioning: "#f59e0b",
  Unknown: "#9ca3af",
};

export const STATUS_DOT_CLASSES: Record<string, string> = {
  Running: "bg-emerald-500",
  Stopped: "bg-gray-500",
  Error: "bg-red-500",
  Provisioning: "bg-yellow-500",
  Unknown: "bg-gray-400",
};

export default function StatusBadge({ status, showLabel = false }: Props) {
  const config = STATUS_CONFIG[status] || STATUS_CONFIG.Unknown;

  return (
    <div className="flex items-center gap-1.5">
      <span className={`inline-block h-2.5 w-2.5 rounded-full ${config.color}`} />
      {showLabel && (
        <span className="text-xs text-muted-foreground">{config.label}</span>
      )}
    </div>
  );
}
