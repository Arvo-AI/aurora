"""
Amazon Bedrock provider implementation via the Bedrock Converse API.

Uses ChatBedrockConverse from langchain-aws. Configure a region plus one
standard AWS auth method: IAM role/instance profile, AWS profile, static
AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, or AWS_BEARER_TOKEN_BEDROCK.
"""

import logging
import os

from langchain_aws import ChatBedrockConverse
from langchain_core.language_models.chat_models import BaseChatModel

from .base_provider import BaseLLMProvider

logger = logging.getLogger(__name__)


class BedrockProvider(BaseLLMProvider):
    """Direct Amazon Bedrock provider for models using the Converse API."""

    def __init__(self):
        super().__init__()
        self.region = (
            os.getenv("BEDROCK_REGION")
            or os.getenv("AWS_REGION")
            or os.getenv("AWS_DEFAULT_REGION")
        )
        self.endpoint_url = os.getenv("BEDROCK_ENDPOINT_URL")
        self.credentials_profile_name = os.getenv("BEDROCK_CREDENTIALS_PROFILE")
        self.access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.session_token = os.getenv("AWS_SESSION_TOKEN")
        self.bearer_token = os.getenv("AWS_BEARER_TOKEN_BEDROCK")
        self.model_provider = os.getenv("BEDROCK_MODEL_PROVIDER")

    def get_chat_model(
        self, model: str, temperature: float = 0.4, **kwargs
    ) -> BaseChatModel:
        if not self.is_available():
            raise RuntimeError(
                "Bedrock provider is not available. Set BEDROCK_REGION or "
                "AWS_DEFAULT_REGION, and configure AWS credentials via IAM role, "
                "BEDROCK_CREDENTIALS_PROFILE, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY, "
                "or AWS_BEARER_TOKEN_BEDROCK."
            )

        if self._has_partial_static_credentials():
            raise RuntimeError(
                "Bedrock static credentials are incomplete. Set both "
                "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or use an IAM role/profile."
            )

        if not self.supports_model(model):
            raise ValueError(f"Model {model} is not supported by Bedrock provider")

        native_model = self.get_native_model_name(model)

        logger.info(
            "Creating Bedrock chat model: %s (region=%s, custom_endpoint=%s)",
            native_model,
            self.region,
            bool(self.endpoint_url),
        )

        # ChatBedrockConverse does not accept the OpenAI-style streaming kwarg.
        kwargs.pop("streaming", None)

        config = {
            "model": native_model,
            "temperature": temperature,
            "region_name": self.region,
        }

        if self.endpoint_url:
            config["base_url"] = self.endpoint_url
        if self.credentials_profile_name:
            config["credentials_profile_name"] = self.credentials_profile_name
        if self.access_key_id and self.secret_access_key:
            config["aws_access_key_id"] = self.access_key_id
            config["aws_secret_access_key"] = self.secret_access_key
        if self.session_token:
            config["aws_session_token"] = self.session_token
        if native_model.startswith("arn:") and self.model_provider:
            config["provider"] = self.model_provider

        config.update(kwargs)

        return ChatBedrockConverse(**config)

    def is_available(self) -> bool:
        """Check if Bedrock has enough configuration to create a client."""
        return bool(self.region) and not self._has_partial_static_credentials()

    def supports_model(self, model: str) -> bool:
        if "/" in model:
            return model.split("/")[0] == "bedrock"
        return False

    def get_native_model_name(self, model: str) -> str:
        if "/" in model and model.split("/")[0] == "bedrock":
            return model.split("/", 1)[1]
        return model

    def get_supported_models(self) -> list[str]:
        return []

    def _has_partial_static_credentials(self) -> bool:
        return bool(self.access_key_id) != bool(self.secret_access_key)
