"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { datadogService } from "@/lib/services/datadog";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { HelpCircle } from "lucide-react";

const toISOString = (minutesAgo: number) => {
  const now = new Date();
  now.setMinutes(now.getMinutes() - minutesAgo);
  return now.toISOString();
};

const toMillis = (minutesAgo: number) => {
  const now = Date.now();
  return now - minutesAgo * 60 * 1000;
};

export default function DatadogOverviewPage() {
  const [logQuery, setLogQuery] = useState("@status:error");
  const [logsLoading, setLogsLoading] = useState(false);
  const [logsResult, setLogsResult] = useState<string>("");
  const [logsError, setLogsError] = useState<string | null>(null);

  const [metricsQuery, setMetricsQuery] = useState("avg:system.cpu.user{*}");
  const [metricsLoading, setMetricsLoading] = useState(false);
  const [metricsResult, setMetricsResult] = useState<string>("");
  const [metricsError, setMetricsError] = useState<string | null>(null);

  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsResult, setEventsResult] = useState<string>("");
  const [eventsError, setEventsError] = useState<string | null>(null);

  const fetchLogs = async () => {
    try {
      setLogsLoading(true);
      setLogsError(null);
      const data = await datadogService.searchLogs({
        query: logQuery,
        from: toISOString(15),
        to: new Date().toISOString(),
        limit: 100,
      });
      setLogsResult(JSON.stringify(data, null, 2));
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to fetch logs';
      setLogsError(message);
      setLogsResult('');
    } finally {
      setLogsLoading(false);
    }
  };

  const fetchMetrics = async () => {
    try {
      setMetricsLoading(true);
      setMetricsError(null);
      const data = await datadogService.queryMetrics({
        query: metricsQuery,
        fromMs: toMillis(30),
        toMs: Date.now(),
      });
      setMetricsResult(JSON.stringify(data, null, 2));
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to query metrics';
      setMetricsError(message);
      setMetricsResult('');
    } finally {
      setMetricsLoading(false);
    }
  };

  const fetchEvents = async () => {
    try {
      setEventsLoading(true);
      setEventsError(null);
      const params = new URLSearchParams({
        start: Math.floor((Date.now() - 3600 * 1000) / 1000).toString(),
        end: Math.floor(Date.now() / 1000).toString(),
      });
      const data = await datadogService.getEvents(params);
      setEventsResult(JSON.stringify(data, null, 2));
    } catch (error: unknown) {
      const message = error instanceof Error ? error.message : 'Failed to fetch events';
      setEventsError(message);
      setEventsResult('');
    } finally {
      setEventsLoading(false);
    }
  };

  return (
    <div className="container mx-auto py-8 px-4 max-w-6xl space-y-6">
      <div>
        <h1 className="text-3xl font-bold">Datadog Observability Explorer</h1>
        <p className="text-muted-foreground mt-1">
          Run ad-hoc queries against your connected Datadog instance without leaving Aurora.
        </p>
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>Logs Search</CardTitle>
            <CardDescription>Query Datadog logs using the standard search syntax</CardDescription>
          </div>
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Log search tips">
                <HelpCircle className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-80 text-sm space-y-2" align="end">
              <p className="font-semibold text-foreground">Example queries</p>
              <ul className="list-disc list-inside space-y-1 text-muted-foreground">
                <li><code className="bg-muted px-1 rounded">@service:frontend @status:error</code></li>
                <li><code className="bg-muted px-1 rounded">{'env:prod source:nginx "502"'}</code></li>
                <li><code className="bg-muted px-1 rounded">{"kube_namespace:aurora status:error"}</code></li>
              </ul>
              <p className="text-muted-foreground">
                Aurora sends a 15 minute window by default. Adjust the query text to narrow services, envs, or message content exactly as you would inside Datadog Logs Explorer.
              </p>
            </PopoverContent>
          </Popover>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="logs-query">Query</Label>
            <Input
              id="logs-query"
              placeholder="@service:frontend @status:error"
              value={logQuery}
              onChange={(event) => setLogQuery(event.target.value)}
            />
          </div>
          <Button onClick={fetchLogs} disabled={logsLoading}>
            {logsLoading ? 'Searching…' : 'Search logs'}
          </Button>
          {logsError && <p className="text-sm text-destructive">{logsError}</p>}
          {logsResult && (
            <Textarea className="font-mono text-xs h-64" readOnly value={logsResult} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>Metrics Query</CardTitle>
            <CardDescription>Issue timeseries queries across your Datadog metrics</CardDescription>
          </div>
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Metrics query tips">
                <HelpCircle className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-80 text-sm space-y-2" align="end">
              <p className="font-semibold text-foreground">Try a few starters</p>
              <ul className="list-disc list-inside space-y-1 text-muted-foreground">
                <li><code className="bg-muted px-1 rounded">{"avg:system.cpu.user{host:my-host}"}</code></li>
                <li><code className="bg-muted px-1 rounded">{"sum:aws.elb.latency.avg{availability-zone:us-east-1a}"}</code></li>
                <li><code className="bg-muted px-1 rounded">{"max:trace.flask.request.duration{service:api} by {resource_name}"}</code></li>
              </ul>
              <p className="text-muted-foreground">
                Queries follow the Datadog Metrics Query Language. Responses mirror the <code className="bg-muted px-1 rounded text-xs">/api/v2/query/timeseries</code> payload, so you can plug the JSON into dashboards or follow-up tooling.
              </p>
            </PopoverContent>
          </Popover>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="metrics-query">Metrics Query</Label>
            <Input
              id="metrics-query"
              placeholder="avg:system.cpu.user{*}"
              value={metricsQuery}
              onChange={(event) => setMetricsQuery(event.target.value)}
            />
          </div>
          <Button onClick={fetchMetrics} disabled={metricsLoading}>
            {metricsLoading ? 'Querying…' : 'Query metrics'}
          </Button>
          {metricsError && <p className="text-sm text-destructive">{metricsError}</p>}
          {metricsResult && (
            <Textarea className="font-mono text-xs h-64" readOnly value={metricsResult} />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div>
            <CardTitle>Events Feed</CardTitle>
            <CardDescription>Inspect events returned from Datadog&apos;s events API</CardDescription>
          </div>
          <Popover>
            <PopoverTrigger asChild>
              <Button variant="ghost" size="icon" aria-label="Events API tips">
                <HelpCircle className="h-4 w-4" />
              </Button>
            </PopoverTrigger>
            <PopoverContent className="w-80 text-sm space-y-2" align="end">
              <p className="text-muted-foreground">
                The Events API is great for release notes, deployment markers, or service announcements. Use query params in the code to scope by <code className="bg-muted px-1 rounded text-xs">sources</code>, <code className="bg-muted px-1 rounded text-xs">tags</code>, or <code className="bg-muted px-1 rounded text-xs">priority</code>.
              </p>
              <p className="text-muted-foreground">
                Aurora requests the past hour by default—tweak the request inputs to widen or narrow the timeframe before calling <strong>Fetch events</strong>.
              </p>
            </PopoverContent>
          </Popover>
        </CardHeader>
        <CardContent className="space-y-4">
          <Button onClick={fetchEvents} disabled={eventsLoading}>
            {eventsLoading ? 'Loading…' : 'Fetch events'}
          </Button>
          {eventsError && <p className="text-sm text-destructive">{eventsError}</p>}
          {eventsResult && (
            <Textarea className="font-mono text-xs h-64" readOnly value={eventsResult} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
