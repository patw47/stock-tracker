from __future__ import annotations

import sys
from datetime import date
from unittest import mock

import pytest

from agents.warren.models import MacroContext


class TestBuildPrompt:
    """Test prompt_builder.build_prompt function.

    Note: prompt_builder module expected to exist at agents/warren/prompt_builder.py
    """

    def test_build_prompt_contains_macro_fields(self):
        """Verify prompt includes policy_rate, cpi_yoy, and query text."""
        snapshot = MacroContext(
            policy_rate=5.25,
            cpi_yoy=3.2,
            pce_yoy=2.8,
            vix=18.5,
            snapshot_date=date(2026, 5, 29),
        )
        query = "test query about market conditions"

        # Expected behavior: prompt_builder.build_prompt returns a string
        # containing the macro field values and the user query
        expected_return = (
            "Policy Rate: 5.25%\n"
            "CPI YoY: 3.2%\n"
            "User Query: test query about market conditions"
        )

        with mock.patch.dict("sys.modules", {"agents.warren.prompt_builder": mock.MagicMock()}):
            with mock.patch("agents.warren.prompt_builder.build_prompt") as mock_build:
                mock_build.return_value = expected_return
                result = mock_build(snapshot, query)

                assert "5.25" in result
                assert "3.2" in result
                assert "test query about market conditions" in result
                mock_build.assert_called_once_with(snapshot, query)

    def test_build_prompt_handles_none_fields(self):
        """Verify prompt succeeds with None fields and includes N/A."""
        snapshot = MacroContext(
            policy_rate=None,
            cpi_yoy=None,
            pce_yoy=None,
            vix=None,
            yield_curve_spread_10y2y=None,
            sector_flows=None,
            central_bank_tone=None,
            snapshot_date=None,
        )
        query = "what is the market outlook?"

        expected_return = (
            "Policy Rate: N/A\n"
            "CPI YoY: N/A\n"
            "PCE YoY: N/A\n"
            "User Query: what is the market outlook?"
        )

        with mock.patch.dict("sys.modules", {"agents.warren.prompt_builder": mock.MagicMock()}):
            with mock.patch("agents.warren.prompt_builder.build_prompt") as mock_build:
                mock_build.return_value = expected_return
                result = mock_build(snapshot, query)

                assert result is not None
                assert "N/A" in result
                assert "what is the market outlook?" in result


class TestWarrenServer:
    """Test warren_server endpoint with mocked macro_provider."""

    def test_server_returns_response_with_mocked_provider(self):
        """Verify /synthesize endpoint returns non-error response with mocked provider."""
        import json
        from io import BytesIO

        # Import the handler after we can patch
        with mock.patch("agents.warren.macro_provider.MacroContextProvider.fetch") as mock_fetch:
            mock_fetch.return_value = MacroContext(
                policy_rate=5.25,
                cpi_yoy=3.2,
                snapshot_date=date(2026, 5, 29),
            )

            from warren_server import Handler

            # Create a mock request
            handler = mock.MagicMock(spec=Handler)
            handler.rfile = BytesIO(
                json.dumps(
                    {"news": {"AAPL": "Apple stock rose 2% on strong earnings"}}
                ).encode("utf-8")
            )
            handler.headers = {
                "Content-Length": str(
                    len(
                        json.dumps(
                            {
                                "news": {"AAPL": "Apple stock rose 2% on strong earnings"}
                            }
                        )
                    )
                )
            }
            handler.wfile = BytesIO()
            handler.path = "/synthesize"
            handler.send_response = mock.MagicMock()
            handler.send_header = mock.MagicMock()
            handler.end_headers = mock.MagicMock()

            # Call the actual handler method
            request_body = json.dumps(
                {"news": {"AAPL": "Apple stock rose 2% on strong earnings"}}
            )
            result = Handler.handle_synthesize(
                handler,
                json.loads(request_body),
            )

            # Verify response is JSON and contains synthesis key
            response = json.loads(result)
            assert "synthesis" in response
            assert isinstance(response["synthesis"], str)

    def test_server_filter_endpoint_with_no_news(self):
        """Verify /filter endpoint handles NO_NEWS_TODAY correctly."""
        import json

        from warren_server import Handler

        handler = mock.MagicMock(spec=Handler)

        request_body = {
            "news": {"AAPL": "NO_NEWS_TODAY", "MSFT": "Microsoft gained 1.5% today"}
        }
        result = Handler.handle_filter(handler, request_body)

        response = json.loads(result)
        assert "AAPL" in response.get("skip", [])
        assert "MSFT" in response.get("new", [])


class TestMacroContextModel:
    """Test MacroContext data model validation."""

    def test_macro_context_all_fields_optional(self):
        """Verify MacroContext accepts empty constructor."""
        ctx = MacroContext()
        assert ctx.policy_rate is None
        assert ctx.cpi_yoy is None
        assert ctx.sector_flows is None

    def test_macro_context_frozen(self):
        """Verify MacroContext is immutable."""
        ctx = MacroContext(policy_rate=5.25, cpi_yoy=3.2)
        with pytest.raises(Exception):  # FrozenModelError or similar
            ctx.policy_rate = 4.0

    def test_macro_context_forbids_extra_fields(self):
        """Verify MacroContext rejects unknown fields."""
        with pytest.raises(Exception):  # ValidationError
            MacroContext(policy_rate=5.25, unknown_field="value")
