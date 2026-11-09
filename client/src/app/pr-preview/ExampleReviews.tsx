'use client';

import { useState } from 'react';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Verdict, VerdictView } from './VerdictView';
import examplesData from './examples.json';

// Pre-run reviews, written by scripts/generate_pr_preview_examples.py and
// never edited by hand. Opening one sends no request: no session, no LLM call,
// nothing counted against the monthly limit.
interface Example {
  url: string;
  kind: string;
  incident: string | null;
  incident_url: string | null;
  repo: string;
  pr_number: number;
  title: string;
  head_sha: string;
  status: string;
  verdict: Verdict | null;
  model: string;
  generated_at: string;
  duration_seconds: number;
}

const examples = examplesData as Example[];

function outcome(example: Example): string | null {
  if (!example.incident || !example.verdict) return null;
  return example.verdict.verdict === 'RISKY' ? 'Aurora flagged it' : 'Aurora missed this one';
}

export function ExampleReviews() {
  const [open, setOpen] = useState<string | null>(null);
  if (examples.length === 0) return null;

  return (
    <section className="space-y-3">
      <div>
        <h2 className="text-sm font-medium text-muted-foreground uppercase tracking-wide">
          Examples
        </h2>
        <p className="text-xs text-muted-foreground mt-1">
          Pre-run reviews of public PRs, most of them named in a public postmortem, issue or revert
          as the cause of an incident. Shown unedited, misses included.
        </p>
      </div>
      <div className="space-y-2">
        {examples.map(example => {
          const isOpen = open === example.url;
          const result = outcome(example);
          return (
            <Card key={example.url}>
              <CardContent className="py-4 space-y-3">
                <button
                  type="button"
                  onClick={() => setOpen(isOpen ? null : example.url)}
                  className="w-full text-left space-y-1"
                  aria-expanded={isOpen}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge variant="outline">Example</Badge>
                    <Badge variant="outline">{example.kind}</Badge>
                    {example.verdict && (
                      <Badge variant={example.verdict.verdict === 'RISKY' ? 'destructive' : 'secondary'}>
                        {example.verdict.verdict}
                      </Badge>
                    )}
                    {result && <span className="text-xs text-muted-foreground">{result}</span>}
                  </div>
                  <p className="text-sm font-medium">{example.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {example.repo}#{example.pr_number}
                  </p>
                </button>

                {isOpen && (
                  <div className="space-y-3 border-t border-border pt-3">
                    {example.incident && (
                      <p className="text-sm">
                        <span className="font-medium">What actually happened: </span>
                        {example.incident}{' '}
                        {example.incident_url && (
                          <a
                            href={example.incident_url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="underline underline-offset-4"
                          >
                            Incident report
                          </a>
                        )}
                      </p>
                    )}
                    {example.verdict ? (
                      <VerdictView verdict={example.verdict} />
                    ) : (
                      <p className="text-sm text-muted-foreground">
                        This run finished without a verdict (status: {example.status}).
                      </p>
                    )}
                    <p className="text-xs text-muted-foreground">
                      Pre-run example, not live · {example.model} ·{' '}
                      {example.generated_at.slice(0, 10)} · commit {example.head_sha.slice(0, 7)} ·{' '}
                      <a
                        href={example.url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="underline underline-offset-4"
                      >
                        PR on GitHub
                      </a>
                    </p>
                  </div>
                )}
              </CardContent>
            </Card>
          );
        })}
      </div>
    </section>
  );
}
