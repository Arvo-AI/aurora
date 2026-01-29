'use client';

import { useVisualizationStream } from '@/hooks/useVisualizationStream';
import { InfraNode, NodeStatus, NodeType } from '@/types/visualization';
import { 
  ReactFlow, 
  Node, 
  Edge, 
  Controls, 
  ControlButton,
  Background, 
  Panel, 
  Handle, 
  Position,
  applyNodeChanges,
  applyEdgeChanges,
  NodeChange,
  EdgeChange,
  useReactFlow
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './visualization.css';
import { useMemo, useCallback, useState, useEffect, useRef } from 'react';
import { Loader2, Maximize, RotateCcw } from 'lucide-react';

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

function getLayoutedElements(nodes: Node[], edges: Edge[]) {
  // Simple hierarchical layout algorithm
  // Build adjacency map to find dependencies
  const nodeMap = new Map(nodes.map(n => [n.id, n]));
  const incomingEdges = new Map<string, string[]>();
  const outgoingEdges = new Map<string, string[]>();
  
  nodes.forEach(node => {
    incomingEdges.set(node.id, []);
    outgoingEdges.set(node.id, []);
  });
  
  edges.forEach(edge => {
    outgoingEdges.get(edge.source)?.push(edge.target);
    incomingEdges.get(edge.target)?.push(edge.source);
  });
  
  // Find root nodes (nodes with no incoming edges)
  const rootNodes = nodes.filter(node => 
    (incomingEdges.get(node.id)?.length || 0) === 0
  );
  
  // Assign layers using BFS
  const layers = new Map<string, number>();
  const queue = rootNodes.map(n => ({ id: n.id, layer: 0 }));
  const visited = new Set<string>();
  
  while (queue.length > 0) {
    const { id, layer } = queue.shift()!;
    if (visited.has(id)) continue;
    
    visited.add(id);
    layers.set(id, layer);
    
    const children = outgoingEdges.get(id) || [];
    children.forEach(childId => {
      if (!visited.has(childId)) {
        queue.push({ id: childId, layer: layer + 1 });
      }
    });
  }
  
  // Assign layer 0 to any nodes not yet assigned
  nodes.forEach(node => {
    if (!layers.has(node.id)) {
      layers.set(node.id, 0);
    }
  });
  
  // Group nodes by layer
  const nodesByLayer = new Map<number, string[]>();
  layers.forEach((layer, nodeId) => {
    if (!nodesByLayer.has(layer)) {
      nodesByLayer.set(layer, []);
    }
    nodesByLayer.get(layer)!.push(nodeId);
  });
  
  // Calculate positions
  const nodeWidth = 150;
  const nodeHeight = 80;
  const horizontalSpacing = 120;
  const verticalSpacing = 100;
  
  const layoutedNodes = nodes.map(node => {
    const layer = layers.get(node.id) || 0;
    const nodesInLayer = nodesByLayer.get(layer) || [];
    const indexInLayer = nodesInLayer.indexOf(node.id);
    
    const layerWidth = nodesInLayer.length * (nodeWidth + horizontalSpacing);
    const startX = -layerWidth / 2;
    
    return {
      ...node,
      targetPosition: 'top' as const,
      sourcePosition: 'bottom' as const,
      position: {
        x: startX + indexInLayer * (nodeWidth + horizontalSpacing),
        y: layer * (nodeHeight + verticalSpacing),
      },
    };
  });

  return { nodes: layoutedNodes, edges };
}

export default function InfrastructureVisualization({ incidentId, className }: Props) {
  const { data, isLoading, error } = useVisualizationStream(incidentId);
  const { fitView } = useReactFlow();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const nodeTypes = useMemo(() => ({ custom: CustomNode }), []);

  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);

  const handleFullscreen = useCallback(() => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setIsFullscreen(true);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  }, []);

  const handleCenter = useCallback(() => {
    fitView({ padding: 0.2, duration: 300 });
  }, [fitView]);

  useEffect(() => {
    const handleFullscreenChange = () => {
      setIsFullscreen(!!document.fullscreenElement);
    };

    document.addEventListener('fullscreenchange', handleFullscreenChange);
    return () => document.removeEventListener('fullscreenchange', handleFullscreenChange);
  }, []);

  useEffect(() => {
    if (!data?.nodes?.length) return;

    const flowNodes: Node[] = data.nodes.map((node) => ({
      id: node.id,
      type: 'custom',
      position: { x: 0, y: 0 },
      draggable: true,
      selectable: true,
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

    const { nodes: layoutedNodes, edges: layoutedEdges } = getLayoutedElements(flowNodes, flowEdges);
    setNodes(layoutedNodes);
    setEdges(layoutedEdges);
    setTimeout(() => fitView({ padding: 0.2, duration: 300 }), 50);
  }, [data, fitView]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)),
    []
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)),
    []
  );

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

  if (!nodes?.length) {
    return (
      <div className={`${className} flex items-center justify-center bg-zinc-900/50 rounded-lg border border-zinc-800`}>
        <p className="text-sm text-zinc-500">No infrastructure data available</p>
      </div>
    );
  }

  return (
    <div ref={containerRef} className={`${className} bg-zinc-950 rounded-lg border border-zinc-800 overflow-hidden`}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        nodesDraggable={true}
        nodesConnectable={false}
        elementsSelectable={true}
        panOnDrag={true}
        selectionOnDrag={false}
        panOnScroll={true}
        panOnScrollMode="free"
        zoomOnScroll={false}
        zoomOnPinch={true}
        zoomOnDoubleClick={true}
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
        <Controls showInteractive={false} showFitView={false}>
          <ControlButton onClick={handleCenter} title="Center view">
            <RotateCcw size={16} strokeWidth={2} style={{ stroke: '#fafafa', fill: 'none' }} />
          </ControlButton>
          <ControlButton onClick={handleFullscreen} title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}>
            <Maximize size={16} strokeWidth={2} style={{ stroke: '#fafafa', fill: 'none' }} />
          </ControlButton>
        </Controls>
        
        {/* Legend */}
        <Panel position="bottom-right" className="bg-zinc-900/95 px-4 py-3 rounded-md border border-zinc-700">
          <div className="text-xs space-y-2">
            <div className="font-semibold text-zinc-300 mb-2">Status Legend</div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2" style={{ borderColor: '#22c55e', backgroundColor: '#052e16' }} />
              <span className="text-zinc-400">Healthy</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2" style={{ borderColor: '#eab308', backgroundColor: '#422006' }} />
              <span className="text-zinc-400">Degraded</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2" style={{ borderColor: '#ef4444', backgroundColor: '#450a0a' }} />
              <span className="text-zinc-400">Failed</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2" style={{ borderColor: '#f97316', backgroundColor: '#431407' }} />
              <span className="text-zinc-400">Investigating</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2 border-dashed" style={{ borderColor: '#22c55e', backgroundColor: '#052e16' }} />
              <span className="text-zinc-400">Affected</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded border-2" style={{ borderColor: '#ef4444', backgroundColor: '#450a0a', boxShadow: '0 0 8px rgba(239, 68, 68, 0.5)' }} />
              <span className="text-zinc-400">Root Cause</span>
            </div>
          </div>
        </Panel>
        
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
