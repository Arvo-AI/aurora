'use client';

import { FormEvent, useEffect, useState } from 'react';
import Link from 'next/link';
import { GitPullRequest, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { isIncidentPreventionEnabled } from '@/lib/feature-flags';
import { jsonFetcher, useQuery } from '@/lib/query';
import { ExampleReviews } from './ExampleReviews';
import { Verdict, VerdictView } from './VerdictView';

const POLL_MS = 3000;
const BOOK_A_CALL_URL = 'https://cal.com/arvo-ai';

interface StartedPreview {
  session_id: string;
  repo: string;
  pr_number: number;
  title?: string | null;
}

interface PreviewStatus {
  status: string;
  verdict: Verdict | null;
}

export default function PrPreviewPage() {
  const [url, setUrl] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [limitReached, setLimitReached] = useState(false);
  const [started, setStarted] = useState<StartedPreview | null>(null);
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());

  const [running, setRunning] = useState(false);
  const { data: status } = useQuery<PreviewStatus>(
    started ? `/api/pr-preview/${started.session_id}` : null,
    jsonFetcher,
    { staleTime: 0, revalidateOnFocus: false, refreshInterval: running ? POLL_MS : 0 },
  );

  useEffect(() => {
    setRunning(Boolean(started) && (!status || status.status === 'in_progress'));
  }, [started, status]);

  // Ticks the elapsed-time counter while the review runs.
  useEffect(() => {
    if (!running) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);

  const start = async (prUrl: string) => {
    setError(null);
    setSubmitting(true);
    try {
      const res = await fetch('/api/pr-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'include',
        body: JSON.stringify({ url: prUrl }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setLimitReached(Boolean(body.limit_reached));
        setError(body.error || 'Could not start the review.');
        return;
      }
      setLimitReached(false);
      setStarted(body as StartedPreview);
      setStartedAt(Date.now());
      setNow(Date.now());
    } catch {
      setError('Could not start the review.');
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    if (url.trim()) start(url.trim());
  };

  if (!isIncidentPreventionEnabled()) {
    return (
      <div className="max-w-3xl mx-auto py-8 px-4">
        <p className="text-muted-foreground">PR review (Incident Prevention) is turned off on this deployment.</p>
      </div>
    );
  }

  const elapsed = startedAt ? Math.max(0, Math.round((now - startedAt) / 1000)) : 0;
  const chatHref = started ? `/chat?sessionId=${started.session_id}` : '#';

  return (
    <div className="max-w-3xl mx-auto py-8 px-4 space-y-6">
      <div>
        <h1 className="text-3xl font-bold flex items-center gap-2">
          <GitPullRequest className="h-6 w-6" />
          Review a pull request
        </h1>
        <p className="text-sm text-muted-foreground mt-2">
          Paste any public GitHub PR. Aurora checks it for the incidents it could cause in
          production, with nothing to install. The result stays here; nothing is posted to GitHub.
        </p>
      </div>

      <form onSubmit={onSubmit} className="flex gap-2">
        <Input
          value={url}
          onChange={e => setUrl(e.target.value)}
          placeholder="https://github.com/owner/repo/pull/123"
          disabled={submitting || running}
          aria-label="Pull request link"
        />
        <Button type="submit" disabled={!url.trim() || submitting || running}>
          {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : 'Review'}
        </Button>
      </form>
      {error && !limitReached && <p className="text-sm text-destructive">{error}</p>}
      {limitReached && (
        <Card>
          <CardContent className="py-5 space-y-3">
            <p className="text-sm">{error}</p>
            <div className="flex flex-wrap gap-2">
              <Button asChild>
                <Link href="/connectors">Connect GitHub</Link>
              </Button>
              <Button asChild variant="outline">
                <a href={BOOK_A_CALL_URL} target="_blank" rel="noopener noreferrer">Book a call</a>
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      {started && (
        <Card>
          <CardContent className="py-6 space-y-4">
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <p className="font-medium truncate">{started.title || `PR #${started.pr_number}`}</p>
                <p className="text-sm text-muted-foreground">
                  {started.repo}#{started.pr_number}
                </p>
              </div>
              <Link href={chatHref} className="text-sm underline underline-offset-4 shrink-0">
                {running ? 'Watch the investigation' : 'Full investigation'}
              </Link>
            </div>

            {running && (
              <p className="text-sm text-muted-foreground flex items-center gap-2">
                <Loader2 className="h-4 w-4 animate-spin" />
                Reviewing… {elapsed}s
              </p>
            )}

            {status?.status === 'completed' && status.verdict && (
              <VerdictView verdict={status.verdict} note={`in ${elapsed}s`} />
            )}

            {status?.status === 'completed' && !status.verdict && (
              <p className="text-sm text-muted-foreground">
                Aurora finished without a structured verdict. Open the full investigation to read it.
              </p>
            )}

            {status && ['failed', 'cancelled'].includes(status.status) && (
              <p className="text-sm text-destructive">
                The review could not finish. Open the full investigation for details.
              </p>
            )}

            {!running && (
              <p className="text-sm text-muted-foreground border-t border-border pt-4">
                Want this on every pull request, posted as a review before merge?{' '}
                <Link href="/connectors" className="underline underline-offset-4 text-foreground">
                  Connect GitHub
                </Link>
              </p>
            )}
          </CardContent>
        </Card>
      )}

      <ExampleReviews />
    </div>
  );
}
