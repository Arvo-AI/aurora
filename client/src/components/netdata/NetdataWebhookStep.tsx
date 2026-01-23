"use client";

import { useRouter } from "next/navigation";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { NetdataStatus } from "@/lib/services/netdata";

interface NetdataWebhookStepProps {
  status: NetdataStatus;
  webhookUrl: string;
  verificationToken?: string;
  copiedField: "url" | "token" | null;
  onCopyUrl: () => void;
  onCopyToken: () => void;
  onRefresh: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function NetdataWebhookStep({
  status,
  webhookUrl,
  verificationToken,
  copiedField,
  onCopyUrl,
  onCopyToken,
  onRefresh,
  onDisconnect,
  loading,
}: NetdataWebhookStepProps) {
  const router = useRouter();

  return (
    <div className="space-y-6">
      {/* Connection Status Card */}
      <Card>
        <CardHeader>
          <div className="flex items-center justify-between">
            <div>
              <CardTitle>Netdata Connected</CardTitle>
              <CardDescription>Your Netdata Cloud is successfully connected</CardDescription>
            </div>
            <div className="flex gap-2">
              <Button variant="outline" onClick={() => router.push("/netdata/alerts")}>
                View Alerts
              </Button>
              <Button variant="destructive" onClick={onDisconnect} disabled={loading}>
                Disconnect
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-2 text-sm">
          {status.baseUrl && (
            <div className="flex justify-between py-1 border-b">
              <span className="font-medium">URL:</span>
              <span className="text-muted-foreground">{status.baseUrl}</span>
            </div>
          )}
          {status.spaceName && (
            <div className="flex justify-between py-1">
              <span className="font-medium">Space:</span>
              <span className="text-muted-foreground">{status.spaceName}</span>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Webhook Configuration Card */}
      <Card>
        <CardHeader>
          <CardTitle>Configure Alert Webhook</CardTitle>
          <CardDescription>Add Aurora as a webhook destination in Netdata Cloud</CardDescription>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Webhook URL */}
          <div>
            <Label className="text-base font-semibold mb-2 block">Webhook URL</Label>
            <div className="flex items-center gap-2 mt-2">
              <code className="flex-1 px-4 py-3 bg-muted rounded text-sm font-mono break-all">
                {webhookUrl}
              </code>
              <Button variant="outline" onClick={onCopyUrl} className="flex-shrink-0">
                {copiedField === "url" ? "Copied!" : "Copy"}
              </Button>
            </div>
          </div>

          {/* Verification Token */}
          <div>
            <div className="flex items-center justify-between mb-2">
              <Label className="text-base font-semibold">Verification Token</Label>
              <Button variant="ghost" size="sm" onClick={onRefresh} disabled={loading}>
                Refresh
              </Button>
            </div>
            {verificationToken ? (
              <div className="flex items-center gap-2">
                <code className="flex-1 px-4 py-3 bg-muted rounded text-sm font-mono break-all">
                  {verificationToken}
                </code>
                <Button variant="outline" onClick={onCopyToken} className="flex-shrink-0">
                  {copiedField === "token" ? "Copied!" : "Copy"}
                </Button>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground px-4 py-3 bg-muted rounded">
                Click Test in Netdata, then Refresh here to see the token
              </p>
            )}
          </div>

          {/* Setup Instructions */}
          <div className="space-y-3 text-sm">
            <p className="font-semibold">Setup Instructions:</p>
            <div className="space-y-2">
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">1.</span>
                <p>In Netdata: <strong>Space Settings</strong> → <strong>Alerts & Notifications</strong> → <strong>+ Add</strong> → <strong>Webhook</strong></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">2.</span>
                <p>Paste the <strong>Webhook URL</strong> above, set Auth to <strong>None</strong></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">3.</span>
                <p>Select Rooms and notification types, then click <strong>Test</strong></p>
              </div>
              <div className="flex items-start gap-2">
                <span className="text-muted-foreground mt-0.5">4.</span>
                <p>Click <strong>Refresh</strong> above to get the verification token, copy it to Netdata and click <strong>Submit</strong></p>
              </div>
            </div>
          </div>

          <div className="p-3 bg-green-50 dark:bg-green-950/20 border border-green-200 dark:border-green-800 rounded">
            <p className="text-xs text-green-800 dark:text-green-400">
              Aurora will receive alerts from Netdata once the webhook is configured.
            </p>
          </div>

          <div className="p-3 bg-blue-50 dark:bg-blue-950/20 border border-blue-200 dark:border-blue-800 rounded">
            <a
              href="https://learn.netdata.cloud/docs/alerts-&-notifications/notifications/centralized-cloud-notifications/webhook"
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-blue-600 dark:text-blue-400 hover:underline flex items-center gap-1"
            >
              View Netdata Webhook Documentation
              <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
            </a>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
