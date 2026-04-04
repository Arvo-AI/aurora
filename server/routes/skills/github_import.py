"""GitHub import integration for discovering and fetching skills from public repos."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
RAW_CONTENT = "https://raw.githubusercontent.com"
SKILL_FILENAMES = {"SKILL.md", "skill.md"}
_GITHUB_HEADERS = {"Accept": "application/vnd.github.v3+json"}


@dataclass
class SkillPreview:
    name: str
    description: str
    path: str


@dataclass
class SkillImport:
    name: str
    description: str
    body: str
    tags: list
    providers: list
    references_data: Dict[str, str] = field(default_factory=dict)


def resolve_repo(url_or_shorthand: str) -> Tuple[str, str]:
    """Parse 'owner/repo', GitHub URL, or skills.sh URL -> (owner, repo)."""
    text = url_or_shorthand.strip().rstrip("/")

    m = re.match(r"https?://(?:www\.)?github\.com/([^/]+)/([^/]+)", text)
    if m:
        return m.group(1), m.group(2).replace(".git", "")

    m = re.match(r"https?://(?:www\.)?skills\.sh/([^/]+)/([^/]+)", text)
    if m:
        return m.group(1), m.group(2)

    m = re.match(r"https?://(?:www\.)?skillsmp\.com/([^/]+)/([^/]+)", text)
    if m:
        return m.group(1), m.group(2)

    m = re.match(r"^([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)$", text)
    if m:
        return m.group(1), m.group(2)

    raise ValueError(f"Cannot parse repository from: {url_or_shorthand}")


def _fetch_tree(owner: str, repo: str) -> List[dict]:
    """Fetch the full recursive git tree for a repo, trying main then master."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/main?recursive=1"
    resp = requests.get(url, timeout=15, headers=_GITHUB_HEADERS)
    if resp.status_code == 404:
        url = f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/master?recursive=1"
        resp = requests.get(url, timeout=15, headers=_GITHUB_HEADERS)
    if resp.status_code == 403:
        raise ValueError("GitHub API rate limit exceeded. Try again later or use a full URL.")
    resp.raise_for_status()
    return resp.json().get("tree", [])


def _fetch_raw(owner: str, repo: str, path: str, timeout: int = 10) -> Optional[str]:
    """Fetch a raw file from a repo, trying main then master. Returns None on 404."""
    raw_url = f"{RAW_CONTENT}/{owner}/{repo}/main/{path}"
    r = requests.get(raw_url, timeout=timeout)
    if r.status_code == 404:
        raw_url = f"{RAW_CONTENT}/{owner}/{repo}/master/{path}"
        r = requests.get(raw_url, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.text


def discover_skills(owner: str, repo: str) -> List[SkillPreview]:
    """Use GitHub API to find all SKILL.md files in repo."""
    tree = _fetch_tree(owner, repo)

    skill_paths = [
        entry["path"]
        for entry in tree
        if entry.get("type") == "blob"
        and (entry.get("path", "").rsplit("/", 1)[-1] in SKILL_FILENAMES)
    ]

    previews = []
    for path in skill_paths:
        try:
            content = _fetch_raw(owner, repo, path)
            if content is None:
                continue
            meta, _ = _split_frontmatter(content)
            name = meta.get("name", _name_from_path(path))
            description = meta.get("description", "")
            previews.append(SkillPreview(name=name, description=description, path=path))
        except Exception as e:
            logger.warning(f"[SkillImport] Failed to preview {path}: {e}")

    return previews


def fetch_skill(owner: str, repo: str, skill_path: str) -> SkillImport:
    """Fetch full SKILL.md body + references/ files for a specific skill."""
    clean_path = _sanitize_path(skill_path)

    content = _fetch_raw(owner, repo, clean_path, timeout=15)
    if content is None:
        raise FileNotFoundError(f"Skill file not found: {clean_path}")

    meta, body = _split_frontmatter(content)

    skill_dir = clean_path.rsplit("/", 1)[0] if "/" in clean_path else ""
    refs_dir = f"{skill_dir}/references" if skill_dir else "references"
    references_data = _fetch_references(owner, repo, refs_dir)

    return SkillImport(
        name=meta.get("name", _name_from_path(clean_path)),
        description=meta.get("description", ""),
        body=body,
        tags=meta.get("tags", []),
        providers=meta.get("providers", []),
        references_data=references_data,
    )


def _split_frontmatter(content: str) -> Tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}

    return meta, parts[2].strip()


def _name_from_path(path: str) -> str:
    parts = path.replace("\\", "/").split("/")
    for i, p in enumerate(parts):
        if p.lower() in SKILL_FILENAMES and i > 0:
            return parts[i - 1]
    return parts[0] if parts else "unnamed"


def _sanitize_path(path: str) -> str:
    clean = path.replace("\\", "/")
    parts = [p for p in clean.split("/") if p and p != ".."]
    return "/".join(parts)


def _fetch_references(owner: str, repo: str, refs_dir: str) -> Dict[str, str]:
    if not refs_dir:
        return {}

    try:
        tree = _fetch_tree(owner, repo)
    except Exception:
        return {}

    prefix = refs_dir.rstrip("/") + "/"
    refs = {}

    for entry in tree:
        if entry.get("type") != "blob":
            continue
        p = entry.get("path", "")
        if not p.startswith(prefix):
            continue
        filename = p[len(prefix):]
        if "/" in filename:
            continue
        try:
            content = _fetch_raw(owner, repo, p)
            if content is not None:
                refs[filename] = content
        except Exception as e:
            logger.warning(f"[SkillImport] Failed to fetch reference {p}: {e}")

    return refs
