from __future__ import annotations

import json
from unittest.mock import patch

import pandas as pd
import pytest

from market_intelligence.normalize_quality import normalize, run_normalization

# Convenience: patch to_parquet on pd.DataFrame so tests don't require pyarrow/fastparquet.
_patch_to_parquet = patch.object(pd.DataFrame, "to_parquet", return_value=None)


class TestNormalize:
    def test_65_row_short_history_false(self, sample_ohlcv_df):
        _, meta = normalize("AAPL", sample_ohlcv_df)
        assert meta["short_history"] is False
        assert meta["bar_count"] == 65

    def test_65_row_columns_lowercase(self, sample_ohlcv_df):
        clean_df, _ = normalize("AAPL", sample_ohlcv_df)
        assert set(clean_df.columns) == {"open", "high", "low", "close", "volume"}

    def test_65_row_index_is_datetimeindex(self, sample_ohlcv_df):
        clean_df, _ = normalize("AAPL", sample_ohlcv_df)
        assert isinstance(clean_df.index, pd.DatetimeIndex)

    def test_30_row_short_history_true(self, short_ohlcv_df):
        _, meta = normalize("TSLA", short_ohlcv_df)
        assert meta["short_history"] is True

    def test_empty_df_short_history_true_bar_count_zero(self, empty_ohlcv_df):
        _, meta = normalize("AAPL", empty_ohlcv_df)
        assert meta["short_history"] is True
        assert meta["bar_count"] == 0

    def test_multiindex_columns_flattened(self, sample_ohlcv_df):
        df = sample_ohlcv_df.copy()
        df.columns = pd.MultiIndex.from_tuples([(c, "AAPL") for c in df.columns])
        clean_df, _ = normalize("AAPL", df)
        assert not isinstance(clean_df.columns, pd.MultiIndex)
        assert "open" in clean_df.columns


class TestRunNormalization:
    def test_parquet_written_with_safe_filename(self, tmp_path, sample_ohlcv_df):
        ohlcv_dir = tmp_path / "ohlcv"
        quality_report = tmp_path / "quality_report.json"

        with (
            _patch_to_parquet as mock_to_parquet,
            patch("market_intelligence.normalize_quality._DATA_DIR", ohlcv_dir),
            patch("market_intelligence.normalize_quality._QUALITY_REPORT", quality_report),
            patch(
                "market_intelligence.normalize_quality.fetch_all",
                return_value={"AAPL": sample_ohlcv_df},
            ),
        ):
            run_normalization()

        mock_to_parquet.assert_called_once()
        written_path = mock_to_parquet.call_args[0][0]
        assert written_path.name == "AAPL.parquet"
        assert written_path.parent == ohlcv_dir

    def test_quality_report_written_one_entry_per_ticker(
        self, tmp_path, sample_ohlcv_df, short_ohlcv_df
    ):
        ohlcv_dir = tmp_path / "ohlcv"
        quality_report = tmp_path / "quality_report.json"
        data = {"AAPL": sample_ohlcv_df, "TSLA": short_ohlcv_df}

        with (
            _patch_to_parquet,
            patch("market_intelligence.normalize_quality._DATA_DIR", ohlcv_dir),
            patch("market_intelligence.normalize_quality._QUALITY_REPORT", quality_report),
            patch("market_intelligence.normalize_quality.fetch_all", return_value=data),
        ):
            run_normalization()

        report = json.loads(quality_report.read_text())
        assert len(report) == 2
        symbols = {entry["symbol"] for entry in report}
        assert symbols == {"AAPL", "TSLA"}
