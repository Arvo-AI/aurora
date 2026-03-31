"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ExternalLink } from "lucide-react";

interface GrafanaConnectionStepProps {
  baseUrl: string;
  setBaseUrl: (url: string) => void;
  apiToken: string;
  setApiToken: (token: string) => void;
  stackSlug: string;
  setStackSlug: (slug: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function GrafanaConnectionStep({
  baseUrl,
  setBaseUrl,
  apiToken,
  setApiToken,
  stackSlug,
  setStackSlug,
  loading,
  onConnect,
}: GrafanaConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Connect to Grafana</CardTitle>
        <CardDescription>
          Enter your Grafana instance URL and a service account token to connect.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={onConnect} className="space-y-4">
          <div className="space-y-2">
            <Label htmlFor="grafana-base-url">Grafana Base URL</Label>
            <Input
              id="grafana-base-url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://your-instance.grafana.net"
              required
              disabled={loading}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="grafana-stack-slug">Stack Slug (Optional)</Label>
            <Input
              id="grafana-stack-slug"
              value={stackSlug}
              onChange={(e) => setStackSlug(e.target.value)}
              placeholder="my-stack"
              disabled={loading}
            />
          </div>

          <div className="space-y-2">
            <Label htmlFor="grafana-token">Service Account Token</Label>
            <Input
              id="grafana-token"
              type="password"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="glsa_xxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              required
              disabled={loading}
            />
          </div>

          <div className="bg-muted/50 rounded-lg p-4 text-sm">
            <p className="font-medium mb-2">How to create a service account token:</p>
            <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
              <li>Log in to Grafana as an <strong className="text-foreground">Admin</strong> user (required to create service accounts)</li>
              <li>Go to Administration &gt; Service accounts</li>
              <li>Create a new service account with the <strong className="text-foreground">Viewer</strong> role — Aurora only needs read access</li>
              <li>Generate a token for the service account and paste it above</li>
            </ol>
            <p className="text-xs text-muted-foreground mt-3">
              The <strong className="text-foreground">Viewer</strong> role is sufficient. Aurora uses the token only to verify connectivity — all alert data arrives via webhook (Grafana pushes to Aurora).
            </p>
            <a
              href="https://grafana.com/docs/grafana/latest/administration/service-accounts/"
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2 text-xs"
            >
              Grafana service accounts docs <ExternalLink className="h-3 w-3" />
            </a>
          </div>

          <Button type="submit" className="w-full" disabled={loading || !apiToken || !baseUrl}>
            {loading ? "Connecting..." : "Connect Grafana"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
