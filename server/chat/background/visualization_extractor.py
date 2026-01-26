"""Extracts infrastructure entities from RCA transcripts for visualization."""
import logging
from datetime import datetime
from typing import Dict, List, Literal, Optional
from pydantic import BaseModel, Field
from chat.backend.agent.llm import LLMManager

logger = logging.getLogger(__name__)


class InfraNode(BaseModel):
    """Infrastructure entity node."""
    id: str = Field(description="Unique identifier (e.g., 'svc-api', 'pod-db-1')")
    label: str = Field(description="Display name (8-15 chars)")
    type: Literal['service', 'pod', 'vm', 'database', 'event', 'alert', 'namespace', 'node']
    status: Literal['healthy', 'degraded', 'failed', 'investigating', 'unknown'] = 'investigating'
    metadata: Dict[str, str] = Field(default_factory=dict, description="Additional context")


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
    
    def __init__(self, llm_manager: Optional[LLMManager] = None):
        from chat.backend.agent.providers import create_chat_model
        # Use Claude 3.5 Haiku for fast, cost-effective extraction
        self.llm = create_chat_model(
            "anthropic/claude-3-5-haiku-latest",
            temperature=0.3,
            streaming=False
        )
    
    def extract_incremental(
        self, 
        recent_messages: List[Dict],
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
    
    def _build_prompt(self, messages: List[Dict], existing: Optional[VisualizationData]) -> str:
        """Build extraction prompt with context."""
        messages_text = "\n\n".join([
            f"Tool: {m.get('tool', 'unknown')}\nOutput:\n{m.get('output', '')[:500]}"
            for m in messages[-10:]  # Last 10 only
        ])
        
        existing_context = ""
        if existing and existing.nodes:
            node_summary = ", ".join([f"{n.id}({n.status})" for n in existing.nodes[:10]])
            existing_context = f"\n\nEXISTING ENTITIES: {node_summary}"
        
        return f"""Analyze these recent RCA tool calls to extract infrastructure entities and relationships.

RECENT TOOL CALLS:
{messages_text}
{existing_context}

Extract:
1. NEW infrastructure entities (services, pods, VMs, databases, alerts)
2. Relationships between entities (dependencies, communication, causation)
3. Update status if errors/failures detected
4. Identify root cause if evident

Rules:
- Keep labels concise (8-15 chars)
- Mark as 'investigating' if uncertain
- Mark 'failed'/'degraded' only with clear error evidence
- Include only incident-relevant entities
- Return ONLY new/updated entities if existing context provided

Return structured JSON matching VisualizationData schema."""
    
    def _merge(self, existing: VisualizationData, new: VisualizationData) -> VisualizationData:
        """Merge new entities with existing ones."""
        merged_nodes = {n.id: n for n in existing.nodes}
        
        for node in new.nodes:
            if node.id in merged_nodes:
                # Update status if new info is more specific
                if node.status != 'investigating' and merged_nodes[node.id].status == 'investigating':
                    merged_nodes[node.id].status = node.status
                merged_nodes[node.id].metadata.update(node.metadata)
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
