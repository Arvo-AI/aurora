"""Sigma YAML -> regex transpiler for the L2 signature matcher.

Loads vendored SigmaHQ rules from ``sigma_rules/`` when
``config.sigma_enabled`` is True, converting them to compiled regex
patterns compatible with ``signature_match.SignatureVerdict``.

Only supports a narrow Sigma subset:
- product: linux, category: process_creation
- level: high or critical
- Detection fields: CommandLine, Image
- Modifiers: contains, endswith, startswith, re, contains|all
- Conditions: selection, all of selection_*, 1 of selection_*

Sigma ``Image|endswith: '/foo'`` selectors assume the structured Image
field from EDR telemetry (always an absolute path). We apply them to
raw command strings, so for a curated LOLBin basename allowlist
(``awk``, ``perl``, ``ncat``, ``vim``, ...) we also accept the bare
basename at the start of the command line. See ``_BARE_BASENAME_ALLOWED``.
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_SIGMA_DIR = Path(__file__).parent / "sigma_rules"

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
        for line in supp_file.read_text(encoding="utf-8").splitlines():
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


_SUPPORTED_FIELDS = {"commandline", "image"}


# Sigma rules express ``Image|endswith: '/ncat'`` assuming the structured
# ``Image`` field (always an absolute path) from EDR telemetry. We match
# against raw command strings, where the binary is often invoked by bare
# basename. For a hand-picked set of uncommon LOLBins the payload-anchored
# rest of the rule (e.g. ``BEGIN {system``, ``-e`` + ``Socket``) makes
# adding a bare-basename alternative safe. Common coreutils like rm/cp/mv
# are intentionally NOT in this list: their Sigma rules would otherwise
# fire on everyday SRE commands.
_BARE_BASENAME_ALLOWED = frozenset({
    "awk", "gawk", "mawk", "nawk",
    "perl", "php", "python", "python2", "python3",
    "vim", "rvim", "vimdiff",
    "nc", "ncat",
})


def _escape_literal(s: str) -> str:
    return re.escape(s)


def _image_pattern(lit: str, modifiers: List[str]) -> str:
    if "endswith" in modifiers:
        pat = f"^\\S*{lit}(?:\\s|$)"
        # If the literal looks like ``/foo`` and ``foo`` is a known
        # LOLBin basename, also match the bare-basename invocation so
        # Sigma rules written against EDR telemetry still catch commands
        # users type without absolute paths. Gate on the allowlist so
        # permissive rules (``/rm``, ``/cp``, ...) keep requiring an
        # absolute path and don't FP on routine commands.
        if lit.startswith("/"):
            basename = lit[1:]
            if basename in _BARE_BASENAME_ALLOWED:
                pat = f"(?:{pat}|^{basename}(?:\\s|$))"
        return pat
    if "startswith" in modifiers:
        return f"^{lit}\\S*(?:\\s|$)"
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


def _and_all(patterns: Iterable[str]) -> str:
    """Combine patterns with AND semantics using lookaheads."""
    items = list(patterns)
    if len(items) == 1:
        return items[0]
    return "".join(f"(?=.*(?:{p}))" for p in items) + ".*"


def _or_all(patterns: Iterable[str]) -> str:
    """Combine patterns with OR semantics."""
    return "|".join(f"(?:{p})" for p in patterns)


def _translate_selection_dict(selection: dict) -> Optional[str]:
    """Translate a single Sigma selection dict into a regex.

    Returns None if any field is unsupported to avoid false positives.
    """
    patterns = []
    for field_spec, values in selection.items():
        pat = _field_to_regex(field_spec, values)
        if pat:
            patterns.append(pat)
        else:
            return None
    return _and_all(patterns) if patterns else None


def _translate_selection(selection: Any) -> Optional[str]:
    """Translate one Sigma selection (dict or list of dicts) into a regex."""
    if isinstance(selection, list):
        parts = [_translate_selection(item) for item in selection if isinstance(item, dict)]
        parts = [p for p in parts if p]
        return _or_all(parts) if parts else None

    if isinstance(selection, dict):
        return _translate_selection_dict(selection)

    return None


def _extract_selections(detection: Dict[str, Any]) -> Dict[str, str]:
    selections = {}
    for key, val in detection.items():
        if key == "condition" or key.startswith("filter"):
            continue
        pat = _translate_selection(val)
        if pat:
            selections[key] = pat
    return selections


def _resolve_token(token: str, selections: Dict[str, str]) -> Optional[str]:
    """Resolve a single condition token (name, '1 of X*', 'all of X*')."""
    if token in selections:
        return selections[token]

    for quantifier, combiner in [("1 of ", _or_all), ("all of ", _and_all)]:
        if token.startswith(quantifier):
            prefix = token[len(quantifier):].replace("*", "").replace("_", "")
            matched = [v for k, v in selections.items()
                       if k.startswith(prefix) or k.replace("_", "").startswith(prefix)]
            return combiner(matched) if matched else None

    return None


def _resolve_compound(cond_lower: str, selections: Dict[str, str]) -> Optional[str]:
    """Resolve compound 'A and B' conditions by parsing each positive token.

    Negative tokens ('not X') are stripped — we don't support filter
    exclusions, so ignoring them errs on the side of over-detection.
    """
    tokens = [t.strip() for t in cond_lower.split(" and ")]
    positive = [t for t in tokens if not t.startswith("not ")]
    if not positive:
        return None
    parts = []
    for token in positive:
        resolved = _resolve_token(token, selections)
        if resolved is None:
            return None
        parts.append(resolved)
    return _and_all(parts) if parts else None


def _resolve_condition(cond_lower: str, selections: Dict[str, str]) -> Optional[str]:
    if cond_lower in ("selection", "all of selection*", "all of selection_*"):
        return _and_all(selections.values())

    if cond_lower in ("1 of selection_*", "1 of selection*"):
        return _or_all(selections.values())

    if "all of selection" in cond_lower and "not" not in cond_lower:
        sel_parts = [v for k, v in selections.items() if k.startswith("selection")]
        if sel_parts:
            return _and_all(sel_parts)

    if " and " in cond_lower:
        return _resolve_compound(cond_lower, selections)

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


_SigmaRule = Tuple[re.Pattern, str, str, str]


def _process_sigma_file(yml_path: Path, suppressions: set) -> Optional[_SigmaRule]:
    """Parse and transpile a single Sigma YAML file. Returns None on skip."""
    try:
        with open(yml_path, encoding="utf-8") as f:
            rule = yaml.safe_load(f)
    except Exception:
        logger.warning("Failed to parse Sigma rule: %s", yml_path.name, exc_info=True)
        return None

    if not isinstance(rule, dict):
        return None

    sigma_id = rule.get("id", "")
    if sigma_id in suppressions:
        logger.debug("Suppressed Sigma rule: %s", sigma_id)
        return None

    level = (rule.get("level") or "").lower()
    if level not in ("high", "critical"):
        return None

    regex_str = _translate_rule(rule)
    if not regex_str:
        logger.warning("Could not translate Sigma rule: %s", yml_path.name)
        return None

    try:
        compiled = re.compile(regex_str, re.IGNORECASE)
    except re.error:
        logger.warning("Invalid regex from Sigma rule %s: %s", yml_path.name, regex_str[:100].replace("\n", "\\n"))
        return None

    technique = _extract_mitre_technique(rule.get("tags"))
    title = rule.get("title", yml_path.stem)
    rule_id = f"sigma-{sigma_id[:8]}" if sigma_id else f"sigma-{yml_path.stem}"
    return compiled, technique, rule_id, title


def load_sigma_rules() -> List[_SigmaRule]:
    """Load and transpile all vendored Sigma rules.

    Returns a list of (compiled_pattern, technique, rule_id, description)
    tuples compatible with ``signature_match._RULES``.
    """
    suppressions = _load_suppressions()

    if not _SIGMA_DIR.is_dir():
        logger.debug("Sigma rules directory not found: %s", _SIGMA_DIR)
        return []

    rules: List[_SigmaRule] = []
    for yml_path in sorted(_SIGMA_DIR.glob("*.yml")):
        result = _process_sigma_file(yml_path, suppressions)
        if result:
            rules.append(result)

    logger.info("Loaded %d Sigma rules from %s", len(rules), _SIGMA_DIR)
    return rules
