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
  useReactFlow,
  addEdge,
  Connection,
  NodeResizer
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import './visualization.css';
import { useMemo, useCallback, useState, useEffect, useRef } from 'react';
import { Loader2, Maximize, RotateCcw, Container, Layers, Network, Database, Server, Zap, HardDrive, Archive, Grid3x3, FolderTree, MapPin, Bell, Activity, LucideIcon, Boxes, Trash2, Plus, Download } from 'lucide-react';
import { toPng } from 'html-to-image';
import { toast } from '@/hooks/use-toast';

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

function CustomNode({ data, id, selected }: { data: InfraNode & { isRootCause?: boolean; isAffected?: boolean; onDelete?: (id: string) => void; onLabelChange?: (id: string, label: string) => void; onTypeChange?: (id: string, type: string) => void; onStatusChange?: (id: string, status: NodeStatus) => void; onIsRootCauseChange?: (id: string, isRootCause: boolean) => void; onIsAffectedChange?: (id: string, isAffected: boolean) => void }; id: string; selected?: boolean }) {
  const colors = statusColors[data.status];
  const isRootCause = data.isRootCause;
  const isAffected = data.isAffected;
  const Icon = getIconForType(data.type);
  const [isEditingLabel, setIsEditingLabel] = useState(false);
  const [isEditingType, setIsEditingType] = useState(false);
  const [label, setLabel] = useState(data.label);
  const [type, setType] = useState(data.type);

  const handleLabelDoubleClick = useCallback(() => {
    if (selected) {
      setIsEditingLabel(true);
    }
  }, [selected]);

  const handleTypeDoubleClick = useCallback(() => {
    if (selected) {
      setIsEditingType(true);
    }
  }, [selected]);

  const handleLabelBlur = useCallback(() => {
    setIsEditingLabel(false);
    if (label !== data.label && data.onLabelChange) {
      data.onLabelChange(id, label);
    }
  }, [label, data, id]);

  const handleTypeBlur = useCallback(() => {
    setIsEditingType(false);
    if (type !== data.type && data.onTypeChange) {
      data.onTypeChange(id, type);
    }
  }, [type, data, id]);

  const handleLabelKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      setIsEditingLabel(false);
      if (label !== data.label && data.onLabelChange) {
        data.onLabelChange(id, label);
      }
    } else if (e.key === 'Escape') {
      setLabel(data.label);
      setIsEditingLabel(false);
    }
  }, [label, data, id]);

  const handleTypeKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      setIsEditingType(false);
      if (type !== data.type && data.onTypeChange) {
        data.onTypeChange(id, type);
      }
    } else if (e.key === 'Escape') {
      setType(data.type);
      setIsEditingType(false);
    }
  }, [type, data, id]);

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
          {isEditingType ? (
            <input
              autoFocus
              value={type}
              onChange={(e) => setType(e.target.value)}
              onBlur={handleTypeBlur}
              onKeyDown={handleTypeKeyDown}
              className="nodrag"
              style={{ 
                fontSize: '9px', 
                fontWeight: 700, 
                color: '#71717a',
                backgroundColor: '#27272a',
                padding: '2px 6px',
                borderRadius: '4px',
                letterSpacing: '0.5px',
                textTransform: 'uppercase',
                border: '1px solid #52525b',
                outline: 'none',
                minWidth: '40px'
              }}
            />
          ) : (
            <div 
              style={{ 
                fontSize: '9px', 
                fontWeight: 700, 
                color: '#71717a',
                backgroundColor: '#27272a',
                padding: '2px 6px',
                borderRadius: '4px',
                letterSpacing: '0.5px',
                textTransform: 'uppercase',
                cursor: selected ? 'text' : 'default'
              }}
              onDoubleClick={handleTypeDoubleClick}
              title={selected ? 'Double-click to edit type' : ''}
            >
              {data.type}
            </div>
          )}
          {isEditingLabel ? (
            <input
              autoFocus
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              onBlur={handleLabelBlur}
              onKeyDown={handleLabelKeyDown}
              className="nodrag"
              style={{ 
                fontSize: '13px', 
                fontWeight: 500, 
                background: '#27272a',
                border: '1px solid #52525b',
                borderRadius: '4px',
                padding: '2px 4px',
                color: '#fafafa',
                outline: 'none',
                minWidth: '80px'
              }}
            />
          ) : (
            <div 
              style={{ fontSize: '13px', fontWeight: 500, cursor: selected ? 'text' : 'default' }}
              onDoubleClick={handleLabelDoubleClick}
              title={selected ? 'Double-click to edit label' : ''}
            >
              {data.label}
            </div>
          )}
        </div>
      </div>
      <Handle type="source" position={Position.Bottom} style={{ background: '#52525b' }} />
    </>
  );
}

// Group node component for containers (deployments, clusters, etc.)
function GroupNode({ data, id, selected }: { data: InfraNode & { isRootCause?: boolean; isAffected?: boolean; onDelete?: (id: string) => void; onLabelChange?: (id: string, label: string) => void; onTypeChange?: (id: string, type: string) => void; onStatusChange?: (id: string, status: NodeStatus) => void; onIsRootCauseChange?: (id: string, isRootCause: boolean) => void; onIsAffectedChange?: (id: string, isAffected: boolean) => void }; id: string; selected?: boolean }) {
  const colors = statusColors[data.status];
  const isRootCause = data.isRootCause;
  const isAffected = data.isAffected;
  const Icon = getIconForType(data.type);
  const [isEditingLabel, setIsEditingLabel] = useState(false);
  const [isEditingType, setIsEditingType] = useState(false);
  const [label, setLabel] = useState(data.label);
  const [type, setType] = useState(data.type);

  const handleLabelDoubleClick = useCallback(() => {
    if (selected) {
      setIsEditingLabel(true);
    }
  }, [selected]);

  const handleTypeDoubleClick = useCallback(() => {
    if (selected) {
      setIsEditingType(true);
    }
  }, [selected]);

  const handleLabelBlur = useCallback(() => {
    setIsEditingLabel(false);
    if (label !== data.label && data.onLabelChange) {
      data.onLabelChange(id, label);
    }
  }, [label, data, id]);

  const handleTypeBlur = useCallback(() => {
    setIsEditingType(false);
    if (type !== data.type && data.onTypeChange) {
      data.onTypeChange(id, type);
    }
  }, [type, data, id]);

  const handleLabelKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      setIsEditingLabel(false);
      if (label !== data.label && data.onLabelChange) {
        data.onLabelChange(id, label);
      }
    } else if (e.key === 'Escape') {
      setLabel(data.label);
      setIsEditingLabel(false);
    }
  }, [label, data, id]);

  const handleTypeKeyDown = useCallback((e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      setIsEditingType(false);
      if (type !== data.type && data.onTypeChange) {
        data.onTypeChange(id, type);
      }
    } else if (e.key === 'Escape') {
      setType(data.type);
      setIsEditingType(false);
    }
  }, [type, data, id]);

  return (
    <>
      {selected && (
        <NodeResizer 
          minWidth={200} 
          minHeight={150}
          color="#52525b"
          handleStyle={{ width: '8px', height: '8px', borderRadius: '2px' }}
        />
      )}
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
        {isEditingType ? (
          <input
            autoFocus
            value={type}
            onChange={(e) => setType(e.target.value)}
            onBlur={handleTypeBlur}
            onKeyDown={handleTypeKeyDown}
            className="nodrag"
            style={{ 
              fontSize: '9px', 
              fontWeight: 700, 
              color: '#71717a',
              backgroundColor: '#27272a',
              padding: '2px 6px',
              borderRadius: '4px',
              letterSpacing: '0.5px',
              textTransform: 'uppercase',
              border: '1px solid #52525b',
              outline: 'none',
              flexShrink: 0,
              minWidth: '40px'
            }}
          />
        ) : (
          <div 
            style={{ 
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
              cursor: selected ? 'text' : 'default'
            }}
            onDoubleClick={handleTypeDoubleClick}
            title={selected ? 'Double-click to edit type' : ''}
          >
            {data.type}
          </div>
        )}
        {isEditingLabel ? (
          <input
            autoFocus
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            onBlur={handleLabelBlur}
            onKeyDown={handleLabelKeyDown}
            className="nodrag"
            style={{ 
              fontSize: '12px', 
              fontWeight: 500, 
              background: '#27272a',
              border: '1px solid #52525b',
              borderRadius: '4px',
              padding: '2px 4px',
              color: '#fafafa',
              outline: 'none',
              flex: 1
            }}
          />
        ) : (
          <div 
            style={{ 
              fontSize: '12px', 
              fontWeight: 500, 
              overflow: 'hidden', 
              textOverflow: 'ellipsis', 
              whiteSpace: 'nowrap',
              cursor: selected ? 'text' : 'default'
            }}
            onDoubleClick={handleLabelDoubleClick}
            title={selected ? 'Double-click to edit label' : ''}
          >
            {data.label}
          </div>
        )}
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
  const { fitView, screenToFlowPosition } = useReactFlow();
  const containerRef = useRef<HTMLDivElement>(null);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [isExporting, setIsExporting] = useState(false);

  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);

  const onDeleteNode = useCallback((nodeId: string) => {
    setNodes((nds) => nds.filter((n) => n.id !== nodeId));
    setEdges((eds) => eds.filter((e) => e.source !== nodeId && e.target !== nodeId));
  }, []);

  const onLabelChange = useCallback((nodeId: string, newLabel: string) => {
    setNodes((nds) => nds.map((n) => 
      n.id === nodeId ? { ...n, data: { ...n.data, label: newLabel } } : n
    ));
  }, []);

  const onTypeChange = useCallback((nodeId: string, newType: string) => {
    setNodes((nds) => nds.map((n) => 
      n.id === nodeId ? { ...n, data: { ...n.data, type: newType } } : n
    ));
  }, []);

  const onStatusChange = useCallback((nodeId: string, newStatus: NodeStatus) => {
    setNodes((nds) => nds.map((n) => 
      n.id === nodeId ? { ...n, data: { ...n.data, status: newStatus } } : n
    ));
  }, []);

  const onIsRootCauseChange = useCallback((nodeId: string, isRootCause: boolean) => {
    setNodes((nds) => nds.map((n) => 
      n.id === nodeId ? { ...n, data: { ...n.data, isRootCause } } : n
    ));
  }, []);

  const onIsAffectedChange = useCallback((nodeId: string, isAffected: boolean) => {
    setNodes((nds) => nds.map((n) => 
      n.id === nodeId ? { ...n, data: { ...n.data, isAffected } } : n
    ));
  }, []);

  const exportAsPng = useCallback(async () => {
    if (isExporting || !containerRef.current) return;
    
    setIsExporting(true);
    try {
      const flowElement = containerRef.current.querySelector('.react-flow') as HTMLElement;
      if (!flowElement) throw new Error('Flow element not found');
      
      const dataUrl = await toPng(flowElement, {
        cacheBust: true,
        backgroundColor: '#18181b',
        pixelRatio: 2
      });
      
      const link = document.createElement('a');
      link.download = `visualization-${incidentId}-${Date.now()}.png`;
      link.href = dataUrl;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      
      toast({
        title: 'Export successful',
        description: 'Visualization exported as PNG',
      });
      
      console.log('[Visualization] Exported as PNG');
    } catch (err) {
      console.error('[Visualization] Export error:', err);
      toast({
        title: 'Export failed',
        description: 'Failed to export visualization',
        variant: 'destructive',
      });
    } finally {
      setIsExporting(false);
    }
  }, [incidentId, isExporting]);

  const nodeTypes = useMemo(() => ({ 
    custom: CustomNode, 
    groupNode: GroupNode 
  }), []);

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
    const nodeWithChildren = new Set(
      validNodes
        .filter(n => n.parentId)
        .map(n => n.parentId!)
    );
    
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
          onDelete: onDeleteNode,
          onLabelChange: onLabelChange,
          onTypeChange: onTypeChange,
          onStatusChange: onStatusChange,
          onIsRootCauseChange: onIsRootCauseChange,
          onIsAffectedChange: onIsAffectedChange
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
  }, [data, fitView, onDeleteNode, onLabelChange, onTypeChange]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((nds) => applyNodeChanges(changes, nds)),
    []
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((eds) => applyEdgeChanges(changes, eds)),
    []
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((eds) => addEdge({
        ...connection,
        type: 'smoothstep',
        animated: false,
        style: { stroke: '#52525b', strokeWidth: 2 },
        markerEnd: { type: 'arrowclosed', color: '#52525b' }
      }, eds));
    },
    []
  );

  const onNodeClick = useCallback((_event: React.MouseEvent, node: Node) => {
    setSelectedNode(node.id);
  }, []);

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
  }, []);

  const addConnectedNode = useCallback((nodeType: string) => {
    if (!selectedNode) return;
    
    const sourceNode = nodes.find(n => n.id === selectedNode);
    if (!sourceNode) return;
    
    const newNodeId = `node-${Date.now()}`;
    const isGroup = nodeType === 'group';
    
    const newNode: Node = {
      id: newNodeId,
      type: isGroup ? 'groupNode' : 'custom',
      position: { 
        x: sourceNode.position.x + 250, 
        y: sourceNode.position.y 
      },
      data: {
        label: isGroup ? 'Group' : 'Node',
        type: 'unknown',
        status: 'unknown',
        onDelete: onDeleteNode,
        onLabelChange: onLabelChange,
        onTypeChange: onTypeChange,
        onStatusChange: onStatusChange,
        onIsRootCauseChange: onIsRootCauseChange,
        onIsAffectedChange: onIsAffectedChange
      },
      draggable: true,
      selectable: true,
      ...(isGroup && {
        style: {
          width: 250,
          height: 200,
          backgroundColor: 'rgba(39, 39, 42, 0.5)',
          border: '2px solid #52525b',
          borderRadius: '8px',
          padding: '20px',
        }
      })
    };
    
    setNodes((nds) => [...nds, newNode]);
    setEdges((eds) => addEdge({
      id: `e-${selectedNode}-${newNodeId}`,
      source: selectedNode,
      target: newNodeId,
      type: 'smoothstep',
      animated: false,
      style: { stroke: '#52525b', strokeWidth: 2 },
      markerEnd: { type: 'arrowclosed', color: '#52525b' }
    }, eds));
    setSelectedNode(newNodeId);
  }, [selectedNode, nodes, onDeleteNode, onLabelChange, onTypeChange, onStatusChange, onIsRootCauseChange, onIsAffectedChange]);

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
        onConnect={onConnect}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        nodesDraggable={true}
        nodesConnectable={true}
        elementsSelectable={true}
        panOnDrag={true}
        selectionOnDrag={false}
        panOnScroll={false}
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
          <ControlButton onClick={exportAsPng} title={isExporting ? 'Exporting...' : 'Export as PNG'} disabled={isExporting}>
            <Download size={16} strokeWidth={2} style={{ stroke: isExporting ? '#71717a' : '#fafafa', fill: 'none' }} />
          </ControlButton>
          <ControlButton onClick={handleCenter} title="Center view">
            <RotateCcw size={16} strokeWidth={2} style={{ stroke: '#fafafa', fill: 'none' }} />
          </ControlButton>
          <ControlButton onClick={handleFullscreen} title={isFullscreen ? 'Exit fullscreen' : 'Fullscreen'}>
            <Maximize size={16} strokeWidth={2} style={{ stroke: '#fafafa', fill: 'none' }} />
          </ControlButton>
        </Controls>
        
        {/* Side Panel for Node Controls */}
        {selectedNode && (() => {
          const node = nodes.find(n => n.id === selectedNode);
          if (!node) return null;
          const nodeData = node.data as unknown as InfraNode & { isRootCause?: boolean; isAffected?: boolean };
          const scale = isFullscreen ? 1 : 0.85;
          
          return (
            <Panel position="top-left" className="bg-zinc-900/95 border border-zinc-700 rounded-md" style={{ 
              padding: isFullscreen ? '12px' : '10px', 
              minWidth: isFullscreen ? '200px' : '170px',
              transform: `scale(${scale})`,
              transformOrigin: 'top left'
            }}>
              <div style={{ marginBottom: isFullscreen ? '12px' : '10px' }}>
                <div style={{ fontSize: isFullscreen ? '12px' : '11px', fontWeight: 600, color: '#fafafa', marginBottom: isFullscreen ? '8px' : '6px' }}>Node Controls</div>
                
                {/* Delete Button */}
                <button
                  onClick={() => onDeleteNode(selectedNode)}
                  className="nodrag"
                  style={{ 
                    width: '100%',
                    padding: isFullscreen ? '6px 10px' : '5px 8px', 
                    background: '#ef4444', 
                    color: 'white', 
                    border: 'none', 
                    borderRadius: '4px', 
                    cursor: 'pointer', 
                    fontSize: isFullscreen ? '12px' : '11px', 
                    display: 'flex', 
                    alignItems: 'center', 
                    gap: '6px',
                    justifyContent: 'center',
                    fontWeight: 500
                  }}
                >
                  <Trash2 size={isFullscreen ? 14 : 12} />
                  Delete Node
                </button>
              </div>

              {/* Color Section */}
              <div style={{ marginBottom: isFullscreen ? '12px' : '10px' }}>
                <div style={{ fontSize: isFullscreen ? '10px' : '9px', color: '#71717a', fontWeight: 600, marginBottom: isFullscreen ? '6px' : '5px' }}>COLOR</div>
                <div style={{ display: 'flex', gap: isFullscreen ? '6px' : '4px', flexWrap: 'wrap' }}>
                  {(['healthy', 'degraded', 'failed', 'investigating', 'unknown'] as NodeStatus[]).map((status) => (
                    <button
                      key={status}
                      onClick={() => onStatusChange(selectedNode, status)}
                      className="nodrag"
                      style={{
                        width: isFullscreen ? '32px' : '26px',
                        height: isFullscreen ? '32px' : '26px',
                        border: `2px solid ${statusColors[status].border}`,
                        backgroundColor: statusColors[status].bg,
                        borderRadius: '4px',
                        cursor: 'pointer',
                        opacity: nodeData.status === status ? 1 : 0.5,
                        transition: 'opacity 0.15s'
                      }}
                      title={status}
                    />
                  ))}
                </div>
              </div>

              {/* Contour Section */}
              <div style={{ marginBottom: isFullscreen ? '12px' : '10px' }}>
                <div style={{ fontSize: isFullscreen ? '10px' : '9px', color: '#71717a', fontWeight: 600, marginBottom: isFullscreen ? '6px' : '5px' }}>CONTOUR</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  <button
                    onClick={() => onIsAffectedChange(selectedNode, !nodeData.isAffected)}
                    className="nodrag"
                    style={{
                      padding: isFullscreen ? '6px 10px' : '5px 8px',
                      background: nodeData.isAffected ? '#3f3f46' : '#27272a',
                      color: '#fafafa',
                      border: '1px solid #52525b',
                      borderStyle: 'dashed',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: isFullscreen ? '11px' : '10px',
                      fontWeight: 500
                    }}
                  >
                    {nodeData.isAffected ? '✓' : ''} Affected
                  </button>
                  <button
                    onClick={() => onIsRootCauseChange(selectedNode, !nodeData.isRootCause)}
                    className="nodrag"
                    style={{
                      padding: isFullscreen ? '6px 10px' : '5px 8px',
                      background: nodeData.isRootCause ? '#3f3f46' : '#27272a',
                      color: '#fafafa',
                      border: '1px solid #52525b',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: isFullscreen ? '11px' : '10px',
                      fontWeight: 500,
                      boxShadow: nodeData.isRootCause ? '0 0 8px rgba(239, 68, 68, 0.5)' : 'none'
                    }}
                  >
                    {nodeData.isRootCause ? '✓' : ''} Root Cause
                  </button>
                </div>
              </div>

              {/* Add Node Section */}
              <div>
                <div style={{ fontSize: isFullscreen ? '10px' : '9px', color: '#71717a', fontWeight: 600, marginBottom: isFullscreen ? '6px' : '5px' }}>ADD CONNECTED NODE</div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                  <button
                    onClick={() => addConnectedNode('node')}
                    className="nodrag"
                    style={{
                      padding: isFullscreen ? '6px 10px' : '5px 8px',
                      background: '#27272a',
                      color: '#fafafa',
                      border: '1px solid #3f3f46',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: isFullscreen ? '12px' : '11px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      fontWeight: 500,
                      transition: 'background 0.15s'
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.background = '#3f3f46'}
                    onMouseLeave={(e) => e.currentTarget.style.background = '#27272a'}
                  >
                    <Plus size={isFullscreen ? 14 : 12} />
                    Node
                  </button>
                  <button
                    onClick={() => addConnectedNode('group')}
                    className="nodrag"
                    style={{
                      padding: isFullscreen ? '6px 10px' : '5px 8px',
                      background: '#27272a',
                      color: '#fafafa',
                      border: '1px solid #3f3f46',
                      borderRadius: '4px',
                      cursor: 'pointer',
                      fontSize: isFullscreen ? '12px' : '11px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '6px',
                      fontWeight: 500,
                      transition: 'background 0.15s'
                    }}
                    onMouseEnter={(e) => e.currentTarget.style.background = '#3f3f46'}
                    onMouseLeave={(e) => e.currentTarget.style.background = '#27272a'}
                  >
                    <Plus size={isFullscreen ? 14 : 12} />
                    Group
                  </button>
                </div>
              </div>
            </Panel>
          );
        })()}
        
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
              v{data.version} · {nodes.length} nodes · {edges.length} edges
            </div>
          </Panel>
        )}
      </ReactFlow>
    </div>
  );
}
