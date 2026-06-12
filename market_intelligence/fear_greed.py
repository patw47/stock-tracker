from __future__ import annotations

import logging
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_CNN_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
_TIMEOUT = 10
_HEADERS = {"User-Agent": "Mozilla/5.0 stock-tracker/1.0"}


@dataclass(frozen=True)
class FearGreedResult:
    """CNN Fear & Greed index snapshot."""

    score: float
    label: str


def get_fear_greed() -> FearGreedResult | None:
    """Fetch Fear & Greed index from CNN production endpoint.

    Best-effort: returns None on any network or parse failure so the
    macro brief is never blocked by this optional data source.
    """
    try:
        resp = requests.get(_CNN_URL, timeout=_TIMEOUT, headers=_HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("get_fear_greed request failed: %s", exc)
        return None

    try:
        fg = data.get("fear_and_greed", {})
        raw_score = fg.get("score")
        raw_label = fg.get("rating")
        if raw_score is None or raw_label is None:
            logger.warning("get_fear_greed: missing score or rating in response")
            return None
        return FearGreedResult(score=round(float(raw_score), 1), label=str(raw_label))
    except Exception as exc:
        logger.warning("get_fear_greed parse failed: %s", exc)
        return None
