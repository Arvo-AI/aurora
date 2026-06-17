"""KCP -> Aurora KB ingestion pipeline.

Reads a KCP manifest, resolves each unit's content file, processes it
through Aurora's existing document chunker, and uploads chunks to
Weaviate with KCP metadata preserved as additional properties.

Usage (CLI)::

    python -m connectors.kcp_connector \\
        --manifest /path/to/knowledge.yaml \\
        --user-id <aurora-user-id> \\
        [--org-id <org-id>] \\
        [--dry-run]

Usage (programmatic)::

    from connectors.kcp_connector.ingest import ingest_kcp_manifest
    result = ingest_kcp_manifest(
        manifest_path="/path/to/knowledge.yaml",
        user_id="<aurora-user-id>",
    )
"""

from __future__ import annotations

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[KCP Ingest]"


@dataclass
class IngestResult:
    """Summary of a KCP ingestion run."""

    manifest_path: str = ""
    kcp_version: str = ""
    project: str = ""
    units_found: int = 0
    units_ingested: int = 0
    units_skipped: int = 0
    total_chunks: int = 0
    errors: list[str] = field(default_factory=list)


def ingest_kcp_manifest(
    manifest_path: str | Path,
    user_id: str,
    org_id: Optional[str] = None,
    *,
    dry_run: bool = False,
) -> IngestResult:
    """Parse a KCP manifest and ingest all units into Aurora's KB.

    Each unit is treated as a virtual document:

    - Content is read from the resolved path (relative to manifest dir).
    - Chunks are produced by Aurora's ``DocumentProcessor``.
    - Chunks are uploaded to Weaviate via ``insert_chunks`` (or the
      extended ``insert_chunks_with_metadata`` when available), with KCP
      metadata (intent, triggers, audience, not_for, temporal) injected
      as additional properties on each chunk.

    Args:
        manifest_path: Path to the ``knowledge.yaml`` file.
        user_id: Aurora user ID for ownership and Weaviate scoping.
        org_id: Aurora org ID.  If ``None``, derived from ``user_id``.
        dry_run: Parse and validate but do not write to Weaviate.

    Returns:
        An :class:`IngestResult` summarising the run.
    """
    from connectors.kcp_connector.manifest import parse_manifest

    result = IngestResult(manifest_path=str(manifest_path))

    try:
        manifest = parse_manifest(manifest_path)
    except Exception as e:
        result.errors.append(f"Manifest parse failed: {e}")
        logger.error("%s %s", _LOG_PREFIX, result.errors[-1])
        return result

    result.kcp_version = manifest.kcp_version
    result.project = manifest.project or manifest.entity_name
    result.units_found = len(manifest.units)

    if org_id is None:
        try:
            from utils.auth.stateless_auth import get_org_id_for_user
            org_id = get_org_id_for_user(user_id)
        except Exception as e:
            logger.warning(
                "%s Could not resolve org_id for user %s: %s", _LOG_PREFIX, user_id, e,
            )

    for unit in manifest.units:
        if not unit.resolved_path:
            msg = f"Unit '{unit.id}': content file not found ({unit.path})"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.warning("%s %s", _LOG_PREFIX, msg)
            continue

        try:
            content_bytes = unit.resolved_path.read_bytes()
        except Exception as e:
            msg = f"Unit '{unit.id}': failed to read {unit.resolved_path}: {e}"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.error("%s %s", _LOG_PREFIX, msg)
            continue

        if not content_bytes.strip():
            msg = f"Unit '{unit.id}': content file is empty"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.warning("%s %s", _LOG_PREFIX, msg)
            continue

        # Stable document ID derived from project + unit ID so
        # re-ingesting the same manifest replaces rather than duplicates.
        doc_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"kcp:{result.project}:{unit.id}")
        )

        file_type = _kcp_format_to_aurora_type(unit.format, unit.path)

        logger.info(
            "%s Processing unit '%s' (%s, %d bytes, type=%s)",
            _LOG_PREFIX, unit.id, unit.path, len(content_bytes), file_type,
        )

        if dry_run:
            result.units_ingested += 1
            logger.info("%s [dry-run] Would ingest unit '%s'", _LOG_PREFIX, unit.id)
            continue

        try:
            chunks = _chunk_unit(user_id, doc_id, unit.path, content_bytes, file_type)
        except Exception as e:
            msg = f"Unit '{unit.id}': chunking failed: {e}"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.error("%s %s", _LOG_PREFIX, msg)
            continue

        if not chunks:
            msg = f"Unit '{unit.id}': no chunks produced"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.warning("%s %s", _LOG_PREFIX, msg)
            continue

        # Inject KCP metadata into each chunk's properties
        _enrich_chunks_with_kcp_metadata(chunks, unit, manifest)

        try:
            inserted = _upload_chunks(
                user_id=user_id,
                document_id=doc_id,
                source_filename=unit.path,
                chunks=chunks,
                org_id=org_id,
            )
            result.total_chunks += inserted
            result.units_ingested += 1
            logger.info(
                "%s Unit '%s': inserted %d chunks", _LOG_PREFIX, unit.id, inserted,
            )
        except Exception as e:
            msg = f"Unit '{unit.id}': Weaviate upload failed: {e}"
            result.errors.append(msg)
            result.units_skipped += 1
            logger.error("%s %s", _LOG_PREFIX, msg)

    logger.info(
        "%s Finished: %d/%d units ingested, %d chunks total, %d errors",
        _LOG_PREFIX,
        result.units_ingested,
        result.units_found,
        result.total_chunks,
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _kcp_format_to_aurora_type(kcp_format: str, path: str) -> str:
    """Map KCP ``format`` field to Aurora's file_type enum."""
    fmt = kcp_format.lower()
    if fmt in ("markdown", "md"):
        return "markdown"
    if fmt == "pdf":
        return "pdf"
    # Fallback: derive from file extension
    ext = Path(path).suffix.lower().lstrip(".")
    if ext == "md":
        return "markdown"
    if ext == "pdf":
        return "pdf"
    return "plaintext"


def _chunk_unit(
    user_id: str,
    document_id: str,
    filename: str,
    content: bytes,
    file_type: str,
) -> list[dict]:
    """Chunk content using Aurora's existing DocumentProcessor."""
    from routes.knowledge_base.document_processor import DocumentProcessor

    processor = DocumentProcessor(user_id, document_id, filename)
    return processor.process(content, file_type)


def _enrich_chunks_with_kcp_metadata(
    chunks: list[dict],
    unit,
    manifest,
) -> None:
    """Inject KCP-specific metadata into each chunk dict.

    These extra keys are stored alongside the standard Weaviate properties
    by ``insert_chunks_with_metadata``.  Even if Weaviate's schema has not
    been extended yet, the standard ``insert_chunks`` call will simply
    ignore unknown keys -- so this is safe against an unmodified Aurora.
    """
    for chunk in chunks:
        chunk["kcp_unit_id"] = unit.id
        chunk["kcp_intent"] = unit.intent
        chunk["kcp_triggers"] = unit.triggers
        chunk["kcp_audience"] = unit.audience
        chunk["kcp_not_for"] = unit.not_for
        chunk["kcp_scope"] = unit.scope
        chunk["kcp_kind"] = unit.kind
        chunk["kcp_depends_on"] = unit.depends_on

        # Temporal validity
        chunk["kcp_valid_from"] = unit.temporal.valid_from or ""
        chunk["kcp_valid_until"] = unit.temporal.valid_until or ""
        chunk["kcp_review_by"] = unit.temporal.review_by or ""

        # Project-level context
        chunk["kcp_project"] = manifest.project or manifest.entity_name
        chunk["kcp_version"] = manifest.kcp_version

        # Prepend intent to heading_context so it appears in search
        # results without changing the search pipeline.
        existing_heading = chunk.get("heading_context", "")
        if unit.intent:
            intent_prefix = f"[KCP:{unit.id}] {unit.intent}"
            chunk["heading_context"] = (
                f"{intent_prefix} > {existing_heading}" if existing_heading
                else intent_prefix
            )


def _upload_chunks(
    user_id: str,
    document_id: str,
    source_filename: str,
    chunks: list[dict],
    org_id: Optional[str],
) -> int:
    """Upload chunks to Weaviate.

    Tries ``insert_chunks_with_metadata`` (extended schema) first.  If
    that import fails (module not yet deployed), falls back to the
    standard ``insert_chunks`` which ignores the extra KCP keys.
    """
    try:
        from connectors.kcp_connector.weaviate_ext import insert_chunks_with_metadata
        return insert_chunks_with_metadata(
            user_id=user_id,
            document_id=document_id,
            source_filename=source_filename,
            chunks=chunks,
            org_id=org_id,
        )
    except ImportError:
        logger.info(
            "%s Extended Weaviate schema not available, using standard insert_chunks",
            _LOG_PREFIX,
        )
    except Exception as e:
        logger.warning(
            "%s insert_chunks_with_metadata failed (%s), falling back to standard",
            _LOG_PREFIX, e,
        )

    # Fallback: standard insert (KCP metadata keys are silently ignored)
    from routes.knowledge_base.weaviate_client import insert_chunks
    return insert_chunks(
        user_id=user_id,
        document_id=document_id,
        source_filename=source_filename,
        chunks=chunks,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a KCP knowledge.yaml manifest into Aurora's knowledge base.",
    )
    parser.add_argument(
        "--manifest", required=True, help="Path to the knowledge.yaml file",
    )
    parser.add_argument(
        "--user-id", required=True, help="Aurora user ID for ownership",
    )
    parser.add_argument(
        "--org-id", default=None, help="Aurora org ID (auto-resolved if omitted)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and validate without writing to Weaviate",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    )

    result = ingest_kcp_manifest(
        manifest_path=args.manifest,
        user_id=args.user_id,
        org_id=args.org_id,
        dry_run=args.dry_run,
    )

    print(f"\n{'=' * 60}")
    print(f"KCP Ingestion {'(dry run) ' if args.dry_run else ''}Summary")
    print(f"{'=' * 60}")
    print(f"  Manifest:   {result.manifest_path}")
    print(f"  Project:    {result.project}")
    print(f"  KCP ver:    {result.kcp_version}")
    print(f"  Units:      {result.units_found} found, {result.units_ingested} ingested, {result.units_skipped} skipped")
    print(f"  Chunks:     {result.total_chunks}")
    if result.errors:
        print(f"  Errors ({len(result.errors)}):")
        for err in result.errors:
            print(f"    - {err}")
    print()

    sys.exit(1 if result.errors else 0)


if __name__ == "__main__":
    main()
