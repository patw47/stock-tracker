"""Fixture-only tests for the v5 -> watchlist bridge. No network, ever."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from urllib.error import URLError

import pytest

from market_intelligence import ticker_onboard, v5_bridge

MANUAL = {"symbol": "NVDA", "added": "2026-01-05"}  # Telegram add: untouchable


@dataclass(frozen=True)
class _FakeValidation:
    status: str
    actual_name: str = ""
    reason: str = ""


@pytest.fixture(autouse=True)
def referentials(tmp_path, monkeypatch):
    """Point the Layer B referentials at tmp_path and stub symbol validation.

    AUTOUSE on purpose: since Epic 10 S2 a reconciliation writes the registry and
    validates symbols over the network. Left to their defaults, every test in this
    file would hit yfinance and edit the real referential files.

    Since Epic 10 S4 the three files it writes are STATE (registry, classifications,
    single-factor list); the sector map next to them is configuration, seeded here
    read-only so nothing in this file can rewrite a committed config file.
    """
    seeds = {
        "REGISTRY_PATH": ("registry.json", {
            "portfolio_tickers": [], "macro_tickers": [], "factor_tickers": [], "alias_map": {},
        }),
        "CLASSIFICATIONS_PATH": ("classifications.json", {"classifications": {}}),
        "SINGLE_FACTORS_PATH": ("single_factor_symbols.json", {"single_factor_symbols": []}),
        "SECTOR_FACTORS_PATH": ("sector_factors.json", {
            "market_factor": "IWM", "sector_factors": {},
        }),
    }
    for attr, (name, seed) in seeds.items():
        path = tmp_path / name
        path.write_text(json.dumps(seed), encoding="utf-8")
        monkeypatch.setattr(v5_bridge, attr, path)
    # An empty portfolio by default: nothing held, so nothing protected. The
    # `portfolio` fixture overwrites it for the tests that need holdings.
    (tmp_path / "portfolio.json").write_text('{"tickers": []}', encoding="utf-8")
    monkeypatch.setattr(v5_bridge, "PORTFOLIO_PATH", tmp_path / "portfolio.json")
    monkeypatch.setattr(
        ticker_onboard, "_validate_symbol",
        lambda sym: _FakeValidation(status="ok", actual_name=f"{sym} Inc"),
    )

    class _Referentials:
        registry = tmp_path / "registry.json"
        thresholds = tmp_path / "classifications.json"
        factors = tmp_path / "single_factor_symbols.json"
        sector_map = tmp_path / "sector_factors.json"

        def symbols(self) -> set[str]:
            data = json.loads(self.registry.read_text(encoding="utf-8"))
            return {t["symbol"] for t in data["portfolio_tickers"]}

        def classifications(self) -> dict:
            return json.loads(self.thresholds.read_text(encoding="utf-8"))["classifications"]

        def factor_covered(self) -> set[str]:
            single = json.loads(self.factors.read_text(encoding="utf-8"))
            sector = json.loads(self.sector_map.read_text(encoding="utf-8"))
            return set(sector["sector_factors"]) | set(single["single_factor_symbols"])

    return _Referentials()


@pytest.fixture
def portfolio(referentials, tmp_path):
    """A temporary portfolio.json; the list that protects its tickers.

    Depends on ``referentials`` so it always runs after it and its holdings win.
    """
    path = tmp_path / "portfolio.json"

    def _write(*symbols: str):
        path.write_text(
            json.dumps({"tickers": [{"symbol": s} for s in symbols]}), encoding="utf-8"
        )
        return path

    _write.path = path
    _write()
    return _write


def _payload(*rows: tuple[str, int, int], scanned_at: str = "2026-08-04T20:40:00Z") -> dict:
    """A frozen /api/scan payload: (ticker, window, days_held) tracking rows.

    Shape mirrors the live payload of 2026-08-04: a flat ``v5.tracking`` list of
    window x ticker rows, alongside the v4 journal the bridge must ignore.
    """
    return {
        "scanned_at": scanned_at,
        "universe_size": 812,
        # Previous protocol, different cohort: must never reach the watchlist.
        "v4_tracking": [
            {"ticker": "V4ONLY", "entry_date": "2026-07-01", "days_held": 3, "window": 21}
        ],
        "v5": {
            "windows": {
                # Cohort rows carry no days_held -> not tracking rows.
                "7": {"mkt": 0.4, "cohort": [{"ticker": "ATNF", "price": 4.2}], "prelist": []}
            },
            "tracking": [
                {
                    "ticker": ticker,
                    "entry_date": "2026-07-01",
                    "entry_price": 4.20,
                    "days_held": days_held,
                    "window": window,
                    "status": "",
                }
                for ticker, window, days_held in rows
            ],
        },
    }


def _context(days_held: int) -> dict:
    """Le contexte de cohorte que ``_payload`` fait porter à chaque ligne suivie."""
    return {"entry_date": "2026-07-01", "entry_price": 4.20, "days_held": days_held}


def _tracked(**days_held: int) -> dict[str, dict]:
    """Journal de suivi par symbole, la forme que ``parse_tracking`` retourne."""
    return {symbol: _context(days) for symbol, days in days_held.items()}


@pytest.fixture
def watchlist(tmp_path):
    """A temporary watchlist.json; call .write(entries) / .read() around a run."""

    class _Watchlist:
        path = tmp_path / "watchlist.json"

        def write(self, entries: list[dict]) -> None:
            self.path.write_text(json.dumps({"tickers": entries}), encoding="utf-8")

        def read(self) -> list[dict]:
            return json.loads(self.path.read_text(encoding="utf-8"))["tickers"]

        def symbols(self) -> list[str]:
            return [t["symbol"] for t in self.read()]

    wl = _Watchlist()
    wl.write([])
    return wl


def _reconcile(payload, watchlist, **kwargs):
    tracked = v5_bridge.parse_tracking(payload)
    return v5_bridge.reconcile(tracked, watchlist_path=watchlist.path, **kwargs)


# 1. simple add -------------------------------------------------------------

def test_tracked_ticker_is_added_and_tagged(watchlist):
    result = _reconcile(_payload(("ATNF", 14, 3)), watchlist)

    assert result["added"] == ["ATNF"]
    assert watchlist.read() == [{"symbol": "ATNF", "added": v5_bridge._today(),
                                "source": "smallcaps-v5", "cohort": _context(3)}]

    # Reconciliation, not an event stream: a second run is a no-op.
    again = _reconcile(_payload(("ATNF", 14, 4)), watchlist)
    assert (again["added"], again["removed"], again["unchanged"]) == ([], [], 1)


# 2. exit at J+63 -----------------------------------------------------------

def test_bridged_ticker_leaves_at_horizon(watchlist):
    watchlist.write([{"symbol": "ATNF", "added": "2026-05-01", "source": "smallcaps-v5"}])

    result = _reconcile(_payload(("ATNF", 14, 63)), watchlist)

    assert result["removed"] == ["ATNF"]
    assert watchlist.symbols() == []


def test_bridged_ticker_leaves_when_gone_from_tracking(watchlist):
    watchlist.write([{"symbol": "ATNF", "added": "2026-05-01", "source": "smallcaps-v5"}])

    result = _reconcile(_payload(("SNTI", 7, 2)), watchlist)

    assert (result["removed"], result["added"]) == (["ATNF"], ["SNTI"])
    assert watchlist.symbols() == ["SNTI"]


# 3. same ticker in several windows -> one entry ----------------------------

def test_ticker_in_three_windows_yields_one_entry(watchlist):
    payload = _payload(("ATNF", 7, 5), ("ATNF", 14, 12), ("ATNF", 21, 19))

    assert v5_bridge.parse_tracking(payload) == _tracked(ATNF=19)  # max days_held

    result = _reconcile(payload, watchlist)
    assert result["added"] == ["ATNF"]
    assert watchlist.symbols() == ["ATNF"]


# 4. manual ticker untouched, on add as on removal --------------------------

def test_manual_ticker_is_never_added_over_nor_removed(watchlist):
    watchlist.write([MANUAL, {"symbol": "ATNF", "added": "2026-05-01",
                              "source": "smallcaps-v5"}])

    # On add: NVDA is tracked under horizon but already there manually — no
    # duplicate entry, and no provenance tag grafted onto the manual one.
    result = _reconcile(_payload(("NVDA", 14, 10), ("ATNF", 14, 20)), watchlist)
    assert result["added"] == []
    assert watchlist.read()[0] == MANUAL

    # On removal: NVDA is past horizon and ATNF is gone from the journal. Only
    # the bridge's own entry may leave; the manual one is untouchable.
    result = _reconcile(_payload(("NVDA", 14, 70)), watchlist)
    assert result["removed"] == ["ATNF"]
    assert watchlist.read() == [MANUAL]


# 5. API dead ---------------------------------------------------------------

def test_dead_api_is_a_logged_no_op(monkeypatch, watchlist, caplog):
    watchlist.write([{"symbol": "ATNF", "added": "2026-05-01", "source": "smallcaps-v5"}])

    def _boom(*args, **kwargs):
        raise URLError("connection refused")

    monkeypatch.setattr(v5_bridge, "urlopen", _boom)

    with caplog.at_level("ERROR"):
        result = v5_bridge.run(
            api_url="http://localhost:8000", watchlist_path=watchlist.path
        )

    assert result["anomalies"] == ["v5 snapshot unavailable"]
    assert "unreachable" in caplog.text
    assert watchlist.symbols() == ["ATNF"]


def test_malformed_payload_is_a_no_op():
    assert v5_bridge.parse_tracking("not a payload") is None
    assert v5_bridge.parse_tracking({"v4_tracking": []}) is None  # no v5 = unusable


def test_v4_journal_is_never_bridged(watchlist):
    """The live payload carries both journals; only v5 members are cohort members."""
    result = _reconcile(_payload(("ATNF", 14, 3)), watchlist)

    assert result["added"] == ["ATNF"]  # V4ONLY sits in v4_tracking, ignored
    assert "V4ONLY" not in watchlist.symbols()


def test_missing_watchlist_is_a_no_op_not_a_creation(tmp_path):
    """Creating watchlist.json in a dev checkout would shadow the example file."""
    absent = tmp_path / "watchlist.json"

    result = v5_bridge.reconcile(_tracked(ATNF=3), watchlist_path=absent)

    assert result["added"] == [] and "does not exist" in result["anomalies"][0]
    assert not absent.exists()


def test_row_without_days_held_is_skipped(watchlist):
    """Delisted rows come back with days_held null - never bridged."""
    payload = _payload(("ATNF", 14, 3))
    payload["v5"]["tracking"].append(
        {"ticker": "GONE", "entry_date": "2026-08-03", "days_held": None,
         "status": "données absentes (délisting ?)"}
    )

    assert v5_bridge.parse_tracking(payload) == _tracked(ATNF=3)


# 6. suspiciously empty tracking -------------------------------------------

def test_empty_tracking_with_live_bridged_tickers_never_purges(watchlist, caplog):
    watchlist.write([{"symbol": "ATNF", "added": "2026-08-01", "source": "smallcaps-v5"}])

    with caplog.at_level("WARNING"):
        result = _reconcile(_payload(), watchlist)

    assert result["removed"] == []
    assert "not a purge" in result["anomalies"][0]
    assert "ATNF" in caplog.text
    assert watchlist.symbols() == ["ATNF"]


# 6bis. source fichier + garde de monotonie (Epic 10 S1) --------------------
#
# Piège de chemin : watchlist.json est gitignoré, donc `git diff` y est vide par
# construction et prouverait n'importe quoi. La non-écriture se prouve par une
# empreinte SHA-256 prise avant exécution et revérifiée après.

def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def snapshot(tmp_path):
    """Le fichier que le poste pousse; .write(payload) puis run(snapshot_path=...)."""
    path = tmp_path / "latest.json"

    def _write(payload: dict):
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    _write.path = path
    return _write


def test_fresh_snapshot_from_a_file_drives_the_reconciliation(watchlist, snapshot):
    watchlist.write([{"symbol": "OLD", "added": "2026-05-01", "source": "smallcaps-v5"}])
    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-13T08:10:01+00:00"))

    result = v5_bridge.run(
        snapshot_path=snapshot.path, watchlist_path=watchlist.path
    )

    assert result["added"] == ["ATNF"] and result["removed"] == ["OLD"]
    assert watchlist.symbols() == ["ATNF"]


def test_replaying_the_same_snapshot_writes_nothing(watchlist, snapshot):
    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-13T08:10:01+00:00"))
    v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)

    before = _digest(watchlist.path)
    result = v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)

    assert (result["added"], result["removed"]) == ([], [])
    assert _digest(watchlist.path) == before  # tolérance nulle


def test_older_snapshot_is_refused_and_logged(watchlist, snapshot, caplog):
    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-13T08:10:01+00:00"))
    v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)
    before = _digest(watchlist.path)

    # Un instantané d'avant-hier re-poussé : il retirerait ATNF, qualifié depuis.
    snapshot(_payload(("SNTI", 7, 2), scanned_at="2026-08-11T08:00:00+00:00"))
    with caplog.at_level("WARNING"):
        result = v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)

    assert (result["added"], result["removed"]) == ([], [])
    assert "not newer than the applied" in result["anomalies"][0]
    assert "refused, no write" in caplog.text
    assert _digest(watchlist.path) == before
    assert watchlist.symbols() == ["ATNF"]


def test_fresh_snapshot_without_change_leaves_the_file_untouched(watchlist, snapshot):
    """Écriture conditionnelle préservée : plus récent mais rien à faire = rien.

    « Rien à faire » inclut le contexte de cohorte depuis le S3 : même cohorte ET
    même contexte (``days_held`` inchangé) = fichier intact. Un ``days_held`` qui
    avance est, lui, un vrai changement de contenu — il doit être écrit, sinon
    l'alerte afficherait un « jour 27/63 » périmé.
    """
    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-13T08:10:01+00:00"))
    v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)
    before = _digest(watchlist.path)

    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-14T08:10:01+00:00"))
    result = v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)

    assert (result["added"], result["removed"], result["unchanged"]) == ([], [], 1)
    assert _digest(watchlist.path) == before


def test_absent_then_unreadable_snapshot_is_a_logged_no_op(watchlist, tmp_path, caplog):
    watchlist.write([{"symbol": "ATNF", "added": "2026-05-01", "source": "smallcaps-v5"}])
    before = _digest(watchlist.path)

    with caplog.at_level("ERROR"):
        absent = v5_bridge.run(
            snapshot_path=tmp_path / "nope.json", watchlist_path=watchlist.path
        )
    assert absent["anomalies"] == ["v5 snapshot unavailable"]
    assert "unreadable" in caplog.text

    broken = tmp_path / "latest.json"
    broken.write_text('{"scanned_at": "2026-08-13T0', encoding="utf-8")
    caplog.clear()
    with caplog.at_level("ERROR"):
        result = v5_bridge.run(snapshot_path=broken, watchlist_path=watchlist.path)

    assert result["anomalies"] == ["v5 snapshot unavailable"]
    assert "not valid JSON" in caplog.text
    assert _digest(watchlist.path) == before  # aucune écriture dans les deux cas


def test_snapshot_without_scanned_at_never_overwrites_a_guarded_watchlist(
    watchlist, snapshot
):
    """Un instantané sans horodatage ne peut pas prouver qu'il est plus récent."""
    snapshot(_payload(("ATNF", 14, 3), scanned_at="2026-08-13T08:10:01+00:00"))
    v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)
    before = _digest(watchlist.path)

    payload = _payload(("SNTI", 7, 2))
    payload.pop("scanned_at")
    snapshot(payload)
    result = v5_bridge.run(snapshot_path=snapshot.path, watchlist_path=watchlist.path)

    assert result["added"] == [] and "not newer" in result["anomalies"][0]
    assert _digest(watchlist.path) == before


# 6ter. le référentiel suit la cohorte (Epic 10 S2) -------------------------

def test_added_symbol_gets_registry_classification_and_factor(watchlist, referentials):
    result = _reconcile(_payload(("ATNF", 14, 3)), watchlist)

    assert result["added"] == ["ATNF"]
    assert "ATNF" in referentials.symbols()
    assert referentials.classifications()["ATNF"] == "speculative"
    assert "ATNF" in referentials.factor_covered()
    assert result["referential"]["onboarded"] == 1


def _seed(referentials, *symbols: str) -> None:
    """Onboard symbols the way the Telegram add flow already does."""
    for symbol in symbols:
        ticker_onboard.onboard_ticker(
            symbol,
            registry_path=referentials.registry,
            classifications_path=referentials.thresholds,
            single_factors_path=referentials.factors,
            sector_factors_path=referentials.sector_map,
        )


def test_portfolio_ticker_is_never_offboarded(watchlist, portfolio, referentials):
    """Artefact : les symboles de portefeuille classés ; invariant : ensemble identique."""
    held = {"HELD"}
    portfolio(*held)
    _seed(referentials, "HELD")
    watchlist.write([{"symbol": "HELD", "added": "2026-05-01", "source": "smallcaps-v5"}])
    before = set(referentials.classifications()) & held
    assert before == held

    # HELD sort de la cohorte, mais reste détenu : la watchlist le lâche, pas le
    # référentiel — sinon la détection d'anomalie cesse sur un titre en portefeuille.
    result = v5_bridge.reconcile(_tracked(SNTI=2), watchlist_path=watchlist.path,
                                 portfolio_path=portfolio.path)

    assert result["removed"] == ["HELD"] and watchlist.symbols() == ["SNTI"]
    assert set(referentials.classifications()) & held == before  # tolérance nulle
    assert held <= referentials.symbols() and held <= referentials.factor_covered()


def test_one_failing_validation_never_sinks_the_others(
    watchlist, referentials, monkeypatch, caplog
):
    """Isolation réseau : un symbole en échec est un saut journalisé, pas une exception."""
    def _flaky(symbol):
        if symbol == "BOOM":
            raise OSError("yfinance timeout")
        return _FakeValidation(status="ok", actual_name=f"{symbol} Inc")

    monkeypatch.setattr(ticker_onboard, "_validate_symbol", _flaky)

    with caplog.at_level("ERROR"):
        result = _reconcile(_payload(("AAA", 14, 3), ("BOOM", 14, 3), ("ZZZ", 14, 3)),
                            watchlist)

    assert result["added"] == ["AAA", "BOOM", "ZZZ"]  # la watchlist, elle, converge
    assert referentials.symbols() == {"AAA", "ZZZ"}
    assert result["referential"] == {"onboarded": 2, "offboarded": 0, "skipped": 0,
                                     "failed": 1}
    assert "onboarding BOOM failed" in caplog.text


def test_registry_equals_cohort_union_portfolio(watchlist, portfolio, referentials):
    portfolio("HELD")
    watchlist.write([{"symbol": "OLD", "added": "2026-05-01", "source": "smallcaps-v5"}])
    _seed(referentials, "OLD", "HELD")  # état de départ cohérent

    v5_bridge.reconcile(_tracked(ATNF=3, SNTI=2), watchlist_path=watchlist.path,
                        portfolio_path=portfolio.path)

    cohort = set(watchlist.symbols())
    assert cohort == {"ATNF", "SNTI"}
    assert referentials.symbols() == cohort | {"HELD"}
    assert set(referentials.classifications()) == cohort | {"HELD"}
    assert referentials.factor_covered() == cohort | {"HELD"}


# 7. cap reached ------------------------------------------------------------

def test_cap_excludes_the_overflow_and_logs_it(watchlist, caplog):
    rows = [(f"T{i:03d}", 14, 5) for i in range(6)]

    with caplog.at_level("WARNING"):
        result = _reconcile(_payload(*rows), watchlist, cap=4)

    assert result["added"] == ["T000", "T001", "T002", "T003"]
    assert result["anomalies"] == ["cap of 4 bridged tickers reached, excluded: T004, T005"]
    assert "T004, T005" in caplog.text  # never a silent truncation
    assert len(watchlist.symbols()) == 4
