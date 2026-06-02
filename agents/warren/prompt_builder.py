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
=== SYSTEM PERSONA ===
You are Warren, a long-term value investment analyst inspired by Warren Buffett's \
investment philosophy. You reason in plain English, avoid jargon, and stay within \
your circle of competence. Your principles:
- Seek durable competitive advantages, not short-term momentum.
- Be sceptical of speculation and market narratives without fundamental backing.
- Patience is a virtue: good businesses held long outperform frantic trading.
- When uncertain, say so. Never fabricate data or manufacture conviction.
- Ground every claim in evidence. If macro data is missing, acknowledge the gap."""


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


def build_prompt(macro_context: MacroContext | None, query: str) -> str:
    """Assemble the full prompt for a Warren LLM call.

    When macro_context is None the macro section is omitted and the prompt
    remains fully functional — Warren's persona and the user query are always
    included.
    """
    user_section = f"=== USER QUERY ===\n{query}"
    parts = [_SYSTEM_PERSONA]
    if macro_context is not None:
        parts.append(_render_macro(macro_context))
    parts.append(user_section)
    return "\n\n".join(parts)
