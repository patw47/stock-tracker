"""Fixture-only tests for the v5 -> watchlist bridge. No network, ever."""
from __future__ import annotations

import json
from urllib.error import URLError

import pytest

from market_intelligence import v5_bridge

MANUAL = {"symbol": "NVDA", "added": "2026-01-05"}  # Telegram add: untouchable


def _payload(*rows: tuple[str, int, int]) -> dict:
    """A frozen /api/scan payload: (ticker, window, days_held) tracking rows.

    Shape mirrors the live payload of 2026-08-04: a flat ``v5.tracking`` list of
    window x ticker rows, alongside the v4 journal the bridge must ignore.
    """
    return {
        "scanned_at": "2026-08-04T20:40:00Z",
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
                                "source": "smallcaps-v5"}]

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

    assert v5_bridge.parse_tracking(payload) == {"ATNF": 19}  # max days_held wins

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
        result = v5_bridge.run(watchlist_path=watchlist.path)

    assert result["anomalies"] == ["v5 tracking unavailable"]
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

    result = v5_bridge.reconcile({"ATNF": 3}, watchlist_path=absent)

    assert result["added"] == [] and "does not exist" in result["anomalies"][0]
    assert not absent.exists()


def test_row_without_days_held_is_skipped(watchlist):
    """Delisted rows come back with days_held null - never bridged."""
    payload = _payload(("ATNF", 14, 3))
    payload["v5"]["tracking"].append(
        {"ticker": "GONE", "entry_date": "2026-08-03", "days_held": None,
         "status": "données absentes (délisting ?)"}
    )

    assert v5_bridge.parse_tracking(payload) == {"ATNF": 3}


# 6. suspiciously empty tracking -------------------------------------------

def test_empty_tracking_with_live_bridged_tickers_never_purges(watchlist, caplog):
    watchlist.write([{"symbol": "ATNF", "added": "2026-08-01", "source": "smallcaps-v5"}])

    with caplog.at_level("WARNING"):
        result = _reconcile(_payload(), watchlist)

    assert result["removed"] == []
    assert "not a purge" in result["anomalies"][0]
    assert "ATNF" in caplog.text
    assert watchlist.symbols() == ["ATNF"]


# 7. cap reached ------------------------------------------------------------

def test_cap_excludes_the_overflow_and_logs_it(watchlist, caplog):
    rows = [(f"T{i:03d}", 14, 5) for i in range(6)]

    with caplog.at_level("WARNING"):
        result = _reconcile(_payload(*rows), watchlist, cap=4)

    assert result["added"] == ["T000", "T001", "T002", "T003"]
    assert result["anomalies"] == ["cap of 4 bridged tickers reached, excluded: T004, T005"]
    assert "T004, T005" in caplog.text  # never a silent truncation
    assert len(watchlist.symbols()) == 4
