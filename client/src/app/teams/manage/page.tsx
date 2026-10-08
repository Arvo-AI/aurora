"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { TeamsMemoryCard } from "@/components/TeamsMemoryCard";
import { teamsService } from "@/lib/services/teams";

export default function TeamsManagePage() {
  const [connected, setConnected] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const canWrite = true;

  const loadStatus = useCallback(async () => {
    try {
      const status = await teamsService.getStatus();
      setConnected(Boolean(status.connected));
    } catch {
      setConnected(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  const onRefresh = async () => {
    setRefreshing(true);
    try {
      await teamsService.refreshChannels();
    } finally {
      setRefreshing(false);
    }
  };

  if (!connected) {
    return (
      <div className="p-8 max-w-2xl">
        <h1 className="text-2xl font-semibold mb-2">Microsoft Teams</h1>
        <p className="text-muted-foreground mb-4">Connect Microsoft Teams from the Connectors page first.</p>
      </div>
    );
  }

  return (
    <div className="p-8 max-w-3xl space-y-6">
      <div className="flex items-center justify-between gap-4">
        <h1 className="text-2xl font-semibold">Microsoft Teams</h1>
        <Button variant="outline" onClick={onRefresh} disabled={refreshing}>
          {refreshing ? "Refreshing…" : "Refresh channels"}
        </Button>
      </div>
      <TeamsMemoryCard canWrite={canWrite} />
    </div>
  );
}
