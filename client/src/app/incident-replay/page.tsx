'use client';

import { useState } from 'react';
import type { ReplayReport } from '@/lib/incidentReplay';

const SAMPLE_ALERT = 'Checkout API returns HTTP 503 after deployment';
const SAMPLE_LOGS = `14:02:10 INFO deployment checkout-api-v2 completed
14:02:21 ERROR checkout-api database connection refused
14:02:24 ERROR checkout-api POST /checkout HTTP 503`;

export default function IncidentReplayPage() {
  const [alert, setAlert] = useState('');
  const [logs, setLogs] = useState('');
  const [loading, setLoading] = useState(false);
  const [report, setReport] = useState<ReplayReport | null>(null);
  const [error, setError] = useState('');

  async function investigate() {
    setError(''); setReport(null); setLoading(true);
    const started = performance.now();
    try {
      const res = await fetch('/api/incident-replay', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ alert, logs }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Unable to investigate incident');
      setReport(data as ReplayReport);
      // Development-only timing; do not include evidence or logs in analytics.
      console.info('Incident replay time to result (ms):', Math.round(performance.now() - started));
    } catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }

  return <main className="mx-auto max-w-3xl px-6 py-12 space-y-6">
    <div>
      <h1 className="text-3xl font-bold">Replay a production incident</h1>
      <p className="mt-2 text-muted-foreground">Paste an alert and historical logs. Get an evidence-backed first investigation without connecting infrastructure.</p>
    </div>
    <div className="rounded-lg border p-4 text-sm">
      <strong>Evidence-only:</strong> No production connectors or commands are used. Your text is sent to your configured LLM provider for analysis. Remove sensitive data before pasting. Results are hypotheses, not a verified production RCA.
    </div>
    <label className="block space-y-2"><span className="font-medium">Alert</span>
      <textarea className="w-full min-h-24 rounded-md border bg-background p-3" maxLength={1500} value={alert} onChange={e => setAlert(e.target.value)} placeholder="What went wrong?" />
    </label>
    <label className="block space-y-2"><span className="font-medium">Logs (up to 12,000 characters / 220 lines)</span>
      <textarea className="w-full min-h-52 rounded-md border bg-background p-3 font-mono text-sm" maxLength={12000} value={logs} onChange={e => setLogs(e.target.value)} placeholder="Paste log lines and timestamps" />
    </label>
    <div className="flex gap-3 flex-wrap">
      <button className="rounded-md bg-primary text-primary-foreground px-4 py-2 disabled:opacity-50" disabled={loading || !alert.trim() || !logs.trim()} onClick={investigate}>{loading ? 'Investigating…' : 'Investigate'}</button>
      <button className="rounded-md border px-4 py-2" onClick={() => {setAlert(SAMPLE_ALERT);setLogs(SAMPLE_LOGS);setReport(null);setError('');}}>Load sample incident</button>
    </div>
    {error && <p role="alert" className="text-red-600">{error}</p>}
    {report && <section aria-label="Incident assessment" className="space-y-5 rounded-lg border p-5">
      <h2 className="text-2xl font-semibold">Incident assessment</h2>
      <p>{report.summary}</p>
      <h3 className="font-semibold">Observed facts</h3>
      {report.observations.length ? <ul className="list-disc pl-6 space-y-2">{report.observations.map((item, index) => <li key={index}>{item.observation} <span className="text-muted-foreground">[lines {item.evidence_ids.join(', ')}]</span></li>)}</ul> : <p>No observations could be supported with cited lines.</p>}
      <h3 className="font-semibold">Possible causes (not verified)</h3>
      <ul className="list-disc pl-6 space-y-2">{report.hypotheses.map((item, index) => <li key={index}>{item.observation} {item.evidence_ids.length > 0 && <span className="text-muted-foreground">[lines {item.evidence_ids.join(', ')}]</span>}</li>)}</ul>
      <h3 className="font-semibold">Recommended checks</h3>
      <ul className="list-disc pl-6 space-y-1">{report.next_checks.map((check, index) => <li key={index}>{check}</li>)}</ul>
      <p className="text-sm text-muted-foreground"><strong>Limitations:</strong> {report.limitations}</p>
      <details><summary className="cursor-pointer font-medium">See supplied evidence with line numbers</summary><ol className="mt-2 space-y-1 font-mono text-xs break-all">{report.evidence.map(line => <li key={line.id}>[{line.id}] {line.text}</li>)}</ol></details>
    </section>}
  </main>;
}
