'use client';

import { useState, useEffect, useCallback, useRef } from 'react';
import ReactMarkdown from 'react-markdown';
import { Download, Edit2, Save, X, ExternalLink, RefreshCw, FileText, Upload, ChevronDown, History, RotateCcw, MessageSquare } from 'lucide-react';
import { postmortemService, type PostmortemData, type PostmortemVersion } from '@/lib/services/incidents';
import { postmortemMarkdownComponents } from '@/lib/markdown-components';
import ExportToNotionDialog from '@/components/postmortem/ExportToNotionDialog';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

interface PostmortemPanelProps {
  readonly incidentId: string;
  readonly incidentTitle: string;
  readonly isVisible: boolean;
  readonly onClose: () => void;
  readonly justResolved?: boolean;
}

function PostmortemContent({ postmortem, regenerating, postmortemNotFound, regenerateSubmitting, editing, editContent, prevContent, onEditChange, onRegenerate }: {
  readonly postmortem: PostmortemData | null;
  readonly regenerating: boolean;
  readonly postmortemNotFound: boolean;
  readonly regenerateSubmitting: boolean;
  readonly editing: boolean;
  readonly editContent: string;
  readonly prevContent: string | null;
  readonly onEditChange: (v: string) => void;
  readonly onRegenerate: () => void;
}) {
  if (postmortem === null && regenerating) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-zinc-500 gap-2">
        <RefreshCw className="w-5 h-5 animate-spin" />
        <p className="text-xs">{prevContent ? 'Regenerating postmortem...' : 'Generating postmortem...'}</p>
      </div>
    );
  }
  if (postmortem === null && postmortemNotFound) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-zinc-500 gap-2">
        <FileText className="w-5 h-5" />
        <p className="text-xs">No postmortem generated yet.</p>
        <button
          onClick={onRegenerate}
          disabled={regenerating || regenerateSubmitting}
          className="mt-2 inline-flex items-center gap-1 px-3 py-1.5 rounded text-xs text-amber-400 hover:text-amber-300 bg-amber-500/10 hover:bg-amber-500/20 transition-colors disabled:opacity-50"
        >
          <RotateCcw className={`w-3 h-3 ${regenerateSubmitting ? 'animate-spin' : ''}`} />
          {regenerateSubmitting ? 'Starting...' : 'Generate Postmortem'}
        </button>
      </div>
    );
  }
  if (postmortem === null) {
    return (
      <div className="flex flex-col items-center justify-center py-12 text-zinc-500 gap-2">
        <RefreshCw className="w-5 h-5 animate-spin" />
        <p className="text-xs">Loading...</p>
      </div>
    );
  }
  if (editing) {
    return (
      <textarea
        value={editContent}
        onChange={e => onEditChange(e.target.value)}
        className="w-full h-96 px-4 py-3 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-300 font-mono focus:outline-none focus:border-zinc-500 resize-y"
        placeholder="Postmortem content in markdown..."
      />
    );
  }
  return (
    <div className="prose prose-invert prose-sm max-w-none">
      <ReactMarkdown components={postmortemMarkdownComponents}>
        {postmortem.content}
      </ReactMarkdown>
    </div>
  );
}

export default function PostmortemPanel({ incidentId, incidentTitle, isVisible, onClose, justResolved }: PostmortemPanelProps) {
  const [postmortem, setPostmortem] = useState<PostmortemData | null>(null);
  const [postmortemNotFound, setPostmortemNotFound] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [exportingToConfluence, setExportingToConfluence] = useState(false);
  const [activeExport, setActiveExport] = useState<'confluence' | 'notion' | null>(null);
  const [confluenceSpaceKey, setConfluenceSpaceKey] = useState('');
  const [confluenceParentPageId, setConfluenceParentPageId] = useState('');
  const [exportSuccess, setExportSuccess] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [regenerating, setRegenerating] = useState(false);
  const [regenerateSubmitting, setRegenerateSubmitting] = useState(false);
  const [regenerateError, setRegenerateError] = useState<string | null>(null);
  const [showVersionHistory, setShowVersionHistory] = useState(false);
  const [versions, setVersions] = useState<PostmortemVersion[]>([]);
  const [currentVersionId, setCurrentVersionId] = useState<string | null>(null);
  const [loadingVersions, setLoadingVersions] = useState(false);

  const notFoundCountRef = useRef(0);

  useEffect(() => {
    notFoundCountRef.current = 0;
  }, [incidentId]);

  const handlePostmortemLoaded = useCallback((data: PostmortemData | null, generating: boolean | undefined) => {
    setPostmortem(data);
    if (data) {
      setEditContent(data.content);
      setPostmortemNotFound(false);
      setRegenerating(false);
      notFoundCountRef.current = 0;
      return;
    }
    if (generating) {
      setPostmortemNotFound(false);
      setRegenerating(true);
      notFoundCountRef.current = 0;
      return;
    }
    notFoundCountRef.current += 1;
    const shouldRetry = !!justResolved && notFoundCountRef.current < 5;
    setRegenerating(shouldRetry);
    setPostmortemNotFound(!shouldRetry);
  }, [justResolved]);

  const loadPostmortem = useCallback(async () => {
    const result = await postmortemService.getPostmortem(incidentId);
    if (result.error) {
      console.error('Failed to load postmortem:', result.error);
      return;
    }
    handlePostmortemLoaded(result.data, result.generating);
  }, [incidentId, handlePostmortemLoaded]);

  useEffect(() => {
    if (isVisible) {
      loadPostmortem();
    }
  }, [isVisible, loadPostmortem]);

  // Auto-poll for postmortem when regenerating
  useEffect(() => {
    if (!isVisible || !regenerating) return;
    
    let attempts = 0;
    const MAX_ATTEMPTS = 40;
    
    const pollInterval = setInterval(() => {
      attempts++;
      if (attempts >= MAX_ATTEMPTS) {
        clearInterval(pollInterval);
        setRegenerating(false);
        return;
      }
      loadPostmortem();
    }, 3000);
    
    return () => clearInterval(pollInterval);
  }, [isVisible, regenerating, loadPostmortem]);

  // Stop regenerating when postmortem content changes
  const prevContentRef = useRef<string | null>(null);
  useEffect(() => {
    if (!regenerating) {
      prevContentRef.current = postmortem?.content ?? null;
      return;
    }
    if (postmortem && postmortem.content !== prevContentRef.current) {
      setRegenerating(false);
      prevContentRef.current = postmortem.content;
    }
  }, [postmortem, regenerating]);

  const handleSave = async () => {
    if (!editContent.trim()) return;
    setSaving(true);
    try {
      await postmortemService.updatePostmortem(incidentId, editContent);
      setPostmortem(prev => prev ? { ...prev, content: editContent } : null);
      setEditing(false);
    } catch (e) {
      console.error('Failed to save postmortem:', e);
    } finally {
      setSaving(false);
    }
  };

  const handleDownload = () => {
    if (!postmortem) return;
    postmortemService.downloadMarkdown(incidentId, postmortem.content, incidentTitle);
  };

  const handleRegenerate = async () => {
    setRegenerateSubmitting(true);
    setRegenerateError(null);
    setPostmortemNotFound(false);
    try {
      const result = await postmortemService.regeneratePostmortem(incidentId);
      if (!result.success) {
        setRegenerateError(result.error || 'Failed to regenerate');
        setPostmortemNotFound(true);
        return;
      }
      setRegenerating(true);
    } catch {
      setRegenerateError('Failed to regenerate');
      setPostmortemNotFound(true);
    } finally {
      setRegenerateSubmitting(false);
    }
  };

  const handleLoadVersions = async () => {
    if (showVersionHistory) {
      setShowVersionHistory(false);
      return;
    }
    setLoadingVersions(true);
    try {
      const { versions: versionList, currentVersionId: cvId } = await postmortemService.getVersions(incidentId);
      setVersions(versionList);
      setCurrentVersionId(cvId);
    } catch {
      setVersions([]);
    }
    setShowVersionHistory(true);
    setLoadingVersions(false);
  };

  const handleRestoreVersion = async (versionId: string) => {
    try {
      const result = await postmortemService.restoreVersion(incidentId, versionId);
      if (!result.success) {
        setRegenerateError(result.error || 'Failed to restore version');
        return;
      }
      if (result.content) {
        setPostmortem(prev => prev ? { ...prev, content: result.content! } : null);
        setEditContent(result.content);
        setShowVersionHistory(false);
        setCurrentVersionId(versionId);
        try {
          const { versions: refreshed, currentVersionId: cvId } = await postmortemService.getVersions(incidentId);
          setVersions(refreshed);
          setCurrentVersionId(cvId);
        } catch {
          // version list refresh is non-critical
        }
      }
    } catch {
      setRegenerateError('Failed to restore version');
    }
  };

  const handleExportToConfluence = async () => {
    if (!confluenceSpaceKey.trim()) return;
    setExportingToConfluence(true);
    setExportError(null);
    setExportSuccess(null);
    try {
      const result = await postmortemService.exportToConfluence(
        incidentId,
        confluenceSpaceKey.trim(),
        confluenceParentPageId.trim() || undefined
      );
      if (result.success) {
        setExportSuccess(result.pageUrl || 'Exported successfully');
        setActiveExport(null);
        await loadPostmortem(); // Refresh to get confluence URL
      } else {
        setExportError(result.error || 'Export failed');
      }
    } catch (e) {
      setExportError('Export failed');
    } finally {
      setExportingToConfluence(false);
    }
  };

  if (!isVisible) return null;

  return (
    <div className="mt-6 pt-6 border-t border-zinc-800">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-2">
          <FileText className="w-4 h-4 text-zinc-400" />
          <h2 className="text-base font-medium text-white">Postmortem</h2>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={loadPostmortem}
            className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-500 hover:text-zinc-300 hover:bg-zinc-800 transition-colors"
            title="Refresh postmortem"
          >
            <RefreshCw className="w-3 h-3" />
          </button>
          {postmortem && !editing && (
            <>
              <button
                onClick={handleRegenerate}
                disabled={regenerating || regenerateSubmitting}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-amber-400 hover:text-amber-300 hover:bg-amber-500/10 transition-colors disabled:opacity-50"
                title="Regenerate postmortem with latest data"
              >
                <RotateCcw className={`w-3 h-3 ${regenerating || regenerateSubmitting ? 'animate-spin' : ''}`} />
                {regenerating || regenerateSubmitting ? 'Generating...' : 'Regenerate'}
              </button>
              <button
                onClick={handleLoadVersions}
                disabled={loadingVersions}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                title="View version history"
              >
                <History className="w-3 h-3" />
                History
              </button>
              <button
                onClick={() => setEditing(true)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                <Edit2 className="w-3 h-3" />
                Edit
              </button>
              <button
                onClick={handleDownload}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                <Download className="w-3 h-3" />
                Download
              </button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
                  >
                    <Upload className="w-3 h-3" />
                    Export
                    <ChevronDown className="w-3 h-3" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-40">
                  <DropdownMenuItem onClick={() => setActiveExport('confluence')}>
                    <ExternalLink className="w-3 h-3" />
                    Confluence
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setActiveExport('notion')}>
                    <ExternalLink className="w-3 h-3" />
                    Notion
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </>
          )}
          {editing && (
            <>
              <button
                onClick={handleSave}
                disabled={saving}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-green-400 hover:text-green-300 hover:bg-green-500/10 transition-colors disabled:opacity-50"
              >
                <Save className="w-3 h-3" />
                {saving ? 'Saving...' : 'Save'}
              </button>
              <button
                onClick={() => { setEditing(false); setEditContent(postmortem?.content || ''); }}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                <X className="w-3 h-3" />
                Cancel
              </button>
            </>
          )}
        </div>
      </div>

      {/* Confluence export form */}
      {activeExport === 'confluence' && (
        <div className="mb-4 p-4 rounded-lg bg-zinc-900 border border-zinc-800">
          <p className="text-xs text-zinc-400 mb-3">Export postmortem to Confluence</p>
          <div className="space-y-2">
            <div>
              <label htmlFor="postmortem-confluence-space-key" className="text-xs text-zinc-500 block mb-1">Space Key *</label>
              <input
                id="postmortem-confluence-space-key"
                type="text"
                value={confluenceSpaceKey}
                onChange={e => setConfluenceSpaceKey(e.target.value)}
                placeholder="e.g. ENG"
                className="w-full px-3 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
              />
            </div>
            <div>
              <label htmlFor="postmortem-confluence-parent-page-id" className="text-xs text-zinc-500 block mb-1">Parent Page ID (optional)</label>
              <input
                id="postmortem-confluence-parent-page-id"
                type="text"
                value={confluenceParentPageId}
                onChange={e => setConfluenceParentPageId(e.target.value)}
                placeholder="e.g. 123456"
                className="w-full px-3 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
              />
            </div>
            <div className="flex gap-2 pt-1">
              <button
                onClick={handleExportToConfluence}
                disabled={exportingToConfluence || !confluenceSpaceKey.trim()}
                className="px-3 py-1.5 rounded text-xs bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
              >
                {exportingToConfluence ? 'Exporting...' : 'Export'}
              </button>
              <button
                onClick={() => setActiveExport(null)}
                className="px-3 py-1.5 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
          {exportError && <p className="text-xs text-red-400 mt-2">{exportError}</p>}
        </div>
      )}

      {/* Regenerate error */}
      {regenerateError && (
        <div className="mb-4 p-3 rounded-lg bg-red-500/10 border border-red-500/30">
          <p className="text-xs text-red-400">{regenerateError}</p>
        </div>
      )}

      {/* Regenerating banner */}
      {regenerating && postmortem && (
        <div className="mb-4 p-3 rounded-lg bg-amber-500/10 border border-amber-500/30 flex items-center gap-2">
          <RefreshCw className="w-3.5 h-3.5 animate-spin text-amber-400" />
          <p className="text-xs text-amber-400">Regenerating postmortem — this may take a minute...</p>
        </div>
      )}

      {/* Version history panel */}
      {showVersionHistory && (
        <div className="mb-4 p-4 rounded-lg bg-zinc-900 border border-zinc-800">
          <div className="flex items-center justify-between mb-3">
            <p className="text-xs text-zinc-400 font-medium">Version History</p>
            <button
              onClick={() => setShowVersionHistory(false)}
              className="text-xs text-zinc-500 hover:text-zinc-300"
            >
              <X className="w-3 h-3" />
            </button>
          </div>
          {versions.length === 0 ? (
            <p className="text-xs text-zinc-500">No version history available.</p>
          ) : (
            <div className="space-y-1 max-h-56 overflow-y-auto">
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
                      {new Date(v.createdAt).toLocaleString(undefined, {
                        month: 'short',
                        day: 'numeric',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </span>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    {v.generationSessionId && (
                      <a
                        href={`/chat?sessionId=${v.generationSessionId}`}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-zinc-400 hover:text-zinc-200 bg-zinc-700/40 hover:bg-zinc-700/70 border border-zinc-700 transition-colors"
                      >
                        <MessageSquare className="w-3 h-3" />
                        Log
                      </a>
                    )}
                    {v.id === currentVersionId ? (
                      <span className="inline-flex items-center px-2 py-1 rounded-md text-[11px] text-green-400 bg-green-500/10 border border-green-500/20">
                        Current
                      </span>
                    ) : (
                      <button
                        onClick={() => handleRestoreVersion(v.id)}
                        className="inline-flex items-center gap-1 px-2 py-1 rounded-md text-[11px] text-blue-400 hover:text-blue-300 bg-blue-500/10 hover:bg-blue-500/20 border border-blue-500/20 transition-colors"
                      >
                        <RotateCcw className="w-3 h-3" />
                        Restore
                      </button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Export success */}
      {exportSuccess && (
        <div className="mb-4 p-3 rounded-lg bg-green-500/10 border border-green-500/30">
          <p className="text-xs text-green-400">
            Exported to Confluence:{' '}
            <a href={exportSuccess} target="_blank" rel="noopener noreferrer" className="underline hover:text-green-300">
              View page
            </a>
          </p>
        </div>
      )}

      {/* Confluence link if already exported */}
      {postmortem?.confluencePageUrl && !exportSuccess && (
        <div className="mb-4">
          <a
            href={postmortem.confluencePageUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
          >
            <ExternalLink className="w-3 h-3" />
            View in Confluence
          </a>
        </div>
      )}

      {/* Notion link if already exported */}
      {postmortem?.notionPageUrl && (
        <div className="mb-4">
          <a
            href={postmortem.notionPageUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
          >
            <ExternalLink className="w-3 h-3" />
            View in Notion
          </a>
        </div>
      )}

      {/* Content */}
      <PostmortemContent
        postmortem={postmortem}
        regenerating={regenerating}
        postmortemNotFound={postmortemNotFound}
        regenerateSubmitting={regenerateSubmitting}
        editing={editing}
        editContent={editContent}
        prevContent={prevContentRef.current}
        onEditChange={setEditContent}
        onRegenerate={handleRegenerate}
      />

      <ExportToNotionDialog
        open={activeExport === 'notion'}
        onOpenChange={(open) => { if (!open) setActiveExport(null); }}
        incidentId={incidentId}
        onExported={() => { loadPostmortem(); }}
      />
    </div>
  );
}
