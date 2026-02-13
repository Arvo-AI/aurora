'use client';

import { useState, useEffect, useRef } from 'react';
import { Suggestion, incidentsService } from '@/lib/services/incidents';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Copy, Check, Play, Terminal, AlertTriangle, Shield, MessageSquare, Loader2 } from 'lucide-react';

interface SuggestionModalProps {
  suggestion: Suggestion | null;
  incidentId: string;
  chatSessionId?: string;  // Existing RCA session to continue
  isOpen: boolean;
  onClose: () => void;
  onExecutionStarted?: () => void;  // Callback when execution starts
  onSuggestionExecuted?: (id: string) => void;  // Callback when suggestion is dispatched
}

const typeIcons = {
  diagnostic: Terminal,
  mitigation: Shield,
  communication: MessageSquare,
};

const typeLabels = {
  diagnostic: 'Diagnostic',
  mitigation: 'Mitigation',
  communication: 'Communication',
};

const typeBadgeStyles = {
  diagnostic: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  mitigation: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  communication: 'bg-pink-500/20 text-pink-400 border-pink-500/30',
};

export default function SuggestionModal({
  suggestion,
  incidentId,
  chatSessionId,
  isOpen,
  onClose,
  onExecutionStarted,
  onSuggestionExecuted,
}: SuggestionModalProps) {
  const [copied, setCopied] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const [executeError, setExecuteError] = useState<string | null>(null);
  const [confirmText, setConfirmText] = useState('');
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
        timeoutRef.current = null;
      }
    };
  }, []);

  // Reset state when modal closes
  useEffect(() => {
    if (!isOpen) {
      setExecuteError(null);
      setIsExecuting(false);
      setCopied(false);
      setConfirmText('');
    }
  }, [isOpen]);

  if (!suggestion) return null;

  const handleCopy = async () => {
    if (!suggestion.command) return;
    try {
      await navigator.clipboard.writeText(suggestion.command);
      setCopied(true);
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const buildChatUrl = () => {
    const baseUrl = `/api/incidents/${incidentId}/chat`;
    return chatSessionId ? `${baseUrl}?session_id=${chatSessionId}` : baseUrl;
  };

  const handleExecute = async () => {
    if (!suggestion.command) return;
    setIsExecuting(true);
    setExecuteError(null);

    try {
      const response = await fetch(buildChatUrl(), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          question: `Execute this command: ${suggestion.command}`,
          mode: 'agent',
        }),
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.error || 'Failed to execute command');
      }

      onExecutionStarted?.();
      onSuggestionExecuted?.(suggestion.id);
      onClose();
    } catch (err) {
      console.error('Failed to execute command:', err);
      setExecuteError(err instanceof Error ? err.message : 'Failed to execute command');
      setIsExecuting(false);
    }
  };

  const suggestionType = suggestion.type as keyof typeof typeIcons;
  const TypeIcon = typeIcons[suggestionType] || Terminal;
  const typeLabel = typeLabels[suggestionType] || suggestion.type;
  const badgeStyles = typeBadgeStyles[suggestionType] || typeBadgeStyles.diagnostic;

  // High-risk commands require typing CONFIRM
  const requiresConfirmation = ['medium', 'high'].includes(suggestion.risk);
  const isConfirmed = !requiresConfirmation || confirmText === 'CONFIRM';

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl bg-zinc-900 border-zinc-700">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-white">
            <TypeIcon className="w-5 h-5 text-orange-400" />
            {suggestion.title}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          {/* Description */}
          <p className="text-sm text-zinc-300">{suggestion.description}</p>

          {/* Type and Risk badges */}
          <div className="flex items-center gap-3">
            <span className={`px-2 py-1 text-xs rounded border ${badgeStyles}`}>
              {typeLabel}
            </span>
            <span
              className={`px-2 py-1 text-xs rounded border ${incidentsService.getRiskColor(suggestion.risk)}`}
            >
              {suggestion.risk} risk
            </span>
          </div>

          {/* Command */}
          {suggestion.command && (
            <div className="space-y-2">
              <label className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
                Command
              </label>
              <div className="relative p-4 rounded-lg bg-zinc-950 border border-zinc-800">
                <code className="text-sm font-mono text-orange-300 whitespace-pre-wrap break-all">
                  {suggestion.command}
                </code>
              </div>
            </div>
          )}

          {/* Warning and confirmation for medium/high risk */}
          {requiresConfirmation && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 p-3 rounded-lg bg-yellow-500/10 border border-yellow-500/30">
                <AlertTriangle className="w-4 h-4 text-yellow-400 mt-0.5 flex-shrink-0" />
                <p className="text-xs text-yellow-300">
                  This command may modify your infrastructure. Review carefully before executing.
                </p>
              </div>
              <div className="space-y-2">
                <label className="text-xs font-medium text-zinc-400">
                  Type <span className="font-mono text-orange-400">CONFIRM</span> to enable execution
                </label>
                <input
                  type="text"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder="CONFIRM"
                  className="w-full px-3 py-2 text-sm bg-zinc-950 border border-zinc-700 rounded-md text-white placeholder:text-zinc-600 focus:outline-none focus:border-orange-500"
                />
              </div>
            </div>
          )}

          {/* Error message */}
          {executeError && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-red-500/10 border border-red-500/30">
              <AlertTriangle className="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-red-300">{executeError}</p>
            </div>
          )}
        </div>

        <DialogFooter className="flex gap-2 sm:gap-2">
          <Button
            variant="outline"
            onClick={handleCopy}
            disabled={!suggestion.command}
            className="border-zinc-700 hover:bg-zinc-800"
          >
            {copied ? (
              <>
                <Check className="w-4 h-4 mr-2 text-green-400" />
                Copied
              </>
            ) : (
              <>
                <Copy className="w-4 h-4 mr-2" />
                Copy Command
              </>
            )}
          </Button>
          <Button
            onClick={handleExecute}
            disabled={!suggestion.command || isExecuting || !isConfirmed}
            className="bg-orange-600 hover:bg-orange-700 text-white"
          >
            {isExecuting ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                Starting...
              </>
            ) : (
              <>
                <Play className="w-4 h-4 mr-2" />
                Execute
              </>
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
