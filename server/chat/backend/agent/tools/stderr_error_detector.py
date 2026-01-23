"""Detect errors in STDERR output from cloud CLI commands."""
import re

def _extract_ovh_error(stderr_text):
    """
    OVH CLI outputs debug JSON to stderr before the actual error.
    Extract only the actual error message after the debug block.
    
    Example stderr:
    2025/12/09 21:42:06 Final parameters:
    {
      "billingPeriod": "hourly",
      ...
    }
    failed to create instance: <actual error>
    """
    # Look for OVH error patterns after the debug JSON
    error_patterns = [
        r'failed to [^:]+: (.+)',  # "failed to create instance: ..."
        r'error[:\s]+(.+)',  # "error: ..." or "Error ..."
        r'(?:^|\n)([A-Z][^{}\n]+(?:not found|denied|failed|invalid|missing)[^{}\n]*)',  # Sentence-style errors
    ]
    
    for pattern in error_patterns:
        match = re.search(pattern, stderr_text, re.IGNORECASE | re.MULTILINE)
        if match:
            return match.group(0).strip()
    
    return None


def detect_errors_in_stderr(stderr_text):
    """
    Detect errors in STDERR output even when return code is 0.
    Returns (has_error: bool, error_message: str or None)
    
    Only flag as error if stderr contains explicit error keywords.
    """
    if not stderr_text or not stderr_text.strip():
        return False, None
    
    stderr_lower = stderr_text.lower()
    
    # Check for OVH CLI debug output - extract actual error if present
    if "final parameters:" in stderr_lower:
        ovh_error = _extract_ovh_error(stderr_text)
        if ovh_error:
            return True, ovh_error
        # OVH debug output only (no actual error) - treat as benign
        # This happens when the debug info is printed but command succeeds
        # Check if there's anything after the JSON block that looks like an error
        # If not, the exit code alone will determine success/failure
        return False, None
    
    # Only flag if stderr contains explicit error indicators
    if "error:" in stderr_lower or "fatal:" in stderr_lower:
        return True, stderr_text.strip()
    
    return False, None
