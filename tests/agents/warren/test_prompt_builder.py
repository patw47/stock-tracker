from __future__ import annotations

from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.warren.models import MacroContext
from agents.warren.prompt_builder import build_prompt


class TestBuildPromptWithMacroContext:
    def _full_context(self) -> MacroContext:
        return MacroContext(
            policy_rate=5.25,
            cpi_yoy=3.2,
            pce_yoy=2.8,
            yield_curve_spread_10y2y=-0.3,
            vix=18.5,
            sector_flows={"XLK": 1.2e9, "XLE": -4.5e8},
            central_bank_tone="hawkish",
            ten_year_yield=4.6,
            two_year_yield=4.9,
            market_regime="neutral",
            as_of=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
            snapshot_date=date(2026, 6, 1),
        )

    def test_returns_non_empty_string(self) -> None:
        result = build_prompt(self._full_context(), "What is the outlook for XLK?")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_contains_macro_section_header(self) -> None:
        result = build_prompt(self._full_context(), "Any question")
        assert "MACRO" in result.upper()

    def test_contains_indicator_values(self) -> None:
        ctx = self._full_context()
        result = build_prompt(ctx, "market check")
        assert "5.25" in result
        assert "3.2" in result
        assert "18.5" in result
        assert "4.6" in result

    def test_contains_query(self) -> None:
        query = "Evaluate AAPL long-term prospects"
        result = build_prompt(self._full_context(), query)
        assert query in result

    def test_contains_snapshot_date(self) -> None:
        result = build_prompt(self._full_context(), "q")
        assert "2026-06-01" in result

    def test_sector_flows_rendered(self) -> None:
        result = build_prompt(self._full_context(), "q")
        assert "XLK" in result
        assert "XLE" in result

    def test_central_bank_tone_rendered(self) -> None:
        result = build_prompt(self._full_context(), "q")
        assert "hawkish" in result.lower()

    def test_market_regime_rendered(self) -> None:
        result = build_prompt(self._full_context(), "q")
        assert "neutral" in result.lower()


class TestBuildPromptWithoutMacroContext:
    def test_none_macro_returns_non_empty_string(self) -> None:
        result = build_prompt(None, "What is the market outlook?")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_none_macro_does_not_raise(self) -> None:
        try:
            build_prompt(None, "any query")
        except Exception as exc:
            pytest.fail(f"build_prompt raised with macro_context=None: {exc}")

    def test_none_macro_excludes_macro_section(self) -> None:
        result = build_prompt(None, "any query")
        assert "MACRO CONTEXT" not in result

    def test_none_macro_still_includes_query(self) -> None:
        query = "Analyse TSLA competitive moat"
        result = build_prompt(None, query)
        assert query in result

    def test_none_macro_includes_persona(self) -> None:
        result = build_prompt(None, "q")
        assert "SYSTEM PERSONA" in result or "Warren" in result


class TestBuildPromptForN8nSkills:
    def test_executive_synthesis_uses_skill_rules_not_generic_output(self) -> None:
        query = "[EXECUTIVE-SYNTHESIS SKILL]\nSynthesize today's market news."

        result = build_prompt(None, query)

        assert "N8N SKILL OUTPUT RULES" in result
        assert "Never answer NO_REPLY" in result
        assert "Valuation Take" not in result
        assert query in result

    def test_ticker_watch_uses_json_instruction_not_generic_output(self) -> None:
        query = "[TICKER-WATCH SKILL]\nClassify each ticker as NEW or SKIP."

        result = build_prompt(None, query)

        assert "return only the JSON object" in result
        assert "Key Strengths" not in result
        assert query in result


class TestBuildPromptWithPartialMacroContext:
    def test_none_fields_rendered_as_na(self) -> None:
        ctx = MacroContext(policy_rate=4.0)
        result = build_prompt(ctx, "q")
        assert "N/A" in result

    def test_partial_values_present(self) -> None:
        ctx = MacroContext(policy_rate=4.0)
        result = build_prompt(ctx, "q")
        assert "4.0" in result

    def test_no_sector_flows_shows_na(self) -> None:
        ctx = MacroContext(policy_rate=5.0, sector_flows=None)
        result = build_prompt(ctx, "q")
        assert "N/A" in result


class TestMacroProviderReturnType:
    def test_get_snapshot_returns_macro_context(self) -> None:
        """get_snapshot must return a MacroContext — mock all FRED network calls."""
        with patch("agents.warren.macro_provider.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.text = "DATE,VALUE\n2026-01-01,5.25\n"
            mock_get.return_value = mock_resp

            from agents.warren.macro_provider import get_snapshot

            result = get_snapshot()
            assert isinstance(result, MacroContext)

    def test_get_snapshot_fallback_on_all_failures(self) -> None:
        """When all FRED fetches fail, get_snapshot returns the hardcoded fallback."""
        with patch("agents.warren.macro_provider.requests.get", side_effect=Exception("timeout")):
            from agents.warren.macro_provider import get_snapshot

            result = get_snapshot()
            assert isinstance(result, MacroContext)
            assert result.policy_rate is not None
