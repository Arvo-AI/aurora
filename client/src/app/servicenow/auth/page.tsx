"use client";

import { useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { useConnectorAuth } from "@/hooks/use-connector-auth";
import { serviceNowService, type ServiceNowConnectPayload } from "@/lib/services/servicenow";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Loader2 } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { ServiceNowConnectionForm } from "@/components/servicenow/ServiceNowConnectionForm";
import { ServiceNowWebhookStep } from "@/components/servicenow/ServiceNowWebhookStep";
import { ServiceNowRcaSettingsCard } from "@/components/servicenow/ServiceNowRcaSettingsCard";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import Image from "next/image";

const BRAND_COLOR = "#62D84E";

export default function ServiceNowAuthPage() {
  const { toast } = useToast();
  const [loading, setLoading] = useState(false);

  const {
    status,
    isConnected,
    isCheckingStatus,
    updateLocalState,
    disconnect,
  } = useConnectorAuth({
    cacheKey: "servicenow_connection_status",
    storageKey: "isServiceNowConnected",
    fetchStatus: () => serviceNowService.getStatus(),
    disconnectPath: "/api/connected-accounts/servicenow",
  });

  const handleConnect = async (payload: ServiceNowConnectPayload) => {
    setLoading(true);
    try {
      const result = await serviceNowService.connect(payload);
      updateLocalState(result);
      toast({ title: "Success", description: "ServiceNow connected successfully!" });
    } catch (err: any) {
      console.error("ServiceNow connection failed", err);
      toast({
        title: "Failed to connect to ServiceNow",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await disconnect();
      window.dispatchEvent(new CustomEvent("servicenowStateChanged"));
      toast({ title: "Success", description: "ServiceNow disconnected successfully" });
    } catch (err: any) {
      console.error("ServiceNow disconnect failed", err);
      toast({
        title: "Failed to disconnect ServiceNow",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  if (isCheckingStatus) {
    return (
      <ConnectorAuthGuard connectorName="ServiceNow">
        <div className="container mx-auto py-8 px-4 max-w-2xl">
          <Card>
            <CardContent className="flex items-center justify-center py-12">
              <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
            </CardContent>
          </Card>
        </div>
      </ConnectorAuthGuard>
    );
  }

  return (
    <ConnectorAuthGuard connectorName="ServiceNow">
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="flex items-center gap-4 mb-6">
          <div className="flex items-center justify-center h-12 w-12 rounded-lg bg-white dark:bg-white p-1.5">
            <Image src="/servicenow.svg" alt="ServiceNow" width={40} height={40} />
          </div>
          <div>
            <h1 className="text-3xl font-bold">ServiceNow</h1>
            <p className="text-muted-foreground mt-0.5">
              Automated RCA for ServiceNow incidents, posted back as work notes
            </p>
          </div>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${isConnected ? "bg-gray-200 text-gray-600" : "text-white"}`}
              style={isConnected ? undefined : { backgroundColor: BRAND_COLOR }}
            >
              1
            </div>
            <div className="w-24 h-1" style={{ backgroundColor: isConnected ? BRAND_COLOR : "#e5e7eb" }}></div>
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${isConnected ? "text-white" : "bg-gray-200 text-gray-600"}`}
              style={isConnected ? { backgroundColor: BRAND_COLOR } : undefined}
            >
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span
            className={isConnected ? "text-muted-foreground" : "text-foreground"}
            style={isConnected ? undefined : { color: BRAND_COLOR }}
          >
            Connect
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span
            className={isConnected ? "text-foreground" : "text-muted-foreground"}
            style={isConnected ? { color: BRAND_COLOR } : undefined}
          >
            Configure Webhook
          </span>
        </div>

        {isConnected ? (
          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle>Connected</CardTitle>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                {status?.displayName && (
                  <div className="flex justify-between gap-4">
                    <span className="text-muted-foreground">Display name</span>
                    <span className="font-medium">{status.displayName}</span>
                  </div>
                )}
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Instance</span>
                  <span className="font-medium break-all text-right">{status?.instanceUrl ?? "Unknown"}</span>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Authentication</span>
                  <span className="font-medium">
                    {status?.authType === "oauth_client_credentials"
                      ? "OAuth client credentials"
                      : status?.authType === "basic"
                        ? `Basic auth${status.username ? ` (${status.username})` : ""}`
                        : "Unknown"}
                  </span>
                </div>
                <div className="pt-2">
                  <Button variant="destructive" onClick={handleDisconnect} disabled={loading}>
                    {loading ? (
                      <>
                        <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                        Disconnecting...
                      </>
                    ) : (
                      "Disconnect ServiceNow"
                    )}
                  </Button>
                </div>
              </CardContent>
            </Card>

            <ServiceNowWebhookStep />
            <ServiceNowRcaSettingsCard />
          </div>
        ) : (
          <ServiceNowConnectionForm onConnect={handleConnect} loading={loading} />
        )}
      </div>
    </ConnectorAuthGuard>
  );
}
