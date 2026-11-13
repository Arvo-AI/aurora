"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useToast } from "@/hooks/use-toast";
import { copyToClipboard, getUserFriendlyError } from "@/lib/utils";
import { Check, Copy, FileJson, Loader2, ShieldCheck, Upload } from "lucide-react";

type Triage = {
  event_id: number;
  title: string;
  category: string;
  confidence: string;
  rule_id: string;
  evidence_lines: string[];
  note?: string | null;
  primary_node?: string | null;
  failing_nodes: { unique_id: string; status: string; file_path?: string | null }[];
  first_suspicious_ancestor?: { unique_id: string; reason: string; strength: string } | null;
  blast_radius: { models: string[]; exposures: string[]; tests: number; tasks: string[] };
  history?: { summary?: string };
  redactions: number;
  classification_ms: number;
};

type EventStatus = {
  incident: { id: string; aurora_status: string | null; status: string } | null;
  rca: { session_status: string | null; final_message: string | null };
};

type WebhookInfo = {
  configured: boolean;
  webhookUrl: string;
  secret: string;
  signatureHeader: string;
  callbackSnippet: string;
  instructions: string[];
};

const CATEGORY_LABEL: Record<string, string> = {
  execution: "Execution",
  ingestion_upstream: "Upstream / ingestion",
  platform: "Platform",
  schema_drift: "Schema drift",
  data_test: "Data test",
  unknown: "Unknown",
};

const RCA_DONE = new Set(["complete", "error", "hook_blocked", "rate_limited"]);

function FileDrop({ label, hint, file, onFile }: {
  label: string; hint: string; file: File | null; onFile: (f: File | null) => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div
      className="rounded-lg border border-dashed border-border p-4 hover:bg-muted/30 transition-colors cursor-pointer"
      onClick={() => inputRef.current?.click()}
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        onFile(e.dataTransfer.files?.[0] ?? null);
      }}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => onFile(e.target.files?.[0] ?? null)}
      />
      <div className="flex items-center gap-3">
        <FileJson className="h-5 w-5 text-muted-foreground shrink-0" />
        <div className="min-w-0">
          <div className="text-sm font-medium">{label}</div>
          <div className="text-xs text-muted-foreground truncate">
            {file ? `${file.name} (${Math.round(file.size / 1024)} KB)` : hint}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function PipelinePage() {
  const { toast } = useToast();
  const [tab, setTab] = useState("dbt");
  const [runResults, setRunResults] = useState<File | null>(null);
  const [manifest, setManifest] = useState<File | null>(null);
  const [recentChanges, setRecentChanges] = useState("");
  const [logText, setLogText] = useState("");
  const [pipelineName, setPipelineName] = useState("");
  const [airflowPayload, setAirflowPayload] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [triage, setTriage] = useState<Triage | null>(null);
  const [status, setStatus] = useState<EventStatus | null>(null);
  const [webhook, setWebhook] = useState<WebhookInfo | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/pipeline/webhook-url")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => d && setWebhook(d))
      .catch(() => undefined);
  }, []);

  // Poll the RCA until the investigation finishes.
  useEffect(() => {
    if (!triage) return;
    let stopped = false;
    const tick = async () => {
      try {
        const r = await fetch(`/api/pipeline/events/${triage.event_id}`);
        if (r.ok) {
          const d: EventStatus = await r.json();
          if (stopped) return;
          setStatus(d);
          const done = RCA_DONE.has(d.incident?.aurora_status ?? "") ||
            ["completed", "failed"].includes(d.rca.session_status ?? "");
          if (done) return;
        }
      } catch {
        /* keep polling */
      }
      if (!stopped) setTimeout(tick, 4000);
    };
    tick();
    return () => {
      stopped = true;
    };
  }, [triage]);

  const submit = useCallback(async () => {
    const form = new FormData();
    form.append("kind", tab === "airflow" ? "airflow" : tab);
    if (tab === "dbt") {
      if (!runResults) {
        toast({ title: "run_results.json is required", variant: "destructive" });
        return;
      }
      form.append("run_results", runResults);
      if (manifest) form.append("manifest", manifest);
      if (recentChanges.trim()) form.append("recent_changes", recentChanges);
    } else if (tab === "log") {
      if (!logText.trim()) {
        toast({ title: "Paste a log first", variant: "destructive" });
        return;
      }
      form.append("log_text", logText);
      if (pipelineName.trim()) form.append("pipeline_name", pipelineName);
      if (recentChanges.trim()) form.append("recent_changes", recentChanges);
    } else {
      if (!airflowPayload.trim()) {
        toast({ title: "Paste an Airflow failure payload first", variant: "destructive" });
        return;
      }
      form.append("airflow_payload", airflowPayload);
    }
    setSubmitting(true);
    setTriage(null);
    setStatus(null);
    try {
      const r = await fetch("/api/pipeline/submit", { method: "POST", body: form });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "Submission failed");
      setTriage(d);
    } catch (e) {
      toast({ title: "Could not submit", description: getUserFriendlyError(e), variant: "destructive" });
    } finally {
      setSubmitting(false);
    }
  }, [tab, runResults, manifest, recentChanges, logText, pipelineName, airflowPayload, toast]);

  const rotateSecret = async () => {
    const r = await fetch("/api/pipeline/webhook-secret", { method: "POST" });
    if (r.ok) setWebhook(await r.json());
    else toast({ title: "Could not generate a secret", variant: "destructive" });
  };

  const copy = async (key: string, text: string) => {
    await copyToClipboard(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 1500);
  };

  const rcaDone = !!status && (RCA_DONE.has(status.incident?.aurora_status ?? "") ||
    ["completed", "failed"].includes(status.rca.session_status ?? ""));

  return (
    <div className="max-w-4xl mx-auto py-8 px-4 sm:px-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Investigate a pipeline failure</h1>
        <p className="text-sm text-muted-foreground mt-1">
          Drop the artifacts from a failed dbt run, paste a log, or wire Airflow&apos;s failure callback.
          No Airflow, dbt Cloud or warehouse credentials needed.
        </p>
      </div>

      <Card>
        <CardContent className="pt-6">
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList>
              <TabsTrigger value="dbt">dbt artifacts</TabsTrigger>
              <TabsTrigger value="log">Paste a log</TabsTrigger>
              <TabsTrigger value="airflow">Airflow</TabsTrigger>
            </TabsList>

            <TabsContent value="dbt" className="space-y-3 pt-4">
              <div className="grid sm:grid-cols-2 gap-3">
                <FileDrop label="run_results.json (required)" hint="Drop or click — from target/ after the failed run"
                  file={runResults} onFile={setRunResults} />
                <FileDrop label="manifest.json (optional)" hint="Adds lineage: upstream cause and blast radius"
                  file={manifest} onFile={setManifest} />
              </div>
            </TabsContent>

            <TabsContent value="log" className="space-y-3 pt-4">
              <Input placeholder="Pipeline name (optional)" value={pipelineName}
                onChange={(e) => setPipelineName(e.target.value)} />
              <Textarea rows={10} className="font-mono text-xs"
                placeholder="Paste dbt CLI output, an Airflow task log or a CI log"
                value={logText} onChange={(e) => setLogText(e.target.value)} />
            </TabsContent>

            <TabsContent value="airflow" className="space-y-4 pt-4">
              <div className="space-y-2">
                <Label>Webhook URL</Label>
                <div className="flex gap-2">
                  <Input readOnly value={webhook?.webhookUrl ?? ""} className="font-mono text-xs" />
                  <Button variant="outline" size="icon" disabled={!webhook}
                    onClick={() => webhook && copy("url", webhook.webhookUrl)}>
                    {copied === "url" ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
                  </Button>
                </div>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <ShieldCheck className="h-3.5 w-3.5" />
                  {webhook?.configured
                    ? <>Signed with an Aurora-issued secret ({webhook.signatureHeader}). Aurora never calls Airflow.</>
                    : <>Generate a signing secret to enable the webhook.</>}
                  <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={rotateSecret}>
                    {webhook?.configured ? "Rotate secret" : "Generate secret"}
                  </Button>
                </div>
              </div>
              {webhook?.configured && (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <Label>on_failure_callback (save as aurora_callback.py)</Label>
                    <Button variant="ghost" size="sm" onClick={() => copy("snippet", webhook.callbackSnippet)}>
                      {copied === "snippet" ? <Check className="h-4 w-4 mr-1" /> : <Copy className="h-4 w-4 mr-1" />}
                      Copy
                    </Button>
                  </div>
                  <pre className="max-h-72 overflow-auto rounded-md bg-muted p-3 text-xs font-mono whitespace-pre">
                    {webhook.callbackSnippet}
                  </pre>
                  <ul className="text-xs text-muted-foreground list-none space-y-0.5">
                    {webhook.instructions.map((i) => <li key={i}>{i}</li>)}
                  </ul>
                </div>
              )}
              <div className="space-y-2">
                <Label>Or paste one failure payload to test</Label>
                <Textarea rows={6} className="font-mono text-xs"
                  placeholder='{"dag_id": "...", "task_id": "...", "exception": "...", "log_excerpt": "..."}'
                  value={airflowPayload} onChange={(e) => setAirflowPayload(e.target.value)} />
              </div>
            </TabsContent>
          </Tabs>

          {tab !== "airflow" && (
            <div className="space-y-2 pt-3">
              <Label className="text-xs text-muted-foreground">
                Recent changes (optional): paste <code>git log -p -3 -- models/your_model.sql</code>
              </Label>
              <Textarea rows={3} className="font-mono text-xs" value={recentChanges}
                onChange={(e) => setRecentChanges(e.target.value)} />
            </div>
          )}

          <div className="flex items-center justify-between pt-4">
            <p className="text-xs text-muted-foreground">Secrets in logs are redacted before anything is stored.</p>
            <Button onClick={submit} disabled={submitting}>
              {submitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Upload className="h-4 w-4 mr-2" />}
              Investigate
            </Button>
          </div>
        </CardContent>
      </Card>

      {triage && (
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2 flex-wrap">
              <Badge>{CATEGORY_LABEL[triage.category] ?? triage.category}</Badge>
              <Badge variant="outline">confidence {triage.confidence}</Badge>
              <span className="text-xs text-muted-foreground">classified in {triage.classification_ms} ms</span>
            </div>
            <CardTitle className="text-base pt-2">{triage.title}</CardTitle>
            {triage.history?.summary && <CardDescription>{triage.history.summary}</CardDescription>}
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1">Evidence ({triage.rule_id})</div>
              <pre className="rounded-md bg-muted p-2 text-xs font-mono whitespace-pre-wrap break-words">
                {triage.evidence_lines.join("\n")}
              </pre>
              {triage.note && <p className="text-xs text-muted-foreground mt-2">{triage.note}</p>}
            </div>
            {triage.first_suspicious_ancestor && (
              <div>
                <div className="text-xs font-medium text-muted-foreground mb-1">Likely origin</div>
                <code className="text-xs">{triage.first_suspicious_ancestor.unique_id}</code>
                <span className="text-xs text-muted-foreground"> — {triage.first_suspicious_ancestor.reason}</span>
              </div>
            )}
            <div>
              <div className="text-xs font-medium text-muted-foreground mb-1">Blast radius</div>
              <p className="text-xs">
                {triage.blast_radius.models.length} downstream models, {triage.blast_radius.exposures.length} exposures,
                {" "}{triage.blast_radius.tests} tests{triage.blast_radius.tasks.length ? `, ${triage.blast_radius.tasks.length} downstream tasks` : ""}
              </p>
            </div>
            <div className="border-t border-border pt-3">
              {!rcaDone ? (
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Aurora is investigating (read-only). Results stay in Aurora.
                </div>
              ) : (
                <div className="space-y-2">
                  <div className="text-xs font-medium text-muted-foreground">Root cause analysis</div>
                  <pre className="rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-words max-h-[480px] overflow-auto">
                    {status?.rca.final_message ?? "The investigation finished without a final answer."}
                  </pre>
                </div>
              )}
              {status?.incident && (
                <Link href={`/incidents/${status.incident.id}`} className="text-xs underline">
                  Open the investigation
                </Link>
              )}
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
