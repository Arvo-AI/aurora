"""Text processing utilities for cleaning and formatting text content."""

import re


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
