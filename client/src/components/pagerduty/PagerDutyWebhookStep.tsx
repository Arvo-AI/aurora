"use client";

import { useState, useEffect, useCallback, useRef } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Label } from "@/components/ui/label";
import { Copy, Check, ExternalLink } from "lucide-react";
import { useToast } from "@/hooks/use-toast";

export function PagerDutyWebhookStep() {
  const { toast } = useToast();
  const [webhookUrl, setWebhookUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [rcaEnabled, setRcaEnabled] = useState(false);
  const [rcaLoading, setRcaLoading] = useState(false);
  const rcaToggleInProgress = useRef(false);

  useEffect(() => {
    loadWebhook();
    loadRcaPreference();
  }, []);

  // Load RCA preference on mount
  const loadRcaPreference = async () => {
    try {
      const response = await fetch('/api/user-preferences?key=automated_rca_enabled');
      if (response.ok) {
        const data = await response.json();
        // Default to TRUE if preference not set (enabled by default)
        setRcaEnabled(data.value !== false);
      } else {
        // Default to enabled if fetch fails
        setRcaEnabled(true);
      }
    } catch (error) {
      console.error('Failed to load RCA preference:', error);
      // Default to enabled if error occurs
      setRcaEnabled(true);
    }
  };

  const loadWebhook = async () => {
    try {
      setLoading(true);
      const response = await fetch('/api/pagerduty/webhook-url');
      if (!response.ok) throw new Error('Failed to load webhook URL');
      const data = await response.json();
      setWebhookUrl(data.webhookUrl);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load webhook');
    } finally {
      setLoading(false);
    }
  };

  const copyToClipboard = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setError('Failed to copy');
    }
  };

  const handleRcaToggle = useCallback(async (checked: boolean) => {
    // Prevent multiple simultaneous calls
    if (rcaToggleInProgress.current) {
      return;
    }

    // Prevent toggling if the value is already the same
    if (checked === rcaEnabled) {
      return;
    }

    rcaToggleInProgress.current = true;
    const previousValue = rcaEnabled;
    // Optimistically update UI to reflect user choice immediately
    setRcaEnabled(checked);
    setRcaLoading(true);
    try {
      const response = await fetch('/api/user-preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          key: 'automated_rca_enabled',
          value: checked,
        }),
      });

      if (response.ok) {
        // Success toast (state already updated optimistically)
        toast({
          title: checked ? 'Automated RCA Enabled' : 'Automated RCA DEACTIVATED',
          description: checked 
            ? 'Aurora will automatically analyze new incidents and wait for runbook links' 
            : 'Automated RCA has been deactivated. You can manually trigger RCA from the incidents page',
        });
      } else {
        throw new Error('Failed to update preference');
      }
    } catch (error) {
      console.error('Failed to update RCA preference:', error);
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


  if (loading) return <Card><CardContent className="py-8 text-center text-muted-foreground">Loading webhook...</CardContent></Card>;
  if (error) return <Card><CardContent className="py-8 text-center text-destructive">{error}</CardContent></Card>;
  if (!webhookUrl) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Webhook Configuration</CardTitle>
        <CardDescription>Configure PagerDuty to send incidents to Aurora</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Webhook URL */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium">Webhook URL</p>
            <Badge variant="outline">Per user</Badge>
          </div>
          <div className="flex gap-2">
            <code className="flex-1 px-3 py-2 rounded bg-muted text-xs break-all border">{webhookUrl}</code>
            <Button variant={copied ? "secondary" : "outline"} size="sm" onClick={() => copyToClipboard(webhookUrl)}>
              {copied ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </Button>
          </div>
        </div>

        {/* Instructions */}
        <div className="space-y-3 text-sm">
          <p className="font-medium">Setup Instructions:</p>
          <ol className="list-decimal list-inside space-y-2 text-muted-foreground">
            <li>Go to <strong>PagerDuty → Integrations → Generic Webhooks (v3) → New Webhook</strong></li>
            <li>Paste the webhook URL above and set scope to <strong>Account</strong> or specific services</li>
            <li>Subscribe to events: <code className="bg-muted px-1 rounded">incident.triggered</code>, <code className="bg-muted px-1 rounded">incident.acknowledged</code>, <code className="bg-muted px-1 rounded">incident.resolved</code>, <code className="bg-muted px-1 rounded">incident.custom_field_values.updated</code></li>
            <li>Click <strong>Add Webhook</strong> and <strong>send a test notification</strong> to verify the connection</li>
          </ol>
          <a
            href="https://support.pagerduty.com/docs/webhooks"
            target="_blank"
            rel="noopener noreferrer"
            className="text-xs text-blue-600 dark:text-blue-400 hover:underline inline-flex items-center gap-1 mt-2"
          >
            PagerDuty Webhook Docs <ExternalLink className="w-3 h-3" />
          </a>
        </div>

        {/* Divider */}
        <div className="border-t pt-6 mt-6">
          <p className="text-sm font-medium mb-4">Automation Settings</p>
        </div>

        {/* Automated RCA Setting */}
        <div className="p-4 border rounded-lg bg-muted/20">
          <div className="flex items-center justify-between">
            <div className="space-y-0.5">
              <Label htmlFor="rca-toggle" className="text-sm font-medium cursor-pointer">
                Automated Root Cause Analysis
              </Label>
              <p className="text-xs text-muted-foreground">
                <strong>ENABLED BY DEFAULT.</strong> Automatically analyze incidents when triggered. RCA will wait for runbook links when available. Toggle off to explicitly deactivate.
              </p>
            </div>
            <Switch
              id="rca-toggle"
              checked={rcaEnabled}
              onCheckedChange={handleRcaToggle}
              disabled={rcaLoading || loading}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

