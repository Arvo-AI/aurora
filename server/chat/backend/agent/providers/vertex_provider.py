"""
Vertex AI provider implementation for Gemini models via Google Cloud.

Uses ChatGoogleGenerativeAI with vertexai=True from langchain-google-genai 4.x.
No separate langchain-google-vertexai package needed.

Auth: VERTEX_AI_PROJECT required. Auth via VERTEX_AI_API_KEY, VERTEX_AI_SERVICE_ACCOUNT_JSON,
GOOGLE_APPLICATION_CREDENTIALS, or Application Default Credentials (ADC).
"""

import logging
import os
import tempfile

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI

from .base_provider import BaseLLMProvider, apply_gemini_thinking_config

logger = logging.getLogger(__name__)


class VertexAIProvider(BaseLLMProvider):
    """Vertex AI provider for Gemini models via Google Cloud."""

    def __init__(self):
        super().__init__()
        self.project = os.getenv("VERTEX_AI_PROJECT")
        self.location = os.getenv("VERTEX_AI_LOCATION", "us-central1")
        self.api_key = os.getenv("VERTEX_AI_API_KEY")
        self._setup_service_account()

    def _setup_service_account(self):
        """Set up service account credentials from env var if provided."""
        sa_json = os.getenv("VERTEX_AI_SERVICE_ACCOUNT_JSON")
        if sa_json and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
            try:
                # Write service account JSON to a temp file and set env var
                tmp = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".json", delete=False
                )
                tmp.write(sa_json)
                tmp.close()
                os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = tmp.name
                logger.info("Vertex AI: Set GOOGLE_APPLICATION_CREDENTIALS from VERTEX_AI_SERVICE_ACCOUNT_JSON")
            except Exception as e:
                logger.warning(f"Vertex AI: Failed to write service account JSON: {e}")

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        if not self.is_available():
            raise RuntimeError(
                "Vertex AI provider is not available. Set VERTEX_AI_PROJECT and provide auth "
                "(VERTEX_AI_API_KEY, VERTEX_AI_SERVICE_ACCOUNT_JSON, or GOOGLE_APPLICATION_CREDENTIALS)."
            )

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Vertex AI provider")

        # Strip vertex/ prefix to get native model name
        native_model = self.get_native_model_name(model)

        logger.info(f"Creating Vertex AI chat model: {native_model} (project={self.project}, location={self.location})")

        # Strip 'streaming' — not a valid ChatGoogleGenerativeAI param in v4.x
        kwargs.pop("streaming", None)

        config = {
            "model": native_model,
            "temperature": temperature if temperature is not None else 0.7,
            "vertexai": True,
            "project": self.project,
            "location": self.location,
        }

        # Use API key if provided, otherwise rely on ADC/service account
        if self.api_key:
            config["google_api_key"] = self.api_key

        apply_gemini_thinking_config(config, native_model)

        config.update(kwargs)

        return ChatGoogleGenerativeAI(**config)

    def is_available(self) -> bool:
        """Check if Vertex AI project is configured.

        ADC is always possible in GCP environments, so having a project set
        is sufficient. Explicit credentials (API key, service account) are optional.
        """
        return bool(self.project)

    def supports_model(self, model: str) -> bool:
        if "/" in model:
            return model.split("/")[0] == "vertex"
        return False

    def get_native_model_name(self, model: str) -> str:
        if "/" in model and model.split("/")[0] == "vertex":
            return model.split("/", 1)[1]
        return model

    def get_supported_models(self) -> list[str]:
        return []
