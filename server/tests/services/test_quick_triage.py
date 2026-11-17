from unittest.mock import MagicMock, patch

import pytest

from services.quick_triage import analyze_log


# Test 1: Check that the LLM returns a valid analysis
@patch("services.quick_triage.create_chat_model")
def test_valid_log(mock_create_model):
    mock_llm = MagicMock()
    mock_create_model.return_value = mock_llm

    mock_llm.with_structured_output.return_value.invoke.return_value = {
        "likely_cause": "Missing Python dependency",
        "evidence": "ModuleNotFoundError: No module named 'pandas'",
        "suggested_fix": "Install pandas in the CI environment",
    }

    result = analyze_log(
        "ModuleNotFoundError: No module named 'pandas'"
    )

    assert result["likely_cause"] == "Missing Python dependency"
    assert "pandas" in result["evidence"]
    assert result["suggested_fix"]


# Test 2: Reject empty logs
@patch("services.quick_triage.create_chat_model")
def test_empty_log(mock_create_model):
    with pytest.raises(ValueError):
        analyze_log("")

    mock_create_model.assert_not_called()


# Test 3: Reject oversized logs
@patch("services.quick_triage.create_chat_model")
def test_oversized_log(mock_create_model):
    with pytest.raises(ValueError):
        analyze_log("x" * 20001)

    mock_create_model.assert_not_called()


# Test 4: Check an ambiguous failure
@patch("services.quick_triage.create_chat_model")
def test_uncertain_analysis(mock_create_model):
    mock_llm = MagicMock()
    mock_create_model.return_value = mock_llm

    mock_llm.with_structured_output.return_value.invoke.return_value = {
        "likely_cause": "Insufficient information",
        "evidence": "Build failed with exit code 1",
        "suggested_fix": "Review preceding build output",
    }

    result = analyze_log("Build failed with exit code 1")

    assert result["likely_cause"] == "Insufficient information"


# Test 5: Check that the LLM is called once
@patch("services.quick_triage.create_chat_model")
def test_single_llm_call(mock_create_model):
    mock_llm = MagicMock()
    mock_create_model.return_value = mock_llm

    mock_llm.with_structured_output.return_value.invoke.return_value = {
        "likely_cause": "Missing dependency",
        "evidence": "ModuleNotFoundError",
        "suggested_fix": "Install dependency",
    }

    analyze_log("ModuleNotFoundError")

    mock_llm.with_structured_output.return_value.invoke.assert_called_once()
