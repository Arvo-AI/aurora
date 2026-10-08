"use client";

import { useCallback, useState } from "react";
import { useRouter } from "next/navigation";
import { ArrowLeft, ExternalLink, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { useToast } from "@/hooks/use-toast";
import { teamsService } from "@/lib/services/teams";
import { apiErrorMessage } from "@/lib/services/api-client";
import { TEAMS_CUSTOMER_SETUP, TEAMS_SETUP_DOCS_URL } from "@/lib/chat-platform/teams-setup";

export default function TeamsSetupPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [isConnecting, setIsConnecting] = useState(false);

  const handleConnect = useCallback(async () => {
    setIsConnecting(true);
    try {
      const response = await teamsService.connect();
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
    <ConnectorAuthGuard connectorName="Microsoft Teams">
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
          </div>

          <Card className="border-zinc-800 bg-zinc-950">
            <CardHeader>
              <CardTitle className="text-lg">{copy.installTitle}</CardTitle>
              <CardDescription>{copy.installWhy}</CardDescription>
            </CardHeader>
            <CardContent>
              <ol className="list-decimal list-inside space-y-2 text-sm text-zinc-300">
                {copy.installSteps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ol>
            </CardContent>
          </Card>

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
            </CardContent>
          </Card>

          <p className="text-xs text-zinc-500">
            Hosting Aurora yourself? Azure Bot and Entra setup for operators is in the{" "}
            <a
              href={TEAMS_SETUP_DOCS_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-primary hover:underline inline-flex items-center gap-1"
            >
              connector documentation
              <ExternalLink className="h-3 w-3" />
            </a>
            .
          </p>
        </div>
      </div>
    </ConnectorAuthGuard>
  );
}
