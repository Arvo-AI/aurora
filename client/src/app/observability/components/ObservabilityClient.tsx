"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";
import { RefreshCw } from "lucide-react";
import { useQuery, type Fetcher } from "@/lib/query";
import type { ObservabilitySummary as SummaryData, ResourcesResponse } from "@/lib/services/observability";
import ObservabilitySummary from "./ObservabilitySummary";
import ResourceFilters from "./ResourceFilters";
import ResourceTable from "./ResourceTable";

type SyncState = "idle" | "syncing" | "done" | "error";

const POLL_INTERVAL_MS = 3000;
const MAX_POLL_DURATION_MS = 5 * 60 * 1000; // 5 minutes
const MAX_POLL_ERRORS = 5;

const summaryFetcher: Fetcher<SummaryData> = async (_key, signal) => {
  const res = await fetch("/api/observability/summary", { credentials: "include", signal });
  if (!res.ok) throw new Error(`summary ${res.status}`);
  return res.json();
};

export default function ObservabilityClient() {
  const [provider, setProvider] = useState<string>("");
  const [category, setCategory] = useState<string>("");
  const [search, setSearch] = useState<string>("");
  const [page, setPage] = useState(1);
  const limit = 50;

  const [syncState, setSyncState] = useState<SyncState>("idle");
  const [syncStatus, setSyncStatus] = useState<string>("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  const { data: summary, isLoading: summaryLoading, mutate: refreshSummary } = useQuery<SummaryData>(
    "/api/observability/summary",
    summaryFetcher,
    { staleTime: 30_000, revalidateOnFocus: true }
  );

  const buildResourcesKey = useCallback(() => {
    const params = new URLSearchParams();
    if (provider) params.set("provider", provider);
    if (category) params.set("category", category);
    if (search) params.set("search", search);
    params.set("page", String(page));
    params.set("limit", String(limit));
    return `/api/observability/resources?${params.toString()}`;
  }, [provider, category, search, page, limit]);

  const resourcesFetcher: Fetcher<ResourcesResponse> = useCallback(
    async (key, signal) => {
      const res = await fetch(key, { credentials: "include", signal });
      if (!res.ok) throw new Error(`resources ${res.status}`);
      return res.json();
    },
    []
  );

  const { data: resourcesData, isLoading: resourcesLoading, mutate: refreshResources } =
    useQuery<ResourcesResponse>(buildResourcesKey(), resourcesFetcher, {
      staleTime: 30_000,
      revalidateOnFocus: true,
    });

  const handleSync = useCallback(async () => {
    if (syncState === "syncing") return;

    setSyncState("syncing");
    setSyncStatus("Starting discovery...");

    try {
      const res = await fetch("/api/observability/discover", {
        method: "POST",
        credentials: "include",
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        setSyncState("error");
        setSyncStatus(data.error || "Failed to start discovery");
        setTimeout(() => setSyncState("idle"), 3000);
        return;
      }

      const { task_id, status: taskStatus } = await res.json();

      if (taskStatus === "already_running") {
        setSyncStatus("Discovery already in progress...");
      }

      if (!task_id) {
        setSyncState("error");
        setSyncStatus("No task ID returned");
        setTimeout(() => setSyncState("idle"), 3000);
        return;
      }

      if (pollRef.current) clearInterval(pollRef.current);
      const pollStartTime = Date.now();
      let pollErrors = 0;
      pollRef.current = setInterval(async () => {
        // Timeout: give up after MAX_POLL_DURATION_MS
        if (Date.now() - pollStartTime > MAX_POLL_DURATION_MS) {
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setSyncState("error");
          setSyncStatus("Discovery timed out");
          setTimeout(() => setSyncState("idle"), 3000);
          return;
        }

        try {
          const pollRes = await fetch(
            `/api/observability/discover/status/${encodeURIComponent(task_id)}`,
            { credentials: "include" }
          );
          if (!pollRes.ok) {
            pollErrors++;
            if (pollErrors >= MAX_POLL_ERRORS) {
              if (pollRef.current) clearInterval(pollRef.current);
              pollRef.current = null;
              setSyncState("error");
              setSyncStatus("Failed to check status");
              setTimeout(() => setSyncState("idle"), 3000);
            }
            return;
          }

          pollErrors = 0;
          const pollData = await pollRes.json();
          setSyncStatus(pollData.status || "Discovering resources...");

          if (pollData.complete) {
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;

            if (pollData.error) {
              setSyncState("error");
              setSyncStatus("Discovery failed");
              setTimeout(() => setSyncState("idle"), 3000);
            } else {
              setSyncState("done");
              setSyncStatus("Discovery complete");
              await Promise.all([refreshSummary(), refreshResources()]);
              setTimeout(() => setSyncState("idle"), 2000);
            }
          }
        } catch {
          pollErrors++;
          if (pollErrors >= MAX_POLL_ERRORS) {
            if (pollRef.current) clearInterval(pollRef.current);
            pollRef.current = null;
            setSyncState("error");
            setSyncStatus("Connection lost");
            setTimeout(() => setSyncState("idle"), 3000);
          }
        }
      }, POLL_INTERVAL_MS);
    } catch {
      setSyncState("error");
      setSyncStatus("Network error");
      setTimeout(() => setSyncState("idle"), 3000);
    }
  }, [syncState, refreshSummary, refreshResources]);

  const handleFilterChange = useCallback(
    (newProvider: string, newCategory: string) => {
      setProvider(newProvider);
      setCategory(newCategory);
      setPage(1);
    },
    []
  );

  const handleSearchChange = useCallback((value: string) => {
    setSearch(value);
    setPage(1);
  }, []);

  return (
    <div className="flex-1 overflow-auto">
      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-foreground">Observability</h1>
            <p className="text-sm text-muted-foreground mt-1">
              Monitor your infrastructure resources across all connected providers.
            </p>
          </div>
          <div className="flex items-center gap-3">
            {syncState !== "idle" && (
              <span className="text-xs text-muted-foreground max-w-[200px] truncate">
                {syncStatus}
              </span>
            )}
            <button
              onClick={handleSync}
              disabled={syncState === "syncing"}
              className="flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-md border border-border bg-card hover:bg-muted transition-colors disabled:opacity-60"
            >
              <RefreshCw
                size={14}
                className={syncState === "syncing" ? "animate-spin" : ""}
              />
              {syncState === "syncing" ? "Syncing..." : syncState === "done" ? "Done" : "Sync Now"}
            </button>
          </div>
        </div>

        {/* Summary Cards */}
        <ObservabilitySummary data={summary} isLoading={summaryLoading} />

        {/* Filters */}
        <ResourceFilters
          provider={provider}
          category={category}
          search={search}
          providers={summary?.by_provider ? Object.keys(summary.by_provider) : []}
          categories={summary?.by_category ? Object.keys(summary.by_category) : []}
          onFilterChange={handleFilterChange}
          onSearchChange={handleSearchChange}
        />

        {/* Resource Table */}
        <ResourceTable
          resources={resourcesData?.resources || []}
          isLoading={resourcesLoading}
          page={page}
          totalPages={resourcesData?.total_pages || 1}
          total={resourcesData?.total || 0}
          onPageChange={setPage}
        />
      </div>
    </div>
  );
}
