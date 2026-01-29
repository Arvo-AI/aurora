"""Extracts infrastructure entities from RCA transcripts for visualization."""
import logging
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Tool output truncation limit (matches task.py constant)
MAX_TOOL_OUTPUT_CHARS = 5000


class InfraNode(BaseModel):
    """Infrastructure entity node."""
    id: str = Field(description="Unique identifier (e.g., 'svc-api', 'pod-db-1')")
    label: str = Field(description="Display name (8-15 chars)")
    type: Literal['service', 'pod', 'vm', 'database', 'event', 'alert', 'namespace', 'node']
    status: Literal['healthy', 'degraded', 'failed', 'investigating', 'unknown'] = 'investigating'


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
    updatedAt: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


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
                merged.updatedAt = datetime.utcnow().isoformat()
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
            existing_context = f"\n\nEXISTING ENTITIES ({len(existing.nodes)} nodes, {len(existing.edges)} edges): {node_summary}"
        
        return f"""Analyze these RCA tool calls and extract infrastructure entities as structured data.

TOOL OUTPUTS:
{messages_text}
{existing_context}

Extract infrastructure entities (services, pods, VMs, databases, alerts, namespaces, nodes) and relationships.

Rules:
- Labels: 8-15 chars
- Status: 'investigating' if uncertain, 'failed'/'degraded' only with clear errors
- Include only incident-relevant entities
- If existing entities provided, return ONLY new/updated ones

Return structured data matching VisualizationData schema."""
    
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
