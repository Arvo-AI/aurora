# Previously on chat.backend.agent.tools.could_tools.py

import contextvars
import logging
import threading
from typing import List, Optional, Any, Dict, Union, Tuple
from datetime import datetime

_context = threading.local()
_user_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("user_id", default=None)
_session_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("session_id", default=None)
_provider_pref_var: contextvars.ContextVar[Optional[List[str]]] = contextvars.ContextVar("provider_pref", default=None)
_selected_project_id_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("selected_project_id", default=None)
_state_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("state", default=None)
_websocket_sender_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("ws_sender", default=None)
_event_loop_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("event_loop", default=None)
_tool_capture_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("tool_capture", default=None)
_workflow_var: contextvars.ContextVar[Optional[Any]] = contextvars.ContextVar("workflow", default=None)
_mode_var: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("mode", default=None)

def _set_ctx(attr: str, value: Any):
    setattr(_context, attr, value)
    if attr == "user_id":
        _user_id_var.set(value)
    elif attr == "session_id":
        _session_id_var.set(value)
    elif attr == "provider_preference":
        _provider_pref_var.set(value)
    elif attr == "selected_project_id":
        _selected_project_id_var.set(value)
    elif attr == "state":
        _state_var.set(value)
    elif attr == "websocket_sender":
        _websocket_sender_var.set(value)
    elif attr == "event_loop":
        _event_loop_var.set(value)
    elif attr == "tool_capture":
        _tool_capture_var.set(value)
    elif attr == "workflow":
        _workflow_var.set(value)
    elif attr == "mode":
        _mode_var.set(value)

def get_provider_preference() -> Optional[List[str]]:
    """Get the provider preference from contextvars first, then thread-local."""
    logging.debug("Getting provider preference from contextvars or thread-local")
    return _provider_pref_var.get() or getattr(_context, 'provider_preference', None)

def get_user_context() -> Dict[str, Optional[str]]:
    """Retrieve user context with user_id and session_id from contextvars first, then thread-local.
    
    Returns:
        Dict with keys: 'user_id', 'session_id'
    """
    logging.debug("Getting user context (user_id + session_id) from contextvars or thread-local")
    user_id = _user_id_var.get() or getattr(_context, 'user_id', None)
    session_id = _session_id_var.get() or getattr(_context, 'session_id', None)
    return {
        'user_id': user_id,
        'session_id': session_id
    }

def set_user_context(
    user_id: str,
    session_id: Optional[str] = None,
    provider_preference: Optional[Union[str, List[str]]] = None,
    selected_project_id: Optional[str] = None,
    state: Optional[Any] = None,
    workflow: Optional[Any] = None,
    mode: Optional[str] = None,
):
    """Set the user context in both thread-local and async contextvars.
    
    Args:
        user_id: User ID
        session_id: Session ID (required for terminal pod isolation)
        provider_preference: Provider preference (string or list)
        selected_project_id: Selected project ID
        state: State object
        workflow: Workflow object
    """
    _set_ctx("user_id", user_id)
    if session_id:
        _set_ctx("session_id", session_id)
    derived_mode = mode or (getattr(state, "mode", None) if state else None)
    if derived_mode:
        _set_ctx("mode", derived_mode)

    if provider_preference is not None:
        # Normalize to list (including empty list to explicitly clear prior preference)
        pref_list = [provider_preference] if isinstance(provider_preference, str) else provider_preference
        _set_ctx("provider_preference", pref_list)
    if selected_project_id:
        _set_ctx("selected_project_id", selected_project_id)
    if state:
        _set_ctx("state", state)
    if workflow:
        _set_ctx("workflow", workflow)


def get_mode_from_context() -> Optional[str]:
    """Return the chat mode stored in the execution context."""
    return _mode_var.get() or getattr(_context, 'mode', None)

def get_state_context():
    """Return state from contextvars/thread-local."""
    return _state_var.get() or getattr(_context, 'state', None)

def get_workflow_context():
    """Return workflow instance from contextvars/thread-local."""
    return _workflow_var.get() or getattr(_context, 'workflow', None)

def set_websocket_context(websocket_sender, event_loop):
    _set_ctx("websocket_sender", websocket_sender)
    _set_ctx("event_loop", event_loop)

def get_websocket_context():
    return (
        _websocket_sender_var.get() or getattr(_context, 'websocket_sender', None),
        _event_loop_var.get() or getattr(_context, 'event_loop', None)
    )

def get_selected_project_id() -> Optional[str]:
    """Get the selected project ID from contextvars first, then thread-local."""
    return _selected_project_id_var.get() or getattr(_context, 'selected_project_id', None)

def set_selected_project_id(project_id: str):
    """Set the selected project ID in thread-local context."""
    _set_ctx("selected_project_id", project_id)

def set_tool_capture(tool_capture):
    """Set the tool capture in thread-local context."""
    _set_ctx("tool_capture", tool_capture)

def get_tool_capture():
    """Get the tool capture from thread-local context."""
    return _tool_capture_var.get() or getattr(_context, 'tool_capture', None)

def set_provider_preference(provider: Union[str, List[str]]):
    """Set the provider preference in thread-local context. Can be a single provider or list of providers."""
    valid_providers = ['gcp', 'azure', 'aws', 'ovh', 'scaleway', 'tailscale']

    if isinstance(provider, str):
        if provider not in valid_providers:
            raise ValueError(f"Invalid provider: {provider}. Must be one of {valid_providers}")
        _set_ctx("provider_preference", [provider])
    else:
        # Handle list of providers
        for p in provider:
            if p not in valid_providers:
                raise ValueError(f"Invalid provider: {p}. Must be one of {valid_providers}")
        _set_ctx("provider_preference", provider)
