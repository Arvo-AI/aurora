'use client';

import { Citation, Suggestion } from '@/lib/services/incidents';
import {
  ChevronDown,
  CheckCircle2,
  AlertCircle,
  Play,
  GitBranch,
  Copy,
} from 'lucide-react';
import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { canExecuteCommand, type ExecutionCapabilities } from '@/hooks/use-execution-capabilities';

const CODE_PILL = "font-mono text-[0.86em] bg-white/[.055] text-[#AEB4BE] border border-white/[.06] rounded-[6px] px-1.5 py-px [overflow-wrap:anywhere] break-words";

const TOKEN_RE = /(`[^`]+`|\*\*[^*]+\*\*|\[\d+(?:,\s*\d+)*\])/g;

function isCodeToken(part: string): boolean {
  return /^[A-Z][a-z]+[A-Z]\w*$/.test(part)
    || /^[a-z]+[A-Z]\w*$/.test(part)
    || /^[\w./]+\.(?:go|py|ts|js|yml|yaml|json|toml|md)$/.test(part)
    || /^\d+(?:\.\d+)?(?:ms|s|m|MB|GB|Ki|Mi|Gi|%)$/.test(part)
    || /^[0-9a-f]{7,40}$/.test(part);
}

function formatPlainSegment(segment: string, baseKey: string): React.ReactNode {
  const words = segment.split(/\s+/).filter(Boolean);
  return words.map((word, j) => {
    const prefix = j > 0 ? ' ' : '';
    if (isCodeToken(word)) return <React.Fragment key={`${baseKey}-${word}`}>{prefix}<code className={CODE_PILL}>{word}</code></React.Fragment>;
    return <React.Fragment key={`${baseKey}-${word}-${j}`}>{prefix}{word}</React.Fragment>;
  });
}

function formatDescriptionWithCode(text: string): React.ReactNode {
  const parts = text.split(TOKEN_RE);
  return parts.map((part, i) => {
    if (!part) return null;
    const codeMatch = /^`([^`]+)`$/.exec(part);
    if (codeMatch) return <code key={`code-${i}`} className={CODE_PILL}>{codeMatch[1]}</code>;
    const boldMatch = /^\*\*([^*]+)\*\*$/.exec(part);
    if (boldMatch) return <strong key={`bold-${i}`} className="text-zinc-200 font-semibold">{boldMatch[1]}</strong>;
    if (/^\[\d+(?:,\s*\d+)*\]$/.test(part)) return null;
    return <span key={`seg-${i}`}>{formatPlainSegment(part, `seg-${i}`)}</span>;
  });
}

function getSevGlyph(s: Suggestion): string {
  if (s.risk === 'high') return '!!';
  if (s.risk === 'medium') return '!';
  if (s.type === 'prevent') return '~';
  return '·';
}

function getSevColor(s: Suggestion): string {
  if (s.risk === 'high') return 'text-rose-400';
  if (s.risk === 'medium') return 'text-amber-400';
  if (s.type === 'prevent') return 'text-blue-400';
  return 'text-zinc-500';
}

function getActionLabel(s: Suggestion, isFixType: boolean, wasExecuted: boolean, execStatus: string | undefined, canExec: boolean): string | null {
  if (isFixType) return s.prUrl ? 'View PR' : 'Create PR';
  if (wasExecuted) {
    if (execStatus === 'completed') return 'Done';
    if (execStatus === 'failed') return 'Failed';
    return 'View output';
  }
  if (s.command) return canExec ? 'Run' : 'Copy';
  return null;
}

export default function NextStepsConsole({ suggestions, citations, canWrite, execCaps, onRunSuggestion, onFixSuggestion, onCitationClick }: {
  readonly suggestions: Suggestion[];
  readonly citations: Citation[];
  readonly canWrite: boolean;
  readonly execCaps: ExecutionCapabilities;
  readonly onRunSuggestion: (s: Suggestion) => void;
  readonly onFixSuggestion: (s: Suggestion) => void;
  readonly onCitationClick: (c: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [expandedItems, setExpandedItems] = useState<Set<string>>(new Set());
  const pendingCount = suggestions.filter(s => !s.executedAt).length;
  const doneCount = suggestions.filter(s => s.executedAt && s.executionStatus === 'completed').length;

  const toggleItem = (id: string) => {
    setExpandedItems(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  return (
    <div className="mt-5 rounded-[14px] border border-white/[.07] overflow-hidden bg-[#0A0C0F]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 border-b border-white/[.07] bg-[#0D0F13] hover:bg-[#10131a] transition-colors"
      >
        <span className="w-2 h-2 rounded-sm bg-emerald-400 shadow-[0_0_8px_theme(colors.emerald.400)]" />
        <span className="text-[13px] font-semibold text-zinc-200 flex-1 text-left">
          Next Steps{' '}
          <span className="ml-2 text-[11px] font-normal text-zinc-500">
            {pendingCount > 0 && <>{pendingCount} pending</>}
            {doneCount > 0 && pendingCount > 0 && ' · '}
            {doneCount > 0 && <>{doneCount} done</>}
          </span>
        </span>
        <motion.span animate={{ rotate: expanded ? 0 : -90 }} transition={{ duration: 0.2 }}>
          <ChevronDown className="w-3.5 h-3.5 text-zinc-500" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="next-steps-content"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ height: { duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }, opacity: { duration: 0.2, delay: 0.05 } }}
            className="overflow-hidden"
          >
            <div className="max-h-[70vh] overflow-y-auto">
            {suggestions.map((suggestion, idx) => {
        const isFixType = suggestion.type === 'fix';
        const wasExecuted = Boolean(suggestion.executedAt);
        const execStatus = suggestion.executionStatus;
        const isFirst = idx === 0;

        const sevGlyph = getSevGlyph(suggestion);
        const sevColor = getSevColor(suggestion);

        const canExec = suggestion.command ? canExecuteCommand(suggestion.command, execCaps) : true;

        const actionLabel = getActionLabel(suggestion, isFixType, wasExecuted, execStatus, canExec);

        const isPrimary = !wasExecuted && suggestion.command && !isFixType && isFirst && canExec;

        const descText = (suggestion.description || '') + ' ' + (suggestion.rationale || '');
        const citationKeys = new Set(
          [...descText.matchAll(/\[(\d+(?:,\s*\d+)*)\]/g)]
            .flatMap(m => m[1].split(/,\s*/))
        );
        const relatedCitations = citations.filter(c => citationKeys.has(c.key));

        return (
          <div
            key={suggestion.id}
            className={`grid grid-cols-[34px_1fr_auto] items-start transition-colors hover:bg-white/[.022] ${idx > 0 ? 'border-t border-white/[.045]' : ''}`}
          >
            <div className={`pt-3.5 pb-3.5 pl-3 pr-2.5 text-right font-mono text-[11px] select-none ${sevColor}`}>
              {sevGlyph}
            </div>
            <div className="py-3.5 pr-2 min-w-0 [overflow-wrap:anywhere]">
              <p className="text-[13px] font-semibold text-zinc-100 tracking-[-0.01em] mb-1 break-words">{formatDescriptionWithCode(suggestion.title)}</p>
              {!expandedItems.has(suggestion.id) && (
                <p className="text-[12.5px] text-zinc-400 leading-[1.55]">
                  {formatDescriptionWithCode(suggestion.summary || suggestion.description)}
                  {' '}
                  <button onClick={() => toggleItem(suggestion.id)} className="text-[11px] text-zinc-600 hover:text-zinc-400 transition-colors">more</button>
                </p>
              )}
              <AnimatePresence initial={false}>
                {expandedItems.has(suggestion.id) && (
                  <motion.div
                    key="expanded"
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ height: { duration: 0.2, ease: [0.25, 0.1, 0.25, 1] }, opacity: { duration: 0.15 } }}
                    className="overflow-hidden"
                  >
                  <div className="max-h-[50vh] overflow-y-auto">
                    {(() => {
                      const desc = suggestion.description || '';
                      const rootCauseSplit = desc.split(/\s*\*?\*?Root Cause:\*?\*?\s*/);
                      const actionText = rootCauseSplit[0];
                      const rootCauseText = rootCauseSplit.length > 1 ? rootCauseSplit.slice(1).join(' ') : null;
                      return (
                        <>
                          <p className="text-[12.5px] text-zinc-400 leading-[1.55]">{formatDescriptionWithCode(actionText)}</p>
                          {(rootCauseText || suggestion.rationale) && (
                            <p className="text-[11.5px] text-zinc-500 mt-1.5 leading-[1.5]">{formatDescriptionWithCode(rootCauseText || suggestion.rationale || '')}</p>
                          )}
                          {rootCauseText && suggestion.rationale && (
                            <p className="text-[11.5px] text-zinc-500 mt-1.5 leading-[1.5]">{formatDescriptionWithCode(suggestion.rationale)}</p>
                          )}
                        </>
                      );
                    })()}
                    {suggestion.undo && (suggestion.risk === 'high' || suggestion.risk === 'medium') && (
                      <p className="text-[11px] text-zinc-500 mt-1.5 font-mono">
                        <span className="text-zinc-600">undo:</span> {suggestion.undo}
                      </p>
                    )}
                    <button onClick={() => toggleItem(suggestion.id)} className="text-[11px] text-zinc-600 hover:text-zinc-400 mt-1.5 transition-colors">less</button>
                  </div>
                  </motion.div>
                )}
              </AnimatePresence>
              {(suggestion.filePath || relatedCitations.length > 0) && (
                <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
                  {suggestion.filePath && (
                    <span className="font-mono text-[10.5px] text-zinc-400 border border-white/[.06] rounded-[5px] px-1.5 py-px">{suggestion.filePath}</span>
                  )}
                  {relatedCitations.map(c => (
                    <button
                      key={c.key}
                      onClick={(e) => { e.stopPropagation(); onCitationClick(c); }}
                      className="font-mono text-[10px] text-zinc-500 border border-white/[.07] rounded-[5px] px-1.5 py-px hover:text-emerald-400 hover:border-emerald-400/30 transition-colors"
                    >
                      {c.key}
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="flex items-center py-3.5 pr-3.5 pl-1.5 self-start">
              {(() => {
                if (actionLabel && canWrite) {
                  return (
                    <button
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        if (isFixType) {
                          onFixSuggestion(suggestion);
                        } else if (!canExec && suggestion.command) {
                          navigator.clipboard.writeText(suggestion.command);
                        } else {
                          onRunSuggestion(suggestion);
                        }
                      }}
                      className={`inline-flex items-center gap-[7px] px-3.5 py-2 rounded-[9px] text-[12.5px] font-semibold tracking-[.005em] whitespace-nowrap transition-all active:translate-y-px ${
                        (() => {
                          if (wasExecuted && execStatus === 'completed') return 'bg-[#14171C] text-emerald-400 border border-white/[.07]';
                          if (wasExecuted && execStatus === 'failed') return 'bg-[#14171C] text-rose-400 border border-white/[.07]';
                          if (isPrimary) return 'bg-gradient-to-b from-emerald-300 to-emerald-400 text-[#04140F] border border-transparent shadow-[0_1px_0_rgba(255,255,255,.25)_inset,0_6px_18px_-8px_rgba(63,224,182,.7)] hover:shadow-[0_1px_0_rgba(255,255,255,.3)_inset,0_8px_22px_-8px_rgba(63,224,182,.9)]';
                          return 'bg-[#14171C] text-zinc-100 border border-white/[.07] hover:border-white/[.18] hover:bg-[#181b21]';
                        })()
                      }`}
                    >
                      {(() => {
                        if (isFixType) return <GitBranch className="w-[13px] h-[13px]" />;
                        if (wasExecuted && execStatus === 'completed') return <CheckCircle2 className="w-[13px] h-[13px]" />;
                        if (wasExecuted && execStatus === 'failed') return <AlertCircle className="w-[13px] h-[13px]" />;
                        if (wasExecuted) return <Play className="w-[13px] h-[13px]" />;
                        if (canExec) return <Play className="w-[13px] h-[13px]" />;
                        return <Copy className="w-[13px] h-[13px]" />;
                      })()}
                      {actionLabel}
                    </button>
                  );
                }
                if (actionLabel && !canWrite) {
                  return <span className="text-[12.5px] text-zinc-500">{actionLabel}</span>;
                }
                return null;
              })()}
            </div>
          </div>
        );
      })}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
