"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import type { OpsGenieStatus } from "@/lib/services/opsgenie";

interface OpsGenieWebhookStepProps {
  status: OpsGenieStatus;
  webhookUrl: string | null;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => Promise<void>;
  loading: boolean;
  authType?: "opsgenie" | "jsm";
}

export function OpsGenieWebhookStep({ status, webhookUrl, copied, onCopy, onDisconnect, loading, authType }: OpsGenieWebhookStepProps) {
  const isJSM = authType === "jsm" || status.authType === "jsm_basic";
  const providerLabel = isJSM ? "JSM Operations" : "OpsGenie";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 2: Configure {providerLabel} Webhook</CardTitle>
        <CardDescription>Send alert events directly into Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className={`grid ${isJSM ? 'md:grid-cols-2' : 'md:grid-cols-3'} gap-4`}>
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">Account Name</p>
            <p className="text-base font-semibold">{status.accountName || providerLabel}</p>
          </div>
          {!isJSM && (
            <div className="p-4 border rounded-lg">
              <p className="text-xs text-muted-foreground uppercase tracking-wide">Plan</p>
              <p className="text-base font-semibold">{status.plan || 'N/A'}</p>
            </div>
          )}
          <div className="p-4 border rounded-lg">
            <p className="text-xs text-muted-foreground uppercase tracking-wide">
              {isJSM ? 'Site' : 'Region'}
            </p>
            <p className="text-base font-semibold">
              {isJSM
                ? (status.siteUrl || 'Atlassian Cloud')
                : (status.region || 'us').toUpperCase()}
            </p>
          </div>
        </div>

        {webhookUrl && (
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium">Webhook URL</p>
              <Badge variant="outline">Per user</Badge>
            </div>
            <div className="flex flex-col md:flex-row md:items-center gap-3">
              <code className="flex-1 px-3 py-2 rounded bg-muted text-xs break-all border">{webhookUrl}</code>
              <Button variant={copied ? "secondary" : "default"} onClick={onCopy}>
                {copied ? "Copied!" : "Copy URL"}
              </Button>
            </div>
          </div>
        )}

        <div className="space-y-3">
          <p className="text-sm font-medium">Configure {providerLabel} Webhook:</p>
          {isJSM ? (
            <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
              <li>Go to <strong>JSM Operations &rarr; Settings &rarr; Integrations</strong></li>
              <li>Search for <strong>Webhook</strong> and select it</li>
              <li>Click <strong>Add</strong> to create a new outgoing webhook</li>
              <li>Paste the webhook URL above</li>
              <li>Select alert actions: <strong>Create</strong>, <strong>Acknowledge</strong>, <strong>Close</strong>, and any others you want to track</li>
              <li><strong>Save</strong> the integration</li>
            </ol>
          ) : (
            <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
              <li>Go to <strong>OpsGenie &rarr; Settings &rarr; Integrations</strong></li>
              <li>Search for <strong>Webhook</strong> and select it</li>
              <li>Click <strong>Add</strong> to create a new webhook</li>
              <li>Paste the webhook URL above</li>
              <li>Select alert actions: <strong>Create</strong>, <strong>Acknowledge</strong>, <strong>Close</strong>, and any others you want to track</li>
              <li><strong>Save</strong> the integration</li>
            </ol>
          )}
        </div>

        <div className="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <div className="text-xs text-muted-foreground">
            Connected to <strong>{status.accountName || providerLabel}</strong>
          </div>
          <Button variant="outline" onClick={onDisconnect} disabled={loading}>
            {loading ? "Disconnecting\u2026" : `Disconnect ${providerLabel}`}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
