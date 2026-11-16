'use client';

import Link from 'next/link';
import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ArrowRight, Check, FileText, Loader2, ShieldCheck } from 'lucide-react';

const MAX_CHARS = 20_000;
const MAX_LINES = 1_000;
const SAMPLE_LOG = `[09:42:01] Run npm ci
[09:42:02] added 621 packages
[09:42:03] Run npm run build
[09:42:03] > checkout-api@1.0.0 build
[09:42:03] > tsc --noEmit
[09:42:05] src/checkout.ts(42,7): error TS2322: Type 'string | undefined' is not assignable to type 'string'.
[09:42:05]   Type 'undefined' is not assignable to type 'string'.
[09:42:05] Process completed with exit code 2.`;

type Evidence = { start_line: number; end_line: number; reason: string; quote: string };
type Diagnosis = {
  status: 'likely_cause' | 'insufficient_evidence';
  summary: string;
  evidence: Evidence[];
  suggested_steps: string[];
  verification: string[];
  missing_information: string[];
  analysis_ms: number;
};

function Steps({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="space-y-3">
      <h3 className="text-sm font-semibold text-white">{title}</h3>
      <ol className="list-decimal space-y-2 pl-5 text-sm leading-relaxed text-zinc-300">
        {items.map((item, i) => <li key={i} className="pl-1 whitespace-pre-wrap break-words">{item}</li>)}
      </ol>
    </section>
  );
}

export default function DiagnoseBuildPage() {
  const [log, setLog] = useState('');
  const [isSample, setIsSample] = useState(false);
  const [consent, setConsent] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<Diagnosis | null>(null);
  const [timeToResult, setTimeToResult] = useState<number | null>(null);
  const openedAt = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  const resultHeading = useRef<HTMLHeadingElement>(null);
  const lineCount = log.replace(/\r\n?/g, '\n').split('\n').length;
  const tooLarge = log.length > MAX_CHARS || lineCount > MAX_LINES;

  useEffect(() => {
    openedAt.current = performance.now();
    return () => activeRequest.current?.abort();
  }, []);

  useEffect(() => {
    if (result) resultHeading.current?.focus();
  }, [result]);

  function updateLog(value: string, sample = false) {
    setLog(value);
    setIsSample(sample);
    setResult(null);
    setError('');
    setConsent(false);
  }

  async function analyze(event: React.FormEvent) {
    event.preventDefault();
    if (activeRequest.current || tooLarge || !consent || !log.trim()) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    const timeout = setTimeout(() => controller.abort(), 60_000);
    setIsLoading(true);
    setError('');
    setResult(null);
    try {
      const response = await fetch('/api/ci-diagnosis', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ log, consent: true }),
        signal: controller.signal,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || 'Analysis failed. Please retry.');
      setResult(data);
      setTimeToResult(Math.max(1, Math.round((performance.now() - openedAt.current) / 1000)));
    } catch (err) {
      setError(err instanceof Error && err.name === 'AbortError'
        ? 'Analysis timed out. Try a shorter excerpt or retry in a moment.'
        : err instanceof Error ? err.message : 'Analysis failed. Please retry.');
    } finally {
      clearTimeout(timeout);
      activeRequest.current = null;
      setIsLoading(false);
    }
  }

  return (
    <main className="h-full overflow-y-auto px-5 py-8 text-zinc-100 sm:px-8">
      <div className="mx-auto max-w-5xl space-y-8 pb-12">
        <nav className="flex items-center justify-between text-sm text-zinc-400">
          <Link href="/onboarding" className="inline-flex items-center gap-2 hover:text-white"><ArrowLeft size={16} /> Back to setup</Link>
          <span className="font-semibold tracking-wide text-zinc-200">Aurora</span>
        </nav>
        <header className="max-w-2xl space-y-3">
          <p className="text-xs font-medium uppercase tracking-widest text-emerald-400">A first result, before you connect anything</p>
          <h1 className="text-3xl font-semibold tracking-tight sm:text-4xl">Diagnose a failed build</h1>
          <p className="text-sm leading-6 text-zinc-400">Paste the part of your CI log around the failure. Get a likely cause, the lines that support it, and a next step you can verify.</p>
          <div className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-zinc-300">
            <span className="inline-flex items-center gap-1.5"><Check size={14} className="text-emerald-400" /> No repository access</span>
            <span className="inline-flex items-center gap-1.5"><Check size={14} className="text-emerald-400" /> No commands executed</span>
          </div>
        </header>

        <div className="grid items-start gap-6 lg:grid-cols-2">
          <form onSubmit={analyze} className="space-y-5 rounded-xl border border-white/10 bg-zinc-950/80 p-5">
            <div className="flex items-center justify-between gap-3">
              <label htmlFor="ci-log" className="font-medium text-sm">Build log</label>
              <button type="button" disabled={isLoading} onClick={() => updateLog(SAMPLE_LOG, true)} className="text-xs text-emerald-400 hover:text-emerald-300 disabled:opacity-50">Use a sample log</button>
            </div>
            {isSample && <p className="text-xs text-amber-300">Sample data · TypeScript build failure. The analysis is generated when you submit.</p>}
            <textarea id="ci-log" value={log} disabled={isLoading} onChange={(e) => updateLog(e.target.value)}
              aria-describedby="log-help log-size" aria-invalid={tooLarge} spellCheck={false}
              placeholder={'Paste the failing step and its surrounding output…\n\nInclude the command, error message, and exit code.'}
              className="min-h-72 w-full resize-y rounded-lg border border-zinc-700 bg-black/50 p-3 font-mono text-xs leading-6 text-zinc-200 placeholder:text-zinc-600 focus:border-emerald-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 disabled:opacity-60" />
            <div id="log-size" className={`text-xs ${tooLarge ? 'text-red-300' : 'text-zinc-500'}`}>
              {log.length.toLocaleString()} / 20,000 characters · {lineCount.toLocaleString()} / 1,000 lines
              {tooLarge && <p className="mt-1">Shorten the excerpt around the failure to continue.</p>}
            </div>
            <div className="flex gap-2.5 rounded-lg bg-white/5 p-3 text-xs leading-5 text-zinc-400" id="log-help">
              <ShieldCheck size={18} className="mt-0.5 shrink-0 text-zinc-300" />
              <p>Remove secrets and personal data before submitting. Only this excerpt is sent to your administrator-configured AI provider. This feature does not save the log or diagnosis to a chat; usage counts and timing are recorded. Your provider&apos;s retention policy applies.</p>
            </div>
            <label className="flex items-start gap-2.5 text-xs leading-5 text-zinc-300">
              <input type="checkbox" checked={consent} disabled={isLoading} onChange={(e) => setConsent(e.target.checked)} className="mt-1 accent-emerald-500" />
              I have reviewed this log and agree to send it for analysis.
            </label>
            {error && <p role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">{error}</p>}
            <button type="submit" disabled={isLoading || !log.trim() || tooLarge || !consent}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-emerald-400 px-4 py-3 text-sm font-semibold text-zinc-950 transition hover:bg-emerald-300 disabled:cursor-not-allowed disabled:opacity-40">
              {isLoading ? <><Loader2 size={16} className="animate-spin" /> Analyzing log…</> : <>Analyze build log <ArrowRight size={16} /></>}
            </button>
          </form>

          <div aria-live="polite" aria-busy={isLoading}>
            {!result ? (
              <section className="rounded-xl border border-dashed border-white/15 p-7 text-sm text-zinc-400">
                <FileText size={24} className="mb-4 text-zinc-500" />
                <h2 className="mb-3 font-medium text-zinc-200">{isLoading ? 'Reading the evidence…' : 'Your diagnosis will appear here'}</h2>
                <p className="leading-6">{isLoading ? 'Analyzing only the submitted excerpt. Allow up to a minute.' : 'Each finding links back to lines from your log. If there is not enough evidence, Aurora will tell you what to collect next.'}</p>
              </section>
            ) : (
              <article className="space-y-6 rounded-xl border border-emerald-500/25 bg-zinc-950/90 p-5">
                <div className="space-y-2">
                  <p className="text-xs text-zinc-500">{isSample ? 'Sample log' : 'Your log'} · Analysis in {(result.analysis_ms / 1000).toFixed(1)}s · {timeToResult}s since opening</p>
                  <h2 ref={resultHeading} tabIndex={-1} className="text-lg font-semibold text-emerald-300 focus:outline-none">{result.status === 'likely_cause' ? 'Likely cause' : 'More evidence needed'}</h2>
                  <p className="whitespace-pre-wrap break-words text-sm leading-6 text-zinc-200">{result.summary}</p>
                </div>
                {result.evidence.length > 0 && <section className="space-y-3">
                  <h3 className="text-sm font-semibold">Evidence from your log</h3>
                  {result.evidence.map((item, i) => (
                    <details key={i} open className="rounded-lg border border-white/10 bg-black/30 p-3">
                      <summary className="cursor-pointer text-xs text-emerald-300">Lines {item.start_line}–{item.end_line}</summary>
                      <pre className="my-3 max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-5 text-zinc-300">{item.quote}</pre>
                      <p className="text-xs leading-5 text-zinc-400">{item.reason}</p>
                    </details>
                  ))}
                </section>}
                <Steps title="Suggested next steps" items={result.suggested_steps} />
                <Steps title="How to verify" items={result.verification} />
                <Steps title="What to collect next" items={result.missing_information} />
                <p className="border-t border-white/10 pt-4 text-xs leading-5 text-zinc-500">Based only on this excerpt. Review suggestions before acting; Aurora has not run or verified them. Copy any result you want to keep before leaving.</p>
              </article>
            )}
          </div>
        </div>
        <p className="text-center text-xs text-zinc-500">Ready to investigate production incidents? <Link href="/onboarding" className="text-zinc-300 underline underline-offset-4 hover:text-white">Continue connecting your tools</Link></p>
      </div>
    </main>
  );
}
