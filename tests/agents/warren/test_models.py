from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from pydantic import ValidationError

from agents.warren.models import MacroContext, MacroSnapshot, UpcomingEvent


class TestMacroContextInstantiation:
    def test_all_fields_populated(self) -> None:
        as_of = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        snap_date = date(2026, 6, 1)
        ctx = MacroContext(
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
            as_of=as_of,
            snapshot_date=snap_date,
        )
        assert isinstance(ctx.policy_rate, float)
        assert isinstance(ctx.cpi_yoy, float)
        assert isinstance(ctx.pce_yoy, float)
        assert isinstance(ctx.yield_curve_spread_10y2y, float)
        assert isinstance(ctx.vix, float)
        assert isinstance(ctx.sector_flows, dict)
        assert isinstance(ctx.central_bank_tone, str)
        assert isinstance(ctx.ten_year_yield, float)
        assert isinstance(ctx.two_year_yield, float)
        assert isinstance(ctx.market_regime, str)
        assert isinstance(ctx.as_of, datetime)
        assert isinstance(ctx.snapshot_date, date)

        assert ctx.policy_rate == 5.25
        assert ctx.cpi_yoy == 3.2
        assert ctx.sector_flows == {"XLK": 1.2e9, "XLE": -4.5e8}
        assert ctx.central_bank_tone == "hawkish"
        assert ctx.as_of == as_of
        assert ctx.snapshot_date == snap_date

    def test_all_optional_fields_default_to_none(self) -> None:
        ctx = MacroContext()
        assert ctx.policy_rate is None
        assert ctx.cpi_yoy is None
        assert ctx.pce_yoy is None
        assert ctx.yield_curve_spread_10y2y is None
        assert ctx.vix is None
        assert ctx.sector_flows is None
        assert ctx.central_bank_tone is None
        assert ctx.ten_year_yield is None
        assert ctx.two_year_yield is None
        assert ctx.market_regime is None
        assert ctx.as_of is None
        assert ctx.snapshot_date is None

    def test_partial_fields(self) -> None:
        ctx = MacroContext(policy_rate=5.0, vix=22.0)
        assert ctx.policy_rate == 5.0
        assert ctx.vix == 22.0
        assert ctx.cpi_yoy is None
        assert ctx.market_regime is None


class TestMacroContextImmutability:
    def test_frozen_raises_on_assignment(self) -> None:
        ctx = MacroContext(policy_rate=5.25)
        with pytest.raises(Exception):
            ctx.policy_rate = 4.0  # type: ignore[misc]

    def test_frozen_raises_on_new_attribute(self) -> None:
        ctx = MacroContext(vix=20.0)
        with pytest.raises(Exception):
            ctx.new_attr = "value"  # type: ignore[attr-defined]


class TestMacroContextValidation:
    def test_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            MacroContext(policy_rate=5.25, unknown_field="oops")  # type: ignore[call-arg]

    def test_rejects_wrong_type_for_float_field(self) -> None:
        with pytest.raises(ValidationError):
            MacroContext(policy_rate="not-a-float")  # type: ignore[arg-type]

    def test_sector_flows_accepts_dict(self) -> None:
        ctx = MacroContext(sector_flows={"XLK": 1.0, "XLE": -2.0})
        assert ctx.sector_flows == {"XLK": 1.0, "XLE": -2.0}


class TestMacroSnapshotInstantiation:
    def test_macro_snapshot_instantiates(self) -> None:
        snap = MacroSnapshot(
            fed_stance="hawkish",
            dollar_signal="USD strengthening on rate expectations",
            geopolitical_notes="Taiwan tensions remain elevated",
            overall_sentiment="risk-on",
            upcoming_events=[
                {"name": "Fed Decision", "date": "2026-06-18"},
                {"name": "CPI Release", "date": "2026-06-12"},
            ],
        )
        assert snap.fed_stance == "hawkish"
        assert snap.dollar_signal == "USD strengthening on rate expectations"
        assert snap.overall_sentiment == "risk-on"
        assert len(snap.upcoming_events) == 2

    def test_macro_snapshot_frozen(self) -> None:
        snap = MacroSnapshot(
            fed_stance="neutral",
            dollar_signal="USD stable",
            geopolitical_notes="No major threats",
            overall_sentiment="neutral",
            upcoming_events=[],
        )
        with pytest.raises(Exception):
            snap.fed_stance = "dovish"  # type: ignore[misc]

    def test_macro_snapshot_rejects_extra_fields(self) -> None:
        with pytest.raises(ValidationError):
            MacroSnapshot(  # type: ignore[call-arg]
                fed_stance="dovish",
                dollar_signal="Weak",
                geopolitical_notes="Notes",
                overall_sentiment="risk-off",
                upcoming_events=[],
                unknown_field="oops",
            )

    def test_macro_snapshot_requires_all_fields(self) -> None:
        with pytest.raises(ValidationError):
            MacroSnapshot(fed_stance="hawkish")  # type: ignore[call-arg]
