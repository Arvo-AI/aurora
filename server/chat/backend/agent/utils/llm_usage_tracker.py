import logging
import time
from typing import Dict, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import tiktoken
import json
from utils.db.connection_pool import db_pool
from .openrouter_pricing_service import get_pricing_service

logger = logging.getLogger(__name__)


@dataclass
class LLMUsage:
    """Data class for LLM usage tracking"""

    user_id: str
    session_id: Optional[str]
    model_name: str
    api_provider: str
    request_type: str
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    response_time_ms: int
    error_message: Optional[str] = None
    request_metadata: Optional[Dict[str, Any]] = None


class LLMUsageTracker:
    """Tracks LLM usage including token counting and cost calculation"""

    MODEL_PRICING = {
        "openai/gpt-5.2": {
            "input": 0.00175,
            "output": 0.014,
        },
        "anthropic/claude-sonnet-4-5": {
            "input": 0.003,
            "output": 0.015,
        },
        "anthropic/claude-opus-4-5": {
            "input": 0.005,
            "output": 0.025,
        },
        "google/gemini-3-pro-preview": {
            "input": 0.00125,
            "output": 0.01,
        },
        "default": {"input": 0.001, "output": 0.002},
    }

    @classmethod
    def count_tokens(cls, text: str, model_name: str = "gpt-4") -> int:
        """Count tokens in text using tiktoken"""
        try:
            # Map model names to tiktoken encodings
            encoding_map = {
                "gpt-4": "cl100k_base",
                "gpt-4o": "o200k_base",
                "gpt-3.5-turbo": "cl100k_base",
                "claude": "cl100k_base",  # Use GPT-4 encoding as approximation
                "default": "cl100k_base",
            }

            # Determine encoding based on model
            if "gpt-4o" in model_name:
                encoding_name = "o200k_base"
            elif "gpt-4" in model_name or "gpt-3.5" in model_name:
                encoding_name = "cl100k_base"
            elif "claude" in model_name.lower():
                encoding_name = "cl100k_base"  # Approximation
            else:
                encoding_name = "cl100k_base"  # Default

            encoding = tiktoken.get_encoding(encoding_name)
            return len(encoding.encode(str(text)))

        except Exception as e:
            logger.warning(
                f"Error counting tokens: {e}. Using character-based estimation."
            )
            # Fallback: rough estimation (1 token ≈ 4 characters)
            return len(str(text)) // 4

    @classmethod
    def count_tokens_from_messages(
        cls, messages: Any, model_name: str = "gpt-4"
    ) -> int:
        """Count tokens from message objects"""
        try:
            total_tokens = 0

            if isinstance(messages, list):
                for message in messages:
                    if hasattr(message, "content"):
                        # Handle multimodal content
                        if isinstance(message.content, list):
                            for content_part in message.content:
                                if isinstance(content_part, dict):
                                    if content_part.get("type") == "text":
                                        total_tokens += cls.count_tokens(
                                            content_part.get("text", ""), model_name
                                        )
                                    elif content_part.get("type") == "image_url":
                                        # Images roughly cost 85 tokens per image for vision models
                                        total_tokens += 85
                                elif isinstance(content_part, str):
                                    total_tokens += cls.count_tokens(
                                        content_part, model_name
                                    )
                        else:
                            total_tokens += cls.count_tokens(
                                str(message.content), model_name
                            )
                    elif hasattr(message, "text"):
                        total_tokens += cls.count_tokens(message.text, model_name)
                    else:
                        total_tokens += cls.count_tokens(str(message), model_name)
            else:
                total_tokens = cls.count_tokens(str(messages), model_name)

            return total_tokens

        except Exception as e:
            logger.warning(f"Error counting tokens from messages: {e}")
            return cls.count_tokens(str(messages), model_name)

    @classmethod
    def calculate_cost(
        cls,
        input_tokens: int,
        output_tokens: int,
        model_name: str,
        use_dynamic_pricing: bool = True,
    ) -> float:
        """Calculate estimated cost based on token usage and model pricing"""
        try:
            pricing = None

            # Try to get dynamic pricing from OpenRouter first
            if use_dynamic_pricing:
                try:
                    pricing_service = get_pricing_service()
                    pricing = pricing_service.get_model_pricing(model_name)
                    logger.debug(f"Using dynamic pricing for {model_name}: {pricing}")
                except Exception as e:
                    logger.warning(
                        f"Failed to get dynamic pricing for {model_name}: {e}"
                    )

            # Fallback to static pricing if dynamic pricing fails or is disabled
            if not pricing:
                # Get pricing for the model (with fallback for version suffixes)
                pricing = cls.MODEL_PRICING.get(model_name)

                # If exact match not found, try without version suffix
                if not pricing:
                    # Remove version suffixes like .1, -v1, etc.
                    base_model = model_name.split(".")[0].split("-v")[0]
                    pricing = cls.MODEL_PRICING.get(base_model)

                # Final fallback to default pricing
                if not pricing:
                    pricing = cls.MODEL_PRICING["default"]
                    logger.info(
                        f"Using default static pricing for unknown model: {model_name}"
                    )
                else:
                    logger.debug(f"Using static pricing for {model_name}: {pricing}")

            # Calculate cost (pricing is per 1K tokens)
            input_cost = (input_tokens / 1000) * pricing["input"]
            output_cost = (output_tokens / 1000) * pricing["output"]

            return round(input_cost + output_cost, 6)

        except Exception as e:
            logger.warning(f"Error calculating cost: {e}")
            return 0.0

    @classmethod
    def extract_usage_from_response(cls, response: Any) -> Dict[str, int]:
        """Extract token usage from API response when available"""
        try:
            usage = {"input_tokens": 0, "output_tokens": 0}

            # Try to extract from various response formats
            # 1 Newer OpenRouter / OpenAI style: usage_metadata dict
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                usage_data = response.usage_metadata
                usage["input_tokens"] = usage_data.get(
                    "prompt_tokens", usage_data.get("input_tokens", 0)
                )
                usage["output_tokens"] = usage_data.get(
                    "completion_tokens", usage_data.get("output_tokens", 0)
                )

            # 2 Traditional attribute-based usage object
            elif hasattr(response, "usage"):
                if hasattr(response.usage, "prompt_tokens"):
                    usage["input_tokens"] = response.usage.prompt_tokens
                if hasattr(response.usage, "completion_tokens"):
                    usage["output_tokens"] = response.usage.completion_tokens

            # 3 Dict-based responses (e.g. OpenAI v1 chat API style)
            elif isinstance(response, dict):
                # Prefer nested usage_metadata first
                if "usage_metadata" in response and response["usage_metadata"]:
                    usage_data = response["usage_metadata"]
                    usage["input_tokens"] = usage_data.get(
                        "prompt_tokens", usage_data.get("input_tokens", 0)
                    )
                    usage["output_tokens"] = usage_data.get(
                        "completion_tokens", usage_data.get("output_tokens", 0)
                    )
                elif "usage" in response:
                    usage_data = response["usage"]
                    usage["input_tokens"] = usage_data.get("prompt_tokens", 0)
                    usage["output_tokens"] = usage_data.get("completion_tokens", 0)

            return usage

        except Exception as e:
            logger.warning(f"Error extracting usage from response: {e}")
            return {"input_tokens": 0, "output_tokens": 0}

    @classmethod
    def store_usage(cls, usage: LLMUsage) -> bool:
        """Store LLM usage data in the database"""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()

                # Set user context for RLS
                cursor.execute("SET myapp.current_user_id = %s;", (usage.user_id,))

                # Insert usage record
                cursor.execute(
                    """
                    INSERT INTO llm_usage_tracking (
                        user_id, session_id, model_name, api_provider, request_type,
                        input_tokens, output_tokens, estimated_cost, response_time_ms,
                        error_message, request_metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                    (
                        usage.user_id,
                        usage.session_id,
                        usage.model_name,
                        usage.api_provider,
                        usage.request_type,
                        usage.input_tokens,
                        usage.output_tokens,
                        usage.estimated_cost,
                        usage.response_time_ms,
                        usage.error_message,
                        json.dumps(usage.request_metadata)
                        if usage.request_metadata
                        else None,
                    ),
                )

                conn.commit()
                logger.info(f"Stored LLM usage: {usage.model_name} - {usage.input_tokens}+{usage.output_tokens} tokens - ${usage.estimated_cost:.6f}")
                
                # Clear API cost cache for this user to force refresh on next cost check
                try:
                    from utils.billing.billing_cache import clear_user_cache

                    clear_user_cache(usage.user_id)
                    logger.debug(f"Cleared API cost cache for user {usage.user_id} after new usage record")
                except Exception as cache_error:
                    logger.warning(f"Failed to clear API cost cache after usage store: {cache_error}")
                
                return True

        except Exception as e:
            logger.error(f"Error storing LLM usage: {e}")
            return False

    @classmethod
    def track_llm_call(
        cls,
        user_id: str,
        session_id: Optional[str],
        model_name: str,
        request_type: str,
        prompt: Any,
        response: Any = None,
        start_time: Optional[float] = None,
        error_message: Optional[str] = None,
        api_provider: str = "openrouter",
    ) -> bool:
        """
        Track a complete LLM API call with token counting and cost calculation

        Args:
            user_id: User identifier
            session_id: Chat session identifier (optional)
            model_name: Name of the model used
            request_type: Type of request (classify_query, generate_sql, etc.)
            prompt: Input prompt/messages
            response: API response (optional)
            start_time: Request start time for calculating response time
            error_message: Error message if request failed
            api_provider: API provider (default: openrouter)
        """
        try:
            # Count input tokens
            input_tokens = cls.count_tokens_from_messages(prompt, model_name)

            # Count output tokens
            output_tokens = 0
            if response:
                # Try to extract from API response first
                usage_data = cls.extract_usage_from_response(response)
                if usage_data["input_tokens"] > 0:
                    input_tokens = usage_data["input_tokens"]
                if usage_data["output_tokens"] > 0:
                    output_tokens = usage_data["output_tokens"]
                else:
                    # Fallback to counting response text
                    if hasattr(response, "content"):
                        output_tokens = cls.count_tokens(
                            str(response.content), model_name
                        )
                    elif isinstance(response, dict) and "content" in response:
                        output_tokens = cls.count_tokens(
                            str(response["content"]), model_name
                        )
                    else:
                        output_tokens = cls.count_tokens(str(response), model_name)

            # Calculate cost
            estimated_cost = cls.calculate_cost(input_tokens, output_tokens, model_name)

            # Calculate response time
            response_time_ms = (
                int((time.time() - start_time) * 1000) if start_time else 0
            )

            # Create usage record
            usage = LLMUsage(
                user_id=user_id,
                session_id=session_id,
                model_name=model_name,
                api_provider=api_provider,
                request_type=request_type,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                estimated_cost=estimated_cost,
                response_time_ms=response_time_ms,
                error_message=error_message,
                request_metadata={
                    "has_images": cls._has_image_content(prompt),
                    "message_count": len(prompt) if isinstance(prompt, list) else 1,
                },
            )

            # Store in database
            return cls.store_usage(usage)

        except Exception as e:
            logger.error(f"Error tracking LLM call: {e}")
            return False

    @classmethod
    def _has_image_content(cls, prompt: Any) -> bool:
        """Check if the prompt contains image content"""
        try:
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
            elif hasattr(prompt, "content") and isinstance(prompt.content, list):
                for content_part in prompt.content:
                    if (
                        isinstance(content_part, dict)
                        and content_part.get("type") == "image_url"
                    ):
                        return True
            return False
        except Exception:
            return False

    @classmethod
    def get_user_usage_summary(cls, user_id: str, days: int = 30) -> Dict[str, Any]:
        """Get usage summary for a user over the specified number of days"""
        try:
            with db_pool.get_user_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SET myapp.current_user_id = %s;", (user_id,))

                # Get LLM summary statistics (exclude non-LLM cost rows)
                cursor.execute(
                    """
                    SELECT 
                        COUNT(*) as total_requests,
                        SUM(input_tokens) as total_input_tokens,
                        SUM(output_tokens) as total_output_tokens,
                        SUM(total_tokens) as total_tokens,
                        SUM(estimated_cost) as total_cost,
                        AVG(response_time_ms) as avg_response_time,
                        COUNT(DISTINCT model_name) as unique_models,
                        COUNT(DISTINCT session_id) as unique_sessions
                    FROM llm_usage_tracking
                    WHERE user_id = %s 
                    AND timestamp >= NOW() - INTERVAL '%s days'
                    AND request_type NOT LIKE '%%\\_cost' ESCAPE '\\'
                """,
                    (user_id, days),
                )

                summary = cursor.fetchone()

                # Get LLM usage by model (exclude non-LLM cost rows)
                cursor.execute(
                    """
                    SELECT 
                        model_name,
                        COUNT(*) as requests,
                        SUM(total_tokens) as tokens,
                        SUM(estimated_cost) as cost
                    FROM llm_usage_tracking
                    WHERE user_id = %s 
                    AND timestamp >= NOW() - INTERVAL '%s days'
                    AND request_type NOT LIKE '%%\\_cost' ESCAPE '\\'
                    GROUP BY model_name
                    ORDER BY cost DESC
                """,
                    (user_id, days),
                )

                model_usage = cursor.fetchall()

                return {
                    "summary": {
                        "total_requests": summary[0] or 0,
                        "total_input_tokens": summary[1] or 0,
                        "total_output_tokens": summary[2] or 0,
                        "total_tokens": summary[3] or 0,
                        "total_cost": float(summary[4]) if summary[4] else 0.0,
                        "avg_response_time_ms": float(summary[5])
                        if summary[5]
                        else 0.0,
                        "unique_models": summary[6] or 0,
                        "unique_sessions": summary[7] or 0,
                    },
                    "by_model": [
                        {
                            "model_name": row[0],
                            "requests": row[1],
                            "tokens": row[2],
                            "cost": float(row[3]),
                        }
                        for row in model_usage
                    ],
                }

        except Exception as e:
            logger.error(f"Error getting usage summary: {e}")
            return {"summary": {}, "by_model": []}

    @classmethod
    def get_pricing_info(cls) -> Dict[str, Any]:
        """Get information about current pricing sources and cache status"""
        try:
            pricing_service = get_pricing_service()
            cache_info = pricing_service.get_cache_info()

            return {
                "dynamic_pricing_enabled": True,
                "cache_info": cache_info,
                "fallback_models_count": len(cls.MODEL_PRICING),
                "pricing_source": "openrouter_api"
                if cache_info.get("has_api_key")
                else "static_fallback",
            }
        except Exception as e:
            logger.warning(f"Error getting pricing info: {e}")
            return {
                "dynamic_pricing_enabled": False,
                "error": str(e),
                "fallback_models_count": len(cls.MODEL_PRICING),
                "pricing_source": "static_fallback",
            }

    @classmethod
    def refresh_pricing(cls) -> bool:
        """Manually refresh pricing from OpenRouter API"""
        try:
            pricing_service = get_pricing_service()
            return pricing_service.refresh_pricing()
        except Exception as e:
            logger.error(f"Error refreshing pricing: {e}")
            return False
