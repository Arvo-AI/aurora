"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { formatDistanceToNow, parseISO } from "date-fns";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
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
import { AlertCircle, Loader2, LogOut } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";

interface NotionStatus {
  connected: boolean;
  oauthConfigured: boolean;
  workspaceName?: string | null;
  botName?: string | null;
  authType?: "oauth" | "iit" | string | null;
  connectedAt?: string | null;
}

interface NotionProviderIntegrationProps {
  onDisconnect: () => void;
}

function formatAuthType(authType: NotionStatus["authType"]): string {
  if (authType === "oauth") return "OAuth";
  if (authType === "iit") return "Integration Token";
  return "Unknown";
}

function parseConnectedAt(value: string | null | undefined): Date | null {
  if (!value) return null;
  try {
    const parsed = parseISO(value);
    if (Number.isNaN(parsed.getTime())) {
      const fallback = new Date(value);
      return Number.isNaN(fallback.getTime()) ? null : fallback;
    }
    return parsed;
  } catch {
    const fallback = new Date(value);
    return Number.isNaN(fallback.getTime()) ? null : fallback;
  }
}

function formatConnectedAt(value: string | null | undefined): {
  absolute: string;
  relative: string;
} | null {
  const date = parseConnectedAt(value);
  if (!date) return null;
  const absolute = new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
  let relative = "";
  try {
    relative = formatDistanceToNow(date, { addSuffix: true });
  } catch {
    relative = "";
  }
  return { absolute, relative };
}

export default function NotionProviderIntegration({
  onDisconnect,
}: NotionProviderIntegrationProps) {
  const { toast } = useToast();
  const [status, setStatus] = useState<NotionStatus | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [isDisconnecting, setIsDisconnecting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setIsLoading(true);
      setLoadError(null);
      try {
        const response = await fetch("/api/notion/status", {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(`status ${response.status}`);
        }
        const data = (await response.json()) as Partial<NotionStatus>;
        if (cancelled) return;
        setStatus({
          connected: Boolean(data.connected),
          oauthConfigured: Boolean(data.oauthConfigured),
          workspaceName: data.workspaceName ?? null,
          botName: data.botName ?? null,
          authType: (data.authType as NotionStatus["authType"]) ?? null,
          connectedAt: data.connectedAt ?? null,
        });
      } catch (error) {
        if (cancelled) return;
        const message =
          error instanceof Error && error.message
            ? error.message
            : "Failed to load Notion status.";
        setLoadError(message);
      } finally {
        if (!cancelled) setIsLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  const handleDisconnect = async () => {
    setIsDisconnecting(true);
    try {
      const response = await fetch("/api/notion/disconnect", {
        method: "DELETE",
        credentials: "include",
      });
      if (!response.ok) {
        const text = await response.text();
        let message = "Failed to disconnect Notion.";
        if (text) {
          try {
            const parsed = JSON.parse(text) as { error?: string };
            if (parsed?.error) message = parsed.error;
          } catch {
            // ignore — keep default
          }
        }
        throw new Error(message);
      }

      toast({
        title: "Notion disconnected",
        description: "You can reconnect anytime from the connectors page.",
      });

      void fetchConnectedAccounts(true).catch(() => {});
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }

      setConfirmOpen(false);
      onDisconnect();
    } catch (error) {
      const message =
        error instanceof Error && error.message
          ? error.message
          : "Failed to disconnect Notion.";
      toast({
        title: "Failed to disconnect",
        description: message,
        variant: "destructive",
      });
    } finally {
      setIsDisconnecting(false);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-4">
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-2 flex-1">
            <Skeleton className="h-6 w-48" />
            <Skeleton className="h-4 w-3/4" />
          </div>
          <Skeleton className="h-9 w-28" />
        </div>
        <div className="rounded-lg border bg-muted/50 p-4 space-y-2">
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      </div>
    );
  }

  if (loadError) {
    return (
      <Alert variant="destructive">
        <AlertCircle className="h-4 w-4" />
        <AlertTitle>Failed to load Notion status</AlertTitle>
        <AlertDescription className="text-sm">{loadError}</AlertDescription>
      </Alert>
    );
  }

  if (!status?.connected) {
    return (
      <Alert>
        <AlertCircle className="h-4 w-4" />
        <AlertTitle>Notion not connected</AlertTitle>
        <AlertDescription className="text-sm space-y-2">
          <p>
            Aurora isn&apos;t currently connected to a Notion workspace.
          </p>
          <Button asChild size="sm" variant="outline">
            <Link href="/notion/connect">Connect Notion</Link>
          </Button>
        </AlertDescription>
      </Alert>
    );
  }

  const workspaceName = status?.workspaceName?.trim() || "Notion workspace";
  const botName = status?.botName?.trim() || null;
  const connectedAt = formatConnectedAt(status?.connectedAt);

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-lg font-semibold">{workspaceName}</h3>
            {status?.authType && (
              <Badge variant="secondary" className="text-xs">
                {formatAuthType(status.authType)}
              </Badge>
            )}
          </div>
          <p className="text-sm text-muted-foreground">
            Aurora can search this workspace and export postmortems and action
            items to it.
          </p>
        </div>
        <Button
          variant="destructive"
          size="sm"
          onClick={() => setConfirmOpen(true)}
          disabled={isDisconnecting}
        >
          <LogOut className="h-4 w-4 mr-2" />
          Disconnect
        </Button>
      </div>

      <div className="rounded-lg border bg-muted/50 p-4 text-sm space-y-2">
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">Workspace</span>
          <span className="font-medium truncate">{workspaceName}</span>
        </div>
        {botName && (
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">Integration</span>
            <span className="font-medium truncate">{botName}</span>
          </div>
        )}
        <div className="flex items-center justify-between gap-4">
          <span className="text-muted-foreground">Authentication</span>
          <span className="font-medium">
            {formatAuthType(status?.authType)}
          </span>
        </div>
        {connectedAt && (
          <div className="flex items-center justify-between gap-4">
            <span className="text-muted-foreground">Connected</span>
            <span
              className="font-medium"
              title={connectedAt.absolute}
            >
              {connectedAt.relative
                ? `${connectedAt.relative} (${connectedAt.absolute})`
                : connectedAt.absolute}
            </span>
          </div>
        )}
      </div>

      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Disconnect Notion?</AlertDialogTitle>
            <AlertDialogDescription>
              You can reconnect anytime. Aurora will stop searching and writing
              to this workspace until it&apos;s reconnected.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDisconnecting}>
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(event) => {
                event.preventDefault();
                void handleDisconnect();
              }}
              disabled={isDisconnecting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDisconnecting ? (
                <>
                  <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                  Disconnecting...
                </>
              ) : (
                "Disconnect"
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
