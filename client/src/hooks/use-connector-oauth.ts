import { useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { BitbucketIntegrationService } from "@/components/bitbucket-provider-integration";
import type { ConnectorConfig } from "@/components/connectors/types";
import { slackService } from "@/lib/services/slack";
import { ProjectCache } from "@/components/cloud-provider/projects/projectUtils";

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

  const handleBitbucketOAuth = async (onStatusChange: () => void) => {
    setIsConnecting(true);

    try {
      const oauthUrl = await BitbucketIntegrationService.initiateOAuth();
      const popup = window.open(
        oauthUrl,
        'bitbucket-oauth',
        'width=600,height=700,scrollbars=yes,resizable=yes'
      );

      const checkClosed = setInterval(() => {
        if (popup?.closed) {
          clearInterval(checkClosed);
          setIsConnecting(false);
          setTimeout(() => {
            onStatusChange();
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
          }, 1000);
        }
      }, 1000);
    } catch (error: any) {
      console.error("Bitbucket OAuth error:", error);
      setIsConnecting(false);
      toast({
        title: "Connection Failed",
        description: error.message || "Failed to connect to Bitbucket",
        variant: "destructive",
      });
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
        const response = await fetch("/api/gcp/oauth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        });

        if (!response.ok) {
          throw new Error(`Login request failed with status: ${response.status}`);
        }

        const data = await response.json();
        
        if (data.login_url) {
          ProjectCache.invalidate('gcp');
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
    handleBitbucketOAuth,
    handleSlackOAuth,
    handleGCPOAuth,
  };
}
