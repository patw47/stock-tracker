from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from market_intelligence.fear_greed import FearGreedResult, get_fear_greed


def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = data
    mock.raise_for_status = MagicMock()
    if status_code >= 400:
        mock.raise_for_status.side_effect = requests.HTTPError(response=mock)
    return mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_fear_greed_returns_score_and_label():
    payload = {"fear_and_greed": {"score": 38.5, "rating": "Fear"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is not None
    assert isinstance(result, FearGreedResult)
    assert result.score == 38.5
    assert result.label == "Fear"


def test_fear_greed_score_rounded():
    payload = {"fear_and_greed": {"score": 72.3456, "rating": "Greed"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is not None
    assert result.score == 72.3


def test_fear_greed_extreme_fear():
    payload = {"fear_and_greed": {"score": 12.0, "rating": "Extreme Fear"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is not None
    assert result.label == "Extreme Fear"


def test_fear_greed_extreme_greed():
    payload = {"fear_and_greed": {"score": 91.0, "rating": "Extreme Greed"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is not None
    assert result.label == "Extreme Greed"


def test_fear_greed_integer_score():
    payload = {"fear_and_greed": {"score": 50, "rating": "Neutral"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is not None
    assert result.score == 50.0


# ---------------------------------------------------------------------------
# Failure / degradation paths — never crash, always return None
# ---------------------------------------------------------------------------

def test_fear_greed_http_error_returns_none():
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response({}, status_code=503)
        result = get_fear_greed()

    assert result is None


def test_fear_greed_network_exception_returns_none():
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.side_effect = requests.ConnectionError("timeout")
        result = get_fear_greed()

    assert result is None


def test_fear_greed_missing_score_returns_none():
    payload = {"fear_and_greed": {"rating": "Fear"}}  # no score
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is None


def test_fear_greed_missing_rating_returns_none():
    payload = {"fear_and_greed": {"score": 30.0}}  # no rating
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is None


def test_fear_greed_missing_fear_and_greed_key_returns_none():
    payload = {"other_data": {"score": 30.0, "rating": "Fear"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is None


def test_fear_greed_malformed_json_returns_none():
    mock = MagicMock()
    mock.raise_for_status = MagicMock()
    mock.json.side_effect = ValueError("invalid json")
    with patch("market_intelligence.fear_greed.requests.get", return_value=mock):
        result = get_fear_greed()

    assert result is None


def test_fear_greed_non_numeric_score_returns_none():
    payload = {"fear_and_greed": {"score": "not-a-number", "rating": "Fear"}}
    with patch("market_intelligence.fear_greed.requests.get") as mock_get:
        mock_get.return_value = _mock_response(payload)
        result = get_fear_greed()

    assert result is None


def test_fear_greed_no_llm_imports():
    """fear_greed must not import any LLM library."""
    import ast
    import importlib
    import inspect
    import sys

    mod_name = "market_intelligence.fear_greed"
    mod = sys.modules.get(mod_name) or importlib.import_module(mod_name)
    source = inspect.getsource(mod)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for name in names:
                assert "anthropic" not in (name or ""), f"LLM import found: {name}"
                assert "openai" not in (name or ""), f"LLM import found: {name}"
