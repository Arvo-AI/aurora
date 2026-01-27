'use client';

import { useVisualizationStream } from '@/hooks/useVisualizationStream';
import { InfraNode, NodeStatus, NodeType } from '@/types/visualization';
import { ReactFlow, Node, Edge, Controls, Background, Panel, Handle, Position } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './visualization.css';
import { useMemo } from 'react';
import { Loader2 } from 'lucide-react';

interface Props {
  incidentId: string;
  className?: string;
}

const statusColors: Record<NodeStatus, { border: string; bg: string; glow: string }> = {
  healthy: { border: '#22c55e', bg: '#052e16', glow: 'rgba(34, 197, 94, 0.3)' },
  degraded: { border: '#eab308', bg: '#422006', glow: 'rgba(234, 179, 8, 0.3)' },
  failed: { border: '#ef4444', bg: '#450a0a', glow: 'rgba(239, 68, 68, 0.3)' },
  investigating: { border: '#f97316', bg: '#431407', glow: 'rgba(249, 115, 22, 0.3)' },
  unknown: { border: '#71717a', bg: '#18181b', glow: 'rgba(113, 113, 122, 0.3)' },
};

function CustomNode({ data }: { data: InfraNode & { isRootCause?: boolean; isAffected?: boolean } }) {
  const colors = statusColors[data.status];
  const isRootCause = data.isRootCause;
  const isAffected = data.isAffected;

  return (
    <>
      <Handle type="target" position={Position.Top} style={{ background: '#52525b' }} />
      <div
        style={{
          padding: '12px 16px',
          border: `2px ${isAffected ? 'dashed' : 'solid'} ${colors.border}`,
          borderRadius: '8px',
          backgroundColor: colors.bg,
          minWidth: '120px',
          boxShadow: isRootCause ? `0 0 20px ${colors.glow}` : `0 0 8px ${colors.glow}`,
          fontWeight: isRootCause ? 600 : 400,
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#fafafa' }}>
          <div style={{ 
            fontSize: '9px', 
            fontWeight: 700, 
            color: '#71717a',
            backgroundColor: '#27272a',
            padding: '2px 6px',
            borderRadius: '4px',
            letterSpacing: '0.5px',
            textTransform: 'uppercase'
          }}>
            {data.type}
          </div>
          <div>
            <div style={{ fontSize: '13px', fontWeight: 500 }}>{data.label}</div>
            <div style={{ fontSize: '10px', color: '#a1a1aa', marginTop: '2px', textTransform: 'lowercase' }}>{data.type}</div>
          </div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: '#52525b' }} />
    </>
  );
}

export default function InfrastructureVisualization({ incidentId, className }: Props) {
  const { data, isLoading, error } = useVisualizationStream(incidentId);

  const { nodes, edges } = useMemo(() => {
    if (!data?.nodes?.length) return { nodes: [], edges: [] };

    const flowNodes: Node[] = data.nodes.map((node, idx) => ({
      id: node.id,
      type: 'custom',
      position: { x: (idx % 4) * 200, y: Math.floor(idx / 4) * 100 },
      data: {
        ...node,
        isRootCause: node.id === data.rootCauseId,
        isAffected: data.affectedIds.includes(node.id),
      },
    }));

    const flowEdges: Edge[] = data.edges.map((edge, idx) => ({
      id: `e${idx}`,
      source: edge.source,
      target: edge.target,
      label: edge.label,
      type: 'smoothstep',
      animated: edge.type === 'causation',
      style: { stroke: '#52525b', strokeWidth: 2 },
      labelStyle: { fill: '#a1a1aa', fontSize: 10 },
      markerEnd: { type: 'arrowclosed', color: '#52525b' },
    }));

    return { nodes: flowNodes, edges: flowEdges };
  }, [data]);

  if (isLoading) {
    return (
      <div className={`${className} flex items-center justify-center bg-zinc-900/50 rounded-lg border border-zinc-800`}>
        <Loader2 className="w-6 h-6 text-zinc-400 animate-spin" />
      </div>
    );
  }

  if (error) {
    return (
      <div className={`${className} flex items-center justify-center bg-zinc-900/50 rounded-lg border border-zinc-800`}>
        <p className="text-sm text-zinc-500">Failed to load visualization</p>
      </div>
    );
  }

  if (!nodes.length) {
    return (
      <div className={`${className} flex items-center justify-center bg-zinc-900/50 rounded-lg border border-zinc-800`}>
        <p className="text-sm text-zinc-500">No infrastructure data available</p>
      </div>
    );
  }

  return (
    <div className={`${className} bg-zinc-950 rounded-lg border border-zinc-800 overflow-hidden`}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={{ custom: CustomNode }}
        nodesDraggable={true}
        nodesConnectable={false}
        elementsSelectable={true}
        panOnDrag={true}
        zoomOnScroll={true}
        fitView
        minZoom={0.5}
        maxZoom={1.5}
        defaultEdgeOptions={{ 
          type: 'smoothstep',
          animated: false,
          style: { stroke: '#52525b', strokeWidth: 2 },
          markerEnd: { type: 'arrowclosed', color: '#52525b' }
        }}
      >
        <Background color="#27272a" gap={16} />
        <Controls showInteractive={false} />
        {data && (
          <Panel position="top-right" className="bg-zinc-900/90 px-3 py-2 rounded-md border border-zinc-700">
            <div className="text-xs text-zinc-400">
              v{data.version} · {data.nodes.length} nodes · {data.edges.length} edges
            </div>
          </Panel>
        )}
      </ReactFlow>
    </div>
  );
}
