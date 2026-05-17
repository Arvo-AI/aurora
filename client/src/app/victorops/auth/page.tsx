"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { victoropsService, VictorOpsStatus } from "@/lib/services/victorops";
import { VictorOpsConnectionStep } from "@/components/victorops/VictorOpsConnectionStep";
import { VictorOpsConnectedView } from "@/components/victorops/VictorOpsConnectedView";
import { VictorOpsWebhookStep } from "@/components/victorops/VictorOpsWebhookStep";
import { ConnectionLoadingOverlay } from "@/components/ui/connection-loading-overlay";
import { DisconnectConfirmDialog } from "@/components/ui/disconnect-confirm-dialog";
import { getUserFriendlyError } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";

const CACHE_KEY = 'victorops_connection_status';

export default function VictorOpsAuthPage() {
  const { toast } = useToast();
  const [displayName, setDisplayName] = useState("Splunk On-Call");
  const [apiId, setApiId] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [status, setStatus] = useState<VictorOpsStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isConnecting, setIsConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showDisconnectDialog, setShowDisconnectDialog] = useState(false);

  const updateLocalStorage = (connected: boolean) => {
    if (typeof globalThis.window === 'undefined') return;
    if (connected) {
      localStorage.setItem('isVictorOpsConnected', 'true');
    } else {
      localStorage.removeItem('isVictorOpsConnected');
    }
    globalThis.dispatchEvent(new Event('victorOpsStateChanged'));
    globalThis.dispatchEvent(new Event('providerStateChanged'));
  };

  const fetchAndUpdateStatus = async () => {
    const result = await victoropsService.getStatus();
    setStatus(result);
    if (typeof globalThis.window !== 'undefined' && result) {
      // Only persist the minimal shape needed for instant UI hydration
      localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: result.connected }));
    }
    updateLocalStorage(result?.connected ?? false);
  };

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof globalThis.window !== 'undefined') {
        const cached = localStorage.getItem(CACHE_KEY);
        if (cached) {
          const minimal = JSON.parse(cached) as VictorOpsStatus;
          setStatus(minimal);
        }
      }
      await fetchAndUpdateStatus();
    } catch {
      toast({
        title: 'Error',
        description: 'Unable to load Splunk On-Call status',
        variant: 'destructive',
      });
    }
  };

  useEffect(() => {
    loadStatus();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setError(null);
    setIsConnecting(true);

    try {
      const result = await victoropsService.connect(apiId, apiKey, displayName);
      if (result.connected) {
        setStatus(result);
        localStorage.setItem(CACHE_KEY, JSON.stringify({ connected: true }));
        updateLocalStorage(true);
        toast({
          title: 'Connected',
          description: `Splunk On-Call connected successfully.`,
        });
        setApiId("");
        setApiKey("");
      }
    } catch (err: unknown) {
      const message = getUserFriendlyError(err);
      setError(message);
      toast({ title: 'Connection Failed', description: message, variant: 'destructive' });
    } finally {
      setIsConnecting(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      await victoropsService.disconnect();
      setStatus(null);
      updateLocalStorage(false);
      if (typeof globalThis.window !== 'undefined') localStorage.removeItem(CACHE_KEY);
      toast({ title: 'Disconnected', description: 'Splunk On-Call disconnected.' });
    } catch (err: unknown) {
      const message = getUserFriendlyError(err);
      toast({ title: 'Error', description: message, variant: 'destructive' });
    } finally {
      setLoading(false);
      setShowDisconnectDialog(false);
    }
  };

  return (
    <ConnectorAuthGuard>
      <div className="max-w-2xl mx-auto space-y-6 p-6">
        <div>
          <h1 className="text-2xl font-bold">Splunk On-Call</h1>
          <p className="text-muted-foreground mt-1">
            Connect Splunk On-Call to receive real-time incident alerts and trigger automated RCA.
          </p>
        </div>

        {isConnecting && (
          <ConnectionLoadingOverlay isVisible={isConnecting} message="Validating Splunk On-Call credentials…" />
        )}

        {status?.connected ? (
          <>
            <VictorOpsConnectedView
              status={status}
              onDisconnect={() => setShowDisconnectDialog(true)}
              disconnecting={loading}
            />
            <VictorOpsWebhookStep />
          </>
        ) : (
          <VictorOpsConnectionStep
            displayName={displayName}
            setDisplayName={setDisplayName}
            apiId={apiId}
            setApiId={setApiId}
            apiKey={apiKey}
            setApiKey={setApiKey}
            loading={isConnecting}
            error={error}
            onConnect={handleConnect}
          />
        )}

        <DisconnectConfirmDialog
          open={showDisconnectDialog}
          onOpenChange={setShowDisconnectDialog}
          onConfirm={() => { handleDisconnect(); }}
          connectorName="Splunk On-Call"
        />
      </div>
    </ConnectorAuthGuard>
  );
}
