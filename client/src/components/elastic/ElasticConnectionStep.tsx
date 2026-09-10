"use client";

import { useState } from "react";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { ChevronDown, ExternalLink, Loader2 } from "lucide-react";
import { ELASTIC_DOCS_API_KEYS, ELASTIC_DOCS_CLOUD_ID, ELASTIC_TEAL } from "./constants";

export type ElasticConnectMode = "cloud_id" | "urls";

interface ElasticConnectionStepProps {
  mode: ElasticConnectMode;
  setMode: (mode: ElasticConnectMode) => void;
  cloudId: string;
  setCloudId: (value: string) => void;
  elasticsearchUrl: string;
  setElasticsearchUrl: (value: string) => void;
  kibanaUrl: string;
  setKibanaUrl: (value: string) => void;
  apiKey: string;
  setApiKey: (value: string) => void;
  indexPattern: string;
  setIndexPattern: (value: string) => void;
  loading: boolean;
  errorMessage: string | null;
  onConnect: (e: React.FormEvent<HTMLFormElement>) => void;
}

const MODES: Array<{ value: ElasticConnectMode; label: string }> = [
  { value: "cloud_id", label: "Cloud ID" },
  { value: "urls", label: "Endpoint URLs" },
];

const ROLE_DESCRIPTOR_EXAMPLE = `{
  "aurora_read": {
    "cluster": ["monitor"],
    "indices": [{
      "names": ["logs-*", "filebeat-*", "metrics-*", ".alerts-*"],
      "privileges": ["read", "view_index_metadata"]
    }],
    "applications": [{
      "application": "kibana-.kibana",
      "privileges": ["read"],
      "resources": ["*"]
    }]
  }
}`;

export function ElasticConnectionStep({
  mode,
  setMode,
  cloudId,
  setCloudId,
  elasticsearchUrl,
  setElasticsearchUrl,
  kibanaUrl,
  setKibanaUrl,
  apiKey,
  setApiKey,
  indexPattern,
  setIndexPattern,
  loading,
  errorMessage,
  onConnect,
}: ElasticConnectionStepProps) {
  const [showKeyHelp, setShowKeyHelp] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const hasTarget = mode === "cloud_id" ? cloudId.trim().length > 0 : elasticsearchUrl.trim().length > 0;
  const canSubmit = hasTarget && apiKey.trim().length > 0 && !loading;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Step 1: Connect Elastic Cloud</CardTitle>
        <CardDescription>
          Works with Elastic Cloud Hosted, Elastic Cloud Serverless and self-managed Elasticsearch + Kibana. Aurora only reads from Elastic; it never writes.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <form className="space-y-6" onSubmit={onConnect}>
          {/* ---- Deployment target ---- */}
          <div className="border rounded-lg">
            <div className="w-full p-4 flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full text-white text-sm font-bold" style={{ backgroundColor: ELASTIC_TEAL }}>
                1
              </div>
              <span className="font-semibold">Where is your deployment?</span>
            </div>
            <div className="p-4 pt-0 space-y-4 text-sm border-t">
              <div className="space-y-2 pt-3">
                <Label id="elastic-mode-label">Connect using</Label>
                <div className="inline-flex rounded-md border overflow-hidden" role="group" aria-labelledby="elastic-mode-label">
                  {MODES.map((m) => (
                    <button
                      type="button"
                      key={m.value}
                      onClick={() => setMode(m.value)}
                      aria-pressed={mode === m.value}
                      className={`px-4 py-2 text-sm font-medium transition-colors ${
                        mode === m.value ? "text-white" : "text-muted-foreground hover:text-foreground bg-background"
                      }`}
                      style={mode === m.value ? { backgroundColor: ELASTIC_TEAL } : undefined}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  {mode === "cloud_id"
                    ? "Easiest for Elastic Cloud Hosted deployments. Aurora derives the Elasticsearch and Kibana endpoints from the Cloud ID."
                    : "Use for Elastic Cloud Serverless projects (no Cloud ID) or self-managed clusters."}
                </p>
              </div>

              {mode === "cloud_id" ? (
                <div className="space-y-2">
                  <Label htmlFor="elastic-cloud-id">Cloud ID</Label>
                  <Input
                    id="elastic-cloud-id"
                    placeholder="my-deployment:dXMtZWFzdC0xLmF3cy5mb3VuZC5pbyQ0ZmE4...$..."
                    value={cloudId}
                    onChange={(e) => setCloudId(e.target.value)}
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <p className="text-xs text-muted-foreground">
                    Elastic Cloud &rarr; Deployments &rarr; your deployment &rarr; <strong>Manage</strong> &rarr; copy <strong>Cloud ID</strong>.{" "}
                    <a href={ELASTIC_DOCS_CLOUD_ID} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-blue-600 hover:underline">
                      Where to find it <ExternalLink className="h-3 w-3" />
                    </a>
                  </p>
                </div>
              ) : (
                <div className="grid md:grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="elastic-es-url">Elasticsearch URL</Label>
                    <Input
                      id="elastic-es-url"
                      placeholder="https://my-project-a1b2c3.es.us-east-1.aws.elastic.cloud"
                      value={elasticsearchUrl}
                      onChange={(e) => setElasticsearchUrl(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <p className="text-xs text-muted-foreground">
                      Serverless: <code>https://&lt;project&gt;.es.&lt;region&gt;.&lt;csp&gt;.elastic.cloud</code>. Self-managed: <code>https://es.internal:9200</code>.
                    </p>
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="elastic-kb-url">Kibana URL <span className="text-muted-foreground font-normal">(optional)</span></Label>
                    <Input
                      id="elastic-kb-url"
                      placeholder="https://my-project-a1b2c3.kb.us-east-1.aws.elastic.cloud"
                      value={kibanaUrl}
                      onChange={(e) => setKibanaUrl(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <p className="text-xs text-muted-foreground">Needed to list alerting rules and deep-link incidents into Kibana. Alerts and logs work without it.</p>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* ---- API key ---- */}
          <div className="border rounded-lg">
            <div className="w-full p-4 flex items-center gap-3">
              <div className="flex h-7 w-7 items-center justify-center rounded-full text-white text-sm font-bold" style={{ backgroundColor: ELASTIC_TEAL }}>
                2
              </div>
              <span className="font-semibold">API key</span>
            </div>
            <div className="p-4 pt-0 space-y-4 text-sm border-t">
              <div className="space-y-2 pt-3">
                <Label htmlFor="elastic-api-key">API key (Encoded)</Label>
                <Input
                  id="elastic-api-key"
                  type="password"
                  placeholder="Paste the Encoded value, e.g. VnVhQ2ZHY0JDZGJrUW0tZTVhT3g6dWkybHAyYXhUTm1zeWFrdzl0dk5udw=="
                  value={apiKey}
                  onChange={(e) => setApiKey(e.target.value)}
                  autoComplete="off"
                  required
                />
                <p className="text-xs text-muted-foreground">
                  Kibana &rarr; <strong>Stack Management &rarr; API keys &rarr; Create API key</strong>. Copy the <strong>Encoded</strong> value (an <code>id:api_key</code> pair also works). The same key is used for Elasticsearch and Kibana.
                </p>
              </div>

              <Collapsible open={showKeyHelp} onOpenChange={setShowKeyHelp}>
                <CollapsibleTrigger asChild>
                  <button type="button" className="flex items-center gap-1 text-xs font-medium text-blue-600 hover:underline">
                    <ChevronDown className={`h-3 w-3 transition-transform ${showKeyHelp ? "rotate-180" : ""}`} />
                    How to create a read-only API key
                  </button>
                </CollapsibleTrigger>
                <CollapsibleContent className="mt-3 space-y-3">
                  <div className="rounded border bg-muted/40 p-3 space-y-2 text-xs">
                    <p className="font-semibold">Recommended: an admin creates a restricted key</p>
                    <ol className="list-decimal list-inside space-y-1 text-muted-foreground">
                      <li>In Kibana open <strong>Stack Management &rarr; API keys</strong> and click <strong>Create API key</strong>.</li>
                      <li>Name it <code>aurora</code>, turn on <em>Restrict privileges</em>, and paste the role descriptor below.</li>
                      <li>Create it and copy the <strong>Encoded</strong> value. It is shown once.</li>
                    </ol>
                    <pre className="rounded bg-background border p-2 overflow-auto text-[11px] leading-snug">{ROLE_DESCRIPTOR_EXAMPLE}</pre>
                    <p className="text-muted-foreground">
                      <code>monitor</code> is optional (it shows cluster version and index sizes). The <code>kibana-.kibana</code> application privilege is what lets Aurora list Kibana rules.
                    </p>
                    <p className="font-semibold pt-2">Alternative: a read-only user creates its own key</p>
                    <p className="text-muted-foreground">
                      The built-in <strong>Viewer</strong> role cannot create API keys by itself. Give the user <strong>Viewer</strong> plus a custom role whose only cluster privilege is <code>manage_own_api_key</code>, then create the key as that user with <em>Restrict privileges</em> off.
                    </p>
                    <a href={ELASTIC_DOCS_API_KEYS} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-blue-600 hover:underline">
                      Elastic API key documentation <ExternalLink className="h-3 w-3" />
                    </a>
                  </div>
                </CollapsibleContent>
              </Collapsible>

              <Collapsible open={showAdvanced} onOpenChange={setShowAdvanced}>
                <CollapsibleTrigger asChild>
                  <button type="button" className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
                    <ChevronDown className={`h-3 w-3 transition-transform ${showAdvanced ? "rotate-180" : ""}`} />
                    Advanced options
                  </button>
                </CollapsibleTrigger>
                <CollapsibleContent className="mt-3">
                  <div className="space-y-2">
                    <Label htmlFor="elastic-index-pattern">Default index pattern</Label>
                    <Input
                      id="elastic-index-pattern"
                      placeholder="logs-*"
                      value={indexPattern}
                      onChange={(e) => setIndexPattern(e.target.value)}
                      autoComplete="off"
                      spellCheck={false}
                    />
                    <p className="text-xs text-muted-foreground">Where Aurora searches when no index is specified. Comma-separate multiple patterns, e.g. <code>logs-*,filebeat-*</code>.</p>
                  </div>
                </CollapsibleContent>
              </Collapsible>
            </div>
          </div>

          {errorMessage && (
            <div role="alert" className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              {errorMessage}
            </div>
          )}

          <div className="flex items-center gap-3">
            <Button
              type="submit"
              disabled={!canSubmit}
              className="w-full md:w-auto text-white hover:opacity-90"
              style={{ backgroundColor: ELASTIC_TEAL }}
            >
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  Validating…
                </>
              ) : (
                "Connect Elastic Cloud"
              )}
            </Button>
            <p className="text-xs text-muted-foreground">Aurora stores the key in Vault; only an encrypted reference is kept in the database.</p>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
