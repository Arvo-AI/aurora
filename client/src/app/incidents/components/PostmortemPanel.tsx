'use client';

import { useState, useEffect, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import { Download, Edit2, Save, X, ExternalLink, RefreshCw, FileText } from 'lucide-react';
import { postmortemService, PostmortemData } from '@/lib/services/incidents';

interface PostmortemPanelProps {
  incidentId: string;
  incidentTitle: string;
  isVisible: boolean;
  onClose: () => void;
}



export default function PostmortemPanel({ incidentId, incidentTitle, isVisible, onClose }: PostmortemPanelProps) {
  const [postmortem, setPostmortem] = useState<PostmortemData | null>(null);
  const [editing, setEditing] = useState(false);
  const [editContent, setEditContent] = useState('');
  const [saving, setSaving] = useState(false);
  const [exportingToConfluence, setExportingToConfluence] = useState(false);
  const [showConfluenceForm, setShowConfluenceForm] = useState(false);
  const [confluenceSpaceKey, setConfluenceSpaceKey] = useState('');
  const [confluenceParentPageId, setConfluenceParentPageId] = useState('');
  const [exportSuccess, setExportSuccess] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);

  const loadPostmortem = useCallback(async () => {
    try {
      const data = await postmortemService.getPostmortem(incidentId);
      setPostmortem(data);
      if (data) setEditContent(data.content);
    } catch (e) {
      console.error('Failed to load postmortem:', e);
    }
  }, [incidentId]);

  useEffect(() => {
    if (isVisible) {
      loadPostmortem();
    }
  }, [isVisible, loadPostmortem]);

  // Auto-poll for postmortem when not yet ready
  useEffect(() => {
    if (!isVisible || postmortem !== null) return;
    
    let attempts = 0;
    const MAX_ATTEMPTS = 40; // 120 seconds at 3s interval
    
    const pollInterval = setInterval(() => {
      attempts++;
      if (attempts >= MAX_ATTEMPTS) {
        clearInterval(pollInterval);
        return;
      }
      loadPostmortem();
    }, 3000);
    
    return () => clearInterval(pollInterval);
  }, [isVisible, postmortem, loadPostmortem]);

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
        setShowConfluenceForm(false);
        loadPostmortem(); // Refresh to get confluence URL
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
              <button
                onClick={() => setShowConfluenceForm(!showConfluenceForm)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                <ExternalLink className="w-3 h-3" />
                Export to Confluence
              </button>
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
      {showConfluenceForm && (
        <div className="mb-4 p-4 rounded-lg bg-zinc-900 border border-zinc-800">
          <p className="text-xs text-zinc-400 mb-3">Export postmortem to Confluence</p>
          <div className="space-y-2">
            <div>
              <label className="text-xs text-zinc-500 block mb-1">Space Key *</label>
              <input
                type="text"
                value={confluenceSpaceKey}
                onChange={e => setConfluenceSpaceKey(e.target.value)}
                placeholder="e.g. ENG"
                className="w-full px-3 py-1.5 rounded bg-zinc-800 border border-zinc-700 text-sm text-white placeholder-zinc-600 focus:outline-none focus:border-zinc-500"
              />
            </div>
            <div>
              <label className="text-xs text-zinc-500 block mb-1">Parent Page ID (optional)</label>
              <input
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
                onClick={() => setShowConfluenceForm(false)}
                className="px-3 py-1.5 rounded text-xs text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
          {exportError && <p className="text-xs text-red-400 mt-2">{exportError}</p>}
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

      {/* Content */}
      {postmortem === null ? (
        <div className="flex flex-col items-center justify-center py-12 text-zinc-500 gap-2">
          <RefreshCw className="w-5 h-5 animate-spin" />
          <p className="text-xs">Generating postmortem...</p>
        </div>
      ) : editing ? (
        <textarea
          value={editContent}
          onChange={e => setEditContent(e.target.value)}
          className="w-full h-96 px-4 py-3 rounded-lg bg-zinc-900 border border-zinc-700 text-sm text-zinc-300 font-mono focus:outline-none focus:border-zinc-500 resize-y"
          placeholder="Postmortem content in markdown..."
        />
      ) : (
        <div className="prose prose-invert prose-sm max-w-none">
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
            {postmortem.content}
          </ReactMarkdown>
        </div>
      )}
    </div>
  );
}
