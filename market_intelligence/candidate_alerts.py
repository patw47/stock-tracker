from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Literal

from market_intelligence.anomaly_signals import AnomalySignals
from market_intelligence.beta_gate import RETURN_WINDOW, BetaGateResult
from market_intelligence.registry_schema import load_registry

Classification = Literal["calm", "speculative"]
Direction = Literal["up", "down"]

_CONFIG_PATH: Final[Path] = Path(__file__).parent / "data" / "alert_thresholds.json"
_VALID_CLASSIFICATIONS: Final[set[str]] = {"calm", "speculative"}
_FATAL_ISSUE_MARKERS: Final[tuple[str, ...]] = (
    "ambiguous",
    "quarantine",
    "name_mismatch",
)
_INVALID_BETA_FALLBACK_ISSUES: Final[set[str]] = {
    "missing_stock_frame",
    "missing_market_frame",
    "stock_return_date_missing",
    "market_return_date_missing",
    "signal_frame_return_mismatch",
    "factor_return_date_missing",
    "singular_regression",
    "zero_residual_scale",
}


class CandidateAlertError(Exception):
    """Base error for Sprint 3 candidate alert decisions."""


class AlertConfigError(CandidateAlertError):
    """Raised when candidate alert threshold configuration is invalid."""


@dataclass(frozen=True)
class AlertThresholdConfig:
    """Define deterministic Sprint 3 thresholds and ticker classifications."""

    calm_residual_z: float
    speculative_residual_z: float
    volume_z: float
    rvol: float
    atr_expansion: float
    short_history_multiplier: float
    short_history_return: float
    breakout_min_window: int
    classifications: dict[str, Classification]

    def residual_threshold(self, symbol: str) -> float:
        """Return the configured residual threshold for a ticker."""
        classification = self.classifications.get(symbol)
        if classification is None:
            raise AlertConfigError(f"Missing alert classification for {symbol}")
        return (
            self.calm_residual_z
            if classification == "calm"
            else self.speculative_residual_z
        )


@dataclass(frozen=True)
class CandidateAlert:
    """Represent one deterministic Sprint 3 candidate alert decision."""

    ticker: str
    as_of: str | None
    classification: Classification | None
    eligible: bool
    is_candidate: bool
    direction: Direction | None
    signal_types: tuple[str, ...]
    z_resid: float | None
    residual_threshold: float | None
    short_history_fallback_applied: bool
    data_issues: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""
        return asdict(self)


def _positive_finite(name: str, value: object, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AlertConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= minimum:
        raise AlertConfigError(f"{name} must be greater than {minimum}")
    return number


def _positive_integer(name: str, value: object) -> int:
    number = _positive_finite(name, value)
    if not number.is_integer():
        raise AlertConfigError(f"{name} must be an integer")
    return int(number)


def load_alert_config(
    path: Path = _CONFIG_PATH,
    classifications_path: Path | None = None,
) -> AlertThresholdConfig:
    """Load and validate ticker classifications and Sprint 3 thresholds.

    Two files since Epic 10 S4, one per side of the split: the thresholds are
    configuration (versioned, reviewed in a PR), the per-symbol classifications are
    state that follows the cohort. An absent state file is an empty classification
    table — the fresh-machine case — while absent or malformed thresholds still
    raise: a threshold is never optional.
    """
    from market_intelligence.registry_check import CLASSIFICATIONS_PATH, load_state

    raw = json.loads(path.read_text(encoding="utf-8"))
    thresholds = raw.get("thresholds")
    state = load_state(classifications_path or CLASSIFICATIONS_PATH)
    classifications = state.get("classifications", {})
    if not isinstance(thresholds, dict) or not isinstance(classifications, dict):
        raise AlertConfigError("Config requires thresholds and classifications objects")

    parsed_classifications: dict[str, Classification] = {}
    for symbol, classification in classifications.items():
        if classification not in _VALID_CLASSIFICATIONS:
            raise AlertConfigError(f"Invalid classification for {symbol}: {classification}")
        parsed_classifications[str(symbol)] = classification

    config = AlertThresholdConfig(
        calm_residual_z=_positive_finite(
            "calm_residual_z", thresholds.get("calm_residual_z")
        ),
        speculative_residual_z=_positive_finite(
            "speculative_residual_z", thresholds.get("speculative_residual_z")
        ),
        volume_z=_positive_finite("volume_z", thresholds.get("volume_z")),
        rvol=_positive_finite("rvol", thresholds.get("rvol")),
        atr_expansion=_positive_finite(
            "atr_expansion", thresholds.get("atr_expansion")
        ),
        short_history_multiplier=_positive_finite(
            "short_history_multiplier",
            thresholds.get("short_history_multiplier"),
            minimum=1.0,
        ),
        short_history_return=_positive_finite(
            "short_history_return", thresholds.get("short_history_return")
        ),
        breakout_min_window=_positive_integer(
            "breakout_min_window", thresholds.get("breakout_min_window")
        ),
        classifications=parsed_classifications,
    )
    if config.speculative_residual_z <= config.calm_residual_z:
        raise AlertConfigError(
            "speculative_residual_z must exceed calm_residual_z"
        )
    return config


def _merged_issues(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(issue for group in groups for issue in group))


def _has_fatal_issue(issues: tuple[str, ...]) -> bool:
    return any(marker in issue for issue in issues for marker in _FATAL_ISSUE_MARKERS)


def _direction(
    signal: AnomalySignals,
    gate: BetaGateResult,
    *,
    residual_crossed: bool,
    short_history_return_crossed: bool,
    breakout_high_crossed: bool,
    breakout_low_crossed: bool,
) -> Direction | None:
    if residual_crossed:
        value = gate.z_resid
    elif short_history_return_crossed:
        value = signal.daily_return
    elif breakout_high_crossed != breakout_low_crossed:
        return "up" if breakout_high_crossed else "down"
    else:
        value = signal.daily_return
    if value is None or value == 0:
        return None
    return "up" if value > 0 else "down"


def _can_use_short_history_fallback(gate: BetaGateResult) -> bool:
    return (
        gate.model_used is None
        and gate.z_resid is None
        and gate.observation_count > 0
        and "insufficient_history" in gate.data_issues
        and not any(issue in _INVALID_BETA_FALLBACK_ISSUES for issue in gate.data_issues)
    )


def _unclassified(
    ticker: str,
    issues: tuple[str, ...],
    *,
    as_of: str | None = None,
    classification: Classification | None = None,
    z_resid: float | None = None,
) -> CandidateAlert:
    return CandidateAlert(
        ticker=ticker,
        as_of=as_of,
        classification=classification,
        eligible=False,
        is_candidate=False,
        direction=None,
        signal_types=(),
        z_resid=z_resid,
        residual_threshold=None,
        short_history_fallback_applied=False,
        data_issues=issues,
    )


def evaluate_candidate(
    signal: AnomalySignals,
    gate: BetaGateResult,
    config: AlertThresholdConfig | None = None,
    *,
    expected_as_of: str,
) -> CandidateAlert:
    """Apply Sprint 3 thresholds to matching Sprint 1 and Sprint 2 records."""
    alert_config = config or load_alert_config()
    classification = alert_config.classifications.get(signal.symbol)
    issues = _merged_issues(signal.data_issues, gate.data_issues)
    if signal.volume_window == 0:
        issues = _merged_issues(issues, ("volume_history_unavailable",))
    if signal.extrema_window < alert_config.breakout_min_window:
        issues = _merged_issues(issues, ("breakout_history_short",))

    if classification is None:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("alert_classification_missing",)),
            as_of=signal.as_of,
        )
    if signal.symbol != gate.symbol:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("signal_gate_symbol_mismatch",)),
            as_of=signal.as_of,
            classification=classification,
        )
    if signal.as_of is None or signal.as_of != gate.as_of:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("signal_gate_date_mismatch",)),
            as_of=signal.as_of,
            classification=classification,
            z_resid=gate.z_resid,
        )
    if signal.as_of != expected_as_of:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("stale_signal",)),
            as_of=signal.as_of,
            classification=classification,
            z_resid=gate.z_resid,
        )
    if _has_fatal_issue(issues):
        return _unclassified(
            signal.symbol,
            issues,
            as_of=signal.as_of,
            classification=classification,
            z_resid=gate.z_resid,
        )
    short_history_gate_fallback = _can_use_short_history_fallback(gate)
    if (gate.model_used is None or gate.z_resid is None) and not short_history_gate_fallback:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("beta_gate_unavailable",)),
            as_of=signal.as_of,
            classification=classification,
            z_resid=gate.z_resid,
        )

    residual_fallback = short_history_gate_fallback or (
        0 < gate.observation_count < RETURN_WINDOW
    )
    volume_fallback = 0 < signal.volume_window < 20
    residual_threshold = alert_config.residual_threshold(signal.symbol)
    if residual_fallback:
        residual_threshold *= alert_config.short_history_multiplier
    volume_z_threshold = alert_config.volume_z
    rvol_threshold = alert_config.rvol
    if volume_fallback:
        volume_z_threshold *= alert_config.short_history_multiplier
        rvol_threshold *= alert_config.short_history_multiplier

    signal_types: list[str] = []
    residual_crossed = gate.z_resid is not None and abs(gate.z_resid) > residual_threshold
    if residual_crossed:
        signal_types.append("residual_z")
    short_history_return_crossed = (
        short_history_gate_fallback
        and signal.daily_return is not None
        and abs(signal.daily_return) > alert_config.short_history_return
    )
    if short_history_return_crossed:
        signal_types.append("short_history_return")

    volume_z_crossed = (
        signal.log_volume_z is not None and signal.log_volume_z > volume_z_threshold
    )
    if volume_z_crossed:
        signal_types.append("volume_z")
    rvol_crossed = signal.rvol is not None and signal.rvol > rvol_threshold
    if rvol_crossed:
        signal_types.append("rvol")

    atr_crossed = (
        signal.atr_expansion_ratio is not None
        and signal.atr_expansion_ratio > alert_config.atr_expansion
    )
    if atr_crossed:
        signal_types.append("atr_expansion")
    breakout_history_eligible = (
        signal.extrema_window >= alert_config.breakout_min_window
    )
    breakout_high_crossed = (
        breakout_history_eligible and signal.breakout_high_52w is True
    )
    breakout_low_crossed = (
        breakout_history_eligible and signal.breakout_low_52w is True
    )
    if breakout_high_crossed:
        signal_types.append("breakout_high_52w")
    if breakout_low_crossed:
        signal_types.append("breakout_low_52w")

    volume_crossed = volume_z_crossed or rvol_crossed
    secondary_crossed = (
        atr_crossed
        or breakout_high_crossed
        or breakout_low_crossed
    )
    standard_candidate = residual_crossed or (volume_crossed and secondary_crossed)
    fallback_candidate = (
        short_history_return_crossed and volume_crossed and secondary_crossed
    )
    is_candidate = standard_candidate if not short_history_gate_fallback else fallback_candidate
    direction = (
        _direction(
            signal,
            gate,
            residual_crossed=residual_crossed,
            short_history_return_crossed=short_history_return_crossed,
            breakout_high_crossed=breakout_high_crossed,
            breakout_low_crossed=breakout_low_crossed,
        )
        if is_candidate
        else None
    )
    if is_candidate and direction is None:
        return _unclassified(
            signal.symbol,
            _merged_issues(issues, ("direction_unavailable",)),
            as_of=signal.as_of,
            classification=classification,
            z_resid=gate.z_resid,
        )

    return CandidateAlert(
        ticker=signal.symbol,
        as_of=signal.as_of,
        classification=classification,
        eligible=True,
        is_candidate=is_candidate,
        direction=direction,
        signal_types=tuple(signal_types),
        z_resid=gate.z_resid,
        residual_threshold=residual_threshold,
        short_history_fallback_applied=residual_fallback or volume_fallback,
        data_issues=issues,
    )


def evaluate_all(
    signals: dict[str, AnomalySignals],
    gates: dict[str, BetaGateResult],
    config: AlertThresholdConfig | None = None,
    *,
    expected_as_of: str,
    expected_symbols: tuple[str, ...] | None = None,
) -> dict[str, CandidateAlert]:
    """Return an explicit Sprint 3 decision for every configured ticker."""
    alert_config = config or load_alert_config()
    symbols = expected_symbols
    if symbols is None:
        symbols = tuple(entry.symbol for entry in load_registry().portfolio_tickers)
    decisions: dict[str, CandidateAlert] = {}
    for symbol in symbols:
        classification = alert_config.classifications.get(symbol)
        if classification is None:
            decisions[symbol] = _unclassified(
                symbol, ("alert_classification_missing",)
            )
            continue
        signal = signals.get(symbol)
        gate = gates.get(symbol)
        if signal is None or gate is None:
            missing = tuple(
                issue
                for condition, issue in (
                    (signal is None, "missing_s1_signal"),
                    (gate is None, "missing_s2_gate"),
                )
                if condition
            )
            decisions[symbol] = _unclassified(
                symbol, missing, classification=classification
            )
            continue
        decisions[symbol] = evaluate_candidate(
            signal, gate, alert_config, expected_as_of=expected_as_of
        )
    return decisions


def select_candidates(
    signals: dict[str, AnomalySignals],
    gates: dict[str, BetaGateResult],
    config: AlertThresholdConfig | None = None,
    *,
    expected_as_of: str,
    expected_symbols: tuple[str, ...] | None = None,
) -> tuple[CandidateAlert, ...]:
    """Return only triggered candidate alerts while preserving config order."""
    return tuple(
        decision
        for decision in evaluate_all(
            signals,
            gates,
            config,
            expected_as_of=expected_as_of,
            expected_symbols=expected_symbols,
        ).values()
        if decision.is_candidate
    )
