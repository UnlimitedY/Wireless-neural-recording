import json
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from continuous_lfp_rhythm.direct_behavior_statistics import (
    compute_direct_behavior_state_statistics,
    run_direct_behavior_state_statistics,
)
from continuous_lfp_rhythm.slice_statistics import build_slice_statistics_table
from test_slice_statistics import CONFIG, _make_h5_set


def test_direct_behavior_group_stats_detect_known_learning_difference():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=5, effect_size=0.65)
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        stats = compute_direct_behavior_state_statistics(table, n_permutations=99, random_seed=4)
        rows = [
            row for row in stats["lfp_group_tests"]
            if row["target"] == "lfp_pfc_theta_daily_norm"
            and row["behavior_variable"] == "perf_state_code"
        ]
        assert rows
        assert rows[0]["effect_size"] > 0.4
        assert rows[0]["permutation_p"] <= 0.05
        assert rows[0]["fdr_q"] <= 0.25


def test_direct_behavior_outputs_are_written():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=4, effect_size=0.45)
        summary = run_direct_behavior_state_statistics(
            paths,
            tmpdir,
            config=CONFIG,
            reference_scope="daily",
            n_permutations=49,
            random_seed=6,
            make_figures=False,
        )
        assert Path(summary["summary_json"]).exists()
        assert Path(summary["lfp_group_tests_csv"]).exists()
        assert Path(summary["lfp_pairwise_tests_csv"]).exists()
        assert Path(summary["state_occupancy_tests_csv"]).exists()
        assert Path(summary["continuous_associations_csv"]).exists()
        with open(summary["summary_json"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["top_lfp_group_differences"]
        assert payload["n_h5_files"] == 4
        assert payload["n_hours"] == 96


def test_direct_behavior_null_keeps_fdr_conservative():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=5, effect_size=0.0)
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        stats = compute_direct_behavior_state_statistics(table, n_permutations=49, random_seed=8)
        significant = [
            row for row in stats["lfp_group_tests"]
            if np.isfinite(row.get("fdr_q", np.nan)) and row["fdr_q"] < 0.05
        ]
        assert len(significant) == 0


if __name__ == "__main__":
    test_direct_behavior_group_stats_detect_known_learning_difference()
    test_direct_behavior_outputs_are_written()
    test_direct_behavior_null_keeps_fdr_conservative()
    print("direct behavior statistics tests passed")
