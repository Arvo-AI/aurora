"use client";

import { useCallback, useEffect, useState } from "react";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useToast } from "@/hooks/use-toast";
import {
  splunkOnCallService,
  type SplunkOnCallStatus,
  type SplunkOnCallWebhookInfo,
} from "@/lib/services/splunk-on-call";
import { copyToClipboard, getUserFriendlyError } from "@/lib/utils";

export default function SplunkOnCallAuthPage() {
  const { toast } = useToast();
  const [status, setStatus] = useState<SplunkOnCallStatus>({ connected: false });
  const [apiId, setApiId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [routingKeyContains, setRoutingKeyContains] = useState("");
  const [webhook, setWebhook] = useState<SplunkOnCallWebhookInfo | null>(null);
  const [loading, setLoading] = useState(true);

  const emitState = (connected: boolean) => {
    if (connected) localStorage.setItem("isSplunkOnCallConnected", "true");
    else localStorage.removeItem("isSplunkOnCallConnected");
    window.dispatchEvent(new CustomEvent("providerStateChanged"));
  };

  const load = useCallback(async () => {
    try {
      const next = await splunkOnCallService.getStatus();
      if (!next) {
        setStatus({ connected: false });
        emitState(false);
        return;
      }
      setStatus(next);
      setRoutingKeyContains(next.routingKeyContains ?? "");
      emitState(next.connected);
      if (next.connected) {
        setWebhook(await splunkOnCallService.getWebhookUrl());
      } else {
        setWebhook(null);
      }
    } catch (error) {
      toast({
        title: "Unable to load Splunk On-Call",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const connect = async (event: React.FormEvent) => {
    event.preventDefault();
    setLoading(true);
    try {
      await splunkOnCallService.connect(apiId, apiKey, routingKeyContains);
      setApiKey("");
      await load();
      toast({
        title: "Connected",
        description: "Splunk On-Call credentials validated.",
      });
    } catch (error) {
      toast({
        title: "Connection failed",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const disconnect = async () => {
    setLoading(true);
    try {
      await splunkOnCallService.disconnect();
      setStatus({ connected: false });
      setWebhook(null);
      emitState(false);
      toast({
        title: "Disconnected",
        description: "Splunk On-Call disconnected.",
      });
    } catch (error) {
      toast({
        title: "Disconnect failed",
        description: getUserFriendlyError(error),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const instructions = webhook?.instructions ?? [
    "Create an Any-Incident outgoing webhook in Splunk On-Call.",
    "Paste the webhook URL and add the authentication header.",
    "Use POST and application/json. Send the native incident body with all available variables.",
  ];

  return (
    <ConnectorAuthGuard connectorName="Splunk On-Call">
      <div className="container mx-auto max-w-4xl px-4 py-8">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Splunk On-Call Integration</h1>
          <p className="mt-1 text-muted-foreground">
            Connect Splunk On-Call to ingest native incidents and trigger Aurora RCA.
          </p>
        </div>

        {!status.connected ? (
          <Card>
            <CardHeader>
              <CardTitle>Connect Splunk On-Call</CardTitle>
              <CardDescription>
                Use an API ID and API key from the Splunk On-Call API settings.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <form className="space-y-4" onSubmit={connect}>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="soc-api-id">API ID</Label>
                    <Input id="soc-api-id" value={apiId} onChange={(event) => setApiId(event.target.value)} required />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="soc-api-key">API Key</Label>
                    <Input id="soc-api-key" type="password" value={apiKey} onChange={(event) => setApiKey(event.target.value)} required />
                  </div>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="soc-routing-key">Routing key contains</Label>
                  <Input id="soc-routing-key" value={routingKeyContains} onChange={(event) => setRoutingKeyContains(event.target.value)} />
                  <p className="text-xs text-muted-foreground">
                    Only matching incidents are ingested. Leave empty to accept all routing keys.
                  </p>
                </div>
                <Button type="submit" disabled={loading}>
                  {loading ? "Connecting…" : "Connect"}
                </Button>
              </form>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <CardTitle>Configure the outbound webhook</CardTitle>
              <CardDescription>
                Send incident events from Splunk On-Call to Aurora.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <div className="space-y-2">
                <Label>Webhook URL</Label>
                <div className="flex gap-2">
                  <code className="flex-1 break-all rounded border bg-muted p-2 text-xs">
                    {webhook?.webhookUrl}
                  </code>
                  <Button variant="outline" onClick={() => webhook && copyToClipboard(webhook.webhookUrl)}>Copy</Button>
                </div>
              </div>
              <div className="space-y-2">
                <Label>Authentication header</Label>
                <div className="flex gap-2">
                  <code className="flex-1 break-all rounded border bg-muted p-2 text-xs">
                    {webhook?.secretHeader}: {webhook?.webhookSecret}
                  </code>
                  <Button variant="outline" onClick={() => webhook && copyToClipboard(webhook.webhookSecret || "")}>Copy secret</Button>
                </div>
              </div>
              <ol className="list-inside list-decimal space-y-1 text-sm text-muted-foreground">
                {instructions.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
              <Button variant="outline" onClick={disconnect} disabled={loading}>
                {loading ? "Disconnecting…" : "Disconnect"}
              </Button>
            </CardContent>
          </Card>
        )}
      </div>
    </ConnectorAuthGuard>
  );
}
