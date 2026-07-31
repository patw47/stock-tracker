"""Epic 6 Sprint 3 — the EOD digest is 100% deterministic (zero LLM).

The frozen reference template lives in the epic; these tests assert the rendered
survivor blocks reproduce its structure (fixed header, one block per survivor
keyed by ``fire_reason`` with the signal_types translation + numbers, the tension
block, the hysteresis explainer, the per-ticker footer) and that the default EOD
path never calls Warren.
"""

from __future__ import annotations

import json

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
            "BBAI", daily_return=0.072, rvol=4.2, log_volume_z=3.1, opening_gap=0.064,
            atr_expansion_ratio=1.9,
        ),
        "HIMS": _signals(
            "HIMS", daily_return=-0.098, opening_gap=0.022, breakout_low_52w=True
        ),
    }


def _tension_block(provenance: dict[str, str] | None = None) -> str:
    asts = TensionSignal(
        symbol="ASTS", as_of="2026-07-17", bar_count=280, bw_pctl=0.08, rvol5=1.6,
        cum5=0.072, expected_move_20d=0.14, squeeze=True, quiet_accumulation=True,
        tension=True, episode_start=True, data_issues=(),
    )
    return format_tension_digest(
        {"ASTS": asts}, as_of="2026-07-17", provenance=provenance
    )


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


# ── Epic 7 S1 — ticker provenance (portfolio / watchlist / registry only) ───


def _runtime_lists(tmp_path, monkeypatch, **lists: object) -> None:
    """Point ``_resolve_runtime_path`` at tmp copies of portfolio/watchlist.json."""
    from market_intelligence import registry_check

    monkeypatch.setattr(registry_check, "REPO_ROOT", tmp_path)
    for name, content in lists.items():
        path = tmp_path / f"{name}.json"
        if isinstance(content, str):
            path.write_text(content, encoding="utf-8")
        else:
            payload = {"tickers": [{"symbol": s} for s in content]}  # type: ignore[union-attr]
            path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_provenance_labels_gives_portfolio_priority(tmp_path, monkeypatch) -> None:
    """A ticker in both lists is a portfolio holding first (epic decision)."""
    _runtime_lists(
        tmp_path, monkeypatch, portfolio=["BBAI", "HIMS"], watchlist=["HIMS", "ASTS"]
    )

    assert orchestrator.load_provenance_labels() == {
        "BBAI": "💼 portefeuille",
        "HIMS": "💼 portefeuille",  # in both → portfolio wins
        "ASTS": "👀 watchlist",
    }


def test_provenance_tags_survivor_headers_and_tension_lines() -> None:
    """Golden: fixed input → exact header/tension text, one case per label."""
    provenance = {"BBAI": "💼 portefeuille", "ASTS": "👀 watchlist"}
    digest = format_digest(
        [_bbai(), _hims()],
        _signal_map(),
        as_of="2026-07-17",
        total_analyzed=181,
        tension_block=_tension_block(provenance),
        provenance=provenance,
    )

    assert "<b>1. HIMS (registre seul) — baisse ↓   [escalade]</b>" in digest
    assert "<b>2. BBAI (💼 portefeuille) — hausse ↑   [première alerte]</b>" in digest
    assert (
        "<b>ASTS</b> (👀 watchlist): squeeze (bw pctl 8%); accumulation calme "
        "(rvol5 1.6, 5j +7.2%) — move attendu 20j ±14%" in digest
    )

    # A watchlist ticker in a survivor *header* too (not only in a tension line).
    watched = format_digest(
        [_hims()], _signal_map(), as_of="2026-07-17", total_analyzed=181,
        provenance={"HIMS": "👀 watchlist"},
    )
    assert "<b>1. HIMS (👀 watchlist) — baisse ↓   [escalade]</b>" in watched


# Exact digest for the fixtures above with ``provenance=None`` — the frozen anchor
# of the untagged rendering (fail-soft path). Update only on a deliberate template
# change, never to make a test pass.
_UNTAGGED_DIGEST = """\
📊 <b>EOD anomalies — 2026-07-17</b>
2 survivants sur 181 symboles analysés.
Un « survivant » = un ticker dont l'anomalie est NEUVE aujourd'hui.
────────────────────────────
<b>1. HIMS — baisse ↓   [escalade]</b>

Escalade : HIMS était déjà verrouillé (il avait déclenché à −2,4), mais son \
z-résiduel s'est aggravé jusqu'à −3,4, soit +1,0 au-delà du niveau qui l'avait \
fait alerter. Concrètement : clôture −9,8 % aujourd'hui, alors que le titre avait \
OUVERT en hausse (+2,2 %) — il s'est retourné en cours de journée — la situation \
s'aggrave au lieu de se calmer. Il casse en plus son plus-bas 52 semaines.
→ Pour l'analyse Warren : « point sur HIMS »
────────────────────────────
<b>2. BBAI — hausse ↑   [première alerte]</b>

Première alerte : BBAI n'était pas encore « verrouillé » et vient de franchir son \
seuil de déclenchement. Concrètement : clôture +7,2 % aujourd'hui, après une \
ouverture déjà en hausse (gap +6,4 %). Mouvement porté par un volume relatif de \
4,2× la normale et un volume anormalement élevé (z +3,1). Son z-résiduel atteint \
+2,8 (seuil 2,5) — le titre bouge nettement plus que son comportement habituel : \
son mouvement propre, une fois retirée la part expliquée par le marché, fait \
environ 3× sa journée typique. Gap d'ouverture +6,4 %, volatilité en expansion \
(ATR ×1,9).
⚠ Profil squeeze possible (short interest élevé).
→ Pour l'analyse Warren : « point sur BBAI »
────────────────────────────
⚡ <b>Tension — Layer C (2026-07-17)</b>

<b>ASTS</b>: squeeze (bw pctl 8%); accumulation calme (rvol5 1.6, 5j +7.2%) — \
move attendu 20j ±14%
   ↳ volatilité comprimée dans le pire décile de son année — un mouvement se \
prépare, direction inconnue
   ↳ volume anormal sans mouvement de prix — accumulation silencieuse possible
────────────────────────────
ℹ️ <b>Le filtre d'hystérésis — pourquoi si peu d'alertes ?</b>

Un ticker n'alerte qu'UNE fois en franchissant son seuil (z-résiduel ~2,5). Il \
est ensuite « verrouillé » et reste silencieux tant qu'il ne fait rien de neuf. \
Il ne réapparaît que dans 4 cas :
 • escalade — son z-résiduel s'aggrave d'au moins +1,0
 • renversement — la direction s'inverse (hausse ↔ baisse)
 • nouveau signal — un type de signal s'ajoute (volume, cassure 52 sem…)
 • ré-armement — il retombe au calme (sous 1,0) puis re-franchit le seuil"""


def test_provenance_falls_back_to_registry_only_in_tension_lines() -> None:
    """A tension symbol in no runtime list is flagged ``registre seul``."""
    assert "<b>ASTS</b> (registre seul):" in _tension_block({"BBAI": "💼 portefeuille"})


def test_unreadable_runtime_list_leaves_the_digest_untouched(tmp_path, monkeypatch) -> None:
    """Fail-soft: a corrupt list drops the tags, it never breaks the run.

    Anchored on ``_UNTAGGED_DIGEST`` (the exact expected text, frozen here) rather
    than on a second render of the same code, so a regression in the untagged
    rendering actually fails.
    """
    _runtime_lists(tmp_path, monkeypatch, portfolio="{ not json", watchlist=["ASTS"])

    assert orchestrator.load_provenance_labels() is None

    without_tags = format_digest(
        [_bbai(), _hims()], _signal_map(), as_of="2026-07-17", total_analyzed=181,
        tension_block=_tension_block(None), provenance=None,
    )
    assert without_tags == _UNTAGGED_DIGEST
    assert "portefeuille" not in without_tags
    assert "watchlist" not in without_tags
    assert "registre seul" not in without_tags


# ── Epic 7 S2 — explicit prose (raw close, intraday reading, z gloss) ───────


def _concrete(**overrides: object) -> str:
    """« Concrètement … » clause for one set of signals (no trailing dot)."""
    return orchestrator._concrete_clause(_signals("XYZ", **overrides))


def test_concrete_clause_same_signs_credits_the_open() -> None:
    """Rule 1 — close and gap agree, |gap| >= 1 %: the move was there at the open."""
    assert _concrete(daily_return=0.072, opening_gap=0.064) == (
        "Concrètement : clôture +7,2 % aujourd'hui, après une ouverture déjà en "
        "hausse (gap +6,4 %)"
    )


def test_concrete_clause_flat_open_means_built_in_session() -> None:
    """Rule 2 — |gap| < 1 %: nothing happened overnight."""
    assert _concrete(daily_return=-0.055, opening_gap=-0.003) == (
        "Concrètement : clôture −5,5 % aujourd'hui, après une ouverture quasi "
        "inchangée (gap −0,3 %) — le mouvement s'est construit en séance, pas sur "
        "une nouvelle nocturne"
    )


def test_concrete_clause_plain_reversal() -> None:
    """Rule 3 without a 52w breakout: reversal, no "cassure ratée"."""
    assert _concrete(daily_return=-0.098, opening_gap=0.022) == (
        "Concrètement : clôture −9,8 % aujourd'hui, alors que le titre avait "
        "OUVERT en hausse (+2,2 %) — il s'est retourné en cours de journée"
    )


def test_concrete_clause_failed_breakout_high() -> None:
    """Epic reference rendering, verbatim: 52w high touched then sold off."""
    assert _concrete(
        daily_return=-0.098, opening_gap=0.022, breakout_high_52w=True
    ) + "." == (
        "Concrètement : clôture −9,8 % aujourd'hui, alors que le titre avait "
        "OUVERT en hausse (+2,2 %) et touché un plus-haut de 52 semaines en séance "
        "— il s'est retourné en cours de journée (cassure ratée : l'élan du matin "
        "a été vendu)."
    )


def test_concrete_clause_failed_breakout_low() -> None:
    """Mirror case: 52w low touched at a down open, closed up."""
    assert _concrete(daily_return=0.081, opening_gap=-0.034, breakout_low_52w=True) == (
        "Concrètement : clôture +8,1 % aujourd'hui, alors que le titre avait "
        "OUVERT en baisse (−3,4 %) et touché un plus-bas de 52 semaines en séance "
        "— il s'est retourné en cours de journée (cassure ratée : l'élan du matin "
        "a été vendu)"
    )


def test_breakout_in_the_close_direction_is_not_a_failed_breakout() -> None:
    """A 52w low with a down close is just a breakout — no reversal narrative."""
    assert "cassure ratée" not in _concrete(
        daily_return=-0.098, opening_gap=0.022, breakout_low_52w=True
    )


def test_concrete_sentence_omitted_without_daily_return() -> None:
    """Fail-soft: no raw close computed → the sentence disappears, run intact."""
    assert _concrete(opening_gap=0.064) == ""
    digest = format_digest(
        [_bbai()], {"BBAI": _signals("BBAI", rvol=4.2)},
        as_of="2026-07-17", total_analyzed=181,
    )
    assert "Concrètement" not in digest
    assert "un volume relatif de 4,2× la normale" in digest  # rest of the prose stands


def test_escalation_prose_carries_the_worsening_clause() -> None:
    """Escalation gets the same sentence plus the "s'aggrave" tail."""
    digest = format_digest(
        [_hims()], _signal_map(), as_of="2026-07-17", total_analyzed=181
    )
    assert (
        "Concrètement : clôture −9,8 % aujourd'hui, alors que le titre avait OUVERT "
        "en hausse (+2,2 %) — il s'est retourné en cours de journée — la situation "
        "s'aggrave au lieu de se calmer." in digest
    )


def test_z_gloss_keeps_the_exact_figure_and_signs_the_direction() -> None:
    """|z| rounded to an integer, "vers le bas" when negative — a complement only."""
    up = format_digest(
        [_bbai()], _signal_map(), as_of="2026-07-17", total_analyzed=181
    )
    assert "Son z-résiduel atteint +2,8 (seuil 2,5)" in up  # exact figure kept
    assert (
        "son mouvement propre, une fois retirée la part expliquée par le marché, "
        "fait environ 3× sa journée typique." in up
    )

    falling = DeduplicatedAlert(
        candidate=_candidate("XYZ", "down", -3.4, ("residual_z",)),
        squeeze_prone=None, fire_reason="initial", signal_types=("residual_z",),
    )
    down = format_digest(
        [falling], {"XYZ": _signals("XYZ")}, as_of="2026-07-17", total_analyzed=181
    )
    assert "fait environ 3× sa journée typique, vers le bas." in down


def test_tension_glosses_translate_each_signal_type() -> None:
    """SQUEEZE / QUIET each get their canned plain-language line."""
    block = _tension_block()
    assert (
        "   ↳ volatilité comprimée dans le pire décile de son année — un mouvement "
        "se prépare, direction inconnue" in block
    )
    assert (
        "   ↳ volume anormal sans mouvement de prix — accumulation silencieuse "
        "possible" in block
    )


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
    """The pipeline has no LLM stage: analysis_count stays 0 and the digest is canned."""
    monkeypatch.delenv("ANOMALY_DEDUP_READONLY", raising=False)

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
        dry_run=True,
        provenance={"BBAI": "💼 portefeuille"},
    )

    assert result.analysis_count == 0
    assert "BBAI (💼 portefeuille)" in result.digest and "HIMS (registre seul)" in result.digest
    # Dry-run here (to avoid state writes): digest is built with zero LLM but not
    # sent. The prod send path (dry_run=False → should_send=True) is covered by
    # test_skip_warren_without_dry_run_is_allowed.
    assert result.should_send is False
    assert "📊 <b>EOD anomalies — 2026-07-17</b>" in result.digest
    assert "point sur BBAI" in result.digest and "point sur HIMS" in result.digest
    assert "Le filtre d'hystérésis" in result.digest
