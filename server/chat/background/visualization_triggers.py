"""Smart triggers for visualization updates during RCA investigation."""
import re
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class VisualizationTrigger:
    """Detects when to regenerate visualization based on significant events."""
    
    ENTITY_PATTERNS = {
        'kubectl': [
            r'NAME\s+READY\s+STATUS',
            r'NAME\s+TYPE\s+CLUSTER-IP',
            r'NAME\s+HOSTS\s+ADDRESS',
        ],
        'gcloud': [
            r'NAME\s+ZONE\s+MACHINE_TYPE',
            r'NAME\s+LOCATION\s+TIER',
            r'NAME\s+TYPE\s+ADDRESS',
        ],
        'aws': [
            r'InstanceId\s+InstanceType\s+State',
            r'DBInstanceIdentifier\s+Engine',
        ],
    }
    
    ERROR_PATTERNS = [
        r'error:',
        r'failed:',
        r'exception:',
        r'crashloopbackoff',
        r'imagepullbackoff',
        r'oomkilled',
        r'http\s+(500|502|503|504)',
        r'connection\s+refused',
    ]
    
    def __init__(self, incident_id: str, debounce_seconds: int = 10):
        self.incident_id = incident_id
        self.debounce_seconds = debounce_seconds
        self.tool_count = 0
        self.last_trigger_time: Optional[float] = None
    
    def should_trigger(self, tool_name: str, tool_output: str) -> bool:
        """Determine if visualization should update based on tool call."""
        import time
        
        # Debounce: prevent updates more frequent than debounce_seconds
        now = time.time()
        if self.last_trigger_time and (now - self.last_trigger_time) < self.debounce_seconds:
            return False
        
        self.tool_count += 1
        
        # Smart detection: entity discovery
        if self._detects_entity(tool_name, tool_output):
            logger.info(f"[VizTrigger] Entity detected in {tool_name} output")
            self.last_trigger_time = now
            return True
        
        # Smart detection: errors/failures
        if self._detects_error(tool_output):
            logger.info(f"[VizTrigger] Error detected in {tool_name} output")
            self.last_trigger_time = now
            return True
        
        # Fallback: every 5 tool calls
        if self.tool_count % 5 == 0:
            logger.info(f"[VizTrigger] Milestone: {self.tool_count} tool calls")
            self.last_trigger_time = now
            return True
        
        return False
    
    def _detects_entity(self, tool_name: str, output: str) -> bool:
        """Check if output contains infrastructure entity listings."""
        patterns = self.ENTITY_PATTERNS.get(tool_name, [])
        return any(re.search(pattern, output, re.IGNORECASE) for pattern in patterns)
    
    def _detects_error(self, output: str) -> bool:
        """Check for error indicators in tool output."""
        output_lower = output.lower()
        return any(re.search(pattern, output_lower) for pattern in self.ERROR_PATTERNS)
