import json
from typing import Dict, List, Tuple
import openai
import re
from openai.types.chat import ChatCompletionSystemMessageParam, ChatCompletionUserMessageParam
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

        OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
        assert OPENAI_API_KEY is not None, "OPENAI_API_KEY environment variable not set"
        os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
        

        WEAVIATE_HOST = os.getenv("WEAVIATE_HOST", "weaviate.default.svc.cluster.local")  # Default Kubernetes service name


        #production version-----------------------------
        self.client = weaviate.connect_to_custom(
            http_host=WEAVIATE_HOST,
            http_port=8080,
            http_secure=False,   # Using HTTP, not HTTPS
            grpc_host=WEAVIATE_HOST,
            grpc_port=50051,
            grpc_secure=False,   # Using insecure gRPC
            headers={"X-OpenAI-Api-Key": OPENAI_API_KEY}
        )
        #------------------------------------------------

        """
        #local version (Docker Compose only)-----------------------------------
        self.client = weaviate.connect_to_local(
            host="weaviate",
            headers={"X-OpenAI-Api-Key": OPENAI_API_KEY},
            port=8080,
            grpc_port=50051
        )
        #------------------------------------------------
        """
       

        assert self.client.is_ready(), "Weaviate client is not ready. Check the connection."

    def is_connected(self) -> bool:
        """Check if the Weaviate client is connected."""
        return self.client.is_connected()

    def close(self) -> None:
        """Close the Weaviate client connection."""
        if self.client.is_connected():
            self.client.close()
