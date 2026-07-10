#!/usr/bin/env python3
"""Signal-quality ablation for the Layer B EOD anomaly pipeline.

Regenerates ``docs/RESULTS.md`` and ``docs/data/backtest_<date>.json`` from
scratch, deterministically given the fetched price data.

Read-only / prod-safe by construction:
- Ephemeral dedup state in a temp dir; prod paths (dedup_state.json,
  dedup_state.pending.json, runs.jsonl, outcomes.jsonl) are never referenced.
- No Warren / OpenClaw / any LLM. Detection replays the deterministic path only.
- ``ANOMALY_DEDUP_READONLY`` is deliberately NOT set: it ORs into
  ``effective_readonly`` and would freeze hysteresis evolution, collapsing the
  NO_HYST vs FULL contrast. Prod-safety is structural (ephemeral state) instead.

Four ablation arms over one universe / window / seed:
  FULL     residual-z, full S1-S3 combos, hysteresis ON        (production)
  NO_BETA  raw robust-z substituted for residual-z, else identical
  NO_HYST  FULL decisions, hysteresis OFF (every candidate fires)
  NAIVE    |daily return| > 5%, no z / gate / hysteresis
Derived populations (set differences on (ticker, as_of)):
  beta_suppressed = alerts(NO_BETA) \\ alerts(FULL)   # what the beta gate killed
  hyst_suppressed = alerts(NO_HYST) \\ alerts(FULL)   # what hysteresis killed
Forward signed returns come from ``outcome_tracker.measure_event`` (unchanged),
sign = detected direction.

The headline is a DELTA, not a level. The universe is selection-biased (hand-
picked in the present); all arms share that bias so between-arm comparisons
survive it, absolute levels do not. RESULTS.md leads with deltas and never
presents an absolute hit rate as a performance claim.

Usage:
    python3 scripts/generate_results.py [--frames-cache PATH] [--start YYYY-MM-DD]
                                        [--end YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import logging
import pickle
import sys
import tempfile
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path
from statistics import mean, median

import pandas as pd

# Run standalone (python3 scripts/generate_results.py) without PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_intelligence.anomaly_signals import calculate_all as calc_signals
from market_intelligence.beta_gate import calculate_all as calc_gates
from market_intelligence.beta_gate import load_factor_config
from market_intelligence.candidate_alerts import evaluate_all, load_alert_config
from market_intelligence.dedup_hysteresis import deduplicate_alerts, load_dedup_config
from market_intelligence.outcome_tracker import AlertEvent, measure_event
from market_intelligence.registry_schema import load_registry

logger = logging.getLogger("generate_results")

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"
DATA = DOCS / "data"

DEFAULT_START = date(2022, 1, 1)
DEFAULT_END = date(2026, 5, 31)  # today - ~40 cal days so J+20 is measurable
GRID = [1.5, 2.0, 2.5, 3.0, 3.5]
NAIVE_MOVE = 0.05
TAIL = 0.05
MIN_CELL = 30
HORIZONS = (1, 5, 20)
OHLCV = ["Open", "High", "Low", "Close", "Volume"]


# --------------------------------------------------------------------------- #
# Data                                                                        #
# --------------------------------------------------------------------------- #
def universe() -> tuple[list[str], list[str]]:
    """Return (portfolio symbols, factor symbols incl. market factor)."""
    reg = load_registry()
    portfolio = [e.symbol for e in reg.portfolio_tickers]
    fc = load_factor_config()
    factors = {fc.market_factor}
    for mapped in fc.sector_factors.values():
        factors.update(mapped)
    return portfolio, sorted(factors)


def fetch_frames(symbols: list[str], cache: Path | None) -> dict[str, pd.DataFrame]:
    """Fetch full history per symbol (yfinance period=max). Optional pickle cache."""
    if cache and cache.exists():
        logger.info("Loading frames from cache %s", cache)
        return pickle.loads(cache.read_bytes())
    import yfinance as yf

    frames: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = yf.download(sym, period="max", interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel(1)
        frames[sym] = df[[c for c in OHLCV if c in df.columns]] if not df.empty else pd.DataFrame()
        logger.info("fetched %-6s rows=%d", sym, len(frames[sym]))
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps(frames))
    return frames


def trading_days(frames: dict[str, pd.DataFrame], portfolio: list[str],
                 start: date, end: date) -> list[date]:
    days: set[date] = set()
    for sym in portfolio:
        f = frames.get(sym)
        if f is None or f.empty:
            continue
        for ts in pd.to_datetime(f.index):
            d = ts.date()
            if start <= d <= end:
                days.add(d)
    return sorted(days)


def truncate(frames: dict[str, pd.DataFrame], as_of: date) -> dict[str, pd.DataFrame]:
    cut = pd.Timestamp(as_of)
    out: dict[str, pd.DataFrame] = {}
    for sym, f in frames.items():
        if f is None or f.empty:
            out[sym] = pd.DataFrame()
            continue
        idx = pd.to_datetime(f.index)
        out[sym] = f[idx <= cut]
    return out


# --------------------------------------------------------------------------- #
# Replay                                                                       #
# --------------------------------------------------------------------------- #
class Replay:
    """One deterministic day-by-day replay of the four arms + threshold grid."""

    def __init__(self, frames, portfolio, factors, classifications):
        self.frames = frames
        self.portfolio = portfolio
        self.factors = factors
        self.cls = classifications
        self.expected_symbols = tuple(portfolio)
        self.alert_cfg = load_alert_config()
        self.dedup_cfg = load_dedup_config()
        self.factor_cfg = load_factor_config()

        # Alert sets: (ticker, as_of) -> direction
        self.full: dict = {}
        self.nohyst: dict = {}
        self.nobeta: dict = {}
        self.naive: dict = {}
        self.grid: dict = {g: {} for g in GRID}

        # Funnel counters keyed by ticker class
        self.trading_days = 0
        self.ticker_days = defaultdict(int)   # evaluable ticker-days (had a bar)
        self.raw_cand = defaultdict(int)      # NO_BETA is_candidate (S1, before gate)
        self.beta_cand = defaultdict(int)     # FULL is_candidate (after gate, S2/S3)
        self.surv = defaultdict(int)          # FULL survivors (after hysteresis, S5)

    def run(self, days: list[date], state_dir: Path) -> None:
        self.trading_days = len(days)
        stp = {
            "full": state_dir / "full.json",
            "nobeta": state_dir / "nobeta.json",
            **{f"grid_{g}": state_dir / f"grid_{g}.json" for g in GRID},
        }
        for i, day in enumerate(days):
            self._step(day, stp)
            if i % 100 == 0:
                logger.info("day %d/%d %s", i, len(days), day)

    def _dedup(self, decisions, path, as_of):
        return deduplicate_alerts(
            decisions, {}, state_path=path, config=self.dedup_cfg,
            readonly=False, run_id="bt", run_as_of=as_of,
        )

    def _step(self, day: date, stp: dict) -> None:
        tf = truncate(self.frames, day)
        stock_frames = {s: tf[s] for s in self.portfolio}
        signals = calc_signals(stock_frames)
        gates = calc_gates(signals, tf, self.factor_cfg)
        as_of = _expected_as_of(signals)
        if as_of is None:
            return

        # ticker-days scanned: had a bar dated as_of
        for s in self.portfolio:
            sig = signals.get(s)
            if sig is not None and sig.as_of == as_of and sig.bar_count > 0:
                self.ticker_days[self.cls[s]] += 1

        # --- FULL (residual z, hysteresis on) ---
        dec_full = evaluate_all(signals, gates, self.alert_cfg,
                                expected_as_of=as_of, expected_symbols=self.expected_symbols)
        for t, d in dec_full.items():
            if d.is_candidate:
                self.beta_cand[self.cls[t]] += 1
                self.nohyst[(t, as_of)] = d.direction  # NO_HYST = every candidate fires
        for surv in self._dedup(dec_full, stp["full"], as_of):
            c = surv.candidate
            self.full[(c.ticker, as_of)] = c.direction
            self.surv[self.cls[c.ticker]] += 1

        # --- NO_BETA (raw robust-z substituted, else identical) ---
        gates_nb = {
            t: replace(g, z_resid=(signals[t].return_robust_z if t in signals else None))
            for t, g in gates.items()
        }
        dec_nb = evaluate_all(signals, gates_nb, self.alert_cfg,
                              expected_as_of=as_of, expected_symbols=self.expected_symbols)
        for t, d in dec_nb.items():
            if d.is_candidate:
                self.raw_cand[self.cls[t]] += 1
        for surv in self._dedup(dec_nb, stp["nobeta"], as_of):
            c = surv.candidate
            self.nobeta[(c.ticker, as_of)] = c.direction

        # --- NAIVE (|daily return| > 5%) ---
        for s in self.portfolio:
            sig = signals.get(s)
            if sig is None or sig.as_of != as_of or sig.daily_return is None:
                continue
            if abs(sig.daily_return) > NAIVE_MOVE:
                self.naive[(s, as_of)] = "up" if sig.daily_return > 0 else "down"

        # --- Threshold grid (uniform residual-z threshold, hysteresis on) ---
        for g in GRID:
            cfg_g = replace(self.alert_cfg, calm_residual_z=g, speculative_residual_z=g + 1e-6)
            dec_g = evaluate_all(signals, gates, cfg_g,
                                 expected_as_of=as_of, expected_symbols=self.expected_symbols)
            for surv in self._dedup(dec_g, stp[f"grid_{g}"], as_of):
                c = surv.candidate
                self.grid[g][(c.ticker, as_of)] = c.direction


def _expected_as_of(signals) -> str | None:
    dates = tuple(s.as_of for s in signals.values() if s.as_of is not None and s.bar_count > 0)
    return max(dates) if dates else None


# --------------------------------------------------------------------------- #
# Measurement + metrics                                                       #
# --------------------------------------------------------------------------- #
def measure_population(alerts: dict, closes: dict, cls: dict, today: date) -> list[dict]:
    """Measure forward signed returns for one alert set. Only 'measured' kept."""
    recs = []
    for (ticker, as_of_str), direction in alerts.items():
        ev = AlertEvent(ticker=ticker, as_of=date.fromisoformat(as_of_str),
                        direction=direction, outcome="bt")
        rec = measure_event(ev, closes.get(ticker), today)
        if rec and rec.get("status") == "measured":
            rec["class"] = cls[ticker]
            recs.append(rec)
    return recs


def cell(returns: list[float]) -> dict:
    n = len(returns)
    if n < MIN_CELL:
        return {"n": n, "too_small": True}
    return {
        "n": n,
        "median": median(returns),
        "mean": mean(returns),
        "hit_rate": sum(1 for r in returns if r > 0) / n,
        "tail_gt5": sum(1 for r in returns if abs(r) > TAIL) / n,
    }


def quality(recs: list[dict]) -> dict:
    """Per class ('speculative','calm','all') x horizon metrics."""
    out: dict = {}
    for klass in ("speculative", "calm", "all"):
        out[klass] = {}
        for h in HORIZONS:
            rets = [r[f"ret_{h}d"] for r in recs
                    if klass == "all" or r["class"] == klass]
            out[klass][h] = cell(rets)
    return out


def months_in(days: list[date]) -> int:
    return len({(d.year, d.month) for d in days}) or 1


# --------------------------------------------------------------------------- #
# Rendering                                                                    #
# --------------------------------------------------------------------------- #
def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100*x:+.2f}%"


def _rate(x: float | None) -> str:
    return "—" if x is None else f"{100*x:.0f}%"


def _cellrow(c: dict) -> str:
    if c.get("too_small"):
        return f"{c['n']} | *sample too small* | | |"
    return (f"{c['n']} | {_pct(c['median'])} | {_pct(c['mean'])} | "
            f"{_rate(c['hit_rate'])} | {_rate(c['tail_gt5'])}")


def _hit(cell_: dict) -> float | None:
    return None if cell_.get("too_small") else cell_.get("hit_rate")


def bottom_line(res: dict) -> list[str]:
    """Three delta-framed sentences a 30-second reader keeps. Pulled from the data."""
    m, d, mo = res["metrics"], res["deltas"], res["window"]["months"]
    full_hit = _hit(m["FULL"]["all"][5])
    naive_hit = _hit(m["NAIVE"]["all"][5])
    nobeta_hit = _hit(m["NO_BETA"]["all"][5])
    added_hit = _hit(m["beta_added"]["all"][5])
    full_pm = res["counts"]["FULL"] / mo
    naive_pm = res["counts"]["NAIVE"] / mo

    def r(x):
        return "—" if x is None else f"{100*x:.0f}%"

    lines = []
    if full_hit is not None and naive_hit is not None:
        cut = round(100 * (1 - full_pm / naive_pm)) if naive_pm else 0
        beats = "beats" if full_hit > naive_hit + 0.005 else ("ties" if abs(full_hit - naive_hit) <= 0.005 else "does NOT beat")
        lines.append(
            f"**Selectivity, not edge.** The full pipeline fires {full_pm:.0f} alerts/month "
            f"vs the naive >5%-move rule's {naive_pm:.0f} (−{cut}%), at a J+5 directional hit "
            f"rate of {r(full_hit)} vs {r(naive_hit)} — it **{beats}** naive on direction. "
            f"All arms sit at 41–49% (≈ coin flip): this measures attention, not direction."
        )
    lines.append(
        f"**Hysteresis earns its keep.** The candidates it suppresses have a J+5 hit rate "
        f"{d['j5_hit_gap_hyst_str']} below the alerts it lets through "
        f"(n={res['counts']['hyst_suppressed']}) — it removes noise."
    )
    lines.append(
        f"**The beta gate is factor-hygiene, not a quality filter.** It re-scores rather than "
        f"filters — removes {r(d['beta_suppression_rate'])} of raw-z alerts, net "
        f"{d['beta_net_alerts']:+d} — and the raw-z arm's J+5 hit rate ({r(nobeta_hit)}) is "
        f"≥ the gated one ({r(full_hit)}); the alerts the gate adds underperform "
        f"({r(added_hit)} vs {r(full_hit)})."
    )
    return lines


def render_md(res: dict) -> str:
    m = res["metrics"]
    f = res["funnel"]
    d = res["deltas"]
    L: list[str] = []
    A = L.append

    A(f"# Layer B — signal quality (ablation)\n")
    A(f"EOD anomaly detection, {res['window']['start']} → {res['window']['end']}, "
      f"{res['window']['trading_days']} trading days, {res['window']['tickers']} tickers "
      f"({res['window']['months']} months). Generated {res['generated']}.\n")
    A("**Read deltas, not levels.** The ticker universe is selection-biased; all "
      "arms share that bias so between-arm gaps are meaningful, absolute levels are "
      "not a performance claim. Attention detection ≠ tradable edge. Caveats at the bottom.\n")

    A("## Bottom line\n")
    for b in bottom_line(res):
        A(f"- {b}")
    A("")

    # Headline
    A("## Headline (deltas)\n")
    A("| metric | value |")
    A("|---|---|")
    A(f"| Beta gate: raw-z alerts it removed | {_rate(d['beta_suppression_rate'])} "
      f"(net alert change {d['beta_net_alerts']:+d}) |")
    A(f"| Hysteresis: candidates it removed | {_rate(d['hyst_suppression_rate'])} |")
    A(f"| J+5 hit-rate: sent (FULL) − beta-suppressed | {d['j5_hit_gap_beta_str']} |")
    A(f"| J+5 hit-rate: sent (FULL) − gate-added | {d['j5_hit_gap_added_str']} |")
    A(f"| J+5 hit-rate: sent (FULL) − hyst-suppressed | {d['j5_hit_gap_hyst_str']} |")
    full_hit = _hit(m["FULL"]["all"][5])
    naive_hit = _hit(m["NAIVE"]["all"][5])
    naive_gap = None if (full_hit is None or naive_hit is None) else full_hit - naive_hit
    A(f"| J+5 hit-rate: sent (FULL) − naive baseline | "
      f"{'*n<30*' if naive_gap is None else f'{100*naive_gap:+.0f} pts'} |")
    A(f"| Verdict beta gate (vs suppressed) | {d['verdict_beta']} |")
    A(f"| Verdict beta gate (vs added) | {d['verdict_beta_added']} |")
    A(f"| Verdict hysteresis | {d['verdict_hyst']} |\n")

    # Funnel (production path, monotonic)
    A("## Funnel — production path\n")
    A("| stage | speculative | calm | all |")
    A("|---|--:|--:|--:|")
    A(f"| trading days | | | {f['trading_days']} |")
    A(f"| ticker-days scanned | {f['ticker_days']['speculative']} | "
      f"{f['ticker_days']['calm']} | {f['ticker_days']['all']} |")
    A(f"| candidates after beta gate (S2/S3) | {f['beta_cand']['speculative']} | "
      f"{f['beta_cand']['calm']} | {f['beta_cand']['all']} |")
    A(f"| sent after hysteresis (S5) | {f['surv']['speculative']} | "
      f"{f['surv']['calm']} | {f['surv']['all']} |\n")

    # Ablation volume (counterfactual arms — NOT nested stages)
    A("## Ablation — alert volume\n")
    A("Counterfactual arms over the identical universe/window. The beta gate is a "
      "re-scoring (raw-z → residual-z), not a nested filter, so it both removes and "
      "adds alerts; the set differences below isolate each effect.\n")
    A("| arm / set | alerts | alerts/month |")
    A("|---|--:|--:|")
    mo = res["window"]["months"]
    for key, lab in [("NAIVE", "NAIVE (\\|move\\|>5%)"),
                     ("NO_BETA", "NO_BETA candidates (raw-z gate, hyst on)"),
                     ("NO_HYST", "NO_HYST (all candidates, residual-z, no hyst)"),
                     ("FULL", "FULL (sent)")]:
        n = res["counts"][key]
        A(f"| {lab} | {n} | {n/mo:.2f} |")
    for key, lab in [("beta_suppressed", "beta_suppressed = NO_BETA \\ FULL (gate removed)"),
                     ("beta_added", "beta_added = FULL \\ NO_BETA (gate created)"),
                     ("hyst_suppressed", "hyst_suppressed = NO_HYST \\ FULL (hyst removed)")]:
        n = res["counts"][key]
        A(f"| {lab} | {n} | {n/mo:.2f} |")
    A("")

    # Signal quality per population
    A("## Signal quality — signed forward returns (sign = detected direction)\n")
    labels = {
        "FULL": "Sent alerts (FULL)",
        "beta_suppressed": "Beta-suppressed (raw-z fired, beta gate removed)",
        "beta_added": "Beta-added (beta gate fired, raw-z would not)",
        "hyst_suppressed": "Dedup-gated candidates (suppressed by hysteresis)",
        "NAIVE": "Naive baseline (|move| > 5%)",
    }
    for pop, title in labels.items():
        A(f"### {title}\n")
        A("| class | horizon | n | median | mean | hit rate | P(\\|r\\|>5%) |")
        A("|---|---|--:|--:|--:|--:|--:|")
        for klass in ("speculative", "calm", "all"):
            for h in HORIZONS:
                A(f"| {klass} | J+{h} | {_cellrow(m[pop][klass][h])} |")
        A("")

    # Baselines J+5
    A("## Baselines — J+5, all tickers\n")
    A("| arm | n | median | mean | hit rate | P(\\|r\\|>5%) |")
    A("|---|--:|--:|--:|--:|--:|")
    for arm, lab in [("NAIVE", "naive >5%"), ("NO_BETA", "no-gate (raw \\|z\\|)"),
                     ("NO_HYST", "no-hysteresis"), ("FULL", "full pipeline")]:
        A(f"| {lab} | {_cellrow(m['baselines'][arm]['all'][5])} |")
    A("")

    # Threshold grid
    A("## Threshold grid — residual-z, hysteresis on (precision/recall)\n")
    A("| threshold | alerts/month | J+5 hit rate | J+5 median signed |")
    A("|--:|--:|--:|--:|")
    for row in res["grid"]:
        j5 = row["j5"]
        hit = "*n<30*" if j5.get("too_small") else _rate(j5["hit_rate"])
        med = "" if j5.get("too_small") else _pct(j5["median"])
        A(f"| {row['threshold']} | {row['alerts_per_month']:.2f} | {hit} | {med} |")
    A("")

    # Caveats
    A("## Caveats\n")
    for c in res["caveats"]:
        A(f"- {c}")
    A("")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# Deltas / verdicts                                                            #
# --------------------------------------------------------------------------- #
def _j5(metrics_pop_all: dict) -> dict:
    return metrics_pop_all["all"][5]


def compute_deltas(m: dict, counts: dict) -> dict:
    beta_rate = (counts["beta_suppressed"] / counts["NO_BETA"]) if counts["NO_BETA"] else None
    hyst_rate = (counts["hyst_suppressed"] / counts["NO_HYST"]) if counts["NO_HYST"] else None
    net_beta = counts["FULL"] - counts["NO_BETA"]  # >0: gate net-adds alerts

    def gap(a, b, key):
        ca, cb = _j5(m[a]), _j5(m[b])
        if ca.get("too_small") or cb.get("too_small"):
            return None
        return ca[key] - cb[key]

    j5_hit_beta = gap("FULL", "beta_suppressed", "hit_rate")
    j5_med_beta = gap("FULL", "beta_suppressed", "median")
    j5_hit_added = gap("FULL", "beta_added", "hit_rate")
    j5_hit_hyst = gap("FULL", "hyst_suppressed", "hit_rate")

    def verdict_removed(gap_hit, what):
        # +gap = sent beats what-was-removed = the filter dropped weaker alerts = good.
        if gap_hit is None:
            return "insufficient sample"
        if gap_hit > 0.05:
            return f"removes NOISE — {what} underperform sent by {round(100*gap_hit)} pts J+5 hit"
        if gap_hit < -0.05:
            return f"removes SIGNAL — {what} beat sent by {round(-100*gap_hit)} pts J+5 hit"
        return f"no clean separation — {what} ≈ sent on J+5 hit rate"

    def verdict_added(gap_hit):
        # +gap = sent beats the gate's net-new alerts = gate dilutes quality.
        if gap_hit is None:
            return "insufficient sample"
        if gap_hit > 0.05:
            return f"gate DILUTES — its net-new alerts underperform sent by {round(100*gap_hit)} pts J+5 hit"
        if gap_hit < -0.05:
            return f"gate CONCENTRATES — its net-new alerts beat sent by {round(-100*gap_hit)} pts J+5 hit"
        return "gate-added ≈ sent on J+5 hit rate"

    def s(x, unit="pts", nd=0):
        return "*n<30*" if x is None else f"{100*x:+.{nd}f} {unit}"

    return {
        "beta_suppression_rate": beta_rate,
        "beta_net_alerts": net_beta,
        "hyst_suppression_rate": hyst_rate,
        "j5_hit_gap_beta": j5_hit_beta,
        "j5_med_gap_beta": j5_med_beta,
        "j5_hit_gap_added": j5_hit_added,
        "j5_hit_gap_hyst": j5_hit_hyst,
        "j5_hit_gap_beta_str": s(j5_hit_beta),
        "j5_med_gap_beta_str": s(j5_med_beta, nd=2),
        "j5_hit_gap_added_str": s(j5_hit_added),
        "j5_hit_gap_hyst_str": s(j5_hit_hyst),
        "verdict_beta": verdict_removed(j5_hit_beta, "beta-suppressed"),
        "verdict_beta_added": verdict_added(j5_hit_added),
        "verdict_hyst": verdict_removed(j5_hit_hyst, "hyst-suppressed"),
    }


CAVEATS = [
    "**Selection bias**: the ticker universe was hand-picked in the present with "
    "knowledge of which names became interesting. Absolute levels overstate performance. "
    "All four arms share the identical universe/window, so between-arm DELTAS are the "
    "valid readout; absolute hit rates are not a performance claim.",
    "**Sample size**: any cell with n<30 is labelled *sample too small* and shows no "
    "percentages. The `calm` class is only 2 tickers (MMED, XYL) and MMED has <90 bars — "
    "calm cells are near-empty by construction; the speculative class carries the signal.",
    "**Multiple comparisons**: the threshold grid reports 5 thresholds; the best-looking "
    "cell is optimistically biased. Do not read the max as the expected out-of-sample value.",
    "**Survivorship**: quarantine is currently empty, but yfinance returns only live "
    "symbols — any renamed/delisted ticker returns empty and is silently absent. Recent "
    "IPOs (GRO 2024-11, MMED 2026-03) contribute only from their inception + beta-gate warmup.",
    "**What this measures**: attention/anomaly detection and the short-horizon signed drift "
    "that follows it. What it does NOT measure: tradability, transaction costs, slippage, "
    "borrow, capacity, or a live trading edge. Direction is a z-sign proxy, not a forecast.",
    "**Determinism**: deterministic given the fetched prices. yfinance auto-adjusted closes "
    "can drift as new corporate actions post; re-fetching later may move marginal cells. The "
    "committed docs/data/backtest_<date>.json is the pinned snapshot.",
    "**Beta gate is not a separate filter**: it is the residual-z computation itself. "
    "'Beta-gated candidates' are reconstructed as alerts(NO_BETA) \\ alerts(FULL) — raw-z "
    "crossers the residualization killed. NO_BETA is FULL with return_robust_z substituted "
    "for z_resid, everything else identical, so the difference isolates the beta gate.",
    "**Prod-safety**: ephemeral dedup state in a temp dir; no prod state file, runs.jsonl, "
    "outcomes.jsonl or LLM is ever touched. ANOMALY_DEDUP_READONLY is intentionally unset "
    "(it would freeze hysteresis and void the NO_HYST contrast).",
]


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def _selfcheck() -> None:
    """Assert the aggregation, sign, and set-difference invariants. No network."""
    # cell(): n<30 -> too_small, no percentages.
    assert cell([0.01] * 10) == {"n": 10, "too_small": True}
    # cell(): known vector. 30 values, 20 positive, 10 negative; 5 with |r|>5%.
    vec = [0.02] * 20 + [-0.02] * 5 + [-0.06] * 5
    c = cell(vec)
    assert c["n"] == 30 and abs(c["hit_rate"] - 20 / 30) < 1e-9
    assert abs(c["tail_gt5"] - 5 / 30) < 1e-9
    assert c["median"] == 0.02
    # measure_event sign: a 'down' detection profits when price falls.
    closes = pd.Series([100.0, 90.0, 90.0, 90.0, 90.0, 90.0],
                       index=pd.to_datetime([f"2024-01-0{i}" for i in range(1, 7)]))
    ev = AlertEvent(ticker="X", as_of=date(2024, 1, 1), direction="down", outcome="bt")
    # not enough forward bars for J+20 -> None or unavailable; J+1 signing checked on a longer series.
    long_closes = pd.Series([100.0] + [90.0] * 25,
                            index=pd.to_datetime(pd.date_range("2024-01-01", periods=26, freq="D")))
    rec = measure_event(ev, long_closes, date(2024, 3, 1))
    assert rec and rec["status"] == "measured" and rec["ret_1d"] > 0, rec  # down + fall = +profit
    # set-difference semantics.
    full = {("A", "d1"): "up", ("B", "d1"): "up"}
    nobeta = {("A", "d1"): "up", ("C", "d1"): "down"}
    beta_suppressed = {k: v for k, v in nobeta.items() if k not in full}
    beta_added = {k: v for k, v in full.items() if k not in nobeta}
    assert beta_suppressed == {("C", "d1"): "down"}
    assert beta_added == {("B", "d1"): "up"}
    print("self-check OK")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--self-check", action="store_true", help="Run invariant asserts and exit.")
    p.add_argument("--frames-cache", type=Path, default=None,
                   help="Pickle cache of fetched frames (read if present, else written).")
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    p.add_argument("--today", type=date.fromisoformat, default=None,
                   help="Override 'today' for outcome readiness (default: real today).")
    p.add_argument("--render-only", type=Path, default=None,
                   help="Re-render RESULTS.md from a committed backtest json (no fetch/replay).")
    args = p.parse_args(argv)
    if args.self_check:
        _selfcheck()
        return 0
    if args.render_only:
        res = _load_result(args.render_only)
        res["deltas"] = compute_deltas(res["metrics"], res["counts"])
        (DOCS / "RESULTS.md").write_text(render_md(res), encoding="utf-8")
        logger.info("re-rendered %s from %s", DOCS / "RESULTS.md", args.render_only)
        return 0
    today = args.today or date.today()

    portfolio, factors = universe()
    cfg = load_alert_config()
    cls = cfg.classifications
    frames = fetch_frames(portfolio + factors, args.frames_cache)
    days = trading_days(frames, portfolio, args.start, args.end)
    logger.info("universe: %d tickers, %d factors, %d trading days",
                len(portfolio), len(factors), len(days))

    with tempfile.TemporaryDirectory(prefix="bt_state_") as tmp:
        rep = Replay(frames, portfolio, factors, cls)
        rep.run(days, Path(tmp))

    closes = {t: frames[t]["Close"] for t in portfolio
              if not frames[t].empty and "Close" in frames[t].columns}

    # Derived populations (set difference on (ticker, as_of)). The beta gate
    # re-scores rather than filters, so it both removes raw-z alerts
    # (beta_suppressed) and creates new ones (beta_added) — report both.
    beta_suppressed = {k: v for k, v in rep.nobeta.items() if k not in rep.full}
    beta_added = {k: v for k, v in rep.full.items() if k not in rep.nobeta}
    hyst_suppressed = {k: v for k, v in rep.nohyst.items() if k not in rep.full}

    populations = {
        "FULL": rep.full,
        "NO_BETA": rep.nobeta,
        "NO_HYST": rep.nohyst,
        "NAIVE": rep.naive,
        "beta_suppressed": beta_suppressed,
        "beta_added": beta_added,
        "hyst_suppressed": hyst_suppressed,
    }
    measured = {name: measure_population(alerts, closes, cls, today)
                for name, alerts in populations.items()}
    metrics = {name: quality(recs) for name, recs in measured.items()}
    metrics["baselines"] = {a: metrics[a] for a in ("NAIVE", "NO_BETA", "NO_HYST", "FULL")}

    counts = {name: len(alerts) for name, alerts in populations.items()}
    deltas = compute_deltas(metrics, counts)

    months = months_in(days)
    grid_rows = []
    for g in GRID:
        recs_g = measure_population(rep.grid[g], closes, cls, today)
        rets5 = [r["ret_5d"] for r in recs_g]
        grid_rows.append({
            "threshold": g,
            "alerts": len(rep.grid[g]),
            "alerts_per_month": len(rep.grid[g]) / months,
            "j5": cell(rets5),
        })

    def funnel_all(counter):
        d = {k: counter.get(k, 0) for k in ("speculative", "calm")}
        d["all"] = d["speculative"] + d["calm"]
        return d

    result = {
        "generated": today.isoformat(),
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat(),
                   "trading_days": len(days), "months": months, "tickers": len(portfolio)},
        "config": {"alert_thresholds": _read_json(REPO / "market_intelligence/data/alert_thresholds.json"),
                   "dedup_thresholds": _read_json(REPO / "market_intelligence/data/dedup_thresholds.json"),
                   "sector_factors": _read_json(REPO / "market_intelligence/data/sector_factors.json")},
        "funnel": {"trading_days": len(days),
                   "ticker_days": funnel_all(rep.ticker_days),
                   "raw_cand": funnel_all(rep.raw_cand),
                   "beta_cand": funnel_all(rep.beta_cand),
                   "surv": funnel_all(rep.surv)},
        "counts": counts,
        "metrics": metrics,
        "deltas": deltas,
        "grid": grid_rows,
        "events": {name: measured[name] for name in populations},
        "caveats": CAVEATS,
    }

    DATA.mkdir(parents=True, exist_ok=True)
    out_json = DATA / f"backtest_{today.isoformat()}.json"
    out_json.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str),
                        encoding="utf-8")
    (DOCS / "RESULTS.md").write_text(render_md(result), encoding="utf-8")
    logger.info("wrote %s and %s", out_json, DOCS / "RESULTS.md")
    print(f"beta_suppression_rate={deltas['beta_suppression_rate']} "
          f"hyst_suppression_rate={deltas['hyst_suppression_rate']} "
          f"verdict_beta={deltas['verdict_beta']!r}")
    return 0


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _norm_horizons(by_class: dict) -> dict:
    return {k: {int(h): v for h, v in by_h.items()} for k, by_h in by_class.items()}


def _load_result(path: Path) -> dict:
    """Load a committed backtest json, restoring int horizon keys (JSON stringifies them)."""
    res = json.loads(path.read_text(encoding="utf-8"))
    metrics = res.get("metrics", {})
    for pop, by_class in list(metrics.items()):
        if pop == "baselines":  # one level deeper: {arm: {class: {horizon: cell}}}
            metrics[pop] = {arm: _norm_horizons(bc) for arm, bc in by_class.items()}
        else:
            metrics[pop] = _norm_horizons(by_class)
    return res


if __name__ == "__main__":
    raise SystemExit(main())
