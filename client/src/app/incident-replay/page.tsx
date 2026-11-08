"use client";

import { useCallback, useEffect, useState } from "react";

type Record_ = {
  id: number;
  source: string;
  locator: string;
  record_type: string;
  at: string | null;
  content: string;
};

type Finding = {
  index: number;
  claim_type: string;
  claim: string;
  cited: number[];
  status: string;
  reasons: string[];
};

type Validation = {
  findings: Finding[];
  status_counts: Record<string, number>;
  unknown_claim_types: string[];
  missing_ids: number[];
  total_citations: number;
  resolved_citations: number;
  citation_resolution_rate: number;
  evidence_type_validity: number;
  timeline_ok: boolean;
  timeline_problems: string[];
  timeline_entries: number;
  timeline_evidenced: number;
  narration: { source: string; verified: boolean; note: string };
};

type Investigation = {
  incident_summary: string;
  timeline: { at: string; event: string; evidence_ids: number[] }[];
  findings: { claim_type: string; claim: string; evidence_ids: number[]; confidence: string }[];
  unverified_hypotheses: string[];
  missing_evidence: string[];
};

type RunResponse = {
  replay_id: string;
  bundle: string;
  model: string;
  elapsed_s: number;
  evidence: { name: string; records: Record_[]; warnings: string[] };
  investigation: Investigation;
  validation: Validation;
  error?: string;
};

const STATUS_STYLE: Record<string, { label: string; className: string }> = {
  verified_observation: { label: "Verified observation", className: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  supported_inference: { label: "Supported inference", className: "bg-sky-500/15 text-sky-300 border-sky-500/30" },
  unverified_hypothesis: { label: "Unverified hypothesis", className: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  invalid_citation: { label: "Invalid citation", className: "bg-rose-500/15 text-rose-300 border-rose-500/30" },
};

function StatusBadge({ status }: Readonly<{ status: string }>) {
  const style = STATUS_STYLE[status] ?? { label: status, className: "bg-neutral-700/40 text-neutral-300 border-neutral-600" };
  return (
    <span className={`rounded border px-2 py-0.5 text-[11px] font-medium ${style.className}`}>
      {style.label}
    </span>
  );
}

function Metric({ label, value, hint }: Readonly<{ label: string; value: string; hint?: string }>) {
  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/60 px-4 py-3">
      <div className="text-[11px] uppercase tracking-wide text-neutral-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-neutral-100">{value}</div>
      {hint ? <div className="mt-0.5 text-[11px] text-neutral-500">{hint}</div> : null}
    </div>
  );
}

export default function IncidentReplayPage() {
  const [bundles, setBundles] = useState<string[]>([]);
  const [bundle, setBundle] = useState<string>("checkout-api");
  const [running, setRunning] = useState(false);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());
  const [data, setData] = useState<RunResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/incident-replay/bundles")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((j) => {
        setBundles(j.bundles ?? []);
        if (j.bundles?.length) setBundle(j.bundles[0]);
      })
      .catch(() => setBundles([]));
  }, []);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(timer);
  }, [running]);

  const run = useCallback(async () => {
    setRunning(true);
    setError(null);
    setData(null);
    setStartedAt(Date.now());
    setNow(Date.now());
    try {
      const res = await fetch("/api/incident-replay/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ bundle }),
      });
      const payload = await res.json().catch(() => null);
      if (!res.ok) {
        setError(payload?.error ?? `Request failed (${res.status})`);
        return;
      }
      setData(payload as RunResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Request failed");
    } finally {
      setRunning(false);
    }
  }, [bundle]);

  const records = data?.evidence.records ?? [];
  const elapsedShown = running && startedAt ? ((now - startedAt) / 1000).toFixed(0) : null;

  return (
    <div className="mx-auto max-w-5xl px-6 py-10 text-neutral-200">
      <header className="mb-8">
        <h1 className="text-2xl font-semibold text-neutral-50">Evidence-verified RCA</h1>
        <p className="mt-2 max-w-3xl text-sm text-neutral-400">
          Run a real Aurora investigation against a bundled incident evidence package. No cloud,
          Kubernetes or GitHub connection is required, and the model is given no Aurora tools.
          After it answers, every citation is checked in code against the bundle: a claim that
          cites evidence of the wrong type, or an identifier that does not exist, is refused, and
          a causal hypothesis is never reported as a fact.
        </p>
      </header>

      <section className="mb-8 flex flex-wrap items-end gap-4 rounded-xl border border-neutral-800 bg-neutral-900/50 p-5">
        <div>
          <label htmlFor="bundle" className="block text-[11px] uppercase tracking-wide text-neutral-500">
            Evidence bundle
          </label>
          <select
            id="bundle"
            value={bundle}
            onChange={(event) => setBundle(event.target.value)}
            disabled={running}
            className="mt-1 min-w-56 rounded-md border border-neutral-700 bg-neutral-950 px-3 py-2 text-sm text-neutral-200"
          >
            {(bundles.length ? bundles : ["checkout-api"]).map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={run}
          disabled={running}
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500 disabled:opacity-50"
        >
          {running ? `Investigatingâ€¦ ${elapsedShown ?? 0}s` : "Run investigation"}
        </button>
        <p className="text-[11px] text-neutral-500">
          Uses the deployment&apos;s configured model. The call typically takes 30â€“60s.
        </p>
      </section>

      {error ? (
        <div className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-4 py-3 text-sm text-rose-200">
          {error}
        </div>
      ) : null}

      {data ? (
        <div className="space-y-8">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Metric label="Model" value={data.model} />
            <Metric label="Elapsed" value={`${data.elapsed_s}s`} />
            <Metric
              label="Citation resolution"
              value={`${Math.round(data.validation.citation_resolution_rate * 100)}%`}
              hint={`${data.validation.resolved_citations}/${data.validation.total_citations} ids exist`}
            />
            <Metric
              label="Evidence type validity"
              value={`${Math.round(data.validation.evidence_type_validity * 100)}%`}
              hint="claim cites the right record kind"
            />
            <Metric
              label="Timeline"
              value={data.validation.timeline_ok ? "ordered" : "problem"}
              hint={`${data.validation.timeline_evidenced}/${data.validation.timeline_entries} entries evidenced`}
            />
          </div>

          <section>
            <h2 className="mb-2 flex items-baseline gap-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
              Summary
              <span className="rounded border border-amber-500/30 bg-amber-500/10 px-1.5 py-0.5 text-[10px] font-normal normal-case tracking-normal text-amber-300">
                model narrative — not verified
              </span>
            </h2>
            <p className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4 text-sm leading-relaxed text-neutral-200">
              {data.investigation.incident_summary}
            </p>
            {(data.validation.status_counts.unverified_hypothesis ?? 0) > 0 ? (
              <p className="mt-2 text-[11px] text-amber-300/90">
                This narrative is the model&apos;s own account and is not checked.{" "}
                {data.validation.status_counts.unverified_hypothesis} claim
                {data.validation.status_counts.unverified_hypothesis === 1 ? "" : "s"} below could not
                be supported by the evidence, so a cause stated here may be one of them.
              </p>
            ) : null}
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
              Findings <span className="font-normal text-neutral-500">â€” {data.validation.findings.length}, each checked against the bundle</span>
            </h2>
            <ul className="space-y-3">
              {data.validation.findings.map((finding) => (
                <li key={`${finding.claim_type}:${finding.claim}`} className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-4">
                  <div className="mb-2 flex flex-wrap items-center gap-2">
                    <StatusBadge status={finding.status} />
                    <span className="text-[11px] uppercase tracking-wide text-neutral-500">
                      {finding.claim_type}
                    </span>
                    {finding.cited.map((id) => (
                      <a
                        key={id}
                        href={`#evidence-${id}`}
                        className="rounded bg-neutral-800 px-1.5 py-0.5 text-[11px] text-sky-300 hover:bg-neutral-700"
                      >
                        [{id}]
                      </a>
                    ))}
                  </div>
                  <p className="text-sm text-neutral-200">{finding.claim}</p>
                  <ul className="mt-2 space-y-1">
                    {finding.reasons.map((reason, index) => (
                      <li key={reason} className="text-[11px] text-neutral-500">
                        · {reason}
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ul>
          </section>

          {data.investigation.unverified_hypotheses?.length ? (
            <section>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
                Still unproven
              </h2>
              <ul className="space-y-1 rounded-lg border border-amber-500/20 bg-amber-500/5 p-4 text-sm text-amber-100/90">
                {data.investigation.unverified_hypotheses.map((item, index) => (
                  <li key={item}>· {item}</li>
                ))}
              </ul>
            </section>
          ) : null}

          {data.investigation.missing_evidence?.length ? (
            <section>
              <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
                Evidence that would settle it
              </h2>
              <ul className="space-y-1 rounded-lg border border-neutral-800 bg-neutral-900/40 p-4 text-sm text-neutral-300">
                {data.investigation.missing_evidence.map((item, index) => (
                  <li key={item}>· {item}</li>
                ))}
              </ul>
            </section>
          ) : null}

          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
              Timeline
            </h2>
            <ol className="space-y-1 border-l border-neutral-800 pl-4">
              {data.investigation.timeline.map((entry, index) => (
                <li key={`${entry.at}:${entry.event}`} className="text-sm text-neutral-300">
                  <span className="mr-2 font-mono text-[11px] text-neutral-500">{entry.at}</span>
                  {entry.event}
                  {entry.evidence_ids?.map((id) => (
                    <a key={id} href={`#evidence-${id}`} className="ml-1 text-[11px] text-sky-400">
                      [{id}]
                    </a>
                  ))}
                </li>
              ))}
            </ol>
          </section>

          <section>
            <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-neutral-400">
              Evidence bundle <span className="font-normal text-neutral-500">â€” {records.length} records</span>
            </h2>
            <div className="max-h-96 overflow-auto rounded-lg border border-neutral-800">
              <table className="w-full text-left text-[12px]">
                <thead className="sticky top-0 bg-neutral-900 text-[11px] uppercase tracking-wide text-neutral-500">
                  <tr>
                    <th className="px-3 py-2">#</th>
                    <th className="px-3 py-2">Type</th>
                    <th className="px-3 py-2">UTC</th>
                    <th className="px-3 py-2">Source</th>
                    <th className="px-3 py-2">Content</th>
                  </tr>
                </thead>
                <tbody>
                  {records.map((record) => (
                    <tr key={record.id} id={`evidence-${record.id}`} className="border-t border-neutral-800/70">
                      <td className="px-3 py-1.5 font-mono text-neutral-500">{record.id}</td>
                      <td className="px-3 py-1.5 text-neutral-400">{record.record_type}</td>
                      <td className="whitespace-nowrap px-3 py-1.5 font-mono text-neutral-500">
                        {record.at ?? "â€”"}
                      </td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-neutral-500">
                        {record.source}:{record.locator}
                      </td>
                      <td className="px-3 py-1.5 text-neutral-300">{record.content}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {data.evidence.warnings?.length ? (
              <ul className="mt-2 text-[11px] text-amber-300">
                {data.evidence.warnings.map((warning, index) => (
                  <li key={warning}>· {warning}</li>
                ))}
              </ul>
            ) : null}
          </section>
        </div>
      ) : null}
    </div>
  );
}



