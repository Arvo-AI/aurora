"""Simple time-based triggers for visualization updates during RCA investigation."""
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)


class VisualizationTrigger:
    """Simple 30-second timer for visualization updates."""
    
    def __init__(self, incident_id: str, interval_seconds: int = 30):
        self.incident_id = incident_id
        self.interval_seconds = interval_seconds
        self.last_trigger_time: Optional[float] = None
    
    def should_trigger(self) -> bool:
        """Trigger every 30 seconds. Extraction logic handles 'no new data' case."""
        now = time.time()
        
        # First call always triggers
        if self.last_trigger_time is None:
            self.last_trigger_time = now
            logger.info(f"[VizTrigger] Initial trigger for incident {self.incident_id}")
            return True
        
        # Check if 30 seconds elapsed
        if (now - self.last_trigger_time) >= self.interval_seconds:
            self.last_trigger_time = now
            logger.info(f"[VizTrigger] 30s elapsed, triggering update for incident {self.incident_id}")
            return True
        
        return False
