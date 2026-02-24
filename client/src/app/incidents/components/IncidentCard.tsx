'use client';

import { Incident, AuroraStatus, Citation, incidentsService, postmortemService } from '@/lib/services/incidents';
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
} from 'lucide-react';
import React, { useState, useMemo, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';
import { useRouter } from 'next/navigation';
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
import { Suggestion } from '@/lib/services/incidents';
import InfrastructureVisualization from '@/components/incidents/InfrastructureVisualization';
import { ReactFlowProvider } from '@xyflow/react';

interface IncidentCardProps {
  incident: Incident;
  duration: string;
  showThoughts: boolean;
  onToggleThoughts: () => void;
  citations?: Citation[];
  onExecutionStarted?: () => void;
  onRefresh?: () => void;
}

function StatusPill({ status }: { status: AuroraStatus }) {
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

export default function IncidentCard({ incident, duration, showThoughts, onToggleThoughts, citations = [], onExecutionStarted, onRefresh }: IncidentCardProps) {
  const [showRawPayload, setShowRawPayload] = useState(false);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [selectedSuggestion, setSelectedSuggestion] = useState<Suggestion | null>(null);
  const [selectedFixSuggestion, setSelectedFixSuggestion] = useState<Suggestion | null>(null);
  const [showVisualization, setShowVisualization] = useState(false);
  const [showPostmortem, setShowPostmortem] = useState(false);
  const [resolvingIncident, setResolvingIncident] = useState(false);
  const alert = incident.alert;
  const router = useRouter();
  const showSeverity = (alert.severity && alert.severity !== 'unknown') || incident.status === 'analyzed';

  const handleResolveIncident = async () => {
    setResolvingIncident(true);
    try {
      await incidentsService.resolveIncident(incident.id);
      onRefresh?.();
    } catch (e) {
      console.error('Failed to resolve incident:', e);
    } finally {
      setResolvingIncident(false);
    }
  };

  // Extract significant words (length > 3) from text for matching
  const extractSignificantWords = useCallback((text: string): string[] => {
    const normalized = text.toLowerCase().replace(/[^\w\s]/g, '');
    return normalized.split(/\s+/).filter(word => word.length > 3);
  }, []);

  // Matches summary list items to suggestions by comparing significant words
  // Returns the suggestion with the BEST match (most matching words), not just the first match
  const findMatchingSuggestion = useCallback((text: string): Suggestion | null => {
    if (!incident.suggestions?.length) return null;

    const textWords = extractSignificantWords(text);
    const normalizedText = text.toLowerCase().replace(/[^\w\s]/g, '');

    let bestMatch: Suggestion | null = null;
    let bestMatchCount = 0;

    for (const suggestion of incident.suggestions) {
      // Match by title word overlap (at least 2 significant words in common)
      const titleWords = extractSignificantWords(suggestion.title);
      const matchingWordCount = titleWords.filter(word => textWords.includes(word)).length;

      // Keep track of the best match (most words in common)
      if (matchingWordCount >= 2 && matchingWordCount > bestMatchCount) {
        bestMatch = suggestion;
        bestMatchCount = matchingWordCount;
      }

      // Match by description prefix overlap (only if no better title match)
      if (!bestMatch) {
        const normalizedDesc = suggestion.description.toLowerCase().replace(/[^\w\s]/g, '');
        const textPrefix = normalizedText.slice(0, 50);
        const descPrefix = normalizedDesc.slice(0, 50);
        if (normalizedText.includes(descPrefix) || normalizedDesc.includes(textPrefix)) {
          bestMatch = suggestion;
          bestMatchCount = 2; // Treat description match as 2 words
        }
      }
    }
    return bestMatch;
  }, [incident.suggestions, extractSignificantWords]);

  // Function to render text with citation badges
  const renderTextWithCitations = useCallback((text: string): React.ReactNode => {
    if (!citations.length) return text;

    // Split text by citation patterns: [1], [2], [1, 2], [6, 7], etc.
    const parts = text.split(/(\[\d+(?:,\s*\d+)*\])/g);

    return parts.map((part, index) => {
      // Match single [1] or multiple [1, 2] or [6, 7]
      const match = part.match(/^\[(\d+(?:,\s*\d+)*)\]$/);
      if (match) {
        const citationKeys = match[1].split(/,\s*/).map(k => k.trim());

        // Render each citation key as a separate badge
        return (
          <span key={`citation-group-${index}`}>
            {citationKeys.map((citationKey, keyIndex) => {
              const citation = citations.find(c => c.key === citationKey);
              if (citation) {
                return (
                  <CitationBadge
                    key={`citation-${index}-${citationKey}`}
                    citationKey={citationKey}
                    onClick={() => setSelectedCitation(citation)}
                  />
                );
              }
              // If citation not found, render as plain text
              return <span key={`citation-${index}-${citationKey}`}>[{citationKey}]</span>;
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
  const extractTextFromNode = useCallback((node: React.ReactNode): string => {
    if (typeof node === 'string') return node;
    if (Array.isArray(node)) return node.map(extractTextFromNode).join('');
    if (React.isValidElement(node) && node.props.children) {
      return extractTextFromNode(node.props.children);
    }
    return '';
  }, []);

  // Preprocess summary to prevent ReactMarkdown from interpreting consecutive
  // citations like [5][6][7] as markdown link references
  const preprocessedSummary = useMemo(() => {
    if (!incident.summary) return '';
    // Add space between consecutive brackets: [5][6] → [5] [6]
    return incident.summary.replace(/\](\[)/g, '] $1');
  }, [incident.summary]);

  // Memoize markdown rendering to prevent re-parsing on every render
  const renderedSummary = useMemo(() => (
    <ReactMarkdown
      components={{
        h1: ({ children }) => (
          <h1 className="text-base font-semibold text-white mb-1">{processChildren(children)}</h1>
        ),
        h2: ({ children }) => (
          <h2 className="text-sm font-semibold text-white mt-3 mb-1">{processChildren(children)}</h2>
        ),
        strong: ({ children }) => (
          <strong className="text-orange-300 font-semibold">{processChildren(children)}</strong>
        ),
        p: ({ children }) => (
          <p className="mb-2 text-zinc-300 text-sm leading-normal">{processChildren(children)}</p>
        ),
        ul: ({ children }) => (
          <ul className="list-disc list-outside ml-4 mb-2 space-y-1">{children}</ul>
        ),
        li: ({ children }) => {
          const textContent = extractTextFromNode(children);
          const matchingSuggestion = findMatchingSuggestion(textContent);
          const isFixType = matchingSuggestion?.type === 'fix';
          const canExecute = Boolean(matchingSuggestion?.command);
          const canShowAction = canExecute || isFixType;

          return (
            <li className="text-zinc-300 text-sm">
              {processChildren(children)}
              {canShowAction && matchingSuggestion && (
                <button
                  onClick={(e) => {
                    e.preventDefault();
                    e.stopPropagation();
                    if (isFixType) {
                      setSelectedFixSuggestion(matchingSuggestion);
                    } else {
                      setSelectedSuggestion(matchingSuggestion);
                    }
                  }}
                  className={`inline-flex items-center justify-center w-5 h-5 rounded transition-colors align-middle ml-1.5 ${
                    isFixType
                      ? 'bg-green-500/20 hover:bg-green-500/40 text-green-400'
                      : 'bg-orange-500/20 hover:bg-orange-500/40 text-orange-400'
                  }`}
                  title={isFixType
                    ? `Create PR: ${matchingSuggestion.filePath || 'Fix suggestion'}`
                    : `Run: ${matchingSuggestion.command?.split('\n')[0] || ''}`
                  }
                >
                  {isFixType ? <GitBranch className="w-3 h-3" /> : <Play className="w-3 h-3" />}
                </button>
              )}
            </li>
          );
        },
        code: ({ children }) => (
          <code className="bg-zinc-800 px-1.5 py-0.5 rounded text-orange-300 text-xs font-mono">
            {children}
          </code>
        ),
      }}
    >
      {preprocessedSummary}
    </ReactMarkdown>
  ), [preprocessedSummary, processChildren, findMatchingSuggestion, extractTextFromNode]);

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
              <Image 
                src={alert.source === 'pagerduty' ? '/pagerduty-icon.svg' : `/${alert.source}.svg`}
                alt={alert.source}
                width={20}
                height={20}
                className="object-contain"
              />
              {isSafeUrl(alert.sourceUrl) ? (
                <a 
                  href={alert.sourceUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1.5 text-zinc-400 hover:text-white transition-colors"
                >
                  {alert.source.charAt(0).toUpperCase() + alert.source.slice(1)} Alert
                  <ExternalLink className="w-3.5 h-3.5" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1.5 text-zinc-400">
                  {alert.source.charAt(0).toUpperCase() + alert.source.slice(1)} Alert
                </span>
              )}
            </div>
          </div>
          <StatusPill status={incident.auroraStatus} />
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
                href={alert.metadata.alertUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300"
              >
                View Alert
              </a>
            )}
            {isSafeUrl(alert.metadata?.dashboardUrl) && (
              <a 
                href={alert.metadata.dashboardUrl}
                target="_blank"
                rel="noopener noreferrer"
                className="text-blue-400 hover:text-blue-300"
              >
                Dashboard
              </a>
            )}
            {isSafeUrl(alert.metadata?.runbookUrl) && (
              <a 
                href={alert.metadata.runbookUrl}
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
            {alert.metadata?.customFields?.runbook_link ? (
              isSafeUrl(alert.metadata.customFields.runbook_link) ? (
                <a 
                  href={alert.metadata.customFields.runbook_link}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="text-green-400 hover:text-green-300"
                  title="Runbook from PagerDuty"
                >
                  Runbook Link
                </a>
              ) : (
                <span className="text-zinc-500" title="Invalid runbook URL">
                  Runbook (invalid URL)
                </span>
              )
            ) : (
              alert.source === 'pagerduty' && (
                <span className="text-zinc-600" title="No runbook configured">
                  Runbook: none
                </span>
              )
            )}
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
            {incident.auroraStatus === 'running' && (
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
              <span>{incident.auroraStatus === 'running' ? 'Thinking' : 'View Thoughts'}</span>
              <ChevronRight className={`w-3 h-3 transition-transform ${showThoughts ? 'rotate-90' : ''}`} />
            </button>
          </div>

          {/* The most valuable content */}
          <div className="prose prose-invert prose-sm max-w-none">
            {renderedSummary}
          </div>

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

      {/* View RCA and Visualization Buttons */}
      {incident.chatSessionId && (
        <div className="mt-6 pt-6 border-t border-zinc-800/50 flex items-center gap-3">
          {incident.auroraStatus === 'complete' && incident.status !== 'merged' ? (
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
          )}
          
          {(incident.auroraStatus === 'complete' || incident.auroraStatus === 'running') && (
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
          {incident.auroraStatus === 'complete' && incident.status !== 'resolved' && incident.status !== 'merged' && (
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
          {incident.auroraStatus === 'complete' && incident.status !== 'merged' && (
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
        </div>
      )}

      {/* Feedback Section - only show when analysis is complete */}
      {incident.auroraStatus === 'complete' && (
        <div className="mt-6 pt-6 border-t border-zinc-800/50">
          <IncidentFeedback incidentId={incident.id} />
        </div>
      )}

      {/* Postmortem Panel */}
      <PostmortemPanel
        incidentId={incident.id}
        incidentTitle={incident.alert.title}
        isVisible={showPostmortem}
        onClose={() => setShowPostmortem(false)}
      />

      {/* Infrastructure Visualization */}
      {showVisualization && (incident.auroraStatus === 'complete' || incident.auroraStatus === 'running') && (
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
        onExecutionStarted={onExecutionStarted}
      />

      {/* Fix Suggestion Modal */}
      <FixSuggestionModal
        suggestion={selectedFixSuggestion}
        isOpen={selectedFixSuggestion !== null}
        onClose={() => setSelectedFixSuggestion(null)}
      />
    </div>
  );
}
