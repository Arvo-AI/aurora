"""
Knowledge Base Weaviate Client

Manages the KnowledgeBaseChunk collection in Weaviate for storing
and retrieving document chunks with vector embeddings.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import AdditionalConfig, Timeout
from weaviate.classes.query import Filter, HybridFusion
from weaviate.util import generate_uuid5

logger = logging.getLogger(__name__)

COLLECTION_NAME = "KnowledgeBaseChunk"

# Weaviate connection settings from environment
WEAVIATE_HOST = os.getenv("WEAVIATE_HOST")
WEAVIATE_PORT = int(os.getenv("WEAVIATE_PORT"))
WEAVIATE_GRPC_PORT = int(os.getenv("WEAVIATE_GRPC_PORT"))

_client: weaviate.WeaviateClient | None = None
_collection = None


def _get_weaviate_client():
    """Get or create the Weaviate client instance."""
    global _client, _collection

    if _client is not None:
        try:
            # Check if connection is still valid
            ready = _client.is_ready()
            if not ready:
                logger.warning("[KB Weaviate] Connection not ready (is_ready returned False), reconnecting...")
                try:
                    _client.close()
                except Exception:
                    pass  
                _client = None
                _collection = None
            else:
                return _client, _collection
        except Exception:
            logger.warning("[KB Weaviate] Connection lost, reconnecting...")
            try:
                _client.close()
            except Exception:
                pass  # Best effort cleanup
            _client = None
            _collection = None

    try:
        openai_api_key = os.getenv("OPENAI_API_KEY")
        headers = {}
        if openai_api_key:
            headers["X-OpenAI-Api-Key"] = openai_api_key

        _client = weaviate.connect_to_local(
            host=WEAVIATE_HOST,
            port=WEAVIATE_PORT,
            grpc_port=WEAVIATE_GRPC_PORT,
            headers=headers,
            additional_config=AdditionalConfig(
                timeout=Timeout(init=10, query=30, insert=60)
            ),
        )

        logger.info(f"[KB Weaviate] Connected to {WEAVIATE_HOST}:{WEAVIATE_PORT}")

        # Ensure collection exists
        _collection = _ensure_collection(_client)

        return _client, _collection

    except Exception as e:
        logger.error(f"[KB Weaviate] Failed to connect: {e}")
        raise


def _ensure_collection(client: weaviate.WeaviateClient):
    """Create KnowledgeBaseChunk collection if it doesn't exist."""
    try:
        if client.collections.exists(COLLECTION_NAME):
            logger.info(f"[KB Weaviate] Collection {COLLECTION_NAME} already exists")
            return client.collections.get(COLLECTION_NAME)

        logger.info(f"[KB Weaviate] Creating collection {COLLECTION_NAME}")

        collection = client.collections.create(
            name=COLLECTION_NAME,
            vectorizer_config=Configure.Vectorizer.text2vec_transformers(),
            properties=[
                Property(name="user_id", data_type=DataType.TEXT),
                Property(name="document_id", data_type=DataType.TEXT),
                Property(name="chunk_index", data_type=DataType.INT),
                Property(name="content", data_type=DataType.TEXT),
                Property(name="heading_context", data_type=DataType.TEXT),
                Property(name="source_filename", data_type=DataType.TEXT),
                Property(name="created_at", data_type=DataType.DATE),
            ],
        )

        logger.info(f"[KB Weaviate] Collection {COLLECTION_NAME} created successfully")
        return collection

    except Exception as e:
        logger.error(f"[KB Weaviate] Error ensuring collection: {e}")
        raise


def insert_chunks(
    user_id: str,
    document_id: str,
    source_filename: str,
    chunks: list[dict[str, Any]],
) -> int:
    """
    Insert document chunks into Weaviate.

    Args:
        user_id: User identifier
        document_id: Document identifier (UUID)
        source_filename: Original filename of the document
        chunks: List of chunk dictionaries with:
            - content: str (the chunk text)
            - heading_context: str (optional, parent heading hierarchy)
            - chunk_index: int (position in document)

    Returns:
        Number of successfully inserted chunks
    """
    if not chunks:
        return 0

    try:
        _, collection = _get_weaviate_client()
        success_count = 0
        now = datetime.now(timezone.utc).isoformat()

        with collection.batch.dynamic() as batch:
            for chunk in chunks:
                try:
                    chunk_index = chunk.get("chunk_index", 0)
                    # Generate deterministic UUID
                    uuid = generate_uuid5(f"{user_id}:{document_id}:{chunk_index}")

                    properties = {
                        "user_id": user_id,
                        "document_id": document_id,
                        "chunk_index": chunk_index,
                        "content": chunk.get("content", ""),
                        "heading_context": chunk.get("heading_context", ""),
                        "source_filename": source_filename,
                        "created_at": now,
                    }

                    batch.add_object(properties=properties, uuid=uuid)
                    success_count += 1

                except Exception as e:
                    logger.error(f"[KB Weaviate] Error adding chunk {chunk_index}: {e}")

        # Check for failed objects
        if collection.batch.failed_objects:
            logger.error(
                f"[KB Weaviate] Batch insertion had {len(collection.batch.failed_objects)} failures"
            )
            for i, failed in enumerate(collection.batch.failed_objects[:5]):
                logger.error(f"[KB Weaviate] Failed object {i+1}: {failed}")
            actual_success = success_count - len(collection.batch.failed_objects)
            logger.info(
                f"[KB Weaviate] Inserted {actual_success}/{len(chunks)} chunks for doc {document_id}"
            )
            return actual_success

        logger.info(
            f"[KB Weaviate] Successfully inserted {success_count} chunks for doc {document_id}"
        )
        return success_count

    except Exception as e:
        logger.error(f"[KB Weaviate] Error inserting chunks: {e}")
        raise


def search_knowledge_base(
    user_id: str,
    query: str,
    limit: int = 5,
    alpha: float = 0.5,
    min_score: float = 0.0,
) -> list[dict[str, Any]]:
    """
    Search the knowledge base using hybrid search.

    Args:
        user_id: User identifier
        query: Search query string
        limit: Maximum number of results
        alpha: Balance between vector (1.0) and keyword (0.0) search
        min_score: Minimum score threshold (0.0 to skip filtering)

    Returns:
        List of result dictionaries with content, source, and score
    """
    if not query.strip():
        return []

    try:
        _, collection = _get_weaviate_client()

        # Build user filter
        user_filter = Filter.by_property("user_id").equal(user_id)

        # Perform hybrid search
        response = collection.query.hybrid(
            query=query,
            limit=limit,
            alpha=alpha,
            fusion_type=HybridFusion.RANKED,
            filters=user_filter,
            return_metadata=["score"],
        )

        results = []
        for obj in response.objects:
            score = obj.metadata.score if obj.metadata else 0.0

            # Apply minimum score filter
            if min_score > 0.0 and score < min_score:
                continue

            results.append({
                "content": obj.properties.get("content", ""),
                "heading_context": obj.properties.get("heading_context", ""),
                "source_filename": obj.properties.get("source_filename", ""),
                "document_id": obj.properties.get("document_id", ""),
                "chunk_index": obj.properties.get("chunk_index", 0),
                "score": score,
            })

        logger.info(
            f"[KB Weaviate] Search for '{query[:50]}...' returned {len(results)} results"
        )
        return results

    except Exception as e:
        logger.error(f"[KB Weaviate] Error searching: {e}")
        return []


def delete_document_chunks(user_id: str, document_id: str) -> int:
    """
    Delete all chunks for a document.

    Args:
        user_id: User identifier
        document_id: Document identifier

    Returns:
        Number of deleted chunks, or -1 if an error occurred
    """
    try:
        _, collection = _get_weaviate_client()

        # Build filter for user_id AND document_id
        doc_filter = (
            Filter.by_property("user_id").equal(user_id)
            & Filter.by_property("document_id").equal(document_id)
        )

        # Delete matching objects
        result = collection.data.delete_many(where=doc_filter)

        deleted_count = result.successful if hasattr(result, "successful") else 0
        logger.info(
            f"[KB Weaviate] Deleted {deleted_count} chunks for doc {document_id}"
        )
        return deleted_count

    except Exception as e:
        logger.error(f"[KB Weaviate] Error deleting chunks for doc {document_id}: {e}")
        return -1  # Return -1 to distinguish error from "deleted 0 chunks"


def delete_user_chunks(user_id: str) -> int:
    """
    Delete all chunks for a user (cleanup).

    Args:
        user_id: User identifier

    Returns:
        Number of deleted chunks, or -1 if an error occurred
    """
    try:
        _, collection = _get_weaviate_client()

        user_filter = Filter.by_property("user_id").equal(user_id)
        result = collection.data.delete_many(where=user_filter)

        deleted_count = result.successful if hasattr(result, "successful") else 0
        logger.info(f"[KB Weaviate] Deleted {deleted_count} chunks for user {user_id}")
        return deleted_count

    except Exception as e:
        logger.error(f"[KB Weaviate] Error deleting chunks for user {user_id}: {e}")
        return -1  # Return -1 to distinguish error from "deleted 0 chunks"


def get_document_chunk_count(user_id: str, document_id: str) -> int:
    """
    Get the number of chunks for a document.

    Args:
        user_id: User identifier
        document_id: Document identifier

    Returns:
        Number of chunks
    """
    try:
        _, collection = _get_weaviate_client()

        doc_filter = (
            Filter.by_property("user_id").equal(user_id)
            & Filter.by_property("document_id").equal(document_id)
        )

        response = collection.aggregate.over_all(filters=doc_filter)
        return response.total_count if response else 0

    except Exception as e:
        logger.error(f"[KB Weaviate] Error getting chunk count: {e}")
        return 0
