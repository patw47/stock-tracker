from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MacroContext(BaseModel):
    """Snapshot of macro indicators feeding Warren's market analysis.

    All fields optional so a provider can return partial data without
    raising when one source (e.g. sector flows) is unavailable.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_rate: float | None = Field(
        default=None,
        description="Central bank policy rate, percent (e.g. Fed funds target).",
    )
    cpi_yoy: float | None = Field(
        default=None,
        description="Headline CPI year-over-year change, percent.",
    )
    pce_yoy: float | None = Field(
        default=None,
        description="Core PCE year-over-year change, percent.",
    )
    yield_curve_spread_10y2y: float | None = Field(
        default=None,
        description="10Y minus 2Y Treasury yield spread, percent.",
    )
    vix: float | None = Field(
        default=None,
        description="CBOE VIX closing level.",
    )
    sector_flows: dict[str, float] | None = Field(
        default=None,
        description="Net flow per sector, currency-agnostic (e.g. {'XLK': 1.2e9}).",
    )
    central_bank_tone: str | None = Field(
        default=None,
        description="Qualitative tone tag: 'hawkish' | 'dovish' | 'neutral' | free text.",
    )
    ten_year_yield: float | None = Field(
        default=None,
        description="10-Year Treasury constant maturity yield, percent.",
    )
    two_year_yield: float | None = Field(
        default=None,
        description="2-Year Treasury constant maturity yield, percent.",
    )
    hy_spread: float | None = Field(
        default=None,
        description="ICE BofA US High Yield Option-Adjusted Spread, percent (FRED: BAMLH0A0HYM2).",
    )
    ig_spread: float | None = Field(
        default=None,
        description="ICE BofA US Corporate IG Option-Adjusted Spread, percent (FRED: BAMLC0A0CM).",
    )
    dollar_index: float | None = Field(
        default=None,
        description="Trade-weighted broad nominal US dollar index (FRED: DTWEXBGS).",
    )
    unemployment_rate: float | None = Field(
        default=None,
        description="US civilian unemployment rate, percent (FRED: UNRATE).",
    )
    spx_level: float | None = Field(
        default=None,
        description="S&P 500 index closing level (FRED: SP500).",
    )
    spx_pct_change_1m: float | None = Field(
        default=None,
        description="S&P 500 approximate 1-month percent change (last ~22 trading days).",
    )
    market_regime: str | None = Field(
        default=None,
        description="Qualitative market regime: 'risk_on' | 'neutral' | 'risk_off' | 'crisis' | 'unknown'.",
    )
    as_of: datetime | None = Field(
        default=None,
        description="UTC datetime when snapshot was fetched.",
    )
    snapshot_date: date | None = Field(
        default=None,
        description="Date the snapshot represents (not fetch time).",
    )


class UpcomingEvent(BaseModel):
    """Scheduled economic or geopolitical event."""

    name: str
    date: str


class MacroSnapshot(BaseModel):
    """Qualitative macro snapshot for market narrative synthesis.

    Contains only directional, human-readable macro context — no raw numbers.
    This design ensures Warren synthesizes market sentiment from forward-looking
    signals (Fed tone, geopolitical risks, upcoming events) rather than
    mechanically digesting CPI/PCE figures.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fed_stance: Literal["hawkish", "dovish", "neutral"]
    dollar_signal: str = Field(
        description="Short directional description of USD strength/weakness, no raw numbers."
    )
    geopolitical_notes: str = Field(
        description="Summary of active geopolitical risks and their market relevance."
    )
    overall_sentiment: Literal["risk-on", "risk-off", "neutral"]
    upcoming_events: list[UpcomingEvent]
