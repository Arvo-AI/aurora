import { useState, createElement } from "react";
import { useToast } from "@/hooks/use-toast";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { isSlackEnabled } from "@/lib/feature-flags";
import type { ConnectorConfig } from "@/components/connectors/types";
import { ToastAction } from "@/components/ui/toast";
import { ExternalLink } from "lucide-react";

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
      const oauthUrl = await GitHubIntegrationService.initiateOAuth(userId);
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
    } catch (error: any) {
      // Prevent error from being logged as uncaught
      if (error.isHandled) {
        // Error is already marked as handled, just log for debugging
        console.log("OAuth error (handled):", error.message);
      } else {
        console.error("OAuth error:", error);
      }
      setIsConnecting(false);
      
      // Check if this is a configuration error
      if (error.errorCode === 'GITHUB_NOT_CONFIGURED' || error.message?.includes('not configured')) {
        const readmeAction = createElement(
          ToastAction,
          {
            altText: "View GitHub setup guide",
            onClick: () => {
              window.open(
                "https://github.com/arvo-ai/aurora/blob/main/server/connectors/github_connector/README.md",
                "_blank",
                "noopener,noreferrer"
              );
            },
            className: "flex items-center gap-1",
          },
          createElement(ExternalLink, { className: "h-3 w-3" }),
          " View Setup Guide"
        );
        
        toast({
          title: "GitHub OAuth Not Configured",
          description: "GitHub OAuth environment variables are not configured. Please configure them to connect GitHub.",
          variant: "destructive",
          action: readmeAction,
        });
      } else {
        toast({
          title: "Connection Failed",
          description: error.message || "Failed to connect to GitHub",
          variant: "destructive",
        });
      }
      
      // Don't re-throw the error to prevent app crash
      // The error is already handled with user feedback via toast
      return;
    }
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
