"""Build the full prompt string for Warren LLM calls.

Usage::

    from agents.warren.models import MacroContext
    from agents.warren.prompt_builder import build_prompt

    prompt = build_prompt(macro_context, query)
    prompt_no_macro = build_prompt(None, query)  # macro section omitted
"""
from __future__ import annotations

from agents.warren.models import MacroContext

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
Structure every response using exactly these section headers, in this order:

**Summary** — two to three sentences: what the company does and the single most important \
investment insight.

**Key Strengths** — bullet list, three to five items, each grounded in specific evidence \
(not generic praise).

**Key Risks** — bullet list, two to four items. Focus on permanent capital impairment, not \
volatility.

**Valuation Take** — one paragraph: intrinsic value range estimate, current price context, \
explicit margin of safety or lack thereof.

**Verdict** — one sentence. Format: `[Accumulate | Hold | Avoid | Reduce] — <rationale>.`

Rules: no sections beyond the five above; no filler; if data is insufficient for a section \
write "Insufficient data — <what is missing>." rather than speculating."""

_N8N_SKILL_OUTPUT_RULES = """\
=== N8N SKILL OUTPUT RULES ===
The user query contains a pipeline skill marker. Follow that marker's task-specific \
format instead of the generic company-analysis output format.
- For [TICKER-WATCH SKILL], return only the JSON object requested by the query.
- For [EXECUTIVE-SYNTHESIS SKILL], write the French executive briefing requested by \
the query.
- Never answer NO_REPLY. If the input contains no material news, explicitly say so in \
the requested format."""


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
    return "[TICKER-WATCH SKILL]" in query or "[EXECUTIVE-SYNTHESIS SKILL]" in query


def build_prompt(macro_context: MacroContext | None, query: str) -> str:
    """Assemble the full prompt for a Warren LLM call.

    When macro_context is None the macro section is omitted and the prompt
    remains fully functional — Warren's persona and the user query are always
    included.
    """
    user_section = f"=== USER QUERY ===\n{query}"
    parts = [_SYSTEM_PERSONA]
    if _is_n8n_skill_query(query):
        parts.append(_N8N_SKILL_OUTPUT_RULES)
    else:
        parts.append(_OUTPUT_STRUCTURE)
    if macro_context is not None:
        parts.append(_render_macro(macro_context))
    parts.append(user_section)
    return "\n\n".join(parts)
