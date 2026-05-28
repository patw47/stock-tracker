from __future__ import annotations

from datetime import date

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
    snapshot_date: date | None = Field(
        default=None,
        description="Date the snapshot represents (not fetch time).",
    )
