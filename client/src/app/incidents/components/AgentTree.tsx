'use client';

import { useMemo, useState } from 'react';
import { Loader2 } from 'lucide-react';
import { useQuery } from '@/lib/query';
import {
  fetchSubAgentDetail,
  type SubAgentDetail,
  type SubAgentRun,
} from '@/lib/services/incidents';
import SubAgentCard from './SubAgentCard';

interface AgentTreeProps {
  incidentId: string;
  runs: SubAgentRun[];
}

interface TreeNode {
  run: SubAgentRun;
  children: TreeNode[];
}

function buildTree(runs: SubAgentRun[]): TreeNode[] {
  const byParent = new Map<string | null, SubAgentRun[]>();
  for (const r of runs) {
    const key = r.role === 'main' ? null : r.parent_agent_id;
    const list = byParent.get(key) ?? [];
    list.push(r);
    byParent.set(key, list);
  }

  const buildChildren = (parentId: string): TreeNode[] => {
    return (byParent.get(parentId) ?? []).map((run) => ({
      run,
      children: buildChildren(run.agent_id),
    }));
  };

  const roots = (byParent.get(null) ?? []).map((run) => ({
    run,
    children: buildChildren(run.agent_id),
  }));

  return roots;
}

function ExpandedDetail({ incidentId, agentId }: { incidentId: string; agentId: string }) {
  const { data, error, isLoading } = useQuery<SubAgentDetail>(
    `subagent-detail:${incidentId}:${agentId}`,
    (_key, _signal) => fetchSubAgentDetail(incidentId, agentId),
    { staleTime: 30_000 },
  );

  if (isLoading) {
    return (
      <div className="flex items-center gap-2 px-3 py-3 text-xs text-zinc-500">
        <Loader2 className="w-3 h-3 animate-spin" />
        Loading findings...
      </div>
    );
  }

  if (error) {
    return (
      <div className="px-3 py-3 text-xs text-red-400">
        Failed to load findings: {error.message}
      </div>
    );
  }

  if (!data) return null;

  const sections = data.findings_sections;
  if (!sections || Object.keys(sections).length === 0) {
    return (
      <div className="px-3 py-3 text-xs text-zinc-500">
        {data.findings_markdown ? (
          <pre className="whitespace-pre-wrap font-mono text-zinc-400">{data.findings_markdown}</pre>
        ) : (
          <span>No findings recorded.</span>
        )}
      </div>
    );
  }

  return (
    <div className="px-3 py-3 space-y-3">
      {Object.entries(sections).map(([heading, body]) => (
        <div key={heading}>
          <p className="text-[11px] font-semibold text-zinc-400 uppercase tracking-wider mb-1">
            {heading}
          </p>
          <p className="text-xs text-zinc-300 whitespace-pre-wrap">{body}</p>
        </div>
      ))}
    </div>
  );
}

function TreeBranch({
  node,
  depth,
  incidentId,
  expandedIds,
  onToggle,
}: {
  node: TreeNode;
  depth: number;
  incidentId: string;
  expandedIds: Set<string>;
  onToggle: (id: string) => void;
}) {
  const isExpanded = expandedIds.has(node.run.agent_id);

  return (
    <div className="space-y-2">
      <div style={{ marginLeft: depth * 20 }}>
        <SubAgentCard
          run={node.run}
          expanded={isExpanded}
          onClick={() => onToggle(node.run.agent_id)}
        />
        {isExpanded && (
          <div className="mt-1 ml-2 border-l border-zinc-800">
            <ExpandedDetail incidentId={incidentId} agentId={node.run.agent_id} />
          </div>
        )}
      </div>
      {node.children.map((child) => (
        <TreeBranch
          key={child.run.agent_id}
          node={child}
          depth={depth + 1}
          incidentId={incidentId}
          expandedIds={expandedIds}
          onToggle={onToggle}
        />
      ))}
    </div>
  );
}

export default function AgentTree({ incidentId, runs }: AgentTreeProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const tree = useMemo(() => buildTree(runs), [runs]);

  const toggle = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  if (!runs.length) {
    return (
      <div className="rounded-lg border border-zinc-800 bg-zinc-900/40 px-3 py-4 text-xs text-zinc-500">
        No multi-agent run for this incident.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      {tree.map((root) => (
        <TreeBranch
          key={root.run.agent_id}
          node={root}
          depth={0}
          incidentId={incidentId}
          expandedIds={expandedIds}
          onToggle={toggle}
        />
      ))}
    </div>
  );
}
