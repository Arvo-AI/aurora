"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Loader2, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Finding = {
  severity: "HIGH" | "MEDIUM" | "LOW";
  file_path: string;
  line?: number | null;
  title: string;
  explanation: string;
  in_diff?: boolean;
};

type ReviewResult = {
  verdict: "SAFE" | "RISKY";
  summary: string;
  findings: Finding[];
  scope_note: string;
  session_id?: string | null;
  duration_seconds?: number;
  source: { kind: "public_pr" | "diff"; repo?: string; pr_number?: number; title?: string; url?: string };
  transparency: { read: string[]; stored: string[]; not_stored: string[] };
};

type JobState =
  | { status: "queued" }
  | { status: "running"; stage?: "fetching" | "reviewing" }
  | { status: "completed"; result: ReviewResult }
  | { status: "failed"; error: { code: string; message: string } };

// A realistic infrastructure change so a first-time visitor can get a result with zero input.
const SAMPLE_DIFF = `diff --git a/deploy/api-deployment.yaml b/deploy/api-deployment.yaml
--- a/deploy/api-deployment.yaml
+++ b/deploy/api-deployment.yaml
@@ -5,17 +5,12 @@ metadata:
   name: api
 spec:
-  replicas: 6
+  replicas: 1
   template:
     spec:
       containers:
       - name: api
         image: registry.example.com/api:latest
-        readinessProbe:
-          httpGet: {path: /healthz, port: 8080}
-          periodSeconds: 5
-        resources:
-          requests: {cpu: 500m, memory: 512Mi}
+        env:
+        - name: DB_PASSWORD
+          value: "hunter2"
`;

const POLL_MS = 2000;
const MAX_POLLS = 300; // ~10 minutes

const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  fetching: "Reading the change",
  reviewing: "Reviewing for incident risk (usually a minute or two)",
};

function severityVariant(severity: Finding["severity"]) {
  if (severity === "HIGH") return "destructive" as const;
  if (severity === "MEDIUM") return "default" as const;
  return "secondary" as const;
}

export default function TryReviewPage() {
  const [tab, setTab] = useState<"url" | "diff">("url");
  const [prUrl, setPrUrl] = useState("");
  const [diff, setDiff] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [job, setJob] = useState<JobState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const polls = useRef(0);

  const submit = useCallback(async () => {
    setJobId(null);
    setJob(null);
    setError(null);
    polls.current = 0;
    setSubmitting(true);
    try {
      const body = tab === "url" ? { pr_url: prUrl.trim() } : { diff };
      const response = await fetch("/api/change-gating/try", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        setError(data?.error?.message || "Could not start the review. Try again.");
        return;
      }
      setJobId(data.job_id);
      setJob({ status: "queued" });
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setSubmitting(false);
    }
  }, [tab, prUrl, diff]);

  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      polls.current += 1;
      if (polls.current > MAX_POLLS) {
        clearInterval(timer);
        setError("The review is taking longer than expected. Check your chat history or try again.");
        return;
      }
      try {
        const response = await fetch(`/api/change-gating/try/${jobId}`, { cache: "no-store" });
        if (!response.ok) return; // transient; keep polling
        const state = (await response.json()) as JobState;
        if (cancelled) return;
        setJob(state);
        if (state.status === "completed" || state.status === "failed") clearInterval(timer);
      } catch {
        // network blip: next tick retries
      }
    }, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [jobId]);

  const running = job?.status === "queued" || job?.status === "running";
  const canSubmit = !submitting && !running && (tab === "url" ? prUrl.trim().length > 0 : diff.trim().length > 0);

  return (
    <div className="mx-auto w-full max-w-3xl space-y-6 p-6">
      <div className="space-y-2">
        <div className="flex items-center gap-2">
          <ShieldCheck className="h-6 w-6" />
          <h1 className="text-2xl font-semibold">Try a review</h1>
        </div>
        <p className="text-muted-foreground">
          See Aurora review a change for the incidents it could cause. Nothing to connect: no GitHub
          install, no credentials, nothing written back.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>What should Aurora review?</CardTitle>
          <CardDescription>
            A public pull request, or any diff you paste (works for private code too).
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <Tabs value={tab} onValueChange={(value) => setTab(value as "url" | "diff")}>
            <TabsList>
              <TabsTrigger value="url">Public PR URL</TabsTrigger>
              <TabsTrigger value="diff">Paste a diff</TabsTrigger>
            </TabsList>
            <TabsContent value="url" className="space-y-2">
              <Input
                placeholder="https://github.com/owner/repo/pull/123"
                value={prUrl}
                onChange={(event) => setPrUrl(event.target.value)}
                aria-label="Public pull request URL"
              />
              <p className="text-xs text-muted-foreground">
                Aurora reads the PR the same way a signed-out visitor would. Merged and closed PRs
                work too, so you can replay a change that caused an incident.
              </p>
            </TabsContent>
            <TabsContent value="diff" className="space-y-2">
              <Textarea
                placeholder="Paste the output of: git diff main...HEAD"
                rows={10}
                className="font-mono text-xs"
                value={diff}
                onChange={(event) => setDiff(event.target.value)}
                aria-label="Unified diff"
              />
              <div className="flex items-center justify-between">
                <p className="text-xs text-muted-foreground">Up to about 300 KB.</p>
                <Button type="button" variant="ghost" size="sm" onClick={() => setDiff(SAMPLE_DIFF)}>
                  Use a sample diff
                </Button>
              </div>
            </TabsContent>
          </Tabs>

          <Button onClick={submit} disabled={!canSubmit}>
            {submitting || running ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
            Review for incident risk
          </Button>
          {error ? <p className="text-sm text-destructive">{error}</p> : null}
        </CardContent>
      </Card>

      {running ? (
        <Card>
          <CardContent className="flex items-center gap-3 py-6">
            <Loader2 className="h-5 w-5 animate-spin" />
            <span>
              {STAGE_LABEL[job?.status === "running" ? job.stage || "queued" : "queued"] || "Working"}
            </span>
          </CardContent>
        </Card>
      ) : null}

      {job?.status === "failed" ? (
        <Card>
          <CardContent className="py-6">
            <p className="text-sm text-destructive">{job.error.message}</p>
            {job.error.code === "not_found_or_private" ? (
              <Button className="mt-3" variant="outline" size="sm" onClick={() => setTab("diff")}>
                Paste the diff instead
              </Button>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {job?.status === "completed" ? <ResultView result={job.result} /> : null}
    </div>
  );
}

function ResultView({ result }: { result: ReviewResult }) {
  const risky = result.verdict === "RISKY";
  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-3">
            <Badge variant={risky ? "destructive" : "secondary"}>{result.verdict}</Badge>
            <CardTitle className="text-lg">
              {result.source.kind === "public_pr" && result.source.url ? (
                <a href={result.source.url} target="_blank" rel="noopener noreferrer" className="hover:underline">
                  {result.source.repo}#{result.source.pr_number}
                </a>
              ) : (
                "Pasted diff"
              )}
            </CardTitle>
          </div>
          {result.source.title ? <CardDescription>{result.source.title}</CardDescription> : null}
        </CardHeader>
        <CardContent className="space-y-4">
          <p>{result.summary}</p>

          {result.findings.map((finding, index) => (
            <div key={`${finding.file_path}-${finding.line}-${index}`} className="rounded-md border p-3 space-y-1">
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={severityVariant(finding.severity)}>{finding.severity}</Badge>
                <span className="font-medium">{finding.title}</span>
              </div>
              <p className="font-mono text-xs text-muted-foreground">
                {finding.file_path}
                {finding.line != null ? `:${finding.line}` : ""}
                {finding.in_diff === false ? " (line not in the diff)" : ""}
              </p>
              <p className="text-sm">{finding.explanation}</p>
            </div>
          ))}

          <p className="text-xs text-muted-foreground">{result.scope_note}</p>

          <details className="text-sm">
            <summary className="cursor-pointer font-medium">What was read and stored</summary>
            <div className="mt-2 space-y-2 text-muted-foreground">
              <div>
                <p className="font-medium text-foreground">Read</p>
                <ul className="list-disc pl-5">
                  {result.transparency.read.map((item) => (
                    <li key={item} className="font-mono text-xs">{item}</li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="font-medium text-foreground">Stored</p>
                <ul className="list-disc pl-5">
                  {result.transparency.stored.map((item) => (<li key={item}>{item}</li>))}
                </ul>
              </div>
              <div>
                <p className="font-medium text-foreground">Not stored or sent</p>
                <ul className="list-disc pl-5">
                  {result.transparency.not_stored.map((item) => (<li key={item}>{item}</li>))}
                </ul>
              </div>
            </div>
          </details>

          {result.session_id ? (
            <Link href={`/chat?sessionId=${result.session_id}`} className="text-sm underline">
              Open the full investigation
            </Link>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Want this on every pull request?</CardTitle>
          <CardDescription>
            Install the GitHub app and Aurora reviews each PR automatically, with full repository
            context. It still needs nothing else connected.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button asChild variant="outline">
            <Link href="/connectors">Set up automatic reviews</Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
