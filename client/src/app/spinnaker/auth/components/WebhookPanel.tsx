"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Check, Copy, Webhook } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { copyToClipboard } from "@/lib/utils";
import type { SpinnakerWebhookInfo } from "@/lib/services/spinnaker";

interface WebhookPanelProps {
  webhookInfo: SpinnakerWebhookInfo;
}

export function WebhookPanel({ webhookInfo }: WebhookPanelProps) {
  const [copied, setCopied] = useState(false);
  const { toast } = useToast();

  const handleCopy = async (text: string) => {
    try {
      await copyToClipboard(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast({ title: "Copy failed", description: "Unable to copy to clipboard", variant: "destructive" });
    }
  };

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-center gap-2">
          <Webhook className="h-5 w-5 text-teal-600" />
          <CardTitle className="text-lg">Send Deployment Events to Aurora</CardTitle>
        </div>
        <CardDescription>
          Configure Spinnaker Echo to send pipeline events to Aurora for deployment tracking
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div>
          <p className="text-xs font-medium text-muted-foreground mb-1.5">Webhook URL</p>
          <div className="relative">
            <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all pr-20">
              <code>{webhookInfo.webhookUrl}</code>
            </pre>
            <Button size="sm" variant="secondary" className="absolute top-2 right-2 h-8 gap-1.5"
              onClick={() => handleCopy(webhookInfo.webhookUrl)}>
              {copied ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
              {copied ? "Copied!" : "Copy"}
            </Button>
          </div>
        </div>

        {webhookInfo.echoConfig && (
          <div>
            <p className="text-xs font-medium text-muted-foreground mb-1.5">Echo Configuration</p>
            <pre className="text-xs bg-muted p-3 rounded-lg whitespace-pre-wrap break-all">
              <code>{webhookInfo.echoConfig}</code>
            </pre>
          </div>
        )}

        {webhookInfo.instructions && webhookInfo.instructions.length > 0 && (
          <div className="space-y-1.5 text-xs text-muted-foreground">
            <p className="font-medium text-foreground">Setup Instructions:</p>
            {webhookInfo.instructions.map((instruction, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className="font-mono w-4 text-right shrink-0">{i + 1}.</span>
                <span>{instruction}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
