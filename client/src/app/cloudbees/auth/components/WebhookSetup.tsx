"use client";

import { Copy, Check, Loader2 } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { useState } from "react";

interface WebhookSetupProps {
  webhookInfo: any;
  onDone: () => void;
}

export function WebhookSetup({ webhookInfo, onDone }: Readonly<WebhookSetupProps>) {
  const { toast } = useToast();
  const [copied, setCopied] = useState(false);

  const copyUrl = () => {
    if (!webhookInfo?.webhookUrl) return;
    navigator.clipboard.writeText(webhookInfo.webhookUrl);
    setCopied(true);
    toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="animate-step-in">
      <h1 className="text-[28px] font-bold tracking-tight mb-3">Set up deployment tracking</h1>
      <p className="text-[15px] text-[#777] mb-10">
        Add this webhook to your Jenkinsfile so Aurora is notified when deployments complete.
      </p>

      {webhookInfo ? (
        <div className="space-y-6">
          <div>
            <p className="block text-[13px] text-[#999] mb-2">Webhook URL</p>
            <div className="flex items-center gap-2">
              <code className="flex-1 text-[13px] text-[#777] bg-white/[0.02] border border-white/[0.06] px-4 py-3.5 rounded-xl truncate">
                {webhookInfo.webhookUrl}
              </code>
              <button
                type="button"
                onClick={copyUrl}
                className="p-3.5 rounded-xl border border-white/[0.06] hover:bg-white/[0.04] transition-colors"
              >
                {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4 text-[#777]" />}
              </button>
            </div>
          </div>

          {webhookInfo.jenkinsfileBasic && (
            <div>
              <p className="block text-[13px] text-[#999] mb-2">Jenkinsfile snippet</p>
              <div className="relative">
                <button
                  onClick={() => { navigator.clipboard.writeText(webhookInfo.jenkinsfileBasic); toast({ title: "Copied", description: "Jenkinsfile snippet copied to clipboard" }); }}
                  className="absolute top-3 right-3 p-1.5 rounded-lg bg-white/[0.05] hover:bg-white/[0.1] text-[#666] hover:text-white transition-all"
                  title="Copy snippet"
                >
                  <Copy className="w-3.5 h-3.5" />
                </button>
                <pre className="text-[13px] text-[#999] bg-white/[0.02] border border-white/[0.06] p-5 rounded-xl overflow-x-auto whitespace-pre leading-relaxed">
                  {webhookInfo.jenkinsfileBasic}
                </pre>
              </div>
            </div>
          )}
        </div>
      ) : (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="h-5 w-5 animate-spin text-[#555]" />
        </div>
      )}

      <div className="flex items-center gap-3 mt-10">
        <button
          type="button"
          onClick={onDone}
          className="flex-1 py-3.5 rounded-xl bg-white text-black font-medium text-[15px] hover:bg-white/90 transition-colors flex items-center justify-center"
        >
          Done
        </button>
      </div>
      <div className="text-center mt-4">
        <button
          type="button"
          onClick={onDone}
          className="text-[13px] text-[#777] hover:text-white transition-colors"
        >
          Skip for now
        </button>
      </div>
    </div>
  );
}
