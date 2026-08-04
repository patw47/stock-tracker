#!/usr/bin/env python3
"""Rapport mensuel de track record — Epic 5 Sprint 2.

Le premier vendredi du mois, agrège ``outcomes.jsonl`` (Sprint 1) sur le mois
calendaire écoulé et produit un bilan Telegram : nb d'alertes, ce qu'elles sont
devenues (rendements médians, continuation vs réversion), répartition par
classification et signal, top regret parmi les candidats gated, data issues
chroniques.

Template Python pur, **zéro LLM, zéro langage de recommandation**. Le module
imprime le message sur stdout ; l'envoi Telegram est fait par n8n (executeCommand
→ chaîne Aggregate/Split/Send, parse_mode HTML de l'epic Livraison). Le texte est
donc échappé HTML côté producteur et n'utilise jamais de ``#``.
"""
from __future__ import annotations

import argparse
import html
import json
import statistics
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from market_intelligence.telegram_split import split_telegram_html

_RUNTIME_DIR = Path(__file__).parent.parent / "runtime" / "market_intelligence"
OUTCOMES_PATH = _RUNTIME_DIR / "outcomes.jsonl"
RUNS_LOG_PATH = _RUNTIME_DIR / "runs.jsonl"
_ALERT_THRESHOLDS_PATH = Path(__file__).parent / "data" / "alert_thresholds.json"

# Below this many measured alerts, medians/rates are hidden as misleading.
MIN_SAMPLE = 10
HORIZONS = (1, 5, 20)


def previous_month(today: date) -> tuple[date, date]:
    """Return (first_day, last_day) of the calendar month before ``today``."""
    first_this = today.replace(day=1)
    last_prev = first_this - timedelta(days=1)
    return last_prev.replace(day=1), last_prev


def load_jsonl(path: Path) -> list[dict]:
    """Read a JSONL file into a list of dicts; missing file → empty list."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            records.append(parsed)
    return records


def is_first_friday(day: date) -> bool:
    """True if ``day`` is the first Friday of its month."""
    return day.weekday() == 4 and day.day <= 7


def _measured_date(record: dict) -> date | None:
    raw = record.get("measured_at")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _dedup_last_wins(outcomes: list[dict]) -> list[dict]:
    """Collapse repeated event ids to their last record (append order wins).

    An event can appear twice in outcomes.jsonl (unavailable then upgraded to
    measured); only the final state must feed the aggregates so it is never
    counted both in the stats and in the data-issues.
    """
    by_id: dict[str, dict] = {}
    without_id: list[dict] = []
    for record in outcomes:
        eid = record.get("event_id")
        if isinstance(eid, str):
            by_id[eid] = record
        else:
            without_id.append(record)
    return list(by_id.values()) + without_id


def signal_types_by_event(runs: list[dict]) -> dict[str, tuple[str, ...]]:
    """Map ``ticker:as_of`` → signal_types, read best-effort from the run journal."""
    mapping: dict[str, tuple[str, ...]] = {}
    for record in runs:
        as_of = record.get("as_of")
        if not isinstance(as_of, str):
            continue
        for detail in record.get("candidates_detail", []):
            ticker = detail.get("ticker")
            if not isinstance(ticker, str):
                continue
            types = tuple(str(t) for t in detail.get("signal_types", []))
            mapping.setdefault(f"{ticker}:{as_of}", types)
    return mapping


def load_classifications(path: Path = _ALERT_THRESHOLDS_PATH) -> dict[str, str]:
    """Return the ticker → classification map from alert_thresholds.json."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}
    classifications = data.get("classifications", {})
    return {str(k): str(v) for k, v in classifications.items()} if isinstance(classifications, dict) else {}


def _pct(value: float) -> str:
    return f"{value:+.1%}"


def build_report(
    outcomes: list[dict],
    *,
    period_start: date,
    period_end: date,
    signal_types: dict[str, tuple[str, ...]] | None = None,
    classifications: dict[str, str] | None = None,
) -> str:
    """Build the monthly track-record message (pure, HTML-safe, no ``#``)."""
    signal_types = signal_types or {}
    classifications = classifications or {}

    # Select by measurement date, not as_of: S1 lags ~28-30 trading days, so an
    # alert fired on the 15th is only measured the next month. Keying on
    # measured_at guarantees every measured event lands in exactly one report.
    in_period = [
        o for o in _dedup_last_wins(outcomes)
        if (d := _measured_date(o)) is not None and period_start <= d <= period_end
    ]
    measured = [o for o in in_period if o.get("status") == "measured"]
    survived = [o for o in measured if o.get("outcome") == "survived"]
    gated = [o for o in measured if str(o.get("outcome", "")).startswith("gated_dedup")]

    header = f"📊 <b>Rapport track record — mesurés en {period_start.strftime('%Y-%m')}</b>"
    lines = [header, f"Alertes envoyées : {len(survived)} · candidats gated : {len(gated)}"]

    if len(survived) < MIN_SAMPLE:
        lines.append(
            f"Échantillon insuffisant (N={len(survived)} &lt; {MIN_SAMPLE}) — "
            "pas de statistiques fiables ce mois."
        )
    else:
        lines.append("Rendements médians (signés selon la direction) :")
        for horizon in HORIZONS:
            values = [float(o[f"ret_{horizon}d"]) for o in survived if f"ret_{horizon}d" in o]
            if values:
                lines.append(f"  • J+{horizon} : {_pct(statistics.median(values))}")
        ret20 = [float(o["ret_20d"]) for o in survived if "ret_20d" in o]
        if ret20:
            cont = sum(1 for v in ret20 if v > 0) / len(ret20)
            rev = sum(1 for v in ret20 if v < 0) / len(ret20)
            lines.append(f"Continuation J+20 : {cont:.0%} · réversion : {rev:.0%}")

        by_class: Counter[str] = Counter(
            classifications.get(str(o.get("ticker")), "unknown") for o in survived
        )
        lines.append("Par classification : " + ", ".join(
            f"{html.escape(name)}={count}" for name, count in by_class.most_common()
        ))

        by_signal: Counter[str] = Counter()
        for o in survived:
            for sig in signal_types.get(str(o.get("event_id")), ()):
                by_signal[sig] += 1
        if by_signal:
            lines.append("Par signal : " + ", ".join(
                f"{html.escape(sig)}={count}" for sig, count in by_signal.most_common()
            ))

    regret = _top_regret(gated)
    if regret is not None:
        ticker, move = regret
        lines.append(f"Top regret (gated) : {html.escape(ticker)} {_pct(move)} à J+20")

    issues = Counter(
        str(o.get("reason", "unknown"))
        for o in in_period if o.get("status") == "unavailable"
    )
    if issues:
        lines.append("Data issues chroniques : " + ", ".join(
            f"{html.escape(reason)}={count}" for reason, count in issues.most_common(3)
        ))

    return "\n".join(lines)


def _top_regret(gated: list[dict]) -> tuple[str, float] | None:
    """Return the gated candidate with the largest post-filter |J+20| move."""
    best: tuple[str, float] | None = None
    for o in gated:
        if "ret_20d" not in o:
            continue
        move = float(o["ret_20d"])
        if best is None or abs(move) > abs(best[1]):
            best = (str(o.get("ticker", "?")), move)
    return best


def run(
    *,
    outcomes_path: Path = OUTCOMES_PATH,
    runs_path: Path = RUNS_LOG_PATH,
    thresholds_path: Path = _ALERT_THRESHOLDS_PATH,
    today: date | None = None,
) -> str:
    """Aggregate the previous calendar month and return the Telegram message."""
    today = today or datetime.now(timezone.utc).date()
    period_start, period_end = previous_month(today)
    return build_report(
        load_jsonl(outcomes_path),
        period_start=period_start,
        period_end=period_end,
        signal_types=signal_types_by_event(load_jsonl(runs_path)),
        classifications=load_classifications(thresholds_path),
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for `python3 -m market_intelligence.monthly_report`.

    The n8n trigger fires every Friday (Vixie cron cannot AND day-of-month with
    day-of-week); this guard keeps only the first Friday. On other Fridays nothing
    is printed so the workflow's If node skips the Telegram send.
    """
    argparse.ArgumentParser(description="Rapport mensuel de track record (Telegram).").parse_args(argv)
    today = datetime.now(timezone.utc).date()
    if not is_first_friday(today):
        return 0
    text = run(today=today)
    # Epic 9 S4 : le découpage Telegram est fait ici ; le nœud n8n
    # `Split for Telegram` ne fait plus que relayer `chunks`.
    print(
        json.dumps(
            {"text": text, "chunks": split_telegram_html(text)}, ensure_ascii=False
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
