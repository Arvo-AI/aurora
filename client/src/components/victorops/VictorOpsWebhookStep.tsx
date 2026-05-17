"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Copy, Check, ExternalLink } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { copyToClipboard } from "@/lib/utils";

export function VictorOpsWebhookStep() {
  const { toast } = useToast();
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [rcaEnabled, setRcaEnabled] = useState(true);
  const [rcaLoading, setRcaLoading] = useState(false);
  const rcaToggleInProgress = useRef(false);

  useEffect(() => {
    loadWebhook();
    loadRcaPreference();
  }, []);

  const loadRcaPreference = async () => {
    try {
      const response = await fetch('/api/user-preferences?key=automated_rca_enabled');
      if (response.ok) {
        const data = await response.json();
        setRcaEnabled(data.value !== false);
      }
    } catch {
      setRcaEnabled(true);
    }
  };

  const loadWebhook = async () => {
    try {
      setLoading(true);
      const response = await fetch('/api/victorops/webhook-url');
      if (!response.ok) throw new Error('Failed to load webhook URL');
      const data = await response.json();
      setWebhookUrl(data.webhookUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load webhook');
    } finally {
      setLoading(false);
    }
  };

  const handleCopy = async (text: string) => {
    try {
      await copyToClipboard(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({
        title: 'Copy failed',
        description: 'Please copy the URL manually.',
        variant: 'destructive',
      });
    }
  };

  const handleRcaToggle = useCallback(async (checked: boolean) => {
    if (rcaToggleInProgress.current || checked === rcaEnabled) return;

    rcaToggleInProgress.current = true;
    const previousValue = rcaEnabled;
    setRcaEnabled(checked);
    setRcaLoading(true);

    try {
      const response = await fetch('/api/user-preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key: 'automated_rca_enabled', value: checked }),
      });

      if (response.ok) {
        toast({
          title: checked ? 'Automated RCA Enabled' : 'Automated RCA Disabled',
          description: checked
            ? 'Aurora will automatically analyze new Splunk On-Call incidents'
            : 'Automated RCA has been disabled',
        });
      } else {
        throw new Error('Failed to update preference');
      }
    } catch {
      setRcaEnabled(previousValue);
      toast({
        title: 'Error',
        description: 'Failed to update automated RCA setting',
        variant: 'destructive',
      });
    } finally {
      setRcaLoading(false);
      rcaToggleInProgress.current = false;
    }
  }, [rcaEnabled, toast]);

  if (loading) return (
    <Card>
      <CardContent className="py-8 text-center text-muted-foreground">
        Loading webhook…
      </CardContent>
    </Card>
  );

  if (error) return (
    <Card>
      <CardContent className="py-8 text-center text-destructive">{error}</CardContent>
    </Card>
  );

  if (!webhookUrl) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Webhook Configuration</CardTitle>
        <CardDescription>
          Configure Splunk On-Call to forward incidents to Aurora
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium">Webhook URL</p>
            <Badge variant="outline">Per user</Badge>
          </div>
          <div className="flex gap-2">
            <code className="flex-1 px-3 py-2 rounded bg-muted text-xs break-all border">
              {webhookUrl}
            </code>
            <Button
              variant={copied ? "secondary" : "outline"}
              size="sm"
              onClick={() => handleCopy(webhookUrl)}
              aria-label="Copy webhook URL"
              title="Copy webhook URL"
            >
              {copied ? (
                <Check className="h-4 w-4" />
              ) : (
                <Copy className="h-4 w-4" />
              )}
            </Button>
          </div>
        </div>

        <div className="space-y-3 text-sm">
          <p className="font-medium">Setup Instructions:</p>
          <ol className="list-decimal list-inside space-y-2 text-muted-foreground">
            <li>
              Log in to your Splunk On-Call portal and go to{" "}
              <strong>Integrations → Outgoing Webhooks</strong>
            </li>
            <li>Click <strong>Add Webhook</strong></li>
            <li>
              Set <strong>Event Type</strong> to{" "}
              <code className="bg-muted px-1 rounded">Any-Incident</code>
            </li>
            <li>Paste the webhook URL above and save</li>
            <li>Trigger a test alert to verify the connection</li>
          </ol>
          <a
            href="https://help.victorops.com/knowledge-base/outbound-webhooks/"
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1 mt-2"
          >
            Splunk On-Call Webhook Docs <ExternalLink className="w-3 h-3" />
          </a>
        </div>

        <div className="border-t pt-6">
          <p className="text-sm font-medium mb-4">Automation Settings</p>
          <div className="p-4 border rounded-lg bg-muted/20">
            <div className="flex items-center justify-between">
              <div className="space-y-0.5">
                <Label htmlFor="vo-rca-toggle" className="text-sm font-medium cursor-pointer">
                  Automated Root Cause Analysis
                </Label>
                <p className="text-xs text-muted-foreground">
                  Automatically investigate new incidents with AI-powered RCA.
                </p>
              </div>
              <Switch
                id="vo-rca-toggle"
                checked={rcaEnabled}
                onCheckedChange={handleRcaToggle}
                disabled={rcaLoading || loading}
              />
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
