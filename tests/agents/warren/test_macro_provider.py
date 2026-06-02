from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

from agents.warren.macro_provider import (
    _sanitize_qualitative,
    _PERCENT_RE,
    _extract_snapshot_from_text,
    fetch_macro_snapshot,
)
from agents.warren.models import MacroSnapshot


class TestPercentRegex:
    def test_matches_integer_percent(self) -> None:
        assert _PERCENT_RE.search("rate rose 3%")

    def test_matches_decimal_percent(self) -> None:
        assert _PERCENT_RE.search("up 2.5%")

    def test_no_match_qualitative_only(self) -> None:
        assert not _PERCENT_RE.search("dollar strengthened broadly")

    def test_no_match_number_without_sign(self) -> None:
        assert not _PERCENT_RE.search("VIX at 18 today")


class TestSanitizeQualitative:
    def test_strips_percent_value(self) -> None:
        result = _sanitize_qualitative("dollar up 3.5% this week")
        assert "3.5%" not in result

    def test_preserves_text_around_percent(self) -> None:
        result = _sanitize_qualitative("dollar up 3.5% this week")
        assert "dollar up" in result
        assert "this week" in result

    def test_no_change_when_no_percent(self) -> None:
        text = "dollar strengthened broadly"
        assert _sanitize_qualitative(text) == text

    def test_strips_multiple_percents(self) -> None:
        result = _sanitize_qualitative("up 2% then fell 1.5%")
        assert "2%" not in result
        assert "1.5%" not in result


def _make_mock_response(text: str) -> MagicMock:
    """Build a minimal anthropic-like response object."""
    block = MagicMock()
    block.text = text
    resp = MagicMock()
    resp.content = [block]
    return resp


class TestExtractSnapshotFromText:
    def _patch_client(self, json_payload: dict) -> MagicMock:
        mock_client = MagicMock()
        mock_client.messages.create.return_value = _make_mock_response(
            json.dumps(json_payload)
        )
        return mock_client

    def test_returns_macro_snapshot(self) -> None:
        payload = {
            "fed_stance": "hawkish",
            "dollar_signal": "dollar strengthening broadly",
            "geopolitical_notes": "Middle East tensions weigh on sentiment",
            "overall_sentiment": "risk-off",
            "upcoming_events": [{"name": "FOMC", "date": "2026-06-18"}],
        }
        with patch("agents.warren.macro_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = self._patch_client(payload)
            result = _extract_snapshot_from_text("some macro context")
        assert isinstance(result, MacroSnapshot)
        assert result.fed_stance == "hawkish"
        assert result.overall_sentiment == "risk-off"
        assert len(result.upcoming_events) == 1
        assert result.upcoming_events[0].name == "FOMC"

    def test_sanitizes_percent_in_dollar_signal(self) -> None:
        payload = {
            "fed_stance": "neutral",
            "dollar_signal": "dollar up 2.3% on strong data",
            "geopolitical_notes": "no major disruptions",
            "overall_sentiment": "neutral",
            "upcoming_events": [],
        }
        with patch("agents.warren.macro_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = self._patch_client(payload)
            result = _extract_snapshot_from_text("context")
        assert "2.3%" not in result.dollar_signal

    def test_sanitizes_percent_in_geopolitical_notes(self) -> None:
        payload = {
            "fed_stance": "dovish",
            "dollar_signal": "dollar weakening",
            "geopolitical_notes": "oil prices up 5% on Middle East risk",
            "overall_sentiment": "risk-off",
            "upcoming_events": [],
        }
        with patch("agents.warren.macro_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = self._patch_client(payload)
            result = _extract_snapshot_from_text("context")
        assert "5%" not in result.geopolitical_notes

    def test_fallback_defaults_when_empty_json(self) -> None:
        with patch("agents.warren.macro_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = self._patch_client({})
            result = _extract_snapshot_from_text("context")
        assert result.fed_stance == "neutral"
        assert isinstance(result.dollar_signal, str)
        assert len(result.dollar_signal) > 0


class TestFetchMacroSnapshot:
    def test_returns_macro_snapshot(self) -> None:
        payload = {
            "fed_stance": "neutral",
            "dollar_signal": "dollar stable amid low volatility",
            "geopolitical_notes": "no active escalations",
            "overall_sentiment": "neutral",
            "upcoming_events": [],
        }

        search_resp = _make_mock_response("Fed holds rates. Dollar stable. No major geopolitical risks.")
        extract_resp = _make_mock_response(json.dumps(payload))

        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [search_resp, extract_resp]

        with patch("agents.warren.macro_provider.anthropic") as mock_anthropic:
            mock_anthropic.Anthropic.return_value = mock_client
            result = asyncio.run(fetch_macro_snapshot())

        assert isinstance(result, MacroSnapshot)
        assert result.fed_stance == "neutral"

    def test_does_not_accept_ticker_input(self) -> None:
        import inspect
        sig = inspect.signature(fetch_macro_snapshot)
        assert len(sig.parameters) == 0, "fetch_macro_snapshot must not accept any parameters"
