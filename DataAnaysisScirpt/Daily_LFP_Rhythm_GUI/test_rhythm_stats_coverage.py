import os
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from continuous_lfp_rhythm.rhythm_stats import summarize_rhythm


def _make_h5_with_empty_middle_hour(path):
    n_hours = 3
    lfp_fs = 1.0
    hour_s = 3600.0
    total_s = n_hours * hour_s
    n_samples = int(total_s * lfp_fs)
    feature_window_s = 60.0
    state_window_s = 10.0

    feature_time_s = np.arange(feature_window_s / 2.0, total_s, feature_window_s)
    state_time_s = np.arange(state_window_s / 2.0, total_s, state_window_s)
    band_power = np.ones((feature_time_s.size, 2, 1), dtype=np.float32)
    band_power[(feature_time_s >= hour_s) & (feature_time_s < 2.0 * hour_s)] = 100.0
    band_power[feature_time_s >= 2.0 * hour_s] = 3.0

    coverage = np.ones((n_samples,), dtype=bool)
    coverage[int(hour_s):int(2.0 * hour_s)] = False
    missing = ~coverage

    with h5py.File(path, "w") as h5f:
        meta = h5f.create_group("metadata")
        meta.attrs["lfp_sample_rate_hz"] = lfp_fs
        time = h5f.create_group("time")
        time.attrs["lfp_time_s_extent"] = [0.0, total_s]
        time.create_dataset("feature_time_s", data=feature_time_s)
        time.create_dataset("state_time_s", data=state_time_s)
        lfp = h5f.create_group("lfp")
        lfp.create_dataset("file_coverage_mask", data=coverage)
        lfp.create_dataset("missing_mask", data=missing)
        features = h5f.create_group("features")
        features.create_dataset("band_power", data=band_power)
        features.create_dataset("valid_fraction", data=np.ones(feature_time_s.shape, dtype=np.float32))
        dtype = h5py.string_dtype(encoding="utf-8")
        features.create_dataset("feature_band_names", data=np.asarray(["theta"], dtype=dtype))
        states = h5f.create_group("states")
        states.create_dataset("state_label", data=np.zeros(state_time_s.shape, dtype=np.int16))
        states.create_dataset("state_valid_mask", data=np.ones(state_time_s.shape, dtype=bool))
        h5f.create_group("rhythm")


def test_summarize_rhythm_excludes_empty_coverage_hours():
    with tempfile.TemporaryDirectory() as tmpdir:
        h5_path = Path(tmpdir) / "coverage_gap.h5"
        _make_h5_with_empty_middle_hour(h5_path)
        summarize_rhythm(
            h5_path,
            {
                "features": {"feature_window_s": 60.0},
                "states": {"scoring_window_s": 10.0},
                "rhythm": {"hour_bin_s": 3600.0},
                "missing_data": {"valid_fraction_threshold": 0.8},
            },
        )

        with h5py.File(h5_path, "r") as h5f:
            hour_valid = h5f["rhythm/hour_valid_mask"][:]
            hourly_count = h5f["rhythm/hourly_feature_count"][:]
            hourly_norm = h5f["rhythm/hourly_feature_table_normalized"][:]
            reference = h5f["rhythm/hourly_band_daily_reference"][:]
            state_occ = h5f["rhythm/state_occupancy_table"][:]
            state_count = h5f["rhythm/state_epoch_count"][:]

        assert hour_valid.tolist() == [True, False, True]
        assert np.all(hourly_count[1] == 0)
        assert np.all(np.isnan(hourly_norm[1]))
        assert np.allclose(reference, 2.0)
        assert np.all(np.isnan(state_occ[1]))
        assert np.all(state_count[1] == 0)


if __name__ == "__main__":
    test_summarize_rhythm_excludes_empty_coverage_hours()
    print("rhythm coverage tests passed")
