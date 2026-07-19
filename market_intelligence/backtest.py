#!/usr/bin/env python3
"""Harness de backtest & calibration des seuils — Epic 5 Sprint 3.

Rejoue le pipeline S0-S5 jour par jour sur l'historique des tickers pour répondre :
combien d'alertes/mois produit une combinaison de seuils, et quelle est la
sensibilité au couple (speculative_z, rearm_z) ?

Sécurité (rules du sprint) :
- **Jamais l'état dedup de prod** : état dedup éphémère (fichiers tmp par
  combinaison), short_interest stub. Le pipeline est 100 % déterministe (zéro
  LLM) depuis Epic 6.
- **Aucun changement de seuil en prod** : les combinaisons sont des overrides
  en mémoire (``dataclasses.replace``), le fichier de config n'est jamais réécrit.
- **No-look-ahead** : chaque jour simulé ne voit que les barres <= ce jour
  (troncature du cache), un seul fetch batch (rate limit yfinance).
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from statistics import median
from typing import Callable

import pandas as pd

from market_intelligence.candidate_alerts import AlertThresholdConfig, load_alert_config
from market_intelligence.dedup_hysteresis import (
    DedupConfig,
    deduplicate_alerts,
    load_dedup_config,
)
from market_intelligence.eod_orchestrator import EodRunResult, run_eod_anomaly_pipeline
from market_intelligence.fetch_eod import fetch_all
from market_intelligence.registry_schema import Registry, load_registry

logger = logging.getLogger(__name__)

DEFAULT_SPECULATIVE_Z = (2.0, 2.5, 3.0)
DEFAULT_REARM_Z = (0.8, 1.0, 1.5)
# Enough history before `start` for the beta gate / breakout windows to warm up.
_WARMUP_DAYS = 320

PipelineRunner = Callable[..., EodRunResult]


@dataclass(frozen=True)
class ComboResult:
    """Backtest outcome for one (speculative_z, rearm_z) combination."""

    speculative_z: float
    rearm_z: float
    months: int
    total_alerts: int
    alerts_per_month: float
    z_median: float | None
    z_max: float | None
    per_ticker: dict[str, int] = field(default_factory=dict)
    per_signal: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "speculative_z": self.speculative_z,
            "rearm_z": self.rearm_z,
            "months": self.months,
            "total_alerts": self.total_alerts,
            "alerts_per_month": round(self.alerts_per_month, 3),
            "z_median": self.z_median,
            "z_max": self.z_max,
            "per_ticker": self.per_ticker,
            "per_signal": self.per_signal,
        }


def default_grid() -> list[tuple[float, float]]:
    """Return the default (speculative_z, rearm_z) grid."""
    return [(sz, rz) for sz in DEFAULT_SPECULATIVE_Z for rz in DEFAULT_REARM_Z]


def _no_short_interest(_registry: Registry) -> dict:
    """Short-interest stub — no network, neutral (no squeeze) for every ticker."""
    return {}


def _ephemeral_deduplicator(state_path: Path, dedup_config: DedupConfig):
    """Return a deduplicator bound to an ephemeral state file.

    The pipeline passes ``readonly=True`` (dry-run) and no pending path; both are
    ignored here so hysteresis (rearm/escalation) evolves read-write across the
    simulated days — but only inside ``state_path`` (tmp), never prod state.
    """
    def _dedup(decisions, short_interest, *, readonly=False, pending_path=None,
               run_id=None, run_as_of=None, suppressions=None):
        return deduplicate_alerts(
            decisions,
            short_interest,
            state_path=state_path,
            config=dedup_config,
            readonly=False,
            run_id="backtest",
            run_as_of=run_as_of,
            suppressions=suppressions,
        )
    return _dedup


def _truncating_fetcher(frames: dict[str, pd.DataFrame], as_of: date):
    """Return a frame_fetcher yielding only bars dated <= ``as_of`` (no look-ahead)."""
    cutoff = pd.Timestamp(as_of)

    def _fetch(_history_days: int) -> dict[str, pd.DataFrame]:
        truncated: dict[str, pd.DataFrame] = {}
        for symbol, frame in frames.items():
            if frame is None or frame.empty:
                truncated[symbol] = pd.DataFrame()
                continue
            index = pd.to_datetime(frame.index)
            truncated[symbol] = frame[index <= cutoff]
        return truncated

    return _fetch


def trading_days(frames: dict[str, pd.DataFrame], start: date, end: date) -> list[date]:
    """Union of bar dates across frames within [start, end], sorted ascending."""
    days: set[date] = set()
    for frame in frames.values():
        if frame is None or frame.empty:
            continue
        for ts in pd.to_datetime(frame.index):
            d = ts.date()
            if start <= d <= end:
                days.add(d)
    return sorted(days)


def _month_count(days: list[date]) -> int:
    return len({(d.year, d.month) for d in days}) or 1


def simulate_combo(
    frames: dict[str, pd.DataFrame],
    days: list[date],
    *,
    speculative_z: float,
    rearm_z: float,
    state_path: Path,
    base_alert_config: AlertThresholdConfig,
    base_dedup_config: DedupConfig,
    registry: Registry | None,
    pipeline_runner: PipelineRunner = run_eod_anomaly_pipeline,
) -> ComboResult:
    """Replay every simulated day for one threshold combination."""
    alert_config = replace(base_alert_config, speculative_residual_z=speculative_z)
    dedup_config = replace(base_dedup_config, rearm_z=rearm_z)
    if state_path.exists():
        state_path.unlink()

    per_ticker: Counter[str] = Counter()
    per_signal: Counter[str] = Counter()
    z_values: list[float] = []
    total_alerts = 0

    for day in days:
        result = pipeline_runner(
            # The truncating fetcher ignores history_days; kept for signature parity.
            history_days=_WARMUP_DAYS + len(days),
            registry=registry,
            frame_fetcher=_truncating_fetcher(frames, day),
            short_interest_fetcher=_no_short_interest,
            deduplicator=_ephemeral_deduplicator(state_path, dedup_config),
            dry_run=True,
            skip_warren=True,
            journal_path=None,
            alert_config=alert_config,
        )
        for detail in result.candidates_detail:
            if detail.outcome != "survived":
                continue
            total_alerts += 1
            per_ticker[detail.ticker] += 1
            for signal in detail.signal_types:
                per_signal[signal] += 1
            if isinstance(detail.z_resid, (int, float)):
                z_values.append(float(detail.z_resid))

    months = _month_count(days)
    return ComboResult(
        speculative_z=speculative_z,
        rearm_z=rearm_z,
        months=months,
        total_alerts=total_alerts,
        alerts_per_month=total_alerts / months,
        z_median=median(z_values) if z_values else None,
        z_max=max(z_values) if z_values else None,
        per_ticker=dict(per_ticker.most_common()),
        per_signal=dict(per_signal.most_common()),
    )


def run_backtest(
    frames: dict[str, pd.DataFrame],
    start: date,
    end: date,
    *,
    grid: list[tuple[float, float]] | None = None,
    state_dir: Path,
    registry: Registry | None = None,
    pipeline_runner: PipelineRunner = run_eod_anomaly_pipeline,
) -> list[ComboResult]:
    """Run the backtest over the grid; returns one ComboResult per combination."""
    grid = grid or default_grid()
    days = trading_days(frames, start, end)
    base_alert_config = load_alert_config()
    base_dedup_config = load_dedup_config()
    state_dir.mkdir(parents=True, exist_ok=True)

    results: list[ComboResult] = []
    for speculative_z, rearm_z in grid:
        state_path = state_dir / f"dedup_bt_{speculative_z}_{rearm_z}.json"
        results.append(simulate_combo(
            frames, days,
            speculative_z=speculative_z, rearm_z=rearm_z,
            state_path=state_path,
            base_alert_config=base_alert_config,
            base_dedup_config=base_dedup_config,
            registry=registry,
            pipeline_runner=pipeline_runner,
        ))
    return results


def format_summary(results: list[ComboResult]) -> str:
    """Render a readable calibration summary, sorted by alerts/mois."""
    lines = ["Backtest calibration — alertes/mois par (speculative_z, rearm_z) :"]
    for combo in sorted(results, key=lambda c: c.alerts_per_month, reverse=True):
        top = ", ".join(f"{t}={n}" for t, n in list(combo.per_ticker.items())[:3]) or "—"
        zmed = f"{combo.z_median:.2f}" if combo.z_median is not None else "—"
        lines.append(
            f"  spec_z={combo.speculative_z} rearm={combo.rearm_z} : "
            f"{combo.alerts_per_month:.2f}/mois ({combo.total_alerts} sur {combo.months} mois), "
            f"z méd={zmed}, top: {top}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: `python3 -m market_intelligence.backtest --start ... --end ...`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Harness de backtest & calibration des seuils.")
    parser.add_argument("--start", required=True, help="Date de début YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Date de fin YYYY-MM-DD")
    parser.add_argument("--out", default="runtime/market_intelligence/backtest.jsonl",
                        help="Fichier JSONL de sortie")
    parser.add_argument("--state-dir", default=None,
                        help="Répertoire des états dedup éphémères (défaut: tmp)")
    args = parser.parse_args(argv)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    lookback = (datetime.now(timezone.utc).date() - start).days + _WARMUP_DAYS
    logger.info("Fetch batch unique (%d jours) — no refetch ensuite", lookback)
    frames = fetch_all(days=lookback)

    import tempfile
    state_dir = Path(args.state_dir) if args.state_dir else Path(tempfile.mkdtemp(prefix="bt_dedup_"))
    results = run_backtest(frames, start, end, state_dir=state_dir)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        "\n".join(json.dumps(c.to_dict(), ensure_ascii=False, sort_keys=True) for c in results) + "\n",
        encoding="utf-8",
    )
    print(format_summary(results))
    print(f"\nJSONL détaillé : {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
