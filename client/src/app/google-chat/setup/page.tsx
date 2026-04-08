"use client";

import React, { useState, useEffect } from "react";
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
import { getEnv } from "@/lib/env";
import { useToast } from "@/hooks/use-toast";

interface EnvCheckResult {
  configured: boolean;
  hasClientId: boolean;
  hasClientSecret: boolean;
  hasServiceAccount: boolean;
  baseUrl: string;
}

const BACKEND_URL = getEnv('NEXT_PUBLIC_BACKEND_URL');

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
      }
    } catch (err) {
      console.error("Failed to check Google Chat env:", err);
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
      };
      toast({
        title: "Connection Failed",
        description: errorMessages[error] || `Google Chat setup error: ${error}`,
        variant: "destructive",
      });
    }
  }, [searchParams, toast, router]);

  // Auto-trigger OAuth when env is configured
  useEffect(() => {
    if (!isLoading && envCheck?.configured) {
      handleConnect();
    }
  }, [isLoading, envCheck]);

  const handleConnect = async () => {
    setIsConnecting(true);
    try {
      const response = await googleChatService.connect();
      if (response.oauth_url) {
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
  };

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

          {/* Not configured — show setup guide */}
          <Card className="bg-black border-white/10 overflow-hidden">
              <CardHeader className="pb-4">
                <CardTitle className="text-white flex items-center gap-2 text-lg">
                  <AlertCircle className="w-5 h-5" />
                  Google Chat Not Configured
                </CardTitle>
                <CardDescription className="text-white/50 mt-2 text-sm">
                  Follow these steps to create a Google Chat app and connect it
                  to Aurora.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-4 overflow-x-hidden">
                {/* Status indicators for what's missing */}
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

                <div className="bg-white/5 rounded-lg p-4 border border-white/10 space-y-4 text-sm text-white/70">
                  {/* Step 1 */}
                  <div className="break-words">
                    <p className="text-white/90 font-medium mb-1">
                      1. Create a Google Cloud project
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      Go to the Google Cloud Console and create a new project
                      (or select an existing one).
                    </p>
                    <a
                      href="https://console.cloud.google.com/projectcreate"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 hover:underline"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Open Google Cloud Console
                    </a>
                  </div>

                  {/* Step 2 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      2. Enable the Google Chat API
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      In your project, go to{" "}
                      <strong>APIs &amp; Services → Library</strong>, search
                      for &quot;Google Chat API&quot;, and click{" "}
                      <strong>Enable</strong>.
                    </p>
                    <a
                      href="https://console.cloud.google.com/apis/library/chat.googleapis.com"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 hover:underline"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Enable Google Chat API
                    </a>
                  </div>

                  {/* Step 3 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      3. Create OAuth credentials
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      Go to{" "}
                      <strong>
                        APIs &amp; Services → Credentials → Create Credentials → OAuth client ID
                      </strong>
                      . Select <strong>Web application</strong> as the type.
                    </p>
                    <p className="text-white/40 text-xs mb-2">
                      Add this as an <strong>Authorized redirect URI</strong>:
                    </p>
                    <div className="relative mt-1 mb-2">
                      <pre className="bg-black/50 p-2.5 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                        {`${envCheck?.baseUrl ?? BACKEND_URL}/google-chat/callback`}
                      </pre>
                      <Button
                        variant="outline"
                        size="icon"
                        onClick={() =>
                          handleCopy(
                            `${envCheck?.baseUrl ?? BACKEND_URL}/google-chat/callback`,
                            "redirectUri"
                          )
                        }
                        className="absolute top-1.5 right-1.5 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                      >
                        {copySuccess["redirectUri"] ? (
                          <CheckCircle className="w-3 h-3" />
                        ) : (
                          <Copy className="w-3 h-3" />
                        )}
                      </Button>
                    </div>
                    <p className="text-white/40 text-xs">
                      The Client ID and Secret are your{" "}
                      <code className="bg-black/50 px-1 py-0.5 rounded">GOOGLE_CHAT_CLIENT_ID</code>{" "}
                      and{" "}
                      <code className="bg-black/50 px-1 py-0.5 rounded">GOOGLE_CHAT_CLIENT_SECRET</code>
                      .
                    </p>
                    <a
                      href="https://console.cloud.google.com/apis/credentials/oauthclient"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 hover:underline mt-2"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Create OAuth Client
                    </a>
                  </div>

                  {/* Step 4 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      4. Create a service account
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      Go to{" "}
                      <strong>
                        IAM &amp; Admin → Service Accounts → Create Service Account
                      </strong>
                      . Name it something like <strong>aurora-chat-bot</strong>.
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      No IAM roles are needed — the service account
                      authenticates as the Chat app via the{" "}
                      <code className="bg-black/50 px-1 py-0.5 rounded">chat.bot</code>{" "}
                      scope, which is granted automatically when you link it
                      in the Chat app settings (step 5).
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      After creating the account, go to its{" "}
                      <strong>Keys</strong> tab, click{" "}
                      <strong>Add Key → Create new key → JSON</strong>.
                      The downloaded JSON is your{" "}
                      <code className="bg-black/50 px-1 py-0.5 rounded">
                        GOOGLE_CHAT_SERVICE_ACCOUNT_KEY
                      </code>
                    </p>
                    <a
                      href="https://console.cloud.google.com/iam-admin/serviceaccounts/create"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 hover:underline mt-2"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Create Service Account
                    </a>
                  </div>

                  {/* Step 5 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      5. Configure the Chat app
                    </p>
                    <p className="text-white/60 text-xs mb-2">
                      In the Google Chat API settings page, configure the
                      following. Leave everything else as default.
                    </p>

                    <div className="space-y-3 text-xs">
                      {/* Workspace add-on */}
                      <div className="bg-amber-500/10 border border-amber-500/20 rounded-md p-2.5">
                        <p className="text-amber-300/90">
                          Uncheck{" "}
                          <strong>
                            Build this Chat app as a Workspace add-on
                          </strong>{" "}
                          at the top of the page.
                        </p>
                      </div>
                      {/* Application info */}
                      <div>
                        <p className="text-white/50 font-medium mb-1.5">
                          Application info
                        </p>
                        <div className="space-y-2 pl-2">
                          <div>
                            <span className="text-white/70">App name →</span>{" "}
                            <strong className="text-white/90">Aurora</strong>
                          </div>
                          <div>
                            <span className="text-white/70">Avatar URL:</span>
                            <div className="relative mt-1">
                              <pre className="bg-black/50 p-2.5 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                                {
                                  "https://raw.githubusercontent.com/arvo-ai/aurora/main/client/public/arvologo.png"
                                }
                              </pre>
                              <Button
                                variant="outline"
                                size="icon"
                                onClick={() =>
                                  handleCopy(
                                    "https://raw.githubusercontent.com/arvo-ai/aurora/main/client/public/arvologo.png",
                                    "avatarUrl"
                                  )
                                }
                                className="absolute top-1.5 right-1.5 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                              >
                                {copySuccess["avatarUrl"] ? (
                                  <CheckCircle className="w-3 h-3" />
                                ) : (
                                  <Copy className="w-3 h-3" />
                                )}
                              </Button>
                            </div>
                          </div>
                          <div>
                            <span className="text-white/70">
                              Description →
                            </span>{" "}
                            <strong className="text-white/90">
                              AI incident response assistant
                            </strong>
                          </div>
                        </div>
                      </div>

                      {/* Interactive features */}
                      <div>
                        <p className="text-white/50 font-medium mb-1.5">
                          Interactive features
                        </p>
                        <div className="space-y-1 pl-2 text-white/70">
                          <p>
                            Enable <strong className="text-white/90">Interactive features</strong>
                          </p>
                          <p>
                            Under Functionality, check:{" "}
                            <strong className="text-white/90">
                              Join spaces and group conversations
                            </strong>
                          </p>
                        </div>
                      </div>

                      {/* Connection settings */}
                      <div>
                        <p className="text-white/50 font-medium mb-1.5">
                          Connection settings
                        </p>
                        <div className="space-y-2 pl-2">
                          <p className="text-white/70">
                            Select the{" "}
                            <strong className="text-white/90">
                              HTTP endpoint URL
                            </strong>{" "}
                            radio button, then paste this URL in the field
                            below it (must be publicly accessible HTTPS):
                          </p>
                          <div className="relative">
                            <pre className="bg-black/50 p-2.5 pr-10 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono">
                              {`${envCheck?.baseUrl ?? BACKEND_URL}/google-chat/events`}
                            </pre>
                            <Button
                              variant="outline"
                              size="icon"
                              onClick={() =>
                                handleCopy(
                                  `${envCheck?.baseUrl ?? BACKEND_URL}/google-chat/events`,
                                  "appUrl"
                                )
                              }
                              className="absolute top-1.5 right-1.5 h-6 w-6 border-white/10 hover:bg-white/5 text-white/70"
                            >
                              {copySuccess["appUrl"] ? (
                                <CheckCircle className="w-3 h-3" />
                              ) : (
                                <Copy className="w-3 h-3" />
                              )}
                            </Button>
                          </div>
                          <p className="text-white/50">
                            Set{" "}
                            <strong className="text-white/70">
                              Authentication Audience
                            </strong>{" "}
                            to{" "}
                            <strong className="text-white/90">
                              HTTP endpoint URL
                            </strong>
                          </p>
                          <p className="text-white/50">
                            Under{" "}
                            <strong className="text-white/70">
                              Service account
                            </strong>
                            , select the service account you created in step 4
                            (e.g. <em>aurora-chat-bot@your-project.iam.gserviceaccount.com</em>).
                            This links the service account to the Chat app so
                            it can send messages as &quot;Aurora&quot;.
                          </p>
                        </div>
                      </div>

                      {/* Visibility */}
                      <div>
                        <p className="text-white/50 font-medium mb-1.5">
                          Visibility
                        </p>
                        <div className="space-y-1.5 pl-2">
                          <p className="text-white/70">
                            Check{" "}
                            <strong className="text-white/90">
                              Make this Chat app available to specific people
                              and groups
                            </strong>{" "}
                            and add your email address (or a Google Group to
                            let multiple people find and add the bot).
                          </p>
                          <p className="text-white/50">
                            This controls who can <em>find and add</em> the
                            bot — once added to a space, all members of that
                            space can interact with it. You don&apos;t need to
                            add every user here.
                          </p>
                        </div>
                      </div>
                    </div>

                    <a
                      href="https://console.cloud.google.com/apis/api/chat.googleapis.com/hangouts-chat"
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1.5 text-xs text-blue-400 hover:text-blue-300 hover:underline mt-2"
                    >
                      <ExternalLink className="w-3 h-3" />
                      Open Chat API Configuration
                    </a>
                  </div>

                  {/* Step 6 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      6. Add to{" "}
                      <code className="bg-black/50 px-1 py-0.5 rounded text-xs">
                        .env
                      </code>
                    </p>
                    <div className="relative mt-2">
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
                  </div>

                  {/* Step 7 */}
                  <div className="break-words border-t border-white/5 pt-4">
                    <p className="text-white/90 font-medium mb-1">
                      7. Rebuild and restart Aurora
                    </p>
                    <pre className="bg-black/50 p-3 rounded border border-white/10 text-xs overflow-x-auto whitespace-pre text-white/80 font-mono mt-2">
                      {`make down\nmake dev`}
                    </pre>
                  </div>
                </div>

                <div className="flex flex-col sm:flex-row gap-3">
                  <Button
                    onClick={checkEnv}
                    className="flex-1 bg-white text-black hover:bg-white/90"
                  >
                    Check Again
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => router.push("/connectors")}
                    className="border-white/10 hover:bg-white/5 text-white/70"
                  >
                    Back to Connectors
                  </Button>
                </div>
              </CardContent>
            </Card>
        </div>
      </div>
    </ConnectorAuthGuard>
  );
}
