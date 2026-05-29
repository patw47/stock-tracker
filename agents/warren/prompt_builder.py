"""Build the full prompt string for Warren LLM calls.

Usage::

    from agents.warren.models import MacroContext
    from agents.warren.prompt_builder import build_prompt

    prompt = build_prompt(macro_snapshot, query)
    # pass prompt to your LLM client
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


def _render_macro(macro: MacroContext) -> str:
    flows_str: str
    if macro.sector_flows is None:
        flows_str = "N/A"
    else:
        flows_str = ", ".join(
            f"{sector}: {flow:+.2e}" for sector, flow in macro.sector_flows.items()
        )

    return f"""\
=== MACRO CONTEXT ===
As-of Date         : {_fmt(macro.snapshot_date)}
Fed Funds Rate     : {_fmt(macro.policy_rate, "%")}
CPI YoY            : {_fmt(macro.cpi_yoy, "%")}
Core PCE YoY       : {_fmt(macro.pce_yoy, "%")}
10Y-2Y Yield Spread: {_fmt(macro.yield_curve_spread_10y2y, "%")}
VIX                : {_fmt(macro.vix)}
Central Bank Tone  : {_fmt(macro.central_bank_tone)}
Sector Flows       : {flows_str}"""


def build_prompt(macro_snapshot: MacroContext, query: str) -> str:
    """Assemble the full prompt for a Warren LLM call."""
    user_section = f"=== USER QUERY ===\n{query}"
    return "\n\n".join([_SYSTEM_PERSONA, _render_macro(macro_snapshot), user_section])
