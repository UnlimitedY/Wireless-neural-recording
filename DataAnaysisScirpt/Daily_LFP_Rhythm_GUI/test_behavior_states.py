import tempfile
import sys
from pathlib import Path

import h5py
import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common_Analysis.behavior_analysis import (
    classify_bias_state,
    classify_perf_state,
    compute_hourly_behavior_states,
    infer_choice_from_trial,
    parse_behavior_trials,
    slice_behavior_result_for_window,
)
from continuous_lfp_rhythm.h5_schema import build_slice_h5


def _trial_line(epoch_s, trial_num, protocol, trial_type, outcome, rule):
    base = [
        str(epoch_s),
        str(trial_num),
        str(protocol),
        str(trial_type),
        str(outcome),
        "100",
        "200",
        "0",
        str(rule),
        "3.5",
        "4.5",
        "0",
    ]
    metadata = ["10", "87", "1", "0", "1", "2", "3", "0", "4", "1", "255"]
    tail = ["12345", "0"]
    return " ".join(base + metadata + tail)


def test_parse_behavior_trials_new_format_and_bad_line_warning():
    with tempfile.TemporaryDirectory() as tmpdir:
        sd = Path(tmpdir)
        trial_file = sd / "Trial.txt"
        trial_file.write_text(
            "\n".join(
                [
                    _trial_line(1713000000, 7, 5, 1, 1, 1),
                    "bad line",
                ]
            ),
            encoding="utf-8",
        )
        trials, warnings = parse_behavior_trials(sd)
        assert len(trials) == 1
        assert warnings
        assert trials[0]["rule"] == 1
        assert trials[0]["protocol_index"] == 5
        assert trials[0]["performance_pct"] == 87.0
        assert trials[0]["trial_outcome"] == 1
        assert trials[0]["trial_type"] == 1


def test_inferred_choice_from_trial_type_and_outcome():
    assert infer_choice_from_trial(1, 1) == 1
    assert infer_choice_from_trial(1, 2) == 2
    assert infer_choice_from_trial(2, 1) == 2
    assert infer_choice_from_trial(2, 2) == 1
    assert infer_choice_from_trial(1, 0) == -1
    assert infer_choice_from_trial(2, 4) == -1


def test_hourly_behavior_states_thresholds_and_forward_fill():
    assert classify_perf_state(59.9) == 0
    assert classify_perf_state(60.0) == 1
    assert classify_perf_state(70.0) == 2
    assert classify_bias_state(-0.11) == 0
    assert classify_bias_state(-0.1) == 1
    assert classify_bias_state(0.1) == 1
    assert classify_bias_state(0.11) == 2

    start_ms = 0
    end_ms = 3 * 3600 * 1000
    trials = []
    for idx in range(12):
        outcome = 1 if idx < 8 else 2
        trial_type = 1 if idx % 2 == 0 else 2
        trials.append(
            {
                "time_epoch_ms": 3600 * 1000 + idx * 1000,
                "trial_num": idx + 1,
                "protocol_index": 5,
                "rule": 0,
                "trial_type": trial_type,
                "trial_outcome": outcome,
            }
        )

    result = compute_hourly_behavior_states(trials, start_ms, end_ms)
    assert result["hourly_time_s"].shape[0] == 3
    assert result["hourly_fill_source"][0] == 0
    assert result["hourly_perf_state"][0] == -1
    assert result["hourly_fill_source"][1] == 1
    assert result["hourly_perf_state"][1] == 1
    assert result["hourly_bias_state"][1] == 1
    assert result["hourly_fill_source"][2] == 3
    assert result["hourly_perf_state"][2] == result["hourly_perf_state"][1]


def test_build_slice_h5_writes_behavior_group():
    with tempfile.TemporaryDirectory() as tmpdir:
        start_ms = 1700000000000
        payload = {
            "version": 1,
            "bw_id": "BW10",
            "source_root": str(Path(tmpdir) / "BW10"),
            "timezone": "Asia/Shanghai",
        }
        slice_item = {
            "id": 1,
            "label": "behavior_check",
            "start_epoch_ms": start_ms,
            "end_epoch_ms": start_ms + 2000,
            "duration_s": 2.0,
        }
        trials = [
            {
                "time_epoch_ms": start_ms + 100,
                "trial_num": 1,
                "protocol_index": 5,
                "rule": 0,
                "trial_type": 1,
                "trial_outcome": 1,
                "source_file": str(Path(tmpdir) / "Trial.txt"),
            }
        ]
        h5_path = build_slice_h5(
            payload,
            slice_item,
            [],
            tmpdir,
            behavior_trials=trials,
            behavior_source_metadata={"sd_card_dir": str(Path(tmpdir) / "SD_card_files")},
            config={
                "io": {
                    "lfp_sample_rate_hz": 1000,
                    "imu_target_rate_hz": 100,
                    "compression": None,
                }
            },
        )

        with h5py.File(h5_path, "r") as f:
            assert "behavior" in f
            assert f["behavior/trial_time_s"].shape == (1,)
            assert np.isclose(f["behavior/trial_time_s"][0], 0.1)
            assert f["behavior/hourly_time_s"].shape == (1,)
            assert f["behavior/hourly_fill_source"][0] == 2
            assert "rule_labels_json" in f["behavior"].attrs


def test_behavior_window_slice_keeps_parent_smoothed_trial_values():
    start_ms = 0
    parent_end_ms = 3 * 3600 * 1000
    trials = []
    for idx in range(30):
        trials.append(
            {
                "time_epoch_ms": 30 * 60 * 1000 + idx * 5 * 60 * 1000,
                "trial_num": idx + 1,
                "protocol_index": 5,
                "rule": 1,
                "trial_type": 1 if idx % 2 == 0 else 2,
                "trial_outcome": 1 if idx < 20 else 2,
            }
        )
    parent = compute_hourly_behavior_states(trials, start_ms, parent_end_ms)
    day = slice_behavior_result_for_window(parent, start_ms, 3600 * 1000, 2 * 3600 * 1000)
    parent_trial_mask = (parent["trial_epoch_ms"] >= 3600 * 1000) & (parent["trial_epoch_ms"] < 2 * 3600 * 1000)

    assert day["trial_epoch_ms"].shape[0] == int(np.sum(parent_trial_mask))
    assert np.allclose(day["trial_smoothed_perf_pct"], parent["trial_smoothed_perf_pct"][parent_trial_mask])
    assert np.all(day["trial_time_s"] >= 0.0)
    assert np.all(day["trial_time_s"] < 3600.0)
    assert np.all(day["hourly_time_s"] >= 0.0)
    assert np.all(day["hourly_time_s"] < 3600.0)


if __name__ == "__main__":
    test_parse_behavior_trials_new_format_and_bad_line_warning()
    test_inferred_choice_from_trial_type_and_outcome()
    test_hourly_behavior_states_thresholds_and_forward_fill()
    test_build_slice_h5_writes_behavior_group()
    test_behavior_window_slice_keeps_parent_smoothed_trial_values()
    print("behavior state tests passed")
