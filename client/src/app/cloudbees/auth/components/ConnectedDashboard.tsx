"use client";

import { Copy, Check } from "lucide-react";
import { Switch } from "@/components/ui/switch";
import { useToast } from "@/hooks/use-toast";
import { useState } from "react";
import type { CIProviderStatus } from "@/lib/services/ci-provider";

interface DiscoveredController {
  name: string;
  url: string;
  status: string;
}

interface ConnectedDashboardProps {
  status: CIProviderStatus | null;
  summary: any;
  webhookInfo: any;
  deployments: any[];
  controllers: DiscoveredController[];
  rcaEnabled: boolean;
  rcaLoading: boolean;
  loading: boolean;
  onDisconnect: () => void;
  onRcaToggle: (checked: boolean) => void;
}

function timeAgo(dateStr: string | null | undefined): string {
  if (!dateStr) return "";
  const date = new Date(dateStr);
  if (Number.isNaN(date.getTime())) return "";
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function ConnectedDashboard({
  status, summary, webhookInfo, deployments, controllers,
  rcaEnabled, rcaLoading, loading,
  onDisconnect, onRcaToggle,
}: Readonly<ConnectedDashboardProps>) {
  const { toast } = useToast();
  const [webhookCopied, setWebhookCopied] = useState(false);

  const copyWebhookUrl = () => {
    if (!webhookInfo?.webhookUrl) return;
    navigator.clipboard.writeText(webhookInfo.webhookUrl);
    setWebhookCopied(true);
    toast({ title: "Copied", description: "Webhook URL copied to clipboard" });
    setTimeout(() => setWebhookCopied(false), 2000);
  };

  return (
    <div className="animate-step-in">
      <div className="flex items-baseline gap-3 mb-1">
        <h1 className="text-[28px] font-bold tracking-tight">Connected</h1>
        <button
          onClick={onDisconnect}
          disabled={loading}
          className="text-[13px] text-[#666] hover:text-white transition-colors ml-auto"
        >
          {loading ? "Disconnecting..." : "Disconnect"}
        </button>
      </div>

      <div className="mt-6 space-y-4">
        <div>
          <p className="text-[13px] text-[#999] mb-1">URL</p>
          <p className="text-[15px]">{status?.baseUrl}</p>
        </div>
        {status?.username && (
          <div>
            <p className="text-[13px] text-[#999] mb-1">User</p>
            <p className="text-[15px]">{status.username}</p>
          </div>
        )}
        {status?.server?.version && status.server.version !== "unknown" && (
          <div>
            <p className="text-[13px] text-[#999] mb-1">Version</p>
            <p className="text-[15px]">{status.server.version}</p>
          </div>
        )}
      </div>

      {/* Stats grid */}
      {summary && (
        <div className="grid grid-cols-4 gap-4 mt-8 mb-8">
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
            <p className="text-[24px] font-bold">{summary.jobCount ?? "—"}</p>
            <p className="text-[11px] text-[#666] mt-1">Jobs</p>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
            <p className="text-[24px] font-bold">{summary.nodesOnline ?? "—"}<span className="text-[14px] text-[#555] font-normal">/{(summary.nodesOnline ?? 0) + (summary.nodesOffline ?? 0)}</span></p>
            <p className="text-[11px] text-[#666] mt-1">Nodes online</p>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
            <p className="text-[24px] font-bold">{summary.busyExecutors ?? "—"}<span className="text-[14px] text-[#555] font-normal">/{summary.totalExecutors ?? 0}</span></p>
            <p className="text-[11px] text-[#666] mt-1">Executors busy</p>
          </div>
          <div className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04]">
            <p className="text-[24px] font-bold">{summary.queueSize ?? "—"}</p>
            <p className="text-[11px] text-[#666] mt-1">Queue</p>
          </div>
        </div>
      )}

      {/* Job health bar */}
      {summary?.jobHealth && (() => {
        const health = summary.jobHealth;
        const total = (health.healthy || 0) + (health.unstable || 0) + (health.failing || 0) + (health.disabled || 0) + (health.other || 0);
        if (total === 0) return null;
        return (
          <div className="mb-8">
            <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-3">Job Health</p>
            <div className="h-2 rounded-full overflow-hidden bg-white/[0.04] flex">
              {health.healthy > 0 && <div className="bg-emerald-400" style={{ width: `${(health.healthy / total) * 100}%` }} />}
              {health.unstable > 0 && <div className="bg-yellow-400" style={{ width: `${(health.unstable / total) * 100}%` }} />}
              {health.failing > 0 && <div className="bg-red-400" style={{ width: `${(health.failing / total) * 100}%` }} />}
              {health.disabled > 0 && <div className="bg-[#555]" style={{ width: `${(health.disabled / total) * 100}%` }} />}
              {health.other > 0 && <div className="bg-[#444]" style={{ width: `${(health.other / total) * 100}%` }} />}
            </div>
            <div className="flex gap-4 mt-2 text-[11px] text-[#666]">
              {health.healthy > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-emerald-400" />{health.healthy} healthy</span>}
              {health.unstable > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-yellow-400" />{health.unstable} unstable</span>}
              {health.failing > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-red-400" />{health.failing} failing</span>}
              {health.disabled > 0 && <span className="flex items-center gap-1.5"><span className="w-2 h-2 rounded-full bg-[#555]" />{health.disabled} disabled</span>}
            </div>
          </div>
        );
      })()}

      {/* RCA toggle */}
      <div className="flex items-center justify-between py-4 border-t border-white/[0.04]">
        <div>
          <p className="text-[14px] text-white">Auto-trigger RCA on failures</p>
          <p className="text-[12px] text-[#666]">Automatically investigate when a build fails</p>
        </div>
        <Switch checked={rcaEnabled} onCheckedChange={onRcaToggle} disabled={rcaLoading} />
      </div>

      {/* Webhook section */}
      {webhookInfo && (
        <div className="mt-8 pt-6 border-t border-white/[0.04]">
          <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Deployment Webhook</p>
          <div className="flex gap-2 mb-4">
            <code className="flex-1 px-4 py-3 rounded-xl bg-white/[0.02] border border-white/[0.04] text-[12px] text-[#888] font-mono truncate">{webhookInfo.webhookUrl}</code>
            <button
              type="button"
              onClick={copyWebhookUrl}
              className="px-3 py-3 rounded-xl border border-white/[0.06] hover:bg-white/[0.04] transition-colors"
            >
              {webhookCopied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4 text-[#777]" />}
            </button>
          </div>
          {webhookInfo.jenkinsfileBasic && (
            <details>
              <summary className="text-[12px] text-[#666] cursor-pointer hover:text-[#999] transition-colors">View Jenkinsfile snippet</summary>
              <div className="relative mt-3">
                <button
                  onClick={() => { navigator.clipboard.writeText(webhookInfo.jenkinsfileBasic); toast({ title: "Copied", description: "Jenkinsfile snippet copied to clipboard" }); }}
                  className="absolute top-3 right-3 p-1.5 rounded-lg bg-white/[0.05] hover:bg-white/[0.1] text-[#666] hover:text-white transition-all"
                  title="Copy snippet"
                >
                  <Copy className="w-3.5 h-3.5" />
                </button>
                <pre className="p-4 rounded-xl bg-white/[0.02] border border-white/[0.04] text-[11px] text-[#888] font-mono overflow-x-auto whitespace-pre">{webhookInfo.jenkinsfileBasic}</pre>
              </div>
            </details>
          )}
        </div>
      )}

      {/* Recent deployments */}
      {deployments.length > 0 && (
        <div className="mt-8 pt-6 border-t border-white/[0.04]">
          <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Recent Deployments</p>
          <div className="space-y-2">
            {deployments.slice(0, 5).map((d) => {
              const statusColor = d.result === "SUCCESS" ? "bg-emerald-400" : d.result === "FAILURE" ? "bg-red-400" : "bg-[#666]";
              return (
              <div key={`${d.service}-${d.buildNumber}`} className="flex items-center justify-between py-2.5 px-4 rounded-xl bg-white/[0.02]">
                <div className="flex items-center gap-3">
                  <div className={`w-2 h-2 rounded-full ${statusColor}`} />
                  <span className="text-[13px]">{d.service}</span>
                  {d.environment && <span className="text-[11px] text-[#555]">{d.environment}</span>}
                </div>
                <span className="text-[11px] text-[#555]">#{d.buildNumber} · {timeAgo(d.receivedAt)}</span>
              </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Managed controllers (OC mode) */}
      {controllers.length > 0 && (
        <div className="mt-8 pt-6 border-t border-white/[0.04]">
          <p className="text-[11px] uppercase tracking-[0.12em] text-[#555] mb-4">Managed Controllers</p>
          <div className="space-y-1">
            {controllers.map((ctrl) => (
              <div key={ctrl.url} className="flex items-center justify-between py-3 px-4 rounded-xl bg-white/[0.02]">
                <div className="flex-1 min-w-0">
                  <p className="text-[14px] truncate">{ctrl.name}</p>
                  <p className="text-[12px] text-[#555] truncate">{ctrl.url}</p>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`h-2 w-2 rounded-full ${ctrl.status === "online" ? "bg-green-500" : "bg-[#555]"}`} />
                  <span className="text-[12px] text-[#777]">{ctrl.status}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
