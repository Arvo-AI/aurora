"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { elasticService, ElasticStatus } from "@/lib/services/elastic";
import { ElasticConnectionStep, ElasticConnectMode } from "@/components/elastic/ElasticConnectionStep";
import { ElasticWebhookStep } from "@/components/elastic/ElasticWebhookStep";
import { getUserFriendlyError } from "@/lib/utils";
import ConnectorAuthGuard from "@/components/connectors/ConnectorAuthGuard";
import { ELASTIC_TEAL } from "@/components/elastic/constants";
import { isElasticEnabled } from "@/lib/feature-flags";
import { ConnectorNotEnabled } from "@/components/elastic/ConnectorNotEnabled";

function broadcastStateChange() {
  if (globalThis.window === undefined) return;
  globalThis.window.dispatchEvent(new CustomEvent("providerStateChanged"));
  globalThis.window.dispatchEvent(new Event("elasticStateChanged"));
}


export default function ElasticAuthPage() {
  const { toast } = useToast();
  const [mode, setMode] = useState<ElasticConnectMode>("cloud_id");
  const [cloudId, setCloudId] = useState("");
  const [elasticsearchUrl, setElasticsearchUrl] = useState("");
  const [kibanaUrl, setKibanaUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [indexPattern, setIndexPattern] = useState("logs-*");
  const [status, setStatus] = useState<ElasticStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [connectError, setConnectError] = useState<string | null>(null);

  const applyStatus = (next: ElasticStatus | null) => {
    setStatus(next);
    if (next) elasticService.cacheStatus(next);
    else elasticService.clearCachedStatus();
    broadcastStateChange();
  };

  const fetchAndUpdateStatus = async () => {
    const result = await elasticService.getStatus();
    // null means the request failed (a real "not connected" is a non-null
    // object); keep the current/cached state rather than flipping the page.
    if (result !== null) applyStatus(result);
  };

  const loadStatus = async () => {
    try {
      const cached = elasticService.loadCachedStatus();
      if (cached) {
        applyStatus({ ...cached });
        fetchAndUpdateStatus();
        return;
      }
      await fetchAndUpdateStatus();
    } catch (error: unknown) {
      console.error("[elastic] Failed to load status", error);
      toast({ title: "Error", description: "Unable to load Elastic status", variant: "destructive" });
    }
  };

  useEffect(() => {
    if (!isElasticEnabled()) return;
    loadStatus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);
    setConnectError(null);
    try {
      const payload =
        mode === "cloud_id"
          ? { apiKey, cloudId: cloudId.trim(), kibanaUrl: undefined, indexPattern: indexPattern.trim() || undefined }
          : {
              apiKey,
              elasticsearchUrl: elasticsearchUrl.trim(),
              kibanaUrl: kibanaUrl.trim() || undefined,
              indexPattern: indexPattern.trim() || undefined,
            };
      const result = await elasticService.connect(payload);
      applyStatus(result);
      toast({
        title: "Elastic Cloud connected",
        description: result.kibanaUrl && !result.kibanaReachable
          ? "Elasticsearch validated. Kibana could not be reached; alerts and logs still work."
          : "Set up the Kibana webhook below to send alerts to Aurora.",
      });
      setApiKey("");
    } catch (error: unknown) {
      console.error("[elastic] Connect failed", error);
      const message = getUserFriendlyError(error);
      setConnectError(message);
      toast({ title: "Failed to connect to Elastic", description: message, variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);
    try {
      const response = await fetch("/api/connected-accounts/elastic", { method: "DELETE", credentials: "include" });
      if (!response.ok && response.status !== 204) {
        throw new Error((await response.text()) || "Failed to disconnect Elastic");
      }
      applyStatus({ connected: false });
      setCloudId("");
      setElasticsearchUrl("");
      setKibanaUrl("");
      toast({ title: "Disconnected", description: "Elastic Cloud disconnected successfully." });
    } catch (error: unknown) {
      console.error("[elastic] Disconnect failed", error);
      toast({ title: "Failed to disconnect Elastic", description: getUserFriendlyError(error), variant: "destructive" });
    } finally {
      setLoading(false);
    }
  };

  if (!isElasticEnabled()) {
    return <ConnectorNotEnabled />;
  }

  const isConnected = Boolean(status?.connected);

  return (
    <ConnectorAuthGuard connectorName="Elastic Cloud">
      <div className="container mx-auto py-8 px-4 max-w-5xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Elastic Cloud Integration</h1>
          <p className="text-muted-foreground mt-1">
            Search Elasticsearch logs and read Kibana alerts during investigations, and turn Kibana alert rules into Aurora incidents.
          </p>
        </div>

        <div className="flex items-center justify-center mb-8">
          <div className="flex items-center">
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${!isConnected ? "text-white" : "bg-gray-200 text-gray-600"}`}
              style={!isConnected ? { backgroundColor: ELASTIC_TEAL } : undefined}
            >
              1
            </div>
            <div className="w-24 h-1" style={{ backgroundColor: isConnected ? ELASTIC_TEAL : "#e5e7eb" }}></div>
            <div
              className={`flex items-center justify-center w-10 h-10 rounded-full font-bold ${isConnected ? "text-white" : "bg-gray-200 text-gray-600"}`}
              style={isConnected ? { backgroundColor: ELASTIC_TEAL } : undefined}
            >
              2
            </div>
          </div>
        </div>

        <div className="flex items-center justify-center mb-6 text-sm font-medium">
          <span style={{ color: !isConnected ? ELASTIC_TEAL : undefined }} className={!isConnected ? undefined : "text-muted-foreground"}>
            Connect
          </span>
          <span className="mx-4 text-muted-foreground">&rarr;</span>
          <span style={{ color: isConnected ? ELASTIC_TEAL : undefined }} className={isConnected ? undefined : "text-muted-foreground"}>
            Webhook &amp; RCA
          </span>
        </div>

        {isConnected && status ? (
          <ElasticWebhookStep status={status} onDisconnect={handleDisconnect} loading={loading} />
        ) : (
          <ElasticConnectionStep
            mode={mode}
            setMode={setMode}
            cloudId={cloudId}
            setCloudId={setCloudId}
            elasticsearchUrl={elasticsearchUrl}
            setElasticsearchUrl={setElasticsearchUrl}
            kibanaUrl={kibanaUrl}
            setKibanaUrl={setKibanaUrl}
            apiKey={apiKey}
            setApiKey={setApiKey}
            indexPattern={indexPattern}
            setIndexPattern={setIndexPattern}
            loading={loading}
            errorMessage={connectError}
            onConnect={handleConnect}
          />
        )}
      </div>
    </ConnectorAuthGuard>
  );
}
