'use client';

import { useState, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import { Download, ExternalLink, ChevronDown, ChevronRight, FileText, Edit2, Save, X } from 'lucide-react';
import { postmortemService, PostmortemListItem } from '@/lib/services/incidents';

interface ConfluenceFormState {
  spaceKey: string;
  parentPageId: string;
  showForm: boolean;
  exporting: boolean;
  exportSuccess: string | null;
  exportError: string | null;
}

const defaultConfluenceState: ConfluenceFormState = {
  spaceKey: '',
  parentPageId: '',
  showForm: false,
  exporting: false,
  exportSuccess: null,
  exportError: null,
};

interface EditState {
  editing: boolean;
  draft: string;
  saving: boolean;
}

const defaultEditState: EditState = {
  editing: false,
  draft: '',
  saving: false,
};

export function PostmortemsSettings() {
  const [items, setItems] = useState<PostmortemListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [confluenceState, setConfluenceState] = useState<Record<string, ConfluenceFormState>>({});
  const [editState, setEditState] = useState<Record<string, EditState>>({});

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data = await postmortemService.listPostmortems();
        if (!cancelled) setItems(data);
      } catch (e) {
        console.error('Failed to load postmortems:', e);
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
  }, []);

  function getConfluence(id: string): ConfluenceFormState {
    return confluenceState[id] ?? defaultConfluenceState;
  }

  function updateConfluence(id: string, patch: Partial<ConfluenceFormState>) {
    setConfluenceState(prev => ({
      ...prev,
      [id]: { ...(prev[id] ?? defaultConfluenceState), ...patch },
    }));
  }

  function getEdit(id: string): EditState {
    return editState[id] ?? defaultEditState;
  }

  function updateEdit(id: string, patch: Partial<EditState>) {
    setEditState(prev => ({
      ...prev,
      [id]: { ...(prev[id] ?? defaultEditState), ...patch },
    }));
  }

  function handleToggle(id: string) {
    setExpandedId(prev => (prev === id ? null : id));
  }

  function handleDownload(item: PostmortemListItem) {
    postmortemService.downloadMarkdown(
      item.incidentId,
      item.content,
      item.incidentTitle ?? 'postmortem'
    );
  }

  async function handleExport(item: PostmortemListItem) {
    const state = getConfluence(item.id);
    if (!state.spaceKey.trim()) return;

    updateConfluence(item.id, { exporting: true, exportError: null, exportSuccess: null });

    try {
      const result = await postmortemService.exportToConfluence(
        item.incidentId,
        state.spaceKey.trim(),
        state.parentPageId.trim() || undefined
      );

      if (result.success) {
        updateConfluence(item.id, {
          exporting: false,
          exportSuccess: result.pageUrl || 'Exported successfully',
          showForm: false,
        });
      } else {
        updateConfluence(item.id, {
          exporting: false,
          exportError: result.error || 'Export failed',
        });
      }
    } catch {
      updateConfluence(item.id, { exporting: false, exportError: 'Export failed' });
    }
  }

  async function handleSave(item: PostmortemListItem) {
    const draft = getEdit(item.id).draft;
    if (!draft.trim()) return;
    updateEdit(item.id, { saving: true });
    try {
      await postmortemService.updatePostmortem(item.incidentId, draft);
      setItems(prev => prev.map(i => i.id === item.id ? { ...i, content: draft } : i));
      updateEdit(item.id, { editing: false, saving: false });
    } catch {
      updateEdit(item.id, { saving: false });
    }
  }

  function formatDate(dateStr: string): string {
    try {
      return new Date(dateStr).toLocaleDateString(undefined, {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return dateStr;
    }
  }

  if (loading) {
    return (
      <div className="p-6">
        <h2 className="text-xl font-semibold mb-4">Postmortems</h2>
        <div className="h-12 bg-zinc-800 rounded-lg mb-2 animate-pulse" />
        <div className="h-12 bg-zinc-800 rounded-lg mb-2 animate-pulse" />
        <div className="h-12 bg-zinc-800 rounded-lg mb-2 animate-pulse" />
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="p-6">
        <h2 className="text-xl font-semibold mb-4">Postmortems</h2>
        <div className="text-center py-12">
          <FileText className="w-10 h-10 text-zinc-600 mx-auto mb-3" />
          <p className="text-sm text-zinc-500">
            No postmortems yet. Resolve an incident to generate one.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="p-6">
      <h2 className="text-xl font-semibold mb-4">Postmortems</h2>

      {items.map(item => {
        const isExpanded = expandedId === item.id;
        const cfl = getConfluence(item.id);
        const ed = getEdit(item.id);
        const title = item.incidentTitle || `Incident ${item.incidentId.slice(0, 8)}`;

        return (
          <div key={item.id} className="border border-zinc-800 rounded-lg mb-2">
            {/* Collapsed row header */}
            <div
              className="flex items-center justify-between p-4 cursor-pointer hover:bg-zinc-800/50"
              onClick={() => handleToggle(item.id)}
            >
              <div className="flex items-center gap-2 min-w-0">
                {isExpanded ? (
                  <ChevronDown className="w-4 h-4 text-zinc-500 shrink-0" />
                ) : (
                  <ChevronRight className="w-4 h-4 text-zinc-500 shrink-0" />
                )}
                <span className="text-sm text-white truncate">{title}</span>
              </div>
              <span className="text-xs text-zinc-500 shrink-0 ml-4">
                {formatDate(item.generatedAt)}
              </span>
            </div>

            {/* Expanded content */}
            {isExpanded && (
              <div className="px-4 pb-4">
                {/* Rendered markdown or edit textarea */}
                {ed.editing ? (
                  <textarea
                    value={ed.draft}
                    onChange={e => updateEdit(item.id, { draft: e.target.value })}
                    className="w-full h-96 px-4 py-3 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-300 font-mono focus:outline-none focus:border-zinc-500 resize-y mb-4"
                  />
                ) : (
                  <div className="prose prose-invert prose-sm max-w-none mb-4">
                    <ReactMarkdown
                      components={{
                        h1: ({ children }) => <h1 className="text-base font-semibold text-white mb-2">{children}</h1>,
                        h2: ({ children }) => <h2 className="text-sm font-semibold text-white mt-4 mb-2">{children}</h2>,
                        h3: ({ children }) => <h3 className="text-sm font-medium text-zinc-200 mt-3 mb-1">{children}</h3>,
                        strong: ({ children }) => <strong className="text-orange-300 font-semibold">{children}</strong>,
                        p: ({ children }) => <p className="mb-2 text-zinc-300 text-sm leading-normal">{children}</p>,
                        ul: ({ children }) => <ul className="list-disc list-outside ml-4 mb-2 space-y-1">{children}</ul>,
                        li: ({ children }) => <li className="text-zinc-300 text-sm">{children}</li>,
                        code: ({ children }) => <code className="bg-zinc-800 px-1.5 py-0.5 rounded text-orange-300 text-xs font-mono">{children}</code>,
                      }}
                    >
                      {item.content}
                    </ReactMarkdown>
                  </div>
                )}

                {/* Action buttons */}
                <div className="flex items-center gap-2 mb-3">
                  {ed.editing ? (
                    <>
                      <button
                        onClick={() => handleSave(item)}
                        disabled={ed.saving}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors disabled:opacity-50"
                      >
                        <Save className="w-3 h-3" />
                        {ed.saving ? 'Saving...' : 'Save'}
                      </button>
                      <button
                        onClick={() => updateEdit(item.id, { editing: false, draft: item.content })}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                      >
                        <X className="w-3 h-3" />
                        Cancel
                      </button>
                    </>
                  ) : (
                    <>
                      <button
                        onClick={() => updateEdit(item.id, { editing: true, draft: item.content })}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                      >
                        <Edit2 className="w-3 h-3" />
                        Edit
                      </button>
                      <button
                        onClick={() => handleDownload(item)}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                      >
                        <Download className="w-3 h-3" />
                        Download
                      </button>
                      <button
                        onClick={() => updateConfluence(item.id, { showForm: !cfl.showForm })}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                      >
                        <ExternalLink className="w-3 h-3" />
                        Export to Confluence
                      </button>
                    </>
                  )}
                </div>

                {/* Export success */}
                {cfl.exportSuccess && (
                  <div className="mb-3 p-3 rounded-lg bg-green-500/10 border border-green-500/30">
                    <p className="text-xs text-green-400">
                      Exported to Confluence:{' '}
                      <a
                        href={cfl.exportSuccess}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="underline hover:text-green-300"
                      >
                        View page
                      </a>
                    </p>
                  </div>
                )}

                {/* Confluence export form */}
                {cfl.showForm && (
                  <div className="p-4 rounded-lg bg-zinc-900 border border-zinc-800">
                    <p className="text-xs text-zinc-400 mb-3">Export postmortem to Confluence</p>
                    <div className="space-y-2">
                      <div>
                        <label className="text-xs text-zinc-500 block mb-1">Space Key *</label>
                        <input
                          type="text"
                          value={cfl.spaceKey}
                          onChange={e => updateConfluence(item.id, { spaceKey: e.target.value })}
                          placeholder="e.g. ENG"
                          className="w-full px-3 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
                        />
                      </div>
                      <div>
                        <label className="text-xs text-zinc-500 block mb-1">Parent Page ID (optional)</label>
                        <input
                          type="text"
                          value={cfl.parentPageId}
                          onChange={e => updateConfluence(item.id, { parentPageId: e.target.value })}
                          placeholder="e.g. 123456"
                          className="w-full px-3 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
                        />
                      </div>
                      <div className="flex gap-2 pt-1">
                        <button
                          onClick={() => handleExport(item)}
                          disabled={cfl.exporting || !cfl.spaceKey.trim()}
                          className="px-3 py-1.5 rounded text-xs bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
                        >
                          {cfl.exporting ? 'Exporting...' : 'Export'}
                        </button>
                        <button
                          onClick={() => updateConfluence(item.id, { showForm: false })}
                          className="px-3 py-1.5 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                    {cfl.exportError && (
                      <p className="text-xs text-red-400 mt-2">{cfl.exportError}</p>
                    )}
                  </div>
                )}

                {/* Existing Confluence link */}
                {item.confluencePageUrl && !cfl.exportSuccess && (
                  <div className="mt-3">
                    <a
                      href={item.confluencePageUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                    >
                      <ExternalLink className="w-3 h-3" />
                      View in Confluence
                    </a>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
