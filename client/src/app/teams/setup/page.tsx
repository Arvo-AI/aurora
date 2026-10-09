"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { useToast } from "@/hooks/use-toast";
import { teamsService } from "@/lib/services/teams";
import { apiErrorMessage } from "@/lib/services/api-client";
import {
  TEAMS_CUSTOMER_SETUP,
  TEAMS_SETUP_DOCS_URL,
  teamsOAuthFailureMessage,
} from "@/lib/chat-platform/teams-setup";
import { TeamsSetupIncompleteBanner } from "@/components/chat-platform/TeamsSetupIncompleteBanner";
import type { TeamsStatus } from "@/lib/services/teams";

function TeamsSetupContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { toast } = useToast();
  const [isConnecting, setIsConnecting] = useState(false);
  const [redirectUriHint, setRedirectUriHint] = useState<string | null>(null);
  const [teamsStatus, setTeamsStatus] = useState<TeamsStatus | null>(null);
  const oauthFailureHandled = useRef(false);

  useEffect(() => {
    void teamsService.getStatus().then(setTeamsStatus);
  }, []);

  useEffect(() => {
    if (oauthFailureHandled.current) return;
    if (searchParams.get("teams_auth") !== "failed") return;
    oauthFailureHandled.current = true;
    const error = searchParams.get("error") || "unknown";
    const uriFromCallback = searchParams.get("redirect_uri");
    const msDetail = searchParams.get("error_description");
    if (uriFromCallback) {
      setRedirectUriHint(uriFromCallback);
    }
    const baseMessage = teamsOAuthFailureMessage(
      error,
      uriFromCallback ?? redirectUriHint ?? undefined,
      msDetail ?? undefined,
    );
    toast({
      title: "Microsoft Teams not connected",
      description:
        baseMessage.includes("TEAMS_TENANT_ID") || !msDetail
          ? baseMessage
          : `${baseMessage} Microsoft: ${msDetail}`,
      variant: "destructive",
      duration: 16_000,
    });
    if (globalThis.window !== undefined) {
      const url = new URL(globalThis.window.location.href);
      url.searchParams.delete("teams_auth");
      url.searchParams.delete("error");
      url.searchParams.delete("redirect_uri");
      url.searchParams.delete("error_description");
      globalThis.window.history.replaceState({}, "", url.pathname + url.search);
    }
  }, [searchParams, toast, redirectUriHint]);

  const handleConnect = useCallback(async () => {
    setIsConnecting(true);
    try {
      const response = await teamsService.connect();
      if (response.redirect_uri) {
        setRedirectUriHint(response.redirect_uri);
      }
      if (response.oauth_url) {
        window.location.href = response.oauth_url;
        return;
      }
      throw new Error("No OAuth URL received");
    } catch (error: unknown) {
      console.error("Teams connect error:", error);
      toast({
        title: "Connection Failed",
        description: apiErrorMessage(error, "Failed to connect Microsoft Teams"),
        variant: "destructive",
        duration: 12_000,
      });
      setIsConnecting(false);
    }
  }, [toast]);

  const copy = TEAMS_CUSTOMER_SETUP;

  return (
    <div className="min-h-screen bg-black text-white p-6 sm:p-8">
      <div className="max-w-2xl mx-auto space-y-6">
        <Button
          variant="ghost"
          size="sm"
          className="text-zinc-400 hover:text-white -ml-2"
          onClick={() => router.push("/connectors")}
        >
          <ArrowLeft className="h-4 w-4 mr-2" />
          Back to Connectors
        </Button>

          <div>
            <h1 className="text-2xl font-bold">Set up Microsoft Teams</h1>
            <p className="text-sm text-zinc-400 mt-1">{copy.summary}</p>
            <p className="text-xs text-amber-500/90 mt-2">{copy.orderNote}</p>
          </div>

          {teamsStatus?.connected && <TeamsSetupIncompleteBanner status={teamsStatus} />}

          <Card className="border-zinc-800 bg-zinc-950">
            <CardHeader>
              <CardTitle className="text-lg">{copy.oauthTitle}</CardTitle>
            <CardDescription>{copy.oauthWhy}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ol className="list-decimal list-inside space-y-2 text-sm text-zinc-300">
              {copy.oauthSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            {redirectUriHint && (
              <p className="text-xs text-zinc-500 break-all">
                Entra redirect URI must include:{" "}
                <span className="text-zinc-300 font-mono">{redirectUriHint}</span>
              </p>
            )}
              {teamsStatus?.connected ? (
                <p className="text-sm text-green-500 flex items-center gap-2">
                  <span className="inline-block h-2 w-2 rounded-full bg-green-500" />
                  Microsoft sign-in complete — continue with steps 2 and 3 below.
                </p>
              ) : (
                <Button onClick={handleConnect} disabled={isConnecting} className="w-full sm:w-auto">
                  {isConnecting ? (
                    <>
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                      Redirecting to Microsoft…
                    </>
                  ) : (
                    "Connect Microsoft Teams"
                  )}
                </Button>
              )}
          </CardContent>
        </Card>

        <Card className="border-zinc-800 bg-zinc-950">
          <CardHeader>
            <CardTitle className="text-lg">{copy.installTitle}</CardTitle>
            <CardDescription>{copy.installWhy}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3 text-sm text-zinc-300">
            <p className="text-xs text-zinc-500">{copy.installNote}</p>
            <ol className="list-decimal list-inside space-y-2">
              {copy.installSteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
          </CardContent>
        </Card>

        <Card className="border-zinc-800 bg-zinc-950">
          <CardHeader>
            <CardTitle className="text-lg">{copy.verifyTitle}</CardTitle>
            <CardDescription>{copy.verifyWhy}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <ol className="list-decimal list-inside space-y-2 text-sm text-zinc-300">
              {copy.verifySteps.map((step) => (
                <li key={step}>{step}</li>
              ))}
            </ol>
            <p className="text-xs text-zinc-500">
              Manage (channels, bot check) is available after you complete step 1.
            </p>
          </CardContent>
        </Card>

          <p className="text-xs text-zinc-500">
            Deploying or hosting Aurora (Azure Bot, Entra, Teams Developer Portal)? That is platform
            operator setup — see the{" "}
            <a
              href={TEAMS_SETUP_DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary hover:underline inline-flex items-center gap-1"
            >
              operator connector guide
              <ExternalLink className="h-3 w-3" />
            </a>
            , not this page.
          </p>
      </div>
    </div>
  );
}

export default function TeamsSetupPage() {
  return (
    <ConnectorAuthGuard connectorName="Microsoft Teams">
      <Suspense
        fallback={
          <div className="min-h-screen bg-black text-white flex items-center justify-center">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </div>
        }
      >
        <TeamsSetupContent />
      </Suspense>
    </ConnectorAuthGuard>
  );
}
