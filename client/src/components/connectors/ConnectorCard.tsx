"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, CardTitle, CardContent, CardFooter } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Check, ExternalLink, AlertCircle, Loader2, BarChart2, LogOut, KeyRound, Settings } from "lucide-react";
import { useToast } from "@/hooks/use-toast";
import { isSlackEnabled } from "@/lib/feature-flags";
import { useConnectorStatus } from "@/hooks/use-connector-status";
import { useConnectorOAuth } from "@/hooks/use-connector-oauth";
import { ConnectorDialogs } from "./ConnectorDialogs";
import { ConnectorCardContent } from "./ConnectorCardContent";
import type { ConnectorConfig } from "./types";
import { useGitHubStatus } from "@/hooks/use-github-status";
import { useGraphDiscoveryStatus } from "@/hooks/use-graph-discovery-status";

const slackService = isSlackEnabled() ? require("@/lib/services/slack").slackService : null;

interface ConnectorCardProps {
  connector: ConnectorConfig;
}

export default function ConnectorCard({ connector }: ConnectorCardProps) {
  const router = useRouter();
  const { toast } = useToast();
  const [showGitHubDialog, setShowGitHubDialog] = useState(false);
  const [showGcpDialog, setShowGcpDialog] = useState(false);
  const [showOvhDialog, setShowOvhDialog] = useState(false);
  const [showScalewayDialog, setShowScalewayDialog] = useState(false);
  const [userId, setUserId] = useState<string | null>(null);
  const [isConnectingOAuth, setIsConnectingOAuth] = useState(false);
  
  // Single source of truth for GitHub status
  const githubStatus = useGitHubStatus(connector.id === "github" ? userId : null);

  const {
    isConnected,
    setIsConnected,
    isCheckingConnection,
    isLoadingDetails,
    slackStatus,
    checkGitHubStatus,
  } = useConnectorStatus(connector, userId);

  // Graph discovery status (only active for supported cloud providers)
  const { syncStatus } = useGraphDiscoveryStatus(connector.id, isConnected, userId);

  const {
    isConnecting: isConnectingOAuthHandler,
    handleGitHubOAuth,
    handleSlackOAuth,
    handleGCPOAuth,
  } = useConnectorOAuth(connector, userId);

  const isConnecting = isConnectingOAuth || isConnectingOAuthHandler;

  useEffect(() => {
    const fetchUserId = async () => {
      try {
        const response = await fetch('/api/getUserId');
        if (response.ok) {
          const data = await response.json();
          setUserId(data.userId);
        }
      } catch (error) {
        console.error('Error fetching user ID:', error);
      }
    };
    
    fetchUserId();
  }, []);

  const handleDisconnect = async () => {
    if (connector.id === "slack") {
      setIsConnectingOAuth(true);
      try {
        await slackService.disconnect();
        setIsConnected(false);
        if (typeof window !== "undefined") {
          localStorage.removeItem('isSlackConnected');
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }
        toast({
          title: "Success",
          description: "Slack disconnected successfully",
        });
      } catch (error: any) {
        console.error("Slack disconnect error:", error);
        toast({
          title: "Disconnect Failed",
          description: error.message || "Failed to disconnect Slack",
          variant: "destructive",
        });
      } finally {
        setIsConnectingOAuth(false);
      }
    }
  };

  const handleConnect = async () => {
    if (connector.id === "github") {
      if (!isConnected) {
        await handleGitHubOAuth(checkGitHubStatus);
      } else {
        setShowGitHubDialog(true);
      }
      return;
    }

    if (connector.id === "slack") {
      if (!isConnected) {
        await handleSlackOAuth();
      } else {
        await handleDisconnect();
      }
      return;
    }

    if (connector.id === "gcp") {
      if (!isConnected) {
        await handleGCPOAuth();
      } else {
        setShowGcpDialog(true);
      }
      return;
    }

    if (connector.id === "ovh") {
      if (!isConnected) {
        router.push("/ovh/onboarding");
      } else {
        setShowOvhDialog(true);
      }
      return;
    }

    if (connector.id === "scaleway") {
      if (!isConnected) {
        router.push("/scaleway/onboarding");
      } else {
        setShowScalewayDialog(true);
      }
      return;
    }

    if (connector.id === "onprem") {
      if (!isConnected) {
        router.push("/settings/ssh-keys");
      } else {
        router.push("/vm-config");
      }
      return;
    }
    
    if (connector.path) {
      router.push(connector.path);
    } else if (connector.onConnect) {
      await connector.onConnect(null);
    }
  };

  const handleViewAlerts = () => {
    if (connector.alertsPath) {
      router.push(connector.alertsPath);
    }
  };

  const handleOverview = () => {
    if (connector.overviewPath) {
      router.push(connector.overviewPath);
    }
  };

  const IconComponent = connector.icon;

  return (
    <>
      <Card className="flex flex-col hover:shadow-lg transition-all duration-200 hover:border-primary/50">
        <CardHeader>
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              <div className={`p-2 rounded-lg ${connector.iconBgColor || "bg-muted"}`}>
                {connector.iconPath ? (
                  <div className="relative h-6 w-6">
                    <img src={connector.iconPath} alt={`${connector.name} icon`} className="h-6 w-6 object-contain" />
                  </div>
                ) : IconComponent ? (
                  <IconComponent className={`h-6 w-6 ${connector.iconColor || "text-foreground"}`} />
                ) : null}
              </div>
              <div>
                <CardTitle className="text-lg">{connector.name}</CardTitle>
                {connector.category && (
                  <Badge variant="outline" className="mt-1 text-xs">
                    {connector.category}
                  </Badge>
                )}
            </div>
          </div>
            {connector.id === "github" && githubStatus.isAuthenticated ? (
              githubStatus.isConnected ? (
                <div className="flex items-center gap-1 text-green-600 dark:text-green-500">
                  <Check className="h-4 w-4" />
                  <span className="text-xs font-medium">Connected</span>
                </div>
              ) : (
                <div className="flex items-center gap-1 text-yellow-600 dark:text-yellow-500">
                  <AlertCircle className="h-4 w-4" />
                  <span className="text-xs font-medium">Available</span>
                </div>
              )
            ) : connector.id === "onprem" && isCheckingConnection ? (
              <div className="flex items-center gap-1 text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                <span className="text-xs font-medium">Checking...</span>
              </div>
            ) : isConnected ? (
              <div className="flex items-center gap-1 text-green-600 dark:text-green-500">
                <Check className="h-4 w-4" />
                <span className="text-xs font-medium">Connected</span>
              </div>
            ) : null}
          </div>
        </CardHeader>
        
        <CardContent className="flex-1">
          {connector.id === "slack" && isConnected ? (
            <ConnectorCardContent
              isLoading={isLoadingDetails}
              slackStatus={slackStatus}
              description={connector.description}
            />
          ) : (
            <ConnectorCardContent
              isLoading={false}
              slackStatus={null}
              description={connector.description}
            />
          )}
          {syncStatus !== "idle" && (
            <div className="flex items-center gap-1.5 mt-2 text-xs">
              {syncStatus === "building" && (
                <>
                  <Loader2 className="h-3 w-3 animate-spin text-muted-foreground" />
                  <span className="text-muted-foreground">Building dependency graph...</span>
                </>
              )}
              {syncStatus === "synced" && (
                <>
                  <Check className="h-3 w-3 text-green-600 dark:text-green-500" />
                  <span className="text-green-600 dark:text-green-500">Graph synced</span>
                </>
              )}
              {syncStatus === "error" && (
                <>
                  <AlertCircle className="h-3 w-3 text-red-600 dark:text-red-500" />
                  <span className="text-red-600 dark:text-red-500">Graph sync failed</span>
                </>
              )}
            </div>
          )}
        </CardContent>
        
        <CardFooter className="flex flex-col gap-2">
          {connector.id === 'onprem' ? (
            // On Prem always shows both buttons
            <div className="flex gap-2 w-full">
              <Button
                onClick={() => router.push("/settings/ssh-keys")}
                className="w-full bg-white text-black hover:bg-gray-100"
              >
                <KeyRound className="h-4 w-4 mr-2" />
                SSH Keys
              </Button>
              <Button
                onClick={() => router.push("/vm-config")}
                className="w-full bg-white text-black hover:bg-gray-100"
              >
                <Settings className="h-4 w-4 mr-2" />
                VM Config
              </Button>
            </div>
          ) : (
            <>
              <Button 
                onClick={handleConnect} 
                className="w-full"
                variant={isConnected ? "outline" : "default"}
                disabled={isConnecting}
              >
                {isConnecting ? (
                  <>
                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    {connector.id === "slack" && isConnected ? "Disconnecting..." : "Connecting..."}
                  </>
                ) : connector.id === "slack" && isConnected ? (
                  <>
                    <LogOut className="h-4 w-4 mr-2" />
                    Disconnect
                  </>
                ) : isConnected ? (
                  <>
                    <ExternalLink className="h-4 w-4 mr-2" />
                    {connector.id === 'kubectl' ? 'Manage Clusters' : 'Manage'}
                  </>
                ) : (
                  "Connect"
                )}
              </Button>
              
              {isConnected && connector.id === 'kubectl' && (
                <Button
                  onClick={() => router.push('/kubectl/auth')}
                  className="w-full"
                  variant="secondary"
                >
                  Add Cluster
                </Button>
              )}
            </>
          )}
          
          {isConnected && (connector.alertsPath || connector.overviewPath) && (
            <div className="flex w-full flex-col gap-2">
              {connector.alertsPath && (
                <Button
                  onClick={handleViewAlerts}
                  className="w-full sm:flex-1"
                  variant="secondary"
                >
                  <AlertCircle className="h-4 w-4 mr-2" />
                  {connector.alertsLabel ?? "View Alerts"}
                </Button>
              )}
              {connector.overviewPath && (
                <Button
                  onClick={handleOverview}
                  className="w-full sm:flex-1"
                  variant="secondary"
                >
                  <BarChart2 className="h-4 w-4 mr-2" />
                  {connector.overviewLabel ?? "Overview"}
                </Button>
              )}
            </div>
          )}
        </CardFooter>
      </Card>
      
      <ConnectorDialogs
        connectorId={connector.id}
        showGitHubDialog={showGitHubDialog}
        showGcpDialog={showGcpDialog}
        showOvhDialog={showOvhDialog}
        showScalewayDialog={showScalewayDialog}
        onGitHubDialogChange={(open) => {
          setShowGitHubDialog(open);
          if (!open) {
            setTimeout(() => {
              checkGitHubStatus();
              githubStatus.refresh();
            }, 500);
          }
        }}
        onGcpDialogChange={setShowGcpDialog}
        onOvhDialogChange={setShowOvhDialog}
        onScalewayDialogChange={setShowScalewayDialog}
        onGitHubDialogClose={() => setShowGitHubDialog(false)}
      />
    </>
  );
}
