"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface NetdataConnectionStepProps {
  apiToken: string;
  setApiToken: (token: string) => void;
  spaceName: string;
  setSpaceName: (name: string) => void;
  loading: boolean;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

export function NetdataConnectionStep({
  apiToken,
  setApiToken,
  spaceName,
  setSpaceName,
  loading,
  onConnect,
}: NetdataConnectionStepProps) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Connect Your Netdata Cloud</CardTitle>
        <CardDescription>Create an API token in Netdata Cloud and connect it to Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-3 text-sm">
          <p className="text-muted-foreground">
            To connect Netdata, you need to generate an API token from your Netdata Cloud account.
          </p>
          
          <div className="space-y-2">
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">1.</span>
              <p>Sign in to <a href="https://app.netdata.cloud" target="_blank" rel="noopener noreferrer" className="text-blue-600 hover:underline">Netdata Cloud</a></p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">2.</span>
              <p>Click your profile picture → <strong>User Settings</strong></p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">3.</span>
              <p>Navigate to <strong>API Tokens</strong> section</p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">4.</span>
              <p>Click <strong>+</strong> to create a new token with <code className="px-1.5 py-0.5 bg-muted rounded text-xs">scope:all</code></p>
            </div>
            <div className="flex items-start gap-2">
              <span className="text-muted-foreground mt-0.5">5.</span>
              <p>Copy the token and paste it below</p>
            </div>
          </div>

          <div className="mt-4 p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
            <a
              href="https://learn.netdata.cloud/docs/netdata-cloud/authentication-&-authorization/api-tokens"
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
            >
              View Netdata API Tokens Documentation
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
            </a>
          </div>
        </div>

        <form onSubmit={onConnect} className="space-y-4">
          <div className="grid gap-2">
            <Label htmlFor="netdata-token">API Token *</Label>
            <textarea
              id="netdata-token"
              className="min-h-[80px] rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
              value={apiToken}
              onChange={(e) => setApiToken(e.target.value)}
              placeholder="Paste your Netdata API token here"
              required
              disabled={loading}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="netdata-space">Space Name (Optional)</Label>
            <Input
              id="netdata-space"
              value={spaceName}
              onChange={(e) => setSpaceName(e.target.value)}
              placeholder="My Space"
              disabled={loading}
            />
            <p className="text-xs text-muted-foreground">
              A friendly name to identify your Netdata space
            </p>
          </div>

          <div className="flex items-center justify-end pt-4">
            <Button type="submit" disabled={loading || !apiToken.trim()}>
              {loading ? "Connecting..." : "Connect Netdata"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
