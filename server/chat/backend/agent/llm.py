import logging
import os
import time
from typing import Dict, Optional

from langchain_core.language_models import LanguageModelInput
from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel

from chat.backend.agent.call_options import CHEAP_MODEL, default_auxiliary_model, max_tokens_for
from chat.backend.agent.providers import create_chat_model, get_cached_chat_model
from chat.backend.agent.utils.llm_usage_tracker import LLMUsageTracker
from chat.backend.agent.utils.message_content import extract_text_from_content

logger = logging.getLogger(__name__)


def _optimize_costs_enabled(raw: Optional[str]) -> bool:
    """Interpret ``RCA_OPTIMIZE_COSTS``. Unset AND empty both mean the default (true):
    Compose passes ``${RCA_OPTIMIZE_COSTS}`` through as an empty string when the
    variable is missing from ``.env``, which ``os.getenv(name, "true")`` would have
    read as "not true" and silently switched background RCA to the expensive model.
    """
    value = (raw or "").strip().lower()
    if not value:
        return True
    return value in ("1", "true", "yes")


def _model_label(model) -> str:
    """Best-effort display name / pricing key for a chat model instance.

    ChatOpenAI exposes ``.model_name``; ChatBedrockConverse exposes ``.model_id``
    (``.model`` is only an input alias). Fall back gracefully so native Bedrock
    models don't ``AttributeError`` on logging or usage tracking.
    """
    return (
        getattr(model, "model_name", None)
        or getattr(model, "model_id", None)
        or getattr(model, "model", None)
        or str(model)
    )


class ModelConfig:
    """Centralized model configuration for all Aurora LLM usage.

    All model selections are defined here in one place for easy maintenance.
    Change these values to switch providers across the entire application.

    Two tiers. ``MAIN_MODEL`` (interactive chat, default Opus 5.5) is the only
    place quality is user-visible turn by turn. Everything that runs in the
    background or as a classifier — RCA, the command-safety judge and input rail,
    summaries, the memory agent/selector, severity, suggestions, email reports,
    visualization, the recurrence agent, the Jira judge, enrichment — defaults to
    the cheap tier (``CHEAP_MODEL`` = Claude Haiku 5.5) and has its own env var
    so an operator can raise any single one without touching the others.

    The cheap tier is only the default when the configured provider settings can
    route it (``LLM_PROVIDER_MODE`` openrouter / anthropic / bedrock, or
    ``ANTHROPIC_API_KEY`` under direct routing). Otherwise every auxiliary
    variable falls back to ``MAIN_MODEL`` — the model this install serves by
    definition — so an OpenAI / Google / Vertex / Ollama-only deployment, or a
    Bedrock gateway pinned to explicit ``bedrock/`` ids, keeps working after an
    upgrade instead of having the fail-closed judge and input rail block
    everything. A ``MAIN_MODEL`` pinned to an explicit ``bedrock/<id>`` keeps the
    cheap tier on that same Bedrock geo (``bedrock/<geo>.anthropic.claude-haiku-5-5``
    in native mode, when the geo publishes Haiku 5.5) or follows ``MAIN_MODEL``
    (gateway, ``apac.``, non-Claude pins) — never another provider. See
    ``call_options.default_auxiliary_model``.

    Every variable uses ``or`` semantics: unset AND empty string both mean "use
    the default". Docker Compose forwards ``${VAR}`` as "" when the variable is
    missing from ``.env``, so ``os.getenv(name, default)`` would return "" and
    break model resolution.
    """

    # Opus 5.5 rejects a forced tool_choice, which Trigger RCA and /action pin on their
    # first turn. ForceToolChoice detects that rejection and retries with tool_choice=auto
    # plus an explicit prompt directive, so both flows still reach their tool.
    _DEFAULT_MODEL = "anthropic/claude-opus-5.5"
    # Cheap tier for background and auxiliary calls (see chat.backend.agent.call_options).
    CHEAP_MODEL = CHEAP_MODEL

    # Primary models - configurable via env vars
    MAIN_MODEL = os.getenv("MAIN_MODEL") or _DEFAULT_MODEL
    VISION_MODEL = os.getenv("VISION_MODEL") or os.getenv("MAIN_MODEL") or _DEFAULT_MODEL

    # What "the cheap tier" resolves to in THIS deployment: CHEAP_MODEL when the
    # provider settings can route an anthropic/ id, otherwise MAIN_MODEL. Every
    # auxiliary variable below defaults to this; explicit env values always win.
    AUXILIARY_DEFAULT_MODEL = default_auxiliary_model(MAIN_MODEL)

    # Background RCA model. RCA_MODEL wins when set; otherwise RCA_OPTIMIZE_COSTS
    # (default true, empty counts as true) picks the cheap tier over MAIN's default.
    RCA_MODEL = os.getenv("RCA_MODEL") or (
        AUXILIARY_DEFAULT_MODEL
        if _optimize_costs_enabled(os.getenv("RCA_OPTIMIZE_COSTS"))
        else _DEFAULT_MODEL
    )
    # Yes/no per Jira ticket — a classifier, so cheap tier regardless of RCA_MODEL.
    JIRA_RCA_JUDGE_MODEL = os.getenv("JIRA_RCA_JUDGE_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Multi-agent RCA orchestrator — required when ORCHESTRATOR_ENABLED=true.
    # No fallback to MAIN_MODEL/RCA_MODEL: must be set explicitly.
    RCA_ORCHESTRATOR_MODEL = os.getenv("RCA_ORCHESTRATOR_MODEL") or None  # triage + synthesis
    RCA_SUBAGENT_MODEL = os.getenv("RCA_SUBAGENT_MODEL") or None          # sub-agents

    # Summarization models. SUMMARIZATION_MODEL covers incident/initial/metadata/synopsis
    # summaries; TOOL_OUTPUT_SUMMARIZATION_MODEL (large tool-output capping) may be set
    # separately and otherwise follows SUMMARIZATION_MODEL.
    INCIDENT_REPORT_SUMMARIZATION_MODEL = os.getenv("SUMMARIZATION_MODEL") or AUXILIARY_DEFAULT_MODEL
    TOOL_OUTPUT_SUMMARIZATION_MODEL = (
        os.getenv("TOOL_OUTPUT_SUMMARIZATION_MODEL")
        or os.getenv("SUMMARIZATION_MODEL")
        or AUXILIARY_DEFAULT_MODEL
    )

    # Visualization extraction model
    VISUALIZATION_MODEL = os.getenv("VISUALIZATION_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Suggestion extraction (hypotheses, diagnostics plan, recommendations)
    SUGGESTION_MODEL = os.getenv("SUGGESTION_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Next Steps enrichment (descriptions / one-line summaries of suggestions).
    ENRICHMENT_MODEL = os.getenv("ENRICHMENT_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Email report / RCA summary generation
    EMAIL_REPORT_MODEL = os.getenv("EMAIL_REPORT_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Root-cause recurrence detection agent
    RECURRENCE_AGENT_MODEL = os.getenv("RECURRENCE_AGENT_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Memory extraction agent (runs after each investigation) and the per-message
    # memory selector (one small call per chat turn).
    MEMORY_AGENT_MODEL = os.getenv("MEMORY_AGENT_MODEL") or AUXILIARY_DEFAULT_MODEL
    MEMORY_SELECTOR_MODEL = os.getenv("MEMORY_SELECTOR_MODEL") or AUXILIARY_DEFAULT_MODEL

    # One-word severity classification after a background RCA.
    SEVERITY_MODEL = os.getenv("SEVERITY_MODEL") or AUXILIARY_DEFAULT_MODEL

    # Command-safety judge + prompt-injection input rail. Under OpenRouter the historical
    # default (a small Gemini) is kept; everywhere else the cheap tier (or MAIN_MODEL when
    # the cheap tier is not routable — both fail closed, so they must get a model that
    # this install can actually serve).
    GUARDRAILS_MODEL = os.getenv("GUARDRAILS_LLM_MODEL") or (
        "google/gemini-2.5-flash-lite"
        if (os.getenv("LLM_PROVIDER_MODE") or "").strip().lower() == "openrouter"
        else AUXILIARY_DEFAULT_MODEL
    )


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

    def _get_or_create_model(self, model_name: str) -> BaseChatModel:
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
            logger.debug(f"Error checking for image content: {e}")
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
            logger.info("Detected multimodal content")
            self._log_multimodal_content(prompt)

        # Determine which model to use
        if has_images:
            # For images, use vision model or selected model if it supports vision
            if selected_model:
                # Use selected model for images if provided
                logger.info(f"Using selected model for vision: {selected_model}")
                model = self._get_or_create_model(selected_model)
            else:
                logger.info(
                    f"Using default vision model: {_model_label(self.vision_llm)}"
                )
                model = self.vision_llm
        elif selected_model:
            # Use the model selected from frontend
            logger.info(f"Using selected model: {selected_model}")
            model = self._get_or_create_model(selected_model)
        else:
            logger.info(f"Using default main model: {_model_label(self.main_llm)}")
            model = self.main_llm

        # Log the actual prompt being sent
        model_label = _model_label(model)
        logger.info(f"Sending prompt to {model_label}")

        # Variables for tracking
        result = None
        error_message = None
        llm_response = None  # Store the raw LLM response for usage extraction

        try:
            if output_struct:
                raw_result = model.with_structured_output(
                    schema=output_struct, include_raw=True, method="function_calling"
                ).invoke(prompt)
                llm_response = raw_result.get("raw")
                parsed = raw_result.get("parsed")
                if parsed is None:
                    parsing_error = raw_result.get("parsing_error")
                    logger.warning(f"Structured output parsing failed: {parsing_error}")
                    result = {}
                else:
                    result = dict(parsed)
                logger.info(f"Structured output result: {str(result)[:200]}...")
            else:
                llm_response = model.invoke(prompt)
                result = {"messages": [llm_response]}
                response_content = (
                    str(result.get("messages", [{}])[0])[:200]
                    if result.get("messages")
                    else "No response"
                )
                logger.info(f"LLM response preview: {response_content}...")

        except Exception as e:
            error_message = str(e)
            logger.error(f"Error invoking LLM: {error_message}")
            raise

        finally:
            # Track token usage from provider-reported usage_metadata
            if user_id:
                try:
                    actual_request_type = f"structured_{request_type}" if output_struct else request_type

                    input_tokens = 0
                    output_tokens = 0
                    cached_input_tokens = 0
                    cache_write_tokens = 0

                    # Prefer usage_metadata (standardized across all LangChain providers;
                    # whole prompt plus the cache read / write breakdown)
                    if llm_response and getattr(llm_response, "usage_metadata", None):
                        counts = LLMUsageTracker.extract_token_counts(llm_response.usage_metadata)
                        input_tokens = counts["input_tokens"]
                        output_tokens = counts["output_tokens"]
                        cached_input_tokens = counts["cached_input_tokens"]
                        cache_write_tokens = counts["cache_write_tokens"]
                        logger.info(
                            f"Provider usage_metadata: {input_tokens} + {output_tokens} tokens"
                            + (f" ({cached_input_tokens} cached)" if cached_input_tokens else "")
                            + (f" ({cache_write_tokens} cache write)" if cache_write_tokens else "")
                        )

                    # Fallback: response_metadata.token_usage (OpenAI-style)
                    if input_tokens == 0 and output_tokens == 0:
                        if llm_response and hasattr(llm_response, "response_metadata"):
                            usage = llm_response.response_metadata.get("token_usage", {})
                            if not usage:
                                usage = llm_response.response_metadata.get("usage", {})
                            if usage:
                                input_tokens = usage.get("prompt_tokens", 0)
                                output_tokens = usage.get("completion_tokens", 0)
                                logger.info(
                                    f"Provider response_metadata: {input_tokens} + {output_tokens} tokens"
                                )

                    if input_tokens == 0 and output_tokens == 0:
                        logger.warning(f"No provider usage data for {model_label} - tokens will be 0")

                    from chat.backend.agent.utils.llm_usage_tracker import (
                        LLMUsage,
                        resolve_serving_provider,
                    )

                    # Label the row with the provider that served the call (Bedrock for a
                    # Claude model under LLM_PROVIDER_MODE=bedrock), not the id prefix.
                    actual_provider = resolve_serving_provider(model_label, self.provider_mode)

                    estimated_cost = LLMUsageTracker.calculate_cost(
                        input_tokens, output_tokens, model_label,
                        provider_mode=actual_provider or self.provider_mode,
                        cached_input_tokens=cached_input_tokens,
                        cache_write_tokens=cache_write_tokens,
                    )
                    response_time_ms = int((time.time() - start_time) * 1000)

                    usage_record = LLMUsage(
                        user_id=user_id,
                        session_id=session_id,
                        model_name=model_label,
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
                        },
                        cache_read_input_tokens=cached_input_tokens,
                        cache_write_input_tokens=cache_write_tokens,
                    )

                    success = LLMUsageTracker.store_usage(usage_record)
                    if success:
                        logger.info(
                            f"Tracked usage: {model_label} - {input_tokens}+{output_tokens} tokens - ${estimated_cost:.6f}"
                        )
                    else:
                        logger.warning("Failed to store usage data")

                except Exception as tracking_error:
                    logger.warning(f"Error tracking LLM usage: {tracking_error}")
            else:
                logger.debug("No user_id provided, skipping usage tracking")

        return (
            result if result is not None else {"messages": [], "error": error_message}
        )

    def summarize(self, content: str, model: Optional[str] = None,
                  user_id: Optional[str] = None, session_id: Optional[str] = None,
                  request_type: str = "tool_output_summarization") -> str:
        """
        Summarize long content to reduce token usage in LLM context.

        Thin wrapper over :func:`summarize_text` kept for existing callers; the
        model instance comes from the shared cache, not from this manager.

        Args:
            content: The content to summarize
            model: Optional model to use for summarization (defaults to ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL)
            request_type: Usage-tracking label for the call.

        Returns:
            Summarized content
        """
        return summarize_text(
            content,
            model=model,
            user_id=user_id,
            session_id=session_id,
            request_type=request_type,
            provider_mode=self.provider_mode,
        )


_SUMMARY_PROMPT = """Please provide a concise summary of the following tool output.
Focus on the key information that would be useful for an AI assistant to understand the result.
Keep the summary under 500 words while preserving important details and structure.

Content to summarize:
{content}

Summary:"""


def summarize_text(
    content: str,
    *,
    model: Optional[str] = None,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    request_type: str = "tool_output_summarization",
    provider_mode: Optional[str] = None,
    max_tokens: Optional[int] = None,
    raise_on_error: bool = False,
) -> str:
    """Summarize ``content`` with the (cheap) summarization model.

    One shared, non-streaming, callback-free model instance per
    ``(model, max_tokens)`` is reused across calls (``get_cached_chat_model``), so
    the summarizer no longer builds a fresh client — let alone a whole
    ``LLMManager`` with its main and vision models — for every oversized tool
    output. Thinking is off and the output is capped at
    ``LLM_MAX_TOKENS_SUMMARY`` (default 1500). ``request_type`` labels the usage
    row: ``tool_output_summarization`` for tool outputs, ``context_compression``
    for conversation compaction. By default never raises: on any failure the head
    of the content is returned with a truncation marker; ``raise_on_error=True``
    re-raises instead so a caller with its own fallback (and a result cache that
    must not remember failures) can tell the two apart.
    """
    summarization_model = model or ModelConfig.INCIDENT_REPORT_SUMMARIZATION_MODEL
    output_cap = max_tokens if max_tokens is not None else max_tokens_for("summary")

    try:
        # Cap content to fit within the summarization model's context window.
        # Without this, huge tool outputs (e.g. multi-MB log dumps) get embedded
        # verbatim in the prompt and blow past the model's context limit.
        from chat.backend.agent.utils.chat_context_manager import ChatContextManager
        context_limit = ChatContextManager.get_context_limit(summarization_model)
        # Leave room for the prompt template (~200 tokens) and the response.
        max_content_chars = (context_limit - 200 - output_cap) * 4  # ~4 chars per token
        if len(content) > max_content_chars:
            logger.warning(
                f"Truncating content from {len(content)} to {max_content_chars} chars "
                f"for summarization (model limit: {context_limit} tokens)"
            )
            content = content[:max_content_chars] + "\n\n[Content truncated to fit summarization model context window]"

        logger.info(f"Summarizing {len(content)} chars using {summarization_model}")

        summarization_prompt = _SUMMARY_PROMPT.format(content=content)

        # Shared non-streaming instance without callbacks, so the summary is never
        # streamed to the WebSocket/frontend and no client is built per call.
        summarizer = get_cached_chat_model(
            summarization_model,
            temperature=0.4,
            provider_mode=provider_mode,
            max_tokens=output_cap,
            # Summaries are mechanical; extended thinking only adds output tokens.
            disable_thinking=True,
        )

        if user_id:
            from chat.backend.agent.utils.llm_usage_tracker import tracked_invoke
            response = tracked_invoke(
                summarizer,
                summarization_prompt,
                user_id=user_id,
                session_id=session_id,
                model_name=summarization_model,
                request_type=request_type,
            )
        else:
            response = summarizer.invoke(summarization_prompt)

        if hasattr(response, "content"):
            # Gemini thinking models return a list of thinking/text blocks
            summary = extract_text_from_content(response.content)
        else:
            summary = str(response)

        logger.info(f"Generated summary ({len(summary)} chars)")
        return summary

    except Exception as e:
        logger.error(f"Error during summarization: {e}")
        if raise_on_error:
            raise
        truncated = content[:2000] + "... [truncated due to summarization error]"
        return truncated
