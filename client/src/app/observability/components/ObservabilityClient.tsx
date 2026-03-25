"use client";

import React, { useState, useCallback } from "react";
import { useQuery, type Fetcher } from "@/lib/query";
import type { ObservabilitySummary as SummaryData, ResourcesResponse } from "@/lib/services/observability";
import ObservabilitySummary from "./ObservabilitySummary";
import ResourceFilters from "./ResourceFilters";
import ResourceTable from "./ResourceTable";

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

  const handleRefresh = useCallback(async () => {
    await Promise.all([refreshSummary(), refreshResources()]);
  }, [refreshSummary, refreshResources]);

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
          <button
            onClick={handleRefresh}
            className="px-4 py-2 text-sm font-medium rounded-md border border-border bg-card hover:bg-muted transition-colors"
          >
            Sync Now
          </button>
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
