'use client';

import { Incident, AuroraStatus, Citation, Suggestion, incidentsService, getSourceIconSrc, getSourceIconBgColor } from '@/lib/services/incidents';
import { Badge } from '@/components/ui/badge';
import {
  ExternalLink,
  Clock,
  Server,
  ChevronDown,
  ChevronUp,
  CheckCircle2,
  AlertCircle,
  ChevronRight,
  Play,
  GitBranch,
  FileText,
  Coins,
  Activity,
  Copy,
} from 'lucide-react';
import React, { useState, useMemo, useCallback } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { useToast } from '@/hooks/use-toast';
import { useUser } from '@/hooks/useAuthHooks';
import { canWrite as checkCanWrite } from '@/lib/roles';
import Link from 'next/link';
import Image from 'next/image';
import CitationBadge from './CitationBadge';
import CitationModal from './CitationModal';
import SuggestionModal from './SuggestionModal';
import FixSuggestionModal from './FixSuggestionModal';
import IncidentFeedback from './IncidentFeedback';
import CorrelatedAlertsSection from './CorrelatedAlertsSection';
import RecentAlertsSection from './RecentAlertsSection';
import PostmortemPanel from './PostmortemPanel';
import InfrastructureVisualization from '@/components/incidents/InfrastructureVisualization';
import IncidentActionRuns from './IncidentActionRuns';
import ExecutionWaterfall from './ExecutionWaterfall';
import { ReactFlowProvider } from '@xyflow/react';
import { connectorRegistry } from '@/components/connectors/ConnectorRegistry';
import { useExecutionCapabilities, canExecuteCommand, type ExecutionCapabilities } from '@/hooks/use-execution-capabilities';

function sourceDisplayName(source: string): string {
  const connector = connectorRegistry.get(source);
  if (connector) return connector.name;
  return source.charAt(0).toUpperCase() + source.slice(1);
}

interface IncidentCardProps {
  readonly incident: Incident;
  readonly duration: string;
  readonly showThoughts: boolean;
  readonly onToggleThoughts: () => void;
  readonly citations?: Citation[];
  readonly onRefresh?: () => void;
}

function StatusPill({ status }: { readonly status: AuroraStatus }) {
  switch (status) {
    case 'running':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-orange-500/10 border border-orange-500/30">
          <span className="relative flex h-2.5 w-2.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-orange-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-orange-500"></span>
          </span>
          <span className="text-xs font-semibold text-orange-400">Aurora Investigating...</span>
        </div>
      );
    case 'summarizing':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-blue-500/10 border border-blue-500/30">
          <span className="relative flex h-2.5 w-2.5">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-blue-400 opacity-75"></span>
            <span className="relative inline-flex rounded-full h-2.5 w-2.5 bg-blue-500"></span>
          </span>
          <span className="text-xs font-semibold text-blue-400">Generating Summary...</span>
        </div>
      );
    case 'complete':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-green-500/10 border border-green-500/30">
          <CheckCircle2 className="w-3.5 h-3.5 text-green-400" />
          <span className="text-xs font-semibold text-green-400">Analysis Complete</span>
        </div>
      );
    case 'error':
      return (
        <div className="flex items-center gap-2 px-3 py-1.5 rounded-full bg-red-500/10 border border-red-500/30">
          <AlertCircle className="w-3.5 h-3.5 text-red-400" />
          <span className="text-xs font-semibold text-red-400">Analysis Error</span>
        </div>
      );
    default:
      return null;
  }
}

function isSafeUrl(url: string | undefined): boolean {
  if (!url) return false;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:';
  } catch {
    return false;
  }
}

type ChildrenProps = { children?: React.ReactNode };

function buildMdComponents(processChildren: (children: React.ReactNode) => React.ReactNode) {
  return {
    h1: (props: ChildrenProps) => <h1 className="text-base font-semibold text-white mb-1">{processChildren(props.children)}</h1>,
    h2: (props: ChildrenProps) => <h2 className="text-sm font-semibold text-white mt-3 mb-1">{processChildren(props.children)}</h2>,
    strong: (props: ChildrenProps) => <strong className="text-orange-300 font-semibold">{processChildren(props.children)}</strong>,
    p: (props: ChildrenProps) => <p className="mb-2 text-zinc-300 text-sm leading-normal">{processChildren(props.children)}</p>,
    ol: (props: ChildrenProps) => <ol className="list-decimal list-outside ml-4 mb-2 space-y-2">{props.children}</ol>,
    ul: (props: ChildrenProps) => <ul className="list-disc list-outside ml-4 mb-2 space-y-2">{props.children}</ul>,
    li: (props: ChildrenProps) => <li className="text-zinc-300 text-sm [&>p]:inline [&>p]:mb-0">{processChildren(props.children)}</li>,
    code: (props: ChildrenProps) => <code className="bg-zinc-800 px-1.5 py-0.5 rounded text-orange-300 text-xs font-mono">{props.children}</code>,
    table: (props: ChildrenProps) => <table className="w-full text-sm border-collapse my-3">{props.children}</table>,
    thead: (props: ChildrenProps) => <thead className="border-b border-zinc-700">{props.children}</thead>,
    tbody: (props: ChildrenProps) => <tbody>{props.children}</tbody>,
    tr: (props: ChildrenProps) => <tr className="border-b border-zinc-800/50">{props.children}</tr>,
    th: (props: ChildrenProps) => <th className="text-left text-xs font-semibold text-zinc-400 py-1.5 pr-4">{processChildren(props.children)}</th>,
    td: (props: ChildrenProps) => <td className="text-zinc-300 text-sm py-1.5 pr-4">{processChildren(props.children)}</td>,
  };
}

function parseRuledOutItems(content: string | undefined) {
  if (!content) return [];
  return content
    .split(/(?:^|\n)\s*[-*]\s+/)
    .filter(s => s.trim())
    .map(item => {
      const cleaned = item.replace(/^\*\*/, '').trim();
      const dashSplit = /^(.+?)\s*[—–]\s*([\s\S]+)$/.exec(cleaned);
      if (dashSplit) {
        const title = dashSplit[1].replaceAll('**', '').trim();
        const explanation = dashSplit[2].trim();
        return { title, explanation };
      }
      return { title: cleaned.replaceAll('**', ''), explanation: '' };
    });
}

function renderRuledOutItemText(
  str: string,
  citations: Citation[],
  onCitationClick: (c: Citation) => void,
) {
  const parts = str.split(/(`[^`]+`|\[\d+(?:,\s*\d+)*\])/g);
  return parts.map((part, i) => {
    if (!part) return null;
    const codeMatch = /^`([^`]+)`$/.exec(part);
    if (codeMatch) return <code key={`code-${i}`} className="font-mono text-[0.86em] bg-white/[.055] text-[#AEB4BE] border border-white/[.06] rounded-[6px] px-1.5 py-px whitespace-nowrap">{codeMatch[1]}</code>;
    const citeMatch = /^\[(\d+(?:,\s*\d+)*)\]$/.exec(part);
    if (citeMatch) {
      const keys = citeMatch[1].split(/,\s*/);
      return (<span key={`cites-${i}`}>{keys.map(key => {
        const citation = citations.find(c => c.key === key);
        return citation ? (
          <button key={`cite-${key}`} onClick={() => onCitationClick(citation)} className="font-mono text-[10px] text-zinc-500 border border-white/[.07] rounded-[5px] px-1.5 py-px mx-0.5 hover:text-emerald-400 hover:border-emerald-400/30 transition-colors">{key}</button>
        ) : <span key={`cite-${key}`} className="font-mono text-[10px] text-zinc-500 border border-white/[.07] rounded-[5px] px-1.5 py-px mx-0.5">{key}</span>;
      })}</span>);
    }
    return <span key={`text-${i}`}>{part}</span>;
  });
}

function RuledOutConsole({ text, citations, onCitationClick }: {
  readonly text: string;
  readonly citations: Citation[];
  readonly onCitationClick: (c: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const sections = useMemo(() => {
    const ruledOutMatch = /##\s*Ruled Out\s*\n([\s\S]*?)(?=##\s*Not Checked|$)/.exec(text);
    const notCheckedMatch = /##\s*Not Checked\s*\n([\s\S]*?)$/.exec(text);
    return {
      ruledOut: parseRuledOutItems(ruledOutMatch?.[1]),
      notChecked: parseRuledOutItems(notCheckedMatch?.[1]),
    };
  }, [text]);

  const renderItemText = (str: string) => renderRuledOutItemText(str, citations, onCitationClick);

  if (sections.ruledOut.length === 0 && sections.notChecked.length === 0) return null;

  return (
    <div className="mt-4 rounded-[14px] border border-white/[.07] overflow-hidden bg-[#0A0C0F]">
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center gap-3 px-4 py-3 bg-[#0D0F13] hover:bg-[#10131a] transition-colors"
      >
        <span className="w-1.5 h-1.5 rounded-full bg-zinc-500" />
        <span className="text-[13px] font-semibold text-zinc-400 flex-1 text-left">
          Ruled Out &amp; Not Checked{' '}
          <span className="ml-2 text-[11px] font-normal text-zinc-500">
            {sections.ruledOut.length} eliminated · {sections.notChecked.length} skipped
          </span>
        </span>
        <motion.span animate={{ rotate: expanded ? 0 : -90 }} transition={{ duration: 0.2 }}>
          <ChevronDown className="w-3.5 h-3.5 text-zinc-500" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="ruled-out-content"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ height: { duration: 0.3, ease: [0.25, 0.1, 0.25, 1] }, opacity: { duration: 0.2, delay: 0.05 } }}
            className="overflow-hidden"
          >
            <div className="border-t border-white/[.07]">
              {sections.ruledOut.length > 0 && (
                <>
                  <div className="px-4 pt-3 pb-1.5">
                    <span className="text-[10.5px] tracking-[.05em] uppercase text-zinc-600 font-medium">Ruled Out</span>
                  </div>
                  {sections.ruledOut.map((item) => (
                    <div key={item.title} className="px-4 py-2.5 border-t border-white/[.035] first:border-t-0">
                      <p className="text-[12.5px] font-medium text-zinc-300">{renderItemText(item.title)}</p>
                      {item.explanation && <p className="text-[12px] text-zinc-500 mt-1 leading-relaxed">{renderItemText(item.explanation)}</p>}
                    </div>
                  ))}
                </>
              )}
              {sections.notChecked.length > 0 && (
                <>
                  <div className={`px-4 pt-3 pb-1.5 ${sections.ruledOut.length > 0 ? 'border-t border-white/[.07]' : ''}`}>
                    <span className="text-[10.5px] tracking-[.05em] uppercase text-amber-500/70 font-medium">Not Checked</span>
                  </div>
                  {sections.notChecked.map((item) => (
                    <div key={item.title} className="px-4 py-2.5 border-t border-white/[.035] first:border-t-0">
                      <p className="text-[12.5px] font-medium text-zinc-300">{renderItemText(item.title)}</p>
                      {item.explanation && <p className="text-[12px] text-zinc-500 mt-1 leading-relaxed">{renderItemText(item.explanation)}</p>}
                    </div>
                  ))}
                </>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

const CODE_PILL = "font-mono text-[0.86em] bg-white/[.055] text-[#AEB4BE] border border-white/[.06] rounded-[6px] px-1.5 py-px whitespace-nowrap";

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

function NextStepsConsole({ suggestions, citations, canWrite, execCaps, onRunSuggestion, onFixSuggestion, onCitationClick }: {
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

        // Find citation keys referenced in description or rationale
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
            <div className="py-3.5 pr-2 min-w-0">
              <p className="text-[13px] font-semibold text-zinc-100 tracking-[-0.01em] mb-1">{formatDescriptionWithCode(suggestion.title)}</p>
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
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

export default function IncidentCard({ incident, duration, showThoughts, onToggleThoughts, citations = [], onRefresh }: IncidentCardProps) {
  const [showRawPayload, setShowRawPayload] = useState(false);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [selectedSuggestion, setSelectedSuggestion] = useState<Suggestion | null>(null);
  const [selectedFixSuggestion, setSelectedFixSuggestion] = useState<Suggestion | null>(null);
  const [showVisualization, setShowVisualization] = useState(false);
  const [showPostmortem, setShowPostmortem] = useState(false);
  const [showTokenUsage, setShowTokenUsage] = useState(false);
  const [showWaterfall, setShowWaterfall] = useState(false);
  const [showActions, setShowActions] = useState(false);
  const [resolvingIncident, setResolvingIncident] = useState(false);
  const alert = incident.alert;
  const { toast } = useToast();
  const { user } = useUser();
  const canWrite = checkCanWrite(user?.role);
  const execCaps = useExecutionCapabilities();
  const showSeverity = (alert.severity && (alert.severity as string) !== 'unknown') || incident.status === 'analyzed';
  const sourceIconSrc = getSourceIconSrc(alert.source);
  const sourceIconBgColor = getSourceIconBgColor(alert.source);

  const [justResolved, setJustResolved] = useState(false);

  const handleResolveIncident = async () => {
    setResolvingIncident(true);
    try {
      await incidentsService.resolveIncident(incident.id);
      toast({ title: 'Incident resolved', description: 'Postmortem is being generated in the background.' });
      setJustResolved(true);
      setShowPostmortem(true);
      onRefresh?.();
    } catch (e) {
      console.error('Failed to resolve incident:', e);
      toast({ title: 'Failed to resolve incident', variant: 'destructive' });
    } finally {
      setResolvingIncident(false);
    }
  };

  // Function to render text with citation badges
  const renderTextWithCitations = useCallback((text: string): React.ReactNode => {
    const CITATION_RE = /^\[(\d+(?:,\s*\d+)*)\]$/;
    const parts = text.split(/(\[\d+(?:,\s*\d+)*\])/g);

    return parts.map((part) => {
      const match = CITATION_RE.exec(part);
      if (match) {
        const keys = match[1].split(/,\s*/).map(k => k.trim());
        return (
          <span key={`cg-${match[1]}`}>
            {keys.map((citationKey) => {
              const citation = citations.find(c => c.key === citationKey);
              if (citation) {
                return (
                  <CitationBadge
                    key={`cb-${citationKey}`}
                    citationKey={citationKey}
                    onClick={() => setSelectedCitation(citation)}
                  />
                );
              }
              return <span key={`cs-${citationKey}`}>[{citationKey}]</span>;
            })}
          </span>
        );
      }
      return part;
    });
  }, [citations]);


  // Helper to process children and replace citation patterns
  const processChildren = useCallback((children: React.ReactNode): React.ReactNode => {
    return React.Children.map(children, (child) => {
      if (typeof child === 'string') {
        return renderTextWithCitations(child);
      }
      return child;
    });
  }, [renderTextWithCitations]);

  // Recursively extract text content from React nodes for suggestion matching
  // Preprocess summary to prevent ReactMarkdown from interpreting consecutive
  // citations like [5][6][7] as markdown link references
  const { summaryMain, summaryTrailing } = useMemo(() => {
    if (!incident.summary) return { summaryMain: '', summaryTrailing: '' };
    let text = incident.summary.replace(/]\[/g, '] [');
    text = text.replace(/---\s*\n##\s*Suggested Next Steps[\s\S]*?(?=---\s*\n##|\s*$)/, '');
    text = text.replace(/##\s*Suggested Next Steps[\s\S]*?(?=---\s*\n##|\n##|\s*$)/, '');
    const trailingMatch = /((---\s*\n)?##\s*Ruled Out[\s\S]*)$/m.exec(text);
    if (trailingMatch?.index !== undefined) {
      return { summaryMain: text.slice(0, trailingMatch.index).trim(), summaryTrailing: trailingMatch[1].trim() };
    }
    return { summaryMain: text.trim(), summaryTrailing: '' };
  }, [incident.summary]);

  const mdComponents = useMemo(() => buildMdComponents(processChildren), [processChildren]);

  const renderedSummary = useMemo(() => (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
      {summaryMain}
    </ReactMarkdown>
  ), [summaryMain, mdComponents]);


  return (
    <div className="space-y-8">
      {/* Alert Section */}
      <div>
        {/* Top row: severity, source, status */}
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-4">
            {/* Severity - hide if unknown during investigation */}
            {showSeverity && (
              <Badge className={`${incidentsService.getSeverityColor(alert.severity)} text-sm font-bold uppercase tracking-wider px-3 py-1`}>
                {alert.severity} severity
              </Badge>
            )}
            <div className="flex items-center gap-2">
              {sourceIconSrc && (
                <Image 
                  src={sourceIconSrc}
                  alt={alert.source}
                  width={20}
                  height={20}
                  className={sourceIconBgColor}
                />
              )}
              {isSafeUrl(alert.sourceUrl) ? (
                <a 
                  href={alert.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-zinc-400 hover:text-white transition-colors"
                >
                  {sourceDisplayName(alert.source)} Alert
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1.5 text-zinc-400">
                  {sourceDisplayName(alert.source)} Alert
                </span>
              )}
            </div>
          </div>
          <div className="flex items-center gap-3">
            <StatusPill status={incident.auroraStatus} />
          </div>
        </div>

        {/* Alert title */}
        <h1 className="text-lg font-semibold text-white mb-3">
          {alert.title}
        </h1>

        {/* Metadata row with Raw Alert */}
        <div className="flex items-center text-sm text-zinc-500">
          <div className="flex flex-wrap items-center gap-4">
            {alert.service !== 'unknown' && (
              <div className="flex items-center gap-1.5">
                <Server className="w-4 h-4" />
                <span className="text-zinc-300">{alert.service}</span>
              </div>
            )}
            <div className="flex items-center gap-1.5">
              <Clock className="w-4 h-4" />
              <span>{incidentsService.formatTimeAgo(alert.triggeredAt)}</span>
            </div>
            {/* Provider-specific metadata fields */}
            {alert.metadata?.hostname && (
              <>
                <span className="text-zinc-700">•</span>
                <span className="text-zinc-300">{alert.metadata.hostname}</span>
              </>
            )}
            {alert.metadata?.chart && (
              <>
                <span className="text-zinc-700">•</span>
                <span className="font-mono text-orange-300">{alert.metadata.chart}</span>
              </>
            )}
            {alert.metadata?.metric && (
              <>
                <span className="text-zinc-700">•</span>
                <span className="font-mono text-orange-300">{alert.metadata.metric}</span>
              </>
            )}
            {alert.metadata?.value && (
              <>
                <span className="text-zinc-700">•</span>
                <span className="text-red-400">{alert.metadata.value}</span>
              </>
            )}
            {alert.metadata?.priority && (
              <>
                <span className="text-zinc-700">•</span>
                <span className="text-yellow-400">{alert.metadata.priority}</span>
              </>
            )}
            {isSafeUrl(alert.metadata?.alertUrl) && (
              <a 
                href={alert.metadata?.alertUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300"
              >
                View Alert
              </a>
            )}
            {isSafeUrl(alert.metadata?.dashboardUrl) && (
              <a 
                href={alert.metadata!.dashboardUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300"
              >
                Dashboard
              </a>
            )}
            {isSafeUrl(alert.metadata?.runbookUrl) && (
              <a 
                href={alert.metadata!.runbookUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-green-400 hover:text-green-300"
              >
                Runbook
              </a>
            )}
            <button
              onClick={() => setShowRawPayload(!showRawPayload)}
              className="inline-flex items-center text-zinc-500 hover:text-zinc-300 transition-colors"
              aria-label={showRawPayload ? "Hide raw alert" : "Show raw alert"}
              aria-expanded={showRawPayload}
            >
              {showRawPayload ? (
                <ChevronUp className="w-4 h-4 mr-1" />
              ) : (
                <ChevronDown className="w-4 h-4 mr-1" />
              )}
              Raw Alert
            </button>
            {/* PagerDuty custom fields runbook */}
            {(() => {
              const runbookUrl = alert.metadata?.customFields?.runbook_link;
              if (runbookUrl && isSafeUrl(runbookUrl)) {
                return (
                  <a href={runbookUrl} target="_blank" rel="noopener noreferrer" className="text-green-400 hover:text-green-300" title="Runbook from PagerDuty">
                    Runbook Link
                  </a>
                );
              }
              if (runbookUrl) {
                return <span className="text-zinc-500" title="Invalid runbook URL">Runbook (invalid URL)</span>;
              }
              if (alert.source === 'pagerduty') {
                return <span className="text-zinc-600" title="No runbook configured">Runbook: none</span>;
              }
              return null;
            })()}
          </div>
        </div>

        {/* Raw payload (collapsible) */}
        {showRawPayload && (
          <div className="mt-3 p-4 rounded-lg bg-zinc-900 border border-zinc-800">
            {alert.rawPayload ? (
              <pre className="text-xs font-mono text-zinc-400 overflow-x-auto">
                {alert.rawPayload}
              </pre>
            ) : (
              <p className="text-xs text-zinc-500 italic">No raw payload available</p>
            )}
          </div>
        )}
      </div>

      {/* Separator */}
      <div className="border-t border-zinc-800" />

      {/* Summary Section - hide for merged incidents */}
      {incident.status !== 'merged' ? (
        <div>
          <div className="flex items-center gap-3 mb-4">
            {(incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && (
              <h2 className="text-lg font-medium text-white">Current Summary</h2>
            )}
            
            {/* Thinking/View Thoughts toggle - ChatGPT style */}
            <button
              onClick={(e) => {
                e.stopPropagation();
                onToggleThoughts();
              }}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
                showThoughts 
                  ? 'text-orange-300 bg-orange-500/10' 
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              }`}
              aria-label={showThoughts ? "Hide thoughts panel" : "Show thoughts panel"}
              aria-expanded={showThoughts}
            >
              <span>{incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing' ? 'Thinking' : 'View Thoughts'}</span>
              <ChevronRight className={`w-3 h-3 transition-transform ${showThoughts ? 'rotate-90' : ''}`} />
            </button>
          </div>

          {/* The most valuable content */}
          <div className="prose prose-invert prose-sm max-w-none">
            {renderedSummary}
          </div>

          {/* Next Steps — right after summary findings */}
          {incident.suggestions && incident.suggestions.length > 0 && incident.auroraStatus === 'complete' && (
            <NextStepsConsole
              suggestions={incident.suggestions}
              citations={citations}
              canWrite={canWrite}
              execCaps={execCaps}
              onRunSuggestion={setSelectedSuggestion}
              onFixSuggestion={setSelectedFixSuggestion}
              onCitationClick={setSelectedCitation}
            />
          )}

          {/* Ruled Out / Not Checked — collapsible console style */}
          {summaryTrailing && (
            <RuledOutConsole text={summaryTrailing} citations={citations} onCitationClick={setSelectedCitation} />
          )}

          {/* Correlated Alerts Section */}
          {incident.correlatedAlerts && incident.correlatedAlerts.length > 0 && (
            <CorrelatedAlertsSection alerts={incident.correlatedAlerts} />
          )}

          {/* Other Recent Alerts - for manual correlation */}
          <RecentAlertsSection
            currentIncidentId={incident.id}
            auroraStatus={incident.auroraStatus}
            onAlertMerged={onRefresh}
          />
        </div>
      ) : (
        <div className="text-center py-8 text-zinc-500">
          <p className="text-sm">This incident&apos;s investigation was merged into another incident.</p>
          <p className="text-xs mt-2">View the main incident for the combined analysis.</p>
        </div>
      )}

      {/* Action bar — Waterfall and SRE Metrics live here independently of
          chatSessionId so legacy incidents (no RCA session) still get them. */}
      <div className="mt-6 pt-6 border-t border-zinc-800/50 flex items-center gap-3">
        {incident.chatSessionId && (
          incident.auroraStatus === 'complete' && incident.status !== 'merged' ? (
            <Link
              href={`/chat?sessionId=${incident.chatSessionId}`}
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800"
            >
              <span>Root Cause Analysis</span>
              <ExternalLink className="w-3 h-3" />
            </Link>
          ) : (
            <button
              disabled
              title={
                incident.status === 'merged'
                  ? "This incident was merged into another investigation"
                  : "RCA report will be available only when RCA is complete"
              }
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors text-zinc-600 cursor-not-allowed"
            >
              <span>Root Cause Analysis</span>
            </button>
          )
        )}
          
          {(incident.auroraStatus === 'complete' || incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && (
            <button
              onClick={() => setShowVisualization(!showVisualization)}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
                showVisualization
                  ? 'text-orange-300 bg-orange-500/10'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              }`}
            >
              <span>Visualization</span>
              <ChevronRight className={`w-3 h-3 transition-transform ${showVisualization ? 'rotate-90' : ''}`} />
            </button>
          )}

          {/* Resolve Incident button */}
          {canWrite && incident.auroraStatus === 'complete' && incident.status !== 'resolved' && incident.status !== 'merged' && (
            <button
              onClick={handleResolveIncident}
              disabled={resolvingIncident}
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors text-green-400 hover:text-green-300 hover:bg-green-500/10 disabled:opacity-50"
            >
              <CheckCircle2 className="w-3 h-3" />
              {resolvingIncident ? 'Resolving...' : 'Resolve Incident'}
            </button>
          )}

          {/* Postmortem button */}
          {incident.auroraStatus === 'complete' && incident.status === 'resolved' && (
            <button
              onClick={() => setShowPostmortem(!showPostmortem)}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
                showPostmortem
                  ? 'text-orange-300 bg-orange-500/10'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              }`}
            >
              <FileText className="w-3 h-3" />
              Postmortem
            </button>
          )}

          {/* Token Usage button */}
          {incident.tokenUsage && (
            <button
              onClick={() => setShowTokenUsage(!showTokenUsage)}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
                showTokenUsage
                  ? 'text-orange-300 bg-orange-500/10'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              }`}
            >
              <Coins className="w-3 h-3" />
              Token Usage
              <ChevronRight className={`w-3 h-3 transition-transform ${showTokenUsage ? 'rotate-90' : ''}`} />
            </button>
          )}

          {/* Waterfall button */}
          {(incident.auroraStatus === 'complete' || incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && (
            <button
              onClick={() => setShowWaterfall(!showWaterfall)}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
                showWaterfall
                  ? 'text-orange-300 bg-orange-500/10'
                  : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
              }`}
            >
              <Activity className="w-3 h-3" />
              Waterfall
              <ChevronRight className={`w-3 h-3 transition-transform ${showWaterfall ? 'rotate-90' : ''}`} />
            </button>
          )}

          {/* Actions button */}
          <button
            onClick={() => setShowActions(!showActions)}
            className={`inline-flex items-center gap-1.5 px-2 py-1 rounded-md text-xs transition-colors ${
              showActions
                ? 'text-orange-300 bg-orange-500/10'
                : 'text-zinc-400 hover:text-zinc-200 hover:bg-zinc-800'
            }`}
          >
            <Play className="w-3 h-3" />
            Actions
            <ChevronRight className={`w-3 h-3 transition-transform ${showActions ? 'rotate-90' : ''}`} />
          </button>

      </div>

      {/* Feedback Section - only show when analysis is complete */}
      {incident.auroraStatus === 'complete' && (
        <div className="mt-6 pt-6 border-t border-zinc-800/50">
          <IncidentFeedback incidentId={incident.id} readOnly={!canWrite} />
        </div>
      )}

      {/* Action Runs linked to this incident (collapsible, lazy-loaded) */}
      <div className="collapsible-panel" data-open={showActions}>
        <div>
          <div className="border-t border-zinc-800 mt-4" />
          <div className="mt-4">
            {showActions && <IncidentActionRuns incidentId={incident.id} />}
          </div>
        </div>
      </div>

      {/* Token Usage Panel (collapsible) */}
      {incident.tokenUsage && (
        <div className="collapsible-panel" data-open={showTokenUsage}>
          <div>
            <div className="border-t border-zinc-800 mt-4" />
            <div className="rounded-lg bg-zinc-900/50 border border-zinc-800 p-4 mt-4">
              <h3 className="text-sm font-medium text-zinc-300 mb-3">Investigation Token Usage</h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <p className="text-[11px] text-zinc-500 uppercase tracking-wider">Input Tokens</p>
                  <p className="text-sm font-mono text-zinc-200 mt-0.5">
                    {(incident.tokenUsage.totalInputTokens ?? 0).toLocaleString()}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] text-zinc-500 uppercase tracking-wider">Output Tokens</p>
                  <p className="text-sm font-mono text-zinc-200 mt-0.5">
                    {(incident.tokenUsage.totalOutputTokens ?? 0).toLocaleString()}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] text-zinc-500 uppercase tracking-wider">Total Tokens</p>
                  <p className="text-sm font-mono text-zinc-200 mt-0.5">
                    {(incident.tokenUsage.totalTokens ?? 0).toLocaleString()}
                  </p>
                </div>
                <div>
                  <p className="text-[11px] text-zinc-500 uppercase tracking-wider">Estimated Cost</p>
                  <p className="text-sm font-mono text-green-400 mt-0.5">
                    ${(incident.tokenUsage.totalCost ?? 0).toFixed(4)}
                  </p>
                </div>
              </div>

              {/* Per-model breakdown */}
              {incident.tokenUsage.models && incident.tokenUsage.models.length > 0 && (
                <div className="mt-3 pt-3 border-t border-zinc-800/50">
                  <p className="text-[11px] text-zinc-500 uppercase tracking-wider mb-2">By Model</p>
                  <div className="space-y-1.5">
                    {incident.tokenUsage.models.map((m) => {
                      const shortName = m.model.includes('/') ? m.model.split('/').pop() : m.model;
                      return (
                        <div key={m.model} className="flex items-center justify-between text-xs">
                          <div className="flex items-center gap-2 min-w-0">
                            <span className="text-zinc-300 truncate" title={m.model}>{shortName}</span>
                            <span className="text-zinc-600">x{m.requestCount ?? 0}</span>
                          </div>
                          <div className="flex items-center gap-3 shrink-0 ml-2">
                            <span className="font-mono tabular-nums text-zinc-500">
                              {(m.inputTokens ?? 0).toLocaleString()} in / {(m.outputTokens ?? 0).toLocaleString()} out
                            </span>
                            <span className="font-mono tabular-nums text-green-400/80 w-16 text-right">
                              ${(m.cost ?? 0).toFixed(4)}
                            </span>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}

              <p className="text-[11px] text-zinc-600 mt-3">
                {incident.tokenUsage.requestCount ?? 0} LLM request{(incident.tokenUsage.requestCount ?? 0) !== 1 ? 's' : ''} during investigation
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Waterfall Panel (collapsible) — only mount when expanded so the
          incident page doesn't pay the fetch cost upfront. */}
      <div className="collapsible-panel" data-open={showWaterfall}>
        <div>
          <div className="border-t border-zinc-800 mt-4" />
          <div className="mt-4">
            {showWaterfall && <ExecutionWaterfall incidentId={incident.id} />}
          </div>
        </div>
      </div>

      {/* Postmortem Panel */}
      <PostmortemPanel
        incidentId={incident.id}
        incidentTitle={incident.alert.title}
        isVisible={showPostmortem}
        onClose={() => setShowPostmortem(false)}
        justResolved={justResolved}
      />

      {/* Infrastructure Visualization */}
      {showVisualization && (incident.auroraStatus === 'complete' || incident.auroraStatus === 'running' || incident.auroraStatus === 'summarizing') && (
        <>
          <div className="border-t border-zinc-800" />
          <div>
            <h2 className="text-lg font-medium text-white mb-4">Infrastructure Analysis</h2>
            <ReactFlowProvider>
              <InfrastructureVisualization incidentId={incident.id} className="h-[500px]" />
            </ReactFlowProvider>
          </div>
        </>
      )}

      {/* Citation Modal */}
      <CitationModal
        citation={selectedCitation}
        isOpen={selectedCitation !== null}
        onClose={() => setSelectedCitation(null)}
      />

      {/* Suggestion Modal */}
      <SuggestionModal
        suggestion={selectedSuggestion}
        incidentId={incident.id}
        chatSessionId={incident.chatSessionId}
        isOpen={selectedSuggestion !== null}
        onClose={() => setSelectedSuggestion(null)}
      />

      {/* Fix Suggestion Modal */}
      <FixSuggestionModal
        suggestion={selectedFixSuggestion}
        isOpen={selectedFixSuggestion !== null}
        onClose={() => setSelectedFixSuggestion(null)}
        onPRCreated={(prUrl) => {
          // Update local suggestion state so reopening the modal shows the PR URL
          if (selectedFixSuggestion) {
            setSelectedFixSuggestion({ ...selectedFixSuggestion, prUrl });
          }
          onRefresh?.();
        }}
      />
    </div>
  );
}
