"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Image from "next/image";
import { ArrowUpRight, Check, Database, FileText, Loader2, Play, RotateCcw, X } from "lucide-react";
import { parseCases, parseInvestigation, readSandboxResponse } from "./contract";
import type { Evidence, Finding, Investigation, SampleCase } from "./contract";
import styles from "./sandbox.module.css";
import ChallengePanel from "./ChallengePanel";

type Run = { status: "idle" | "running" | "complete" | "error"; started?: number; result?: Investigation; error?: string };

function EvidenceDialog({ evidence, onClose }: { evidence: Evidence | null; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (evidence && !dialog.current?.open) dialog.current?.showModal();
    if (!evidence && dialog.current?.open) dialog.current.close();
  }, [evidence]);
  return (
    <dialog ref={dialog} className={styles.dialog} aria-labelledby="evidence-title" onCancel={onClose} onClose={onClose}>
      {evidence && <>
        <div className={styles.dialogHeader}>
          <div><span className={styles.eyebrow}>{evidence.id} / {evidence.kind}</span><h2 id="evidence-title">{evidence.title}</h2></div>
          <button className={styles.iconButton} onClick={onClose} aria-label="Close evidence"><X size={20} /></button>
        </div>
        <pre className={styles.evidenceContent}>{evidence.content}</pre>
        <p className={styles.dialogFoot}>Synthetic sample evidence. No customer systems are connected.</p>
      </>}
    </dialog>
  );
}

export default function SandboxPage() {
  const [cases, setCases] = useState<SampleCase[]>([]);
  const [loadError, setLoadError] = useState("");
  const [runs, setRuns] = useState<Record<string, Run>>({});
  const [selectedEvidence, setSelectedEvidence] = useState<Evidence | null>(null);
  const [now, setNow] = useState(Date.now());
  const controllers = useRef(new Map<string, AbortController>());
  const running = Object.values(runs).some(run => run.status === "running");
  const completed = Object.values(runs).filter(run => run.status === "complete").length;

  const loadCases = useCallback(async () => {
    controllers.current.get("cases")?.abort();
    const controller = new AbortController();
    controllers.current.set("cases", controller);
    const timeout = setTimeout(() => controller.abort("timeout"), 15_000);
    setLoadError("");
    try {
      const response = await fetch("/api/sandbox/cases", { signal: controller.signal, cache: "no-store" });
      const samples = parseCases(await readSandboxResponse(response));
      if (controllers.current.get("cases") === controller) setCases(samples);
    } catch (error) {
      if (controllers.current.get("cases") !== controller) return;
      if (controller.signal.aborted && controller.signal.reason !== "timeout") return;
      setLoadError(controller.signal.reason === "timeout"
        ? "Sample evidence took too long to load. Please reload it."
        : error instanceof Error ? error.message : "Sample evidence could not be loaded.");
    } finally {
      clearTimeout(timeout);
      if (controllers.current.get("cases") === controller) controllers.current.delete("cases");
    }
  }, []);

  useEffect(() => {
    void loadCases();
    const activeControllers = controllers.current;
    return () => {
      activeControllers.forEach(controller => controller.abort());
      activeControllers.clear();
    };
  }, [loadCases]);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);

  const runCase = async (sample: SampleCase) => {
    // A synchronous guard also covers a double click before React disables buttons.
    if (controllers.current.has(sample.id)) return;
    const controller = new AbortController();
    controllers.current.set(sample.id, controller);
    setRuns(previous => ({ ...previous, [sample.id]: { status: "running", started: Date.now() } }));
    const timeout = setTimeout(() => controller.abort("timeout"), 100_000);
    try {
      const response = await fetch("/api/sandbox/investigate", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ case_id: sample.id }), signal: controller.signal,
      });
      const data = parseInvestigation(await readSandboxResponse(response), sample);
      if (controllers.current.get(sample.id) !== controller) return;
      setRuns(previous => ({ ...previous, [sample.id]: { status: "complete", result: data } }));
    } catch (error) {
      if (controllers.current.get(sample.id) !== controller) return;
      if (controller.signal.aborted && controller.signal.reason !== "timeout") return;
      const message = controller.signal.reason === "timeout"
        ? "This investigation reached its time limit. You can retry this incident."
        : error instanceof Error ? error.message : "This investigation could not finish.";
      setRuns(previous => ({ ...previous, [sample.id]: { status: "error", error: message } }));
    } finally {
      clearTimeout(timeout);
      if (controllers.current.get(sample.id) === controller) controllers.current.delete(sample.id);
    }
  };

  const closeEvidence = useCallback(() => setSelectedEvidence(null), []);

  function citations(finding: Finding, sample: SampleCase) {
    return <div className={styles.citations}>{[...new Set(finding.evidence_ids)].map(id => {
      const evidence = sample.evidence.find(item => item.id === id);
      return evidence ? <button key={id} onClick={() => setSelectedEvidence(evidence)} title={evidence.title}><FileText size={11} />{id}</button> : null;
    })}</div>;
  }

  return (
    <div className={styles.page}>
      <header className={styles.nav}>
        <a href="https://www.aurorasre.ai/" className={styles.brand}>
          <span className={styles.brandMark}><Image src="/brand/aurora-mark.png" alt="" width={25} height={25} /></span>
          <span className={styles.wordmark}>Aurora <span className={styles.brandByline}>by Arvo AI</span></span>
          <span className={styles.labLabel}>Investigation lab</span>
        </a>
        <a href="https://github.com/Arvo-AI/aurora" target="_blank" rel="noreferrer" className={styles.sourceLink}>Open source <ArrowUpRight size={15} /></a>
      </header>
      <main className={styles.main}>
        <section className={styles.hero}>
          <span className={styles.eyebrow}><span className={styles.dot} /> NO ACCOUNT. NO INFRASTRUCTURE ACCESS.</span>
          <h1>Same error,<br />same tool,<br />different outcomes.</h1>
          <p className={styles.challenge}>Put Aurora to the test.</p>
          <p className={styles.intro}>No infrastructure or environment required. Use the Aurora sandbox to inspect three separate incidents, and verify the evidence supporting each diagnosis.</p>
          <div className={styles.actionRow}>
            <button id="run-comparison" className={styles.primaryButton} disabled={!cases.length || running} onClick={() => cases.forEach(sample => void runCase(sample))}>
              {running ? <Loader2 size={17} className={styles.spin} /> : <Play size={16} fill="currentColor" />}
              {running ? "Investigations in progress" : completed > 0 ? "Run a new comparison" : "Investigate all three"}
            </button>
          </div>
        </section>

        <div className={styles.sharedAlert}><Database size={20} /><div><span className={styles.eyebrow}>THE SHARED ERROR</span><code>Timeout acquiring a database connection</code></div><span className={styles.alertBadge}>checkout-api / connection timeout</span></div>

        {loadError && <div className={styles.loadError} role="alert"><p>{loadError}</p><button onClick={() => void loadCases()}>Reload sample evidence</button></div>}
        {!cases.length && !loadError && <p className={styles.loading}><Loader2 size={16} className={styles.spin} /> Loading sample evidence...</p>}

        <section className={styles.grid} aria-label="Independent investigations">
          {cases.map((sample, index) => {
            const run = runs[sample.id] || { status: "idle" };
            const result = run.result;
            return <article key={sample.id} className={`${styles.card} ${run.status === "complete" ? styles.cardComplete : ""}`} data-testid={sample.id}>
              <div className={styles.cardHeader}><span className={styles.caseNumber}>0{index + 1}</span><h2>{sample.label}</h2><span className={`${styles.status} ${run.status === "complete" ? styles.statusComplete : ""}`}>
                {run.status === "complete" && <Check size={12} />}{run.status === "running" && <Loader2 size={12} className={styles.spin} />}
                {run.status === "idle" ? "Ready" : run.status === "running" ? "Investigating" : run.status === "complete" ? "Result ready" : "Incomplete"}
              </span></div>

              <div className={styles.result} aria-live="polite" aria-atomic="false">
                {run.status === "idle" && <div className={styles.emptyResult}><Database size={28} strokeWidth={1} /><h3>Same symptom. Separate evidence.</h3><p>Six records: logs, metrics, recent changes, and connection traces. Aurora has to establish the cause.</p></div>}
                {run.status === "running" && <div className={styles.emptyResult}><Loader2 size={28} className={styles.spin} /><h3>Examining this incident</h3><p>Aurora can retrieve only this case&apos;s sample evidence.</p><span className={styles.elapsed}>{Math.max(0, Math.floor((now - (run.started || now)) / 1000))}s elapsed</span></div>}
                {run.status === "error" && <div className={styles.errorResult}><h3>Investigation incomplete</h3><p>{run.error}</p><button onClick={() => void runCase(sample)} className={styles.retryButton}><RotateCcw size={14} /> Retry this incident</button></div>}
                {result && <>
                  <span className={styles.eyebrow}>AURORA&apos;S DIAGNOSIS</span><h3 className={styles.diagnosis}>{result.diagnosis.root_cause.explanation}</h3>{citations(result.diagnosis.root_cause, sample)}
                  <div className={styles.findings}>{result.diagnosis.observations.map((finding, i) => <div key={i}><p>{finding.explanation}</p>{citations(finding, sample)}</div>)}</div>
                  <details className={styles.details}><summary>Alternative explanation</summary><p>{result.diagnosis.alternative.explanation}</p>{citations(result.diagnosis.alternative, sample)}</details>
                  <details className={styles.details}><summary>Uncertainty &amp; next check</summary><p>{result.diagnosis.uncertainty}</p><p><strong>Check next:</strong> {result.diagnosis.next_check}</p></details>
                  <details className={styles.details}><summary>Evidence retrieval activity</summary><ol className={styles.activity}>{result.tool_calls.map((call, i) => <li key={i}>{call.tool}{call.evidence_id ? ` / ${call.evidence_id}` : ""}</li>)}</ol></details>
                  <p className={styles.runMeta}>{(result.elapsed_ms / 1000).toFixed(1)}s · {result.retrieved_evidence_ids.length} records read<br />{result.model}</p>
                  <ChallengePanel key={result.run_id} sample={sample} investigation={result} citations={finding => citations(finding, sample)} />
                </>}
              </div>

              <div className={styles.evidenceList}><span className={styles.eyebrow}>INSPECT THE SAMPLE EVIDENCE</span>{sample.evidence.map(evidence => <button key={evidence.id} onClick={() => setSelectedEvidence(evidence)}><span><FileText size={13} />{evidence.title}</span><ArrowUpRight size={13} /></button>)}</div>
            </article>;
          })}
        </section>

        <aside className={styles.disclosure} aria-labelledby="install-title">
          <h2 id="install-title">Has Aurora passed your test?</h2>
          <p>These are three different, fabricated incidents. All diagnoses are generated live and the answer key remains hidden. Aurora&apos;s behaviour is on full display.</p>
          <p>Want to see how Aurora works in your environment? Hit install and let Aurora investigate for you.</p>
          <a href="https://api.aurora-ai.net/github/app/signup/start" target="_blank" rel="noreferrer" className={styles.primaryButton} aria-label="Install Aurora via GitHub">Install Aurora <ArrowUpRight size={16} /></a>
        </aside>
      </main>
      <footer className={styles.footer}><span>Aurora / Open-source AI SRE</span><span>Read the evidence. Form your own judgment.</span></footer>
      <EvidenceDialog evidence={selectedEvidence} onClose={closeEvidence} />
    </div>
  );
}
