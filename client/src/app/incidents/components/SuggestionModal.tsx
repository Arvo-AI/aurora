'use client';

import { useState, useEffect, useRef } from 'react';
import { useRouter } from 'next/navigation';
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
import { copyToClipboard } from '@/lib/utils';

interface SuggestionModalProps {
  suggestion: Suggestion | null;
  incidentId: string;
  chatSessionId?: string;
  isOpen: boolean;
  onClose: () => void;
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
}: SuggestionModalProps) {
  const router = useRouter();
  const [copied, setCopied] = useState(false);
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

  useEffect(() => {
    if (!isOpen) {
      setCopied(false);
      setConfirmText('');
    }
  }, [isOpen]);

  if (!suggestion) return null;

  const handleCopy = async () => {
    if (!suggestion.command) return;
    try {
      await copyToClipboard(suggestion.command);
      setCopied(true);
      if (timeoutRef.current) {
        clearTimeout(timeoutRef.current);
      }
      timeoutRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      console.error('Failed to copy:', err);
    }
  };

  const handleExecute = async () => {
    if (!suggestion.command) return;

    // Mark the suggestion as executed in the database
    try {
      await fetch(`/api/incidents/suggestions/${suggestion.id}/mark-executed`, {
        method: 'POST',
      });
    } catch (err) {
      console.error('Failed to mark suggestion as executed:', err);
    }

    const message = `Execute this command and report the output concisely. If the command fails, report the error and stop — do NOT investigate further or run alternative commands.\n\nCommand: ${suggestion.command}`;
    const params = new URLSearchParams({ message, mode: 'agent' });
    if (chatSessionId) {
      params.set('sessionId', chatSessionId);
    }
    onClose();
    router.push(`/chat?${params.toString()}`);
  };

  const handleViewOutput = () => {
    if (!suggestion.executionSessionId) return;
    onClose();
    router.push(`/chat?sessionId=${suggestion.executionSessionId}`);
  };

  const suggestionType = suggestion.type as keyof typeof typeIcons;
  const TypeIcon = typeIcons[suggestionType] || Terminal;
  const typeLabel = typeLabels[suggestionType] || suggestion.type;
  const badgeStyles = typeBadgeStyles[suggestionType] || typeBadgeStyles.diagnostic;

  const requiresConfirmation = ['medium', 'high'].includes(suggestion.risk);
  const isConfirmed = !requiresConfirmation || confirmText === 'CONFIRM';
  const isAlreadyExecuted = Boolean(suggestion.executedAt);

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
          <p className="text-sm text-zinc-300">{suggestion.description}</p>

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

          {isAlreadyExecuted && (
            <div className="flex items-start gap-2 p-3 rounded-lg bg-green-500/10 border border-green-500/30">
              <Check className="w-4 h-4 text-green-400 mt-0.5 flex-shrink-0" />
              <div className="text-xs text-green-300">
                <span className="font-medium">Already executed</span>
                {suggestion.executionStatus === 'completed' && ' — completed successfully'}
                {suggestion.executionStatus === 'failed' && ' — execution failed'}
                {suggestion.executionStatus === 'in_progress' && ' — still running...'}
              </div>
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
          {isAlreadyExecuted && suggestion.executionSessionId && (
            <Button
              variant="outline"
              onClick={handleViewOutput}
              className="border-zinc-700 hover:bg-zinc-800 text-green-400"
            >
              <MessageSquare className="w-4 h-4 mr-2" />
              View Output
            </Button>
          )}
          <Button
            onClick={handleExecute}
            disabled={!suggestion.command || !isConfirmed}
            className="bg-orange-600 hover:bg-orange-700 text-white"
          >
            <Play className="w-4 h-4 mr-2" />
            {isAlreadyExecuted ? 'Re-execute' : 'Execute'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
