from typing import Dict, Optional
from langchain_core.language_models import LanguageModelInput
from langchain_openai import ChatOpenAI
from pydantic import BaseModel
import os
import logging
import json
import time
from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
from chat.backend.agent.providers import create_chat_model
from chat.backend.agent.model_mapper import ModelMapper

logger = logging.getLogger(__name__)


class ModelConfig:
    """Centralized model configuration for all Aurora LLM usage.
    
    All model selections are defined here in one place for easy maintenance.
    Change these values to switch providers across the entire application.
    """
    
    # Primary models for chat and operations
    MAIN_MODEL = "anthropic/claude-sonnet-4.5"
    VISION_MODEL = "anthropic/claude-sonnet-4.5"
    
    # Background RCA model - selected based on RCA_OPTIMIZE_COSTS env var
    @staticmethod
    def get_rca_model() -> str:
        """Get RCA model based on RCA_OPTIMIZE_COSTS env var (defaults to cost-optimized)."""
        optimize_costs = os.getenv("RCA_OPTIMIZE_COSTS", "true").lower() == "true"
        if optimize_costs:
            return "anthropic/claude-3-haiku"  # Cost-optimized
        else:
            return "anthropic/claude-opus-4.5"  # High-quality
    
    # Summarization models
    INCIDENT_REPORT_SUMMARIZATION_MODEL = "anthropic/claude-sonnet-4.5"  # For incident reports and chat context
    TOOL_OUTPUT_SUMMARIZATION_MODEL = "anthropic/claude-sonnet-4.5"  # For summarizing large tool outputs to reduce token usage
    
    # Suggestion extraction
    SUGGESTION_MODEL = "anthropic/claude-sonnet-4.5"
    
    # Email report generation
    EMAIL_REPORT_MODEL = "anthropic/claude-sonnet-4.5"


class LLMManager:
    def __init__(
        self,
        main_model: Optional[str] = None,
        vision_model: Optional[str] = None,
        provider_mode: Optional[str] = None,
    ):
        """
        Initialize LLM Manager with support for multiple provider modes.

        Args:
            main_model: Default model for general tasks (defaults to ModelConfig.MAIN_MODEL)
            vision_model: Model for vision/multimodal tasks (defaults to ModelConfig.VISION_MODEL)
            provider_mode: LLM provider mode ('direct', 'auto', 'openrouter')
                          Defaults to env LLM_PROVIDER_MODE or 'direct'
        """
        # Get provider mode from param or environment
        self.provider_mode = provider_mode or os.getenv("LLM_PROVIDER_MODE")

        # Initialize default LLMs using provider-aware factory
        self.main_llm = create_chat_model(
            main_model or ModelConfig.MAIN_MODEL,
            temperature=0.4,
            provider_mode=self.provider_mode,
        )
        # Vision-capable model for multimodal content
        self.vision_llm = create_chat_model(
            vision_model or ModelConfig.VISION_MODEL,
            temperature=0.4,
            provider_mode=self.provider_mode,
        )

        # Cache for dynamically created models
        self._model_cache = {}

    def _get_or_create_model(self, model_name: str) -> ChatOpenAI:
        """Get or create a model instance for the specified model using provider-aware factory."""
        if model_name in self._model_cache:
            return self._model_cache[model_name]

        # Create new model instance using provider-aware factory
        model_instance = create_chat_model(
            model_name,
            temperature=0.4,
            provider_mode=self.provider_mode,
        )

        # Cache it for future use
        self._model_cache[model_name] = model_instance
        logger.info(
            f"Created new model instance: {model_name} (mode={self.provider_mode})"
        )

        return model_instance

    def _has_image_content(self, prompt: LanguageModelInput) -> bool:
        """Check if the prompt contains image content."""
        try:
            # Check if it's a list of messages
            if isinstance(prompt, list):
                for message in prompt:
                    if hasattr(message, "content") and isinstance(
                        message.content, list
                    ):
                        for content_part in message.content:
                            if (
                                isinstance(content_part, dict)
                                and content_part.get("type") == "image_url"
                            ):
                                return True
            # Check if it's a single message with multimodal content
            elif hasattr(prompt, "content") and isinstance(prompt.content, list):
                for content_part in prompt.content:
                    if (
                        isinstance(content_part, dict)
                        and content_part.get("type") == "image_url"
                    ):
                        return True
        except Exception as e:
            logger.warning(f"Error checking for image content: {e}")
        return False

    def _log_multimodal_content(self, prompt: LanguageModelInput):
        """Debug logging for multimodal content."""
        try:
            if isinstance(prompt, list):
                for i, message in enumerate(prompt):
                    if hasattr(message, "content") and isinstance(
                        message.content, list
                    ):
                        logger.info(
                            f"Message {i} has multimodal content with {len(message.content)} parts"
                        )
                        for j, part in enumerate(message.content):
                            if isinstance(part, dict):
                                if part.get("type") == "image_url":
                                    image_url = part.get("image_url", {}).get("url", "")
                                    logger.info(
                                        f"  Part {j}: Image URL length: {len(image_url)}, starts with: {image_url[:50]}..."
                                    )
                                else:
                                    logger.info(
                                        f"  Part {j}: {part.get('type', 'unknown')} - {str(part)[:100]}..."
                                    )
            elif hasattr(prompt, "content") and isinstance(prompt.content, list):
                logger.info(
                    f"Single message has multimodal content with {len(prompt.content)} parts"
                )
                for j, part in enumerate(prompt.content):
                    if isinstance(part, dict):
                        if part.get("type") == "image_url":
                            image_url = part.get("image_url", {}).get("url", "")
                            logger.info(
                                f"  Part {j}: Image URL length: {len(image_url)}, starts with: {image_url[:50]}..."
                            )
                        else:
                            logger.info(
                                f"  Part {j}: {part.get('type', 'unknown')} - {str(part)[:100]}..."
                            )
        except Exception as e:
            logger.error(f"Error logging multimodal content: {e}")

    def invoke(
        self,
        prompt: LanguageModelInput,
        output_struct: type[BaseModel] | None = None,
        selected_model: str | None = None,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        request_type: str = "general",
    ) -> Dict:
        """Invoke the LLM with the given prompt and return the response."""

        # Start timing for response time calculation
        start_time = time.time()

        # Debug logging for multimodal content
        has_images = self._has_image_content(prompt)
        if has_images:
            logger.info(" DETECTED MULTIMODAL CONTENT - Analyzing...")
            self._log_multimodal_content(prompt)

        # Determine which model to use
        if has_images:
            # For images, use vision model or selected model if it supports vision
            if selected_model:
                # Use selected model for images if provided
                logger.info(f" Using selected model for vision: {selected_model}")
                model = self._get_or_create_model(selected_model)
            else:
                logger.info(
                    f" Using default vision model: {self.vision_llm.model_name}"
                )
                model = self.vision_llm
        elif selected_model:
            # Use the model selected from frontend
            logger.info(f" Using selected model: {selected_model}")
            model = self._get_or_create_model(selected_model)
        else:
            logger.info(f" Using default main model: {self.main_llm.model_name}")
            model = self.main_llm

        # Log the actual prompt being sent
        logger.info(f"Sending prompt to {model.model_name}")

        # Variables for tracking
        result = None
        error_message = None
        llm_response = None  # Store the raw LLM response for usage extraction

        try:
            # Pydantic provides type validation, but not output streaming support
            if output_struct:
                llm_response = model.with_structured_output(
                    schema=output_struct
                ).invoke(prompt)
                result = dict(llm_response)
                logger.info(f" Structured output result: {str(result)[:200]}...")
            else:
                llm_response = model.invoke(prompt)
                result = {"messages": [llm_response]}
                response_content = (
                    str(result.get("messages", [{}])[0])[:200]
                    if result.get("messages")
                    else "No response"
                )
                logger.info(f" LLM Response preview: {response_content}...")

        except Exception as e:
            error_message = str(e)
            logger.error(f" Error invoking LLM: {error_message}")
            raise

        finally:
            # Track usage using OpenRouter response data
            if user_id:
                try:
                    # Determine request type based on parameters
                    if output_struct:
                        actual_request_type = f"structured_{request_type}"
                    else:
                        actual_request_type = request_type

                    # Extract usage from OpenRouter response
                    input_tokens = 0
                    output_tokens = 0

                    # Try to extract usage from the LLM response
                    if llm_response and hasattr(llm_response, "response_metadata"):
                        # LangChain stores OpenRouter usage in response_metadata
                        usage = llm_response.response_metadata.get("token_usage", {})
                        if not usage:
                            # Also try 'usage' key (OpenAI standard)
                            usage = llm_response.response_metadata.get("usage", {})
                        if usage:
                            input_tokens = usage.get("prompt_tokens", 0)
                            output_tokens = usage.get("completion_tokens", 0)
                            logger.info(
                                f" OpenRouter usage: {input_tokens} + {output_tokens} tokens"
                            )

                    # Fallback to manual counting if no usage data
                    if input_tokens == 0 and output_tokens == 0:
                        logger.info(
                            " No usage data from OpenRouter, using manual counting"
                        )
                        input_tokens = LLMUsageTracker.count_tokens_from_messages(
                            prompt, model.model_name
                        )
                        if llm_response:
                            output_tokens = LLMUsageTracker.count_tokens(
                                str(
                                    llm_response.content
                                    if hasattr(llm_response, "content")
                                    else llm_response
                                ),
                                model.model_name,
                            )

                    # Calculate cost and response time
                    estimated_cost = LLMUsageTracker.calculate_cost(
                        input_tokens, output_tokens, model.model_name
                    )
                    response_time_ms = int((time.time() - start_time) * 1000)

                    # Store usage directly in database
                    from chat.backend.agent.utils.llm_usage_tracker import LLMUsage

                    # Detect actual provider from model name or provider mode
                    actual_provider = (
                        ModelMapper.detect_provider(model.model_name)
                        or self.provider_mode
                    )

                    usage_record = LLMUsage(
                        user_id=user_id,
                        session_id=session_id,
                        model_name=model.model_name,
                        api_provider=actual_provider,
                        request_type=actual_request_type,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        estimated_cost=estimated_cost,
                        response_time_ms=response_time_ms,
                        error_message=error_message,
                        request_metadata={
                            "has_images": has_images,
                            "provider_mode": self.provider_mode,
                            "has_usage_data": bool(
                                input_tokens > 0 and output_tokens > 0
                            ),
                        },
                    )

                    success = LLMUsageTracker.store_usage(usage_record)
                    if success:
                        logger.info(
                            f"Tracked usage: {model.model_name} - {input_tokens}+{output_tokens} tokens - ${estimated_cost:.6f}"
                        )
                    else:
                        logger.warning("Failed to store usage data")

                except Exception as tracking_error:
                    logger.warning(f" Error tracking LLM usage: {tracking_error}")
            else:
                logger.debug("No user_id provided, skipping usage tracking")

        return (
            result if result is not None else {"messages": [], "error": error_message}
        )

    def summarize(self, content: str, model: Optional[str] = None) -> str:
        """
        Summarize long content to reduce token usage in LLM context.

        Args:
            content: The content to summarize
            model: Optional model to use for summarization (defaults to ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL)

        Returns:
            Summarized content
        """
        try:
            # CRITICAL DEBUGGING: Log the call stack to see WHO is calling summarize
            import traceback

            call_stack = traceback.format_stack()
            logger.error(f" SUMMARIZE CALLED! Call stack:")
            for i, frame in enumerate(call_stack[-5:]):  # Last 5 frames
                logger.error(f" Frame {i}: {frame.strip()}")

            # Use centralized model config
            summarization_model = model or ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL

            logger.error(f" SUMMARIZING: {len(content)} chars -> {summarization_model}")
            logger.error(f" CONTENT PREVIEW: {content[:200]}...")

            # Create summarization prompt
            summarization_prompt = f"""Please provide a concise summary of the following tool output. 
Focus on the key information that would be useful for an AI assistant to understand the result.
Keep the summary under 500 words while preserving important details and structure.

Content to summarize:
{content}

Summary:"""

            # CRITICAL: Create a completely isolated model instance without any callbacks or streaming
            # This prevents the summary from being sent to WebSocket/frontend
            isolated_summarizer = create_chat_model(
                summarization_model,
                temperature=0.4,
                streaming=False,  # Explicitly disable streaming
                callbacks=None,  # No callbacks to prevent WebSocket sending
                provider_mode=self.provider_mode,
            )

            logger.error(
                f" SUMMARIZATION: Using isolated model {summarization_model} (no streaming, no callbacks)"
            )

            # Invoke the isolated summarizer - this should NOT trigger any WebSocket sends
            logger.error(f" ABOUT TO CALL isolated_summarizer.invoke()")
            response = isolated_summarizer.invoke(summarization_prompt)
            logger.error(f" SUMMARIZER INVOKE COMPLETED")

            if hasattr(response, "content"):
                content = response.content
                # Handle Gemini thinking model responses (list with thinking/text blocks)
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict):
                            part_type = part.get("type", "")
                            if part_type not in ("thinking", "reasoning"):
                                text = part.get("text", "")
                                if text:
                                    text_parts.append(str(text))
                        elif isinstance(part, str):
                            text_parts.append(part)
                    summary = "".join(text_parts)
                else:
                    summary = str(content)
            else:
                summary = str(response)

            logger.error(
                f" GENERATED SUMMARY ({len(summary)} chars): {summary[:200]}..."
            )
            logger.error(f" RETURNING SUMMARY TO CALLER")
            return summary

        except Exception as e:
            logger.error(f" Error during summarization: {e}")
            # Fallback to truncation if summarization fails
            truncated = content[:2000] + "... [truncated due to summarization error]"
            logger.error(f" FALLBACK: Returning truncated content")
            return truncated
