from __future__ import annotations

import os


def get_twelve_data_api_key() -> str:
    """Return Twelve Data API key from environment; raise if unset."""
    key = os.getenv("TWELVE_DATA_API_KEY", "")
    if not key:
        raise ValueError("TWELVE_DATA_API_KEY environment variable not set")
    return key
