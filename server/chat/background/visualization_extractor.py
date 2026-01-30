"""Extracts infrastructure entities from RCA transcripts for visualization."""
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from chat.backend.constants import MAX_TOOL_OUTPUT_CHARS

logger = logging.getLogger(__name__)


class InfraNode(BaseModel):
    """Infrastructure entity node."""
    id: str = Field(description="Unique identifier (e.g., 'svc-api', 'pod-db-1')")
    label: str = Field(description="Display name - use full entity name from infrastructure")
    type: str = Field(description="Infrastructure entity type (e.g., 'pod', 'deployment', 'lambda', 'load-balancer', 'database')")
    status: Literal['healthy', 'degraded', 'failed', 'investigating', 'unknown'] = 'investigating'
    parentId: Optional[str] = Field(default=None, description="ID of parent node for hierarchical grouping (e.g., cluster, namespace, region)")


class InfraEdge(BaseModel):
    """Relationship between entities."""
    source: str = Field(description="Source node ID")
    target: str = Field(description="Target node ID")
    label: str = Field(default="", description="Relationship description")
    type: Literal['dependency', 'communication', 'causation', 'hosts'] = 'dependency'


class VisualizationData(BaseModel):
    """Complete visualization state."""
    nodes: List[InfraNode] = Field(default_factory=list)
    edges: List[InfraEdge] = Field(default_factory=list)
    rootCauseId: Optional[str] = Field(default=None, description="Node ID of root cause")
    affectedIds: List[str] = Field(default_factory=list, description="Affected node IDs")
    version: int = Field(default=1, description="Incremented on each update")
    updatedAt: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class VisualizationExtractor:
    """Extracts infrastructure entities from RCA tool calls."""
    
    def __init__(self):
        from chat.backend.agent.providers import create_chat_model
        from chat.backend.agent.llm import ModelConfig
        
        self.llm = create_chat_model(
            ModelConfig.VISUALIZATION_MODEL,
            temperature=0.3,
            streaming=False
        )
    
    def extract_incremental(
        self, 
        recent_messages: List[Dict[str, Any]],
        existing_viz: Optional[VisualizationData] = None
    ) -> VisualizationData:
        """Extract entities from recent tool calls and merge with existing state."""
        if not recent_messages:
            logger.warning("[VizExtractor] No messages to extract from")
            return existing_viz or VisualizationData()
        
        prompt = self._build_prompt(recent_messages, existing_viz)
        
        try:
            extractor = self.llm.with_structured_output(VisualizationData)
            new_viz = extractor.invoke(prompt)
            
            if existing_viz:
                merged = self._merge(existing_viz, new_viz)
                merged.version = existing_viz.version + 1
                merged.updatedAt = datetime.now(timezone.utc).isoformat()
                return merged
            
            return new_viz
        
        except Exception as e:
            logger.error(f"[VizExtractor] Extraction failed: {e}")
            return existing_viz or VisualizationData()
    
    def _build_prompt(self, messages: List[Dict[str, Any]], existing: Optional[VisualizationData]) -> str:
        """Build extraction prompt with context."""
        messages_text = "\n\n".join([
            f"Tool: {m.get('tool', 'unknown')}\nOutput:\n{m.get('output', '')[:MAX_TOOL_OUTPUT_CHARS]}"
            for m in messages[-10:]
        ])
        
        logger.debug(f"[VizExtractor] Processing {len(messages)} messages, prompt length: {len(messages_text)} chars")
        
        existing_context = ""
        if existing and existing.nodes:
            node_summary = ", ".join([f"{n.id}({n.status})" for n in existing.nodes])
            existing_context = f"\n\nEXISTING GRAPH ({len(existing.nodes)} nodes, {len(existing.edges)} edges):\n{node_summary}"
        
        return f"""You are building a visual incident graph to help SREs quickly understand WHAT caused WHAT during an incident.

GOAL: Create a FLAT graph with maximum 2 levels of nesting. Show causation chain from root cause to impact.

**CRITICAL CONSTRAINT**: parentId creates visual nesting. NEVER chain parentIds (no grandchildren). 
If node A has parentId=B, then B MUST have parentId=null.

TOOL OUTPUTS FROM INVESTIGATION:
{messages_text}
{existing_context}

EXTRACTION RULES:

1. HIERARCHY RULES (STRICT - MAX 2 LEVELS):
   - A node with parentId is a CHILD. Its parent MUST have parentId=null.
   - NEVER create chains: if pod.parentId=deploy, then deploy.parentId MUST be null
   - Pick ONE grouping level (deployment OR namespace OR cluster, not all):
     * Pods failing? → Group pods under deployment (deployment.parentId=null, pod.parentId=deployment)
     * Multiple services? → Group services under namespace (namespace.parentId=null, svc.parentId=namespace)
   - All other nodes (alerts, events, databases) should have parentId=null
   - CORRECT example:
     * {{"id": "api-deploy", "parentId": null}}  // Parent has null
     * {{"id": "pod-1", "parentId": "api-deploy"}}  // Child points to parent
     * {{"id": "alert", "parentId": null}}  // Standalone node
   - WRONG (grandchildren):
     * {{"id": "cluster", "parentId": null}}
     * {{"id": "deploy", "parentId": "cluster"}}
     * {{"id": "pod", "parentId": "deploy"}}  // WRONG: pod is grandchild of cluster!

2. FOCUS ON CAUSALITY:
   - Prioritize entities directly involved in the failure chain
   - Use 'causation' edges to show what caused what (e.g., pod restart → service downtime → alert)
   - Identify the ROOT CAUSE (first point of failure) and set rootCauseId
   - Mark all downstream affected entities in affectedIds

3. ENTITY SELECTION (only include if relevant to incident):
   - Alert/event that triggered investigation
   - Any infrastructure entities showing failures/degradation
   - Upstream dependencies that may have caused the issue
   - Container/grouping entities (clusters, namespaces, regions) when multiple children are involved
   - Use specific entity types from your infrastructure knowledge:
     * Kubernetes: pod, deployment, service, statefulset, daemonset, replicaset, node, namespace, cluster, ingress, pvc
     * Cloud: lambda, cloud-function, vm, instance, load-balancer, api-gateway, bucket, queue, region, vpc, subnet, availability-zone
     * Databases: database, postgres, mysql, redis, mongodb, elasticsearch
     * Monitoring: alert, event, metric
   - Keep graph focused - omit healthy unrelated infrastructure

4. STATUS ASSIGNMENT (use evidence from tool outputs):
   - 'failed': Clear errors, crashes, restarts, or unavailability (CrashLoopBackOff, 5xx errors, OOMKilled)
   - 'degraded': High latency, resource exhaustion, partial failures (CPU/memory pressure, slow responses)
   - 'investigating': Mentioned in investigation but status unclear
   - 'healthy': Explicitly confirmed working normally
   - 'unknown': No status information available
   - For group/container nodes: use worst status of children

5. RELATIONSHIPS (be specific):
   - **CRITICAL - NO HIERARCHY EDGES**: NEVER create edges between parent and child nodes
     * If node A has parentId=B, do NOT create an edge A→B or B→A
     * Hierarchy is shown visually by containment (parentId), NOT by edges
     * Example: If pod has parentId=deployment, NO edge between pod and deployment
   - **IMPORTANT**: Do NOT create edges for hierarchical containment (cluster→namespace, namespace→deployment, etc.)
     * These are automatically shown via parentId
     * Edges like "contains namespace", "contains deployment", "manages pod" should NOT exist
   - Only create edges for functional relationships:
     * 'causation': A directly caused B (e.g., OOMKilled pod → service unavailable)
     * 'dependency': A depends on B (e.g., API service → database)
     * 'communication': A talks to B (e.g., frontend → backend API)
   - Add descriptive labels explaining the relationship
   - Example: If pod A crashes and causes service B to fail, create causation edge A→B
   - Example: If service A calls database B, create dependency edge A→B

6. LABELING:
   - Use actual names from infrastructure (pod names, service names, etc.)
   - For nodes: use full entity names as they appear in the infrastructure (e.g., 'api-pod-3x7k2s', 'postgres-primary-0')
   - For group nodes: use descriptive names (e.g., 'Production Cluster', 'us-east-1')
   - Do NOT truncate or abbreviate names

7. INCREMENTAL UPDATES:
   - If existing graph provided, return ONLY new entities or status updates
   - Preserve existing parentId relationships unless evidence shows they're wrong
   - Update rootCauseId only if you have stronger evidence
   - Add to affectedIds as more downstream impact is discovered

OUTPUT: Structured VisualizationData with FLAT hierarchy (max 2 levels). 
REMINDER: If a node has parentId, that parent MUST have parentId=null. No grandchildren allowed."""
    
    def _merge(self, existing: VisualizationData, new: VisualizationData) -> VisualizationData:
        """Merge new entities with existing ones."""
        merged_nodes = {n.id: n for n in existing.nodes}
        
        for node in new.nodes:
            if node.id in merged_nodes:
                # Update status if new info is more specific
                if node.status != 'investigating' and merged_nodes[node.id].status == 'investigating':
                    merged_nodes[node.id].status = node.status
            else:
                merged_nodes[node.id] = node
        
        # Merge edges (dedupe by source-target)
        edge_keys = {(e.source, e.target): e for e in existing.edges}
        for edge in new.edges:
            key = (edge.source, edge.target)
            if key not in edge_keys:
                edge_keys[key] = edge
        
        return VisualizationData(
            nodes=list(merged_nodes.values()),
            edges=list(edge_keys.values()),
            rootCauseId=new.rootCauseId or existing.rootCauseId,
            affectedIds=list(set(existing.affectedIds + new.affectedIds)),
        )
