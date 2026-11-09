'use client';

import { FormEvent, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { ArrowLeft, Github, Loader2, ShieldCheck } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';

export default function RepoPreviewPage() {
  const router = useRouter();
  const [repositoryUrl, setRepositoryUrl] = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState('');

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const trimmedUrl = repositoryUrl.trim();

    if (!trimmedUrl) {
      setError('Enter a public GitHub repository URL.');
      return;
    }

    setIsSubmitting(true);
    setError('');

    try {
      const response = await fetch('/api/public-repo-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repository_url: trimmedUrl }),
      });
      const data = await response.json().catch(() => ({}));

      if (response.status !== 202) {
        throw new Error(data.error || data.message || 'Could not start the repository preview.');
      }
      if (!data.session_id) {
        throw new Error('The preview started without a session. Please try again.');
      }

      router.push(`/chat?sessionId=${encodeURIComponent(data.session_id)}`);
    } catch (submissionError) {
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : 'Could not start the repository preview.',
      );
      setIsSubmitting(false);
    }
  };

  return (
    <div className="max-w-2xl mx-auto py-8 px-4">
      <Link
        href="/incidents"
        className="inline-flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground mb-6"
      >
        <ArrowLeft className="h-4 w-4" />
        Back to incidents
      </Link>

      <div className="mb-6">
        <div className="flex items-center gap-3 mb-2">
          <Github className="h-7 w-7" aria-hidden="true" />
          <h1 className="text-3xl font-bold">Preview a public repository</h1>
        </div>
        <p className="text-muted-foreground">
          Paste a public GitHub repository URL to look for repository-visible reliability risks.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Paste a public GitHub repository URL</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="repository-url">Repository URL</Label>
              <Input
                id="repository-url"
                type="url"
                inputMode="url"
                value={repositoryUrl}
                onChange={(event) => {
                  setRepositoryUrl(event.target.value);
                  if (error) setError('');
                }}
                placeholder="https://github.com/owner/repository"
                autoComplete="url"
                disabled={isSubmitting}
                aria-describedby={error ? 'repository-url-error' : 'repository-preview-scope'}
                aria-invalid={Boolean(error)}
              />
              {error && (
                <p id="repository-url-error" role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              )}
            </div>

            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                  Starting preview…
                </>
              ) : (
                'Preview repository'
              )}
            </Button>
          </form>

          <div id="repository-preview-scope" className="mt-6 rounded-lg bg-muted/50 p-4">
            <div className="flex items-center gap-2 mb-2">
              <ShieldCheck className="h-4 w-4 text-muted-foreground" aria-hidden="true" />
              <p className="text-sm font-medium">What this preview does</p>
            </div>
            <ul className="list-disc pl-5 space-y-1 text-sm text-muted-foreground">
              <li>Works with public GitHub repositories only.</li>
              <li>Reads selected configuration and deployment files; repository code is never executed.</li>
              <li>Reports repository-visible reliability risks, not a root cause from a live runtime.</li>
            </ul>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
