"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Loader2,
  CheckCircle,
  AlertCircle,
  Copy,
  ExternalLink,
} from "lucide-react";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { googleChatService } from "@/lib/services/google-chat";
import { copyToClipboard } from "@/lib/utils";
import { useToast } from "@/hooks/use-toast";

interface EnvCheckResult {
  configured: boolean;
  hasClientId: boolean;
  hasClientSecret: boolean;
  hasServiceAccount: boolean;
  baseUrl: string;
}

const DOCS_URL = "https://arvo-ai.github.io/aurora/docs/integrations/connectors#google-chat";

export default function GoogleChatSetupPage() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const { toast } = useToast();
  const [envCheck, setEnvCheck] = useState<EnvCheckResult | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isConnecting, setIsConnecting] = useState(false);
  const [copySuccess, setCopySuccess] = useState<Record<string, boolean>>({});

  const handleCopy = (text: string, key: string) => {
    copyToClipboard(text);
    setCopySuccess((prev) => ({ ...prev, [key]: true }));
    setTimeout(
      () => setCopySuccess((prev) => ({ ...prev, [key]: false })),
      2000
    );
  };

  const checkEnv = async () => {
    setIsLoading(true);
    try {
      const res = await fetch("/api/google-chat/env/check", {
        credentials: "include",
      });
      if (res.ok) {
        const data: EnvCheckResult = await res.json();
        setEnvCheck(data);
      } else {
        toast({
          title: "Status Check Failed",
          description: "Could not verify environment configuration. The setup guide may be incomplete.",
          variant: "destructive",
        });
      }
    } catch (err) {
      console.error("Failed to check Google Chat env:", err);
      toast({
        title: "Connection Error",
        description: "Could not reach the server to check configuration status.",
        variant: "destructive",
      });
    } finally {
      setIsLoading(false);
    }
  };

  useEffect(() => {
    checkEnv();
  }, []);

  // Handle OAuth callback results (success/error query params)
  useEffect(() => {
    const success = searchParams.get("success");
    const error = searchParams.get("error");

    if (success === "true") {
      toast({
        title: "Google Chat Connected",
        description: "Incidents space is ready. Notifications will appear as Aurora.",
      });
      if (typeof window !== "undefined") {
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
      router.push("/connectors");
      return;
    }

    if (error) {
      const errorMessages: Record<string, string> = {
        missing_params: "OAuth callback is missing required parameters.",
        no_access_token: "Failed to get access token from Google.",
        space_creation_failed: "Failed to create the incidents space.",
        space_not_resolved: "Could not find or create the incidents space.",
        setup_failed: "Google Chat setup failed unexpectedly.",
        insufficient_permissions: "You don't have permission to create Google Chat spaces. Ask your Workspace admin to allow space creation or to connect Aurora.",
        callback_failed: "OAuth callback failed unexpectedly.",
        access_denied: "You denied access. Please try again.",
        invalid_state: "OAuth session expired or was tampered with. Please try again.",
        oauth_error: "Google returned an error during authorization. Please try again.",
        app_install_failed: "Failed to add the Aurora bot to the space. Check that the Chat app is properly configured in your Google Cloud project.",
      };
      toast({
        title: "Connection Failed",
        description: errorMessages[error] || `Google Chat setup error: ${error}`,
        variant: "destructive",
      });
    }
  }, [searchParams, toast, router]);

  const handleConnect = useCallback(async () => {
    setIsConnecting(true);
    try {
      const response = await googleChatService.connect();
      if (response.oauth_url) {
        if (!response.oauth_url.startsWith("https://accounts.google.com")) {
          toast({
            title: "Connection Failed",
            description: "Received an unexpected OAuth URL. Please contact your administrator.",
            variant: "destructive",
          });
          setIsConnecting(false);
          return;
        }
        window.location.href = response.oauth_url;
      } else if (response.error) {
        toast({
          title: "Connection Failed",
          description: response.error,
          variant: "destructive",
        });
        setIsConnecting(false);
      }
    } catch (error: any) {
      console.error("Connect error:", error);
      toast({
        title: "Connection Failed",
        description: error.message || "Failed to connect Google Chat",
        variant: "destructive",
      });
      setIsConnecting(false);
    }
  }, [toast]);

  // Auto-trigger OAuth when env is configured (skip if returning from an error)
  useEffect(() => {
    const hasError = searchParams.get("error");
    const hasSuccess = searchParams.get("success");
    if (!isLoading && envCheck?.configured && !hasError && !hasSuccess) {
      handleConnect();
    }
  }, [isLoading, envCheck, handleConnect, searchParams]);

  if (isLoading || isConnecting) {
    return (
      <ConnectorAuthGuard connectorName="Google Chat">
        <div className="min-h-screen bg-black flex items-center justify-center">
          <div className="text-center">
            <Loader2 className="w-12 h-12 animate-spin mx-auto mb-6 text-gray-600" />
            <p className="text-slate-300 text-lg">
              {isConnecting ? "Redirecting to Google..." : "Checking Google Chat configuration..."}
            </p>
          </div>
        </div>
      </ConnectorAuthGuard>
    );
  }

  const envVarSnippet = `GOOGLE_CHAT_CLIENT_ID=your-client-id
GOOGLE_CHAT_CLIENT_SECRET=your-client-secret
GOOGLE_CHAT_SERVICE_ACCOUNT_KEY='{"type":"service_account",...}'`;

  const baseUrl = envCheck?.baseUrl ?? '';
  const redirectUri = `${baseUrl}/google-chat/callback`;
  const eventsUrl = `${baseUrl}/google-chat/events`;

  return (
    <ConnectorAuthGuard connectorName="Google Chat">
      <div className="min-h-screen bg-black flex flex-col items-center justify-center p-4 sm:p-6">
        <div className="w-full max-w-2xl space-y-6">
          {/* Header */}
          <div className="text-center space-y-3">
            <h1 className="text-3xl font-bold text-white tracking-tight">
              Google Chat Setup
            </h1>
            <p className="text-white/50 max-w-xl mx-auto">
              Connect Aurora to Google Chat to receive incident notifications
              and interact with the AI assistant directly from your workspace.
            </p>
          </div>

          <Card className="bg-black border-white/10 overflow-hidden">
            <CardHeader className="pb-4">
              <CardTitle className="text-white flex items-center gap-2 text-lg">
                <AlertCircle className="w-5 h-5" />
                Google Chat Not Configured
              </CardTitle>
              <CardDescription className="text-white/50 mt-2 text-sm">
                Follow the setup guide to create a Google Chat app and connect
                it to Aurora.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5 overflow-x-hidden">
              {/* Status indicators */}
              <div className="grid grid-cols-2 gap-2">
                {[
                  { label: "OAuth Client ID", ok: envCheck?.hasClientId },
                  { label: "OAuth Client Secret", ok: envCheck?.hasClientSecret },
                  { label: "Service Account", ok: envCheck?.hasServiceAccount },
                ].map((item) => (
                  <div
                    key={item.label}
                    className="flex items-center gap-2 text-xs px-3 py-2 rounded bg-white/5 border border-white/10"
                  >
                    {item.ok ? (
                      <CheckCircle className="w-3 h-3 text-green-500 shrink-0" />
                    ) : (
                      <AlertCircle className="w-3 h-3 text-red-400 shrink-0" />
                    )}
                    <span
                      className={
                        item.ok ? "text-white/70" : "text-red-400/90"
                      }
                    >
                      {item.label}
                      {!item.ok && " — missing"}
                    </span>
                  </div>
                ))}
              </div>

              {/* Setup guide link */}
              <a
                href={DOCS_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-3 p-4 rounded-lg bg-blue-500/10 border border-blue-500/20 hover:bg-blue-500/15 transition-colors"
              >
                <ExternalLink className="w-5 h-5 text-blue-400 shrink-0" />
                <div>
                  <p className="text-blue-300 font-medium text-sm">
                    Full Setup Guide →
                  </p>
                  <p className="text-blue-300/60 text-xs mt-0.5">
                    Step-by-step instructions for creating the Google Cloud
                    project, OAuth credentials, service account, and Chat app
                    configuration.
                  </p>
                </div>
              </a>

              {/* URLs to copy */}
              <div className="bg-white/5 rounded-lg p-4 border border-white/10 space-y-4 text-sm">
                <p className="text-white/90 font-medium">
                  Your URLs
                </p>
                <p className="text-white/50 text-xs">
                  These are specific to your deployment. Copy them when
                  following the setup guide.
                </p>

                {/* Redirect URI */}
                <div>
                  <p className="text-white/60 text-xs mb-1.5">
                    Authorized redirect URI{" "}
                    <span className="text-white/30">(for OAuth credentials)</span>
                  </p>
                  <div className="relative">
                    <pre className="bg-black/50 p-2.5 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                      {redirectUri}
                    </pre>
                    <Button
                      variant="outline"
                      size="icon"
                      onClick={() => handleCopy(redirectUri, "redirectUri")}
                      className="absolute top-1.5 right-1.5 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                    >
                      {copySuccess["redirectUri"] ? (
                        <CheckCircle className="w-3 h-3" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                    </Button>
                  </div>
                </div>

                {/* Events endpoint */}
                <div>
                  <p className="text-white/60 text-xs mb-1.5">
                    HTTP endpoint URL{" "}
                    <span className="text-white/30">(for Chat app configuration)</span>
                  </p>
                  <div className="relative">
                    <pre className="bg-black/50 p-2.5 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                      {eventsUrl}
                    </pre>
                    <Button
                      variant="outline"
                      size="icon"
                      onClick={() => handleCopy(eventsUrl, "appUrl")}
                      className="absolute top-1.5 right-1.5 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                    >
                      {copySuccess["appUrl"] ? (
                        <CheckCircle className="w-3 h-3" />
                      ) : (
                        <Copy className="w-3 h-3" />
                      )}
                    </Button>
                  </div>
                </div>
              </div>

              {/* Env vars snippet */}
              <div className="bg-white/5 rounded-lg p-4 border border-white/10 space-y-3 text-sm">
                <p className="text-white/90 font-medium">
                  Environment Variables
                </p>
                <p className="text-white/50 text-xs">
                  Add these to your{" "}
                  <code className="bg-black/50 px-1 py-0.5 rounded">.env</code>{" "}
                  file, then restart Aurora.
                </p>
                <div className="relative">
                  <pre className="bg-black/50 p-3 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                    {envVarSnippet}
                  </pre>
                  <Button
                    variant="outline"
                    size="icon"
                    onClick={() => handleCopy(envVarSnippet, "envVars")}
                    className="absolute top-2 right-2 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                  >
                    {copySuccess["envVars"] ? (
                      <CheckCircle className="w-3 h-3" />
                    ) : (
                      <Copy className="w-3 h-3" />
                    )}
                  </Button>
                </div>
                <div className="bg-amber-500/10 border border-amber-500/20 rounded-md p-2.5">
                  <p className="text-amber-300/90 text-xs">
                    <strong>Important:</strong> The service account JSON
                    must be on a <strong>single line</strong>. Convert it
                    with:{" "}
                    <code className="bg-black/30 px-1 py-0.5 rounded">
                      cat your-key-file.json | jq -c .
                    </code>
                  </p>
                </div>
              </div>

            </CardContent>
          </Card>
        </div>
      </div>
    </ConnectorAuthGuard>
  );
}
