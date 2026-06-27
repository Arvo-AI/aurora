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
  FileText,
  Coins,
  Activity,
} from 'lucide-react';
import React, { useState, useMemo, useCallback } from 'react';
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
import NextStepsConsole from './NextStepsConsole';
import RuledOutConsole from './RuledOutConsole';
import { ReactFlowProvider } from '@xyflow/react';
import { connectorRegistry } from '@/components/connectors/ConnectorRegistry';
import { useExecutionCapabilities } from '@/hooks/use-execution-capabilities';

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

    return parts.map((part, idx) => {
      const match = CITATION_RE.exec(part);
      if (match) {
        const keys = match[1].split(/,\s*/).map(k => k.trim());
        return (
          <span key={`cg-${idx}-${match[1]}`}>
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
      return <span key={`t-${idx}`}>{part}</span>;
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
