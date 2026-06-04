from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from market_intelligence.registry_schema import QuarantineEntry, Registry, TickerEntry
from market_intelligence.short_interest import (
    ShortInterestConfig,
    ShortInterestConfigError,
    evaluate_squeeze,
    fetch_all_short_interest,
    fetch_short_interest,
    load_short_interest_config,
)


def _entry(symbol: str = "TEST", api_symbol: str | None = None) -> TickerEntry:
    return TickerEntry(
        symbol=symbol,
        api_symbol=api_symbol or symbol,
        expected_name="Test Corp",
    )


def _config(
    *,
    short_percent_float: float = 0.20,
    days_to_cover: float = 5.0,
    unsupported_symbols: frozenset[str] = frozenset(),
) -> ShortInterestConfig:
    return ShortInterestConfig(
        short_percent_float=short_percent_float,
        days_to_cover=days_to_cover,
        unsupported_symbols=unsupported_symbols,
    )


def _info(
    short_percent_float: object = 0.21,
    shares_short: object = 1_000_000,
    days_to_cover: object = 5.1,
) -> dict[str, object]:
    return {
        "longName": "Test Corp",
        "shortPercentOfFloat": short_percent_float,
        "sharesShort": shares_short,
        "shortRatio": days_to_cover,
    }


def _ticker_mock(info: object) -> MagicMock:
    ticker = MagicMock()
    ticker.info = info
    return ticker


def test_flags_squeeze_only_when_both_thresholds_crossed() -> None:
    result = evaluate_squeeze(_entry(), _info(), _config())

    assert result.squeeze_prone is True
    assert result.coverage_status == "covered"
    assert result.data_issues == ()


@pytest.mark.parametrize(
    ("short_percent_float", "days_to_cover"),
    [(0.21, 4.0), (0.10, 6.0), (0.20, 5.1), (0.21, 5.0)],
)
def test_complete_data_below_or_at_either_threshold_is_not_flagged(
    short_percent_float: float, days_to_cover: float
) -> None:
    result = evaluate_squeeze(
        _entry(), _info(short_percent_float, days_to_cover=days_to_cover), _config()
    )

    assert result.squeeze_prone is False
    assert result.coverage_status == "covered"


@pytest.mark.parametrize(
    ("info", "issue"),
    [
        ({"sharesShort": 1, "shortRatio": 6.0}, "missing_short_percent_float"),
        (
            {"shortPercentOfFloat": 0.25, "sharesShort": 1},
            "missing_days_to_cover",
        ),
        ({}, "missing_short_percent_float"),
    ],
)
def test_missing_required_metric_is_unknown_never_false(
    info: dict[str, object], issue: str
) -> None:
    result = evaluate_squeeze(_entry(), info, _config())

    assert result.squeeze_prone is None
    assert result.coverage_status == "incomplete"
    assert issue in result.data_issues


@pytest.mark.parametrize("value", [-1, "bad", float("nan"), float("inf"), 1.1])
def test_invalid_short_percent_is_unknown(value: object) -> None:
    result = evaluate_squeeze(_entry(), _info(short_percent_float=value), _config())

    assert result.short_percent_float is None
    assert result.squeeze_prone is None
    assert "invalid_short_percent_float" in result.data_issues
    assert "NaN" not in json.dumps(result.to_dict())
    assert "Infinity" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("value", [-1, "bad", float("nan"), float("inf")])
def test_invalid_days_to_cover_is_unknown(value: object) -> None:
    result = evaluate_squeeze(_entry(), _info(days_to_cover=value), _config())

    assert result.days_to_cover is None
    assert result.squeeze_prone is None
    assert "invalid_days_to_cover" in result.data_issues


def test_missing_shares_short_is_context_issue_but_does_not_block_flag() -> None:
    result = evaluate_squeeze(_entry(), _info(shares_short=None), _config())

    assert result.squeeze_prone is True
    assert result.shares_short is None
    assert result.coverage_status == "incomplete"
    assert result.data_issues == ("missing_shares_short",)


def test_fetch_uses_api_alias_and_preserves_canonical_symbol() -> None:
    with patch(
        "market_intelligence.short_interest.yf.Ticker",
        return_value=_ticker_mock(_info()),
    ) as ticker:
        result = fetch_short_interest(_entry("CANON", "YAHOO"), _config())

    ticker.assert_called_once_with("YAHOO")
    assert result.ticker == "CANON"
    assert result.api_symbol == "YAHOO"


def test_fetch_exception_returns_unknown() -> None:
    with patch(
        "market_intelligence.short_interest.yf.Ticker",
        side_effect=TimeoutError("timeout"),
    ):
        result = fetch_short_interest(_entry(), _config())

    assert result.squeeze_prone is None
    assert result.coverage_status == "api_error"
    assert result.data_issues == ("short_interest_fetch_failed",)


@pytest.mark.parametrize(
    ("identity", "issue"),
    [
        ({}, "ticker_identity_missing"),
        ({"longName": "Wrong Company"}, "ticker_identity_mismatch"),
    ],
)
def test_fetch_identity_problem_never_produces_flag(
    identity: dict[str, object], issue: str
) -> None:
    info = _info() | identity
    if not identity:
        info.pop("longName")
    with patch(
        "market_intelligence.short_interest.yf.Ticker",
        return_value=_ticker_mock(info),
    ):
        result = fetch_short_interest(_entry(), _config())

    assert result.squeeze_prone is None
    assert result.coverage_status == "incomplete"
    assert issue in result.data_issues


def test_direct_fetch_respects_unsupported_policy_without_yahoo_call() -> None:
    config = _config(unsupported_symbols=frozenset({"ALTD"}))

    with patch("market_intelligence.short_interest.yf.Ticker") as ticker:
        result = fetch_short_interest(_entry("ALTD"), config)

    ticker.assert_not_called()
    assert result.squeeze_prone is None
    assert result.coverage_status == "unsupported"


def test_batch_returns_portfolio_only_and_handles_unsupported_and_quarantine() -> None:
    registry = Registry(
        portfolio_tickers=(
            _entry("GOOD"),
            _entry("ALTD"),
            _entry("QUARANTINED"),
        ),
        macro_tickers=(_entry("IWM"),),
        alias_map={},
    )
    quarantine = [
        QuarantineEntry(
            symbol="QUARANTINED", reason="ambiguous", timestamp="2026-06-04"
        )
    ]
    config = _config(unsupported_symbols=frozenset({"ALTD"}))

    with patch(
        "market_intelligence.short_interest.yf.Ticker",
        return_value=_ticker_mock(_info() | {"longName": "Test Corp"}),
    ) as ticker:
        results = fetch_all_short_interest(registry, quarantine, config)

    assert tuple(results) == ("GOOD", "ALTD", "QUARANTINED")
    ticker.assert_called_once_with("GOOD")
    assert results["GOOD"].squeeze_prone is True
    assert results["ALTD"].coverage_status == "unsupported"
    assert results["ALTD"].squeeze_prone is None
    assert results["QUARANTINED"].coverage_status == "quarantined"
    assert results["QUARANTINED"].squeeze_prone is None


def test_batch_failure_does_not_abort_other_tickers() -> None:
    registry = Registry(
        portfolio_tickers=(_entry("GOOD"), _entry("FAIL")),
        macro_tickers=(),
        alias_map={},
    )

    def ticker_for(symbol: str) -> MagicMock:
        if symbol == "FAIL":
            raise TimeoutError("timeout")
        return _ticker_mock(_info())

    with patch("market_intelligence.short_interest.yf.Ticker", side_effect=ticker_for):
        results = fetch_all_short_interest(registry, [], _config())

    assert results["GOOD"].squeeze_prone is True
    assert results["FAIL"].coverage_status == "api_error"


def test_default_config_matches_sprint_4_policy() -> None:
    config = load_short_interest_config()

    assert config.short_percent_float == pytest.approx(0.20)
    assert config.days_to_cover == pytest.approx(5.0)
    assert config.unsupported_symbols == frozenset({"ALTD"})


@pytest.mark.parametrize(
    "payload",
    [
        {"thresholds": {}, "unsupported_symbols": []},
        {
            "thresholds": {"short_percent_float": 20, "days_to_cover": 5},
            "unsupported_symbols": [],
        },
        {
            "thresholds": {"short_percent_float": 0.2, "days_to_cover": 5},
            "unsupported_symbols": [1],
        },
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, payload: dict[str, object]) -> None:
    path = tmp_path / "short_interest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ShortInterestConfigError):
        load_short_interest_config(path)
