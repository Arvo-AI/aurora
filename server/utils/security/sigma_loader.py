"""Sigma YAML → regex transpiler for the L2 signature matcher.

Loads vendored SigmaHQ rules from ``sigma_rules/`` at import time and
converts them to compiled regex patterns compatible with
``signature_match.SignatureVerdict``.

Only supports a narrow Sigma subset:
- product: linux, category: process_creation
- level: high or critical
- Detection fields: CommandLine, Image, OriginalFileName
- Modifiers: contains, endswith, startswith, re, contains|all
- Conditions: selection, all of selection_*, 1 of selection_*
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_SIGMA_DIR = Path(__file__).parent / "sigma_rules"

_SUPPRESSIONS: set = set()


def _load_suppressions() -> set:
    """Load suppressed rule IDs from env var and suppressions.txt."""
    suppressed: set = set()
    env = os.getenv("GUARDRAILS_SIGMA_SUPPRESS", "")
    if env:
        for sid in env.split(","):
            sid = sid.strip()
            if sid:
                suppressed.add(sid)
    supp_file = _SIGMA_DIR / "suppressions.txt"
    if supp_file.exists():
        for line in supp_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                suppressed.add(line)
    return suppressed


def _extract_mitre_technique(tags: list) -> str:
    """Extract the first MITRE ATT&CK technique ID from Sigma tags."""
    for tag in (tags or []):
        tag_str = str(tag)
        if tag_str.startswith("attack.t") and len(tag_str) > 8:
            return tag_str.replace("attack.", "").upper()
    return ""


_SUPPORTED_FIELDS = {"commandline", "image", "originalfilename"}


def _escape_literal(s: str) -> str:
    return re.escape(s)


def _image_pattern(lit: str, modifiers: List[str]) -> str:
    if "endswith" in modifiers:
        return f"^\\S*{lit}(?:\\s|$)"
    if "startswith" in modifiers:
        return f"^{lit}"
    if "contains" in modifiers:
        return f"^\\S*{lit}\\S*(?:\\s|$)"
    return f"^{lit}(?:\\s|$)"


def _cmdline_pattern(lit: str, modifiers: List[str]) -> str:
    if "endswith" in modifiers:
        return f".*{lit}$"
    if "startswith" in modifiers:
        return f"^{lit}"
    if "contains" in modifiers:
        return lit
    return f"^{lit}$"


def _field_to_regex(field_spec: str, values: Any) -> Optional[str]:
    """Convert a single Sigma field|modifier spec + values to a regex pattern."""
    parts = field_spec.lower().split("|")
    field = parts[0]

    if field not in _SUPPORTED_FIELDS:
        return None

    modifiers = parts[1:]

    if not isinstance(values, list):
        values = [values]

    if "re" in modifiers:
        return "|".join(str(v) for v in values)

    if "contains" in modifiers and "all" in modifiers:
        lookaheads = "".join(
            f"(?=.*{_escape_literal(str(v))})" for v in values
        )
        return lookaheads + ".*"

    build = _image_pattern if field == "image" else _cmdline_pattern
    alternatives = [build(_escape_literal(str(v)), modifiers) for v in values]
    return "|".join(alternatives) if alternatives else None


def _and_all(patterns) -> str:
    """Combine patterns with AND semantics using lookaheads."""
    items = list(patterns)
    if len(items) == 1:
        return items[0]
    return "".join(f"(?=.*(?:{p}))" for p in items) + ".*"


def _or_all(patterns) -> str:
    """Combine patterns with OR semantics."""
    return "|".join(f"(?:{p})" for p in patterns)


def _translate_selection(selection: Any) -> Optional[str]:
    """Translate one Sigma selection dict (or list of dicts) into a regex.

    Returns None if any field in the selection is unsupported — dropping AND
    conditions would silently widen the match and cause false positives.
    """
    if isinstance(selection, list):
        parts = []
        for item in selection:
            if isinstance(item, dict):
                p = _translate_selection(item)
                if p:
                    parts.append(p)
        return _or_all(parts) if parts else None

    if not isinstance(selection, dict):
        return None

    patterns = []
    has_unsupported = False
    for field_spec, values in selection.items():
        pat = _field_to_regex(field_spec, values)
        if pat:
            patterns.append(pat)
        else:
            has_unsupported = True

    if not patterns or has_unsupported:
        return None
    return _and_all(patterns)


def _extract_selections(detection: Dict[str, Any]) -> Dict[str, str]:
    selections = {}
    for key, val in detection.items():
        if key == "condition" or key.startswith("filter"):
            continue
        pat = _translate_selection(val)
        if pat:
            selections[key] = pat
    return selections


def _resolve_condition(cond_lower: str, selections: Dict[str, str]) -> Optional[str]:
    if cond_lower in ("selection", "all of selection*", "all of selection_*"):
        return _and_all(selections.values())

    if cond_lower in ("1 of selection_*", "1 of selection*"):
        return _or_all(selections.values())

    if "all of selection" in cond_lower and "not" not in cond_lower:
        sel_parts = [v for k, v in selections.items() if k.startswith("selection")]
        if sel_parts:
            return _and_all(sel_parts)

    if " and " in cond_lower and "not" not in cond_lower:
        referenced = [k for k in selections if k in cond_lower.replace("_", " ").replace("*", "")]
        if not referenced:
            referenced = list(selections.keys())
        parts = [selections[k] for k in referenced if k in selections]
        if parts:
            return _and_all(parts)

    if len(selections) == 1:
        return next(iter(selections.values()))

    return None


def _translate_rule(rule: Dict[str, Any]) -> Optional[str]:
    """Translate a full Sigma rule's detection block into a single regex."""
    detection = rule.get("detection", {})
    condition = detection.get("condition", "")
    selections = _extract_selections(detection)
    if not selections:
        return None
    return _resolve_condition(condition.lower().strip(), selections)


def load_sigma_rules() -> List[Tuple[re.Pattern, str, str, str]]:
    """Load and transpile all vendored Sigma rules.

    Returns a list of (compiled_pattern, technique, rule_id, description)
    tuples compatible with ``signature_match._RULES``.
    """
    global _SUPPRESSIONS
    _SUPPRESSIONS = _load_suppressions()

    if not _SIGMA_DIR.is_dir():
        logger.debug("Sigma rules directory not found: %s", _SIGMA_DIR)
        return []

    rules: List[Tuple[re.Pattern, str, str, str]] = []

    for yml_path in sorted(_SIGMA_DIR.glob("*.yml")):
        try:
            with open(yml_path) as f:
                rule = yaml.safe_load(f)
        except Exception:
            logger.warning("Failed to parse Sigma rule: %s", yml_path.name)
            continue

        if not isinstance(rule, dict):
            continue

        sigma_id = rule.get("id", "")
        if sigma_id in _SUPPRESSIONS:
            logger.debug("Suppressed Sigma rule: %s", sigma_id)
            continue

        title = rule.get("title", yml_path.stem)
        level = (rule.get("level") or "").lower()
        if level not in ("high", "critical"):
            continue

        technique = _extract_mitre_technique(rule.get("tags"))
        rule_id = f"sigma-{sigma_id[:8]}" if sigma_id else f"sigma-{yml_path.stem}"

        regex_str = _translate_rule(rule)
        if not regex_str:
            logger.debug("Could not translate Sigma rule: %s", yml_path.name)
            continue

        try:
            compiled = re.compile(regex_str, re.IGNORECASE)
        except re.error:
            logger.warning("Invalid regex from Sigma rule %s: %s", yml_path.name, regex_str[:100].replace("\n", "\\n"))
            continue

        rules.append((compiled, technique, rule_id, title))

    logger.info("Loaded %d Sigma rules from %s", len(rules), _SIGMA_DIR)
    return rules
