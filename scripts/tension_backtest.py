#!/usr/bin/env python3
"""Phase 0 — tension score prototype (compression / quiet accumulation).

Question: P(|move| > X% within 20 trading days | tension today) vs the same
probability on a random day OF THE SAME TICKER. The readout is a LIFT ratio —
it survives universe selection bias the same way the ablation deltas did.
Direction is deliberately not predicted (compression predicts expansion, not sign).

Signals (close bars only, no look-ahead — all baselines use prior-day windows):
  VOLQUIET  quiet accumulation: rvol5 >= T and max|z_price| over 5d < 1
  COMPRESS  volatility compression: ATR14(t) / ATR14(t-20) <= T
  EITHER    VOLQUIET(rvol>=3) or COMPRESS(<=0.7)

Read-only. No prod state, no LLM, no dedup. Reuses the generate_results frames
cache. Output: lift table (stdout) + JSON snapshot in the scratch dir.

Usage:
    python3 scripts/tension_backtest.py --frames-cache PATH [--start] [--end]
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import date
from pathlib import Path
from statistics import median

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from market_intelligence.candidate_alerts import load_alert_config

DEFAULT_START = date(2022, 1, 1)
DEFAULT_END = date(2026, 5, 31)
HORIZON = 20          # forward trading days
MOVE_THRESHOLDS = (0.10, 0.15)
WARMUP = 65           # bars needed before a day is eligible (60d z + margin)
MIN_CELL = 30

# (label, kind, threshold)
CONDITIONS = [
    # v0 (kept for reference — saturated event / too-loose compress documented in v1)
    ("VOLQUIET5 rvol5>=2 z5<1", "volquiet", 2.0),
    ("COMPRESS atr<=0.7", "compress", 0.7),
    # v1: realistic quiet accumulation (flat 5d cum return, not 5 flat days)
    ("VQ1 rvol5>=1.5 |cum5|<3%", "vq_cum", 1.5),
    ("VQ1 rvol5>=2 |cum5|<3%", "vq_cum", 2.0),
    ("VQ1d rvol>=3 |z|<1 today", "vq_day", 3.0),
    # v1: percentile squeeze (bottom decile of trailing-year bollinger bandwidth)
    ("SQUEEZE bw pctl<=10%", "squeeze", 0.10),
    ("SQUEEZE bw pctl<=5%", "squeeze", 0.05),
    ("VQ1(2.0) | SQUEEZE(10%)", "either_v1", None),
]


def features(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-day tension features. Baselines exclude the current day (shift(1))."""
    f = frame.copy()
    f.columns = [c.lower() for c in f.columns]
    f = f[~f.index.duplicated(keep="last")].sort_index()
    close, vol = f["close"], f["volume"]

    ret = close.pct_change()
    med = ret.rolling(60).median().shift(1)
    mad = (ret - ret.rolling(60).median()).abs().rolling(60).median().shift(1)
    z = 0.6745 * (ret - med) / mad.replace(0, float("nan"))

    vol_base = vol.rolling(20).mean().shift(1)
    rvol = vol / vol_base.replace(0, float("nan"))
    rvol5 = rvol.rolling(5).mean()
    z5max = z.abs().rolling(5).max()

    prev_close = close.shift(1)
    tr = pd.concat([f["high"] - f["low"], (f["high"] - prev_close).abs(),
                    (f["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()
    atr_ratio = atr14 / atr14.shift(20)

    # v1: bollinger bandwidth percentile within the trailing year (squeeze)
    bw = close.rolling(20).std() / close.rolling(20).mean()
    bw_pctl = bw.rolling(252, min_periods=120).rank(pct=True)
    # v1: flat 5-day cumulative return (quiet), single-day rvol
    cum5 = close.pct_change(5)
    # v1: vol-normalized expected 20d move, known at t (no look-ahead)
    exp_move = (atr14 / close) * (HORIZON ** 0.5)

    # forward max |cumulative move| over the next HORIZON closes; columns = k
    fwd = pd.concat(
        {k: (close.shift(-k) / close - 1).abs() for k in range(1, HORIZON + 1)}, axis=1
    )
    out = pd.DataFrame({
        "rvol5": rvol5, "z5max": z5max, "atr_ratio": atr_ratio,
        "rvol": rvol, "z": z, "cum5": cum5, "bw_pctl": bw_pctl,
        "fwd_max": fwd.max(axis=1), "fwd_complete": fwd.notna().all(axis=1),
        # explosion normalized by the ticker's own vol at t: >2x expected move
        "fwd_norm": fwd.max(axis=1) / exp_move.replace(0, float("nan")),
    })
    # first day the move exceeds each threshold (lead time)
    for thr in MOVE_THRESHOLDS:
        hit = fwd.ge(thr)
        out[f"lead_{int(thr*100)}"] = hit.idxmax(axis=1).where(hit.any(axis=1))
    hit_n = fwd.ge(2.0 * exp_move, axis=0)
    out["lead_norm"] = hit_n.idxmax(axis=1).where(hit_n.any(axis=1))
    out["eligible"] = out.index.to_series().notna() & (pd.Series(
        range(len(out)), index=out.index) >= WARMUP) & out["fwd_complete"]
    return out


def tension_mask(df: pd.DataFrame, kind: str, thr: float | None) -> pd.Series:
    if kind == "volquiet":
        return (df["rvol5"] >= thr) & (df["z5max"] < 1.0)
    if kind == "compress":
        return df["atr_ratio"] <= thr
    if kind == "vq_cum":
        return (df["rvol5"] >= thr) & (df["cum5"].abs() < 0.03)
    if kind == "vq_day":
        return (df["rvol"] >= thr) & (df["z"].abs() < 1.0)
    if kind == "squeeze":
        return df["bw_pctl"] <= thr
    # either_v1
    return ((df["rvol5"] >= 2.0) & (df["cum5"].abs() < 0.03)) | (df["bw_pctl"] <= 0.10)


def episode_starts(mask: pd.Series) -> pd.Series:
    """Collapse consecutive tension days into episodes; keep the first day."""
    return mask & ~mask.shift(1, fill_value=False)


def evaluate(feats: dict[str, pd.DataFrame], event: str, thr: float) -> list[dict]:
    """event: 'fixed' -> fwd_max > thr ; 'norm' -> fwd_norm > thr (vol-adjusted)."""
    col, lead_col = (("fwd_max", f"lead_{int(thr*100)}") if event == "fixed"
                     else ("fwd_norm", "lead_norm"))
    base = {t: df.loc[df["eligible"], col].gt(thr).mean()
            for t, df in feats.items() if df["eligible"].sum() > 0}
    rows = []
    for label, kind, cthr in CONDITIONS:
        hits, expected, n, leads = 0, 0.0, 0, []
        days = 0
        for t, df in feats.items():
            if t not in base or pd.isna(base[t]):
                continue
            el = df["eligible"]
            mask = tension_mask(df, kind, cthr) & el
            days += int(mask.sum())
            starts = episode_starts(mask) & el
            sel = df.loc[starts]
            n += len(sel)
            hits += int(sel[col].gt(thr).sum())
            expected += base[t] * len(sel)
            leads.extend(sel.loc[sel[col].gt(thr), lead_col].dropna().tolist())
        p = hits / n if n else None
        exp_p = expected / n if n else None
        rows.append({
            "condition": label, "tension_days": days, "episodes": n,
            "p_explosion": p, "base_matched": exp_p,
            "lift": (p / exp_p) if p is not None and exp_p else None,
            "median_lead_days": median(leads) if leads else None,
        })
    return rows


def fmt(rows: list[dict], title: str, base_all: float) -> str:
    L = [f"\n=== {title} — universe base rate {100*base_all:.0f}% ===",
         f"{'condition':<30} {'days':>5} {'episodes':>8} {'P(expl)':>8} "
         f"{'base':>6} {'lift':>6} {'lead(med)':>9}"]
    for r in rows:
        small = " (n<30!)" if r["episodes"] < MIN_CELL else ""
        p = "—" if r["p_explosion"] is None else f"{100*r['p_explosion']:.0f}%"
        b = "—" if r["base_matched"] is None else f"{100*r['base_matched']:.0f}%"
        lift = "—" if r["lift"] is None else f"{r['lift']:.2f}"
        lead = "—" if r["median_lead_days"] is None else f"{r['median_lead_days']:.0f}d"
        L.append(f"{r['condition']:<30} {r['tension_days']:>5} {r['episodes']:>8} "
                 f"{p:>8} {b:>6} {lift:>6} {lead:>9}{small}")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--frames-cache", type=Path, required=True)
    p.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    p.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    p.add_argument("--out", type=Path, default=None, help="JSON snapshot path")
    args = p.parse_args(argv)

    frames = pickle.loads(args.frames_cache.read_bytes())
    cls = load_alert_config().classifications
    portfolio = [t for t in cls if t in frames and not frames[t].empty]

    feats: dict[str, pd.DataFrame] = {}
    for t in portfolio:
        df = features(frames[t])
        idx = pd.to_datetime(df.index)
        df = df[(idx >= pd.Timestamp(args.start)) & (idx <= pd.Timestamp(args.end))]
        feats[t] = df

    events = [("fixed", 0.10, f"|move| > 10% within {HORIZON}d"),
              ("fixed", 0.15, f"|move| > 15% within {HORIZON}d"),
              ("norm", 2.0, f"vol-normalized: move > 2x expected ({HORIZON}d, ATR-based)")]
    all_results = {}
    for event, thr, title in events:
        col = "fwd_max" if event == "fixed" else "fwd_norm"
        rows = evaluate(feats, event, thr)
        pooled = pd.concat([df.loc[df["eligible"], col] for df in feats.values()])
        base_all = float(pooled.gt(thr).mean())
        print(fmt(rows, title, base_all))
        all_results[f"{event}_{thr}"] = {"universe_base": base_all, "rows": rows}

    # per-class split on the vol-normalized event (the non-saturated one)
    for klass in ("speculative", "calm"):
        sub = {t: df for t, df in feats.items() if cls[t] == klass}
        if not sub:
            continue
        rows = evaluate(sub, "norm", 2.0)
        pooled = pd.concat([df.loc[df["eligible"], "fwd_norm"] for df in sub.values()])
        print(fmt(rows, f"[{klass}] move > 2x expected", float(pooled.gt(2.0).mean())))
        all_results[f"class_{klass}_norm"] = {"rows": rows}

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(all_results, indent=2, default=str), encoding="utf-8")
        print(f"\nsnapshot: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
