"use client";

import { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ChevronDown, ChevronRight, ChevronLeft, Loader2 } from "lucide-react";
import { splunkService, SplunkJobStatus } from "@/lib/services/splunk";
import { useToast } from "@/hooks/use-toast";

const TIME_RANGES = [
  { label: "Last 15 minutes", earliest: "-15m", latest: "now" },
  { label: "Last hour", earliest: "-1h", latest: "now" },
  { label: "Last 4 hours", earliest: "-4h", latest: "now" },
  { label: "Last 24 hours", earliest: "-24h", latest: "now" },
  { label: "Last 7 days", earliest: "-7d", latest: "now" },
  { label: "Last 30 days", earliest: "-30d", latest: "now" },
];

const PAGE_SIZES = [20, 50, 100, 200];

function formatTimestamp(timestamp: string | number | undefined): string {
  if (!timestamp) return "-";
  try {
    const date = new Date(typeof timestamp === "number" ? timestamp * 1000 : timestamp);
    return date.toLocaleString("en-US", {
      month: "2-digit",
      day: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch {
    return String(timestamp);
  }
}

function EventRow({ row, index }: { row: Record<string, unknown>; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const rawEvent = row._raw as string | undefined;
  const time = row._time as string | number | undefined;

  // Get display fields (non-internal fields for summary)
  const displayFields = Object.entries(row).filter(
    ([k]) => !k.startsWith("_") && k !== "linecount"
  );

  return (
    <>
      <tr
        className={`border-b hover:bg-muted/50 cursor-pointer ${expanded ? "bg-muted/30" : ""}`}
        onClick={() => setExpanded(!expanded)}
      >
        <td className="p-2 w-8">
          {rawEvent ? (
            expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />
          ) : null}
        </td>
        <td className="p-2 font-mono text-xs text-muted-foreground whitespace-nowrap">
          {formatTimestamp(time)}
        </td>
        <td className="p-2 font-mono text-xs">
          {rawEvent ? (
            <div className="max-w-[800px] truncate">{rawEvent}</div>
          ) : (
            <div className="flex flex-wrap gap-x-4 gap-y-1">
              {displayFields.slice(0, 6).map(([key, value]) => (
                <span key={key}>
                  <span className="text-muted-foreground">{key}=</span>
                  <span>{typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
                </span>
              ))}
              {displayFields.length > 6 && (
                <span className="text-muted-foreground">+{displayFields.length - 6} more</span>
              )}
            </div>
          )}
        </td>
      </tr>
      {expanded && rawEvent && (
        <tr className="bg-muted/20">
          <td colSpan={3} className="p-4">
            <div className="space-y-3">
              <div className="font-mono text-xs whitespace-pre-wrap break-all bg-background p-3 rounded border">
                {rawEvent}
              </div>
              {displayFields.length > 0 && (
                <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-2 text-xs">
                  {displayFields.map(([key, value]) => (
                    <div key={key} className="bg-background p-2 rounded border">
                      <span className="text-muted-foreground">{key}: </span>
                      <span className="font-mono">{typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export default function SplunkSearchPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [timeRange, setTimeRange] = useState("-24h");
  const [searchMode, setSearchMode] = useState<"sync" | "async">("sync");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<Record<string, unknown>[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<SplunkJobStatus | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [hasSearched, setHasSearched] = useState(false);
  const pollingRef = useRef<NodeJS.Timeout | null>(null);

  const totalPages = Math.ceil(totalCount / pageSize);
  const startIndex = (currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalCount);
  const displayedResults = results.slice(startIndex, endIndex);

  useEffect(() => {
    return () => {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
      }
    };
  }, []);

  // Reset to page 1 when results change
  useEffect(() => {
    setCurrentPage(1);
  }, [results]);

  const handleSyncSearch = async () => {
    setLoading(true);
    setError(null);
    setResults([]);
    setTotalCount(0);
    setHasSearched(true);

    try {
      const result = await splunkService.search({
        query,
        earliestTime: timeRange,
        latestTime: "now",
        maxCount: 10000, // Fetch more results for pagination
      });
      setResults(result.results);
      setTotalCount(result.count);
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : "Search failed";
      setError(errorMessage);
      toast({
        title: "Search Failed",
        description: errorMessage,
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const pollJobStatus = async (sid: string) => {
    try {
      const status = await splunkService.getJobStatus(sid);
      setJobStatus(status);

      if (status.isDone) {
        if (pollingRef.current) {
          clearInterval(pollingRef.current);
          pollingRef.current = null;
        }

        if (status.isFailed) {
          setError("Search job failed");
          setLoading(false);
          return;
        }

        const resultsData = await splunkService.getJobResults(sid, { count: 10000 });
        setResults(resultsData.results);
        setTotalCount(resultsData.count);
        setLoading(false);
      }
    } catch (err: unknown) {
      if (pollingRef.current) {
        clearInterval(pollingRef.current);
        pollingRef.current = null;
      }
      const errorMessage = err instanceof Error ? err.message : "Failed to get job status";
      setError(errorMessage);
      setLoading(false);
    }
  };

  const handleAsyncSearch = async () => {
    setLoading(true);
    setError(null);
    setResults([]);
    setTotalCount(0);
    setJobStatus(null);
    setHasSearched(true);

    try {
      const job = await splunkService.createSearchJob({
        query,
        earliestTime: timeRange,
        latestTime: "now",
      });

      if (!job.sid) {
        throw new Error("No job ID returned");
      }

      pollingRef.current = setInterval(() => {
        pollJobStatus(job.sid!);
      }, 1000);

      pollJobStatus(job.sid);
    } catch (err: unknown) {
      const errorMessage = err instanceof Error ? err.message : "Failed to create search job";
      setError(errorMessage);
      toast({
        title: "Search Failed",
        description: errorMessage,
        variant: "destructive",
      });
      setLoading(false);
    }
  };

  const handleSearch = () => {
    if (!query.trim()) {
      toast({
        title: "Query Required",
        description: "Please enter an SPL query",
        variant: "destructive",
      });
      return;
    }

    if (searchMode === "sync") {
      handleSyncSearch();
    } else {
      handleAsyncSearch();
    }
  };

  const handleCancel = async () => {
    if (pollingRef.current) {
      clearInterval(pollingRef.current);
      pollingRef.current = null;
    }

    if (jobStatus?.sid) {
      try {
        await splunkService.cancelJob(jobStatus.sid);
      } catch {
        // Ignore cancel errors
      }
    }

    setLoading(false);
    setJobStatus(null);
  };

  const goToPage = (page: number) => {
    setCurrentPage(Math.max(1, Math.min(page, totalPages)));
  };

  const renderPagination = () => {
    if (totalPages <= 1) return null;

    const pages: (number | string)[] = [];
    const maxVisiblePages = 7;

    if (totalPages <= maxVisiblePages) {
      for (let i = 1; i <= totalPages; i++) pages.push(i);
    } else {
      pages.push(1);
      if (currentPage > 3) pages.push("...");

      const start = Math.max(2, currentPage - 1);
      const end = Math.min(totalPages - 1, currentPage + 1);

      for (let i = start; i <= end; i++) pages.push(i);

      if (currentPage < totalPages - 2) pages.push("...");
      pages.push(totalPages);
    }

    return (
      <div className="flex items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          onClick={() => goToPage(currentPage - 1)}
          disabled={currentPage === 1}
        >
          <ChevronLeft className="h-4 w-4" />
          Prev
        </Button>

        <div className="flex items-center gap-1">
          {pages.map((page, idx) => (
            page === "..." ? (
              <span key={`ellipsis-${idx}`} className="px-2 text-muted-foreground">...</span>
            ) : (
              <Button
                key={page}
                variant={currentPage === page ? "default" : "outline"}
                size="sm"
                onClick={() => goToPage(page as number)}
                className="min-w-[36px]"
              >
                {page}
              </Button>
            )
          ))}
        </div>

        <Button
          variant="outline"
          size="sm"
          onClick={() => goToPage(currentPage + 1)}
          disabled={currentPage === totalPages}
        >
          Next
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>
    );
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-7xl">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-3xl font-bold">Splunk Search</h1>
          <p className="text-muted-foreground mt-1">Run SPL queries against your Splunk instance</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/splunk/alerts")}>
            Alerts
          </Button>
          <Button variant="outline" onClick={() => router.push("/splunk/auth")}>
            Settings
          </Button>
        </div>
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Search Query</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <Label htmlFor="query">SPL Query</Label>
            <Textarea
              id="query"
              placeholder="index=main | head 100"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="font-mono min-h-[100px] mt-1"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Enter your Splunk Processing Language (SPL) query
            </p>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <div>
              <Label>Time Range</Label>
              <Select value={timeRange} onValueChange={setTimeRange}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIME_RANGES.map((range) => (
                    <SelectItem key={range.earliest} value={range.earliest}>
                      {range.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div>
              <Label>Search Mode</Label>
              <Select value={searchMode} onValueChange={(v) => setSearchMode(v as "sync" | "async")}>
                <SelectTrigger className="mt-1">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="sync">Sync (Immediate)</SelectItem>
                  <SelectItem value="async">Async (Background Job)</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground mt-1">
                {searchMode === "sync" ? "Results stream back immediately" : "Creates a background job for long queries"}
              </p>
            </div>

            <div className="flex items-end">
              {loading ? (
                <Button variant="destructive" onClick={handleCancel} className="w-full">
                  Cancel
                </Button>
              ) : (
                <Button onClick={handleSearch} className="w-full" disabled={!query.trim()}>
                  Run Search
                </Button>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {loading && (
        <Card className="mb-6">
          <CardContent className="pt-6">
            <div className="flex items-center gap-3">
              <Loader2 className="h-5 w-5 animate-spin" />
              <div>
                <p className="font-medium">
                  {searchMode === "async" && jobStatus
                    ? `Search ${Math.round((jobStatus.doneProgress || 0) * 100)}% complete`
                    : "Running search..."}
                </p>
                {jobStatus && (
                  <p className="text-sm text-muted-foreground">
                    State: {jobStatus.dispatchState} | Events: {jobStatus.eventCount || 0}
                  </p>
                )}
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {error && (
        <Card className="mb-6 border-destructive">
          <CardContent className="pt-6">
            <p className="text-destructive">{error}</p>
          </CardContent>
        </Card>
      )}

      {!loading && results.length > 0 && (
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between flex-wrap gap-4">
              <div className="flex items-center gap-4">
                <CardTitle>Results</CardTitle>
                <span className="text-sm text-muted-foreground">
                  {totalCount.toLocaleString()} events
                  {totalCount > pageSize && ` (showing ${startIndex + 1}-${endIndex})`}
                </span>
              </div>
              <div className="flex items-center gap-4">
                <div className="flex items-center gap-2">
                  <Label className="text-sm whitespace-nowrap">Per page:</Label>
                  <Select value={String(pageSize)} onValueChange={(v) => { setPageSize(Number(v)); setCurrentPage(1); }}>
                    <SelectTrigger className="w-20">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PAGE_SIZES.map((size) => (
                        <SelectItem key={size} value={String(size)}>{size}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <div className="overflow-auto">
              <table className="w-full text-sm">
                <thead className="sticky top-0 bg-background border-b">
                  <tr>
                    <th className="p-2 w-8"></th>
                    <th className="text-left p-2 font-medium whitespace-nowrap">Time</th>
                    <th className="text-left p-2 font-medium">Event</th>
                  </tr>
                </thead>
                <tbody>
                  {displayedResults.map((row, idx) => (
                    <EventRow key={startIndex + idx} row={row} index={startIndex + idx} />
                  ))}
                </tbody>
              </table>
            </div>

            {totalPages > 1 && (
              <div className="flex justify-center mt-6 pt-4 border-t">
                {renderPagination()}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {!loading && !error && results.length === 0 && hasSearched && (
        <Card>
          <CardContent className="pt-6 text-center py-12">
            <p className="text-muted-foreground">No results found</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
