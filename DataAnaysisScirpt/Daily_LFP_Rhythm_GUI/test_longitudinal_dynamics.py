import json
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from continuous_lfp_rhythm.longitudinal_dynamics import (
    build_longitudinal_hourly_table,
    fit_longitudinal_effects,
    run_longitudinal_dynamics_analysis,
)


def _make_synthetic_longitudinal_h5(path):
    n_hours = 72
    n_channels = 2
    n_bands = 2
    n_states = 3
    hourly_time_s = np.arange(n_hours, dtype=np.float64) * 3600.0 + 1800.0
    zt = (hourly_time_s / 3600.0) % 24.0
    circadian = 0.2 * np.sin(2.0 * np.pi * zt / 24.0)
    rule = np.where(np.arange(n_hours) < 36, 0, 1).astype(np.int16)
    perf_state = np.where(np.arange(n_hours) < 24, 0, np.where(np.arange(n_hours) < 48, 1, 2)).astype(np.int8)
    bias_state = np.where(np.arange(n_hours) % 3 == 0, 0, np.where(np.arange(n_hours) % 3 == 1, 1, 2)).astype(np.int8)
    behavior_effect = (rule == 1).astype(np.float64) * 0.45 + (perf_state == 2).astype(np.float64) * 0.25
    feature = np.ones((n_hours, n_channels, n_bands), dtype=np.float32)
    feature[:, :, 0] += (circadian + behavior_effect)[:, np.newaxis]
    feature[:, :, 1] += circadian[:, np.newaxis]
    state_occ = np.zeros((n_hours, n_states), dtype=np.float32)
    state_occ[:, 0] = 0.5 + 0.2 * np.sin(2.0 * np.pi * zt / 24.0)
    state_occ[:, 1] = 1.0 - state_occ[:, 0]
    state_occ[:, 2] = 0.1
    state_occ = state_occ / np.sum(state_occ, axis=1, keepdims=True)
    state_stratified = np.repeat(feature[:, np.newaxis, :, :], n_states, axis=1)

    with h5py.File(path, "w") as h5f:
        meta = h5f.create_group("metadata")
        meta.attrs["timezone"] = "Asia/Shanghai"
        meta.attrs["time_origin_epoch_ms"] = 1700000000000.0
        time = h5f.create_group("time")
        time.attrs["time_origin_epoch_ms"] = 1700000000000.0
        time.create_dataset("hourly_time_s", data=hourly_time_s)
        rhythm = h5f.create_group("rhythm")
        rhythm.create_dataset("hourly_feature_table_normalized", data=feature)
        rhythm.create_dataset("hourly_feature_table", data=feature)
        rhythm.create_dataset("state_occupancy_table", data=state_occ)
        rhythm.create_dataset("state_stratified_feature_table_normalized", data=state_stratified)
        dtype = h5py.string_dtype(encoding="utf-8")
        rhythm.create_dataset("band_names", data=np.asarray(["theta", "beta"], dtype=dtype))
        rhythm.create_dataset("state_names_occupancy", data=np.asarray(["Wake", "NREM", "Working"], dtype=dtype))
        behavior = h5f.create_group("behavior")
        behavior.create_dataset("hourly_time_s", data=hourly_time_s)
        behavior.create_dataset("hourly_edges_s", data=np.arange(n_hours + 1, dtype=np.float64) * 3600.0)
        behavior.create_dataset("hourly_rule", data=rule)
        behavior.create_dataset("hourly_perf_pct", data=np.where(perf_state == 2, 80.0, np.where(perf_state == 1, 65.0, 55.0)))
        behavior.create_dataset("hourly_perf_state", data=perf_state)
        behavior.create_dataset("hourly_bias_score", data=np.where(bias_state == 0, -0.3, np.where(bias_state == 2, 0.3, 0.0)))
        behavior.create_dataset("hourly_bias_state", data=bias_state)
        behavior.create_dataset("hourly_trial_count", data=np.full(n_hours, 20, dtype=np.int32))
        behavior.create_dataset("hourly_valid_perf_count", data=np.full(n_hours, 18, dtype=np.int32))
        behavior.create_dataset("hourly_choice_count", data=np.full(n_hours, 18, dtype=np.int32))
        behavior.create_dataset("hourly_fill_source", data=np.ones(n_hours, dtype=np.int8))


def test_build_hourly_table_and_fit_effects():
    with tempfile.TemporaryDirectory() as tmpdir:
        h5_path = Path(tmpdir) / "synthetic.h5"
        _make_synthetic_longitudinal_h5(h5_path)
        table = build_longitudinal_hourly_table(
            str(h5_path),
            config={
                "rhythm": {
                    "average_channel_groups": [[0, 1]],
                    "average_group_names": ["PFC"],
                }
            },
        )
        assert len(table["rows"]) == 72
        assert "lfp_pfc_theta_norm" in table["numeric_columns"]
        assert table["rows"][40]["rule_label"] == "frequency_rule"

        effects = fit_longitudinal_effects(table, min_observations=24)
        assert effects["rows"]
        assert effects["rows"][0]["delta_r2_behavior"] > 0.01


def test_run_longitudinal_dynamics_writes_outputs_and_h5_group():
    with tempfile.TemporaryDirectory() as tmpdir:
        h5_path = Path(tmpdir) / "synthetic.h5"
        _make_synthetic_longitudinal_h5(h5_path)
        summary = run_longitudinal_dynamics_analysis(
            str(h5_path),
            output_dir=tmpdir,
            config={
                "rhythm": {
                    "average_channel_groups": [[0, 1]],
                    "average_group_names": ["PFC"],
                }
            },
        )
        assert Path(summary["hourly_csv"]).exists()
        assert Path(summary["effect_csv"]).exists()
        assert Path(summary["summary_json"]).exists()
        with open(summary["summary_json"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["n_hours"] == 72
        with h5py.File(h5_path, "r") as h5f:
            assert "longitudinal" in h5f
            assert "hourly_numeric_matrix" in h5f["longitudinal"]
            assert "effect_numeric_matrix" in h5f["longitudinal"]


if __name__ == "__main__":
    test_build_hourly_table_and_fit_effects()
    test_run_longitudinal_dynamics_writes_outputs_and_h5_group()
    print("longitudinal dynamics tests passed")
