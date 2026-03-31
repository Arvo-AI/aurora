"use client";

import { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { ChevronDown, ChevronRight, ChevronLeft, Loader2 } from "lucide-react";
import { elasticsearchService, ElasticsearchIndex } from "@/lib/services/elasticsearch";
import { useToast } from "@/hooks/use-toast";

const TIME_RANGES = [
  { label: "Last 15 minutes", earliest: "now-15m", latest: "now" },
  { label: "Last hour", earliest: "now-1h", latest: "now" },
  { label: "Last 4 hours", earliest: "now-4h", latest: "now" },
  { label: "Last 24 hours", earliest: "now-24h", latest: "now" },
  { label: "Last 7 days", earliest: "now-7d", latest: "now" },
  { label: "Last 30 days", earliest: "now-30d", latest: "now" },
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

function DocumentRow({ row, index }: { row: Record<string, unknown>; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const message = (row.message || row.log || row.msg) as string | undefined;
  const time = row["@timestamp"] as string | number | undefined;

  const displayFields = Object.entries(row).filter(
    ([k]) => k !== "@timestamp" && k !== "message" && k !== "log" && k !== "msg"
  );

  return (
    <>
      <tr
        className={`border-b hover:bg-muted/50 cursor-pointer ${expanded ? "bg-muted/30" : ""}`}
        onClick={() => setExpanded(!expanded)}
      >
        <td className="p-2 w-8">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </td>
        <td className="p-2 font-mono text-xs text-muted-foreground whitespace-nowrap">
          {formatTimestamp(time)}
        </td>
        <td className="p-2 font-mono text-xs">
          {message ? (
            <div className="max-w-[800px] truncate">{message}</div>
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
      {expanded && (
        <tr className="bg-muted/20">
          <td colSpan={3} className="p-4">
            <div className="space-y-3">
              {message && (
                <div className="font-mono text-xs whitespace-pre-wrap break-all bg-background p-3 rounded border">
                  {message}
                </div>
              )}
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

export default function ElasticsearchSearchPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [query, setQuery] = useState("");
  const [indexPattern, setIndexPattern] = useState("*");
  const [timeRange, setTimeRange] = useState("now-24h");
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<Record<string, unknown>[]>([]);
  const [totalCount, setTotalCount] = useState(0);
  const [took, setTook] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [hasSearched, setHasSearched] = useState(false);
  const [indices, setIndices] = useState<ElasticsearchIndex[]>([]);

  const totalPages = Math.ceil(totalCount / pageSize);
  const startIndex = (currentPage - 1) * pageSize;
  const endIndex = Math.min(startIndex + pageSize, totalCount);
  const displayedResults = results.slice(startIndex, endIndex);

  useEffect(() => {
    elasticsearchService.getIndices().then(setIndices);
  }, []);

  useEffect(() => {
    setCurrentPage(1);
  }, [results]);

  const handleSearch = async () => {
    if (!query.trim() && !indexPattern) {
      toast({
        title: "Query Required",
        description: "Please enter a query or select an index",
        variant: "destructive",
      });
      return;
    }

    setLoading(true);
    setError(null);
    setResults([]);
    setTotalCount(0);
    setHasSearched(true);

    const selectedTime = TIME_RANGES.find((t) => t.earliest === timeRange);

    try {
      const result = await elasticsearchService.search({
        index: indexPattern || "*",
        queryString: query || "*",
        earliestTime: selectedTime?.earliest,
        latestTime: selectedTime?.latest,
        size: 10000,
      });
      setResults(result.results);
      setTotalCount(result.total);
      setTook(result.took);
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
          <h1 className="text-3xl font-bold">Elasticsearch Search</h1>
          <p className="text-muted-foreground mt-1">Query logs and data from your Elasticsearch cluster</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/elasticsearch/alerts")}>
            Alerts
          </Button>
          <Button variant="outline" onClick={() => router.push("/elasticsearch/auth")}>
            Settings
          </Button>
        </div>
      </div>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Search Query</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div>
              <Label htmlFor="index">Index Pattern</Label>
              <Input
                id="index"
                placeholder="*"
                value={indexPattern}
                onChange={(e) => setIndexPattern(e.target.value)}
                className="mt-1"
                list="index-suggestions"
              />
              <datalist id="index-suggestions">
                {indices.map((idx) => (
                  <option key={idx.index} value={idx.index} />
                ))}
              </datalist>
              <p className="text-xs text-muted-foreground mt-1">
                e.g., logs-*, filebeat-*, my-index
              </p>
            </div>
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
          </div>

          <div>
            <Label htmlFor="query">Query String</Label>
            <Textarea
              id="query"
              placeholder='status:error OR message:"connection refused"'
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="font-mono min-h-[100px] mt-1"
            />
            <p className="text-xs text-muted-foreground mt-1">
              Uses Elasticsearch query string syntax (Lucene)
            </p>
          </div>

          <div className="flex items-end">
            <Button onClick={handleSearch} disabled={loading} className="w-full md:w-auto">
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Searching...
                </>
              ) : (
                "Run Search"
              )}
            </Button>
          </div>
        </CardContent>
      </Card>

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
                  {totalCount.toLocaleString()} hits in {took}ms
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
                    <th className="text-left p-2 font-medium whitespace-nowrap">Timestamp</th>
                    <th className="text-left p-2 font-medium">Document</th>
                  </tr>
                </thead>
                <tbody>
                  {displayedResults.map((row, idx) => (
                    <DocumentRow key={startIndex + idx} row={row} index={startIndex + idx} />
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
