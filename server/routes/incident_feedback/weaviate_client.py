"""
Aurora Learn - Weaviate Client

Manages the IncidentKnowledge collection in Weaviate for the Aurora Learn feature.
Stores positively-rated RCAs to provide context for similar future incidents.
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import AdditionalConfig, Timeout
from weaviate.classes.query import Filter, MetadataQuery
from weaviate.util import generate_uuid5

logger = logging.getLogger(__name__)

COLLECTION_NAME = "IncidentKnowledge"


def _parse_json_field(value: str) -> list:
    """Parse a JSON string field, returning an empty list on failure."""
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []


# Weaviate connection settings from environment
WEAVIATE_HOST = os.getenv("WEAVIATE_HOST")
WEAVIATE_PORT = int(os.getenv("WEAVIATE_PORT"))
WEAVIATE_GRPC_PORT = int(os.getenv("WEAVIATE_GRPC_PORT"))

_client: weaviate.WeaviateClient | None = None
_collection = None
_client_lock = threading.Lock()


def _reset_client() -> None:
    """Reset the client state, attempting to close any existing connection."""
    global _client, _collection
    if _client is not None:
        try:
            _client.close()
        except Exception as e:
            logger.warning(f"[AURORA LEARN] Error closing client: {e}")
    _client = None
    _collection = None


def _get_weaviate_client():
    """Get or create the Weaviate client instance."""
    global _client, _collection

    # Fast path: check if client is ready without lock
    if _client is not None:
        try:
            if _client.is_ready():
                return _client, _collection
            logger.warning("[AURORA LEARN] Connection not ready, reconnecting...")
        except Exception:
            logger.warning("[AURORA LEARN] Connection lost, reconnecting...")

    # Slow path: acquire lock to create/recreate client
    with _client_lock:
        # Double-check after acquiring lock
        if _client is not None:
            try:
                if _client.is_ready():
                    return _client, _collection
            except Exception:
                pass
            _reset_client()

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

            logger.info(f"[AURORA LEARN] Connected to {WEAVIATE_HOST}:{WEAVIATE_PORT}")

            # Ensure collection exists
            _collection = _ensure_collection(_client)

            return _client, _collection

        except Exception as e:
            logger.error(f"[AURORA LEARN] Failed to connect: {e}")
            raise


def _ensure_collection(client: weaviate.WeaviateClient):
    """Create IncidentKnowledge collection if it doesn't exist."""
    try:
        if client.collections.exists(COLLECTION_NAME):
            logger.info(f"[AURORA LEARN] Collection {COLLECTION_NAME} already exists")
            return client.collections.get(COLLECTION_NAME)

        logger.info(f"[AURORA LEARN] Creating collection {COLLECTION_NAME}")

        collection = client.collections.create(
            name=COLLECTION_NAME,
            vectorizer_config=Configure.Vectorizer.text2vec_transformers(),
            properties=[
                # Metadata - stored but not vectorized
                Property(name="user_id", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="incident_id", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="feedback_id", data_type=DataType.TEXT, skip_vectorization=True),
                # Core matching signals - vectorized
                Property(name="alert_title", data_type=DataType.TEXT),
                Property(name="alert_service", data_type=DataType.TEXT),
                Property(name="source_type", data_type=DataType.TEXT),
                Property(name="severity", data_type=DataType.TEXT),
                Property(name="aurora_summary", data_type=DataType.TEXT),
                # Retrieved but not vectorized (large JSON / redundant)
                Property(name="thoughts", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="citations", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="full_context", data_type=DataType.TEXT, skip_vectorization=True),
                Property(name="created_at", data_type=DataType.DATE),
            ],
        )

        logger.info(f"[AURORA LEARN] Collection {COLLECTION_NAME} created successfully")
        return collection

    except Exception as e:
        logger.error(f"[AURORA LEARN] Error ensuring collection: {e}")
        raise


def store_good_rca(
    user_id: str,
    incident_id: str,
    feedback_id: str,
    alert_title: str,
    alert_service: str,
    source_type: str,
    severity: str,
    aurora_summary: str,
    thoughts: List[Dict[str, Any]],
    citations: List[Dict[str, Any]],
) -> bool:
    """
    Store a positively-rated RCA in Weaviate for future reference.

    Args:
        user_id: User identifier
        incident_id: Incident identifier
        feedback_id: Feedback record identifier
        alert_title: Title of the alert
        alert_service: Service that triggered the alert
        source_type: Alert source (grafana, datadog, etc.)
        severity: Alert severity
        aurora_summary: Aurora's RCA summary
        thoughts: List of investigation thoughts
        citations: List of evidence citations

    Returns:
        True if stored successfully, False otherwise
    """
    try:
        _, collection = _get_weaviate_client()
        now = datetime.now(timezone.utc).isoformat()

        # Build full context for embedding - combines all relevant text
        thoughts_text = "\n".join([t.get("content", "") for t in thoughts])
        full_context = f"""
Alert: {alert_title}
Service: {alert_service}
Source: {source_type}
Severity: {severity}

Summary:
{aurora_summary}

Investigation:
{thoughts_text}
""".strip()

        # Generate deterministic UUID based on user_id and incident_id
        uuid = generate_uuid5(f"{user_id}:{incident_id}")

        properties = {
            "user_id": user_id,
            "incident_id": incident_id,
            "feedback_id": feedback_id,
            "alert_title": alert_title,
            "alert_service": alert_service or "unknown",
            "source_type": source_type,
            "severity": severity or "unknown",
            "aurora_summary": aurora_summary,
            "thoughts": json.dumps(thoughts),
            "citations": json.dumps(citations),
            "full_context": full_context,
            "created_at": now,
        }

        collection.data.insert(properties=properties, uuid=uuid)

        logger.info(
            f"[AURORA LEARN] Stored good RCA for incident {incident_id} (user: {user_id})"
        )
        return True

    except Exception as e:
        logger.error(f"[AURORA LEARN] Error storing good RCA: {e}")
        return False


def search_similar_good_rcas(
    user_id: str,
    alert_title: str,
    alert_service: str,
    source_type: str,
    limit: int = 2,
    min_score: float = 0.7,
) -> List[Dict[str, Any]]:
    """
    Search for similar past incidents with positive feedback.

    Args:
        user_id: User identifier
        alert_title: Title of the current alert
        alert_service: Service of the current alert
        source_type: Source type of the current alert
        limit: Maximum number of results (default 2)
        min_score: Minimum similarity score (default 0.7)

    Returns:
        List of matching RCAs with similarity scores
    """
    try:
        _, collection = _get_weaviate_client()

        # Build search query combining alert details
        search_query = f"Alert: {alert_title} Service: {alert_service} Source: {source_type}"

        # Build user filter
        user_filter = Filter.by_property("user_id").equal(user_id)

        # Perform vector search
        response = collection.query.near_text(
            query=search_query,
            limit=limit,
            filters=user_filter,
            return_metadata=MetadataQuery(distance=True),
        )

        results = []
        for obj in response.objects:
            # Convert distance to similarity score (1 - distance for cosine)
            distance = obj.metadata.distance if obj.metadata else 1.0
            similarity = 1.0 - distance
            obj_title = obj.properties.get("alert_title", "?")
            logger.info(f"[AURORA LEARN] Candidate: '{obj_title[:30]}' distance={distance:.3f} similarity={similarity:.3f}")

            # Apply minimum score filter
            if similarity < min_score:
                logger.info(f"[AURORA LEARN] Filtered out '{obj_title[:30]}' (similarity {similarity:.3f} < {min_score})")
                continue

            thoughts = _parse_json_field(obj.properties.get("thoughts", "[]"))
            citations = _parse_json_field(obj.properties.get("citations", "[]"))

            results.append({
                "incident_id": obj.properties.get("incident_id", ""),
                "alert_title": obj.properties.get("alert_title", ""),
                "alert_service": obj.properties.get("alert_service", ""),
                "source_type": obj.properties.get("source_type", ""),
                "severity": obj.properties.get("severity", ""),
                "aurora_summary": obj.properties.get("aurora_summary", ""),
                "thoughts": thoughts,
                "citations": citations,
                "similarity": round(similarity, 3),
            })

        logger.info(
            f"[AURORA LEARN] Search for '{alert_title[:30]}...' returned {len(results)} matches (min_score={min_score})"
        )
        return results

    except Exception as e:
        logger.error(f"[AURORA LEARN] Error searching for similar RCAs: {e}")
        return []


def delete_incident_knowledge(user_id: str, incident_id: str) -> bool:
    """
    Delete stored knowledge for a specific incident.

    Args:
        user_id: User identifier
        incident_id: Incident identifier

    Returns:
        True if deleted successfully, False otherwise
    """
    try:
        _, collection = _get_weaviate_client()

        # Build filter for user_id AND incident_id
        doc_filter = (
            Filter.by_property("user_id").equal(user_id)
            & Filter.by_property("incident_id").equal(incident_id)
        )

        result = collection.data.delete_many(where=doc_filter)

        deleted_count = result.successful if hasattr(result, "successful") else 0
        logger.info(
            f"[AURORA LEARN] Deleted {deleted_count} knowledge entries for incident {incident_id}"
        )
        return True

    except Exception as e:
        logger.error(f"[AURORA LEARN] Error deleting incident knowledge: {e}")
        return False


def delete_user_knowledge(user_id: str) -> int:
    """
    Delete all stored knowledge for a user.

    Args:
        user_id: User identifier

    Returns:
        Number of deleted entries, or -1 if error
    """
    try:
        _, collection = _get_weaviate_client()

        user_filter = Filter.by_property("user_id").equal(user_id)
        result = collection.data.delete_many(where=user_filter)

        deleted_count = result.successful if hasattr(result, "successful") else 0
        logger.info(f"[AURORA LEARN] Deleted {deleted_count} knowledge entries for user {user_id}")
        return deleted_count

    except Exception as e:
        logger.error(f"[AURORA LEARN] Error deleting user knowledge: {e}")
        return -1
