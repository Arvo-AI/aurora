"use client";

import type { ReactNode } from "react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  AlertCircle,
  Loader2,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { ProjectCache } from "@/components/cloud-provider/projects/projectUtils";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";
import { GcpServiceAccountForm } from "@/components/connectors/GcpServiceAccountForm";

interface ServiceAccount {
  account_id: string;
  account_alias: string | null;
  project_id: string | null;
  accessible_project_ids: string[];
  // Free-form so the backend can introduce new values without breaking the UI.
  visibility: string;
  status: string;
  last_verified_at: string | null;
}

interface ListResponse {
  service_accounts: ServiceAccount[];
}

function formatTimestamp(value: string | null): string {
  if (!value) return "—";
  try {
    return new Date(value).toLocaleString();
  } catch {
    return value;
  }
}

function dispatchProviderChanged() {
  if (globalThis.window === undefined) return;
  globalThis.window.dispatchEvent(new CustomEvent("providerStateChanged"));
  fetchConnectedAccounts(true).catch(() => {});
  ProjectCache.invalidate("gcp");
}

export default function GcpAuthPage() {
  const { toast } = useToast();

  const [serviceAccounts, setServiceAccounts] = useState<ServiceAccount[]>([]);
  const [loading, setLoading] = useState(true);
  const [pendingAction, setPendingAction] = useState<string | null>(null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);

  const loadServiceAccounts = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/proxy/gcp/service-accounts", {
        credentials: "include",
      });
      if (!res.ok) {
        throw new Error(`Request failed (${res.status})`);
      }
      const data = (await res.json()) as ListResponse;
      setServiceAccounts(Array.isArray(data.service_accounts) ? data.service_accounts : []);
    } catch (err) {
      const message = err instanceof Error ? err.message : "Failed to load service accounts";
      setError(message);
      setServiceAccounts([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadServiceAccounts().catch(() => {});
  }, [loadServiceAccounts]);

  // Disconnect deletes the Vault secret, so inactive rows cannot be
  // reconnected without re-uploading the SA key. The backend already filters
  // them out of the list response; this is just a defensive guard.
  const active = useMemo(
    () => serviceAccounts.filter((sa) => sa.status === "active"),
    [serviceAccounts],
  );

  const toggleProjects = (accountId: string) => {
    setExpandedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });
  };

  const handleDelete = async (sa: ServiceAccount) => {
    setPendingAction(`delete:${sa.account_id}`);
    try {
      const res = await fetch(
        `/api/proxy/gcp/service-accounts/${encodeURIComponent(sa.account_id)}`,
        { method: "DELETE", credentials: "include" },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error || `Failed (${res.status})`);
      }
      toast({ title: "Disconnected", description: sa.account_id });
      dispatchProviderChanged();
      await loadServiceAccounts();
    } catch (err) {
      toast({
        title: "Failed to disconnect",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setPendingAction(null);
    }
  };

  const handleDisconnectAll = async () => {
    if (active.length === 0) return;
    const confirmed = globalThis.window === undefined
      ? true
      : globalThis.confirm(`Disconnect all ${active.length} GCP service account(s)?`);
    if (!confirmed) return;

    setPendingAction("disconnect-all");
    try {
      const res = await fetch("/api/proxy/gcp/service-accounts/disconnect-all", {
        method: "POST",
        credentials: "include",
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.error || `Failed (${res.status})`);
      }
      toast({ title: "Disconnected all GCP service accounts" });
      dispatchProviderChanged();
      await loadServiceAccounts();
    } catch (err) {
      toast({
        title: "Failed to disconnect all",
        description: err instanceof Error ? err.message : "Unknown error",
        variant: "destructive",
      });
    } finally {
      setPendingAction(null);
    }
  };

  const handleFormSuccess = async () => {
    dispatchProviderChanged();
    await loadServiceAccounts();
  };

  let saListContent: ReactNode;
  if (loading) {
    saListContent = (
      <div className="flex items-center justify-center py-8">
        <Loader2 className="h-6 w-6 animate-spin text-primary" />
      </div>
    );
  } else if (active.length === 0) {
    saListContent = (
      <p className="text-sm text-muted-foreground py-4">
        No active service accounts yet. Add one below.
      </p>
    );
  } else {
    saListContent = (
      <div className="divide-y rounded-md border">
        {active.map((sa) => {
          const isExpanded = expandedProjects.has(sa.account_id);
          const accessibleCount = sa.accessible_project_ids?.length ?? 0;
          return (
            <div key={sa.account_id} className="p-4 space-y-2">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="font-medium text-sm truncate">
                    {sa.account_alias || sa.account_id}
                  </div>
                  {sa.account_alias && (
                    <div className="text-xs text-muted-foreground truncate">
                      {sa.account_id}
                    </div>
                  )}
                  <div className="flex flex-wrap items-center gap-2 mt-1.5">
                    {sa.project_id && (
                      <Badge variant="secondary" className="text-xs font-mono">
                        {sa.project_id}
                      </Badge>
                    )}
                    <Badge
                      variant={sa.visibility === "org" ? "default" : "outline"}
                      className="text-xs"
                    >
                      {sa.visibility === "org" ? "Org" : "Private"}
                    </Badge>
                    <button
                      type="button"
                      onClick={() => toggleProjects(sa.account_id)}
                      className="text-xs underline text-muted-foreground hover:text-foreground"
                    >
                      {accessibleCount} project{accessibleCount === 1 ? "" : "s"}
                    </button>
                    <span className="text-xs text-muted-foreground">
                      Verified {formatTimestamp(sa.last_verified_at)}
                    </span>
                  </div>
                  {isExpanded && accessibleCount > 0 && (
                    <ul className="mt-2 text-xs text-muted-foreground font-mono space-y-0.5 max-h-40 overflow-auto">
                      {sa.accessible_project_ids.map((pid) => (
                        <li key={pid}>{pid}</li>
                      ))}
                    </ul>
                  )}
                </div>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => handleDelete(sa)}
                  disabled={pendingAction === `delete:${sa.account_id}`}
                >
                  {pendingAction === `delete:${sa.account_id}` ? (
                    <Loader2 className="h-4 w-4 animate-spin" />
                  ) : (
                    <Trash2 className="h-4 w-4" />
                  )}
                </Button>
              </div>
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-4xl">
      <div className="flex items-center gap-4 mb-8">
        <div className="p-2 rounded-xl shadow-sm border overflow-hidden">
          <img
            src="/google-cloud-svgrepo-com.svg"
            alt="Google Cloud"
            className="h-9 w-9 object-contain rounded-md"
          />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Google Cloud</h1>
          <p className="text-muted-foreground text-sm">
            Manage the service accounts Aurora uses to access your Google Cloud
            projects.
          </p>
        </div>
      </div>

      <Card className="mb-6">
        <CardHeader className="flex flex-row items-start justify-between gap-3 space-y-0">
          <div>
            <CardTitle className="text-lg">Connected Service Accounts</CardTitle>
            <CardDescription>
              {active.length} active
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => loadServiceAccounts()}
              disabled={loading}
            >
              <RefreshCw className={`h-4 w-4 mr-2 ${loading ? "animate-spin" : ""}`} />
              Refresh
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={handleDisconnectAll}
              disabled={active.length === 0 || pendingAction === "disconnect-all"}
            >
              {pendingAction === "disconnect-all" ? (
                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
              ) : (
                <Trash2 className="h-4 w-4 mr-2" />
              )}
              Disconnect All
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {error && (
            <div className="flex items-center gap-2 text-sm text-destructive mb-4">
              <AlertCircle className="h-4 w-4" />
              {error}
            </div>
          )}
          {saListContent}
        </CardContent>
      </Card>

      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="text-lg">Add a Service Account</CardTitle>
          <CardDescription>
            Upload or paste a Google Cloud service account JSON key. Aurora will
            verify it has access to the project before storing it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <GcpServiceAccountForm onSuccess={handleFormSuccess} />
        </CardContent>
      </Card>
    </div>
  );
}
