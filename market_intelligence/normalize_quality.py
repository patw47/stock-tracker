from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from market_intelligence.fetch_eod import fetch_all

logger = logging.getLogger(__name__)

_DATA_DIR = Path("market_intelligence/data/ohlcv")
_QUALITY_REPORT = Path("market_intelligence/data/quality_report.json")
_OHLCV_FLOAT_COLS = ["open", "high", "low", "close"]
_VOLUME_COL = "volume"


def _safe_symbol(symbol: str) -> str:
    return symbol.replace("^", "").replace("=", "_").replace("-", "_").upper()


def normalize(symbol: str, df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Flatten, clean, and type-cast a raw OHLCV DataFrame from fetch_all."""
    if df.empty:
        return pd.DataFrame(), {
            "symbol": symbol,
            "bar_count": 0,
            "date_start": None,
            "date_end": None,
            "short_history": True,
        }

    if isinstance(df.columns, pd.MultiIndex):
        df = df.copy()
        df.columns = df.columns.get_level_values(0)

    col_map = {c: c.lower() for c in df.columns}
    df = df.rename(columns=col_map)

    needed = _OHLCV_FLOAT_COLS + [_VOLUME_COL]
    df = df[[c for c in needed if c in df.columns]].copy()

    df.index = pd.to_datetime(df.index)
    df = df.sort_index()

    df = df.dropna(how="all")

    for col in _OHLCV_FLOAT_COLS:
        if col in df.columns:
            df[col] = df[col].astype("float64")

    if _VOLUME_COL in df.columns:
        df[_VOLUME_COL] = pd.to_numeric(df[_VOLUME_COL], errors="coerce").fillna(0).astype("int64")

    bar_count = len(df)
    date_start = df.index[0].date().isoformat() if bar_count > 0 else None
    date_end = df.index[-1].date().isoformat() if bar_count > 0 else None

    meta: dict = {
        "symbol": symbol,
        "bar_count": bar_count,
        "date_start": date_start,
        "date_end": date_end,
        "short_history": bar_count < 60,
    }
    return df, meta


def run_normalization(days: int = 60) -> list[dict]:
    """Fetch, normalize, persist Parquet files and quality report. Returns list of meta dicts."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)

    raw = fetch_all(days)
    report: list[dict] = []

    for symbol, df in raw.items():
        clean_df, meta = normalize(symbol, df)
        parquet_path = _DATA_DIR / f"{_safe_symbol(symbol)}.parquet"

        if not clean_df.empty:
            clean_df.to_parquet(parquet_path)
            logger.info(
                "Wrote %s (%d bars, %s→%s)",
                parquet_path,
                meta["bar_count"],
                meta["date_start"],
                meta["date_end"],
            )
        else:
            logger.warning("Empty after normalization: %s", symbol)

        report.append(meta)

    _QUALITY_REPORT.write_text(json.dumps(report, indent=2))
    logger.info("Quality report written: %s (%d entries)", _QUALITY_REPORT, len(report))
    return report


if __name__ == "__main__":
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    results = run_normalization()

    col_w = [10, 9, 12, 12, 8]
    header = f"{'Symbol':<{col_w[0]}} {'Bars':>{col_w[1]}} {'Start':<{col_w[2]}} {'End':<{col_w[3]}} {'Short':>{col_w[4]}}"
    print(header)
    print("-" * sum(col_w + [len(col_w) - 1]))
    for m in results:
        print(
            f"{m['symbol']:<{col_w[0]}} "
            f"{m['bar_count']:>{col_w[1]}} "
            f"{str(m['date_start']):<{col_w[2]}} "
            f"{str(m['date_end']):<{col_w[3]}} "
            f"{'yes' if m['short_history'] else 'no':>{col_w[4]}}"
        )
