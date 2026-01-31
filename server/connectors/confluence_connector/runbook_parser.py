"""Parse Confluence runbooks into structured steps."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from bs4 import BeautifulSoup, NavigableString, Tag

SECTION_TITLES = {
    "overview": "Overview",
    "symptoms": "Symptoms",
    "impact": "Impact",
    "steps": "Steps",
    "rollback": "Rollback",
    "verification": "Verification",
}

RISKY_KEYWORDS = {
    "delete",
    "drop",
    "restart",
    "reboot",
    "terminate",
    "shutdown",
    "stop",
    "kill",
    "rm ",
    "rollback",
    "failover",
    "scale down",
    "scale up",
    "apply",
    "deploy",
    "upgrade",
    "downgrade",
    "truncate",
    "purge",
    "revoke",
    "disable",
}

SAFE_COMMAND_HINTS = {
    "get ",
    "describe ",
    "list ",
    "status",
    "logs",
    "top",
    "curl ",
    "wget ",
    "ping ",
    "dig ",
    "nslookup ",
    "cat ",
    "ls ",
    "head ",
    "tail ",
    "grep ",
}

MANUAL_KEYWORDS = {
    "click",
    "open",
    "navigate",
    "login",
    "log in",
    "select",
    "choose",
    "dashboard",
    "console",
    "browser",
    "ui",
    "page",
    "menu",
    "button",
}

EVIDENCE_KEYWORDS = {
    "verify",
    "confirm",
    "validate",
    "check",
    "ensure",
}


def confluence_storage_to_markdown(storage_html: str) -> str:
    """Convert Confluence storage HTML into lightweight Markdown."""
    soup = BeautifulSoup(storage_html or "", "html.parser")

    _replace_confluence_code_macros(soup)
    _replace_confluence_task_lists(soup)

    lines: List[str] = []

    def append_line(line: str = "") -> None:
        lines.append(line)

    def render_element(element: Any, indent: int = 0) -> None:
        if isinstance(element, NavigableString):
            text = str(element).strip()
            if text:
                append_line(text)
            return

        if not isinstance(element, Tag):
            return

        tag_name = element.name.lower() if element.name else ""
        if tag_name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag_name[1])
            heading = element.get_text(" ", strip=True)
            if heading:
                append_line(f"{'#' * level} {heading}")
                append_line("")
            return

        if tag_name == "p":
            text = element.get_text(" ", strip=True)
            if text:
                append_line(text)
                append_line("")
            return

        if tag_name == "pre":
            code_text = element.get_text("\n", strip=False).rstrip()
            append_line("```")
            if code_text:
                append_line(code_text)
            append_line("```")
            append_line("")
            return

        if tag_name in {"ul", "ol"}:
            ordered = tag_name == "ol"
            items = element.find_all("li", recursive=False)
            for idx, li in enumerate(items, start=1):
                prefix = f"{idx}." if ordered else "-"
                item_text = li.get_text(" ", strip=True)
                append_line(f"{'  ' * indent}{prefix} {item_text}")
                for child_list in li.find_all(["ul", "ol"], recursive=False):
                    render_element(child_list, indent + 1)
            append_line("")
            return

        if tag_name == "code":
            code_text = element.get_text(" ", strip=True)
            if code_text:
                append_line(f"`{code_text}`")
            return

        for child in element.children:
            render_element(child, indent)

    for child in soup.contents:
        render_element(child)

    return _collapse_blank_lines(lines)


def extract_sections(markdown: str) -> Dict[str, str]:
    """Extract common runbook sections from Markdown."""
    sections: Dict[str, str] = {}
    current_key: Optional[str] = None
    buffer: List[str] = []

    for line in markdown.splitlines():
        match = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if match:
            if current_key and buffer:
                sections[current_key] = "\n".join(buffer).strip()
            heading_text = match.group(1).strip()
            canonical = SECTION_TITLES.get(heading_text.lower())
            current_key = canonical or heading_text
            buffer = []
            continue
        if current_key is not None:
            buffer.append(line)

    if current_key and buffer:
        sections[current_key] = "\n".join(buffer).strip()

    return {key: value for key, value in sections.items() if value}


def parse_confluence_runbook(storage_html: str) -> Dict[str, Any]:
    """Parse Confluence storage HTML into markdown, sections, and classified steps."""
    markdown = confluence_storage_to_markdown(storage_html)
    sections = extract_sections(markdown)
    steps = _extract_steps_from_html(storage_html)
    return {
        "markdown": markdown,
        "sections": sections,
        "steps": steps,
    }


def _replace_confluence_code_macros(soup: BeautifulSoup) -> None:
    for macro in soup.find_all(lambda tag: tag.name and tag.name.endswith("structured-macro")):
        if macro.get("ac:name") != "code":
            continue
        plain_text = macro.find(lambda tag: tag.name and tag.name.endswith("plain-text-body"))
        code_text = plain_text.get_text("\n", strip=False) if plain_text else ""
        pre_tag = soup.new_tag("pre")
        pre_tag.string = code_text
        macro.replace_with(pre_tag)


def _replace_confluence_task_lists(soup: BeautifulSoup) -> None:
    for task_list in soup.find_all(lambda tag: tag.name and tag.name.endswith("task-list")):
        ul_tag = soup.new_tag("ul")
        tasks = task_list.find_all(lambda tag: tag.name and tag.name.endswith("task"), recursive=False)
        for task in tasks:
            body = task.find(lambda tag: tag.name and tag.name.endswith("task-body"))
            if not body:
                continue
            li_tag = soup.new_tag("li")
            li_tag.string = body.get_text(" ", strip=True)
            ul_tag.append(li_tag)
        task_list.replace_with(ul_tag)


def _extract_steps_from_html(storage_html: str) -> List[Dict[str, Any]]:
    soup = BeautifulSoup(storage_html or "", "html.parser")
    steps: List[Dict[str, Any]] = []
    step_index = 1

    _replace_confluence_code_macros(soup)
    _replace_confluence_task_lists(soup)

    for list_tag in soup.find_all(["ol", "ul"]):
        for li in list_tag.find_all("li", recursive=False):
            step_text = li.get_text(" ", strip=True)
            if not step_text:
                continue
            command = _extract_command_from_node(li)
            steps.append(_build_step(step_index, step_text, command))
            step_index += 1

    return steps


def _extract_command_from_node(node: Tag) -> Optional[str]:
    code_blocks = node.find_all("pre")
    if code_blocks:
        blocks = [block.get_text("\n", strip=True) for block in code_blocks if block.get_text(strip=True)]
        return "\n".join(blocks) if blocks else None

    inline_code = node.find_all("code")
    if inline_code:
        inline_text = inline_code[0].get_text(" ", strip=True)
        return inline_text or None

    text = node.get_text(" ", strip=True)
    for hint in ("kubectl ", "gcloud ", "aws ", "terraform ", "docker ", "helm ", "psql ", "systemctl "):
        if hint in text.lower():
            return text

    return None


def _build_step(step_index: int, text: str, command: Optional[str]) -> Dict[str, Any]:
    classification = _classify_step(text, command)
    return {
        "step_id": f"step_{step_index}",
        "text": text,
        "command": command,
        "tool_hint": _infer_tool_hint(command or text),
        "classification": classification,
        "required_approval": classification == "risky_automated",
        "evidence_required": _requires_evidence(text),
    }


def _classify_step(text: str, command: Optional[str]) -> str:
    lower_text = text.lower()

    if _contains_any(lower_text, MANUAL_KEYWORDS) and not command:
        return "manual"

    if command:
        command_lower = command.lower()
        if _contains_any(command_lower, RISKY_KEYWORDS) or _contains_any(lower_text, RISKY_KEYWORDS):
            return "risky_automated"
        if any(command_lower.startswith(prefix) for prefix in SAFE_COMMAND_HINTS) or _contains_any(
            command_lower, SAFE_COMMAND_HINTS
        ):
            return "safe_automated"
        return "manual"

    if _contains_any(lower_text, RISKY_KEYWORDS):
        return "risky_automated"

    return "manual"


def _infer_tool_hint(text: str) -> Optional[str]:
    lower_text = text.lower()
    if "kubectl" in lower_text:
        return "kubectl"
    if "gcloud" in lower_text:
        return "gcloud"
    if re.search(r"\baws\b", lower_text):
        return "aws"
    if "terraform" in lower_text:
        return "terraform"
    if "docker" in lower_text:
        return "docker"
    if "helm" in lower_text:
        return "helm"
    if "psql" in lower_text:
        return "psql"
    if "systemctl" in lower_text:
        return "systemctl"
    return None


def _requires_evidence(text: str) -> bool:
    return _contains_any(text.lower(), EVIDENCE_KEYWORDS)


def _contains_any(text: str, keywords: Any) -> bool:
    return any(keyword in text for keyword in keywords)


def _collapse_blank_lines(lines: List[str]) -> str:
    cleaned: List[str] = []
    previous_blank = False
    for line in lines:
        if not line.strip():
            if not previous_blank:
                cleaned.append("")
            previous_blank = True
        else:
            cleaned.append(line)
            previous_blank = False
    return "\n".join(cleaned).strip()
