"use client";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Copy, Check, ExternalLink, Unplug } from "lucide-react";
import type { SentryStatus } from "@/lib/services/sentry";
import { SENTRY_PURPLE } from "./constants";

interface SentryWebhookStepProps {
  status: SentryStatus;
  webhookUrl: string;
  copied: boolean;
  onCopy: () => void;
  onDisconnect: () => void;
  loading: boolean;
}

export function SentryWebhookStep({
  status,
  webhookUrl,
  copied,
  onCopy,
  onDisconnect,
  loading,
}: SentryWebhookStepProps) {
  const projectCount = status.accessibleProjects?.length ?? 0;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <div>
            <CardTitle>Step 2: Verify Webhook Setup</CardTitle>
            <CardDescription>Confirm the webhook URL is configured in your Sentry Internal Integration</CardDescription>
          </div>
          <Badge variant="outline" style={{ borderColor: SENTRY_PURPLE, color: SENTRY_PURPLE }}>Connected</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="border rounded-lg p-4 space-y-3">
          <span className="font-semibold text-sm">Connection Details</span>
          <div className="grid md:grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-muted-foreground">Organization:</span>{" "}
              <span className="font-medium">{status.orgName || status.orgSlug}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Region:</span>{" "}
              <span className="font-medium uppercase">{status.region || "US"}</span>
            </div>
            <div>
              <span className="text-muted-foreground">Client Secret:</span>{" "}
              {status.hasWebhookSecret ? (
                <Badge variant="secondary" className="text-xs">Configured</Badge>
              ) : (
                <Badge variant="destructive" className="text-xs">Missing &mdash; reconnect to add</Badge>
              )}
            </div>
            <div>
              <span className="text-muted-foreground">Projects:</span>{" "}
              <span className="font-medium">{projectCount}</span>
            </div>
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
            <p className="font-medium text-foreground">Verify in Sentry:</p>
            <ol className="space-y-1 list-decimal list-inside">
              <li>Open your <strong>Aurora</strong> Internal Integration in Sentry (<strong>Settings &rarr; Custom Integrations</strong>).</li>
              <li>Confirm the <strong>Webhook URL</strong> field matches the URL above exactly.</li>
              <li>Confirm <code>issue</code> and <code>error</code> are checked under <strong>Webhooks</strong>.</li>
              <li>Save the integration. Aurora will start receiving alerts on the next event.</li>
            </ol>
          </div>

          <a
            href="https://docs.sentry.io/integrations/integration-platform/internal-integration/"
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs hover:underline"
            style={{ color: SENTRY_PURPLE }}
          >
            Sentry Internal Integration Documentation <ExternalLink className="h-3 w-3" />
          </a>
        </div>

        <div className="flex justify-end pt-2">
          <Button variant="destructive" onClick={onDisconnect} disabled={loading} size="sm">
            <Unplug className="h-4 w-4 mr-2" />
            {loading ? "Disconnecting..." : "Disconnect Sentry"}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
