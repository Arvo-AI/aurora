"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Copy, Check, ExternalLink, Unplug } from "lucide-react";
import type { NewRelicStatus } from "@/lib/services/newrelic";

interface NewRelicWebhookStepProps {
  status: NewRelicStatus;
  webhookUrl: string;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function NewRelicWebhookStep({
  status,
  webhookUrl,
  copied,
  onCopy,
  onDisconnect,
  loading,
}: NewRelicWebhookStepProps) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Step 2: Configure Alert Notifications</CardTitle>
            <CardDescription>Set up a webhook in New Relic to send alert notifications to Aurora</CardDescription>
          </div>
          <Badge variant="outline" className="border-[#00AC69] text-[#00AC69]">Connected</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg p-4 space-y-3">
          <div className="flex items-center gap-2">
            <span className="font-semibold text-sm">Connection Details</span>
          </div>
          <div className="grid md:grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-muted-foreground">Account:</span>{" "}
              <span className="font-medium">{status.accountName || status.accountId}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Region:</span>{" "}
              <span className="font-medium uppercase">{status.region || "US"}</span>
            </div>
            {status.userEmail && (
              <div>
                <span className="text-muted-foreground">User:</span>{" "}
                <span className="font-medium">{status.userName ? `${status.userName} (${status.userEmail})` : status.userEmail}</span>
              </div>
            )}
            {status.hasLicenseKey && (
              <div>
                <span className="text-muted-foreground">License Key:</span>{" "}
                <Badge variant="secondary" className="text-xs">Configured</Badge>
              </div>
            )}
          </div>
        </div>

        <div className="border rounded-lg p-4 space-y-3">
          <span className="font-semibold text-sm">Webhook URL</span>
          <div className="flex items-center gap-2">
            <code className="flex-1 bg-muted px-3 py-2 rounded text-xs break-all">{webhookUrl}</code>
            <Button
              variant="outline"
              size="sm"
              onClick={onCopy}
              className="shrink-0"
              aria-label={copied ? "Webhook URL copied" : "Copy webhook URL"}
            >
              {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>

          <div className="text-sm space-y-2 text-muted-foreground">
            <p className="font-medium text-foreground">Setup instructions:</p>
            <ol className="space-y-1 list-decimal list-inside">
              <li>In New Relic, navigate to <strong>Alerts &rarr; Destinations</strong>.</li>
              <li>Create a new <strong>Webhook</strong> destination with the URL above.</li>
              <li>Under <strong>Workflows</strong>, create or edit a workflow.</li>
              <li>Add a notification channel using the webhook destination.</li>
              <li>In the channel settings, enable <strong>Use custom payload</strong> and paste the JSON template below.</li>
              <li>Configure the workflow filter for the issues you want Aurora to investigate.</li>
              <li>Save and test the webhook to verify connectivity.</li>
            </ol>
          </div>

          <div className="space-y-2">
            <p className="text-sm font-medium">Recommended custom payload template:</p>
            <pre className="bg-muted p-3 rounded text-xs overflow-x-auto whitespace-pre leading-relaxed">{`{
  "issueTitle": {{ json annotations.title }},
  "issueId": {{ json issueId }},
  "issueUrl": {{ json issuePageUrl }},
  "state": {{ json stateText }},
  "priority": {{ json priority }},
  "conditionName": {{ json accumulations.conditionName }},
  "policyName": {{ json accumulations.policyName }},
  "totalIncidents": {{ json totalIncidents }},
  "entitiesData": {
    "names": {{ json entitiesData.names }},
    "types": {{ json entitiesData.types }}
  },
  "accountId": {{ json nrAccountId }}
}`}</pre>
            <p className="text-xs text-muted-foreground">
              This ensures Aurora receives the alert title and condition details for accurate incident reports.
            </p>
          </div>

          <a
            href="https://docs.newrelic.com/docs/alerts/get-notified/notification-integrations/#webhook"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-[#00AC69] hover:underline"
          >
            New Relic Webhook Documentation <ExternalLink className="h-3 w-3" />
          </a>
        </div>

        <div className="flex justify-end pt-2">
          <Button variant="destructive" onClick={onDisconnect} disabled={loading} size="sm">
            <Unplug className="h-4 w-4 mr-2" />
            {loading ? "Disconnecting..." : "Disconnect New Relic"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
