from __future__ import annotations

import json
import logging
import math
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Final, Iterator, Literal

import fcntl

from market_intelligence.candidate_alerts import CandidateAlert, Direction
from market_intelligence.short_interest import ShortInterestResult

logger = logging.getLogger(__name__)

FireReason = Literal[
    "initial",
    "direction_reversal",
    "new_signal_type",
    "escalation",
]

_CONFIG_PATH: Final[Path] = Path(__file__).parent / "data" / "dedup_thresholds.json"
_REPO_ROOT: Final[Path] = Path(__file__).parent.parent
_CONFIGURED_STATE_PATH: Final[Path] = Path(
    os.getenv("ANOMALY_DEDUP_STATE_PATH", "runtime/market_intelligence/dedup_state.json")
).expanduser()
_DEFAULT_STATE_PATH: Final[Path] = (
    _CONFIGURED_STATE_PATH
    if _CONFIGURED_STATE_PATH.is_absolute()
    else _REPO_ROOT / _CONFIGURED_STATE_PATH
)
_SCHEMA_VERSION: Final[int] = 1
_SQUEEZE_SIGNAL: Final[str] = "squeeze_prone"
_VALID_DIRECTIONS: Final[set[str]] = {"up", "down"}
_READONLY_ENV_VAR: Final[str] = "ANOMALY_DEDUP_READONLY"
_TRUTHY_ENV_VALUES: Final[set[str]] = {"1", "true", "yes", "on"}


def dedup_readonly_env() -> bool:
    """Return True when ANOMALY_DEDUP_READONLY requests read-only dedup."""
    value = os.getenv(_READONLY_ENV_VAR)
    return value is not None and value.strip().lower() in _TRUTHY_ENV_VALUES


class DedupError(Exception):
    """Base error for Sprint 5 alert deduplication."""


class DedupConfigError(DedupError):
    """Raised when deduplication configuration is invalid."""


class DedupStateError(DedupError):
    """Raised when persistent deduplication state is invalid."""


class DedupInputError(DedupError):
    """Raised when an eligible Sprint 3 decision is malformed."""


@dataclass(frozen=True)
class DedupConfig:
    """Define deterministic Sprint 5 hysteresis thresholds."""

    rearm_z: float
    escalation_z_delta: float
    max_latch_observations: int


@dataclass(frozen=True)
class TickerDedupState:
    """Represent persistent hysteresis state for one ticker."""

    last_observed_as_of: str
    last_alert_as_of: str | None
    latched_since: str | None
    latched: bool
    direction: Direction | None
    trigger_z_resid: float | None
    seen_signal_types: tuple[str, ...]
    latch_observations: int


@dataclass(frozen=True)
class DeduplicatedAlert:
    """Represent a genuinely new alert that may proceed to the digest."""

    candidate: CandidateAlert
    squeeze_prone: bool | None
    fire_reason: FireReason
    signal_types: tuple[str, ...]
    # On an escalation, the |z| level that triggered the *previous* alert, so the
    # digest can say "il avait déclenché à −2,4". None for every other fire_reason.
    prev_trigger_z_resid: float | None = None


SuppressionReason = Literal[
    "already_observed",
    "below_rearm",
    "max_latch_refresh",
    "latched_no_override",
]


@dataclass(frozen=True)
class SuppressionDetail:
    """Explain why an eligible candidate was gated by dedup (Epic 2 observability)."""

    ticker: str
    z_resid: float | None
    reason: SuppressionReason


def _record_suppression(
    suppressions: list[SuppressionDetail] | None,
    candidate: CandidateAlert,
    reason: SuppressionReason,
) -> None:
    """Append a suppression detail for a gated candidate (only real candidates)."""
    if suppressions is not None and candidate.is_candidate:
        suppressions.append(SuppressionDetail(candidate.ticker, candidate.z_resid, reason))


def _positive_finite(name: str, value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DedupConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number <= 0:
        raise DedupConfigError(f"{name} must be greater than 0")
    return number


def load_dedup_config(path: Path = _CONFIG_PATH) -> DedupConfig:
    """Load and validate Sprint 5 hysteresis thresholds."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DedupConfigError(f"Unable to load dedup config: {path}") from exc
    thresholds = raw.get("thresholds")
    if not isinstance(thresholds, dict):
        raise DedupConfigError("Config requires a thresholds object")
    max_latch = thresholds.get("max_latch_observations")
    if not isinstance(max_latch, int) or isinstance(max_latch, bool) or max_latch <= 0:
        raise DedupConfigError("max_latch_observations must be a positive integer")
    return DedupConfig(
        rearm_z=_positive_finite("rearm_z", thresholds.get("rearm_z")),
        escalation_z_delta=_positive_finite(
            "escalation_z_delta", thresholds.get("escalation_z_delta")
        ),
        max_latch_observations=max_latch,
    )


def _valid_date(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise DedupStateError(f"{name} must be an ISO date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise DedupStateError(f"{name} must be an ISO date") from exc
    return value


def _optional_date(value: object, name: str) -> str | None:
    return None if value is None else _valid_date(value, name)


def _optional_finite(value: object, name: str) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise DedupStateError(f"{name} must be finite or null") from exc
    if not math.isfinite(number):
        raise DedupStateError(f"{name} must be finite or null")
    return number


def _parse_state(ticker: str, raw: object) -> TickerDedupState:
    if not isinstance(raw, dict):
        raise DedupStateError(f"State for {ticker} must be an object")
    direction = raw.get("direction")
    if direction is not None and direction not in _VALID_DIRECTIONS:
        raise DedupStateError(f"Invalid direction for {ticker}")
    signal_types = raw.get("seen_signal_types")
    if not isinstance(signal_types, list) or not all(
        isinstance(signal, str) and signal for signal in signal_types
    ):
        raise DedupStateError(f"Invalid signal types for {ticker}")
    observations = raw.get("latch_observations")
    if (
        not isinstance(observations, int)
        or isinstance(observations, bool)
        or observations < 0
    ):
        raise DedupStateError(f"Invalid latch observations for {ticker}")
    latched = raw.get("latched")
    if not isinstance(latched, bool):
        raise DedupStateError(f"Invalid latched flag for {ticker}")
    state = TickerDedupState(
        last_observed_as_of=_valid_date(
            raw.get("last_observed_as_of"), f"{ticker}.last_observed_as_of"
        ),
        last_alert_as_of=_optional_date(
            raw.get("last_alert_as_of"), f"{ticker}.last_alert_as_of"
        ),
        latched_since=_optional_date(raw.get("latched_since"), f"{ticker}.latched_since"),
        latched=latched,
        direction=direction,
        trigger_z_resid=_optional_finite(
            raw.get("trigger_z_resid"), f"{ticker}.trigger_z_resid"
        ),
        seen_signal_types=tuple(sorted(set(signal_types))),
        latch_observations=observations,
    )
    if state.latched and (
        state.direction is None
        or state.last_alert_as_of is None
        or state.latched_since is None
        or state.latch_observations == 0
    ):
        raise DedupStateError(f"Latched state for {ticker} is incomplete")
    return state


def load_dedup_state(
    path: Path = _DEFAULT_STATE_PATH,
) -> dict[str, TickerDedupState]:
    """Load validated persistent deduplication state."""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DedupStateError(f"Unable to load dedup state: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise DedupStateError("Invalid dedup state schema version")
    tickers = raw.get("tickers")
    if not isinstance(tickers, dict):
        raise DedupStateError("Dedup state requires a tickers object")
    return {str(ticker): _parse_state(str(ticker), state) for ticker, state in tickers.items()}


def save_dedup_state(
    states: dict[str, TickerDedupState],
    path: Path = _DEFAULT_STATE_PATH,
) -> None:
    """Persist validated deduplication state atomically."""
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "tickers": {ticker: asdict(states[ticker]) for ticker in sorted(states)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DedupStateError(f"Unable to save dedup state: {path}") from exc


def _pending_path_for(state_path: Path) -> Path:
    """Derive the two-phase pending path (dedup_state.json -> dedup_state.pending.json)."""
    return state_path.with_name(f"{state_path.stem}.pending.json")


def default_pending_path() -> Path:
    """Return the pending path derived from the default (env-resolved) state path."""
    return _pending_path_for(_DEFAULT_STATE_PATH)


@dataclass(frozen=True)
class PendingDedupState:
    """Represent a candidate dedup state awaiting commit after Telegram send."""

    run_id: str
    as_of: str | None
    tickers: dict[str, TickerDedupState]


def save_pending_state(
    states: dict[str, TickerDedupState],
    path: Path,
    *,
    run_id: str,
    as_of: str | None,
) -> None:
    """Persist a candidate dedup state (schema + run_id/as_of) atomically.

    Two-phase commit stage 1: the official run stages its computed state here
    instead of overwriting the real state file. ``dedup_admin commit`` promotes
    it once Telegram delivery is confirmed.
    """
    if not run_id:
        raise DedupStateError("Pending state requires a non-empty run_id")
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "run_id": run_id,
        "as_of": as_of,
        "tickers": {ticker: asdict(states[ticker]) for ticker in sorted(states)},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.replace(temporary, path)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise DedupStateError(f"Unable to save pending dedup state: {path}") from exc


def load_pending_state(path: Path) -> PendingDedupState:
    """Load and validate a pending dedup state. Raises on absence or corruption."""
    if not path.exists():
        raise DedupStateError(f"No pending dedup state: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DedupStateError(f"Unable to load pending dedup state: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
        raise DedupStateError("Invalid pending dedup state schema version")
    run_id = raw.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise DedupStateError("Pending dedup state requires a non-empty run_id")
    as_of = _optional_date(raw.get("as_of"), "pending.as_of")
    tickers = raw.get("tickers")
    if not isinstance(tickers, dict):
        raise DedupStateError("Pending dedup state requires a tickers object")
    states = {
        str(ticker): _parse_state(str(ticker), state) for ticker, state in tickers.items()
    }
    return PendingDedupState(run_id=run_id, as_of=as_of, tickers=states)


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with lock_path.open("a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        raise DedupStateError(f"Unable to lock dedup state: {path}") from exc


def _effective_signal_types(
    candidate: CandidateAlert, short_interest: ShortInterestResult | None
) -> tuple[str, ...]:
    signals = set(candidate.signal_types)
    if short_interest is not None and short_interest.squeeze_prone is True:
        signals.add(_SQUEEZE_SIGNAL)
    return tuple(sorted(signals))


def _squeeze_flag(short_interest: ShortInterestResult | None) -> bool | None:
    return None if short_interest is None else short_interest.squeeze_prone


def _armed_state(as_of: str) -> TickerDedupState:
    return TickerDedupState(
        last_observed_as_of=as_of,
        last_alert_as_of=None,
        latched_since=None,
        latched=False,
        direction=None,
        trigger_z_resid=None,
        seen_signal_types=(),
        latch_observations=0,
    )


def _latched_state(
    candidate: CandidateAlert,
    signal_types: tuple[str, ...],
    *,
    observations: int = 1,
) -> TickerDedupState:
    if candidate.as_of is None or candidate.direction is None:
        raise DedupInputError(f"Candidate for {candidate.ticker} lacks date or direction")
    return TickerDedupState(
        last_observed_as_of=candidate.as_of,
        last_alert_as_of=candidate.as_of,
        latched_since=candidate.as_of,
        latched=True,
        direction=candidate.direction,
        trigger_z_resid=candidate.z_resid,
        seen_signal_types=signal_types,
        latch_observations=observations,
    )


def _updated_latch_state(
    candidate: CandidateAlert,
    previous: TickerDedupState,
    signal_types: tuple[str, ...],
    observations: int,
) -> TickerDedupState:
    if candidate.as_of is None or candidate.direction is None:
        raise DedupInputError(f"Candidate for {candidate.ticker} lacks date or direction")
    return TickerDedupState(
        last_observed_as_of=candidate.as_of,
        last_alert_as_of=candidate.as_of,
        latched_since=previous.latched_since,
        latched=True,
        direction=candidate.direction,
        trigger_z_resid=candidate.z_resid,
        seen_signal_types=signal_types,
        latch_observations=observations,
    )


def _refreshed_latch_state(
    candidate: CandidateAlert,
    previous: TickerDedupState,
    signal_types: tuple[str, ...],
) -> TickerDedupState:
    if candidate.as_of is None or candidate.direction is None:
        raise DedupInputError(f"Candidate for {candidate.ticker} lacks date or direction")
    return TickerDedupState(
        last_observed_as_of=candidate.as_of,
        last_alert_as_of=previous.last_alert_as_of,
        latched_since=candidate.as_of,
        latched=True,
        direction=candidate.direction,
        trigger_z_resid=candidate.z_resid,
        seen_signal_types=signal_types,
        latch_observations=1,
    )


def _override_reason(
    candidate: CandidateAlert,
    state: TickerDedupState,
    signal_types: tuple[str, ...],
    config: DedupConfig,
) -> FireReason | None:
    if candidate.direction != state.direction:
        return "direction_reversal"
    if set(signal_types) - set(state.seen_signal_types):
        return "new_signal_type"
    if (
        candidate.z_resid is not None
        and state.trigger_z_resid is not None
        and abs(candidate.z_resid)
        >= abs(state.trigger_z_resid) + config.escalation_z_delta
    ):
        return "escalation"
    return None


def deduplicate_alerts(
    decisions: dict[str, CandidateAlert],
    short_interest: dict[str, ShortInterestResult] | None = None,
    *,
    state_path: Path = _DEFAULT_STATE_PATH,
    config: DedupConfig | None = None,
    readonly: bool = False,
    pending_path: Path | None = None,
    run_id: str | None = None,
    run_as_of: str | None = None,
    suppressions: list[SuppressionDetail] | None = None,
) -> tuple[DeduplicatedAlert, ...]:
    """Filter Sprint 3 candidates through persistent Sprint 5 hysteresis.

    When ``readonly`` is True (or the ``ANOMALY_DEDUP_READONLY`` env var is set),
    survivors are computed from the loaded state but the state is never
    persisted. The exclusive flock is still acquired to keep reads consistent
    with concurrent writers.

    When ``pending_path`` is given (two-phase commit, official run), the real
    state is loaded and computed as usual but the candidate result is staged to
    ``pending_path`` (with ``run_id``/``run_as_of``) instead of overwriting the
    real state — the real file is committed only by ``dedup_admin commit`` after
    Telegram delivery. ``readonly`` takes precedence (dry-run stages nothing).
    """
    if pending_path is not None and not readonly and not run_id:
        raise DedupInputError("pending_path requires a non-empty run_id")
    dedup_config = config or load_dedup_config()
    squeeze_results = short_interest or {}
    effective_readonly = readonly or dedup_readonly_env()
    with _state_lock(state_path):
        return _deduplicate_locked(
            decisions,
            squeeze_results,
            state_path,
            dedup_config,
            readonly=effective_readonly,
            pending_path=pending_path,
            run_id=run_id,
            run_as_of=run_as_of,
            suppressions=suppressions,
        )


def _deduplicate_locked(
    decisions: dict[str, CandidateAlert],
    squeeze_results: dict[str, ShortInterestResult],
    state_path: Path,
    dedup_config: DedupConfig,
    *,
    readonly: bool = False,
    pending_path: Path | None = None,
    run_id: str | None = None,
    run_as_of: str | None = None,
    suppressions: list[SuppressionDetail] | None = None,
) -> tuple[DeduplicatedAlert, ...]:
    states = load_dedup_state(state_path)
    alerts: list[DeduplicatedAlert] = []
    for ticker, candidate in decisions.items():
        if ticker != candidate.ticker:
            raise DedupInputError(f"Decision key does not match candidate ticker: {ticker}")
        squeeze = squeeze_results.get(ticker)
        if squeeze is not None and squeeze.ticker != ticker:
            raise DedupInputError(f"Short-interest key does not match result ticker: {ticker}")
        if not candidate.eligible:
            previous = states.get(ticker)
            if (
                previous is not None
                and previous.latched
                and isinstance(candidate.as_of, str)
                and candidate.as_of > previous.last_observed_as_of
            ):
                try:
                    date.fromisoformat(candidate.as_of)
                except ValueError:
                    continue
                observations = previous.latch_observations + 1
                states[ticker] = (
                    _armed_state(candidate.as_of)
                    if observations > dedup_config.max_latch_observations
                    else TickerDedupState(
                        **{
                            **asdict(previous),
                            "last_observed_as_of": candidate.as_of,
                            "latch_observations": observations,
                        }
                    )
                )
            continue
        if not isinstance(candidate.as_of, str):
            raise DedupInputError(f"Eligible decision for {ticker} lacks an as_of date")
        try:
            date.fromisoformat(candidate.as_of)
        except (TypeError, ValueError) as exc:
            raise DedupInputError(f"Eligible decision for {ticker} has invalid as_of") from exc

        previous = states.get(ticker)
        if previous is not None and candidate.as_of <= previous.last_observed_as_of:
            _record_suppression(suppressions, candidate, "already_observed")
            continue
        if previous is None or not previous.latched:
            if candidate.is_candidate:
                signal_types = _effective_signal_types(candidate, squeeze)
                states[ticker] = _latched_state(candidate, signal_types)
                alerts.append(
                    DeduplicatedAlert(
                        candidate=candidate,
                        squeeze_prone=_squeeze_flag(squeeze),
                        fire_reason="initial",
                        signal_types=signal_types,
                    )
                )
            else:
                states[ticker] = _armed_state(candidate.as_of)
            continue

        observations = previous.latch_observations + 1
        if candidate.z_resid is not None and abs(candidate.z_resid) < dedup_config.rearm_z:
            states[ticker] = _armed_state(candidate.as_of)
            _record_suppression(suppressions, candidate, "below_rearm")
            continue
        if not candidate.is_candidate:
            states[ticker] = (
                _armed_state(candidate.as_of)
                if observations > dedup_config.max_latch_observations
                else TickerDedupState(
                    **{
                        **asdict(previous),
                        "last_observed_as_of": candidate.as_of,
                        "latch_observations": observations,
                    }
                )
            )
            continue

        signal_types = _effective_signal_types(candidate, squeeze)
        if observations > dedup_config.max_latch_observations:
            states[ticker] = _refreshed_latch_state(candidate, previous, signal_types)
            _record_suppression(suppressions, candidate, "max_latch_refresh")
            continue

        reason = _override_reason(candidate, previous, signal_types, dedup_config)
        if reason is None:
            states[ticker] = TickerDedupState(
                **{
                    **asdict(previous),
                    "last_observed_as_of": candidate.as_of,
                    "seen_signal_types": tuple(
                        sorted(set(previous.seen_signal_types) | set(signal_types))
                    ),
                    "latch_observations": observations,
                }
            )
            _record_suppression(suppressions, candidate, "latched_no_override")
            continue

        merged_types = (
            signal_types
            if reason == "direction_reversal"
            else tuple(sorted(set(previous.seen_signal_types) | set(signal_types)))
        )
        states[ticker] = (
            _latched_state(candidate, merged_types)
            if reason == "direction_reversal"
            else _updated_latch_state(candidate, previous, merged_types, observations)
        )
        alerts.append(
            DeduplicatedAlert(
                candidate=candidate,
                squeeze_prone=_squeeze_flag(squeeze),
                fire_reason=reason,
                signal_types=merged_types,
                prev_trigger_z_resid=(
                    previous.trigger_z_resid if reason == "escalation" else None
                ),
            )
        )

    if readonly:
        return tuple(alerts)
    if pending_path is not None:
        if pending_path.exists():
            logger.warning(
                "Overwriting orphan pending dedup state at %s (previous run never committed)",
                pending_path,
            )
        save_pending_state(states, pending_path, run_id=run_id or "", as_of=run_as_of)
    else:
        save_dedup_state(states, state_path)
    return tuple(alerts)
