from __future__ import annotations

import fcntl
import html
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Final

import pandas as pd

logger = logging.getLogger(__name__)

# Layer C — tension. Alerting promoted early (user decision 2026-07-10) while the
# live measurement keeps running (docs/TENSION.md).
# ponytail: thresholds are pre-registered constants, not config — changing them
# mid-validation would invalidate the live test.
SQUEEZE_BW_PCTL: Final[float] = 0.10   # bottom decile of trailing-year bandwidth
QUIET_RVOL5: Final[float] = 2.0        # 5-day mean relative volume
QUIET_CUM5: Final[float] = 0.03        # |5-day cumulative return| ceiling
HORIZON_DAYS: Final[int] = 20          # forward window for the outcome event
EXPECTED_MOVE_MULT: Final[float] = 2.0  # explosion = move > MULT x expected

# Couverture minimale du journal de tension pour qu'une ABSENCE de compression
# soit une information (Epic 10 S3). Dérivée de HORIZON_DAYS : c'est la fenêtre
# sur laquelle l'ISSUE d'un épisode se juge (docs/TENSION.md) — à ne pas confondre
# avec _BW_WINDOW, la fenêtre de mesure de la bande, qui vaut 20 elle aussi. Cette
# durée est donc la mémoire minimale qu'il faut avoir d'un titre pour affirmer
# qu'il ne s'est rien passé : un ticker entré en cohorte la semaine dernière n'a
# aucun historique, et son absence de marque se lirait à tort comme un signal
# négatif.
MIN_TENSION_HISTORY_DAYS: Final[int] = HORIZON_DAYS

# Un journal peut être assez LONG sans être assez DENSE : 20 relevés étalés sur
# trois mois ne disent rien des semaines manquantes. Les jours journalisés sont
# des jours ouvrés (~1,4 jour calendaire chacun) ; au-delà du double, la fenêtre
# est trouée et l'absence de marque redevient une ignorance, pas une information.
_MAX_SPAN_MULT: Final[int] = 2

_BW_WINDOW: Final[int] = 20
_BW_RANK_WINDOW: Final[int] = 252
_BW_RANK_MIN: Final[int] = 120
_VOL_BASE_WINDOW: Final[int] = 20
_ATR_WINDOW: Final[int] = 14
_REQUIRED: Final[tuple[str, ...]] = ("high", "low", "close", "volume")

# Canned plain-language gloss per tension type (Epic 7 S2), one per rendered stat.
_SQUEEZE_GLOSS: Final[str] = (
    "volatilité comprimée dans le pire décile de son année — un mouvement se "
    "prépare, direction inconnue"
)
_QUIET_GLOSS: Final[str] = (
    "volume anormal sans mouvement de prix — accumulation silencieuse possible"
)


@dataclass(frozen=True)
class TensionSignal:
    """Deterministic tension measurements for one ticker's latest EOD bar.

    Same formulas as scripts/tension_backtest.py v1 so the live journal is
    directly comparable to the phase-0 backtest.
    """

    symbol: str
    as_of: str | None
    bar_count: int
    bw_pctl: float | None            # bollinger bandwidth percentile (trailing year)
    rvol5: float | None              # 5-day mean relative volume vs prior 20d
    cum5: float | None               # 5-day cumulative return
    expected_move_20d: float | None  # ATR14/close * sqrt(20), for the outcome event
    squeeze: bool
    quiet_accumulation: bool
    tension: bool
    episode_start: bool              # tension today, not on the previous bar
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _empty(symbol: str, issues: tuple[str, ...]) -> TensionSignal:
    return TensionSignal(
        symbol=symbol, as_of=None, bar_count=0,
        bw_pctl=None, rvol5=None, cum5=None, expected_move_20d=None,
        squeeze=False, quiet_accumulation=False, tension=False,
        episode_start=False, data_issues=issues,
    )


def calculate_tension(symbol: str, df: pd.DataFrame) -> TensionSignal:
    """Compute tension features from normalized EOD bars (trailing-only, no look-ahead)."""
    if df is None or df.empty:
        return _empty(symbol, ("empty_frame",))
    frame = df.copy()
    frame.columns = [str(c).lower() for c in frame.columns]
    missing = [c for c in _REQUIRED if c not in frame.columns]
    if missing:
        return _empty(symbol, tuple(f"missing_column:{c}" for c in missing))
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[~frame.index.isna()]
    frame = frame[~frame.index.duplicated(keep="last")].sort_index()
    if frame.empty:
        return _empty(symbol, ("invalid_date",))

    close = pd.to_numeric(frame["close"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce")
    low = pd.to_numeric(frame["low"], errors="coerce")
    issues: list[str] = []

    bw = close.rolling(_BW_WINDOW).std() / close.rolling(_BW_WINDOW).mean()
    bw_pctl_series = bw.rolling(_BW_RANK_WINDOW, min_periods=_BW_RANK_MIN).rank(pct=True)

    vol_base = volume.rolling(_VOL_BASE_WINDOW).mean().shift(1)
    rvol = volume / vol_base.replace(0, float("nan"))
    rvol5_series = rvol.rolling(5).mean()
    cum5_series = close.pct_change(5)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()],
                   axis=1).max(axis=1)
    atr14 = tr.rolling(_ATR_WINDOW).mean()
    expected_series = (atr14 / close.replace(0, float("nan"))) * math.sqrt(HORIZON_DAYS)

    squeeze_series = bw_pctl_series <= SQUEEZE_BW_PCTL
    quiet_series = (rvol5_series >= QUIET_RVOL5) & (cum5_series.abs() < QUIET_CUM5)
    tension_series = (squeeze_series | quiet_series).fillna(False)

    bw_pctl = _finite(bw_pctl_series.iloc[-1])
    rvol5 = _finite(rvol5_series.iloc[-1])
    cum5 = _finite(cum5_series.iloc[-1])
    expected = _finite(expected_series.iloc[-1])
    if bw_pctl is None:
        issues.append("squeeze_history_short")
    if rvol5 is None or cum5 is None:
        issues.append("volume_history_short")
    if expected is None:
        issues.append("expected_move_unavailable")

    tension = bool(tension_series.iloc[-1])
    prev_tension = bool(tension_series.iloc[-2]) if len(tension_series) >= 2 else False
    return TensionSignal(
        symbol=symbol,
        as_of=frame.index[-1].date().isoformat(),
        bar_count=len(frame),
        bw_pctl=bw_pctl,
        rvol5=rvol5,
        cum5=cum5,
        expected_move_20d=expected,
        squeeze=bool(squeeze_series.iloc[-1]) if bw_pctl is not None else False,
        quiet_accumulation=bool(quiet_series.iloc[-1]) if rvol5 is not None else False,
        tension=tension,
        episode_start=tension and not prev_tension,
        data_issues=tuple(issues),
    )


def calculate_all(frames: dict[str, pd.DataFrame]) -> dict[str, TensionSignal]:
    """One independent tension record per supplied ticker."""
    return {symbol: calculate_tension(symbol, frame) for symbol, frame in frames.items()}


def append_tension_journal(
    signals: dict[str, TensionSignal], path: Path, *, dry_run: bool = False
) -> int:
    """Append one JSONL line per ticker with a dated bar. Returns lines written.

    Readers dedupe on (ticker, as_of) keeping the first record, so re-runs and
    dry-runs of the same day are harmless.
    """
    records = [
        {"timestamp": datetime.now(timezone.utc).isoformat(), "dry_run": dry_run,
         **signal.to_dict()}
        for signal in signals.values()
        if signal.as_of is not None
    ]
    if not records:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = "".join(
        json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n" for record in records
    )
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(lines)
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    return len(records)


def fr_date(iso: str) -> str:
    """``2026-06-24`` → ``24/06``; anything unparseable comes back untouched."""
    try:
        return date.fromisoformat(iso).strftime("%d/%m")
    except (TypeError, ValueError):
        return str(iso)


def load_tension_history(path: Path) -> dict[str, dict[str, bool]]:
    """``{symbol: {as_of: was_an_episode_start}}`` from the journal; ``{}`` if unreadable.

    Fail-soft end to end: the trajectory annotation is a comment on an alert, it
    must never be the reason an alert is not sent. A missing journal, a truncated
    line or a malformed record simply shrink the known history — which the caller
    then reports as "too short to tell", never as "no tension".
    """
    history: dict[str, dict[str, bool]] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        logger.error("tension journal %s unreadable (%s) - trajectory unknown", path, exc)
        return history
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        symbol, as_of = record.get("symbol"), record.get("as_of")
        if not symbol or not as_of:
            continue
        days = history.setdefault(str(symbol), {})
        started = bool(record.get("tension") and record.get("episode_start"))
        # The same bar is journaled by every run of the day (dry-runs included):
        # an episode seen once stays seen.
        days[str(as_of)] = days.get(str(as_of), False) or started
    return history


def _window_span_days(window: list[str]) -> int:
    """Durée calendaire couverte par une fenêtre de jours journalisés (bornes incluses).

    0 pour une fenêtre vide ou de dates illisibles — un span nul ne peut pas
    dépasser le plafond, donc l'inconnu ne dégrade jamais le verdict tout seul.
    """
    try:
        return (date.fromisoformat(window[-1]) - date.fromisoformat(window[0])).days + 1
    except (IndexError, TypeError, ValueError):
        return 0


def tension_trajectory(
    symbol: str,
    history: Mapping[str, Mapping[str, bool]],
    *,
    as_of: str | None,
) -> str:
    """One sentence among THREE states, describing what the journal knows.

    1. a compression episode was recorded before this alert (its date, its lead);
    2. none over a journal both long AND dense enough to conclude;
    3. journal too short, or too sparse to cover its own window — "can't tell".

    State 3 is not a detail: a ticker that joined the cohort last week has no
    history at all, and without it the absence of a mark would read as a negative
    signal it is not. Sparseness is the same trap seen from the other side — a
    journal with holes (EOD job down, ticker in and out of the cohort) would
    otherwise claim "nothing happened" over weeks it never observed. Descriptive
    only — a recorded episode is never presented as the cause of the anomaly (the
    measured gap rests on non-independent observations, epic decision).
    """
    journal = history.get(symbol, {})
    known = sorted(day for day in journal if as_of is None or day < as_of)
    window = known[-MIN_TENSION_HISTORY_DAYS:]
    episodes = [day for day in window if journal[day]]
    if episodes:
        lead = ""
        if as_of:
            try:
                gap = (date.fromisoformat(as_of) - date.fromisoformat(episodes[-1])).days
                lead = f", {gap} jours avant cette alerte"
            except ValueError:
                lead = ""
        return f"🔎 Compression repérée le {fr_date(episodes[-1])}{lead}."
    if len(window) < MIN_TENSION_HISTORY_DAYS:
        return (
            f"🔎 Suivi de compression trop court ({len(window)} jour(s) suivis sur "
            f"{MIN_TENSION_HISTORY_DAYS}) — impossible de dire s'il y a eu compression."
        )
    if _window_span_days(window) > MIN_TENSION_HISTORY_DAYS * _MAX_SPAN_MULT:
        return (
            f"🔎 Suivi de compression trop clairsemé ({len(window)} jours suivis "
            f"depuis le {fr_date(window[0])}) — impossible de dire s'il y a eu "
            "compression."
        )
    return (
        f"🔎 Aucune compression repérée sur les {MIN_TENSION_HISTORY_DAYS} "
        "derniers jours suivis."
    )


def format_tension_digest(
    signals: dict[str, TensionSignal],
    *,
    as_of: str | None,
    provenance: Mapping[str, str] | None = None,
) -> str:
    """Telegram-ready HTML block for episode starts. Deterministic, no LLM.

    One line per new tension episode, empty string when none. Follows the
    anomaly digest HTML conventions (values escaped, each <b> tag within
    one line). ``provenance`` (Epic 7 S1) maps a symbol to its runtime-list
    origin label; ``None`` renders the pre-Epic-7 lines untouched.
    """
    starts = [s for s in signals.values() if s.tension and s.episode_start]
    if not starts:
        return ""
    date_label = html.escape(as_of or "unknown date")
    # Section title says what the section means, not which pipeline stage produced
    # it (Epic 10 S3): these tickers are compressing, the direction is unknown,
    # and the whole tier is an hypothesis still under measurement.
    lines = [
        f"⚡ <b>Titres qui se compriment — {date_label}</b>",
        "",
        "Ces titres se resserrent : un mouvement se prépare, sa direction est "
        "INCONNUE. Ce n'est ni un achat ni une vente — hypothèse en cours de "
        "validation, mesurée épisode par épisode.",
        "",
    ]
    for s in sorted(starts, key=lambda x: x.symbol):
        parts: list[str] = []
        glosses: list[str] = []
        if s.squeeze and s.bw_pctl is not None:
            parts.append(f"squeeze (bw pctl {100 * s.bw_pctl:.0f}%)")
            glosses.append(_SQUEEZE_GLOSS)
        if s.quiet_accumulation and s.rvol5 is not None and s.cum5 is not None:
            parts.append(
                f"accumulation calme (rvol5 {s.rvol5:.1f}, 5j {100 * s.cum5:+.1f}%)"
            )
            glosses.append(_QUIET_GLOSS)
        expected = (
            f" — move attendu 20j ±{100 * s.expected_move_20d:.0f}%"
            if s.expected_move_20d is not None
            else ""
        )
        detail = html.escape("; ".join(parts) + expected)
        origin = (
            ""
            if provenance is None
            else f" ({html.escape(provenance.get(s.symbol, 'registre seul'))})"
        )
        lines.append(f"<b>{html.escape(s.symbol)}</b>{origin}: {detail}")
        lines.extend(f"   ↳ {gloss}" for gloss in glosses)
    return "\n".join(lines)
