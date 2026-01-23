'use client';

import { useState, useEffect, useRef } from 'react';
import { Citation } from '@/lib/services/incidents';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Copy, Check, Terminal, Clock } from 'lucide-react';
import { RenderOutput } from '@/components/tool-calls/tool-output-renderer';

interface CitationModalProps {
  citation: Citation | null;
  isOpen: boolean;
  onClose: () => void;
}

export default function CitationModal({ citation, isOpen, onClose }: CitationModalProps) {
  const [copied, setCopied] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Cleanup timeout on unmount
  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    };
  }, []);

  if (!citation) return null;

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(citation.output);
      setCopied(true);
      // Clear any existing timeout
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const formatTimestamp = (timestamp?: string) => {
    if (!timestamp) return 'Unknown';
    try {
      return new Date(timestamp).toLocaleString();
    } catch {
      return timestamp;
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-3xl max-h-[80vh] overflow-hidden flex flex-col bg-zinc-900 border-zinc-700">
        <DialogHeader className="flex-shrink-0">
          <DialogTitle className="flex items-center gap-2 text-white">
            <span className="inline-flex items-center justify-center w-6 h-6 text-sm font-medium rounded bg-blue-500/20 text-blue-400 border border-blue-500/30">
              [{citation.key}]
            </span>
            <span>Evidence Citation</span>
          </DialogTitle>
        </DialogHeader>

        <div className="flex-1 overflow-y-auto space-y-4">
          {/* Tool info */}
          <div className="flex flex-wrap items-center gap-4 text-sm">
            <div className="flex items-center gap-2 text-zinc-300">
              <Terminal className="w-4 h-4 text-orange-400" />
              <span className="font-medium">{citation.toolName}</span>
            </div>
            {citation.executedAt && (
              <div className="flex items-center gap-2 text-zinc-500">
                <Clock className="w-4 h-4" />
                <span>{formatTimestamp(citation.executedAt)}</span>
              </div>
            )}
          </div>

          {/* Command */}
          {citation.command && citation.command !== 'Command not available' && (
            <div className="space-y-1">
              <label className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
                Command
              </label>
              <div className="p-3 rounded-lg bg-zinc-800 border border-zinc-700">
                <code className="text-sm font-mono text-orange-300 break-all">
                  {citation.command}
                </code>
              </div>
            </div>
          )}

          {/* Output */}
          <div className="space-y-1">
            <div className="flex items-center justify-between">
              <label className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
                Output
              </label>
              <button
                onClick={handleCopy}
                className="flex items-center gap-1.5 px-2 py-1 text-xs rounded bg-zinc-800 text-zinc-400 hover:text-white hover:bg-zinc-700 transition-colors"
                title="Copy output"
              >
                {copied ? (
                  <>
                    <Check className="w-3.5 h-3.5 text-green-400" />
                    <span>Copied</span>
                  </>
                ) : (
                  <>
                    <Copy className="w-3.5 h-3.5" />
                    <span>Copy</span>
                  </>
                )}
              </button>
            </div>
            <div className="p-4 rounded-lg bg-zinc-950 border border-zinc-800 max-h-[40vh] overflow-auto">
              {citation.output ? (
                <RenderOutput
                  output={citation.output}
                  toolName={citation.toolName}
                  theme="dark"
                  allowEditing={false}
                  editedContent={null}
                  lastSavedContent={null}
                  handleEditorChange={() => {}}
                  handleSave={() => {}}
                  handlePlan={() => {}}
                  hasSavedEdit={false}
                />
              ) : (
                <div className="text-sm text-zinc-400">No output available</div>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
