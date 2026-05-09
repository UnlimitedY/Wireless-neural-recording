import json
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from continuous_lfp_rhythm.slice_statistics import (
    build_slice_statistics_table,
    fit_slice_statistical_models,
    run_slice_statistical_analysis,
)


CONFIG = {
    "rhythm": {
        "average_channel_groups": [[0, 1]],
        "average_group_names": ["PFC"],
    },
    "general": {
        "timezone": "Asia/Shanghai",
        "light_on_hour": 6.0,
        "light_off_hour": 18.0,
    },
}


def _make_day_h5(path, day_idx, learned_pattern, effect_size=0.0, seed=0):
    rng = np.random.default_rng(seed + day_idx)
    n_hours = 24
    n_channels = 2
    n_bands = 1
    n_states = 3
    origin_ms = 1700000000000.0 + day_idx * 86400000.0
    hourly_time_s = np.arange(n_hours, dtype=np.float64) * 3600.0 + 1800.0
    zt = (hourly_time_s / 3600.0 + 8.0) % 24.0
    circadian = 0.08 * np.sin(2.0 * np.pi * zt / 24.0)
    learned = np.asarray(learned_pattern, dtype=bool)
    if learned.shape[0] != n_hours:
        raise ValueError("learned_pattern must have 24 values")

    feature = np.ones((n_hours, n_channels, n_bands), dtype=np.float32)
    feature[:, :, 0] += circadian[:, np.newaxis]
    feature[:, :, 0] += effect_size * learned[:, np.newaxis]
    feature += rng.normal(0.0, 0.02, size=feature.shape).astype(np.float32)
    count = np.full(feature.shape, 60.0, dtype=np.float32)

    state_occ = np.zeros((n_hours, n_states), dtype=np.float32)
    state_occ[:, 0] = 0.45 + 0.1 * np.sin(2.0 * np.pi * zt / 24.0)
    state_occ[:, 1] = 0.45 - 0.1 * np.sin(2.0 * np.pi * zt / 24.0)
    state_occ[:, 2] = 0.10
    state_occ = state_occ / np.sum(state_occ, axis=1, keepdims=True)

    state_feature = np.ones((n_hours, n_states, n_channels, n_bands), dtype=np.float32)
    state_feature[:, :, :, 0] += circadian[:, np.newaxis, np.newaxis]
    state_feature[:, 0, :, 0] += effect_size * learned[:, np.newaxis]
    state_feature[:, 1, :, 0] += 0.08 * effect_size * learned[:, np.newaxis]
    state_feature += rng.normal(0.0, 0.025, size=state_feature.shape).astype(np.float32)
    state_count = np.repeat(state_occ[:, :, np.newaxis, np.newaxis], n_channels, axis=2)
    state_count = np.repeat(state_count, n_bands, axis=3).astype(np.float32) * 60.0

    perf_state = np.where(learned, 2, 0).astype(np.int8)
    rule = np.where(day_idx % 3 == 0, 0, np.where(day_idx % 3 == 1, 1, 2)).astype(np.int16)
    rule = np.full(n_hours, rule, dtype=np.int16)
    bias_state = np.where(np.arange(n_hours) % 3 == 0, 0, np.where(np.arange(n_hours) % 3 == 1, 1, 2)).astype(np.int8)

    with h5py.File(path, "w") as h5f:
        meta = h5f.create_group("metadata")
        meta.attrs["timezone"] = "Asia/Shanghai"
        meta.attrs["time_origin_epoch_ms"] = origin_ms
        meta.attrs["slice_source_root"] = "/data/BW08"
        meta.attrs["slice_json_path"] = "/data/BW08/slices.json"
        meta.attrs["parent_slice_id"] = 1
        meta.attrs["parent_slice_label"] = "slice_001"
        time = h5f.create_group("time")
        time.attrs["time_origin_epoch_ms"] = origin_ms
        time.create_dataset("hourly_time_s", data=hourly_time_s)
        rhythm = h5f.create_group("rhythm")
        rhythm.create_dataset("hourly_feature_table", data=feature)
        rhythm.create_dataset("hourly_feature_count", data=count)
        rhythm.create_dataset("state_occupancy_table", data=state_occ)
        rhythm.create_dataset("state_epoch_count", data=np.full(state_occ.shape, 360.0, dtype=np.float32))
        rhythm.create_dataset("state_stratified_feature_table", data=state_feature)
        rhythm.create_dataset("state_stratified_count", data=state_count)
        rhythm.create_dataset("hour_valid_mask", data=np.ones(n_hours, dtype=bool))
        dtype = h5py.string_dtype(encoding="utf-8")
        rhythm.create_dataset("band_names", data=np.asarray(["theta"], dtype=dtype))
        rhythm.create_dataset("state_names", data=np.asarray(["Wake", "NREM", "Working"], dtype=dtype))
        rhythm.create_dataset("state_names_occupancy", data=np.asarray(["Wake", "NREM", "Working"], dtype=dtype))
        behavior = h5f.create_group("behavior")
        behavior.create_dataset("hourly_time_s", data=hourly_time_s)
        behavior.create_dataset("hourly_rule", data=rule)
        behavior.create_dataset("hourly_perf_pct", data=np.where(learned, 82.0, 52.0))
        behavior.create_dataset("hourly_perf_state", data=perf_state)
        behavior.create_dataset("hourly_bias_score", data=np.where(bias_state == 0, -0.25, np.where(bias_state == 2, 0.25, 0.0)))
        behavior.create_dataset("hourly_bias_state", data=bias_state)
        behavior.create_dataset("hourly_trial_count", data=np.full(n_hours, 20, dtype=np.int32))
        behavior.create_dataset("hourly_valid_perf_count", data=np.full(n_hours, 18, dtype=np.int32))
        behavior.create_dataset("hourly_choice_count", data=np.full(n_hours, 18, dtype=np.int32))
        behavior.create_dataset("hourly_fill_source", data=np.ones(n_hours, dtype=np.int8))


def _make_h5_set(tmpdir, n_days=5, effect_size=0.0):
    paths = []
    rng = np.random.default_rng(123)
    for day in range(n_days):
        learned = rng.random(24) > 0.45
        path = Path(tmpdir) / f"synthetic_day_{day}.h5"
        _make_day_h5(path, day, learned, effect_size=effect_size, seed=500)
        paths.append(str(path))
    return paths


def test_build_slice_statistics_table_continuous_time():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=3, effect_size=0.3)
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        assert len(table["rows"]) == 72
        assert table["slice_id"].startswith("BW08_slice_1")
        assert "lfp_pfc_theta_daily_norm" in table["target_columns"]
        assert "state_wake_pfc_theta_daily_norm" in table["target_columns"]
        epochs = np.asarray([row["epoch_ms"] for row in table["rows"]], dtype=np.float64)
        assert np.all(np.diff(epochs) > 0)
        assert table["rows"][0]["day_index"] == 0
        assert table["rows"][-1]["day_index"] == 2


def test_known_learning_effect_is_detected():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=5, effect_size=0.55)
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        models = fit_slice_statistical_models(table, n_permutations=99, random_seed=1)
        rows = [
            row for row in models["term_tests"]
            if row["target"] == "lfp_pfc_theta_daily_norm" and row["test_family"] == "learning_stage_block"
        ]
        assert rows
        assert rows[0]["delta_r2"] > 0.2
        assert rows[0]["permutation_p"] <= 0.05
        assert rows[0]["fdr_q"] <= 0.25


def test_null_model_does_not_create_many_significant_hits():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=5, effect_size=0.0)
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        models = fit_slice_statistical_models(table, n_permutations=49, random_seed=3)
        significant = [
            row for row in models["term_tests"]
            if np.isfinite(row.get("fdr_q", np.nan)) and row["fdr_q"] < 0.05
        ]
        assert len(significant) == 0


def test_run_slice_statistics_writes_outputs_and_fdr_is_monotonic():
    with tempfile.TemporaryDirectory() as tmpdir:
        paths = _make_h5_set(tmpdir, n_days=4, effect_size=0.45)
        summary = run_slice_statistical_analysis(
            paths,
            tmpdir,
            config=CONFIG,
            reference_scope="daily",
            n_permutations=49,
            random_seed=2,
            make_figures=False,
        )
        assert Path(summary["summary_json"]).exists()
        assert Path(summary["hourly_csv"]).exists()
        assert Path(summary["effect_csv"]).exists()
        with open(summary["summary_json"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["n_h5_files"] == 4
        assert Path(payload["lfp_behavior_modulation_csv"]).exists()
        assert Path(payload["state_specific_lfp_summary_csv"]).exists()
        assert Path(payload["state_occupancy_tests_csv"]).exists()
        assert Path(payload["interaction_tests_csv"]).exists()
        assert payload["top_lfp_behavior_modulations"]
        assert payload["top_state_specific_lfp"]
        assert payload["top_state_occupancy_effects"]
        table = build_slice_statistics_table(paths, config=CONFIG, reference_scope="daily")
        models = fit_slice_statistical_models(table, n_permutations=49, random_seed=2)
        assert models["state_specific_lfp_summary"]
        assert any(row["target"] == "occ_wake" for row in models["state_occupancy_tests"])
        finite_rows = [
            row for row in models["term_tests"]
            if np.isfinite(row.get("permutation_p", np.nan)) and np.isfinite(row.get("fdr_q", np.nan))
        ]
        finite_rows = sorted(finite_rows, key=lambda row: row["permutation_p"])
        assert all(row["fdr_q"] >= row["permutation_p"] for row in finite_rows)
        assert all(
            finite_rows[idx]["fdr_q"] <= finite_rows[idx + 1]["fdr_q"] + 1e-12
            for idx in range(len(finite_rows) - 1)
        )


if __name__ == "__main__":
    test_build_slice_statistics_table_continuous_time()
    test_known_learning_effect_is_detected()
    test_null_model_does_not_create_many_significant_hits()
    test_run_slice_statistics_writes_outputs_and_fdr_is_monotonic()
    print("slice statistics tests passed")
