from typing import List, Any, Dict, Literal, Optional
from langchain_core.messages import AnyMessage
from pydantic import BaseModel, ConfigDict


class State(BaseModel):
    messages: List[AnyMessage] = []
    question: str
    refined_question: Optional[str] = None
    user_id: Optional[str] = None
    session_id: Optional[str] = None  # Add session ID for tracking chat sessions
    provider_preference: Optional[List[str]] = None  # Must be explicitly set, e.g. ["gcp", "aws", "azure"]
    selected_project_id: Optional[str] = None  # Selected project ID from frontend UI
    attachments: Optional[List[Dict[str, Any]]] = None  # File attachments
    model: Optional[str] = None  # Selected model from frontend
    mode: Optional[str] = None  # Chat mode: 'agent' or 'ask'
    is_background: bool = (
        False  # True for background chats (webhook-triggered, no user interaction)
    )
    rca_context: Optional[Dict[str, Any]] = (
        None  # RCA-specific context (source, providers) - used by prompt_builder
    )
    storage_chat_files: Optional[List[Dict[str, Any]]] = (
        None  # Files found in the chat's storage directory
    )
    placeholder_warning: bool = False  # Flag runtime placeholder detection
    last_tool_failure: Optional[Dict[str, Any]] = (
        None  # Most recent tool failure metadata
    )

    model_config = ConfigDict(arbitrary_types_allowed=True)
