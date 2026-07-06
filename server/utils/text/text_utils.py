"""Text processing utilities for cleaning and formatting text content."""

import re

_TRUNCATION_SUFFIX = "...[truncated]"


def truncate(value, limit: int, suffix: str = _TRUNCATION_SUFFIX) -> str:
    """Coerce ``value`` to text and cap it at ``limit`` characters.

    Returns "" for ``None``, leaves short values untouched, and appends
    ``suffix`` only when content was actually cut. Consolidates the many
    copy-pasted ``_truncate``/``_excerpt`` helpers across routes and agent tools.
    """
    if value is None:
        return ""
    s = value if isinstance(value, str) else str(value)
    return s if len(s) <= limit else s[:limit] + suffix


def clean_markdown(text: str) -> str:
    """Strip markdown formatting from text for clean thought display.
    
    Removes:
    - Headers (###, ##, etc.)
    - Bold/italic (**text**, *text*)
    - Strikethrough (~~text~~)
    - Links ([text](url))
    - Bullet lists (-, *, +)
    - Numbered lists (1., 2., etc.)
    - Block quotes (>)
    - Inline code (`code`)
    
    Args:
        text: The markdown text to clean
        
    Returns:
        Plain text without markdown formatting
    """
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)  # Headers
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)  # Bold
    text = re.sub(r'\*([^*]+)\*', r'\1', text)  # Italic
    text = re.sub(r'~~([^~]+)~~', r'\1', text)  # Strikethrough
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)  # Links
    text = re.sub(r'^[-*+]\s+', '', text, flags=re.MULTILINE)  # Bullet lists
    text = re.sub(r'^\d+\.\s+', '', text, flags=re.MULTILINE)  # Numbered lists
    text = re.sub(r'^>\s+', '', text, flags=re.MULTILINE)  # Block quotes
    text = re.sub(r'`([^`]+)`', r'\1', text)  # Inline code
    return text.strip()
