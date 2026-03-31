"use client";

import { useEffect, useState } from "react";
import { useToast } from "@/hooks/use-toast";
import { elasticsearchService, ElasticsearchStatus } from "@/lib/services/elasticsearch";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Loader2, ExternalLink } from "lucide-react";
import { getUserFriendlyError } from "@/lib/utils";
import { ElasticsearchWebhookStep } from "@/components/elasticsearch/ElasticsearchWebhookStep";

const CACHE_KEY = "elasticsearch_connection_status";

export default function ElasticsearchAuthPage() {
  const { toast } = useToast();
  const [baseUrl, setBaseUrl] = useState("");
  const [authMethod, setAuthMethod] = useState<"apiKey" | "basic">("apiKey");
  const [apiKey, setApiKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [status, setStatus] = useState<ElasticsearchStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [isCheckingStatus, setIsCheckingStatus] = useState(true);

  const loadStatus = async (skipCache = false) => {
    try {
      if (!skipCache && typeof window !== "undefined") {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        if (cachedStatus) {
          const parsedStatus = JSON.parse(cachedStatus);
          setStatus(parsedStatus);
          setIsCheckingStatus(false);
          if (parsedStatus?.connected) {
            setBaseUrl(parsedStatus.baseUrl ?? "");
          }
        }
      }
      await fetchAndUpdateStatus();
    } catch (err) {
      console.error("Failed to load Elasticsearch status", err);
      setIsCheckingStatus(false);
    }
  };

  const fetchAndUpdateStatus = async () => {
    try {
      const result = await elasticsearchService.getStatus();
      if (result !== null) {
        const cachedStatus = localStorage.getItem(CACHE_KEY);
        const wasCachedConnected = cachedStatus ? JSON.parse(cachedStatus)?.connected : false;
        const stateChanged = wasCachedConnected !== result.connected;

        setStatus(result);
        if (typeof window !== "undefined") {
          localStorage.setItem(CACHE_KEY, JSON.stringify(result));
          if (result.connected) {
            localStorage.setItem("isElasticsearchConnected", "true");
          } else {
            localStorage.removeItem("isElasticsearchConnected");
          }
          if (stateChanged) {
            window.dispatchEvent(new CustomEvent("providerStateChanged"));
          }
        }
        if (result.connected) {
          setBaseUrl(result.baseUrl ?? "");
        }
      }
    } catch (err) {
      console.error("[Elasticsearch] Failed to fetch status:", err);
    } finally {
      setIsCheckingStatus(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleConnect = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLoading(true);

    try {
      const payload = {
        baseUrl,
        authMethod,
        ...(authMethod === "apiKey" ? { apiKey } : { username, password }),
      };
      const result = await elasticsearchService.connect(payload);
      setStatus(result);

      if (typeof window !== "undefined") {
        localStorage.setItem(CACHE_KEY, JSON.stringify(result));
      }

      toast({
        title: "Success",
        description: "Elasticsearch connected successfully!",
      });

      if (typeof window !== "undefined") {
        localStorage.setItem("isElasticsearchConnected", "true");
        window.dispatchEvent(new CustomEvent("providerStateChanged"));
      }
    } catch (err: any) {
      console.error("Elasticsearch connection failed", err);
      toast({
        title: "Failed to connect to Elasticsearch",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
      setApiKey("");
      setPassword("");
    }
  };

  const handleDisconnect = async () => {
    setLoading(true);

    try {
      const response = await fetch("/api/connected-accounts/elasticsearch", {
        method: "DELETE",
        credentials: "include",
      });

      if (response.ok || response.status === 204) {
        setStatus({ connected: false });
        setBaseUrl("");

        if (typeof window !== "undefined") {
          localStorage.removeItem(CACHE_KEY);
          localStorage.removeItem("isElasticsearchConnected");
          window.dispatchEvent(new CustomEvent("providerStateChanged"));
        }

        toast({
          title: "Success",
          description: "Elasticsearch disconnected successfully",
        });
      } else {
        const text = await response.text();
        throw new Error(text || "Failed to disconnect Elasticsearch");
      }
    } catch (err: any) {
      console.error("Elasticsearch disconnect failed", err);
      toast({
        title: "Failed to disconnect Elasticsearch",
        description: getUserFriendlyError(err),
        variant: "destructive",
      });
    } finally {
      setLoading(false);
    }
  };

  const isFormValid = baseUrl && (
    (authMethod === "apiKey" && apiKey) ||
    (authMethod === "basic" && username && password)
  );

  if (isCheckingStatus) {
    return (
      <div className="container mx-auto py-8 px-4 max-w-2xl">
        <div className="mb-6">
          <h1 className="text-3xl font-bold">Elasticsearch Integration</h1>
          <p className="text-muted-foreground mt-1">
            Connect your Elasticsearch or OpenSearch instance
          </p>
        </div>
        <Card>
          <CardContent className="flex items-center justify-center py-12">
            <Loader2 className="h-8 w-8 animate-spin text-muted-foreground" />
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="container mx-auto py-8 px-4 max-w-2xl">
      <div className="mb-6">
        <h1 className="text-3xl font-bold">Elasticsearch Integration</h1>
        <p className="text-muted-foreground mt-1">
          Connect your Elasticsearch or OpenSearch instance
        </p>
      </div>

      {!status?.connected ? (
        <Card>
          <CardHeader>
            <CardTitle>Connect to Elasticsearch</CardTitle>
            <CardDescription>
              Enter your Elasticsearch instance URL and credentials to establish a connection.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleConnect} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="baseUrl">Instance URL</Label>
                <Input
                  id="baseUrl"
                  type="url"
                  placeholder="https://your-elasticsearch-instance:9200"
                  value={baseUrl}
                  onChange={(e) => setBaseUrl(e.target.value)}
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Default port is <code className="bg-muted px-1 rounded">9200</code>
                </p>
              </div>

              <div className="bg-muted/50 rounded-lg p-3 text-xs space-y-1">
                <p className="font-medium">Example URLs:</p>
                <ul className="text-muted-foreground space-y-0.5">
                  <li><strong>Self-hosted:</strong> https://elasticsearch.yourcompany.com:9200</li>
                  <li><strong>Elastic Cloud:</strong> https://my-deployment.es.us-east-1.aws.elastic.cloud:9243</li>
                  <li><strong>OpenSearch:</strong> https://opensearch.yourcompany.com:9200</li>
                  <li><strong>Local:</strong> https://host.docker.internal:9200</li>
                </ul>
              </div>

              <div className="space-y-2">
                <Label>Authentication Method</Label>
                <Select value={authMethod} onValueChange={(v) => setAuthMethod(v as "apiKey" | "basic")}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="apiKey">API Key</SelectItem>
                    <SelectItem value="basic">Basic Auth (Username/Password)</SelectItem>
                  </SelectContent>
                </Select>
              </div>

              {authMethod === "apiKey" ? (
                <div className="space-y-2">
                  <Label htmlFor="apiKey">API Key</Label>
                  <Input
                    id="apiKey"
                    type="password"
                    placeholder="Enter your Elasticsearch API key"
                    value={apiKey}
                    onChange={(e) => setApiKey(e.target.value)}
                    required
                  />
                  <p className="text-xs text-muted-foreground">
                    Use the base64-encoded API key value
                  </p>
                </div>
              ) : (
                <>
                  <div className="space-y-2">
                    <Label htmlFor="username">Username</Label>
                    <Input
                      id="username"
                      type="text"
                      placeholder="elastic"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                      required
                    />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="password">Password</Label>
                    <Input
                      id="password"
                      type="password"
                      placeholder="Enter your password"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                  </div>
                </>
              )}

              <div className="bg-muted/50 rounded-lg p-4 text-sm">
                <p className="font-medium mb-2">How to get your credentials:</p>
                {authMethod === "apiKey" ? (
                  <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                    <li>Open Kibana and go to Stack Management</li>
                    <li>Navigate to Security → API keys</li>
                    <li>Click &quot;Create API key&quot;</li>
                    <li>Set a name and optional expiration</li>
                    <li>Copy the base64-encoded key and paste it above</li>
                  </ol>
                ) : (
                  <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                    <li>Use the built-in elastic superuser, or</li>
                    <li>Create a dedicated user with appropriate roles</li>
                    <li>Ensure the user has read access to relevant indices</li>
                  </ol>
                )}
                <a
                  href="https://www.elastic.co/guide/en/elasticsearch/reference/current/security-api-create-api-key.html"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-blue-600 hover:underline mt-2"
                >
                  View Elasticsearch documentation <ExternalLink className="h-3 w-3" />
                </a>
              </div>

              <Button type="submit" className="w-full" disabled={loading || !isFormValid}>
                {loading ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Connecting...
                  </>
                ) : (
                  "Connect to Elasticsearch"
                )}
              </Button>
            </form>
          </CardContent>
        </Card>
      ) : (
        <ElasticsearchWebhookStep
          status={status}
          onDisconnect={handleDisconnect}
          loading={loading}
        />
      )}
    </div>
  );
}
