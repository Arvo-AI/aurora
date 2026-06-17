"""Extended Weaviate schema and insert for KCP metadata.

Adds KCP-specific properties (intent, triggers, audience, temporal, etc.)
to the existing ``KnowledgeBaseChunk`` collection and provides an
``insert_chunks_with_metadata`` function that stores them alongside the
standard Aurora chunk fields.

The schema migration is idempotent -- calling ``ensure_kcp_properties``
multiple times is safe and skips properties that already exist.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# KCP-specific properties to add to KnowledgeBaseChunk.
# Maps property name -> Weaviate DataType string.
_KCP_PROPERTIES: dict[str, str] = {
    "kcp_unit_id": "TEXT",
    "kcp_intent": "TEXT",
    "kcp_triggers": "TEXT_ARRAY",
    "kcp_audience": "TEXT_ARRAY",
    "kcp_not_for": "TEXT_ARRAY",
    "kcp_scope": "TEXT",
    "kcp_kind": "TEXT",
    "kcp_depends_on": "TEXT_ARRAY",
    "kcp_valid_from": "TEXT",
    "kcp_valid_until": "TEXT",
    "kcp_review_by": "TEXT",
    "kcp_project": "TEXT",
    "kcp_version": "TEXT",
}


def ensure_kcp_properties() -> bool:
    """Add KCP properties to the ``KnowledgeBaseChunk`` collection if missing.

    Returns ``True`` if the schema was modified, ``False`` if already
    up to date.  Raises on connection or schema errors.
    """
    from routes.knowledge_base.weaviate_client import (
        _get_weaviate_client,
        COLLECTION_NAME,
    )
    from weaviate.classes.config import DataType, Property

    _type_map = {
        "TEXT": DataType.TEXT,
        "TEXT_ARRAY": DataType.TEXT_ARRAY,
        "INT": DataType.INT,
        "DATE": DataType.DATE,
    }

    _client, collection = _get_weaviate_client()

    existing_config = collection.config.get()
    existing_names = {p.name for p in existing_config.properties}

    to_add = []
    for prop_name, dtype_str in _KCP_PROPERTIES.items():
        if prop_name not in existing_names:
            to_add.append(
                Property(name=prop_name, data_type=_type_map[dtype_str])
            )

    if not to_add:
        logger.info(
            "[KCP Weaviate] All KCP properties already present on %s",
            COLLECTION_NAME,
        )
        return False

    for prop in to_add:
        collection.config.add_property(prop)
        logger.info(
            "[KCP Weaviate] Added property '%s' to %s", prop.name, COLLECTION_NAME,
        )

    logger.info(
        "[KCP Weaviate] Extended %s with %d KCP properties",
        COLLECTION_NAME, len(to_add),
    )
    return True


def insert_chunks_with_metadata(
    user_id: str,
    document_id: str,
    source_filename: str,
    chunks: list[dict[str, Any]],
    org_id: Optional[str] = None,
) -> int:
    """Insert chunks with KCP metadata into Weaviate.

    Works like ``weaviate_client.insert_chunks`` but also stores the
    ``kcp_*`` keys present in each chunk dict.  Automatically extends
    the collection schema on first call.
    """
    if not chunks:
        return 0

    from routes.knowledge_base.weaviate_client import _get_weaviate_client
    from weaviate.util import generate_uuid5

    # Ensure schema has KCP properties (idempotent)
    try:
        ensure_kcp_properties()
    except Exception as e:
        logger.warning("[KCP Weaviate] Could not ensure KCP properties: %s", e)
        # Continue anyway -- unknown keys are silently ignored by Weaviate batch.

    _, collection = _get_weaviate_client()
    success_count = 0
    now = datetime.now(timezone.utc).isoformat()

    with collection.batch.dynamic() as batch:
        for chunk in chunks:
            try:
                chunk_index = chunk.get("chunk_index", 0)
                uuid = generate_uuid5(f"{user_id}:{document_id}:{chunk_index}")

                properties: dict[str, Any] = {
                    "user_id": user_id,
                    "document_id": document_id,
                    "chunk_index": chunk_index,
                    "content": chunk.get("content", ""),
                    "heading_context": chunk.get("heading_context", ""),
                    "source_filename": source_filename,
                    "created_at": now,
                }
                if org_id:
                    properties["org_id"] = org_id

                # Copy KCP metadata properties from the enriched chunk dict
                for key in _KCP_PROPERTIES:
                    value = chunk.get(key)
                    if value is not None:
                        properties[key] = value

                batch.add_object(properties=properties, uuid=uuid)
                success_count += 1

            except Exception as e:
                logger.error(
                    "[KCP Weaviate] Error adding chunk %s: %s",
                    chunk.get("chunk_index", "?"), e,
                )

    # Report batch failures
    if collection.batch.failed_objects:
        fail_count = len(collection.batch.failed_objects)
        logger.error(
            "[KCP Weaviate] Batch had %d failures out of %d",
            fail_count, success_count,
        )
        for i, failed in enumerate(collection.batch.failed_objects[:5]):
            logger.error("[KCP Weaviate] Failed object %d: %s", i + 1, failed)
        return max(0, success_count - fail_count)

    logger.info(
        "[KCP Weaviate] Inserted %d chunks (with KCP metadata) for doc %s",
        success_count, document_id,
    )
    return success_count
