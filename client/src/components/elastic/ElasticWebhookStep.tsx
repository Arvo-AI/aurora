"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Check, CheckCircle2, Copy, Eye, EyeOff, ExternalLink, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { elasticService, ElasticStatus, ElasticWebhookInfo } from "@/lib/services/elastic";
import { copyToClipboard } from "@/lib/utils";
import { ELASTIC_DOCS_WEBHOOK, ELASTIC_TEAL } from "./constants";

interface ElasticWebhookStepProps {
  status: ElasticStatus;
  onDisconnect: () => Promise<void>;
  loading: boolean;
}

const DEPLOYMENT_LABELS: Record<string, string> = {
  cloud_hosted: "Elastic Cloud Hosted",
  serverless: "Elastic Cloud Serverless",
  self_managed: "Self-managed",
};

type CopyKey = "url" | "secret" | "body";

export function ElasticWebhookStep({ status, onDisconnect, loading }: ElasticWebhookStepProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [webhook, setWebhook] = useState<ElasticWebhookInfo | null>(null);
  const [loadingWebhook, setLoadingWebhook] = useState(true);
  const [rcaEnabled, setRcaEnabled] = useState(false);
  const [loadingRca, setLoadingRca] = useState(true);
  const [updatingRca, setUpdatingRca] = useState(false);
  const [secretVisible, setSecretVisible] = useState(false);
  const [copied, setCopied] = useState<CopyKey | null>(null);

  useEffect(() => {
    let mounted = true;
    const load = async () => {
      setLoadingWebhook(true);
      setLoadingRca(true);
      const [webhookResult, rcaResult] = await Promise.allSettled([
        elasticService.getWebhookUrl(),
        elasticService.getRcaSettings(),
      ]);
      if (!mounted) return;
      if (webhookResult.status === "fulfilled") setWebhook(webhookResult.value);
      else console.error("[elastic] Failed to load webhook info:", webhookResult.reason);
      if (rcaResult.status === "fulfilled") setRcaEnabled(rcaResult.value.rcaEnabled);
      else console.error("[elastic] Failed to load RCA settings:", rcaResult.reason);
      setLoadingWebhook(false);
      setLoadingRca(false);
    };
    load();
    return () => {
      mounted = false;
    };
  }, []);

  const handleRcaToggle = async (enabled: boolean) => {
    setUpdatingRca(true);
    try {
      const result = await elasticService.updateRcaSettings(enabled);
      setRcaEnabled(result.rcaEnabled);
      toast({
        title: enabled ? "Alert RCA enabled" : "Alert RCA disabled",
        description: enabled
          ? "Kibana alerts sent to the webhook will now create incidents and start an automatic investigation."
          : "Kibana alerts will only be stored under View Alerts.",
      });
    } catch (error) {
      console.error("Failed to update RCA settings:", error);
      toast({ title: "Failed to update settings", description: "Could not update RCA settings. Please try again.", variant: "destructive" });
    } finally {
      setUpdatingRca(false);
    }
  };

  const copy = async (key: CopyKey, value: string | undefined, label: string) => {
    if (!value) return;
    try {
      await copyToClipboard(value);
      setCopied(key);
      toast({ title: "Copied", description: `${label} copied to clipboard` });
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 2000);
    } catch (error) {
      console.error("Failed to copy to clipboard:", error);
      toast({ title: "Copy failed", description: "Could not copy to clipboard. Please copy manually.", variant: "destructive" });
    }
  };

  const CopyButton = ({ k, value, label }: { k: CopyKey; value?: string; label: string }) => (
    <Button
      type="button"
      variant="outline"
      size="icon"
      onClick={() => copy(k, value, label)}
      disabled={!value}
      aria-label={copied === k ? `${label} copied` : `Copy ${label}`}
      className="shrink-0"
    >
      {copied === k ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
    </Button>
  );

  const maskedSecret = webhook?.webhookSecret ? "•".repeat(Math.min(webhook.webhookSecret.length, 32)) : "";

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-4">
          <div>
            <CardTitle className="flex items-center gap-2">
              <CheckCircle2 className="h-5 w-5 text-green-500" />
              Connected to Elastic Cloud
            </CardTitle>
            <CardDescription>Point Kibana alert rules at Aurora and decide whether they should start investigations.</CardDescription>
          </div>
          <Badge variant="outline" style={{ borderColor: ELASTIC_TEAL, color: ELASTIC_TEAL }}>Connected</Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Connection details */}
        <div className="bg-muted/50 rounded-lg p-4 space-y-3">
          <div className="flex flex-wrap gap-2">
            <Badge variant="secondary">{DEPLOYMENT_LABELS[status.deploymentType ?? ""] ?? "Elastic"}</Badge>
            {status.version && <Badge variant="secondary">v{status.version}</Badge>}
            {status.clusterName && <Badge variant="secondary">{status.clusterName}</Badge>}
            {status.kibanaUrl ? (
              <Badge variant={status.kibanaReachable ? "secondary" : "outline"}>
                Kibana {status.kibanaReachable ? "reachable" : "not verified"}
              </Badge>
            ) : (
              <Badge variant="outline">No Kibana URL</Badge>
            )}
          </div>
          <div className="grid md:grid-cols-2 gap-2 text-sm">
            <div className="min-w-0">
              <span className="text-muted-foreground">Elasticsearch:</span>{" "}
              <span className="font-mono text-xs break-all">{status.elasticsearchUrl}</span>
            </div>
            {status.kibanaUrl && (
              <div className="min-w-0">
                <span className="text-muted-foreground">Kibana:</span>{" "}
                <a href={status.kibanaUrl} target="_blank" rel="noopener noreferrer" className="font-mono text-xs break-all text-blue-600 hover:underline">
                  {status.kibanaUrl}
                </a>
              </div>
            )}
            <div>
              <span className="text-muted-foreground">Default index pattern:</span>{" "}
              <code className="text-xs">{status.indexPattern ?? "logs-*"}</code>
            </div>
            {status.username && (
              <div>
                <span className="text-muted-foreground">API key owner:</span> <span className="text-xs">{status.username}</span>
              </div>
            )}
          </div>
        </div>

        {/* Quick actions */}
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => router.push("/elastic/alerts")}>View Alerts</Button>
          {status.kibanaUrl && (
            <Button variant="outline" asChild>
              <a href={`${status.kibanaUrl}/app/observability/alerts`} target="_blank" rel="noopener noreferrer">
                Open Kibana Alerts <ExternalLink className="ml-1 h-3 w-3" />
              </a>
            </Button>
          )}
        </div>

        {/* RCA toggle */}
        <div className="border rounded-lg p-4" style={{ borderColor: rcaEnabled ? ELASTIC_TEAL : undefined }}>
          <div className="flex items-center justify-between gap-4">
            <div className="space-y-0.5">
              <Label htmlFor="elastic-rca-toggle" className="text-base font-medium">Enable Alert RCA</Label>
              <p className="text-sm text-muted-foreground">
                When on, Kibana alerts sent to the webhook create incidents and start an automatic investigation. When off, alerts are only stored under View Alerts.
              </p>
            </div>
            {loadingRca ? (
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            ) : (
              <Switch id="elastic-rca-toggle" checked={rcaEnabled} onCheckedChange={handleRcaToggle} disabled={updatingRca} />
            )}
          </div>
        </div>

        {/* Webhook configuration (always visible) */}
        <div className="border-t pt-6 space-y-4">
          <div>
            <h3 className="font-medium">Kibana Webhook connector</h3>
            <p className="text-sm text-muted-foreground">Create one Webhook connector in Kibana and attach it to any rule you want Aurora to see.</p>
          </div>

          {loadingWebhook ? (
            <div className="flex items-center gap-2 text-muted-foreground text-sm">
              <Loader2 className="h-4 w-4 animate-spin" /> Loading webhook configuration…
            </div>
          ) : webhook ? (
            <div className="space-y-4">
              <div className="space-y-1">
                <span className="text-sm font-medium">Webhook URL</span>
                <div className="flex gap-2">
                  <code className="flex-1 p-2 bg-muted rounded text-xs break-all">{webhook.webhookUrl}</code>
                  <CopyButton k="url" value={webhook.webhookUrl} label="Webhook URL" />
                </div>
              </div>

              <div className="space-y-1">
                <span className="text-sm font-medium">Webhook secret</span>
                <div className="flex gap-2">
                  <code className="flex-1 p-2 bg-muted rounded text-xs break-all font-mono">
                    {secretVisible ? webhook.webhookSecret : maskedSecret}
                  </code>
                  <Button
                    type="button"
                    variant="outline"
                    size="icon"
                    onClick={() => setSecretVisible((v) => !v)}
                    aria-label={secretVisible ? "Hide webhook secret" : "Reveal webhook secret"}
                    className="shrink-0"
                  >
                    {secretVisible ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                  </Button>
                  <CopyButton k="secret" value={webhook.webhookSecret} label="Webhook secret" />
                </div>
                <p className="text-xs text-muted-foreground">
                  Use it as the <strong>Basic auth</strong> password with username <code>{webhook.basicAuthUsername}</code>, or as the value of a <code>{webhook.headerName}</code> header.
                  Disconnecting deletes this secret; after reconnecting, update the Kibana connector with the new one.
                </p>
              </div>

              <div className="space-y-1">
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">Action body (paste into the rule action)</span>
                  <CopyButton k="body" value={webhook.actionBodyTemplate} label="Action body" />
                </div>
                <pre className="p-3 bg-muted rounded text-[11px] leading-snug overflow-auto max-h-64">{webhook.actionBodyTemplate}</pre>
              </div>

              <div className="bg-muted/50 rounded-lg p-4">
                <p className="font-medium text-sm mb-3">Setup steps</p>
                <ol className="list-decimal list-inside space-y-2 text-sm text-muted-foreground">
                  {webhook.instructions.map((instruction) => (
                    <li key={instruction}>{instruction.replace(/^\d+\.\s*/, "")}</li>
                  ))}
                </ol>
                <p className="text-xs text-muted-foreground mt-3">
                  The Webhook connector requires a Gold+ license on self-managed clusters; Elastic Cloud subscriptions include it.
                </p>
              </div>

              <a
                href={ELASTIC_DOCS_WEBHOOK}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-sm text-blue-600 hover:underline"
              >
                Kibana Webhook connector documentation <ExternalLink className="h-3 w-3" />
              </a>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">Failed to load webhook configuration.</p>
          )}
        </div>

        {/* Disconnect */}
        <div className="border-t pt-6">
          <Button variant="destructive" onClick={onDisconnect} disabled={loading} className="w-full">
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Disconnecting…
              </>
            ) : (
              "Disconnect Elastic Cloud"
            )}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
