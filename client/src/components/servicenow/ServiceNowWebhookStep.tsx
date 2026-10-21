"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { ChevronDown, Copy, Eye, EyeOff, Loader2, RefreshCw } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { serviceNowService, type ServiceNowWebhookUrlResponse } from "@/lib/services/servicenow";
import { copyToClipboard } from "@/lib/utils";

export function ServiceNowWebhookStep() {
  const { toast } = useToast();
  const [webhookData, setWebhookData] = useState<ServiceNowWebhookUrlResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [revealSecret, setRevealSecret] = useState(false);
  const [rotating, setRotating] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [scriptOpen, setScriptOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const data = await serviceNowService.getWebhookUrl();
      if (cancelled) return;
      setWebhookData(data);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const copy = async (text: string, label: string) => {
    try {
      await copyToClipboard(text);
      toast({ title: "Copied", description: `${label} copied to clipboard` });
    } catch {
      toast({ title: "Copy failed", description: `Could not copy ${label}`, variant: "destructive" });
    }
  };

  const handleRotate = async () => {
    setRotating(true);
    try {
      const newSecret = await serviceNowService.rotateWebhookSecret();
      setWebhookData((prev) => (prev ? { ...prev, webhookSecret: newSecret } : prev));
      setRevealSecret(true);
      toast({
        title: "Secret rotated",
        description: "Update the secret in your ServiceNow Business Rule script.",
      });
    } catch (err) {
      console.error("ServiceNow secret rotation failed", err);
      toast({
        title: "Failed to rotate secret",
        description: "Could not rotate the webhook secret. Please try again.",
        variant: "destructive",
      });
    } finally {
      setRotating(false);
      setConfirmOpen(false);
    }
  };

  let body: React.ReactNode;
  if (loading) {
    body = (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" />
        Loading webhook details...
      </div>
    );
  } else if (!webhookData) {
    body = <p className="text-sm text-muted-foreground">Unable to load webhook details. Please try refreshing.</p>;
  } else {
    body = (
      <div className="space-y-4">
        {webhookData.instructions?.length > 0 && (
          <ol className="list-decimal list-inside space-y-1 text-sm text-muted-foreground">
            {webhookData.instructions.map((step) => (
              <li key={step}>{step}</li>
            ))}
          </ol>
        )}

        <div>
          <Label htmlFor="servicenow-webhook-url" className="text-sm font-medium">Webhook URL</Label>
          <div className="flex gap-2 mt-1">
            <code id="servicenow-webhook-url" className="flex-1 p-2 bg-muted rounded text-xs break-all">
              {webhookData.webhookUrl}
            </code>
            <Button
              variant="outline"
              size="icon"
              aria-label="Copy webhook URL"
              onClick={() => copy(webhookData.webhookUrl, "Webhook URL")}
            >
              <Copy className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div>
          <Label className="text-sm font-medium">Header name</Label>
          <div className="flex gap-2 mt-1">
            <code className="flex-1 p-2 bg-muted rounded text-xs break-all">{webhookData.headerName}</code>
            <Button
              variant="outline"
              size="icon"
              aria-label="Copy header name"
              onClick={() => copy(webhookData.headerName, "Header name")}
            >
              <Copy className="h-4 w-4" />
            </Button>
          </div>
        </div>

        <div>
          <Label htmlFor="servicenow-webhook-secret" className="text-sm font-medium">Webhook secret</Label>
          <div className="flex gap-2 mt-1">
            <code id="servicenow-webhook-secret" className="flex-1 p-2 bg-muted rounded text-xs break-all">
              {revealSecret ? webhookData.webhookSecret : "•".repeat(32)}
            </code>
            <Button
              variant="outline"
              size="icon"
              aria-label={revealSecret ? "Hide secret" : "Reveal secret"}
              onClick={() => setRevealSecret((v) => !v)}
            >
              {revealSecret ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
            <Button
              variant="outline"
              size="icon"
              aria-label="Copy webhook secret"
              onClick={() => copy(webhookData.webhookSecret, "Webhook secret")}
            >
              <Copy className="h-4 w-4" />
            </Button>
          </div>
          <div className="mt-2">
            <Button variant="outline" size="sm" onClick={() => setConfirmOpen(true)} disabled={rotating}>
              {rotating ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <RefreshCw className="mr-2 h-4 w-4" />
              )}
              Rotate secret
            </Button>
          </div>
        </div>

        <Collapsible open={scriptOpen} onOpenChange={setScriptOpen}>
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="px-0">
              <ChevronDown className={`mr-1 h-4 w-4 transition-transform ${scriptOpen ? "rotate-180" : ""}`} />
              Business Rule script
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="relative mt-2">
              <Button
                variant="outline"
                size="sm"
                className="absolute right-2 top-2"
                onClick={() => copy(webhookData.businessRuleScript, "Business Rule script")}
              >
                <Copy className="mr-1 h-3 w-3" />
                Copy
              </Button>
              <pre className="p-3 pr-20 bg-muted rounded text-xs overflow-x-auto max-h-96">
                <code>{webhookData.businessRuleScript}</code>
              </pre>
            </div>
          </CollapsibleContent>
        </Collapsible>
      </div>
    );
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle>Configure Webhook</CardTitle>
          <CardDescription>
            Create a Business Rule in ServiceNow that sends new incidents to Aurora.
          </CardDescription>
        </CardHeader>
        <CardContent>{body}</CardContent>
      </Card>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Rotate webhook secret?</AlertDialogTitle>
            <AlertDialogDescription>
              The current secret stops working immediately. Incidents will not reach Aurora until you update the
              secret in your ServiceNow Business Rule.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={rotating}>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault();
                void handleRotate();
              }}
              disabled={rotating}
            >
              {rotating ? "Rotating..." : "Rotate secret"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
