'use client';

import { useState, type ReactNode } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { FlaskConical, Plus, GitBranch, FileStack, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useToast } from '@/hooks/use-toast';
import { useQuery, jsonFetcher } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';
import { sandboxApi, type Sandbox } from './lib';

const STEPS = [
  ['Drop in what you have', 'Logs, alert payloads, postmortems, screenshots, links.'],
  ['Point at a public repo', 'Aurora reads its commits and PRs around the incident.'],
  ['Investigate or chat', 'A full RCA with zero credentials. Connect tools later.'],
];

export default function SandboxListPage() {
  const router = useRouter();
  const { toast } = useToast();
  const [creating, setCreating] = useState(false);
  const { data, isLoading } = useQuery<{ sandboxes: Sandbox[] }>('/api/sandboxes', jsonFetcher);
  const sandboxes = data?.sandboxes ?? [];

  const create = async () => {
    setCreating(true);
    try {
      const { sandbox } = await sandboxApi.create('Untitled sandbox');
      router.push(`/sandbox/${sandbox.id}`);
    } catch (e) {
      toast({ title: 'Could not create sandbox', description: (e as Error).message, variant: 'destructive' });
      setCreating(false);
    }
  };

  let content: ReactNode;
  if (isLoading && !data) {
    content = (
      <div className="text-sm text-muted-foreground flex items-center gap-2">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  } else if (sandboxes.length === 0) {
    content = <EmptyState />;
  } else {
    content = <SandboxGrid sandboxes={sandboxes} />;
  }

  return (
    <div className="max-w-[1100px] mx-auto py-8 px-4 sm:px-6">
      <div className="flex items-start justify-between gap-4 mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight flex items-center gap-2">
            <FlaskConical className="h-6 w-6" /> Sandboxes
          </h1>
          <p className="text-sm text-muted-foreground mt-1">
            Investigate with the evidence you already have. No connections, no credentials.
          </p>
        </div>
        <Button onClick={() => void create()} disabled={creating}>
          {creating ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plus className="h-4 w-4" />}
          <span className="ml-1.5">New sandbox</span>
        </Button>
      </div>

      {content}
    </div>
  );
}

function EmptyState() {
  return (
    <div className="rounded-xl border border-dashed border-border p-8">
      <p className="font-medium mb-4">Get a root cause in minutes, before connecting anything.</p>
      <ol className="grid gap-4 sm:grid-cols-3">
        {STEPS.map(([title, body], i) => (
          <li key={title} className="rounded-lg bg-card border border-border p-4">
            <div className="text-xs text-muted-foreground mb-1">Step {i + 1}</div>
            <div className="text-sm font-medium">{title}</div>
            <div className="text-sm text-muted-foreground mt-1">{body}</div>
          </li>
        ))}
      </ol>
    </div>
  );
}

function SandboxGrid({ sandboxes }: Readonly<{ sandboxes: Sandbox[] }>) {
  return (
    <ul className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
      {sandboxes.map((s) => (
        <li key={s.id}>
          <Link
            href={`/sandbox/${s.id}`}
            className="block rounded-xl border border-border bg-card p-4 hover:border-foreground/30 transition-colors"
          >
            <div className="font-medium truncate">{s.name}</div>
            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span className="flex items-center gap-1">
                <FileStack className="h-3.5 w-3.5" /> {s.artifactCount} artifact{s.artifactCount === 1 ? '' : 's'}
              </span>
              {s.publicRepo && (
                <span className="flex items-center gap-1 truncate">
                  <GitBranch className="h-3.5 w-3.5" /> {s.publicRepo}
                </span>
        
  );
}
