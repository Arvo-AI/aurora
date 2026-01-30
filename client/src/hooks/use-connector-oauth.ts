import { useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { isSlackEnabled } from "@/lib/feature-flags";
import type { ConnectorConfig } from "@/components/connectors/types";

const slackService = isSlackEnabled() ? require("@/lib/services/slack").slackService : null;
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL;

export function useConnectorOAuth(connector: ConnectorConfig, userId: string | null) {
  const { toast } = useToast();
  const [isConnecting, setIsConnecting] = useState(false);

  // Wrapper function to handle userId validation and common OAuth flow logic
  const withOAuthHandler = async (
    handler: () => Promise<void>,
    errorMessage: string
  ): Promise<void> => {
    if (!userId) {
      toast({
        title: "Error",
        description: "User ID is required",
        variant: "destructive",
      });
      return;
    }

    setIsConnecting(true);

    try {
      await handler();
    } catch (error: any) {
      console.error("OAuth error:", error);
      toast({
        title: "Connection Failed",
        description: error.message || errorMessage,
        variant: "destructive",
      });
      setIsConnecting(false);
      throw error;
    }
  };

  const handleGitHubOAuth = async (onStatusChange: () => void) => {
    await withOAuthHandler(
      async () => {
        const oauthUrl = await GitHubIntegrationService.initiateOAuth(userId!);
        const popup = window.open(
          oauthUrl,
          'github-oauth',
          'width=600,height=700,scrollbars=yes,resizable=yes'
        );

        const checkClosed = setInterval(() => {
          if (popup?.closed) {
            clearInterval(checkClosed);
            setIsConnecting(false);
            setTimeout(async () => {
              onStatusChange();
              window.dispatchEvent(new CustomEvent("providerStateChanged"));
            }, 1000);
          }
        }, 1000);
      },
      "Failed to connect to GitHub"
    );
  };

  const handleSlackOAuth = async () => {
    await withOAuthHandler(
      async () => {
        const response = await slackService.connect();
        if (response.oauth_url) {
          window.location.href = response.oauth_url;
        } else {
          throw new Error("No OAuth URL received");
        }
      },
      "Failed to connect to Slack"
    );
  };

  const handleGCPOAuth = async () => {
    await withOAuthHandler(
      async () => {
        const response = await fetch(`${BACKEND_URL}/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ userId: userId! }),
        });

        if (!response.ok) {
          throw new Error(`Login request failed with status: ${response.status}`);
        }

        const data = await response.json();
        
        if (data.login_url) {
          // Signal graph discovery to trigger after the OAuth redirect completes
          localStorage.setItem("aurora_graph_discovery_trigger", "1");
          window.location.href = data.login_url;
        } else {
          throw new Error("No OAuth URL received");
        }
      },
      "Failed to connect to Google Cloud"
    );
  };

  return {
    isConnecting,
    handleGitHubOAuth,
    handleSlackOAuth,
    handleGCPOAuth,
  };
}
