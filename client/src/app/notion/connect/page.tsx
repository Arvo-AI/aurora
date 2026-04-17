"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/hooks/use-toast";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { AlertCircle, CheckCircle2, Loader2, ShieldCheck } from "lucide-react";
import { fetchConnectedAccounts } from "@/lib/connected-accounts-cache";
import { NotionIntegrationTokenForm } from "@/components/connectors/NotionIntegrationTokenForm";

interface NotionStatus {
  connected: boolean;
  oauthConfigured: boolean;
  workspaceName?: string | null;
  authType?: "oauth" | "iit" | string | null;
}

const POPUP_WIDTH = 600;
const POPUP_HEIGHT = 720;

export default function NotionConnectPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [status, setStatus] = useState<NotionStatus | null>(null);
  const [statusLoading, setStatusLoading] = useState(true);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [oauthLoading, setOauthLoading] = useState(false);
  const [oauthError, setOauthError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"oauth" | "iit">("iit");
  const popupRef = useRef<Window | null>(null);
  const popupPollRef = useRef<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const loadStatus = async () => {
      try {
        const response = await fetch("/api/notion/status", {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(`status ${response.status}`);
        }
        const data = (await response.json()) as Partial<NotionStatus>;
        if (cancelled) return;
        const next: NotionStatus = {
          connected: Boolean(data.connected),
          oauthConfigured: Boolean(data.oauthConfigured),
          workspaceName: data.workspaceName ?? null,
          authType: (data.authType as NotionStatus["authType"]) ?? null,
        };
        setStatus(next);
        // Default tab: prefer IIT (lower friction first-run). If OAuth is
        // configured AND the user is already connected via OAuth, show
        // OAuth tab (defensive default — they rarely land here in that
        // state since the connectors page routes them to the management
        // view instead).
        if (next.oauthConfigured && next.connected && next.authType === "oauth") {
          setActiveTab("oauth");
        } else {
          setActiveTab("iit");
        }
      } catch (error) {
        if (cancelled) return;
        console.error("Failed to fetch Notion status", error);
        setStatus({ connected: false, oauthConfigured: false });
        setStatusError(
          error instanceof Error && error.message
            ? error.message
            : "Failed to load Notion status.",
        );
        setActiveTab("iit");
      } finally {
        if (!cancelled) setStatusLoading(false);
      }
    };
    void loadStatus();
    return () => {
      cancelled = true;
    };
  }, []);

  const stopPopupPoll = useCallback(() => {
    if (popupPollRef.current !== null) {
      window.clearInterval(popupPollRef.current);
      popupPollRef.current = null;
    }
  }, []);

  useEffect(() => {
    return () => {
      stopPopupPoll();
    };
  }, [stopPopupPoll]);

  const finishOAuth = useCallback(
    async (code: string, state: string) => {
      try {
        const response = await fetch("/api/notion/oauth/callback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          credentials: "include",
          body: JSON.stringify({ code, state }),
        });
        const text = await response.text();
        let payload: unknown = null;
        if (text) {
          try {
            payload = JSON.parse(text);
          } catch {
            payload = null;
          }
        }
        if (!response.ok) {
          const errorMessage =
            payload &&
            typeof payload === "object" &&
            "error" in payload &&
            typeof (payload as { error: unknown }).error === "string"
              ? (payload as { error: string }).error
              : "Notion sign-in failed.";
          throw new Error(errorMessage);
        }
        const successPayload =
          payload && typeof payload === "object"
            ? (payload as { workspaceName?: string; workspace_name?: string })
            : {};
        const workspaceName =
          successPayload.workspaceName ?? successPayload.workspace_name ?? null;

        toast({
          title: "Notion connected",
          description: workspaceName
            ? `Connected to ${workspaceName}.`
            : "You can now search your workspace from Aurora.",
        });

        void fetchConnectedAccounts(true).catch(() => {});
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        router.push("/connectors");
      } catch (error) {
        const message =
          error instanceof Error && error.message
            ? error.message
            : "Notion sign-in failed.";
        setOauthError(message);
        toast({
          title: "Failed to connect Notion",
          description: message,
          variant: "destructive",
        });
      } finally {
        setOauthLoading(false);
      }
    },
    [router, toast],
  );

  useEffect(() => {
    const handleMessage = (event: MessageEvent) => {
      // Cross-origin guard: only trust messages from our own origin.
      if (event.origin !== window.location.origin) return;
      const data = event.data as
        | {
            type?: string;
            code?: string;
            state?: string;
            error?: string;
            errorDescription?: string;
          }
        | null;
      if (!data || typeof data !== "object") return;

      if (data.type === "notion-auth-success" && data.code && data.state) {
        stopPopupPoll();
        void finishOAuth(data.code, data.state);
      } else if (data.type === "notion-auth-error") {
        stopPopupPoll();
        setOauthLoading(false);
        const message =
          data.errorDescription || data.error || "Please try again.";
        setOauthError(message);
        toast({
          title: "Notion sign-in cancelled",
          description: message,
          variant: "destructive",
        });
      }
    };
    window.addEventListener("message", handleMessage);
    return () => window.removeEventListener("message", handleMessage);
  }, [finishOAuth, stopPopupPoll, toast]);

  const handleOAuthConnect = async () => {
    setOauthLoading(true);
    setOauthError(null);
    try {
      const response = await fetch("/api/notion/connect", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
      });
      if (!response.ok) {
        const text = await response.text();
        let message = `Sign-in request failed with status ${response.status}`;
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
      const data = (await response.json()) as { authUrl?: string };
      if (!data.authUrl) {
        throw new Error("No authorization URL returned by Aurora.");
      }

      const left =
        typeof window !== "undefined"
          ? Math.max(0, window.screenX + (window.outerWidth - POPUP_WIDTH) / 2)
          : 0;
      const top =
        typeof window !== "undefined"
          ? Math.max(0, window.screenY + (window.outerHeight - POPUP_HEIGHT) / 2)
          : 0;
      const features = `width=${POPUP_WIDTH},height=${POPUP_HEIGHT},left=${left},top=${top},popup=yes`;
      const popup = window.open(data.authUrl, "notion-oauth", features);
      if (!popup) {
        throw new Error("Popup blocked — allow popups for this site and try again.");
      }
      popupRef.current = popup;
      stopPopupPoll();
      popupPollRef.current = window.setInterval(() => {
        if (popupRef.current && popupRef.current.closed) {
          stopPopupPoll();
          setOauthLoading((prev) => {
            if (prev) {
              toast({
                title: "Notion sign-in cancelled",
                description: "The sign-in window was closed before authorizing.",
              });
            }
            return false;
          });
        }
      }, 500);
    } catch (error) {
      const message =
        error instanceof Error && error.message
          ? error.message
          : "Unable to start Notion sign-in.";
      setOauthError(message);
      toast({
        title: "Failed to connect Notion",
        description: message,
        variant: "destructive",
      });
      setOauthLoading(false);
    }
  };

  const handleIitSuccess = () => {
    router.push("/connectors");
  };

  const oauthConfigured = Boolean(status?.oauthConfigured);
  const connected = Boolean(status?.connected);
  const showTabs = statusLoading || oauthConfigured;

  const renderOAuthPanel = () => (
    <div className="space-y-4">
      <div className="space-y-2 text-sm text-muted-foreground">
        <p>
          Sign in with your Notion account. Aurora will use your OAuth
          consent for ACL-aware access — you&apos;ll only see pages and
          databases you&apos;re already allowed to see.
        </p>
        <p>
          A popup will open for Notion to confirm the workspace and the
          pages you want to share.
        </p>
      </div>
      {oauthError && (
        <Alert variant="destructive">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Failed to start sign-in</AlertTitle>
          <AlertDescription className="text-sm">{oauthError}</AlertDescription>
        </Alert>
      )}
      <Button
        className="w-full"
        onClick={handleOAuthConnect}
        disabled={oauthLoading || statusLoading}
      >
        {oauthLoading ? (
          <>
            <Loader2 className="h-4 w-4 mr-2 animate-spin" />
            Waiting for Notion...
          </>
        ) : (
          "Connect with Notion"
        )}
      </Button>
      <div className="flex items-start gap-2.5 p-3 rounded-lg bg-muted/50 text-xs">
        <ShieldCheck className="h-4 w-4 text-green-600 dark:text-green-500 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <p className="font-medium">Your consent is encrypted at rest</p>
          <p className="text-muted-foreground">
            Aurora stores your Notion OAuth tokens encrypted in its
            secrets vault, and you can disconnect anytime.
          </p>
        </div>
      </div>
    </div>
  );

  return (
    <div className="container mx-auto py-8 px-4 max-w-3xl">
      <div className="flex items-center gap-4 mb-8">
        <div className="p-2 rounded-xl shadow-sm border overflow-hidden bg-white dark:bg-white">
          <img
            src="/notion.svg"
            alt="Notion"
            className="h-9 w-9 object-contain rounded-md text-foreground"
          />
        </div>
        <div>
          <h1 className="text-2xl font-bold tracking-tight">Notion</h1>
          <p className="text-muted-foreground text-sm">
            Export postmortems, search workspace docs, and let Aurora create
            runbooks and action-item rows in your Notion workspace.
          </p>
        </div>
      </div>

      {statusError && !statusLoading && (
        <Alert variant="destructive" className="mb-4">
          <AlertCircle className="h-4 w-4" />
          <AlertTitle>Could not load Notion status</AlertTitle>
          <AlertDescription className="text-sm">
            {statusError} — you can still try connecting below.
          </AlertDescription>
        </Alert>
      )}

      {connected && !statusLoading ? (
        <Card className="border-green-600/40 dark:border-green-500/40">
          <CardContent className="flex items-start gap-3 py-5">
            <CheckCircle2 className="h-5 w-5 text-green-600 dark:text-green-500 mt-0.5 shrink-0" />
            <div className="flex-1 space-y-1">
              <p className="text-sm font-medium">
                Already connected to{" "}
                {status?.workspaceName?.trim() || "your Notion workspace"}
              </p>
              <p className="text-xs text-muted-foreground">
                Manage or disconnect from the connectors page. Disconnect first
                if you want to reconnect with a different token or workspace.
              </p>
            </div>
            <Button
              size="sm"
              variant="outline"
              onClick={() => router.push("/connectors")}
            >
              Go to connectors
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Connect Your Notion Workspace</CardTitle>
            <CardDescription>
              {showTabs
                ? "Choose how Aurora should authenticate with Notion. You can always switch later by disconnecting and reconnecting."
                : "Aurora authenticates with your Notion workspace via an Internal Integration Token."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            {showTabs ? (
              <Tabs
                value={activeTab}
                onValueChange={(value) => setActiveTab(value as "oauth" | "iit")}
                className="w-full"
              >
                <TabsList className="grid w-full grid-cols-2">
                  <TabsTrigger value="iit">Use an Integration Token</TabsTrigger>
                  <TabsTrigger value="oauth" disabled={statusLoading}>
                    Sign in with Notion
                  </TabsTrigger>
                </TabsList>

                <TabsContent value="iit" className="mt-6">
                  <NotionIntegrationTokenForm onSuccess={handleIitSuccess} />
                </TabsContent>

                <TabsContent value="oauth" className="mt-6">
                  {renderOAuthPanel()}
                </TabsContent>
              </Tabs>
            ) : (
              <div className="space-y-4">
                <h3 className="text-sm font-medium">Integration Token</h3>
                <NotionIntegrationTokenForm onSuccess={handleIitSuccess} />
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
