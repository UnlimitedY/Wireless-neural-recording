import json
import sys
import tempfile
from pathlib import Path

import h5py
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
TEST_DIR = Path(__file__).resolve().parent
if str(TEST_DIR) not in sys.path:
    sys.path.insert(0, str(TEST_DIR))

from continuous_lfp_rhythm.paired_trial_context import (
    build_paired_trial_context_table,
    compute_paired_trial_context_statistics,
    run_paired_trial_context_analysis,
)
from continuous_lfp_rhythm.connectivity_features import CONNECTIVITY_VERSION, expected_connectivity_sidecar_paths, load_connectivity_sidecar
from continuous_lfp_rhythm.slice_statistics import build_slice_statistics_table


CONFIG = {
    "general": {"timezone": "Asia/Shanghai", "light_on_hour": 6.0, "light_off_hour": 18.0},
    "rhythm": {"average_channel_groups": [[0]], "average_group_names": ["PFC"]},
}


def _trial_line(trial_num, epoch_s, outcome):
    base = [
        str(epoch_s),
        str(trial_num),
        "5",
        "1",
        str(outcome),
        "100",
        "200",
        "0",
        "1",
        "3.5",
        "4.5",
        "0",
    ]
    extended = ["10", "90", "1", "0", "1", "2", "3", "0", "4", "1", "5"]
    states = ["123456", "3", "20", "1000", "6", "1300", "2", "2600"]
    return " ".join(base + extended + states)


def _make_behavior_files(root, origin_ms):
    gui = Path(root) / "BW10" / "GUIBW10"
    session = gui / "SD_card_files" / "day1"
    session.mkdir(parents=True)
    trial_lines = []
    tevent_lines = []
    for idx in range(12):
        epoch_s = int((origin_ms + (3600 + idx * 120) * 1000) / 1000)
        outcome = 1 if idx % 2 == 0 else 2
        trial_num = idx + 1
        rt = 200 if idx < 4 else 600 if idx < 8 else 1500
        trial_lines.append(_trial_line(trial_num, epoch_s, outcome))
        tevent_lines.append(f"{trial_num} 3 0 1000 8 {1000 + rt} 1 2600")
    (session / "Trial.txt").write_text("\n".join(trial_lines), encoding="utf-8")
    (session / "Tevent.txt").write_text("\n".join(tevent_lines), encoding="utf-8")
    return gui


def _make_h5(path, source_root, origin_ms):
    str_dt = h5py.string_dtype(encoding="utf-8")
    n_hours = 4
    with h5py.File(path, "w") as h5f:
        meta = h5f.create_group("metadata")
        meta.attrs["time_origin_epoch_ms"] = float(origin_ms)
        meta.attrs["timezone"] = "Asia/Shanghai"
        meta.attrs["slice_source_root"] = str(source_root)
        meta.attrs["parent_slice_id"] = "1"
        meta.attrs["parent_slice_label"] = "slice_001"
        time = h5f.create_group("time")
        time.attrs["time_origin_epoch_ms"] = float(origin_ms)
        time.create_dataset("hourly_time_s", data=np.asarray([1800, 5400, 9000, 12600], dtype=np.float32))
        rhythm = h5f.create_group("rhythm")
        feature = np.ones((n_hours, 1, 2), dtype=np.float32)
        feature[:, 0, 0] = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        feature[:, 0, 1] = np.asarray([2.0, 1.0, 2.0, 1.0], dtype=np.float32)
        rhythm.create_dataset("hourly_feature_table", data=feature)
        rhythm.create_dataset("hourly_feature_count", data=np.ones_like(feature, dtype=np.int32))
        rhythm.create_dataset("hourly_feature_table_normalized", data=feature)
        rhythm.create_dataset("hour_valid_mask", data=np.ones(n_hours, dtype=bool))
        rhythm.create_dataset("state_occupancy_table", data=np.asarray([
            [0.8, 0.1, 0.0, 0.1],
            [0.2, 0.7, 0.0, 0.1],
            [0.5, 0.2, 0.2, 0.1],
            [0.4, 0.3, 0.1, 0.2],
        ], dtype=np.float32))
        rhythm.create_dataset("band_names", data=np.asarray(["theta", "gamma"], dtype=str_dt))
        rhythm.create_dataset("state_names", data=np.asarray(["Wake", "NREM", "REM", "Working"], dtype=str_dt))
        rhythm.create_dataset("state_names_occupancy", data=np.asarray(["Wake", "NREM", "REM", "Working"], dtype=str_dt))
        behavior = h5f.create_group("behavior")
        trial_epoch = np.asarray([origin_ms + (3600 + idx * 120) * 1000 for idx in range(12)], dtype=np.int64)
        behavior.create_dataset("trial_epoch_ms", data=trial_epoch)
        behavior.create_dataset("trial_smoothed_perf_pct", data=np.linspace(50, 80, 12).astype(np.float32))
        behavior.create_dataset("trial_smoothed_bias_score", data=np.linspace(-0.2, 0.2, 12).astype(np.float32))
        behavior.create_dataset("hourly_rule", data=np.ones(n_hours, dtype=np.int16))
        behavior.create_dataset("hourly_perf_pct", data=np.asarray([55, 65, 75, 75], dtype=np.float32))
        behavior.create_dataset("hourly_perf_state", data=np.asarray([0, 1, 2, 2], dtype=np.int8))
        behavior.create_dataset("hourly_bias_score", data=np.zeros(n_hours, dtype=np.float32))
        behavior.create_dataset("hourly_bias_state", data=np.ones(n_hours, dtype=np.int8))
        behavior.create_dataset("hourly_trial_count", data=np.asarray([0, 12, 0, 0], dtype=np.int32))
        behavior.create_dataset("hourly_valid_perf_count", data=np.asarray([0, 12, 0, 0], dtype=np.int32))
        behavior.create_dataset("hourly_choice_count", data=np.asarray([0, 12, 0, 0], dtype=np.int32))
        behavior.create_dataset("hourly_fill_source", data=np.ones(n_hours, dtype=np.int8))


def _write_connectivity_sidecar(h5_path, output_dir, origin_ms):
    paths = expected_connectivity_sidecar_paths(str(h5_path), str(output_dir))
    centers = origin_ms + np.asarray([1800, 5400, 9000, 12600], dtype=np.float64) * 1000.0
    plv = np.full((4, 2, 1, 1), np.nan, dtype=np.float32)
    plv[:, 0, 0, 0] = np.asarray([1.0, 0.9, 0.8, 0.7], dtype=np.float32)
    plv[:, 1, 0, 0] = np.asarray([1.0, 0.85, 0.75, 0.65], dtype=np.float32)
    pac = np.asarray([0.01, 0.02, 0.03, 0.04], dtype=np.float32)[:, None, None, None]
    np.savez_compressed(
        paths["npz"],
        hour_start_epoch_ms=centers - 1800000.0,
        hour_center_epoch_ms=centers,
        hour_end_epoch_ms=centers + 1800000.0,
        valid_fraction=np.ones(4, dtype=np.float32),
        plv=plv,
        pac=pac,
    )
    meta = {
        "version": CONNECTIVITY_VERSION,
        "source_h5": str(h5_path),
        "band_names": ["theta", "gamma"],
        "pac_band_pairs": [
            {
                "phase_band": "theta",
                "amplitude_band": "gamma",
                "phase_band_index": 0,
                "amplitude_band_index": 1,
            }
        ],
    }
    Path(paths["json"]).write_text(json.dumps(meta), encoding="utf-8")


def test_paired_context_builds_trial_rows_and_outputs_sidecars():
    with tempfile.TemporaryDirectory() as tmpdir:
        origin_ms = 1700000000000
        source_root = _make_behavior_files(tmpdir, origin_ms)
        h5_path = Path(tmpdir) / "BW10_slice_001_20260101-000000_slice_lfp_rhythm.h5"
        _make_h5(h5_path, source_root, origin_ms)

        table = build_paired_trial_context_table(
            [str(h5_path)],
            tmpdir,
            config=CONFIG,
            reference_scope="daily",
            context_window_s=3600.0,
            connectivity={str(h5_path): None},
        )
        assert table["trial_rows"]
        assert "pre_occ_wake" in table["trial_rows"][0]
        assert "post_lfp_pfc_theta_daily_norm" in table["trial_rows"][0]

        stats = compute_paired_trial_context_statistics(table, n_permutations=9, random_seed=2)
        assert stats["pre_context_behavior_tests"]
        assert stats["pre_post_delta_tests"]

        summary = run_paired_trial_context_analysis(
            [str(h5_path)],
            tmpdir,
            config=CONFIG,
            reference_scope="daily",
            context_window_s=3600.0,
            n_permutations=9,
            make_figures=False,
        )
        assert Path(summary["summary_json"]).exists()
        assert Path(summary["rt_features_csv"]).exists()
        assert Path(summary["paired_context_csv"]).exists()
        with open(summary["summary_json"], "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        assert payload["n_trials"] > 0
        assert payload["warnings"]


def test_slice_statistics_loads_rt_and_connectivity_sidecars():
    with tempfile.TemporaryDirectory() as tmpdir:
        origin_ms = 1700000000000
        source_root = _make_behavior_files(tmpdir, origin_ms)
        h5_path = Path(tmpdir) / "BW10_slice_001_20260101-000000_slice_lfp_rhythm.h5"
        _make_h5(h5_path, source_root, origin_ms)
        _write_connectivity_sidecar(h5_path, tmpdir, origin_ms)

        table = build_slice_statistics_table([str(h5_path)], config=CONFIG, reference_scope="daily")
        assert "rt_median_ms" in table["numeric_columns"]
        assert any(np.isfinite(row["rt_median_ms"]) for row in table["rows"])
        assert "plv_theta_mean" in table["target_columns"]
        assert "pac_theta_to_gamma_mean" in table["target_columns"]
        assert table["rt_metadata"]["available"]
        assert table["connectivity_metadata"]["available"]


def test_paired_context_uses_lightweight_connectivity_targets_by_default():
    with tempfile.TemporaryDirectory() as tmpdir:
        origin_ms = 1700000000000
        source_root = _make_behavior_files(tmpdir, origin_ms)
        h5_path = Path(tmpdir) / "BW10_slice_001_20260101-000000_slice_lfp_rhythm.h5"
        _make_h5(h5_path, source_root, origin_ms)
        _write_connectivity_sidecar(h5_path, tmpdir, origin_ms)
        conn = load_connectivity_sidecar(str(h5_path), tmpdir)

        table = build_paired_trial_context_table(
            [str(h5_path)],
            tmpdir,
            config=CONFIG,
            reference_scope="daily",
            context_window_s=3600.0,
            connectivity={str(h5_path): conn},
        )
        row = table["trial_rows"][0]
        assert "pre_plv_theta_mean" in row
        assert "pre_pac_theta_to_gamma_mean" in row
        assert "pre_plv_theta_ch00_ch00" not in row


if __name__ == "__main__":
    test_paired_context_builds_trial_rows_and_outputs_sidecars()
    test_slice_statistics_loads_rt_and_connectivity_sidecars()
    test_paired_context_uses_lightweight_connectivity_targets_by_default()
    print("paired trial context tests passed")
