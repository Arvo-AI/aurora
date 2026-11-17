"use client";

/**
 * Startup Check page — the repo check-engine light.
 *
 * Full-page home for the read-only whole-repo scan: verdict rendered as a
 * gauge (green SAFE / amber NO_MANIFESTS / red RISKY), findings as a
 * severity-grouped risk map with per-finding dismissal ("clear the code").
 * Gated on GitHub being authenticated — without a GitHub credential
 * there is nothing the check can scan.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { ChevronDown, ChevronRight, ExternalLink, GitBranch, Loader2, SearchCheck } from "lucide-react";
import { useGitHubStatus } from "@/hooks/use-github-status";
import { GitHubIntegrationService } from "@/components/github-provider-integration";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

interface StartupFinding {
  id: string;
  rule: string;
  severity: "HIGH" | "MEDIUM" | "LOW";
  source: "baseline" | "context" | "agent";
  file_path: string;
  line: number | null;
  title: string;
  explanation: string;
}

interface ScanResult {
  scan_id: string;
  status: "queued" | "running" | "complete" | "error";
  repo_full_name?: string;
  verdict?: "SAFE" | "RISKY" | "NO_MANIFESTS";
  summary?: string;
  error?: string;
  counts?: { high: number; medium: number; low: number };
  findings?: StartupFinding[];
  dismissed?: string[];
  scanned_file_count?: number;
  scanned_manifests?: number;
  context_applied?: boolean;
  branch?: string;
  context_refused?: { name: string; reason: string }[];
  agent_used?: boolean;
  agent_model?: string | null;
  agent_summary?: string;
  agent_observations?: StartupFinding[];
  agent_error?: string | null;
}

const POLL_INTERVAL_MS = 2000;
const SEVERITY_ORDER = { HIGH: 0, MEDIUM: 1, LOW: 2 } as const;

/**
 * Verdict gauge — an SVG arc, needle-free: the lit segments ARE the findings.
 * For RISKY, the arc fills with severity-colored segments proportional to
 * the HIGH/MEDIUM/LOW counts (red high → amber medium → slate low), so the
 * fill level and colors communicate the mix at a glance. Full green =
 * checked and clean; half amber = nothing to check.
 */
function VerdictGauge({
  verdict,
  counts,
}: {
  verdict: ScanResult["verdict"];
  counts?: { high: number; medium: number; low: number };
}) {
  if (!verdict) return null;
  const R = 80;
  const CIRC = Math.PI * R; // half-circle length
  const ARC = `M 30 110 A ${R} ${R} 0 0 1 190 110`;
  const MIN_SEG = CIRC * 0.1; // a single finding still shows as a visible sliver

  const label =
    verdict === "RISKY" ? "Check Engine light ON" : verdict === "SAFE" ? "Check Engine light OFF" : "Nothing to check";
  const sub =
    verdict === "RISKY"
      ? "Incident preconditions found — see the risk map"
      : verdict === "SAFE"
        ? "Checked and clean"
        : "No Kubernetes manifests in this repo";

  // Severity-proportional segments: HIGH leftmost, then MEDIUM, then LOW.
  const segs: { color: string; len: number }[] = [];
  if (verdict === "RISKY" && counts) {
    const total = (counts.high || 0) + (counts.medium || 0) + (counts.low || 0);
    const raw = [
      { color: "var(--destructive)", count: counts.high || 0 }, // red
      { color: "#f59e0b", count: counts.medium || 0 }, // amber
      { color: "#94a3b8", count: counts.low || 0 }, // slate
    ];
    if (total > 0) {
      let lens = raw.map((s) => (s.count > 0 ? Math.max((s.count / total) * CIRC, MIN_SEG) : 0));
      const sum = lens.reduce((a, b) => a + b, 0);
      if (sum > CIRC) lens = lens.map((l) => (l / sum) * CIRC); // mins overflowed — rescale
      raw.forEach((s, i) => {
        if (s.count > 0) segs.push({ color: s.color, len: lens[i] });
      });
    }
  }
  let cursor = 0; // distance along the arc where the next segment starts

  return (
    <div className="flex flex-col items-center gap-2">
      <svg width="220" height="130" viewBox="0 0 220 130" role="img" aria-label={label}>
        {/* track */}
        <path d={ARC} fill="none" stroke="currentColor" strokeWidth="14" strokeLinecap="round" className="text-muted" />
        {verdict === "SAFE" && (
          <path
            d={ARC}
            fill="none"
            stroke="#22c55e"
            strokeWidth="14"
            strokeLinecap="round"
            strokeDasharray={`${CIRC} ${CIRC}`}
            style={{ transition: "stroke-dashoffset 900ms ease-out" }}
          />
        )}
        {verdict === "NO_MANIFESTS" && (
          <path
            d={ARC}
            fill="none"
            stroke="#eab308"
            strokeWidth="14"
            strokeLinecap="round"
            strokeDasharray={`${CIRC} ${CIRC}`}
            strokeDashoffset={CIRC / 2}
            style={{ transition: "stroke-dashoffset 900ms ease-out" }}
          />
        )}
        {segs.map((seg, i) => {
          const offset = -cursor; // negative offset slides the dash start forward along the arc
          cursor += seg.len;
          return (
            <path
              key={i}
              d={ARC}
              fill="none"
              stroke={seg.color}
              strokeWidth="14"
              strokeLinecap={i === 0 || cursor >= CIRC - 0.5 ? "round" : "butt"}
              strokeDasharray={`${seg.len} ${CIRC + seg.len}`}
              strokeDashoffset={offset}
              style={{ transition: "stroke-dashoffset 900ms ease-out" }}
            />
          );
        })}
      </svg>
      <p className="text-lg font-semibold">{label}</p>
      <p className="text-xs text-muted-foreground">{sub}</p>
    </div>
  );
}

function SeverityBadge({ severity }: { severity: StartupFinding["severity"] }) {
  const variant =
    severity === "HIGH" ? "destructive" : severity === "MEDIUM" ? "default" : "secondary";
  return <Badge variant={variant}>{severity}</Badge>;
}

function sourceTag(f: StartupFinding): string {
  if (f.source === "context") return "from your facts";
  if (f.source === "agent") return "advisory";
  return "";
}

function githubFileUrl(
  repo: string | undefined,
  branch: string | undefined,
  f: StartupFinding
): string | null {
  if (!repo || !branch) return null;
  return `https://github.com/${repo}/blob/${branch}/${f.file_path}${f.line ? `#L${f.line}` : ""}`;
}

function FindingCard({
  f,
  fileUrl,
  onDismiss,
}: {
  f: StartupFinding;
  fileUrl: string | null;
  onDismiss?: (id: string) => void;
}) {
  return (
    <div className="rounded-lg border bg-card p-4 space-y-2">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2 min-w-0">
          <SeverityBadge severity={f.severity} />
          <span className="text-sm font-medium">{f.title}</span>
        </div>
        {/* Clearing a code without fixing the fault hides a real risk —
            destructive styling says so. Agent observations are advisory and
            get no such button. */}
        {onDismiss && (
          <Button
            size="sm"
            variant="destructive"
            className="h-7 px-3 text-xs shrink-0"
            onClick={() => onDismiss(f.id)}
          >
            Clear code
          </Button>
        )}
      </div>
      <p className="text-xs text-muted-foreground">{f.explanation}</p>
      {fileUrl ? (
        <a
          href={fileUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1.5 font-mono text-[11px] text-red-400 hover:text-red-300 hover:underline"
          title="Open this file at the offending line on GitHub"
        >
          <ExternalLink size={12} />
          <span className="truncate">
            {f.file_path}
            {f.line ? `:${f.line}` : ""}
          </span>
          <span className="font-sans text-[10px] uppercase tracking-wide text-muted-foreground">
            step into
          </span>
        </a>
      ) : (
        <p className="font-mono text-[11px] text-muted-foreground">
          {f.file_path}
          {f.line ? `:${f.line}` : ""}
        </p>
      )}
      <p className="text-[11px] text-muted-foreground">
        {f.rule}
        {sourceTag(f) ? ` · ${sourceTag(f)}` : ""}
      </p>
    </div>
  );
}

export default function StartupCheckPage() {
  const { isAuthenticated: githubAuthed } = useGitHubStatus();
  const [repoFullName, setRepoFullName] = useState("");
  const [connectedRepos, setConnectedRepos] = useState<string[]>([]);
  const [picker, setPicker] = useState<string>("");
  const [scanId, setScanId] = useState<string | null>(null);
  const [result, setResult] = useState<ScanResult | null>(null);
  const [isStarting, setIsStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showContext, setShowContext] = useState(false);

  const [nodeCpu, setNodeCpu] = useState("");
  const [nodeMemory, setNodeMemory] = useState("");
  const [serviceMemory, setServiceMemory] = useState("");
  const [storageClasses, setStorageClasses] = useState("");
  const [clusterDomain, setClusterDomain] = useState("");
  // Free-form facts the agent interprets (and refuses if gibberish).
  const [facts, setFacts] = useState<{ name: string; value: string }[]>([]);
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (pollTimer.current) clearTimeout(pollTimer.current);
    },
    []
  );

  useEffect(() => {
    let mounted = true;
    GitHubIntegrationService.fetchRepoSelections()
      .then((repos) => {
        if (mounted) setConnectedRepos(repos.map((r) => r.repo_full_name));
      })
      .catch(() => {});
    return () => {
      mounted = false;
    };
  }, []);

  const buildContext = useCallback((): Record<string, unknown> => {
    const context: Record<string, unknown> = {};
    const cpu = parseFloat(nodeCpu);
    const mem = parseFloat(nodeMemory);
    if (!Number.isNaN(cpu) && cpu > 0 && !Number.isNaN(mem) && mem > 0) {
      context.nodes = [{ name: "nodes", cpu_cores: cpu, memory_mb: mem }];
    }
    const services: Record<string, number> = {};
    for (const part of serviceMemory.split(/[;,\n]/)) {
      const [name, mb] = part.split(/[:=]/).map((s) => s?.trim());
      const value = parseFloat(mb);
      if (name && !Number.isNaN(value) && value > 0) services[name] = value;
    }
    if (Object.keys(services).length > 0) context.service_memory_mb = services;
    const classes = storageClasses
      .split(/[;,\s]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (classes.length > 0) context.storage_classes = classes;
    if (clusterDomain.trim()) context.cluster_domain = clusterDomain.trim();
    return context;
  }, [nodeCpu, nodeMemory, serviceMemory, storageClasses, clusterDomain]);

  const poll = useCallback(async (id: string) => {
    try {
      const resp = await fetch(`/api/proxy/github/startup-check/scans/${id}`);
      const data: ScanResult = await resp.json();
      if (!resp.ok) throw new Error(data?.error || "Scan not found");
      setResult(data);
      if (data.status === "queued" || data.status === "running") {
        pollTimer.current = setTimeout(() => poll(id), POLL_INTERVAL_MS);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to poll scan");
    }
  }, []);

  const runScan = useCallback(async () => {
    setError(null);
    setResult(null);
    if (!/^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(repoFullName.trim())) {
      setError("Enter a repository as owner/repo");
      return;
    }
    setIsStarting(true);
    try {
      const resp = await fetch("/api/proxy/github/startup-check/scans", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          repo_full_name: repoFullName.trim(),
          context: buildContext(),
          freeform_facts: facts
            .map((f) => ({ name: f.name.trim(), value: f.value.trim() }))
            .filter((f) => f.name || f.value),
        }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data?.error || "Failed to start scan");
      setScanId(data.scan_id);
      pollTimer.current = setTimeout(() => poll(data.scan_id), POLL_INTERVAL_MS);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to start scan");
    } finally {
      setIsStarting(false);
    }
  }, [repoFullName, buildContext, poll, facts]);

  const dismissFinding = useCallback(
    async (findingId: string) => {
      if (!scanId || !result) return;
      const dismissed = [...(result.dismissed || []), findingId];
      try {
        const resp = await fetch(`/api/proxy/github/startup-check/scans/${scanId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ dismissed }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data?.error || "Failed to dismiss");
        setResult(data);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to dismiss");
      }
    },
    [scanId, result]
  );

  const isBusy = isStarting || result?.status === "queued" || result?.status === "running";
  const dismissedSet = new Set(result?.dismissed || []);
  const visibleFindings = (result?.findings || [])
    .filter((f) => !dismissedSet.has(f.id))
    .sort(
      (a, b) => SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity]
    );

  // -- gate -------------------------------------------------------------
  if (!githubAuthed) {
    return (
      <div className="max-w-2xl mx-auto py-16 px-4 text-center space-y-4">
        <SearchCheck size={40} className="mx-auto text-muted-foreground" />
        <h1 className="text-xl font-semibold">Startup Check</h1>
        <p className="text-sm text-muted-foreground">
          The startup check reads a repository through your GitHub
          connection — connect GitHub first, then come back to run a scan.
        </p>
        <Button asChild>
          <Link href="/connectors">Go to Connectors</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto py-8 px-4 sm:px-6 space-y-8">
      {/* Header */}
      <div className="space-y-1">
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <SearchCheck size={20} /> Startup Check
        </h1>
        <p className="text-sm text-muted-foreground">
          A read-only &ldquo;check engine light&rdquo; for a repository: it
          scans the repo&rsquo;s Kubernetes manifests for the preconditions
          of known incident classes — before they reach a cluster. Uses
          nothing but your GitHub read access.
        </p>
      </div>

      {/* Run bar */}
      <div className="rounded-lg border bg-card p-5 space-y-4">
        <div className="flex flex-col sm:flex-row gap-2">
          {connectedRepos.length > 0 && (
            <select
              value={picker}
              onChange={(e) => {
                const v = e.target.value;
                setPicker(v);
                setRepoFullName(v === "__other__" ? "" : v);
              }}
              className="text-sm h-10 rounded-md border border-input bg-transparent px-3"
              aria-label="Repository"
            >
              <option value="" disabled>
                Connected repositories…
              </option>
              {connectedRepos.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
              <option value="__other__">Other repository (type owner/repo)…</option>
            </select>
          )}
          {(picker === "__other__" || connectedRepos.length === 0) && (
            <Input
              placeholder="owner/repo"
              value={repoFullName}
              onChange={(e) => setRepoFullName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !isBusy) runScan();
              }}
              className="text-sm h-10"
              aria-label="Repository owner/repo"
            />
          )}
          <Button onClick={runScan} disabled={isBusy} className="h-10 px-6">
            {isBusy ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Scanning…
              </>
            ) : (
              "Run check"
            )}
          </Button>
        </div>

        <button
          type="button"
          onClick={() => setShowContext((v) => !v)}
          className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
        >
          {showContext ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          Optional: tell it about your environment (sharper checks, zero
          guessing)
        </button>

        {showContext && (
          <div className="grid sm:grid-cols-2 gap-4 pt-1">
            <div className="space-y-1">
              <Label className="text-xs">
                Largest node (cpu cores / memory MB)
              </Label>
              <div className="flex gap-2">
                <Input placeholder="16" inputMode="decimal" value={nodeCpu} onChange={(e) => setNodeCpu(e.target.value)} />
                <Input placeholder="65536" inputMode="decimal" value={nodeMemory} onChange={(e) => setNodeMemory(e.target.value)} />
              </div>
            </div>
            <div className="space-y-1">
              <Label className="text-xs">
                Measured service memory (name: MB, comma-separated)
              </Label>
              <Input placeholder="worker: 150, api: 90" value={serviceMemory} onChange={(e) => setServiceMemory(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">StorageClasses in your cluster</Label>
              <Input placeholder="standard, fast-ssd" value={storageClasses} onChange={(e) => setStorageClasses(e.target.value)} />
            </div>
            <div className="space-y-1">
              <Label className="text-xs">Cluster domain</Label>
              <Input placeholder="cluster.local" value={clusterDomain} onChange={(e) => setClusterDomain(e.target.value)} />
            </div>

            {/* Free-form facts: the agent interprets these — gibberish and
                unusable facts come back refused with a reason. */}
            <div className="sm:col-span-2 space-y-2">
              <div className="flex items-center justify-between">
                <Label className="text-xs">
                  Anything else the agent should know (it interprets — or refuses)
                </Label>
                <Button
                  type="button"
                  size="sm"
                  variant="outline"
                  className="h-7 px-3 text-xs"
                  disabled={facts.length >= 10}
                  onClick={() => setFacts((f) => [...f, { name: "", value: "" }])}
                >
                  + Add fact
                </Button>
              </div>
              {facts.map((fact, i) => (
                <div key={i} className="flex gap-2">
                  <Input
                    className="w-1/3"
                    placeholder="e.g. largest node"
                    value={fact.name}
                    onChange={(e) =>
                      setFacts((f) => f.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))
                    }
                  />
                  <Input
                    placeholder="e.g. prod nodes are 8 cores / 32 GB, our Secrets live in Vault…"
                    value={fact.value}
                    onChange={(e) =>
                      setFacts((f) => f.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))
                    }
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    className="h-9 px-2 shrink-0"
                    aria-label="Remove fact"
                    onClick={() => setFacts((f) => f.filter((_, j) => j !== i))}
                  >
                    ×
                  </Button>
                </div>
              ))}
              {facts.length > 0 && (
                <p className="text-[11px] text-muted-foreground">
                  The agent maps what it can to the checks above and refuses the
                  rest — refusals are listed in the result, never silently ignored.
                </p>
              )}
            </div>
          </div>
        )}

        {error && (
          <p className="text-sm text-destructive" role="alert">
            {error}
          </p>
        )}
      </div>

      {/* Result */}
      {result && result.status === "error" && (
        <div className="rounded-lg border border-destructive/40 bg-card p-5 text-sm text-destructive">
          {result.error}
        </div>
      )}

      {result && result.status === "complete" && (
        <div className="space-y-6">
          {/* Gauge + stats */}
          <div className="rounded-lg border bg-card p-6">
            <div className="flex flex-col md:flex-row items-center md:items-start gap-8">
              <VerdictGauge verdict={result.verdict} counts={result.counts} />
              <div className="flex-1 space-y-3 min-w-0">
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <GitBranch size={14} />
                  <span className="font-mono truncate">
                    {result.repo_full_name}
                  </span>
                </div>
                <p className="text-sm">{result.summary}</p>
                <div className="flex flex-wrap gap-2 text-xs">
                  {result.verdict === "RISKY" && (
                    <>
                      <Badge variant="destructive">{result.counts?.high ?? 0} high</Badge>
                      <Badge>{result.counts?.medium ?? 0} medium</Badge>
                      <Badge variant="secondary">{result.counts?.low ?? 0} low</Badge>
                    </>
                  )}
                  <Badge variant="outline">
                    {result.scanned_file_count} files · {result.scanned_manifests} manifests
                  </Badge>
                  {result.context_applied && (
                    <Badge variant="outline">with your environment facts</Badge>
                  )}
                </div>
              </div>
            </div>
          </div>

          {/* Refused facts — the agent's judgment, surfaced not buried */}
          {(result.context_refused || []).length > 0 && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/5 p-4 space-y-2">
              <p className="text-xs font-semibold text-amber-500">
                The agent refused {result.context_refused!.length} fact
                {result.context_refused!.length > 1 ? "s" : ""} — not used in this check:
              </p>
              <ul className="space-y-1">
                {result.context_refused!.map((r, i) => (
                  <li key={i} className="text-xs text-muted-foreground">
                    <span className="font-mono">{r.name || "(unnamed)"}</span>
                    {" — "}
                    {r.reason}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Risk map */}
          {visibleFindings.length > 0 && (
            <div className="space-y-3">
              <h2 className="text-sm font-semibold">Risk map</h2>
              {visibleFindings.map((f) => (
                <FindingCard
                  key={f.id}
                  f={f}
                  fileUrl={githubFileUrl(result.repo_full_name, result.branch, f)}
                  onDismiss={dismissFinding}
                />
              ))}
            </div>
          )}

          {/* Agent analysis — advisory Layer 2, never flips the verdict */}
          {result.agent_used && (
            <div className="space-y-3 rounded-lg border border-dashed bg-card/50 p-4">
              <div className="flex items-center gap-2">
                <h2 className="text-sm font-semibold">Agent analysis</h2>
                <Badge variant="outline">advisory</Badge>
                {result.agent_model && (
                  <span className="text-[11px] text-muted-foreground">
                    {result.agent_model}
                  </span>
                )}
              </div>
              {result.agent_summary && (
                <p className="text-sm text-muted-foreground">{result.agent_summary}</p>
              )}
              <p className="text-[11px] text-muted-foreground">
                Read-only reasoning over the raw files (Docker Compose, config,
                CI) — it surfaces what the static K8s rules miss but does not
                change the check-engine verdict.
              </p>
              {(result.agent_observations || []).map((f) => (
                <FindingCard
                  key={f.id}
                  f={f}
                  fileUrl={githubFileUrl(result.repo_full_name, result.branch, f)}
                />
              ))}
              {(result.agent_observations || []).length === 0 && (
                <p className="text-xs text-muted-foreground">No additional concerns.</p>
              )}
            </div>
          )}

          {result.agent_error && (
            <p className="text-xs text-muted-foreground">
              Agent analysis unavailable this run ({result.agent_error}) — the
              deterministic checks still completed.
            </p>
          )}

          {(result.dismissed || []).length > 0 && (
            <p className="text-xs text-muted-foreground">
              {result.dismissed!.length} finding
              {result.dismissed!.length > 1 ? "s" : ""} cleared — treated as
              handled for this scan.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
