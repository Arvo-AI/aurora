'use client';

import { useState, useEffect, useCallback } from 'react';
import { Suggestion, incidentsService } from '@/lib/services/incidents';
import {
  Dialog,
  DialogContent,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Play, Terminal, AlertTriangle, Shield, MessageSquare, Loader2, CheckCircle2, XCircle, X } from 'lucide-react';

interface RunAllSuggestionsModalProps {
  suggestions: Suggestion[];
  incidentId: string;
  chatSessionId?: string;
  isOpen: boolean;
  onClose: () => void;
  onExecutionStarted?: () => void;
}

type StepStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

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

export default function RunAllSuggestionsModal({
  suggestions,
  incidentId,
  chatSessionId,
  isOpen,
  onClose,
  onExecutionStarted,
}: RunAllSuggestionsModalProps) {
  const [stepStatuses, setStepStatuses] = useState<StepStatus[]>([]);
  const [isExecuting, setIsExecuting] = useState(false);
  const [excludedIndices, setExcludedIndices] = useState<Set<number>>(new Set());
  const [executionError, setExecutionError] = useState<string | null>(null);

  // Reset state when modal closes
  useEffect(() => {
    if (!isOpen) {
      setStepStatuses([]);
      setIsExecuting(false);
      setExcludedIndices(new Set());
      setExecutionError(null);
    }
  }, [isOpen]);

  const activeCount = suggestions.length - excludedIndices.size;
  const completedCount = stepStatuses.filter(s => s === 'completed').length;
  const failedCount = stepStatuses.filter(s => s === 'failed').length;

  const buildChatUrl = useCallback(() => {
    const baseUrl = `/api/incidents/${incidentId}/chat`;
    return chatSessionId ? `${baseUrl}?session_id=${chatSessionId}` : baseUrl;
  }, [incidentId, chatSessionId]);

  const handleExclude = (index: number) => {
    setExcludedIndices(prev => {
      const next = new Set(prev);
      next.add(index);
      return next;
    });
  };

  const handleRestore = (index: number) => {
    setExcludedIndices(prev => {
      const next = new Set(prev);
      next.delete(index);
      return next;
    });
  };

  const handleExecuteAll = async () => {
    setIsExecuting(true);
    setExecutionError(null);
    setStepStatuses(suggestions.map((_, i) => excludedIndices.has(i) ? 'skipped' : 'pending'));

    const chatUrl = buildChatUrl();
    let hasNotifiedStart = false;

    for (let i = 0; i < suggestions.length; i++) {
      if (excludedIndices.has(i)) continue;

      const suggestion = suggestions[i];
      if (!suggestion.command) continue;

      // Mark current step as running
      setStepStatuses(prev => {
        const next = [...prev];
        next[i] = 'running';
        return next;
      });

      try {
        const response = await fetch(chatUrl, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            question: `Execute this command: ${suggestion.command}`,
            mode: 'agent',
          }),
        });

        if (!response.ok) {
          const errorData = await response.json().catch(() => ({}));
          throw new Error(errorData.error || `Failed to execute step ${i + 1}`);
        }

        // Notify parent on first successful dispatch
        if (!hasNotifiedStart) {
          onExecutionStarted?.();
          hasNotifiedStart = true;
        }

        // Mark step as completed
        setStepStatuses(prev => {
          const next = [...prev];
          next[i] = 'completed';
          return next;
        });

        // Small delay between dispatches to avoid overwhelming the backend
        if (i < suggestions.length - 1) {
          await new Promise(resolve => setTimeout(resolve, 500));
        }
      } catch (err) {
        console.error(`Failed to execute step ${i + 1}:`, err);
        setStepStatuses(prev => {
          const next = [...prev];
          next[i] = 'failed';
          return next;
        });
        setExecutionError(
          err instanceof Error ? err.message : `Failed to execute step ${i + 1}`
        );
      }
    }

    setIsExecuting(false);
  };

  const allDone = stepStatuses.length > 0 && stepStatuses.every(s => s === 'completed' || s === 'failed' || s === 'skipped');

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && !isExecuting && onClose()}>
      <DialogContent className="max-w-2xl p-0 bg-zinc-950 border-zinc-800 overflow-hidden gap-0 [&>button:last-child]:hidden">
        {/* Header bar */}
        <div className="flex items-center justify-between px-4 py-2.5 bg-zinc-900/80 border-b border-zinc-800">
          <div className="flex items-center gap-2">
            {!allDone && (
              <Button
                onClick={handleExecuteAll}
                disabled={isExecuting || activeCount === 0}
                size="sm"
                className="h-7 px-3 text-xs bg-orange-600 hover:bg-orange-700 text-white"
              >
                {isExecuting ? (
                  <>
                    <Loader2 className="w-3 h-3 mr-1.5 animate-spin" />
                    Running {completedCount + failedCount + 1}/{activeCount}...
                  </>
                ) : (
                  <>
                    <Play className="w-3 h-3 mr-1.5" />
                    Run All ({activeCount})
                  </>
                )}
              </Button>
            )}
            {allDone && (
              <span className={`text-xs ${failedCount > 0 ? 'text-yellow-400' : 'text-green-400'}`}>
                {failedCount > 0 ? `${completedCount} done, ${failedCount} failed` : `All ${completedCount} dispatched`}
              </span>
            )}
          </div>
          <button
            onClick={onClose}
            disabled={isExecuting}
            className="p-1 rounded text-zinc-500 hover:text-white hover:bg-zinc-800 transition-colors disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        {/* Command entries */}
        <div className="max-h-80 overflow-y-auto overflow-x-hidden divide-y divide-zinc-800/60">
              {suggestions.map((suggestion, index) => {
                const suggestionType = suggestion.type as keyof typeof typeIcons;
                const typeLabel = typeLabels[suggestionType] || suggestion.type;
                const badgeStyles = typeBadgeStyles[suggestionType] || typeBadgeStyles.diagnostic;
                const status = stepStatuses[index];
                const isExcluded = excludedIndices.has(index);

                return (
                  <div
                    key={suggestion.id}
                    className={`px-4 py-3 transition-colors ${
                      isExcluded
                        ? 'opacity-30'
                        : status === 'running'
                        ? 'bg-orange-500/5'
                        : status === 'completed'
                        ? 'bg-green-500/5'
                        : status === 'failed'
                        ? 'bg-red-500/5'
                        : ''
                    }`}
                  >
                    <div className="flex items-start gap-3">
                      {/* Status indicator */}
                      <div className="flex-shrink-0 mt-0.5">
                        {isExcluded ? (
                          <X className="w-4 h-4 text-zinc-600" />
                        ) : status === 'running' ? (
                          <Loader2 className="w-4 h-4 text-orange-400 animate-spin" />
                        ) : status === 'completed' ? (
                          <CheckCircle2 className="w-4 h-4 text-green-400" />
                        ) : status === 'failed' ? (
                          <XCircle className="w-4 h-4 text-red-400" />
                        ) : (
                          <span className="flex items-center justify-center w-4 h-4 text-xs text-zinc-500 font-mono">
                            {index + 1}
                          </span>
                        )}
                      </div>

                      {/* Content */}
                      <div className="flex-1 min-w-0 overflow-hidden">
                        <div className="flex items-center gap-2">
                          <span className={`text-sm font-medium truncate ${isExcluded ? 'text-zinc-500 line-through' : 'text-white'}`}>
                            {suggestion.title}
                          </span>
                          <span className={`flex-shrink-0 px-1.5 py-0.5 text-[10px] rounded border ${badgeStyles}`}>
                            {typeLabel}
                          </span>
                          <span className={`flex-shrink-0 px-1.5 py-0.5 text-[10px] rounded border ${incidentsService.getRiskColor(suggestion.risk)}`}>
                            {suggestion.risk}
                          </span>
                        </div>
                        {!isExcluded && suggestion.command && (
                          <div className="mt-1.5 flex items-center gap-1.5 overflow-hidden">
                            <span className="text-green-500 font-mono text-xs flex-shrink-0">$</span>
                            <code className="text-xs font-mono text-zinc-400 overflow-hidden text-ellipsis whitespace-nowrap">
                              {suggestion.command.split('\n')[0]}
                            </code>
                          </div>
                        )}
                      </div>

                      {/* Remove / Restore button */}
                      {!isExecuting && !allDone && (
                        <button
                          onClick={() => isExcluded ? handleRestore(index) : handleExclude(index)}
                          className={`flex-shrink-0 mt-0.5 p-1 rounded transition-colors ${
                            isExcluded
                              ? 'text-zinc-500 hover:text-green-400 hover:bg-green-500/10'
                              : 'text-zinc-600 hover:text-red-400 hover:bg-red-500/10'
                          }`}
                          title={isExcluded ? 'Restore this command' : 'Remove from run'}
                        >
                          {isExcluded ? (
                            <Play className="w-3.5 h-3.5" />
                          ) : (
                            <X className="w-3.5 h-3.5" />
                          )}
                        </button>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
        {/* Error message */}
        {executionError && (
          <div className="flex items-start gap-2 px-4 py-2.5 border-t border-red-500/30 bg-red-500/5">
            <AlertTriangle className="w-3.5 h-3.5 text-red-400 mt-0.5 flex-shrink-0" />
            <p className="text-xs text-red-300">{executionError}</p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
