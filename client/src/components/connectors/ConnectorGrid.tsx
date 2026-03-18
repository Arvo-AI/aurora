"use client";

import React from "react";
import ConnectorCard from "./ConnectorCard";
import type { ConnectorConfig } from "./types";

interface ConnectorGridProps {
  connectors: ConnectorConfig[];
  connectedStatus?: Record<string, boolean>;
}

export default function ConnectorGrid({ connectors, connectedStatus }: ConnectorGridProps) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
      {connectors.map((connector) => (
        <ConnectorCard key={connector.id} connector={connector} connectedOverride={connectedStatus?.[connector.id]} />
      ))}
    </div>
  );
}
