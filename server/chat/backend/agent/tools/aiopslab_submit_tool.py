"""AIOpsLab RCA submission tool.

This tool allows Aurora's agent to submit RCA findings during benchmarking
and immediately exit the investigation loop.

Only available when AIOPSLAB_BENCHMARK_MODE=true environment variable is set.
"""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class RCACompletedException(Exception):
    """Exception raised when RCA is completed via submit_rca_result tool.
    
    This signals to the workflow that the investigation should stop immediately.
    """
    def __init__(self, result: dict):
        self.result = result
        super().__init__(f"RCA completed: {result.get('system_level')} / {result.get('fault_type')}")


class SubmitRCAArgs(BaseModel):
    """Arguments for submit_rca_result tool."""
    system_level: Literal[
        "Hardware",
        "Operating System", 
        "Virtualization",
        "Application"
    ] = Field(description="The system level at which the fault occurred")
    
    fault_type: Literal[
        "Misconfiguration",
        "Code Defect",
        "Authentication Issue",
        "Network/Storage Issue",
        "Operation Error",
        "Dependency Problem"
    ] = Field(description="The type of fault that occurred")
    
    reasoning: str = Field(
        description="Detailed explanation of your root cause analysis and why you determined this system_level and fault_type"
    )


def submit_rca_result(
    system_level: Literal["Hardware", "Operating System", "Virtualization", "Application"],
    fault_type: Literal["Misconfiguration", "Code Defect", "Authentication Issue", 
                       "Network/Storage Issue", "Operation Error", "Dependency Problem"],
    reasoning: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
) -> str:
    """Submit RCA analysis result for AIOpsLab benchmark.
    
    This tool writes the RCA findings to a JSON file and raises RCACompletedException
    to stop the agent's investigation loop immediately.
    
    Args:
        system_level: The system level where the fault occurred
        fault_type: The type of fault that was identified
        reasoning: Detailed explanation of the root cause analysis
        user_id: Optional user ID (injected by context)
        session_id: Optional session ID (injected by context)
    
    Returns:
        Success message
        
    Raises:
        RCACompletedException: Always raised to signal investigation completion
    """
    try:
        # Validate benchmark mode
        if os.getenv("AIOPSLAB_BENCHMARK_MODE") != "true":
            return "Error: submit_rca_result is only available in benchmark mode (AIOPSLAB_BENCHMARK_MODE=true)"
        
        # Prepare result data
        result = {
            "session_id": session_id,
            "system_level": system_level,
            "fault_type": fault_type,
            "reasoning": reasoning,
            "timestamp": datetime.utcnow().isoformat(),
        }
        
        # Determine output path
        # Try to find AIOpsLab directory
        aurora_root = Path(__file__).parent.parent.parent.parent.parent.parent
        aiopslab_dir = aurora_root / "AIOpsLab"
        
        if not aiopslab_dir.exists():
            # Fallback to current directory
            aiopslab_dir = Path.cwd() / "AIOpsLab"
        
        results_dir = aiopslab_dir / "results"
        results_dir.mkdir(parents=True, exist_ok=True)
        
        # Write result file
        if session_id:
            result_file = results_dir / f"rca_result_{session_id}.json"
        else:
            result_file = results_dir / f"rca_result_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
        
        with open(result_file, "w") as f:
            json.dump(result, f, indent=2)
        
        logger.info(f"[AIOPSLAB] RCA result submitted: {system_level} / {fault_type}")
        logger.info(f"[AIOPSLAB] Result saved to: {result_file}")
        
        # Return success message (don't raise exception as LangChain catches it)
        return json.dumps({
            "status": "success",
            "message": f"RCA submitted: {system_level} / {fault_type}",
            "result_file": str(result_file)
        })
        
    except Exception as e:
        error_msg = f"Failed to submit RCA result: {e}"
        logger.error(f"[AIOPSLAB] {error_msg}", exc_info=True)
        return json.dumps({"status": "error", "message": error_msg})


# Tool metadata for LangChain
TOOL_NAME = "submit_rca_result"
TOOL_DESCRIPTION = """Submit your root cause analysis findings for the AIOpsLab benchmark.

Use this tool IMMEDIATELY when you have identified the root cause. This will:
1. Record your system_level and fault_type analysis
2. End the investigation (no further tool calls needed)
3. Save results for evaluation

Required fields:
- system_level: Choose from Hardware, Operating System, Virtualization, or Application
- fault_type: Choose from Misconfiguration, Code Defect, Authentication Issue, Network/Storage Issue, Operation Error, or Dependency Problem
- reasoning: Explain your analysis in detail

Example:
submit_rca_result(
    system_level="Application",
    fault_type="Misconfiguration",
    reasoning="The geo service has an incorrect MongoDB connection string in its environment variables, causing connection failures."
)
"""
