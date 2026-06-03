from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Final

_DEFAULT_LISTS_DIR: Final[Path] = Path(__file__).parent / "data"
LISTS_DIR: Final[Path] = Path(os.getenv("WARREN_DATA_DIR", str(_DEFAULT_LISTS_DIR)))


def load_list(name: str) -> list[str]:
    """Load a ticker list by name ('watchlist' or 'portfolio'). Returns [] if not found."""
    path = _list_path(name)
    if not path.exists():
        return []

    with path.open(encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        msg = f"Ticker list must be a JSON array: {path}"
        raise ValueError(msg)

    return _normalize_tickers(data)


def save_list(name: str, tickers: list[str]) -> None:
    """Persist the ticker list atomically as JSON."""
    path = _list_path(name)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    normalized = _normalize_tickers(tickers)

    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(normalized, file)
        file.write("\n")

    os.replace(tmp_path, path)


def add_ticker(name: str, ticker: str) -> list[str]:
    """Add ticker (uppercased, deduplicated) and persist. Returns updated list."""
    normalized_ticker = _normalize_ticker(ticker)
    tickers = load_list(name)
    if normalized_ticker not in tickers:
        tickers.append(normalized_ticker)
        save_list(name, tickers)
    return tickers


def remove_ticker(name: str, ticker: str) -> tuple[list[str], bool]:
    """Remove ticker (case-insensitive). Returns (updated_list, was_found)."""
    normalized_ticker = _normalize_ticker(ticker)
    tickers = load_list(name)
    updated = [item for item in tickers if item != normalized_ticker]
    was_found = len(updated) != len(tickers)
    if was_found:
        save_list(name, updated)
    return updated, was_found


def _list_path(name: str) -> Path:
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    return LISTS_DIR / f"{name}.json"


def _normalize_tickers(tickers: list[object]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for ticker in tickers:
        if not isinstance(ticker, str):
            msg = f"Ticker values must be strings: {ticker!r}"
            raise ValueError(msg)
        normalized_ticker = _normalize_ticker(ticker)
        if normalized_ticker not in seen:
            normalized.append(normalized_ticker)
            seen.add(normalized_ticker)
    return normalized


def _normalize_ticker(ticker: str) -> str:
    return ticker.upper()
