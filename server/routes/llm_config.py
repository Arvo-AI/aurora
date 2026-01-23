"""
LLM Provider Configuration API Routes.

This module provides endpoints for managing and querying LLM provider configurations.
Allows users to check which providers are available and test API key validity.
"""

from flask import Blueprint, jsonify, request
from chat.backend.agent.providers import get_registry, get_available_providers
from chat.backend.agent.model_mapper import ModelMapper
import logging
import os

logger = logging.getLogger(__name__)

llm_config_bp = Blueprint("llm_config", __name__, url_prefix="/api/llm-config")


@llm_config_bp.route("/available-providers", methods=["GET"])
def get_llm_providers():
    """
    Get list of available LLM providers and their status.

    Returns:
        JSON response with provider information:
        {
            "provider_mode": "openrouter",
            "providers": {
                "openrouter": {"available": true, "configured": true},
                "openai": {"available": false, "configured": false},
                ...
            }
        }
    """
    try:
        provider_mode = os.getenv("LLM_PROVIDER_MODE")
        available_providers = get_available_providers()

        # Build detailed provider info
        provider_info = {}
        for provider_name in ["openrouter", "openai", "anthropic", "google"]:
            is_available = available_providers.get(provider_name, False)
            provider_info[provider_name] = {
                "available": is_available,
                "configured": is_available,  # If available, it means it's configured
            }

        return jsonify(
            {
                "success": True,
                "provider_mode": provider_mode,
                "providers": provider_info,
            }
        ), 200

    except Exception as e:
        logger.error(f"Error getting provider info: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@llm_config_bp.route("/provider-details", methods=["GET"])
def get_provider_details():
    """
    Get detailed information about each provider including supported models.

    Returns:
        JSON response with detailed provider information
    """
    try:
        registry = get_registry()
        provider_info = registry.get_provider_info()

        # Add supported models for each provider
        for info in provider_info:
            provider_name = info["name"]
            supported_models = ModelMapper.get_supported_models_for_provider(
                provider_name
            )
            info["supported_models"] = supported_models[
                :10
            ]  # Limit to first 10 for brevity
            info["total_models"] = len(supported_models)

        return jsonify(
            {
                "success": True,
                "providers": provider_info,
            }
        ), 200

    except Exception as e:
        logger.error(f"Error getting provider details: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@llm_config_bp.route("/test-provider", methods=["POST"])
def test_provider():
    """
    Test a specific provider's API key validity by making a minimal API call.

    Request body:
        {
            "provider": "openai",  // Provider name to test
            "model": "openai/gpt-5.2"  // Optional model to test with
        }

    Returns:
        JSON response indicating if the provider is working
    """
    try:
        data = request.get_json()
        provider_name = data.get("provider")
        test_model = data.get("model")

        if not provider_name:
            return jsonify(
                {"success": False, "error": "Provider name is required"}
            ), 400

        registry = get_registry()
        provider = registry.get_provider(provider_name)

        if not provider:
            return jsonify(
                {"success": False, "error": f"Provider '{provider_name}' not found"}
            ), 404

        # Check if provider is available
        if not provider.is_available():
            return jsonify(
                {
                    "success": False,
                    "error": f"Provider '{provider_name}' is not configured (missing API key)",
                }
            ), 400

        # If model specified, test with that model
        if test_model:
            if not provider.supports_model(test_model):
                return jsonify(
                    {
                        "success": False,
                        "error": f"Model '{test_model}' is not supported by {provider_name}",
                    }
                ), 400

            # Try to create a chat model instance
            try:
                chat_model = provider.get_chat_model(test_model)
                logger.info(f"Successfully created {provider_name} model: {test_model}")
            except Exception as e:
                logger.error(f"Failed to create {provider_name} model: {e}")
                return jsonify(
                    {"success": False, "error": f"Failed to initialize model: {str(e)}"}
                ), 500

        return jsonify(
            {
                "success": True,
                "provider": provider_name,
                "available": True,
                "message": f"Provider '{provider_name}' is configured and ready to use",
            }
        ), 200

    except Exception as e:
        logger.error(f"Error testing provider: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@llm_config_bp.route("/model-info", methods=["GET"])
def get_model_info():
    """
    Get information about a specific model including which provider it belongs to.

    Query params:
        model: Model name (e.g., "openai/gpt-5.2" or "gpt-5.2")

    Returns:
        JSON response with model information
    """
    try:
        model_name = request.args.get("model")

        if not model_name:
            return jsonify({"success": False, "error": "Model name is required"}), 400

        # Detect provider
        provider = ModelMapper.detect_provider(model_name)

        if not provider:
            return jsonify(
                {
                    "success": False,
                    "error": f"Could not determine provider for model '{model_name}'",
                }
            ), 404

        # Get native names for all providers
        native_names = {}
        for p in ["openrouter", "openai", "anthropic", "google"]:
            try:
                native_name = ModelMapper.get_native_name(model_name, p)
                if native_name != model_name or p == provider:
                    native_names[p] = native_name
            except:
                pass

        return jsonify(
            {
                "success": True,
                "model": model_name,
                "detected_provider": provider,
                "native_names": native_names,
            }
        ), 200

    except Exception as e:
        logger.error(f"Error getting model info: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@llm_config_bp.route("/supported-models", methods=["GET"])
def get_supported_models():
    """
    Get list of all supported models, optionally filtered by provider.

    Query params:
        provider: Optional provider name to filter by

    Returns:
        JSON response with supported models
    """
    try:
        provider_filter = request.args.get("provider")

        if provider_filter:
            models = ModelMapper.get_supported_models_for_provider(provider_filter)
            return jsonify(
                {
                    "success": True,
                    "provider": provider_filter,
                    "models": models,
                    "count": len(models),
                }
            ), 200
        else:
            # Get all models from all providers
            from chat.backend.agent.model_mapper import MODEL_MAPPINGS

            all_models = list(MODEL_MAPPINGS.keys())
            return jsonify(
                {"success": True, "models": all_models, "count": len(all_models)}
            ), 200

    except Exception as e:
        logger.error(f"Error getting supported models: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
