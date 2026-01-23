"""
Output sanitization utilities for tool results.
Prevents WebSocket encoding errors from command output containing binary data or special characters.
"""

import re
import logging
import json

logger = logging.getLogger(__name__)

def truncate_json_fields(data, max_field_length=10000):
    """
    Recursively truncate string fields in JSON data while preserving the JSON structure.
    Only truncates individual string values, not the entire JSON object.
    """
    if isinstance(data, str):
        if len(data) > max_field_length:
            return data[:max_field_length] + "... [field truncated]"
        return data
    elif isinstance(data, dict):
        truncated_dict = {}
        for key, value in data.items():
            # Truncate the key if it's too long
            safe_key = str(key) if key is not None else "null_key"
            if len(safe_key) > 200:  # Reasonable limit for keys
                safe_key = safe_key[:200] + "..."
            truncated_dict[safe_key] = truncate_json_fields(value, max_field_length)
        return truncated_dict
    elif isinstance(data, list):
        return [truncate_json_fields(item, max_field_length) for item in data]
    else:
        return data

def sanitize_data(data):
    """
    Sanitize data for WebSocket transmission to prevent encoding errors.
    Handles strings, dictionaries, lists, and other data types recursively.
    """
    if isinstance(data, str):
        # If it's a string, try to parse as JSON first, then sanitize
        try:
            # Check if it's a JSON string
            parsed = json.loads(data)
            sanitized_parsed = sanitize_data(parsed)
            # Apply field-level truncation to the parsed object
            return truncate_json_fields(sanitized_parsed)
        except (json.JSONDecodeError, TypeError):
            # If not JSON, sanitize as regular string
            try:
                # Remove null bytes and other problematic control characters
                cleaned = data.replace('\x00', '').replace('\x08', '').replace('\x0c', '').replace('\x0b', '')
                
                # Handle escape sequences that might break JSON
                cleaned = cleaned.replace('\\\\', '\\').replace('\\"', '"')
                
                # Ensure valid UTF-8 encoding
                cleaned = cleaned.encode('utf-8', errors='replace').decode('utf-8')
                
                # Truncate individual string field instead of entire payload
                if len(cleaned) > 10000:  # 10KB limit per field
                    cleaned = cleaned[:10000] + "... [field truncated]"
                
                # Final validation - ensure the string can be JSON encoded
                try:
                    json.dumps({"test": cleaned})
                    return cleaned
                except (UnicodeEncodeError, json.JSONEncodeError):
                    # If still problematic, return safe fallback
                    return "[content sanitized for WebSocket transmission]"
                    
            except Exception:
                return "[invalid string - encoding error]"
    elif isinstance(data, dict):
        try:
            sanitized_dict = {}
            for k, v in data.items():
                # Ensure keys are valid strings
                safe_key = str(k) if k is not None else "null_key"
                safe_key = safe_key.replace('\x00', '').replace('\n', ' ').replace('\r', ' ')
                # Truncate key if too long
                if len(safe_key) > 200:
                    safe_key = safe_key[:200] + "..."
                sanitized_dict[safe_key] = sanitize_data(v)
            return sanitized_dict
        except Exception:
            return {"error": "dictionary sanitization failed"}
    elif isinstance(data, list):
        try:
            return [sanitize_data(item) for item in data]
        except Exception:
            return ["list sanitization failed"]
    elif data is None:
        return None
    elif isinstance(data, (bool, int, float)):
        return data
    else:
        # For other types, convert to string safely
        try:
            str_data = str(data)
            return sanitize_data(str_data)  # Recursively sanitize the string representation
        except Exception:
            return "[object sanitization failed]"

def sanitize_terraform_output(output: str) -> str:
    """Sanitize terraform output to prevent WebSocket encoding errors."""
    if not output:
        return output
    
    try:
        # Remove ANSI color codes
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        cleaned = ansi_escape.sub('', output)
        
        # Remove null bytes and other problematic characters
        cleaned = cleaned.replace('\x00', '')
        
        # Ensure valid UTF-8
        cleaned = cleaned.encode('utf-8', errors='replace').decode('utf-8')
        
        # Truncate if too long (prevent massive outputs from breaking WebSocket)
        if len(cleaned) > 10000:  # 10KB limit for tool outputs
            cleaned = cleaned[:10000] + "\n... [output truncated for WebSocket transmission]"
        
        return cleaned
    except Exception as e:
        logger.warning(f"Failed to sanitize terraform output: {e}")
        return "[terraform output - encoding error during sanitization]"

def _is_ovh_debug_line(line: str) -> bool:
    """Check if a line is part of OVH CLI debug output."""
    line_lower = line.lower().strip()
    # OVH CLI outputs timestamps like "2025/12/09 21:42:06" followed by debug info
    if re.match(r'^\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}', line):
        return True
    # JSON structure lines from debug output
    if line_lower in ['{', '}', '},']:
        return True
    if line_lower.startswith('"') and ':' in line_lower:
        # JSON field lines like "billingPeriod": "hourly"
        json_fields = ['billingperiod', 'bootfrom', 'imageid', 'flavor', 'network', 'public', 'private', 'name', 'id']
        if any(f'"{field}"' in line_lower for field in json_fields):
            return True
    if 'final parameters:' in line_lower:
        return True
    return False


def filter_error_messages(stderr_output: str) -> str:
    """Filter stderr to extract only actual error messages, excluding warnings."""
    if not stderr_output:
        return stderr_output
    
    lines = stderr_output.strip().split('\n')
    error_lines = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        # Skip OVH CLI debug output lines
        if _is_ovh_debug_line(line):
            continue
            
        # Include ERROR messages
        if line.startswith('ERROR:') or 'ERROR:' in line:
            error_lines.append(line)
        # Include FATAL messages
        elif line.startswith('FATAL:') or 'FATAL:' in line:
            error_lines.append(line)
        # Include exception messages
        elif any(keyword in line.lower() for keyword in ['exception:', 'traceback', 'failed:', 'invalid value']):
            error_lines.append(line)
        # Skip WARNING messages and informational messages
        elif (line.startswith('WARNING:') or 
              'WARNING:' in line or 
              line.startswith('As of Cloud SDK') or
              line.startswith('You can disable') or
              line.startswith('To learn more about') or
              'is no longer supported' in line or
              'will be deprecated' in line or
              'All API calls will be executed as' in line or
              'service account impersonation' in line):
            continue
        # Include other potential error indicators
        elif any(keyword in line.lower() for keyword in ['error', 'fail', 'denied', 'not found', 'invalid', 'required']):
            error_lines.append(line)
    
    # If no specific error lines found, return the last few lines of stderr
    # as they often contain the actual error
    if not error_lines and lines:
        # Take last 3 non-warning, non-debug lines
        for line in reversed(lines):
            line = line.strip()
            if (line and 
                not line.startswith('WARNING:') and 
                'WARNING:' not in line and
                not line.startswith('As of Cloud SDK') and
                not line.startswith('You can disable') and
                not _is_ovh_debug_line(line)):
                error_lines.insert(0, line)
                if len(error_lines) >= 3:
                    break
    
    return '\n'.join(error_lines) if error_lines else stderr_output


def sanitize_command_output(output: str, max_length: int = 50000) -> str:
    """Sanitize command output to prevent WebSocket encoding errors.
    
    Args:
        output: Raw command output string
        max_length: Maximum length before truncation (default 50KB)
    """
    if not output:
        return output
    
    try:
        # Remove ANSI color codes
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        cleaned = ansi_escape.sub('', output)
        
        # Remove null bytes and other problematic characters
        cleaned = cleaned.replace('\x00', '')
        
        # Ensure valid UTF-8
        cleaned = cleaned.encode('utf-8', errors='replace').decode('utf-8')
        
        # Truncate very large outputs to prevent context overflow
        if len(cleaned) > max_length:
            logger.warning(f"Truncating large command output from {len(cleaned)} to {max_length} bytes")
            cleaned = cleaned[:max_length] + f"\n\n... [output truncated from {len(cleaned)} bytes to {max_length} bytes]"
        
        return cleaned
    except Exception as e:
        logger.warning(f"Failed to sanitize command output: {e}")
        return "[command output - encoding error during sanitization]" 