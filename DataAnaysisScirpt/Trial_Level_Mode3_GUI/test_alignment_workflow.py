import os
import sys
import tempfile
import types

import numpy as np

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(THIS_DIR, os.pardir))
for path in (PROJECT_ROOT, THIS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from Common_Analysis.channel_map import MODE3_SHALLOW_TO_DEEP_CHANNELS, reorder_lfp_to_unified_probe_order
from Common_Analysis.trial_parsing import TrialData
from data_processing import DataProcessor, alignment_roi_sample_bounds, fit_alignment_anchors, save_trials_to_h5


def _base_trial_fields():
    return [
        "12:00:00",
        "1",
        "5",
        "2",
        "1",
        "100",
        "200",
        "0",
        "1",
        "3.5",
        "4.5",
        "0",
    ]


def _extended_metadata():
    return ["10", "90", "1", "0", "1", "2", "3", "0", "4", "1", "5"]


def test_new_trial_format():
    line = " ".join(
        _base_trial_fields()
        + _extended_metadata()
        + ["123456", "3", "20", "1000", "6", "1500", "2", "2500"]
    )
    trial = TrialData(line)
    assert trial.trial_num == 1
    assert trial.trial_start_timestamp_ms == 123456
    assert trial.n_visited == 3
    assert trial.states_history == [(20, 1000), (6, 1500), (2, 2500)]
    assert trial.start_ts == 1000
    assert trial.end_ts == 2500


def test_old_trial_format():
    line = " ".join(
        _base_trial_fields()
        + _extended_metadata()
        + ["3", "20", "1000", "6", "1500", "2", "2500"]
    )
    trial = TrialData(line)
    assert trial.trial_start_timestamp_ms is None
    assert trial.n_visited == 3
    assert trial.states_history == [(20, 1000), (6, 1500), (2, 2500)]


def test_alignment_fit_single_anchor():
    fit = fit_alignment_anchors(
        [{"trial_id": 1, "trial_start_timestamp_ms": 1000, "alignment_sample": 0, "pulse_sample": 13000}],
        fs_raw=12500,
    )
    assert fit["method"] == "single_anchor_fixed_slope"
    assert fit["slope_samples_per_ms"] == 12.5
    assert fit["offset_samples"] == 500.0
    assert fit["max_abs_residual_ms"] == 0.0


def test_alignment_fit_linear():
    fit = fit_alignment_anchors(
        [
            {"trial_id": 1, "trial_start_timestamp_ms": 1000, "alignment_sample": 0, "pulse_sample": 13000},
            {"trial_id": 2, "trial_start_timestamp_ms": 2000, "alignment_sample": 0, "pulse_sample": 25500},
        ],
        fs_raw=12500,
    )
    assert fit["method"] == "linear_fit"
    assert np.isclose(fit["slope_samples_per_ms"], 12.5)
    assert np.isclose(fit["offset_samples"], 500.0)
    assert fit["max_abs_residual_ms"] < 1e-9


def test_mode3_channel_reorder_matches_daily_order():
    source = np.repeat(np.arange(16, dtype=float)[:, None], 2, axis=1)
    reordered = reorder_lfp_to_unified_probe_order(source, "mode3_lfp")
    assert reordered.shape == (16, 2)
    assert reordered[:, 0].astype(int).tolist() == MODE3_SHALLOW_TO_DEEP_CHANNELS


def test_alignment_roi_window():
    roi_start, roi_end = alignment_roi_sample_bounds(
        edge_sample=5000,
        n_samples=10000,
        fs_raw=10000,
        roi_window_ms=(-300.0, -100.0),
    )
    assert roi_start == 2000
    assert roi_end == 4000


def test_h5_smoke_nan_and_metadata_roundtrip():
    trial_key = "2026-04-24-00-00-00_Trial_1"
    tdata = {
        "raw": (np.array([0.0, 1.0]), np.array([np.nan, 0.2]), np.array([0.0, 1.0])),
        "lfp": (np.array([0.0, 1.0]), np.array([[np.nan, 1.0]] * 16)),
        "esa": (np.array([0.0, 1.0]), np.array([[0.1, np.nan]] * 16)),
        "raster": (np.array([0.0, 1.0]), np.zeros((16, 2))),
        "sen": (np.array([0.0, 1.0]), np.array([[np.nan, 0.1], [0.2, 0.3], [0.4, 0.5]]), ["X", "Y", "Z"]),
        "trial_start_ts": 1000,
        "trial_start_timestamp_ms": 123456.0,
        "alignment_fit": {"method": "single_anchor_fixed_slope", "slope_samples_per_ms": 12.5},
        "states": [(20, 1000), (2, 2000)],
        "tevent": types.SimpleNamespace(events=[(8, 1200)]),
        "lfp_phase_compensation": {"applied": False},
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = os.path.join(tmpdir, "smoke.h5")
        save_trials_to_h5({trial_key: tdata}, out_path)
        dp = DataProcessor()
        keys = dp.load_from_h5(out_path)
        loaded = dp.get_extracted_data(trial_key)
    assert keys == [trial_key]
    assert np.isnan(loaded["raw"][1][0])
    assert loaded["trial_start_timestamp_ms"] == 123456.0
    assert loaded["alignment_fit"]["method"] == "single_anchor_fixed_slope"


if __name__ == "__main__":
    test_new_trial_format()
    test_old_trial_format()
    test_alignment_fit_single_anchor()
    test_alignment_fit_linear()
    test_mode3_channel_reorder_matches_daily_order()
    test_alignment_roi_window()
    test_h5_smoke_nan_and_metadata_roundtrip()
    print("alignment workflow tests passed")
