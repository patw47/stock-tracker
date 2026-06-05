from __future__ import annotations

from collections.abc import Mapping
from unittest.mock import patch

import pandas as pd

from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.macro_snapshot import (
    MacroSnapshot,
    MacroSnapshotCache,
    attach_macro_snapshot,
    build_macro_snapshot,
)
from market_intelligence.registry_schema import Registry, TickerEntry


def _frame(values: list[float]) -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=len(values), freq="B")
    return pd.DataFrame(
        {
            "Open": values,
            "High": values,
            "Low": values,
            "Close": values,
            "Volume": 1_000_000,
        },
        index=dates,
    )


def _registry() -> Registry:
    return Registry(
        portfolio_tickers=(),
        macro_tickers=(
            TickerEntry(symbol="IWM", api_symbol="IWM", expected_name="iShares Russell 2000"),
            TickerEntry(symbol="^VIX", api_symbol="^VIX", expected_name="CBOE VIX"),
            TickerEntry(symbol="^TNX", api_symbol="^TNX", expected_name="10Y Treasury"),
            TickerEntry(symbol="DXY", api_symbol="DX-Y.NYB", expected_name="US Dollar"),
            TickerEntry(symbol="OIL", api_symbol="CL=F", expected_name="Crude Oil"),
        ),
        alias_map={"DXY": "DX-Y.NYB", "OIL": "CL=F"},
    )


def _frames() -> dict[str, pd.DataFrame]:
    return {
        "IWM": _frame([200.0, 204.0]),
        "^VIX": _frame([18.0, 19.0]),
        "^TNX": _frame([44.0, 45.0]),
        "DXY": _frame([104.0, 103.5]),
        "OIL": _frame([75.0, 78.0]),
    }


def _alert(ticker: str) -> DeduplicatedAlert:
    candidate = CandidateAlert(
        ticker=ticker,
        as_of="2026-06-02",
        classification="speculative",
        eligible=True,
        is_candidate=True,
        direction="up",
        signal_types=("residual_z",),
        z_resid=3.2,
        residual_threshold=2.5,
        short_history_fallback_applied=False,
        data_issues=(),
    )
    return DeduplicatedAlert(
        candidate=candidate,
        squeeze_prone=None,
        fire_reason="initial",
        signal_types=("residual_z",),
    )


def test_build_macro_snapshot_contains_sprint_6_fields() -> None:
    snapshot = build_macro_snapshot(_frames(), registry=_registry())

    assert snapshot.ten_year_yield == 4.5
    assert snapshot.iwm_close == 204.0
    assert snapshot.iwm_pct_change == 2.0
    assert snapshot.oil_close == 78.0
    assert snapshot.oil_pct_change == 4.0
    assert snapshot.vix_close == 19.0
    assert snapshot.dxy_close == 103.5
    assert snapshot.data_issues == ()


def test_build_macro_snapshot_consumes_s0_frames_without_network_calls() -> None:
    with patch("yfinance.download", side_effect=AssertionError("no live yfinance")):
        with patch(
            "market_intelligence.fetch_eod.requests.get",
            side_effect=AssertionError("no live Twelve Data"),
        ):
            snapshot = build_macro_snapshot(_frames(), registry=_registry())

    assert snapshot.iwm_close == 204.0


def test_build_macro_snapshot_flags_missing_macro_ticker() -> None:
    frames = _frames()
    del frames["OIL"]

    snapshot = build_macro_snapshot(frames, registry=_registry())

    assert snapshot.oil_close is None
    assert "macro_missing:OIL" in snapshot.data_issues


def test_attach_macro_snapshot_builds_once_and_reuses_snapshot() -> None:
    calls = 0
    snapshot = MacroSnapshot(
        as_of="2026-06-02",
        ten_year_yield=4.5,
        iwm_close=204.0,
        iwm_pct_change=2.0,
        oil_close=78.0,
        oil_pct_change=4.0,
        vix_close=19.0,
        dxy_close=103.5,
        data_issues=(),
    )

    def builder(
        frames: Mapping[str, pd.DataFrame],
        registry: Registry | None,
    ) -> MacroSnapshot:
        nonlocal calls
        calls += 1
        assert frames
        assert registry is not None
        return snapshot

    enriched = attach_macro_snapshot(
        (_alert("AAPL"), _alert("MSFT")),
        _frames(),
        cache=MacroSnapshotCache(),
        registry=_registry(),
        builder=builder,
    )

    assert calls == 1
    assert len(enriched) == 2
    assert enriched[0].macro_snapshot is snapshot
    assert enriched[1].macro_snapshot is snapshot


def test_attach_macro_snapshot_skips_empty_alert_set() -> None:
    def builder(
        frames: Mapping[str, pd.DataFrame],
        registry: Registry | None,
    ) -> MacroSnapshot:
        raise AssertionError("macro should not be built without post-dedup alerts")

    assert attach_macro_snapshot((), _frames(), registry=_registry(), builder=builder) == ()
