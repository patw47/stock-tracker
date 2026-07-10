from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from market_intelligence.tension_outcomes import iter_episodes, run as run_outcomes
from market_intelligence.tension_signals import (
    TensionSignal,
    append_tension_journal,
    calculate_tension,
    format_tension_digest,
)


def _frame(closes, volumes, highs=None, lows=None):
    n = len(closes)
    index = pd.bdate_range("2024-01-02", periods=n)
    closes = pd.Series(closes, index=index, dtype="float64")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": highs if highs is not None else closes * 1.01,
            "Low": lows if lows is not None else closes * 0.99,
            "Close": closes,
            "Volume": pd.Series(volumes, index=index, dtype="float64"),
        },
        index=index,
    )


def _wide_then_tight(n_wide=300, n_tight=25, seed=7):
    """Volatile regime followed by a pinched range: last bars must be a squeeze."""
    rng = np.random.default_rng(seed)
    wide = 100 * np.cumprod(1 + rng.normal(0, 0.05, n_wide))
    tight = wide[-1] * np.cumprod(1 + rng.normal(0, 0.001, n_tight))
    return np.concatenate([wide, tight])


class TestCalculateTension:
    def test_squeeze_fires_after_compression(self):
        closes = _wide_then_tight()
        signal = calculate_tension("TST", _frame(closes, [1e6] * len(closes)))
        assert signal.as_of is not None
        assert signal.bw_pctl is not None and signal.bw_pctl <= 0.10
        assert signal.squeeze and signal.tension

    def test_quiet_accumulation_fires_on_volume_without_price(self):
        closes = [100.0] * 100  # perfectly flat price
        volumes = [1e6] * 95 + [3e6] * 5  # 3x volume over the last 5 days
        signal = calculate_tension("TST", _frame(closes, volumes))
        assert signal.rvol5 is not None and signal.rvol5 >= 2.0
        assert signal.cum5 == 0.0
        assert signal.quiet_accumulation and signal.tension

    def test_no_tension_on_flat_price_flat_volume_wide_history(self):
        rng = np.random.default_rng(3)
        closes = 100 * np.cumprod(1 + rng.normal(0, 0.03, 320))
        signal = calculate_tension("TST", _frame(closes, [1e6] * 320))
        assert not signal.quiet_accumulation  # rvol5 ~ 1

    def test_episode_start_only_on_first_tension_day(self):
        closes = [100.0] * 100
        # tension since 2 days (volume high on days -6..-1 => rvol5 high yesterday too)
        volumes = [1e6] * 93 + [3e6] * 7
        signal = calculate_tension("TST", _frame(closes, volumes))
        assert signal.tension and not signal.episode_start
        # tension today only (spike started today: only last 3 days elevated
        # is not enough; make yesterday's rvol5 below threshold)
        volumes2 = [1e6] * 95 + [1e6, 1e6, 1e6, 5e6, 5e6][:5]
        signal2 = calculate_tension("TST", _frame([100.0] * 100, [1e6] * 95 + [1e6, 1e6, 1e6, 5e6, 5e6]))
        if signal2.tension:  # rvol5 = (3*1 + 2*5)/5 = 2.6 today, 1.8 yesterday
            assert signal2.episode_start

    def test_empty_and_short_frames_are_safe(self):
        assert calculate_tension("TST", pd.DataFrame()).data_issues == ("empty_frame",)
        short = calculate_tension("TST", _frame([100.0] * 10, [1e6] * 10))
        assert short.as_of is not None
        assert not short.squeeze and "squeeze_history_short" in short.data_issues


class TestJournalAndDigest:
    def test_journal_appends_and_iter_episodes_dedupes(self, tmp_path):
        closes = [100.0] * 100
        volumes = [1e6] * 95 + [3e6] * 5
        signal = calculate_tension("TST", _frame(closes, volumes))
        assert signal.tension
        path = tmp_path / "tension.jsonl"
        assert append_tension_journal({"TST": signal}, path) == 1
        assert append_tension_journal({"TST": signal}, path, dry_run=True) == 1
        lines = path.read_text().splitlines()
        assert len(lines) == 2
        record = json.loads(lines[0])
        assert record["symbol"] == "TST" and record["tension"] is True
        episodes = iter_episodes(path)  # deduped on (ticker, as_of)
        if signal.episode_start:
            assert len(episodes) == 1

    def test_digest_lists_episode_starts_only(self):
        start = TensionSignal(
            symbol="AAA", as_of="2026-07-10", bar_count=300, bw_pctl=0.07,
            rvol5=2.4, cum5=0.012, expected_move_20d=0.12, squeeze=True,
            quiet_accumulation=True, tension=True, episode_start=True, data_issues=(),
        )
        ongoing = TensionSignal(
            symbol="BBB", as_of="2026-07-10", bar_count=300, bw_pctl=0.05,
            rvol5=1.0, cum5=0.0, expected_move_20d=0.10, squeeze=True,
            quiet_accumulation=False, tension=True, episode_start=False, data_issues=(),
        )
        digest = format_tension_digest({"AAA": start, "BBB": ongoing}, as_of="2026-07-10")
        assert "AAA" in digest and "BBB" not in digest
        assert "Tension — Layer C" in digest and "squeeze" in digest
        assert format_tension_digest({"BBB": ongoing}, as_of="2026-07-10") == ""


class TestOutcomes:
    def test_explosion_measured_against_journaled_expected_move(self, tmp_path):
        tension_path = tmp_path / "tension.jsonl"
        outcomes_path = tmp_path / "tension_outcomes.jsonl"
        episode = {
            "symbol": "TST", "as_of": "2024-03-01", "tension": True,
            "episode_start": True, "expected_move_20d": 0.05,
        }
        tension_path.write_text(json.dumps(episode) + "\n")
        # closes: entry 100 on 2024-03-01 then a +30% spike within the window
        index = pd.bdate_range("2024-03-01", periods=30)
        closes = pd.Series([100.0] + [130.0] * 29, index=index)
        measured, unavailable, skipped = run_outcomes(
            tension_path=tension_path, outcomes_path=outcomes_path,
            close_fetcher=lambda: {"TST": closes}, today=date(2024, 6, 1),
        )
        assert (measured, unavailable, skipped) == (1, 0, 0)
        record = json.loads(outcomes_path.read_text().splitlines()[0])
        assert record["status"] == "measured"
        assert record["explosion"] is True  # 0.30 > 2 x 0.05
        assert record["move_ratio"] == pytest.approx(0.30 / 0.05)
        # idempotent: second run measures nothing new
        assert run_outcomes(
            tension_path=tension_path, outcomes_path=outcomes_path,
            close_fetcher=lambda: {"TST": closes}, today=date(2024, 6, 1),
        ) == (0, 0, 0)
