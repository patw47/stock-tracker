from __future__ import annotations

import logging

import pandas as pd
import requests
import yfinance as yf

from market_intelligence.registry_schema import load_quarantine, load_registry

logger = logging.getLogger(__name__)

_TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"
_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def _fetch_yfinance(api_symbol: str, days: int) -> pd.DataFrame:
    df = yf.download(
        api_symbol,
        period=f"{days}d",
        interval="1d",
        progress=False,
        auto_adjust=True,
    )
    if df.empty:
        return pd.DataFrame()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)
    available = [c for c in _OHLCV_COLS if c in df.columns]
    return df[available]


def _fetch_twelve_data(api_symbol: str, days: int) -> pd.DataFrame:
    from market_intelligence.config import get_twelve_data_api_key

    resp = requests.get(
        _TWELVE_DATA_URL,
        params={
            "symbol": api_symbol,
            "interval": "1day",
            "outputsize": str(days),
            "apikey": get_twelve_data_api_key(),
        },
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("status") == "error":
        raise ValueError(data.get("message", "Twelve Data API error"))
    values = data.get("values", [])
    if not values:
        return pd.DataFrame()
    df = pd.DataFrame(values)
    df["Date"] = pd.to_datetime(df["datetime"])
    df = df.set_index("Date").sort_index()
    df["Open"] = df["open"].astype(float)
    df["High"] = df["high"].astype(float)
    df["Low"] = df["low"].astype(float)
    df["Close"] = df["close"].astype(float)
    df["Volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    return df[_OHLCV_COLS]


def fetch_all(days: int = 60) -> dict[str, pd.DataFrame]:
    """
    Returns {canonical_symbol: DataFrame} for all non-quarantined tickers.
    DataFrame columns: Open, High, Low, Close, Volume (float64 / int64)
    DataFrame index: pd.DatetimeIndex (date, UTC-naive)
    Skips quarantined symbols silently (logs a warning).
    """
    registry = load_registry()
    quarantined = {e.symbol for e in load_quarantine()}
    results: dict[str, pd.DataFrame] = {}

    for entry in registry.all_tickers():
        if entry.symbol in quarantined:
            logger.warning("Skipping quarantined ticker: %s", entry.symbol)
            continue

        api_symbol = registry.resolve_api_symbol(entry.symbol)
        df: pd.DataFrame = pd.DataFrame()

        try:
            df = _fetch_yfinance(api_symbol, days)
            if len(df) > 0:
                logger.debug("yfinance OK: %s (%d rows)", entry.symbol, len(df))
        except Exception as exc:
            logger.warning("yfinance failed for %s: %s", entry.symbol, str(exc)[:200])

        if len(df) == 0:
            try:
                df = _fetch_twelve_data(api_symbol, days)
                if len(df) > 0:
                    logger.debug("Twelve Data OK: %s (%d rows)", entry.symbol, len(df))
                else:
                    logger.warning("Both sources empty for %s", entry.symbol)
            except Exception as exc:
                logger.warning("Twelve Data failed for %s: %s", entry.symbol, str(exc)[:200])
                df = pd.DataFrame()

        results[entry.symbol] = df

    return results


def fetch_symbols(symbols: list[str], days: int = 280) -> dict[str, pd.DataFrame]:
    """Batch OHLCV for arbitrary symbols (watchlist tension tier — Layer C).

    One batched yfinance call (threads). No registry, no quarantine, no Twelve
    Data fallback: a wrong/delisted symbol yields an empty frame, which the
    tension layer degrades gracefully (``empty_frame`` issue, never journaled).
    """
    if not symbols:
        return {}
    df = yf.download(
        list(symbols),
        period=f"{days}d",
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="ticker",
        threads=True,
    )
    results: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        if isinstance(df.columns, pd.MultiIndex):
            sub = df[symbol] if symbol in df.columns.get_level_values(0) else pd.DataFrame()
        else:
            sub = df  # single-symbol download: flat columns
        sub = sub.dropna(how="all")
        available = [c for c in _OHLCV_COLS if c in sub.columns]
        results[symbol] = sub[available] if not sub.empty else pd.DataFrame()
    return results


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data = fetch_all()
    print(f"\n{'Symbol':<10} {'Rows':>6}  Date range")
    print("-" * 50)
    for symbol, df in data.items():
        if df.empty:
            print(f"{symbol:<10} {'0':>6}  (no data)")
        else:
            start = df.index.min().date()
            end = df.index.max().date()
            print(f"{symbol:<10} {len(df):>6}  {start} → {end}")
