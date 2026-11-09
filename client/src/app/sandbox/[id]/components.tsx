'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import {
  FileText, ScrollText, Image as ImageIcon, Link2, File, X, Loader2, Plug, ShieldCheck,
} from 'lucide-react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { connectorRegistry } from '@/components/connectors/ConnectorRegistry';
import { sandboxApi, formatBytes, type SandboxArtifact, type ConnectorSuggestion, type ArtifactKind } from '../lib';

const KIND_ICON: Record<ArtifactKind, typeof FileText> = {
  log: ScrollText,
  text: FileText,
  document: File,
  image: ImageIcon,
  link: Link2,
};

// ── Artifact chip (Claude-style pasted content) ─────────────────────

export function ArtifactChip({
  sandboxId, artifact, onOpen, onRemove,
}: {
  sandboxId: string;
  artifact: SandboxArtifact;
  onOpen: () => void;
  onRemove: () => void;
}) {
  const Icon = KIND_ICON[artifact.kind];
  const isImage = artifact.kind === 'image';
  return (
    <div className="group relative w-44 rounded-lg border border-border bg-card hover:border-foreground/30 transition-colors">
      <button type="button" onClick={onOpen} className="w-full text-left p-2.5" title={artifact.name}>
        {isImage ? (
          // eslint-disable-next-line @next/next/no-img-element -- auth-proxied bytes, not a static asset
          <img
            src={sandboxApi.imageUrl(sandboxId, artifact.id)}
            alt={artifact.name}
            className="h-16 w-full object-cover rounded mb-2 bg-muted"
          />
        ) : (
          <p className="h-16 overflow-hidden text-[10px] leading-snug font-mono text-muted-foreground whitespace-pre-wrap break-all mb-2">
            {artifact.preview}
          </p>
        )}
        <div className="flex items-center gap-1.5 text-xs">
          <Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate font-medium">{artifact.name}</span>
        </div>
        <div className="mt-0.5 text-[11px] text-muted-foreground flex items-center gap-1">
          <span className="uppercase tracking-wide">{artifact.kind}</span>
          <span>·</span>
          <span>{formatBytes(artifact.sizeBytes)}</span>
          {isImage && artifact.description === 'pending' && (
            <span className="flex items-center gap-0.5"><Loader2 className="h-3 w-3 animate-spin" /> reading</span>
          )}
          {isImage && artifact.description === 'unavailable' && <span>· not described</span>}
        </div>
      </button>
      <button
        type="button"
        onClick={onRemove}
        aria-label={`Remove ${artifact.name}`}
        className="absolute -top-2 -right-2 hidden group-hover:flex h-5 w-5 items-center justify-center rounded-full border border-border bg-background text-muted-foreground hover:text-foreground"
      >
        <X className="h-3 w-3" />
      </button>
    </div>
  );
}

// ── Viewer: exactly what Aurora stored ──────────────────────────────

export function ArtifactViewer({
  sandboxId, artifact, onClose,
}: {
  sandboxId: string;
  artifact: SandboxArtifact | null;
  onClose: () => void;
}) {
  const [content, setContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setContent(null);
    setError(null);
    if (!artifact) return;
    let cancelled = false;
    sandboxApi.getArtifact(sandboxId, artifact.id)
      .then(({ artifact: full }) => { if (!cancelled) setContent(full.content ?? ''); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [sandboxId, artifact]);

  return (
    <Dialog open={!!artifact} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle className="truncate pr-6">{artifact?.name}</DialogTitle>
        </DialogHeader>
        {artifact?.kind === 'image' && (
          // eslint-disable-next-line @next/next/no-img-element -- auth-proxied bytes
          <img src={sandboxApi.imageUrl(sandboxId, artifact.id)} alt={artifact.name} className="max-h-[50vh] w-auto mx-auto rounded" />
        )}
        {artifact?.kind === 'image' && (
          <p className="text-xs font-medium text-muted-foreground mt-2">What Aurora sees in this image</p>
        )}
        {error ? (
          <p className="text-sm text-destructive">{error}</p>
        ) : content === null ? (
          <Loader2 className="h-4 w-4 animate-spin" />
        ) : (
          // Rendered as text, never HTML.
          <pre className="max-h-[50vh] overflow-auto rounded-md bg-muted p-3 text-xs font-mono whitespace-pre-wrap break-words">
            {content || (artifact?.kind === 'image' ? 'No description yet.' : '(empty)')}
          </pre>
        )}
      </DialogContent>
    </Dialog>
  );
}

// ── Guide: the next connection, inferred from the evidence ──────────

export function GuidePanel({ suggestions }: { suggestions: ConnectorSuggestion[] }) {
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <h2 className="text-sm font-semibold flex items-center gap-1.5"><Plug className="h-4 w-4" /> Next connection</h2>
      <p className="text-xs text-muted-foreground mt-1">
        Pasted evidence is a snapshot. Connecting the source lets Aurora query it live on the next incident.
      </p>
      {suggestions.length === 0 ? (
        <p className="text-xs text-muted-foreground mt-3">Add evidence to see which connection would help most.</p>
      ) : (
        <ul className="mt-3 space-y-2">
          {suggestions.map((s) => {
            const c = connectorRegistry.get(s.connector);
            return (
              <li key={s.connector} className="flex items-start gap-2.5 rounded-lg border border-border p-2.5">
                {c?.iconPath ? (
                  // eslint-disable-next-line @next/next/no-img-element -- small static connector logo
                  <img src={c.iconPath} alt="" className="h-6 w-6 rounded bg-white p-0.5 shrink-0" />
                ) : (
                  <Plug className="h-5 w-5 shrink-0" />
                )}
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium">{c?.name ?? s.connector}</div>
                  <div className="text-xs text-muted-foreground">{s.reason}</div>
                </div>
                {c?.path && (
                  <Link href={c.path} className="text-xs font-medium underline underline-offset-2 shrink-0">
                    Connect
                  </Link>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ── Transparency: what is stored ────────────────────────────────────

export function StoragePanel({ artifacts }: { artifacts: SandboxArtifact[] }) {
  const total = artifacts.reduce((n, a) => n + a.sizeBytes, 0);
  const byKind = artifacts.reduce<Record<string, number>>((m, a) => ({ ...m, [a.kind]: (m[a.kind] ?? 0) + 1 }), {});
  return (
    <section className="rounded-xl border border-border bg-card p-4">
      <h2 className="text-sm font-semibold flex items-center gap-1.5"><ShieldCheck className="h-4 w-4" /> What Aurora stores</h2>
      <p className="text-xs text-muted-foreground mt-1">
        {artifacts.length} artifact{artifacts.length === 1 ? '' : 's'}, {formatBytes(total)}
        {artifacts.length > 0 && ` (${Object.entries(byKind).map(([k, n]) => `${n} ${k}`).join(', ')})`}.
      </p>
      <ul className="text-xs text-muted-foreground mt-2 space-y-1 list-disc pl-4">
        <li>Only what you add here. No credentials, nothing read from your systems.</li>
        <li>Links are stored as text and never fetched.</li>
        <li>Screenshots are described once by the model; open any artifact to see exactly what it reads.</li>
        <li>Remove anything at any time. Deleting the sandbox deletes its artifacts and keeps your chats.</li>
      </ul>
    </section>
  );
}
