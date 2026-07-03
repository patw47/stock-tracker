"""Epic 4 Sprint 2 — hook onboarding dans le flux d'ajout Telegram.

Vérifie que _do_add :
  - onboard chaque symbole avant de l'écrire dans la liste runtime ;
  - refuse un symbole invalide sans l'ajouter au fichier runtime ;
  - inclut la notification onboard dans la réponse Telegram.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from agents.warren import manage_tickers


@dataclass
class _Res:
    symbol: str
    status: str


def _setup(monkeypatch, tmp_path):
    portfolio = tmp_path / "portfolio.json"
    portfolio.write_text(json.dumps({"tickers": []}), encoding="utf-8")
    monkeypatch.setitem(manage_tickers.FILES, "portfolio", str(portfolio))
    monkeypatch.setattr(manage_tickers, "clear_pending", lambda: None)
    sent: list[str] = []
    monkeypatch.setattr(manage_tickers, "send", lambda text, keyboard=None: sent.append(text))
    monkeypatch.setattr(manage_tickers, "format_onboard", lambda r: f"onboard:{r.symbol}:{r.status}")
    return portfolio, sent


def test_valid_symbol_added_and_onboarded(monkeypatch, tmp_path):
    portfolio, sent = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(manage_tickers, "_onboard", lambda s: _Res(s, "onboarded"))

    manage_tickers._do_add("portfolio", "NVDA")

    symbols = [t["symbol"] for t in json.loads(portfolio.read_text())["tickers"]]
    assert symbols == ["NVDA"]
    assert any("onboard:NVDA:onboarded" in m for m in sent)


def test_invalid_symbol_refused_not_written(monkeypatch, tmp_path):
    portfolio, sent = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(manage_tickers, "_onboard", lambda s: _Res(s, "invalid"))

    manage_tickers._do_add("portfolio", "BADSYM")

    assert json.loads(portfolio.read_text())["tickers"] == []  # aucun fichier runtime modifié
    joined = "\n".join(sent)
    assert "refused" in joined and "BADSYM" in joined


def test_mixed_valid_and_invalid(monkeypatch, tmp_path):
    portfolio, sent = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        manage_tickers, "_onboard",
        lambda s: _Res(s, "invalid" if s == "BADSYM" else "onboarded"),
    )

    manage_tickers._do_add("portfolio", "NVDA, BADSYM")

    symbols = [t["symbol"] for t in json.loads(portfolio.read_text())["tickers"]]
    assert symbols == ["NVDA"]
