"""Epic 6 Sprint 3 — the EOD digest is 100% deterministic (zero LLM).

The frozen reference template lives in the epic; these tests assert the rendered
survivor blocks reproduce its structure (fixed header, one block per survivor
keyed by ``fire_reason`` with the signal_types translation + numbers, the tension
block, the hysteresis explainer, the per-ticker footer) and that the default EOD
path never calls Warren.
"""

from __future__ import annotations

import pandas as pd

import market_intelligence.eod_orchestrator as orchestrator
from market_intelligence.anomaly_signals import AnomalySignals
from market_intelligence.candidate_alerts import CandidateAlert
from market_intelligence.dedup_hysteresis import DeduplicatedAlert
from market_intelligence.eod_orchestrator import format_digest
from market_intelligence.registry_schema import Registry, TickerEntry
from market_intelligence.short_interest import ShortInterestResult
from market_intelligence.tension_signals import TensionSignal, format_tension_digest


def _signals(symbol: str, **overrides: object) -> AnomalySignals:
    base: dict[str, object] = dict(
        symbol=symbol, as_of="2026-07-17", bar_count=280, short_history=False,
        fallback_applied=False, return_window=60, volume_window=20, extrema_window=252,
        daily_return=None, return_median=None, return_mad=None, return_robust_z=None,
        rvol=None, log_volume_z=None, opening_gap=None, true_range=None, atr14=None,
        atr_expansion_ratio=None, high_52w=None, low_52w=None,
        breakout_high_52w=None, breakout_low_52w=None, data_issues=(),
    )
    base.update(overrides)
    return AnomalySignals(**base)  # type: ignore[arg-type]


def _candidate(
    symbol: str, direction: str, z_resid: float, signal_types: tuple[str, ...]
) -> CandidateAlert:
    return CandidateAlert(
        ticker=symbol, as_of="2026-07-17", classification="speculative",
        eligible=True, is_candidate=True, direction=direction,  # type: ignore[arg-type]
        signal_types=signal_types, z_resid=z_resid, residual_threshold=2.5,
        short_history_fallback_applied=False, data_issues=(),
    )


def _bbai() -> DeduplicatedAlert:
    return DeduplicatedAlert(
        candidate=_candidate(
            "BBAI", "up", 2.8, ("atr_expansion", "residual_z", "rvol", "volume_z")
        ),
        squeeze_prone=True,
        fire_reason="initial",
        signal_types=("atr_expansion", "residual_z", "rvol", "squeeze_prone", "volume_z"),
    )


def _hims() -> DeduplicatedAlert:
    return DeduplicatedAlert(
        candidate=_candidate("HIMS", "down", -3.4, ("breakout_low_52w", "residual_z")),
        squeeze_prone=False,
        fire_reason="escalation",
        signal_types=("breakout_low_52w", "residual_z"),
        prev_trigger_z_resid=-2.4,
    )


def _signal_map() -> dict[str, AnomalySignals]:
    return {
        "BBAI": _signals(
            "BBAI", rvol=4.2, log_volume_z=3.1, opening_gap=0.064, atr_expansion_ratio=1.9
        ),
        "HIMS": _signals("HIMS", breakout_low_52w=True),
    }


def _tension_block() -> str:
    asts = TensionSignal(
        symbol="ASTS", as_of="2026-07-17", bar_count=280, bw_pctl=0.08, rvol5=1.6,
        cum5=0.072, expected_move_20d=0.14, squeeze=True, quiet_accumulation=True,
        tension=True, episode_start=True, data_issues=(),
    )
    return format_tension_digest({"ASTS": asts}, as_of="2026-07-17")


def test_format_digest_reproduces_the_frozen_template() -> None:
    digest = format_digest(
        [_bbai(), _hims()],
        _signal_map(),
        as_of="2026-07-17",
        total_analyzed=181,
        tension_block=_tension_block(),
    )

    # Fixed header.
    assert "📊 <b>EOD anomalies — 2026-07-17</b>" in digest
    assert "2 survivants sur 181 symboles analysés." in digest
    assert "Un « survivant » = un ticker dont l'anomalie est NEUVE aujourd'hui." in digest

    # BBAI — initial: canned phrase + signal_types translation + numbers + squeeze.
    assert "BBAI — hausse ↑" in digest
    assert "[première alerte]" in digest
    assert "Première alerte : BBAI n'était pas encore « verrouillé »" in digest
    assert "un volume relatif de 4,2× la normale" in digest
    assert "un volume anormalement élevé (z +3,1)" in digest
    assert "Son z-résiduel atteint +2,8 (seuil 2,5)" in digest
    assert "Gap d'ouverture +6,4 %" in digest
    assert "volatilité en expansion (ATR ×1,9)" in digest
    assert "⚠ Profil squeeze possible (short interest élevé)." in digest
    assert "→ Pour l'analyse Warren : « point sur BBAI »" in digest

    # HIMS — escalation: rappelle le niveau déclencheur précédent (prev_trigger).
    assert "HIMS — baisse ↓" in digest
    assert "[escalade]" in digest
    assert "Escalade : HIMS était déjà verrouillé (il avait déclenché à −2,4)" in digest
    assert (
        "son z-résiduel s'est aggravé jusqu'à −3,4, soit +1,0 au-delà du niveau "
        "qui l'avait fait alerter" in digest
    )
    assert "Il casse en plus son plus-bas 52 semaines." in digest
    assert "→ Pour l'analyse Warren : « point sur HIMS »" in digest

    # Tension block (existing format_tension_digest, reused unchanged).
    assert "Tension — Layer C" in digest
    assert "ASTS" in digest and "squeeze" in digest

    # Fixed hysteresis explainer — the 4 cases, verbatim.
    assert "ℹ️ <b>Le filtre d'hystérésis — pourquoi si peu d'alertes ?</b>" in digest
    assert " • escalade — son z-résiduel s'aggrave d'au moins +1,0" in digest
    assert " • renversement — la direction s'inverse (hausse ↔ baisse)" in digest
    assert " • nouveau signal — un type de signal s'ajoute (volume, cassure 52 sem…)" in digest
    assert " • ré-armement — il retombe au calme (sous 1,0) puis re-franchit le seuil" in digest

    # Dashed separators between sections.
    assert orchestrator._DIGEST_SEP in digest

    # Sorted by |z_resid| descending: HIMS (|3,4|) before BBAI (|2,8|).
    assert digest.index("HIMS") < digest.index("BBAI")

    # HTML-safe: bold tags balanced (Telegram parse_mode=HTML).
    assert digest.count("<b>") == digest.count("</b>") > 0


def test_format_digest_singular_and_empty() -> None:
    one = format_digest([_bbai()], _signal_map(), as_of="2026-07-17", total_analyzed=181)
    assert "1 survivant sur 181 symboles analysés." in one  # singular
    # Nothing to send: no survivors, no tension.
    assert format_digest([], {}, as_of="2026-07-17", total_analyzed=181) == ""


def test_format_digest_escapes_dynamic_ticker() -> None:
    alert = DeduplicatedAlert(
        candidate=_candidate("A<B&C", "up", 3.0, ("residual_z",)),
        squeeze_prone=None, fire_reason="initial", signal_types=("residual_z",),
    )
    digest = format_digest(
        [alert], {"A<B&C": _signals("A<B&C")}, as_of="2026-07-17", total_analyzed=10
    )
    assert "A&lt;B&amp;C" in digest
    stripped = digest.replace("<b>", "").replace("</b>", "")
    assert "<" not in stripped and ">" not in stripped


# ── Zero LLM in the default EOD path ────────────────────────────────────────


def _registry(symbols: tuple[str, ...]) -> Registry:
    return Registry(
        portfolio_tickers=tuple(
            TickerEntry(symbol=s, api_symbol=s, expected_name=f"{s} Corp") for s in symbols
        ),
        macro_tickers=(),
        alias_map={},
    )


def _frame() -> pd.DataFrame:
    dates = pd.date_range("2026-06-01", periods=2, freq="B")
    return pd.DataFrame(
        {"Open": 100.0, "High": 100.0, "Low": 100.0, "Close": 100.0, "Volume": 1_000_000},
        index=dates,
    )


def _short(symbol: str) -> ShortInterestResult:
    return ShortInterestResult(
        ticker=symbol, api_symbol=symbol, short_percent_float=None, shares_short=None,
        days_to_cover=None, squeeze_prone=None, coverage_status="incomplete",
        data_issues=(),
    )


def test_default_eod_path_never_calls_warren(monkeypatch) -> None:
    """A spy Warren analyzer + macro builder that raise if touched prove zero LLM."""
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)

    def exploding_analyzer(enriched_alerts):
        raise AssertionError("Warren analyze_alerts must not run in the default path")

    def exploding_macro(frames, registry):
        raise AssertionError("macro snapshot (Haiku) must not run in the default path")

    monkeypatch.setattr(orchestrator, "calculate_all", lambda frames: _signal_map())
    monkeypatch.setattr(orchestrator, "calculate_beta_gates", lambda signals, frames: {})
    monkeypatch.setattr(
        orchestrator,
        "evaluate_candidates",
        lambda *a, **k: {"BBAI": _bbai().candidate, "HIMS": _hims().candidate},
    )

    result = orchestrator.run_eod_anomaly_pipeline(
        registry=_registry(("BBAI", "HIMS")),
        frame_fetcher=lambda days: {"BBAI": _frame(), "HIMS": _frame()},
        short_interest_fetcher=lambda reg: {"BBAI": _short("BBAI"), "HIMS": _short("HIMS")},
        deduplicator=lambda decisions, short_interest, **kwargs: (_bbai(), _hims()),
        analyzer=exploding_analyzer,   # spy: raises if called
        macro_builder=exploding_macro,  # spy: raises if called
        dry_run=True,                   # default skip_warren=True is exercised
    )

    assert result.analysis_count == 0
    # Dry-run here (to avoid state writes): digest is built with zero LLM but not
    # sent. The prod send path (dry_run=False → should_send=True) is covered by
    # test_skip_warren_without_dry_run_is_allowed.
    assert result.should_send is False
    assert "📊 <b>EOD anomalies — 2026-07-17</b>" in result.digest
    assert "point sur BBAI" in result.digest and "point sur HIMS" in result.digest
    assert "Le filtre d'hystérésis" in result.digest
