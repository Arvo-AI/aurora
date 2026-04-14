"""
Skill file loader — parses YAML frontmatter and markdown body from .md files.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# Regex to split YAML frontmatter (between --- delimiters) from body
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)", re.DOTALL)


@dataclass
class SkillMetadata:
    """Parsed frontmatter from a skill file."""
    id: str
    name: str
    category: str
    connection_check: Dict[str, str] = field(default_factory=dict)
    tools: List[str] = field(default_factory=list)
    index: str = ""
    rca_priority: int = 99
    file_path: str = ""


@dataclass
class SkillLoadResult:
    """Result of loading a skill's content."""
    skill_id: str
    name: str
    content: str
    token_estimate: int
    tools: List[str]
    is_connected: bool


def parse_skill_file(file_path: str) -> tuple[Optional[SkillMetadata], str]:
    """
    Parse a skill markdown file into metadata + body.

    Returns (metadata, body). metadata is None if parsing fails.
    """
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logger.error(f"Failed to read skill file {file_path}: {e}")
        return None, ""

    match = _FRONTMATTER_RE.match(raw)
    if not match:
        logger.warning(f"Skill file {file_path} has no valid YAML frontmatter")
        return None, raw.strip()

    frontmatter_str, body = match.group(1), match.group(2).strip()

    try:
        fm: Dict[str, Any] = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML in {file_path}: {e}")
        return None, body

    skill_id = fm.get("id")
    if not skill_id:
        logger.warning(f"Skill file {file_path} missing required 'id' field")
        return None, body

    metadata = SkillMetadata(
        id=skill_id,
        name=fm.get("name", skill_id),
        category=fm.get("category", "unknown"),
        connection_check=fm.get("connection_check", {}),
        tools=fm.get("tools", []),
        index=fm.get("index", ""),
        rca_priority=fm.get("rca_priority", 99),
        file_path=file_path,
    )

    return metadata, body


def resolve_template(body: str, context: Dict[str, Any]) -> str:
    """
    Replace {variable} placeholders in the body with values from context.

    Only replaces known keys — unknown placeholders are left as-is.
    """
    for key, value in context.items():
        body = body.replace(f"{{{key}}}", str(value))
    return body


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars per token for English text)."""
    return len(text) // 4


def load_core_prompt(directory: str, segments: Optional[List[str]] = None) -> str:
    """
    Load and concatenate core prompt markdown files from directory.

    If segments is given, only those filenames (without .md) are loaded
    in the specified order. Otherwise all .md files are loaded in sorted order.
    """
    if not os.path.isdir(directory):
        logger.warning(f"Core prompt directory not found: {directory}")
        return ""

    if segments:
        paths = []
        for name in segments:
            path = os.path.join(directory, f"{name}.md")
            if os.path.isfile(path):
                paths.append(path)
            else:
                logger.warning(f"Core prompt segment not found: {path}")
    else:
        paths = sorted(
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.endswith(".md")
        )

    parts: List[str] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                parts.append(content)
        except OSError as e:
            logger.error(f"Failed to read core prompt file {path}: {e}")

    return "\n\n".join(parts)


def discover_skill_files(directory: str) -> List[str]:
    """
    Find skill files following the LangChain deep agents convention:
    each skill is a subdirectory containing a SKILL.md file.

    Falls back to flat .md files for the rca/ directory.
    """
    if not os.path.isdir(directory):
        return []

    paths: List[str] = []

    for entry in sorted(os.listdir(directory)):
        full = os.path.join(directory, entry)

        # LangChain convention: subdirectory with SKILL.md
        skill_md = os.path.join(full, "SKILL.md")
        if os.path.isdir(full) and os.path.isfile(skill_md):
            paths.append(skill_md)
        # Fallback: flat .md files (for rca/ directory)
        elif os.path.isfile(full) and entry.endswith(".md"):
            paths.append(full)

    return paths
