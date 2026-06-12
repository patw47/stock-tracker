"""Build the full prompt string for Warren LLM calls.

Usage::

    from agents.warren.models import MacroContext, MacroSnapshot
    from agents.warren.prompt_builder import build_prompt

    # Legacy path — MacroContext only
    prompt = build_prompt(macro_context, query)
    prompt_no_macro = build_prompt(None, query)  # macro section omitted

    # New path — MacroSnapshot + optional briefing date
    prompt = build_prompt(None, query, macro_snapshot=snap, briefing_date="2026-06-02")
"""
from __future__ import annotations

from agents.warren.models import MacroContext, MacroSnapshot

_SYSTEM_PERSONA = """\
=== IDENTITY ===
You are Warren, a disciplined long-term value investor in the tradition of Warren Buffett \
and Charlie Munger. You evaluate businesses — not tickers. Your edge is patience, rigorous \
fundamental analysis, and an unflinching demand for a margin of safety before committing capital.

=== INVESTMENT PHILOSOPHY ===
- Circle of competence: if you cannot explain the business model and its competitive moat in \
two sentences, say so and proceed with explicit caution.
- Moat first: durable competitive advantages — brand loyalty, switching costs, network \
effects, structural cost leadership — matter more than near-term earnings beats.
- Margin of safety: a significant discount to intrinsic value is required before any \
positive verdict. Overpaying for a great business is still a mistake.
- Management quality: owner-oriented, capital-disciplined management compounds returns; \
promoters and empire-builders destroy them. Judge by capital-allocation track record, not \
investor-relations messaging.
- Patience over activity: inaction is often correct. Never manufacture conviction to fill \
silence.
- Intellectual honesty: if data is missing or uncertain, state it explicitly. Never \
fabricate figures, extrapolate with false precision, or confuse narrative momentum for \
investment merit.

=== REASONING PROTOCOL ===
Reason in this order for every analysis — work through each step before concluding:

1. Business Quality — What does the company do? How durable is the moat? Is the industry \
structurally attractive (pricing power, low capital intensity, high barriers to entry)?

2. Financial Strength — Revenue trend, operating margins, free cash flow conversion, \
balance-sheet leverage, return on invested capital. Flag red flags: declining ROIC, \
ballooning debt, one-time gains masking operating weakness.

3. Valuation — Estimate an intrinsic value range. Compare to current price. State the \
margin of safety explicitly (e.g. "trading at ~30 % discount to estimated IV" or "priced \
for perfection — no margin of safety at current levels").

4. Risks — Identify two to four material risks that could permanently impair value. Focus \
on thesis-killers (technology disruption, regulatory existential threat, leverage spiral), \
not ordinary short-term volatility.

5. Verdict — Derive a clear, actionable conclusion from the four steps above. Do not hedge \
for its own sake."""

_OUTPUT_STRUCTURE = """\
=== OUTPUT FORMAT ===
Structure every daily briefing response using exactly this template, in this order:

📈 Veille du {date}
🌍 MACRO DU JOUR
- Géopolitique : {geopolitical_notes}
- Fed : {fed_stance}
- Dollar/inflation : {dollar_signal}
→ Ambiance générale : {overall_sentiment}
- Prochaines News: {upcoming_events}
────────────────────────
📊 PORTEFEUILLE — À FAIRE AUJOURD'HUI
✅ RENFORCER    {tickers}
⚠️ CONSERVER    {tickers}
🔴 ALLÉGER      {tickers}
{per_ticker_block}
────────────────────────
👀 WATCHLIST — OPPORTUNITÉS
🟢 ACHETER     {tickers}
⏳ ATTENDRE    {tickers}
🚫 IGNORER     {tickers}
{per_ticker_block}
────────────────────────
⚠️ ALERTES
{alert_lines}

Per-ticker block format — signal line first, NO headings:
  {TICKER} {emoji} {action_label}
  {one_line_justification}
  → {conclusion}

Output rules:
1. NEVER use # markdown headings (#, ##, ###, ####) anywhere in the output.
2. NEVER include raw percentage numbers for CPI/PCE — use sentiment only.
3. Signal/recommendation must appear on the FIRST line of each ticker entry.
4. Keep justifications to one sentence maximum, in plain French a non-specialist
can read — no dense jargon, no abbreviations chains.
5. Alerts section must flag any ticker symbol that looks incorrect or ambiguous.
6. MACRO DU JOUR describes the EXTERNAL environment only — geopolitical risks and
conflicts moving markets, Fed stance, dollar — taken from the === MACRO DU JOUR ===
data section. NEVER summarize portfolio sectors or individual tickers there.
7. The REASONING PROTOCOL stays internal: never print its steps (Business Quality,
Financial Strength, Valuation, Risks, Verdict) as sections of the output."""

_N8N_SKILL_OUTPUT_RULES = """\
=== N8N SKILL OUTPUT RULES ===
The user query contains a pipeline skill marker. Follow that marker's task-specific \
format instead of the generic company-analysis output format.
- For [TICKER-WATCH SKILL], return only the JSON object requested by the query.
- Never answer NO_REPLY. If the input contains no material news, explicitly say so in \
the requested format."""

_N8N_SYNTHESIS_OUTPUT_RULES = "\n\n".join(
    (
        """\
=== N8N SKILL OUTPUT RULES ===
The user query contains the [EXECUTIVE-SYNTHESIS SKILL] pipeline marker.
Write the French executive briefing requested by the query, using EXACTLY the
output format below — it overrides any other formatting habit.
- Never answer NO_REPLY. If the input contains no material news, explicitly say so in \
the requested format.""",
        _OUTPUT_STRUCTURE,
    )
)

_N8N_MACRO_BRIEF_OUTPUT_RULES = """\
=== N8N SKILL OUTPUT RULES ===
The user query contains the [MACRO-BRIEF SKILL] pipeline marker.
Write a daily Market Context Brief in French using EXACTLY these rules:

FORMAT RULES (non-negotiable):
1. Pure flowing prose — NO bullet lists, NO tables, NO markdown.
2. NEVER use # headings (no #, ##, ###). No bold **headers** either.
3. Maximum 5 numerical values total in the entire brief (e.g. VIX, IWM variation, \
Fear & Greed score). All other data must be described qualitatively.
4. Length: 150–300 words total.
5. Signal-first: open with the dominant regime (risk-on / risk-off / neutre) and its \
main driver.
6. Rumors and market expectations MUST be explicitly labeled as such \
(e.g. "selon les attentes du marché", "rumeur non confirmée", "les opérateurs anticipent").
7. Close with a single-sentence regime conclusion: risk-on, risk-off, or neutre.

CONTENT TO COVER (use "information indisponible" for any missing rubric):
Fed stance, taux prévus, dollar, pétrole, VIX, IWM / appétit small caps, \
IPOs notables, secteurs chauds, géopolitique, Fear & Greed, rumeurs.

SECTOR ROTATION DATA (when "--- Rotation sectorielle ---" section is present):
- Translate entering/exiting sectors into flowing French prose describing capital flows.
  NEVER mention ETF tickers (XLE, NUKZ, XBI…) in the final brief — use French sector names only.
  NEVER list percentages — convert to directional language.
  Examples: "l'argent tourne vers l'énergie et le nucléaire", "la biotechnologie marque le pas",
  "les small caps surperforment le marché large, signe d'appétit pour le risque".

FEAR & GREED DATA (when "--- Fear & Greed CNN ---" section is present):
- The CNN score is the authoritative source; use it as one of the 5 allowed numerical values.
  Format: "l'indice Fear & Greed CNN pointe à 38 (Fear), témoignant d'une certaine prudence".
  Prefer this structured score over any qualitative fear/greed mention from web-search data.

Never answer NO_REPLY. Degrade gracefully when data is missing."""


def _fmt(value: object, suffix: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value}{suffix}"


def _interpret_vix(vix: float | None) -> str:
    if vix is None:
        return ""
    if vix < 15:
        return " (complacency — tail risk underpriced)"
    if vix < 20:
        return " (low fear, markets calm)"
    if vix < 30:
        return " (elevated anxiety)"
    return " (high fear — potential mean-reversion opportunity)"


def _interpret_yield_spread(spread: float | None) -> str:
    if spread is None:
        return ""
    if spread < 0:
        return " (inverted — recession signal)"
    if spread < 0.5:
        return " (flat — growth outlook uncertain)"
    return " (normal slope)"


def _interpret_10y(yield_10y: float | None) -> str:
    if yield_10y is None:
        return ""
    if yield_10y > 4.5:
        return " (elevated — compresses growth multiples)"
    if yield_10y > 3.5:
        return " (moderate — discount rates manageable)"
    return " (low — supportive for equities)"


def _interpret_tone(tone: str | None) -> str:
    if tone is None:
        return ""
    t = tone.lower()
    if "hawkish" in t:
        return " (tightening bias — headwind for long-duration assets)"
    if "dovish" in t:
        return " (easing bias — tailwind for risk assets)"
    return " (neutral — data-dependent)"


def _interpret_regime(regime: str | None) -> str:
    if regime is None:
        return ""
    mapping = {
        "risk_on": " (broad risk appetite, growth favoured)",
        "neutral": " (mixed signals, sector-selective)",
        "risk_off": " (defensive positioning warranted)",
        "crisis": " (elevated systemic stress — extreme caution)",
        "unknown": " (regime ambiguous)",
    }
    return mapping.get(regime.lower(), "")


def _render_macro_snapshot(snap: MacroSnapshot, briefing_date: str | None = None) -> str:
    date_str = briefing_date or "aujourd'hui"
    if snap.upcoming_events:
        events_str = "; ".join(f"{e.name} ({e.date})" for e in snap.upcoming_events)
    else:
        events_str = "Aucun"
    lines = [
        "=== MACRO DU JOUR ===",
        f"Date              : {date_str}",
        f"Fed               : {snap.fed_stance}",
        f"Dollar/inflation  : {snap.dollar_signal}",
        f"Géopolitique      : {snap.geopolitical_notes}",
        f"Ambiance générale : {snap.overall_sentiment}",
        f"Prochaines News   : {events_str}",
    ]
    return "\n".join(lines)


def _render_macro(macro: MacroContext) -> str:
    flows_str: str
    if macro.sector_flows is None:
        flows_str = "N/A"
    else:
        flows_str = ", ".join(
            f"{sector}: {flow:+.2e}" for sector, flow in macro.sector_flows.items()
        )

    as_of = macro.snapshot_date or (macro.as_of.date() if macro.as_of else None)

    lines = [
        "=== MACRO CONTEXT ===",
        f"As-of Date         : {_fmt(as_of)}",
        f"Fed Funds Rate     : {_fmt(macro.policy_rate, '%')}",
        f"CPI YoY            : {_fmt(macro.cpi_yoy, '%')}",
        f"Core PCE YoY       : {_fmt(macro.pce_yoy, '%')}",
        f"10Y Treasury Yield : {_fmt(macro.ten_year_yield, '%')}{_interpret_10y(macro.ten_year_yield)}",
        f"2Y Treasury Yield  : {_fmt(macro.two_year_yield, '%')}",
        f"10Y-2Y Spread      : {_fmt(macro.yield_curve_spread_10y2y, '%')}{_interpret_yield_spread(macro.yield_curve_spread_10y2y)}",
        f"VIX                : {_fmt(macro.vix)}{_interpret_vix(macro.vix)}",
        f"Central Bank Tone  : {_fmt(macro.central_bank_tone)}{_interpret_tone(macro.central_bank_tone)}",
        f"Market Regime      : {_fmt(macro.market_regime)}{_interpret_regime(macro.market_regime)}",
        f"Sector Flows       : {flows_str}",
    ]
    return "\n".join(lines)


def _is_n8n_skill_query(query: str) -> bool:
    return (
        "[TICKER-WATCH SKILL]" in query
        or "[EXECUTIVE-SYNTHESIS SKILL]" in query
        or "[MACRO-BRIEF SKILL]" in query
    )


def _render_sector_data(sector_data: dict) -> list[str]:
    """Format structured sector rotation data for the brief context."""
    lines: list[str] = ["", "--- Rotation sectorielle (calculé, zéro LLM) ---"]
    entering = sector_data.get("entering", [])
    exiting = sector_data.get("exiting", [])
    iwm_rel = sector_data.get("iwm_rel_1d")
    trend = sector_data.get("small_caps_trend", "neutre")

    if entering:
        parts = []
        for item in entering:
            ticker, name, rel = item
            rel_str = f"{rel:+.2f}% vs SPY" if rel is not None else "N/A"
            parts.append(f"{name} ({rel_str})")
        lines.append(f"Entrants 1j        : {', '.join(parts)}")
    else:
        lines.append("Entrants 1j        : données insuffisantes")

    if exiting:
        parts = []
        for item in exiting:
            ticker, name, rel = item
            rel_str = f"{rel:+.2f}% vs SPY" if rel is not None else "N/A"
            parts.append(f"{name} ({rel_str})")
        lines.append(f"Sortants 1j        : {', '.join(parts)}")
    else:
        lines.append("Sortants 1j        : données insuffisantes")

    if iwm_rel is not None:
        lines.append(f"IWM vs SPY 1j      : {iwm_rel:+.2f}% → small caps {trend}")
    else:
        lines.append(f"Small caps         : {trend}")

    return lines


def _render_fear_greed_data(fear_greed_data: dict) -> list[str]:
    """Format structured CNN Fear & Greed data for the brief context."""
    score = fear_greed_data.get("score")
    label = fear_greed_data.get("label", "")
    if score is None:
        return []
    return [
        "",
        "--- Fear & Greed CNN (structuré) ---",
        f"Score              : {score} ({label})",
    ]


def _render_macro_brief_context(
    macro_context: MacroContext | None,
    macro_snapshot: MacroSnapshot | None,
    market_closes: dict | None,
    briefing_date: str | None = None,
    sector_data: dict | None = None,
    fear_greed_data: dict | None = None,
) -> str:
    """Render combined quantitative + qualitative context for the macro brief prompt."""
    date_str = briefing_date or "aujourd'hui"
    lines = [f"=== MACRO BRIEF CONTEXT — {date_str} ==="]

    if macro_context is not None:
        lines += [
            "",
            "--- Données quantitatives FRED ---",
            f"Fed Funds Rate     : {_fmt(macro_context.policy_rate, '%')}",
            f"10Y Treasury       : {_fmt(macro_context.ten_year_yield, '%')}{_interpret_10y(macro_context.ten_year_yield)}",
            f"2Y Treasury        : {_fmt(macro_context.two_year_yield, '%')}",
            f"10Y-2Y Spread      : {_fmt(macro_context.yield_curve_spread_10y2y, '%')}{_interpret_yield_spread(macro_context.yield_curve_spread_10y2y)}",
            f"VIX                : {_fmt(macro_context.vix)}{_interpret_vix(macro_context.vix)}",
            f"Dollar Index       : {_fmt(macro_context.dollar_index)}",
            f"S&P 500            : {_fmt(macro_context.spx_level)} ({_fmt(macro_context.spx_pct_change_1m, '% 1m')})",
        ]

    if market_closes:
        iwm_close = market_closes.get("iwm_close")
        iwm_pct = market_closes.get("iwm_pct_1d")
        oil_close = market_closes.get("oil_close")
        oil_pct = market_closes.get("oil_pct_1d")
        if iwm_close is not None or oil_close is not None:
            lines += ["", "--- Marchés quasi-réel ---"]
        if iwm_close is not None:
            iwm_pct_str = f" ({iwm_pct:+.2f}% j/j)" if iwm_pct is not None else ""
            lines.append(f"IWM (small caps)   : {iwm_close:.2f}{iwm_pct_str}")
        if oil_close is not None:
            oil_pct_str = f" ({oil_pct:+.2f}% j/j)" if oil_pct is not None else ""
            lines.append(f"Crude Oil WTI      : {oil_close:.2f}{oil_pct_str}")

    if macro_snapshot is not None:
        lines += [
            "",
            "--- Signaux qualitatifs (web search) ---",
            f"Fed stance         : {macro_snapshot.fed_stance}",
            f"Dollar             : {macro_snapshot.dollar_signal}",
            f"Géopolitique       : {macro_snapshot.geopolitical_notes}",
            f"Ambiance générale  : {macro_snapshot.overall_sentiment}",
        ]
        if macro_snapshot.rate_expectations:
            lines.append(f"Attentes de taux   : {macro_snapshot.rate_expectations}")
        if macro_snapshot.ipos:
            lines.append(f"IPOs notables      : {macro_snapshot.ipos}")
        # Suppress qualitative hot_sectors when structured sector_data is present
        if macro_snapshot.hot_sectors and sector_data is None:
            lines.append(f"Secteurs chauds    : {macro_snapshot.hot_sectors}")
        # Suppress qualitative fear_greed when structured CNN data is present
        if macro_snapshot.fear_greed and fear_greed_data is None:
            lines.append(f"Fear & Greed       : {macro_snapshot.fear_greed}")
        if macro_snapshot.notable_rumors:
            lines.append(f"Rumeurs (non conf.): {macro_snapshot.notable_rumors}")
        if macro_snapshot.upcoming_events:
            events_str = "; ".join(
                f"{e.name} ({e.date})" for e in macro_snapshot.upcoming_events
            )
            lines.append(f"Évènements à venir : {events_str}")
    else:
        lines += [
            "",
            "--- Signaux qualitatifs indisponibles ---",
            "(La recherche web a échoué. Rédiger le brief sur la base des données "
            "quantitatives uniquement, avec mention explicite de l'incertitude.)",
        ]

    if sector_data is not None:
        lines += _render_sector_data(sector_data)

    if fear_greed_data is not None:
        lines += _render_fear_greed_data(fear_greed_data)

    return "\n".join(lines)


def build_prompt(
    macro_context: MacroContext | None,
    query: str,
    *,
    macro_snapshot: MacroSnapshot | None = None,
    briefing_date: str | None = None,
    market_closes: dict | None = None,
    sector_data: dict | None = None,
    fear_greed_data: dict | None = None,
) -> str:
    """Assemble the full prompt for a Warren LLM call.

    Priority: macro_snapshot > macro_context for the macro context section.
    When both are None the macro section is omitted.
    New callers should prefer macro_snapshot + briefing_date; macro_context
    remains supported until warren-orchestration-wiring migrates.
    sector_data and fear_greed_data inject structured Sprint 3 quantitative
    data into the macro-brief context; they are ignored for other skills.
    """
    if "[TICKER-WATCH SKILL]" in query:
        output_rules = _N8N_SKILL_OUTPUT_RULES
    elif "[EXECUTIVE-SYNTHESIS SKILL]" in query:
        output_rules = _N8N_SYNTHESIS_OUTPUT_RULES
    elif "[MACRO-BRIEF SKILL]" in query:
        output_rules = _N8N_MACRO_BRIEF_OUTPUT_RULES
    else:
        output_rules = _OUTPUT_STRUCTURE
    parts = [_SYSTEM_PERSONA, output_rules]
    if "[MACRO-BRIEF SKILL]" in query:
        parts.append(
            _render_macro_brief_context(
                macro_context,
                macro_snapshot,
                market_closes,
                briefing_date,
                sector_data=sector_data,
                fear_greed_data=fear_greed_data,
            )
        )
    elif macro_snapshot is not None:
        parts.append(_render_macro_snapshot(macro_snapshot, briefing_date))
    elif macro_context is not None:
        parts.append(_render_macro(macro_context))
    parts.append(f"=== USER QUERY ===\n{query}")
    return "\n\n".join(parts)
