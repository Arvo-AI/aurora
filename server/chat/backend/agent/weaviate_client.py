import json
from typing import Dict, List, Tuple
import re
import weaviate
from weaviate.classes.query import Filter
from weaviate.util import generate_uuid5
import os
from dotenv import load_dotenv
from weaviate.classes.config import Configure, Property, DataType
import logging

from chat.backend.agent.db import PostgreSQLClient

load_dotenv()

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

class WeaviateClient:
    def __init__(self, postgres_client: PostgreSQLClient):
        self.postgres_client = postgres_client

        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()

        headers = {}
        if openai_api_key:
            headers["X-OpenAI-Api-Key"] = openai_api_key

        WEAVIATE_HOST = os.getenv("WEAVIATE_HOST", "weaviate.default.svc.cluster.local")
        weaviate_secure = os.getenv("WEAVIATE_SECURE", "false").lower() in ("1", "true", "yes")

        self.client = weaviate.connect_to_custom(
            http_host=WEAVIATE_HOST,
            http_port=int(os.getenv("WEAVIATE_PORT", "8080")),
            http_secure=weaviate_secure,
            grpc_host=WEAVIATE_HOST,
            grpc_port=int(os.getenv("WEAVIATE_GRPC_PORT", "50051")),
            grpc_secure=weaviate_secure,
            headers=headers,
        )

        assert self.client.is_ready(), "Weaviate client is not ready. Check the connection."

    def is_connected(self) -> bool:
        """Check if the Weaviate client is connected."""
        return self.client.is_connected()

    def close(self) -> None:
        """Close the Weaviate client connection."""
        if self.client.is_connected():
            self.client.close()
