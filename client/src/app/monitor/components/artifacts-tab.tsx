'use client';

import { useState, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  BookOpen, Plus, ArrowLeft, Save, X, Trash2, History, Eye, Edit2, RotateCcw, Loader2,
} from 'lucide-react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { postmortemMarkdownComponents } from '@/lib/markdown-components';
import { formatTimeAgo } from '@/lib/utils/time-format';
import { ChartPanel, EmptyState } from './charts';
import {
  artifactsService,
  type ArtifactSummary,
  type ArtifactData,
  type ArtifactVersion,
  type ArtifactEditor,
} from '@/lib/services/artifacts';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function EditorBadge({ who }: { who: ArtifactEditor }) {
  const isUser = who === 'user';
  return (
    <span
      className={`px-1.5 py-0.5 rounded text-[10px] font-medium ${
        isUser ? 'bg-emerald-500/10 text-emerald-400' : 'bg-blue-500/10 text-blue-400'
      }`}
    >
      {isUser ? 'You' : 'Agent'}
    </span>
  );
}

// ---------------------------------------------------------------------------
// List view
// ---------------------------------------------------------------------------

export default function ArtifactsTab() {
  const { data, isLoading, mutate } = useQuery<{ artifacts: ArtifactSummary[] }>(
    '/api/artifacts', jsonFetcher, { staleTime: 15_000 },
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const artifacts = data?.artifacts ?? [];

  const handleDelete = useCallback(async (id: string) => {
    setDeletingId(id);
    setDeleteError(null);
    const result = await artifactsService.deleteArtifact(id);
    setDeletingId(null);
    if (!result.success) {
      setDeleteError(result.error || 'Failed to delete artifact');
      return;
    }
    setConfirmingId(null);
    mutate();
  }, [mutate]);

  if (creating) {
    return (
      <ArtifactCreate
        existingTitles={artifacts.map((a) => a.title)}
        onBack={() => setCreating(false)}
        onCreated={(id) => { setCreating(false); mutate(); setSelectedId(id); }}
      />
    );
  }

  if (selectedId) {
    return (
      <ArtifactDetail
        id={selectedId}
        onBack={() => { setSelectedId(null); mutate(); }}
        onDeleted={() => { setSelectedId(null); mutate(); }}
      />
    );
  }

  return (
    <ChartPanel title="Artifacts" subtitle="Living documents Aurora maintains across runs" loading={isLoading}>
      <div className="flex justify-end mb-4">
        <button
          onClick={() => setCreating(true)}
          className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-xs font-medium text-zinc-200 bg-zinc-800 hover:bg-zinc-700 border border-zinc-700 transition-colors"
        >
          <Plus className="h-3.5 w-3.5" />
          New artifact
        </button>
      </div>

      {deleteError && <p className="text-xs text-red-400 mb-3">{deleteError}</p>}

      {artifacts.length === 0 ? (
        <EmptyState
          icon={BookOpen}
          message="No artifacts yet"
          hint="Aurora keeps living documents here — findings lists, cost reports, runbooks — that update across runs. Create one or ask Aurora in an action to maintain it."
        />
      ) : (
        <div className="space-y-2">
          {artifacts.map((a) => (
            <div
              key={a.id}
              className="group flex items-center justify-between border border-zinc-800 rounded-lg px-4 py-3 hover:border-zinc-700 hover:bg-zinc-800/20 transition-colors"
            >
              <button
                onClick={() => setSelectedId(a.id)}
                className="flex-1 min-w-0 text-left"
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-zinc-200 truncate">{a.title}</span>
                  <EditorBadge who={a.lastEditedBy} />
                  <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-zinc-700/60 text-[10px] font-mono text-zinc-300">
                    v{a.version}
                  </span>
                </div>
                <p className="text-xs text-zinc-500 mt-1">Updated {a.updatedAt ? formatTimeAgo(a.updatedAt) : '—'}</p>
              </button>

              <div className="flex items-center gap-1.5 shrink-0 ml-3">
                {confirmingId === a.id ? (
                  <>
                    <button
                      onClick={() => handleDelete(a.id)}
                      disabled={deletingId === a.id}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 transition-colors disabled:opacity-50"
                    >
                      {deletingId === a.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                      Confirm
                    </button>
                    <button
                      onClick={() => setConfirmingId(null)}
                      className="inline-flex items-center px-2 py-1 rounded text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <button
                    onClick={() => setConfirmingId(a.id)}
                    aria-label={`Delete ${a.title}`}
                    className="p-1.5 rounded text-zinc-600 hover:text-red-400 hover:bg-red-500/10 opacity-0 group-hover:opacity-100 transition-all"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </ChartPanel>
  );
}

// ---------------------------------------------------------------------------
// Create view
// ---------------------------------------------------------------------------

function ArtifactCreate({ existingTitles, onBack, onCreated }: {
  existingTitles: string[];
  onBack: () => void;
  onCreated: (id: string) => void;
}) {
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSave = async () => {
    if (!title.trim() || !content.trim()) return;
    // POST is an upsert-by-title server-side; guard here so a human creating a
    // new doc can't silently overwrite an existing one (incl. agent-maintained).
    const collision = existingTitles.some((t) => t.toLowerCase() === title.trim().toLowerCase());
    if (collision) {
      setError('An artifact with this title already exists — open it to edit instead.');
      return;
    }
    setSaving(true);
    setError(null);
    const result = await artifactsService.createArtifact(title.trim(), content);
    setSaving(false);
    if (result.success && result.id) {
      onCreated(result.id);
    } else {
      setError(result.error || 'Failed to create artifact');
    }
  };

  return (
    <ChartPanel title="New artifact" subtitle="Create a living document">
      <div className="flex items-center justify-between mb-4">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-zinc-200 transition-colors"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back
        </button>
        <button
          onClick={handleSave}
          disabled={saving || !title.trim() || !content.trim()}
          className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs text-green-400 hover:text-green-300 bg-green-500/10 hover:bg-green-500/20 border border-green-500/20 transition-colors disabled:opacity-50"
        >
          <Save className="h-3 w-3" />
          {saving ? 'Saving...' : 'Save'}
        </button>
      </div>

      <input
        type="text"
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Artifact title"
        aria-label="Artifact title"
        className="w-full mb-3 px-3 py-2 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-200 placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
      />
      <textarea
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder="Document content in markdown..."
        className="w-full h-96 px-4 py-3 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-300 font-mono focus:outline-none focus:border-zinc-500 resize-y"
      />
      {error && <p className="text-xs text-red-400 mt-2">{error}</p>}
    </ChartPanel>
  );
}

// ---------------------------------------------------------------------------
// Detail view
// ---------------------------------------------------------------------------

type DetailMode = 'view' | 'edit' | 'history';

function ArtifactDetail({ id, onBack, onDeleted }: {
  id: string;
  onBack: () => void;
  onDeleted: () => void;
}) {
  const [artifact, setArtifact] = useState<ArtifactData | null>(null);
  const [loading, setLoading] = useState(true);
  const [mode, setMode] = useState<DetailMode>('view');
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const [versions, setVersions] = useState<ArtifactVersion[]>([]);
  const [currentVersionId, setCurrentVersionId] = useState<string | null>(null);
  const [loadingVersions, setLoadingVersions] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    const data = await artifactsService.getArtifact(id);
    setArtifact(data);
    if (data) setEditContent(data.content);
    setLoading(false);
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const loadVersions = useCallback(async () => {
    setLoadingVersions(true);
    const { versions: list, currentVersionId: cvId } = await artifactsService.getVersions(id);
    setVersions(list);
    setCurrentVersionId(cvId);
    setLoadingVersions(false);
  }, [id]);

  useEffect(() => {
    if (mode === 'history') loadVersions();
  }, [mode, loadVersions]);

  const handleSave = async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    setError(null);
    const result = await artifactsService.updateArtifact(id, editContent);
    setSaving(false);
    if (result.success) {
      await load();
      setMode('view');
    } else {
      setError(result.error || 'Failed to save');
    }
  };

  const handleDelete = async () => {
    setDeleting(true);
    setError(null);
    const result = await artifactsService.deleteArtifact(id);
    setDeleting(false);
    if (!result.success) {
      setConfirmingDelete(false);
      setError(result.error || 'Failed to delete artifact');
      return;
    }
    onDeleted();
  };

  const handleRestore = async (versionId: string) => {
    const result = await artifactsService.restoreVersion(id, versionId);
    if (result.success) {
      await load();
      await loadVersions();
    } else {
      setError(result.error || 'Failed to restore version');
    }
  };

  const segBtn = (m: DetailMode, label: string, Icon: typeof Eye) => (
    <button
      onClick={() => setMode(m)}
      className={`inline-flex items-center gap-1 px-2.5 py-1 text-xs font-medium rounded-md transition-colors ${
        mode === m ? 'bg-zinc-700 text-zinc-100' : 'text-zinc-400 hover:text-zinc-200'
      }`}
    >
      <Icon className="h-3 w-3" />
      {label}
    </button>
  );

  return (
    <ChartPanel title={artifact?.title ?? 'Artifact'} subtitle={artifact ? `Version ${artifact.version}` : undefined} loading={loading}>
      <div className="flex items-center mb-4 gap-3">
        <button
          onClick={onBack}
          className="inline-flex items-center gap-1 text-xs text-zinc-400 hover:text-zinc-200 transition-colors shrink-0"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Back
        </button>

        <div className="flex items-center gap-1 bg-zinc-800/40 rounded-lg p-0.5 ml-auto">
          {segBtn('view', 'View', Eye)}
          {segBtn('edit', 'Edit', Edit2)}
          {segBtn('history', 'History', History)}
        </div>

        <div className="flex items-center gap-1.5 shrink-0">
          {confirmingDelete ? (
            <>
              <button
                onClick={handleDelete}
                disabled={deleting}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-[11px] text-red-400 hover:text-red-300 bg-red-500/10 hover:bg-red-500/20 border border-red-500/20 transition-colors disabled:opacity-50"
              >
                {deleting ? <Loader2 className="h-3 w-3 animate-spin" /> : <Trash2 className="h-3 w-3" />}
                Confirm
              </button>
              <button
                onClick={() => setConfirmingDelete(false)}
                className="inline-flex items-center px-2 py-1 rounded text-[11px] text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                Cancel
              </button>
            </>
          ) : (
            <button
              onClick={() => setConfirmingDelete(true)}
              aria-label="Delete artifact"
              className="p-1.5 rounded text-zinc-500 hover:text-red-400 hover:bg-red-500/10 transition-colors"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>

      {error && <p className="text-xs text-red-400 mb-3">{error}</p>}

      {!loading && !artifact && (
        <EmptyState icon={BookOpen} message="Artifact not found" />
      )}

      {artifact && mode === 'view' && (
        <div className="prose prose-invert prose-sm max-w-none">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={postmortemMarkdownComponents}>
            {artifact.content}
          </ReactMarkdown>
        </div>
      )}

      {artifact && mode === 'edit' && (
        <div>
          <textarea
            value={editContent}
            onChange={(e) => setEditContent(e.target.value)}
            className="w-full h-96 px-4 py-3 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-300 font-mono focus:outline-none focus:border-zinc-500 resize-y"
            placeholder="Document content in markdown..."
          />
          <div className="flex items-center gap-2 mt-3">
            <button
              onClick={handleSave}
              disabled={saving || !editContent.trim()}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs text-green-400 hover:text-green-300 bg-green-500/10 hover:bg-green-500/20 border border-green-500/20 transition-colors disabled:opacity-50"
            >
              <Save className="h-3 w-3" />
              {saving ? 'Saving...' : 'Save'}
            </button>
            <button
              onClick={() => { setEditContent(artifact.content); setMode('view'); }}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
            >
              <X className="h-3 w-3" />
              Cancel
            </button>
          </div>
        </div>
      )}

      {artifact && mode === 'history' && (
        <div>
          {loadingVersions ? (
            <div className="flex items-center justify-center py-8 text-zinc-500">
              <Loader2 className="h-4 w-4 animate-spin" />
            </div>
          ) : versions.length === 0 ? (
            <p className="text-xs text-zinc-500 py-4">No version history available.</p>
          ) : (
            <div className="space-y-1 max-h-[28rem] overflow-y-auto">
              {versions.map((v) => (
                <div
                  key={v.id}
                  className="flex items-center justify-between py-2 px-3 rounded-md bg-zinc-800/40 border border-zinc-800 hover:border-zinc-700 transition-colors"
                >
                  <div className="flex items-center gap-2.5 min-w-0">
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-zinc-700/60 text-[10px] font-mono text-zinc-300">
                      v{v.versionNumber}
                    </span>
                    <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-zinc-700/40 text-[10px] text-zinc-400 capitalize">
                      {v.source}
                    </span>
                    <span className="text-[10px] text-zinc-500">
                      {v.createdAt
                        ? new Date(v.createdAt).toLocaleString(undefined, {
                            month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
                          })
                        : '—'}
                    </span>
                  </div>
                  {v.id === currentVersionId ? (
                    <span className="inline-flex items-center px-2 py-1 rounded-md text-[11px] text-green-400 bg-green-500/10 border border-green-500/20">
                      Current
                    </span>
                  ) : (
                    <button
                      onClick={() => handleRestore(v.id)}
                      className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-blue-400 hover:text-blue-300 bg-blue-500/10 hover:bg-blue-500/20 border border-blue-500/20 transition-colors"
                    >
                      <RotateCcw className="h-3 w-3" />
                      Restore
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </ChartPanel>
  );
}
