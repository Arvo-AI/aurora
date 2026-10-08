"""KCP manifest parser -- reads and validates ``knowledge.yaml`` files.

Handles both KCP 0.6+ (``kcp_version`` at top level) and 0.20+
(``version`` at top level with an ``entity`` block) manifests.
Resolves each unit's ``path`` relative to the manifest directory so
content files can be read during ingestion.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class TemporalMetadata:
    """Temporal validity window for a knowledge unit."""

    valid_from: Optional[str] = None
    valid_until: Optional[str] = None
    review_by: Optional[str] = None


@dataclass
class KCPUnit:
    """One knowledge unit from a KCP manifest."""

    id: str
    path: str
    intent: str = ""
    triggers: list[str] = field(default_factory=list)
    audience: list[str] = field(default_factory=list)
    not_for: list[str] = field(default_factory=list)
    scope: str = ""
    kind: str = "knowledge"
    format: str = "markdown"
    depends_on: list[str] = field(default_factory=list)
    validated: Optional[str] = None
    temporal: TemporalMetadata = field(default_factory=TemporalMetadata)

    # Resolved at parse time -- absolute path to the content file.
    resolved_path: Optional[Path] = field(default=None, repr=False)


@dataclass
class KCPManifest:
    """Parsed KCP ``knowledge.yaml``."""

    kcp_version: str = ""
    project: str = ""
    entity_name: str = ""
    entity_domain: str = ""
    language: str = "en"
    units: list[KCPUnit] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)


def parse_manifest(manifest_path: str | Path) -> KCPManifest:
    """Parse a ``knowledge.yaml`` file and resolve unit paths.

    Args:
        manifest_path: Path to the ``knowledge.yaml`` file.

    Returns:
        A populated :class:`KCPManifest` with resolved unit paths.

    Raises:
        FileNotFoundError: If the manifest file does not exist.
        yaml.YAMLError: If the YAML is malformed.
        ValueError: If the manifest has no ``units`` list.
    """
    manifest_path = Path(manifest_path)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"KCP manifest not found: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    manifest_dir = manifest_path.parent

    # Version: prefer top-level ``version`` (0.20+), fall back to ``kcp_version``
    kcp_version = str(raw.get("version", raw.get("kcp_version", "")))

    # Entity block (0.20+) or flat ``project`` field (0.6+)
    entity = raw.get("entity") or {}
    entity_name = entity.get("name", raw.get("project", ""))
    entity_domain = entity.get("domain", "")

    units_raw: list[dict[str, Any]] = raw.get("units") or []
    if not units_raw:
        raise ValueError(f"KCP manifest has no units: {manifest_path}")

    units: list[KCPUnit] = []
    for u in units_raw:
        temporal_raw = u.get("temporal") or {}
        temporal = TemporalMetadata(
            valid_from=temporal_raw.get("valid_from"),
            valid_until=temporal_raw.get("valid_until"),
            review_by=temporal_raw.get("review_by"),
        )

        unit = KCPUnit(
            id=u.get("id", ""),
            path=u.get("path", ""),
            intent=u.get("intent", ""),
            triggers=_as_list(u.get("triggers")),
            audience=_as_list(u.get("audience")),
            not_for=_as_list(u.get("not_for")),
            scope=u.get("scope", ""),
            kind=u.get("kind", "knowledge"),
            format=u.get("format", "markdown"),
            depends_on=_as_list(u.get("depends_on")),
            validated=u.get("validated"),
            temporal=temporal,
        )

        # Resolve the content file path relative to the manifest directory
        if unit.path:
            resolved = manifest_dir / unit.path
            if resolved.is_file():
                unit.resolved_path = resolved
            else:
                logger.warning(
                    "[KCP] Unit '%s' path does not exist: %s", unit.id, resolved,
                )

        units.append(unit)

    return KCPManifest(
        kcp_version=kcp_version,
        project=raw.get("project", ""),
        entity_name=entity_name,
        entity_domain=entity_domain,
        language=raw.get("language", "en"),
        units=units,
        raw=raw,
    )


def _as_list(value: Any) -> list[str]:
    """Coerce a value to a list of strings."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)]
