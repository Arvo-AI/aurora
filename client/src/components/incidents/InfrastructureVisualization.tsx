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
import { Loader2, Maximize, RotateCcw, Container, Layers, Network, Database, Server, Zap, HardDrive, Archive, Grid3x3, FolderTree, MapPin, Bell, Activity, LucideIcon, Boxes } from 'lucide-react';

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

// Layout constants for group nodes
const GROUP_HEADER_HEIGHT = 40;
const CHILD_NODE_HEIGHT = 100;
const CHILD_NODE_SPACING = 30;
const CHILD_TOTAL_HEIGHT = CHILD_NODE_HEIGHT + CHILD_NODE_SPACING;

function getIconForType(type: string): LucideIcon | null {
  const iconMap: Record<string, LucideIcon> = {
    pod: Container, deployment: Layers, service: Network, statefulset: Database, daemonset: Grid3x3, replicaset: Layers,
    vm: Server, instance: Server, lambda: Zap, 'cloud-function': Zap, node: HardDrive,
    'load-balancer': Network, ingress: Network, 'api-gateway': Network,
    database: Database, postgres: Database, mysql: Database, mongodb: Database, redis: Database, elasticsearch: Database,
    bucket: Archive, pvc: HardDrive, queue: Activity,
    cluster: Boxes, namespace: FolderTree, region: MapPin,
    alert: Bell, event: Activity, metric: Activity,
  };
  return iconMap[type.toLowerCase()] || null;
}

function CustomNode({ data }: { data: InfraNode & { isRootCause?: boolean; isAffected?: boolean } }) {
  const colors = statusColors[data.status];
  const isRootCause = data.isRootCause;
  const isAffected = data.isAffected;
  const Icon = getIconForType(data.type);

  return (
    <>
      <Handle type="target" position={Position.Top} style={{ background: '#52525b' }} />
      <div
        style={{
          padding: '12px 16px',
          paddingRight: Icon ? '32px' : '16px',
          border: `2px ${isAffected ? 'dashed' : 'solid'} ${colors.border}`,
          borderRadius: '8px',
          backgroundColor: colors.bg,
          minWidth: '120px',
          boxShadow: isRootCause ? `0 0 20px ${colors.glow}` : `0 0 8px ${colors.glow}`,
          fontWeight: isRootCause ? 600 : 400,
          position: 'relative',
        }}
      >
        {Icon && <Icon size={14} style={{ position: 'absolute', top: 8, right: 8, opacity: 0.6, color: colors.border }} />}
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
          <div style={{ fontSize: '13px', fontWeight: 500 }}>{data.label}</div>
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: '#52525b' }} />
    </>
  );
}

// Group node component for containers (deployments, clusters, etc.)
function GroupNode({ data }: { data: InfraNode & { isRootCause?: boolean; isAffected?: boolean } }) {
  const colors = statusColors[data.status];
  const Icon = getIconForType(data.type);

  return (
    <>
      <Handle type="target" position={Position.Top} style={{ background: '#52525b' }} />
      <div
        style={{
          position: 'absolute',
          top: 0,
          left: 0,
          right: 0,
          height: GROUP_HEADER_HEIGHT,
          padding: '10px 12px',
          paddingRight: Icon ? '36px' : '12px',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          color: '#fafafa',
          borderBottom: `1px solid ${colors.border}40`,
          overflow: 'hidden',
        }}
      >
        {Icon && <Icon size={14} style={{ position: 'absolute', top: 10, right: 12, opacity: 0.6, color: colors.border }} />}
        <div style={{ 
          fontSize: '9px', 
          fontWeight: 700, 
          color: '#71717a',
          backgroundColor: '#27272a',
          padding: '2px 6px',
          borderRadius: '4px',
          letterSpacing: '0.5px',
          textTransform: 'uppercase',
          flexShrink: 0,
          whiteSpace: 'nowrap',
        }}>
          {data.type}
        </div>
        <div style={{ fontSize: '12px', fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          {data.label}
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: '#52525b' }} />
    </>
  );
}

function getLayoutedElements(nodes: Node[], edges: Edge[]) {
  const nodeWidth = 200;
  const nodeHeight = 100;
  const horizontalSpacing = 200;
  const verticalSpacing = 150;
  const groupPadding = 40;

  // Helper to get actual node dimensions (accounting for groups)
  const getNodeDimensions = (node: Node) => {
    if (node.style && typeof node.style === 'object' && 'width' in node.style && 'height' in node.style) {
      return { 
        width: Number(node.style.width) || nodeWidth, 
        height: Number(node.style.height) || nodeHeight 
      };
    }
    return { width: nodeWidth, height: nodeHeight };
  };

  // Build adjacency information
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

  // Find root nodes (nodes without parents and no incoming edges)
  const rootNodes = nodes.filter(node => 
    !node.parentId && (incomingEdges.get(node.id)?.length || 0) === 0
  );

  // Assign layers using BFS
  const layers = new Map<string, number>();
  const visited = new Set<string>();
  const queue: Array<{ id: string; layer: number }> = [];

  rootNodes.forEach(node => {
    queue.push({ id: node.id, layer: 0 });
  });

  while (queue.length > 0) {
    const { id, layer } = queue.shift()!;
    if (visited.has(id)) continue;

    visited.add(id);
    layers.set(id, layer);

    const targets = outgoingEdges.get(id) || [];
    targets.forEach(targetId => {
      const targetNode = nodes.find(n => n.id === targetId);
      if (!visited.has(targetId) && !targetNode?.parentId) {
        const currentLayer = layers.get(targetId);
        if (currentLayer === undefined || layer + 1 > currentLayer) {
          queue.push({ id: targetId, layer: layer + 1 });
        }
      }
    });
  }

  // Assign remaining unvisited nodes to appropriate layers
  nodes.forEach(node => {
    if (!layers.has(node.id) && !node.parentId) {
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

  // Position nodes
  const layoutedNodes = nodes.map(node => {
    // Handle child nodes (relative positioning within parent)
    if (node.parentId) {
      const siblings = nodes.filter(n => n.parentId === node.parentId);
      const index = siblings.findIndex(n => n.id === node.id);
      return {
        ...node,
        targetPosition: Position.Top,
        sourcePosition: Position.Bottom,
        position: {
          x: groupPadding,
          y: groupPadding + index * CHILD_TOTAL_HEIGHT,
        },
      };
    }

    // Handle regular nodes
    const layer = layers.get(node.id) ?? 0;
    const nodesInLayer = nodesByLayer.get(layer) || [node.id];
    const indexInLayer = nodesInLayer.indexOf(node.id);
    
    // Build node lookup map for performance
    const nodeMap = new Map(nodes.map(n => [n.id, n]));
    
    // Center nodes in each layer, accounting for actual node widths
    let totalLayerWidth = 0;
    nodesInLayer.forEach((nodeId, idx) => {
      const layerNode = nodeMap.get(nodeId);
      if (layerNode) {
        const { width } = getNodeDimensions(layerNode);
        totalLayerWidth += width;
        if (idx < nodesInLayer.length - 1) totalLayerWidth += horizontalSpacing;
      }
    });
    
    const startX = -totalLayerWidth / 2;
    let xOffset = startX;
    for (let i = 0; i < indexInLayer; i++) {
      const prevNode = nodeMap.get(nodesInLayer[i]);
      if (prevNode) {
        const { width } = getNodeDimensions(prevNode);
        xOffset += width + horizontalSpacing;
      }
    }
    
    const { width: currentNodeWidth } = getNodeDimensions(node);
    const x = xOffset + currentNodeWidth / 2;
    const y = layer * (nodeHeight + verticalSpacing);

    return {
      ...node,
      targetPosition: Position.Top,
      sourcePosition: Position.Bottom,
      position: { x, y },
    };
  });

  return { nodes: layoutedNodes, edges };
}

export default function InfrastructureVisualization({ incidentId, className }: Props) {
  const { data, isLoading, error } = useVisualizationStream(incidentId);
  const { fitView } = useReactFlow();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const nodeTypes = useMemo(() => ({ custom: CustomNode, groupNode: GroupNode }), []);

  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);

  const handleFullscreen = useCallback(() => {
    if (!containerRef.current) return;

    if (!document.fullscreenElement) {
      containerRef.current.requestFullscreen();
      setIsFullscreen(true);
      // Auto-center when entering fullscreen
      setTimeout(() => fitView({ padding: 0.2, duration: 300 }), 100);
    } else {
      document.exitFullscreen();
      setIsFullscreen(false);
    }
  }, [fitView]);

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

    // Build node ID set for validation
    const nodeIdSet = new Set(data.nodes.map(n => n.id));
    
    // Validate all parentId references exist
    const validNodes = data.nodes.filter(node => {
      if (node.parentId && !nodeIdSet.has(node.parentId)) {
        console.warn(`Node ${node.id} has invalid parentId: ${node.parentId}`);
        return false;
      }
      return true;
    });

    // Separate group nodes (those with children) from regular nodes
    const nodeWithChildren = new Set(validNodes.filter(n => n.parentId).map(n => n.parentId));
    
    // Calculate dynamic group sizes based on child count
    const groupSizes = new Map<string, { width: number; height: number }>();
    nodeWithChildren.forEach(groupId => {
      const childCount = validNodes.filter(n => n.parentId === groupId).length;
      const paddingBottom = 40;
      
      const calculatedHeight = GROUP_HEADER_HEIGHT + (childCount * CHILD_NODE_HEIGHT) + (Math.max(0, childCount - 1) * CHILD_NODE_SPACING) + paddingBottom;
      groupSizes.set(groupId, { width: 250, height: Math.max(200, calculatedHeight) });
    });
    
    const flowNodes: Node[] = validNodes.map((node) => {
      const isGroupNode = nodeWithChildren.has(node.id);
      
      return {
        id: node.id,
        type: isGroupNode ? 'groupNode' : 'custom',
        position: { x: 0, y: 0 },
        draggable: true,
        selectable: true,
        ...(node.parentId && { 
          parentId: node.parentId,
          extent: 'parent' as const,
        }),
        ...(isGroupNode && {
          style: {
            width: groupSizes.get(node.id)?.width || 250,
            height: groupSizes.get(node.id)?.height || 200,
            backgroundColor: 'rgba(39, 39, 42, 0.5)',
            border: '2px solid #52525b',
            borderRadius: '8px',
            padding: '20px',
          },
        }),
        data: {
          ...node,
          isRootCause: node.id === data.rootCauseId,
          isAffected: data.affectedIds.includes(node.id),
        },
      };
    });

    // Sort nodes so parent nodes come before their children
    const sortedFlowNodes = flowNodes.sort((a, b) => {
      // If a is parent of b, a comes first
      if (b.parentId === a.id) return -1;
      // If b is parent of a, b comes first
      if (a.parentId === b.id) return 1;
      // If a has no parent but b does, a comes first
      if (!a.parentId && b.parentId) return -1;
      // If b has no parent but a does, b comes first
      if (a.parentId && !b.parentId) return 1;
      // Otherwise maintain original order
      return 0;
    });

    // Filter out ONLY hierarchy edges (parent ↔ child relationships)
    // Functional edges involving group nodes are OK (e.g., deployment → alert)
    const parentChildEdges = new Set<string>();
    sortedFlowNodes.forEach(node => {
      if (node.parentId) {
        // Create bidirectional edge keys for parent-child relationships
        parentChildEdges.add(`${node.id}-${node.parentId}`);
        parentChildEdges.add(`${node.parentId}-${node.id}`);
      }
    });
    
    const flowEdges: Edge[] = data.edges
      .filter(edge => {
        const edgeKey = `${edge.source}-${edge.target}`;
        return !parentChildEdges.has(edgeKey);
      })
      .map((edge, idx) => ({
        id: `e${idx}`,
        source: edge.source,
        target: edge.target,
        ...(edge.label && { label: edge.label }),
        type: 'smoothstep',
        animated: edge.type === 'causation',
        style: { stroke: '#52525b', strokeWidth: 2 },
        labelStyle: { fill: '#71717a', fontSize: 10, fontWeight: 500 },
        labelBgStyle: { fill: '#18181b', fillOpacity: 0.9 },
        markerEnd: { type: 'arrowclosed', color: '#52525b' },
      }));

    const { nodes: layoutedNodes, edges: layoutedEdges } = getLayoutedElements(sortedFlowNodes, flowEdges);
    
    setNodes(layoutedNodes);
    setEdges(layoutedEdges);
    setTimeout(() => fitView({ padding: 0.2, duration: 300 }), 50);
  }, [data, fitView]); // fitView is stable from useReactFlow

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
        panOnScroll={false}
        panOnScrollMode="free"
        zoomOnScroll={true}
        zoomOnPinch={true}
        zoomOnDoubleClick={true}
        fitView
        minZoom={0.1}
        maxZoom={2}
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
