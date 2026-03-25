"use client";

import React from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useQuery, type Fetcher } from "@/lib/query";
import type { ResourceDetail } from "@/lib/services/observability";
import StatusBadge from "../../components/StatusBadge";

const detailFetcher: Fetcher<ResourceDetail> = async (key, signal) => {
  const res = await fetch(key, { credentials: "include", signal });
  if (!res.ok) throw new Error(`detail ${res.status}`);
  return res.json();
};

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-3">
      <h2 className="text-sm font-semibold text-muted-foreground uppercase tracking-wider border-b border-border pb-2">
        {title}
      </h2>
      {children}
    </div>
  );
}

function PropertyRow({ label, value }: { label: string; value: string | undefined | null }) {
  if (!value) return null;
  return (
    <div className="flex items-start gap-4 py-1">
      <span className="text-sm text-muted-foreground w-40 flex-shrink-0">{label}</span>
      <span className="text-sm text-foreground break-all">{value}</span>
    </div>
  );
}

export default function ResourceDetailClient() {
  const params = useParams();
  const id = typeof params.id === "string" ? decodeURIComponent(params.id) : "";

  const { data: resource, isLoading, error } = useQuery<ResourceDetail>(
    id ? `/api/observability/resources/${encodeURIComponent(id)}` : null,
    detailFetcher,
    { staleTime: 30_000, revalidateOnFocus: true }
  );

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-full">
        <div className="animate-pulse text-muted-foreground">Loading resource...</div>
      </div>
    );
  }

  if (error || !resource) {
    return (
      <div className="flex-1 overflow-auto">
        <div className="max-w-5xl mx-auto px-6 py-6">
          <Link href="/observability" className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-4">
            <ArrowLeft size={16} />
            Back to Observability
          </Link>
          <p className="text-muted-foreground">Resource not found.</p>
        </div>
      </div>
    );
  }

  const metadata = typeof resource.metadata === "string"
    ? (() => { try { return JSON.parse(resource.metadata); } catch { return {}; } })()
    : resource.metadata || {};

  return (
    <div className="flex-1 overflow-auto">
      <div className="max-w-5xl mx-auto px-6 py-6 space-y-8">
        {/* Header */}
        <div>
          <Link href="/observability" className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-4">
            <ArrowLeft size={16} />
            Back to Observability
          </Link>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-foreground">{resource.display_name || resource.name}</h1>
              <p className="text-sm text-muted-foreground mt-1">
                {resource.resource_type}
                {resource.sub_type && resource.sub_type !== resource.resource_type ? ` (${resource.sub_type})` : ""}
                {" · "}
                <span className="capitalize">{resource.provider}</span>
                {resource.region ? ` · ${resource.region}` : ""}
              </p>
            </div>
            <div className="flex items-center gap-2">
              <StatusBadge status={resource.status} showLabel />
            </div>
          </div>
        </div>

        {/* Properties */}
        <Section title="Properties">
          <div className="rounded-lg border border-border bg-card p-4">
            <PropertyRow label="Cloud Resource ID" value={resource.cloud_resource_id} />
            <PropertyRow label="Region" value={resource.region} />
            <PropertyRow label="Provider" value={resource.provider} />
            <PropertyRow label="Endpoint" value={resource.endpoint} />
            <PropertyRow label="Category" value={resource.category} />
            <PropertyRow label="Last Seen" value={resource.updated_at} />
            {metadata.tags && (
              <PropertyRow
                label="Tags"
                value={Object.entries(metadata.tags as Record<string, string>)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")}
              />
            )}
            {metadata.labels && (
              <PropertyRow
                label="Labels"
                value={Object.entries(metadata.labels as Record<string, string>)
                  .map(([k, v]) => `${k}=${v}`)
                  .join(", ")}
              />
            )}
          </div>
        </Section>

        {/* Dependencies */}
        {((resource.upstream && resource.upstream.length > 0) || (resource.downstream && resource.downstream.length > 0)) && (
          <Section title="Dependencies">
            <div className="rounded-lg border border-border bg-card p-4 space-y-4">
              {resource.upstream && resource.upstream.length > 0 && (
                <div>
                  <h3 className="text-xs font-medium text-muted-foreground mb-2">
                    Upstream ({resource.upstream.length})
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {resource.upstream.map((dep) => (
                      <Link
                        key={dep.name}
                        href={`/observability/${encodeURIComponent(dep.name)}`}
                        className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium border border-border bg-muted hover:bg-muted/80 transition-colors"
                      >
                        {dep.name}
                        {dep.type && <span className="ml-1 text-muted-foreground">({dep.type})</span>}
                      </Link>
                    ))}
                  </div>
                </div>
              )}
              {resource.downstream && resource.downstream.length > 0 && (
                <div>
                  <h3 className="text-xs font-medium text-muted-foreground mb-2">
                    Downstream ({resource.downstream.length})
                  </h3>
                  <div className="flex flex-wrap gap-2">
                    {resource.downstream.map((dep) => (
                      <Link
                        key={dep.name}
                        href={`/observability/${encodeURIComponent(dep.name)}`}
                        className="inline-flex items-center px-2.5 py-1 rounded-md text-xs font-medium border border-border bg-muted hover:bg-muted/80 transition-colors"
                      >
                        {dep.name}
                        {dep.type && <span className="ml-1 text-muted-foreground">({dep.type})</span>}
                      </Link>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* K8s Workloads (only for clusters) */}
        {resource.k8s_workloads && (
          <Section title="K8s Workloads">
            <div className="rounded-lg border border-border bg-card overflow-hidden">
              {resource.k8s_workloads.pods.length > 0 && (
                <div>
                  <h3 className="text-xs font-medium text-muted-foreground px-4 py-2 bg-muted/50">
                    Pods ({resource.k8s_workloads.pods.length})
                  </h3>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50">
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Status</th>
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Pod</th>
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Namespace</th>
                      </tr>
                    </thead>
                    <tbody>
                      {resource.k8s_workloads.pods.map((pod) => (
                        <tr key={`${pod.namespace}-${pod.name}`} className="border-b border-border/30">
                          <td className="px-4 py-2">
                            <StatusBadge status={pod.status === "Running" ? "Running" : pod.status === "Failed" ? "Error" : "Unknown"} />
                          </td>
                          <td className="px-4 py-2 text-foreground">{pod.name}</td>
                          <td className="px-4 py-2 text-muted-foreground">{pod.namespace}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {resource.k8s_workloads.deployments.length > 0 && (
                <div>
                  <h3 className="text-xs font-medium text-muted-foreground px-4 py-2 bg-muted/50 border-t border-border">
                    Deployments ({resource.k8s_workloads.deployments.length})
                  </h3>
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border/50">
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Name</th>
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Namespace</th>
                        <th className="text-left px-4 py-2 font-medium text-muted-foreground">Replicas</th>
                      </tr>
                    </thead>
                    <tbody>
                      {resource.k8s_workloads.deployments.map((dep) => (
                        <tr key={`${dep.namespace}-${dep.name}`} className="border-b border-border/30">
                          <td className="px-4 py-2 text-foreground">{dep.name}</td>
                          <td className="px-4 py-2 text-muted-foreground">{dep.namespace}</td>
                          <td className="px-4 py-2 text-muted-foreground">{dep.replicas}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          </Section>
        )}

        {/* Active Alerts */}
        {resource.alerts && resource.alerts.length > 0 && (
          <Section title="Active Alerts">
            <div className="rounded-lg border border-border bg-card divide-y divide-border/50">
              {resource.alerts.map((alert, idx) => (
                <div key={idx} className="flex items-center gap-3 px-4 py-3">
                  <span className={`h-2 w-2 rounded-full ${
                    alert.state === "alerting" || alert.state === "critical" ? "bg-red-500" : "bg-yellow-500"
                  }`} />
                  <div className="flex-1">
                    <p className="text-sm text-foreground">{alert.title}</p>
                    <p className="text-xs text-muted-foreground">
                      {alert.source} — {alert.triggered_at}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Recent Incidents */}
        {resource.incidents && resource.incidents.length > 0 && (
          <Section title="Recent Incidents">
            <div className="rounded-lg border border-border bg-card divide-y divide-border/50">
              {resource.incidents.map((incident) => (
                <div key={incident.id} className="flex items-center justify-between px-4 py-3">
                  <div>
                    <p className="text-sm text-foreground">
                      #{incident.id} {incident.title}
                    </p>
                    <p className="text-xs text-muted-foreground">{incident.created_at}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-2 py-0.5 rounded font-medium ${
                      incident.severity === "critical" ? "bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-200" :
                      incident.severity === "high" ? "bg-orange-100 text-orange-700 dark:bg-orange-900 dark:text-orange-200" :
                      "bg-yellow-100 text-yellow-700 dark:bg-yellow-900 dark:text-yellow-200"
                    }`}>
                      {incident.severity}
                    </span>
                    <span className="text-xs text-muted-foreground capitalize">{incident.status}</span>
                  </div>
                </div>
              ))}
            </div>
          </Section>
        )}
      </div>
    </div>
  );
}
