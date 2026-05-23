"use client";

import { useState, useEffect, useCallback } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
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
  const [rcaResources, setRcaResources] = useState<string[]>(["issue"]);
  const [loadingPref, setLoadingPref] = useState(true);

  useEffect(() => {
    fetch("/api/proxy/user-preferences?key=sentry_rca_resources")
      .then(res => res.ok ? res.json() : null)
      .then(data => {
        if (data?.value && Array.isArray(data.value)) {
          setRcaResources(data.value);
        }
      })
      .catch(() => {})
      .finally(() => setLoadingPref(false));
  }, []);

  const toggleResource = useCallback((resource: string, enabled: boolean) => {
    setRcaResources(prev => {
      const updated = enabled
        ? [...prev, resource]
        : prev.filter(r => r !== resource);

      fetch("/api/proxy/user-preferences", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key: "sentry_rca_resources", value: updated }),
      }).catch(() => {
        setRcaResources(prev);
      });

      return updated;
    });
  }, []);

  const projectCount = status.accessibleProjects?.length ?? 0;
  const secretBadge = status.hasWebhookSecret
    ? <Badge variant="secondary" className="text-xs">Configured</Badge>
    : <Badge variant="destructive" className="text-xs">Missing &mdash; reconnect to add</Badge>;
  const details: Array<[string, React.ReactNode]> = [
    ["Organization", <span key="org" className="font-medium">{status.orgName || status.orgSlug}</span>],
    ["Region", <span key="region" className="font-medium uppercase">{status.region || "US"}</span>],
    ["Client Secret", secretBadge],
    ["Projects", <span key="projects" className="font-medium">{projectCount}</span>],
  ];
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
            {details.map(([label, node]) => (
              <div key={label}>
                <span className="text-muted-foreground">{label}:</span>{" "}{node}
              </div>
            ))}
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

        <div className="border rounded-lg p-4 space-y-3">
          <span className="font-semibold text-sm">RCA Investigation Triggers</span>
          <p className="text-xs text-muted-foreground">Choose which Sentry event types trigger an automatic RCA investigation in Aurora.</p>
          <div className="space-y-3 pt-1">
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm font-medium">Issues</span>
                <p className="text-xs text-muted-foreground">Issue state changes (created, resolved, regression)</p>
              </div>
              <Switch
                checked={rcaResources.includes("issue")}
                onCheckedChange={(checked) => toggleResource("issue", checked)}
                disabled={loadingPref}
              />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <span className="text-sm font-medium">Errors</span>
                <p className="text-xs text-muted-foreground">Individual error/exception events (requires Business plan)</p>
              </div>
              <Switch
                checked={rcaResources.includes("error")}
                onCheckedChange={(checked) => toggleResource("error", checked)}
                disabled={loadingPref}
              />
            </div>
          </div>
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
