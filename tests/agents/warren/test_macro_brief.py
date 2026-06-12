from __future__ import annotations

import json
from io import BytesIO
from unittest import mock

import pytest

from agents.warren.models import MacroContext, MacroSnapshot, UpcomingEvent


class TestHandleMacroBrief:
    """Test /macro-brief endpoint via warren_server.Handler."""

    def _call_macro_brief(self, mock_snapshot=None, mock_context=None, mock_closes=None):
        import warren_server
        from warren_server import Handler

        handler = mock.MagicMock(spec=Handler)
        handler.rfile = BytesIO(b"{}")
        handler.headers = {"Content-Length": "2"}
        handler.wfile = BytesIO()
        handler.send_response = mock.MagicMock()
        handler.send_header = mock.MagicMock()
        handler.end_headers = mock.MagicMock()

        with (
            mock.patch.object(
                warren_server,
                "get_snapshot",
                return_value=mock_context,
            ),
            mock.patch.object(
                warren_server,
                "get_market_closes",
                return_value=mock_closes or {},
            ),
            mock.patch.object(
                warren_server,
                "fetch_macro_snapshot",
                return_value=mock_snapshot,
            ),
            mock.patch.object(
                warren_server,
                "call_warren",
                return_value=json.dumps(
                    {"result": {"finalAssistantVisibleText": "Le marché est en mode risk-on aujourd'hui."}}
                ),
            ),
        ):
            result = Handler.handle_macro_brief(handler, {})

        return json.loads(result)

    def test_returns_brief_key(self):
        """Endpoint returns JSON with a non-empty 'brief' key."""
        snap = MacroSnapshot(
            fed_stance="neutral",
            dollar_signal="dollar stable",
            geopolitical_notes="tensions modérées",
            overall_sentiment="neutral",
            upcoming_events=[],
        )
        ctx = MacroContext(policy_rate=5.25, vix=18.0)
        resp = self._call_macro_brief(mock_snapshot=snap, mock_context=ctx)
        assert "brief" in resp
        assert isinstance(resp["brief"], str)
        assert len(resp["brief"]) > 0

    def test_fallback_when_web_search_fails(self):
        """Endpoint returns a non-empty brief even when fetch_macro_snapshot fails."""
        import warren_server
        from warren_server import Handler

        handler = mock.MagicMock(spec=Handler)
        handler.rfile = BytesIO(b"{}")
        handler.headers = {"Content-Length": "2"}
        handler.wfile = BytesIO()

        ctx = MacroContext(policy_rate=5.25, vix=18.0)

        with (
            mock.patch.object(warren_server, "get_snapshot", return_value=ctx),
            mock.patch.object(warren_server, "get_market_closes", return_value={}),
            mock.patch.object(
                warren_server,
                "fetch_macro_snapshot",
                side_effect=Exception("web search unavailable"),
            ),
            mock.patch.object(
                warren_server,
                "call_warren",
                return_value=json.dumps(
                    {"result": {"finalAssistantVisibleText": "Brief quantitatif seul."}}
                ),
            ),
        ):
            result = Handler.handle_macro_brief(handler, {})

        resp = json.loads(result)
        assert "brief" in resp
        assert resp["brief"]  # non-vide

    def test_fallback_when_all_sources_fail(self):
        """Endpoint returns a non-empty brief even when every data source fails."""
        import warren_server
        from warren_server import Handler

        handler = mock.MagicMock(spec=Handler)
        handler.rfile = BytesIO(b"{}")
        handler.headers = {"Content-Length": "2"}
        handler.wfile = BytesIO()

        with (
            mock.patch.object(warren_server, "get_snapshot", side_effect=Exception("FRED down")),
            mock.patch.object(warren_server, "get_market_closes", side_effect=Exception("yf down")),
            mock.patch.object(
                warren_server, "fetch_macro_snapshot", side_effect=Exception("web down")
            ),
            mock.patch.object(
                warren_server,
                "call_warren",
                return_value=json.dumps(
                    {"result": {"finalAssistantVisibleText": "Brief de secours."}}
                ),
            ),
        ):
            result = Handler.handle_macro_brief(handler, {})

        resp = json.loads(result)
        assert "brief" in resp
        assert resp["brief"]  # jamais vide

    def test_brief_not_empty_when_warren_fails(self):
        """When call_warren itself raises, handle_macro_brief returns a safe fallback."""
        import warren_server
        from warren_server import Handler

        handler = mock.MagicMock(spec=Handler)
        handler.rfile = BytesIO(b"{}")
        handler.headers = {"Content-Length": "2"}
        handler.wfile = BytesIO()

        with (
            mock.patch.object(warren_server, "get_snapshot", return_value=MacroContext()),
            mock.patch.object(warren_server, "get_market_closes", return_value={}),
            mock.patch.object(
                warren_server, "fetch_macro_snapshot", side_effect=Exception("web down")
            ),
            mock.patch.object(warren_server, "call_warren", side_effect=Exception("openclaw down")),
        ):
            result = Handler.handle_macro_brief(handler, {})

        resp = json.loads(result)
        assert "brief" in resp
        assert resp["brief"]


class TestMacroBriefPrompt:
    """Test prompt_builder for [MACRO-BRIEF SKILL]."""

    def test_prompt_contains_macro_brief_skill_marker(self):
        """build_prompt with [MACRO-BRIEF SKILL] includes the output rules."""
        from agents.warren.prompt_builder import build_prompt

        prompt = build_prompt(None, "[MACRO-BRIEF SKILL]\nDATE: 2026-06-12\n")
        assert "[MACRO-BRIEF SKILL]" in prompt
        assert "prose" in prompt.lower() or "format" in prompt.lower()

    def test_prompt_includes_quantitative_data(self):
        """build_prompt includes FRED values when macro_context provided."""
        from agents.warren.prompt_builder import build_prompt

        ctx = MacroContext(policy_rate=5.25, vix=18.0, ten_year_yield=4.3)
        prompt = build_prompt(ctx, "[MACRO-BRIEF SKILL]\nDATE: 2026-06-12\n")
        assert "5.25" in prompt
        assert "4.3" in prompt or "4.30" in prompt

    def test_prompt_includes_qualitative_snapshot(self):
        """build_prompt includes MacroSnapshot fields for macro-brief."""
        from agents.warren.prompt_builder import build_prompt

        snap = MacroSnapshot(
            fed_stance="dovish",
            dollar_signal="dollar affaibli",
            geopolitical_notes="tensions en Moyen-Orient",
            overall_sentiment="risk-on",
            upcoming_events=[UpcomingEvent(name="FOMC", date="2026-06-15")],
            rate_expectations="marché anticipe une baisse en septembre",
            ipos="IPO fintech NextPay attendue",
            hot_sectors="tech IA, énergie renouvelable",
            fear_greed="72 (greed)",
            notable_rumors="RUMEUR: fusion Apple/Disney",
        )
        prompt = build_prompt(None, "[MACRO-BRIEF SKILL]\nDATE: 2026-06-12\n", macro_snapshot=snap)
        assert "dovish" in prompt
        assert "risk-on" in prompt
        assert "marché anticipe" in prompt
        assert "NextPay" in prompt
        assert "72" in prompt
        assert "RUMEUR" in prompt

    def test_prompt_handles_no_snapshot_gracefully(self):
        """build_prompt degrades gracefully when both sources are None."""
        from agents.warren.prompt_builder import build_prompt

        prompt = build_prompt(None, "[MACRO-BRIEF SKILL]\nDATE: 2026-06-12\n")
        assert "USER QUERY" in prompt
        # no crash, prompt non-vide
        assert len(prompt) > 100

    def test_market_closes_included_in_prompt(self):
        """build_prompt includes IWM and oil data when market_closes provided."""
        from agents.warren.prompt_builder import build_prompt

        closes = {"iwm_close": 205.4, "iwm_pct_1d": 1.23, "oil_close": 78.9, "oil_pct_1d": -0.55}
        prompt = build_prompt(
            None,
            "[MACRO-BRIEF SKILL]\nDATE: 2026-06-12\n",
            market_closes=closes,
        )
        assert "205.4" in prompt or "205.40" in prompt
        assert "78.9" in prompt or "78.90" in prompt


class TestMacroSnapshotNewFields:
    """Test MacroSnapshot accepts new optional fields."""

    def test_new_fields_default_to_none(self):
        snap = MacroSnapshot(
            fed_stance="neutral",
            dollar_signal="stable",
            geopolitical_notes="calm",
            overall_sentiment="neutral",
            upcoming_events=[],
        )
        assert snap.rate_expectations is None
        assert snap.ipos is None
        assert snap.hot_sectors is None
        assert snap.fear_greed is None
        assert snap.notable_rumors is None

    def test_new_fields_accept_values(self):
        snap = MacroSnapshot(
            fed_stance="hawkish",
            dollar_signal="dollar fort",
            geopolitical_notes="conflit Ukraine-Russie",
            overall_sentiment="risk-off",
            upcoming_events=[],
            rate_expectations="pas de baisse avant 2027",
            ipos="IPO Stripe attendue Q3",
            hot_sectors="défense, énergie",
            fear_greed="25 (fear)",
            notable_rumors="RUMEUR: rachat de X par Microsoft",
        )
        assert snap.rate_expectations == "pas de baisse avant 2027"
        assert snap.fear_greed == "25 (fear)"
        assert "Microsoft" in snap.notable_rumors
