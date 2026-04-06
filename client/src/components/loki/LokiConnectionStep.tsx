"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface LokiConnectionStepProps {
  baseUrl: string;
  setBaseUrl: (url: string) => void;
  authType: "bearer" | "basic" | "none";
  setAuthType: (type: "bearer" | "basic" | "none") => void;
  token: string;
  setToken: (token: string) => void;
  username: string;
  setUsername: (name: string) => void;
  password: string;
  setPassword: (pw: string) => void;
  tenantId: string;
  setTenantId: (id: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function LokiConnectionStep({
  baseUrl,
  setBaseUrl,
  authType,
  setAuthType,
  token,
  setToken,
  username,
  setUsername,
  password,
  setPassword,
  tenantId,
  setTenantId,
  loading,
  onConnect,
}: LokiConnectionStepProps) {
  const isSubmitDisabled =
    loading ||
    !baseUrl.trim() ||
    (authType === "bearer" && !token.trim()) ||
    (authType === "basic" && (!username.trim() || !password.trim()));

  return (
    <Card>
      <CardHeader>
        <CardTitle>Connect Your Loki Instance</CardTitle>
        <CardDescription>
          Configure your Grafana Loki connection for log aggregation and alert webhooks
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-3 text-sm">
          <div className="space-y-2">
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">1.</span>
              <p>Enter your Loki base URL</p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">2.</span>
              <p>Select your authentication method</p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">3.</span>
              <p>Optionally add a tenant ID for multi-tenant deployments</p>
            </div>
          </div>

          <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
            <a
              href="https://grafana.com/docs/loki/latest/reference/loki-http-api/"
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
            >
              View Loki HTTP API Documentation
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
            </a>
          </div>
        </div>

        <form onSubmit={onConnect} className="space-y-4">
          {/* Base URL */}
          <div className="grid gap-2">
            <Label htmlFor="loki-base-url">Loki Base URL *</Label>
            <Input
              id="loki-base-url"
              type="url"
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
              placeholder="https://loki.example.com:3100"
              required
              disabled={loading}
            />
          </div>

          {/* Auth Type Toggle */}
          <div className="grid gap-2">
            <Label>Authentication Method</Label>
            <div className="flex gap-2">
              <Button
                type="button"
                variant={authType === "bearer" ? "default" : "outline"}
                onClick={() => setAuthType("bearer")}
                disabled={loading}
              >
                Bearer Token
              </Button>
              <Button
                type="button"
                variant={authType === "basic" ? "default" : "outline"}
                onClick={() => setAuthType("basic")}
                disabled={loading}
              >
                Basic Auth
              </Button>
              <Button
                type="button"
                variant={authType === "none" ? "default" : "outline"}
                onClick={() => setAuthType("none")}
                disabled={loading}
              >
                No Auth
              </Button>
            </div>
          </div>

          {/* Conditional Auth Fields */}
          {authType === "bearer" && (
            <div className="grid gap-2">
              <Label htmlFor="loki-token">API Token *</Label>
              <textarea
                id="loki-token"
                className="min-h-[80px] rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Paste your Loki API token or Grafana Cloud Access Policy token"
                required
                disabled={loading}
              />
            </div>
          )}

          {authType === "basic" && (
            <>
              <div className="grid gap-2">
                <Label htmlFor="loki-username">Username *</Label>
                <Input
                  id="loki-username"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                  placeholder="Username"
                  required
                  disabled={loading}
                />
              </div>
              <div className="grid gap-2">
                <Label htmlFor="loki-password">Password *</Label>
                <Input
                  id="loki-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="Password"
                  required
                  disabled={loading}
                />
              </div>
            </>
          )}

          {authType === "none" && (
            <div className="p-3 bg-muted rounded text-sm text-muted-foreground">
              No authentication will be used. Ensure your Loki instance is accessible without credentials.
            </div>
          )}

          {/* Tenant ID */}
          <div className="grid gap-2">
            <Label htmlFor="loki-tenant-id">Tenant ID (Optional)</Label>
            <Input
              id="loki-tenant-id"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder="default"
              disabled={loading}
            />
            <p className="text-xs text-muted-foreground">
              For multi-tenant Loki deployments. Sets the X-Scope-OrgID header.
            </p>
          </div>

          <div className="flex items-center justify-end pt-4">
            <Button type="submit" disabled={isSubmitDisabled}>
              {loading ? "Connecting..." : "Connect Loki"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
