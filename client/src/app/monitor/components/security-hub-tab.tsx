import { useState, useCallback } from 'react';
import { useQuery, jsonFetcher } from '@/lib/query';
import { ShieldAlert, AlertTriangle, AlertCircle, Info, CheckCircle2, ChevronRight, ChevronDown, Clock, Activity, Zap } from 'lucide-react';
import { formatDistanceToNow } from 'date-fns';
import { StatCard, StatCardSkeleton, ChartSkeleton, EmptyState, ChartPanel } from '@/app/monitor/components/charts';

interface SecurityFinding {
  finding_id: string;
  source: string;
  title: string;
  severity_label: string;
  ai_summary: string;
  ai_risk_level: string;
  ai_suggested_fix: string;
  created_at: string;
  updated_at: string;
}

/**
 * Renders a color-coded severity badge for a given risk level.
 */
function severityBadge(severity: string | null) {
  if (!severity) return null;
  const s = severity.toLowerCase();
  const colors: Record<string, string> = {
    critical: 'bg-red-500/15 text-red-400 ring-red-500/20',
    high: 'bg-orange-500/15 text-orange-400 ring-orange-500/20',
    medium: 'bg-yellow-500/15 text-yellow-400 ring-yellow-500/20',
    low: 'bg-blue-500/15 text-blue-400 ring-blue-500/20',
  };
  return (
    <span className={`px-1.5 py-0.5 rounded text-[11px] font-medium ring-1 uppercase tracking-wider ${colors[s] || 'bg-zinc-500/15 text-zinc-400 ring-zinc-500/20'}`}>
      {severity}
    </span>
  );
}

/**
 * Main dashboard tab component for displaying AWS Security Hub findings.
 * Includes summary statistics and a list of expandable threat cards.
 */
export default function SecurityHubTab({ }: { period?: string }) {
  const { data: findings = [], isLoading } = useQuery<SecurityFinding[]>(
    '/api/aws/securityhub/findings?limit=50',
    jsonFetcher,
    { staleTime: 30_000, revalidateOnFocus: true }
  );

  const [expandedId, setExpandedId] = useState<string | null>(null);

  const toggleExpand = useCallback((id: string) => {
    setExpandedId(prev => prev === id ? null : id);
  }, []);

  const criticalCount = findings.filter(f => f.ai_risk_level?.toUpperCase() === 'CRITICAL').length;
  const highCount = findings.filter(f => f.ai_risk_level?.toUpperCase() === 'HIGH').length;

  return (
    <div className="space-y-6">
      
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
        {isLoading ? (
          Array.from({ length: 3 }).map((_, i) => <StatCardSkeleton key={i} />)
        ) : findings && (
          <>
            <StatCard label="Total Findings" value={findings.length.toString()} icon={Activity} />
            <StatCard label="Critical Risk" value={criticalCount.toString()} icon={AlertCircle} />
            <StatCard label="High Risk" value={highCount.toString()} icon={AlertTriangle} />
          </>
        )}
      </div>

      <ChartPanel title="Recent Findings" subtitle="AI Triage evaluated events from AWS Security Hub" loading={isLoading}>
        {!findings || findings.length === 0 ? (
          <EmptyState
            icon={CheckCircle2}
            message="No Active Threats Found"
            hint="Your AWS environment is currently secure. Aurora Agentic AI is continuously monitoring EventBridge webhooks."
          />
        ) : (
          <div className="space-y-2 mt-2">
            {findings.map((finding) => (
              <FindingCard
                key={finding.finding_id}
                finding={finding}
                expanded={expandedId === finding.finding_id}
                onToggle={() => toggleExpand(finding.finding_id)}
              />
            ))}
          </div>
        )}
      </ChartPanel>
    </div>
  );
}

/**
 * Individual expandable card component representing a single security finding.
 * Shows finding details and AI-generated agentic summaries when expanded.
 */
function FindingCard({ finding, expanded, onToggle }: { finding: SecurityFinding; expanded: boolean; onToggle: () => void }) {
  return (
    <div className={`rounded-lg border transition-all duration-200 ${
      expanded ? 'border-zinc-700/60 bg-zinc-800/20' : 'border-zinc-800/50 hover:border-zinc-700/40 bg-zinc-900/30'
    }`}>
      <button
        onClick={onToggle}
        className="w-full text-left px-4 py-3 flex items-start gap-3 outline-none"
      >
        <div className="mt-0.5 text-zinc-600 shrink-0">
          {expanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
        </div>
        
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm font-medium text-zinc-200 truncate max-w-[400px]">
              {finding.title || 'Untitled finding'}
            </span>
            {severityBadge(finding.ai_risk_level || finding.severity_label)}
            <span className="text-[10px] text-zinc-500 font-medium px-1.5 py-0.5 bg-zinc-800/60 rounded uppercase">
              {finding.source || 'Security Hub'}
            </span>
          </div>
          
          <div className="flex items-center gap-3 mt-1 text-xs text-zinc-500">
            <span className="flex items-center gap-1">
               <Clock className="h-3 w-3" />
               {finding.updated_at ? formatDistanceToNow(new Date(finding.updated_at), { addSuffix: true }) : 'Recently'}
            </span>
            <span className="text-zinc-700">·</span>
            <span className="flex items-center gap-1">
               <ShieldAlert className="h-3 w-3" />
               Security Event
            </span>
          </div>
          
          {!expanded && finding.ai_summary && (
            <p className="text-xs text-zinc-500 mt-1.5 line-clamp-1 leading-relaxed">
              {finding.ai_summary}
            </p>
          )}
        </div>
      </button>
      
      {expanded && (
        <div className="px-4 pb-4 pt-0 ml-7 space-y-3">
          <div className="rounded-lg bg-zinc-900/60 border border-zinc-800/50 p-3">
            <div className="flex items-center gap-1.5 mb-2">
              <Activity className="h-3.5 w-3.5 text-blue-400/70" />
              <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">Agentic Summary</span>
            </div>
            <p className="text-sm text-zinc-300 leading-relaxed whitespace-pre-line">
              {finding.ai_summary || "No Agent summary available for this finding."}
            </p>
          </div>
          
          <div className="rounded-lg bg-zinc-900/60 border border-zinc-800/50 p-3">
            <div className="flex items-center gap-1.5 mb-2">
              <Zap className="h-3.5 w-3.5 text-amber-400/70" />
              <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">Agentic Remediation</span>
            </div>
            <div className="text-sm text-zinc-300 leading-relaxed whitespace-pre-line">
              {finding.ai_suggested_fix ? (
                <p>{finding.ai_suggested_fix}</p>
              ) : (
                <p className="italic text-zinc-500">Manual review required. No automated remediation plan exists.</p>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
