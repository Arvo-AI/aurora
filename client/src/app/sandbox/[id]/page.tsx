'use client';

import { use, useCallback, useRef, useState } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import {
  ArrowLeft, Paperclip, Loader2, Search, MessageSquare, Plus, Trash2, GitBranch, Clock, Sparkles,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/hooks/use-toast';
import { useQuery, jsonFetcher, queryClient } from '@/lib/query';
import { formatTimeAgo } from '@/lib/utils/time-format';
import {
  sandboxApi, classifyPaste, pasteName,
  type Sandbox, type SandboxArtifact, type SandboxChat, type ConnectorSuggestion,
} from '../lib';
import { ArtifactChip, ArtifactViewer, GuidePanel, StoragePanel } from './components';

// datetime-local wants "YYYY-MM-DDTHH:mm" in local time.
const toLocalInput = (iso: string | null) => {
  if (!iso) return '';
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60_000).toISOString().slice(0, 16);
};

export default function SandboxPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const router = useRouter();
  const { toast } = useToast();
  const fail = (title: string, e: unknown) =>
    toast({ title, description: (e as Error).message, variant: 'destructive' });

  const sandboxKey = `/api/sandboxes/${id}`;
  const chatsKey = `/api/sandboxes/${id}/chats`;
  const suggestionsKey = `/api/sandboxes/${id}/suggestions`;

  const [describing, setDescribing] = useState(false);
  const { data, isLoading, error } = useQuery<{ sandbox: Sandbox }>(sandboxKey, jsonFetcher, {
    // Poll only while a screenshot is still being described.
    refreshInterval: describing ? 3000 : 0,
    onSuccess: (d) => setDescribing(!!d.sandbox.artifacts?.some((a) => a.kind === 'image' && a.description === 'pending')),
  });
  const sandbox = data?.sandbox;
  const artifacts = sandbox?.artifacts ?? [];
  const { data: chatData } = useQuery<{ chats: SandboxChat[] }>(chatsKey, jsonFetcher);
  const { data: sugData } = useQuery<{ suggestions: ConnectorSuggestion[] }>(suggestionsKey, jsonFetcher);

  const refresh = useCallback(() => {
    queryClient.invalidate(sandboxKey, jsonFetcher);
    queryClient.invalidate(suggestionsKey, jsonFetcher);
  }, [sandboxKey, suggestionsKey]);

  // ── Composer ────────────────────────────────────────────────────
  const [draft, setDraft] = useState('');
  const [busy, setBusy] = useState(0); // in-flight uploads
  const [dragging, setDragging] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const track = async (work: Promise<unknown>, label: string) => {
    setBusy((n) => n + 1);
    try { await work; refresh(); } catch (e) { fail(label, e); } finally { setBusy((n) => n - 1); }
  };

  const addFiles = (files: FileList | File[]) => {
    for (const f of Array.from(files)) track(sandboxApi.addFile(id, f), `Could not add ${f.name}`);
  };

  const onPaste = (e: React.ClipboardEvent<HTMLTextAreaElement>) => {
    if (e.clipboardData.files.length) {
      e.preventDefault();
      addFiles(e.clipboardData.files); // pasted screenshots
      return;
    }
    const text = e.clipboardData.getData('text');
    const kind = classifyPaste(text);
    if (!kind) return; // short prose stays in the composer
    e.preventDefault();
    const name = kind === 'link' ? undefined : pasteName(kind, text);
    track(sandboxApi.addText(id, kind, text.trim(), name), 'Could not add pasted content');
  };

  const saveDraft = () => {
    const text = draft.trim();
    if (!text) return;
    const kind = classifyPaste(text) ?? 'text';
    track(
      sandboxApi.addText(id, kind, text, kind === 'link' ? undefined : pasteName(kind === 'log' ? 'log' : 'text', text)),
      'Could not add note',
    );
    setDraft('');
  };

  // ── Details ─────────────────────────────────────────────────────
  const [repo, setRepo] = useState<string | null>(null);
  const [when, setWhen] = useState<string | null>(null);
  const [name, setName] = useState<string | null>(null);

  const save = async (patch: Parameters<typeof sandboxApi.update>[1]) => {
    try {
      const res = await sandboxApi.update(id, patch);
      queryClient.set(sandboxKey, res);
      setRepo(null); setWhen(null); setName(null);
    } catch (e) { fail('Could not save', e); }
  };

  // ── Actions ─────────────────────────────────────────────────────
  const [question, setQuestion] = useState('');
  const [starting, setStarting] = useState(false);
  const [viewing, setViewing] = useState<SandboxArtifact | null>(null);

  const investigate = async () => {
    setStarting(true);
    try {
      const res = await sandboxApi.investigate(id, question.trim());
      toast({ title: 'Investigation started', description: 'Aurora is working through your evidence.' });
      router.push(`/incidents/${res.incident_id}`);
    } catch (e) { fail('Could not start investigation', e); setStarting(false); }
  };

  const newChat = async () => {
    try {
      const chat = await sandboxApi.newChat(id, `Chat: ${sandbox?.name ?? 'sandbox'}`);
      router.push(`/chat?sessionId=${chat.id}`);
    } catch (e) { fail('Could not start chat', e); }
  };

  const removeArtifact = (a: SandboxArtifact) =>
    track(sandboxApi.removeArtifact(id, a.id), `Could not remove ${a.name}`);

  const deleteSandbox = async () => {
    if (!window.confirm('Delete this sandbox and its artifacts? Chats are kept.')) return;
    try { await sandboxApi.remove(id); router.push('/sandbox'); } catch (e) { fail('Could not delete', e); }
  };

  if (error) {
    return <div className="max-w-[1200px] mx-auto py-8 px-4 text-sm text-destructive">{error.message}</div>;
  }
  if (isLoading || !sandbox) {
    return <div className="max-w-[1200px] mx-auto py-8 px-4"><Loader2 className="h-5 w-5 animate-spin" /></div>;
  }

  const chats = chatData?.chats ?? [];

  return (
    <div className="max-w-[1200px] mx-auto py-6 px-4 sm:px-6">
      <Link href="/sandbox" className="text-xs text-muted-foreground hover:text-foreground flex items-center gap-1 mb-3">
        <ArrowLeft className="h-3.5 w-3.5" /> Sandboxes
      </Link>

      <div className="flex items-center gap-3 mb-6">
        <Input
          value={name ?? sandbox.name}
          onChange={(e) => setName(e.target.value)}
          onBlur={() => name !== null && name.trim() && name !== sandbox.name && save({ name: name.trim() })}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          className="text-xl font-semibold border-transparent hover:border-input focus:border-input bg-transparent px-2 -ml-2 h-auto py-1"
          aria-label="Sandbox name"
        />
        <Button variant="ghost" size="icon" onClick={deleteSandbox} aria-label="Delete sandbox">
          <Trash2 className="h-4 w-4" />
        </Button>
      </div>

      <div className="grid gap-6 lg:grid-cols-[1fr_320px]">
        <div className="space-y-6 min-w-0">
          {/* Knowledge base */}
          <section
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => { e.preventDefault(); setDragging(false); addFiles(e.dataTransfer.files); }}
            className={`rounded-xl border bg-card p-4 transition-colors ${dragging ? 'border-foreground/50 bg-muted/40' : 'border-border'}`}
          >
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-semibold">Knowledge base</h2>
              {busy > 0 && <span className="text-xs text-muted-foreground flex items-center gap-1"><Loader2 className="h-3 w-3 animate-spin" /> Adding…</span>}
            </div>

            <Textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onPaste={onPaste}
              placeholder="Paste logs, alert payloads, a postmortem or a link. Large and log-like pastes become attachments. Drop files or screenshots anywhere here."
              className="min-h-[90px] text-sm"
            />
            <div className="flex items-center justify-between mt-2">
              <div>
                <input
                  ref={fileInput}
                  type="file"
                  multiple
                  hidden
                  accept=".pdf,.txt,.md,.log,.json,.yaml,.yml,.csv,image/png,image/jpeg,image/gif,image/webp"
                  onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = ''; }}
                />
                <Button variant="ghost" size="sm" onClick={() => fileInput.current?.click()}>
                  <Paperclip className="h-4 w-4 mr-1" /> Attach files
                </Button>
              </div>
              <Button variant="secondary" size="sm" onClick={saveDraft} disabled={!draft.trim()}>
                Add to knowledge base
              </Button>
            </div>

            {artifacts.length > 0 && (
              <div className="mt-4 flex flex-wrap gap-3">
                {artifacts.map((a) => (
                  <ArtifactChip
                    key={a.id}
                    sandboxId={id}
                    artifact={a}
                    onOpen={() => setViewing(a)}
                    onRemove={() => removeArtifact(a)}
                  />
                ))}
              </div>
            )}
          </section>

          {/* Investigate + chats */}
          <section className="rounded-xl border border-border bg-card p-4">
            <h2 className="text-sm font-semibold mb-3">Investigate</h2>
            <div className="flex flex-col sm:flex-row gap-2">
              <Input
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Optional: what do you want to know? e.g. why did checkout start timing out?"
                onKeyDown={(e) => e.key === 'Enter' && artifacts.length && !starting && investigate()}
              />
              <Button onClick={investigate} disabled={starting || artifacts.length === 0}>
                {starting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
                <span className="ml-1.5">Run investigation</span>
              </Button>
            </div>
            {artifacts.length === 0 && (
              <p className="text-xs text-muted-foreground mt-2">Add at least one artifact first.</p>
            )}

            <div className="flex items-center justify-between mt-6 mb-2">
              <h3 className="text-sm font-semibold">Chats in this sandbox</h3>
              <Button variant="outline" size="sm" onClick={newChat}>
                <Plus className="h-4 w-4 mr-1" /> New chat
              </Button>
            </div>
            {chats.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                No chats yet. Chats here can see the whole knowledge base.
              </p>
            ) : (
              <ul className="divide-y divide-border">
                {chats.map((c) => {
                  const Icon = c.isInvestigation ? Search : MessageSquare;
                  const href = c.isInvestigation && c.incidentId ? `/incidents/${c.incidentId}` : `/chat?sessionId=${c.id}`;
                  return (
                    <li key={c.id}>
                      <Link href={href} className="flex items-center gap-2.5 py-2 text-sm hover:text-foreground text-foreground/90">
                        <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                        <span className="truncate flex-1">{c.title}</span>
                        {c.isInvestigation && <span className="text-[11px] text-muted-foreground">investigation</span>}
                        <span className="text-[11px] text-muted-foreground shrink-0">{formatTimeAgo(c.updatedAt)}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        </div>

        <aside className="space-y-4">
          <section className="rounded-xl border border-border bg-card p-4 space-y-3">
            <h2 className="text-sm font-semibold">Context</h2>
            <label className="block">
              <span className="text-xs text-muted-foreground flex items-center gap-1"><GitBranch className="h-3.5 w-3.5" /> Public GitHub repo</span>
              <Input
                value={repo ?? sandbox.publicRepo ?? ''}
                onChange={(e) => setRepo(e.target.value)}
                onBlur={() => repo !== null && repo !== (sandbox.publicRepo ?? '') && save({ publicRepo: repo.trim() })}
                onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
                placeholder="owner/repo"
                className="mt-1 h-8 text-sm"
              />
              <span className="text-[11px] text-muted-foreground">Read-only history. No GitHub connection needed.</span>
            </label>
            <label className="block">
              <span className="text-xs text-muted-foreground flex items-center gap-1"><Clock className="h-3.5 w-3.5" /> When it happened</span>
              <Input
                type="datetime-local"
                value={when ?? toLocalInput(sandbox.incidentTime)}
                onChange={(e) => setWhen(e.target.value)}
                onBlur={() => when !== null && save({ incidentTime: when ? new Date(when).toISOString() : '' })}
                className="mt-1 h-8 text-sm"
              />
              <span className="text-[11px] text-muted-foreground">Aurora looks at changes shortly before this.</span>
            </label>
          </section>
          <GuidePanel suggestions={sugData?.suggestions ?? []} />
          <StoragePanel artifacts={artifacts} />
        </aside>
      </div>

      <ArtifactViewer sandboxId={id} artifact={viewing} onClose={() => setViewing(null)} />
    </div>
  );
}
