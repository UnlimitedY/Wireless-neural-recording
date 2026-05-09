import datetime as _dt
import json
import os
from pathlib import Path

import h5py
import numpy as np

from .trial_parsing import TrialData


TIMEZONE = _dt.timezone(_dt.timedelta(hours=8), name="Asia/Shanghai")

RULE_LABELS = {
    -1: "unknown",
    0: "location_rule",
    1: "frequency_rule",
    2: "reversal_frequency_rule",
}
PERF_STATE_LABELS = {
    -1: "unknown",
    0: "unlearned",
    1: "intermediate",
    2: "learned",
}
BIAS_STATE_LABELS = {
    -1: "unknown",
    0: "left_bias",
    1: "neutral",
    2: "right_bias",
}
CHOICE_LABELS = {
    -1: "no_choice",
    1: "left",
    2: "right",
}
FILL_SOURCE_LABELS = {
    0: "unknown",
    1: "local_hour",
    2: "smoothed_low_trial_count",
    3: "forward_fill",
}

DEFAULT_BEHAVIOR_CONFIG = {
    "hour_bin_s": 3600.0,
    "min_trials_per_hour": 10,
    "gaussian_window_trials": 50,
    "gaussian_sigma_trials": 20.0,
    "bias_neutral_threshold": 0.1,
}


def iter_trial_files(sd_card_dir):
    return sorted(Path(sd_card_dir).rglob("Trial.txt"))


def parse_behavior_trials(sd_card_dir):
    trials = []
    warnings = []
    global_idx = 0
    for trial_file in iter_trial_files(sd_card_dir):
        with open(trial_file, "r", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, start=1):
                if "Trial:" in line or "Tevent:" in line or not line.strip():
                    continue
                try:
                    trial = TrialData(line)
                    epoch_ms = int(round(float(trial.time) * 1000.0))
                except Exception as exc:
                    warnings.append(f"Failed to parse {trial_file}:{line_no}: {exc}")
                    continue
                global_idx += 1
                trials.append(_trial_to_record(trial, trial_file, line_no, global_idx, epoch_ms))
    trials.sort(key=lambda item: (item["time_epoch_ms"], item["trial_num"], item["source_file"]))
    return trials, warnings


def filter_behavior_trials_for_slice(trials, slice_start_ms, slice_end_ms):
    start_ms = float(slice_start_ms)
    end_ms = float(slice_end_ms)
    return [
        trial for trial in sorted(trials, key=lambda item: item.get("time_epoch_ms", 0))
        if start_ms <= float(trial.get("time_epoch_ms", -np.inf)) < end_ms
    ]


def resolve_sd_card_dir_from_source_root(source_root):
    source_root = os.path.realpath(os.path.abspath(os.path.expanduser(str(source_root))))
    candidates = []
    if os.path.basename(source_root).upper().startswith("GUIBW"):
        candidates.append(source_root)
        candidates.append(os.path.dirname(source_root))
    else:
        candidates.append(source_root)

    for root in candidates:
        sd_card = _child_dir_case_insensitive(root, "SD_card_files")
        if sd_card:
            return sd_card
        gui_root = _find_single_guibw_child(root)
        if gui_root:
            sd_card = _child_dir_case_insensitive(gui_root, "SD_card_files")
            if sd_card:
                return sd_card
    raise ValueError(f"Could not find SD_card_files for source_root: {source_root}")


def infer_choice_from_trial(trial_type, trial_outcome):
    trial_type = _safe_int(trial_type, default=-1)
    trial_outcome = _safe_int(trial_outcome, default=-1)
    if trial_type not in (1, 2):
        return -1
    if trial_outcome == 1:
        return trial_type
    if trial_outcome == 2:
        return 2 if trial_type == 1 else 1
    return -1


def trial_type_label(trial_type):
    return {1: "left", 2: "right", 3: "middle"}.get(_safe_int(trial_type, default=-1), "unknown")


def outcome_label(outcome):
    return {
        0: "no_response",
        1: "correct",
        2: "error",
        3: "other",
        4: "earlylick_or_other",
    }.get(_safe_int(outcome, default=-1), "unknown")


def classify_perf_state(perf_pct):
    if not np.isfinite(perf_pct):
        return -1
    if perf_pct < 60.0:
        return 0
    if perf_pct < 70.0:
        return 1
    return 2


def classify_bias_state(bias_score, neutral_threshold=0.1):
    if not np.isfinite(bias_score):
        return -1
    threshold = abs(float(neutral_threshold))
    if bias_score < -threshold:
        return 0
    if bias_score > threshold:
        return 2
    return 1


def compute_hourly_behavior_states(trials, slice_start_ms, slice_end_ms, config=None):
    cfg = dict(DEFAULT_BEHAVIOR_CONFIG)
    if config:
        cfg.update(config)

    start_ms = float(slice_start_ms)
    end_ms = float(slice_end_ms)
    if end_ms <= start_ms:
        raise ValueError("slice_end_ms must be later than slice_start_ms.")

    trials = filter_behavior_trials_for_slice(trials or [], start_ms, end_ms)
    trial_time_s = np.asarray(
        [(float(trial["time_epoch_ms"]) - start_ms) / 1000.0 for trial in trials],
        dtype=np.float64,
    )
    trial_epoch_ms = np.asarray([int(round(float(trial["time_epoch_ms"]))) for trial in trials], dtype=np.int64)
    trial_num = np.asarray([_safe_int(trial.get("trial_num"), default=-1) for trial in trials], dtype=np.int32)
    protocol_index = np.asarray([_safe_int(trial.get("protocol_index"), default=-1) for trial in trials], dtype=np.int16)
    trial_type = np.asarray([_safe_int(trial.get("trial_type"), default=-1) for trial in trials], dtype=np.int16)
    trial_outcome = np.asarray([_safe_int(trial.get("trial_outcome"), default=-1) for trial in trials], dtype=np.int16)
    rule = np.asarray([_resolve_rule(trial.get("rule"), trial.get("protocol_index")) for trial in trials], dtype=np.int16)
    inferred_choice = np.asarray(
        [infer_choice_from_trial(tt, outcome) for tt, outcome in zip(trial_type, trial_outcome)],
        dtype=np.int16,
    )
    correct_flag = np.full(trial_outcome.shape, -1, dtype=np.int8)
    correct_flag[trial_outcome == 1] = 1
    correct_flag[trial_outcome == 2] = 0

    perf_values = np.where(correct_flag >= 0, correct_flag.astype(np.float64) * 100.0, np.nan)
    choice_values = np.full(inferred_choice.shape, np.nan, dtype=np.float64)
    choice_values[inferred_choice == 1] = -1.0
    choice_values[inferred_choice == 2] = 1.0
    smoothed_perf = _smooth_series_by_trial_index(
        perf_values,
        int(cfg["gaussian_window_trials"]),
        float(cfg["gaussian_sigma_trials"]),
    )
    smoothed_bias = _smooth_series_by_trial_index(
        choice_values,
        int(cfg["gaussian_window_trials"]),
        float(cfg["gaussian_sigma_trials"]),
    )

    duration_s = (end_ms - start_ms) / 1000.0
    hour_edges_s = _make_hour_edges(duration_s, float(cfg["hour_bin_s"]))
    n_hours = max(0, len(hour_edges_s) - 1)
    hourly_time_s = (hour_edges_s[:-1] + hour_edges_s[1:]) / 2.0
    hourly_rule = np.full(n_hours, -1, dtype=np.int16)
    hourly_perf_pct = np.full(n_hours, np.nan, dtype=np.float32)
    hourly_perf_state = np.full(n_hours, -1, dtype=np.int8)
    hourly_bias_score = np.full(n_hours, np.nan, dtype=np.float32)
    hourly_bias_state = np.full(n_hours, -1, dtype=np.int8)
    hourly_trial_count = np.zeros(n_hours, dtype=np.int32)
    hourly_valid_perf_count = np.zeros(n_hours, dtype=np.int32)
    hourly_choice_count = np.zeros(n_hours, dtype=np.int32)
    hourly_fill_source = np.zeros(n_hours, dtype=np.int8)

    min_trials = int(cfg["min_trials_per_hour"])
    for hour_idx in range(n_hours):
        start_s = hour_edges_s[hour_idx]
        end_s = hour_edges_s[hour_idx + 1]
        if hour_idx == n_hours - 1:
            mask = (trial_time_s >= start_s) & (trial_time_s <= end_s)
        else:
            mask = (trial_time_s >= start_s) & (trial_time_s < end_s)
        hourly_trial_count[hour_idx] = int(np.sum(mask))
        valid_perf_mask = mask & (correct_flag >= 0)
        valid_choice_mask = mask & np.isin(inferred_choice, [1, 2])
        hourly_valid_perf_count[hour_idx] = int(np.sum(valid_perf_mask))
        hourly_choice_count[hour_idx] = int(np.sum(valid_choice_mask))

        if np.any(mask):
            hourly_rule[hour_idx] = _mode_int(rule[mask], valid_values=(0, 1, 2))
            has_local_support = (
                hourly_valid_perf_count[hour_idx] >= min_trials
                or hourly_choice_count[hour_idx] >= min_trials
            )
            hourly_fill_source[hour_idx] = 1 if has_local_support else 2
            if np.isfinite(smoothed_perf[mask]).any():
                hourly_perf_pct[hour_idx] = float(np.nanmean(smoothed_perf[mask]))
            if np.isfinite(smoothed_bias[mask]).any():
                hourly_bias_score[hour_idx] = float(np.nanmean(smoothed_bias[mask]))
        elif hour_idx > 0 and hourly_fill_source[hour_idx - 1] != 0:
            hourly_rule[hour_idx] = hourly_rule[hour_idx - 1]
            hourly_perf_pct[hour_idx] = hourly_perf_pct[hour_idx - 1]
            hourly_bias_score[hour_idx] = hourly_bias_score[hour_idx - 1]
            hourly_fill_source[hour_idx] = 3

        if hourly_rule[hour_idx] == -1 and hour_idx > 0 and hourly_fill_source[hour_idx] != 0:
            hourly_rule[hour_idx] = hourly_rule[hour_idx - 1]
        hourly_perf_state[hour_idx] = classify_perf_state(hourly_perf_pct[hour_idx])
        hourly_bias_state[hour_idx] = classify_bias_state(
            hourly_bias_score[hour_idx],
            neutral_threshold=float(cfg["bias_neutral_threshold"]),
        )

    return {
        "trial_time_s": trial_time_s.astype(np.float64),
        "trial_epoch_ms": trial_epoch_ms,
        "trial_num": trial_num,
        "protocol_index": protocol_index,
        "rule": rule,
        "trial_type": trial_type,
        "trial_outcome": trial_outcome,
        "inferred_choice": inferred_choice,
        "correct_flag": correct_flag,
        "trial_smoothed_perf_pct": smoothed_perf.astype(np.float32),
        "trial_smoothed_bias_score": smoothed_bias.astype(np.float32),
        "hourly_time_s": hourly_time_s.astype(np.float64),
        "hourly_edges_s": hour_edges_s.astype(np.float64),
        "hourly_rule": hourly_rule,
        "hourly_perf_pct": hourly_perf_pct,
        "hourly_perf_state": hourly_perf_state,
        "hourly_bias_score": hourly_bias_score,
        "hourly_bias_state": hourly_bias_state,
        "hourly_trial_count": hourly_trial_count,
        "hourly_valid_perf_count": hourly_valid_perf_count,
        "hourly_choice_count": hourly_choice_count,
        "hourly_fill_source": hourly_fill_source,
        "source_trial_files": sorted({str(trial.get("source_file", "")) for trial in trials if trial.get("source_file")}),
        "config": cfg,
    }


def slice_behavior_result_for_window(parent_result, parent_slice_start_ms, window_start_ms, window_end_ms):
    """
    Extract a day-sized behavior group from a full parent-slice behavior result.

    Trial-level smoothing is intentionally kept from the parent result so natural-day
    H5 boundaries do not restart the Gaussian behavior trend.
    """
    parent_start_ms = float(parent_slice_start_ms)
    start_ms = float(window_start_ms)
    end_ms = float(window_end_ms)
    if end_ms <= start_ms:
        raise ValueError("window_end_ms must be later than window_start_ms.")
    offset_s = (start_ms - parent_start_ms) / 1000.0
    duration_s = (end_ms - start_ms) / 1000.0

    result = {
        "source_trial_files": list(parent_result.get("source_trial_files", [])),
        "config": dict(parent_result.get("config", DEFAULT_BEHAVIOR_CONFIG)),
    }

    trial_epoch_ms = np.asarray(parent_result.get("trial_epoch_ms", []), dtype=np.int64)
    trial_mask = (trial_epoch_ms >= int(round(start_ms))) & (trial_epoch_ms < int(round(end_ms)))
    trial_dataset_names = [
        "trial_epoch_ms",
        "trial_num",
        "protocol_index",
        "rule",
        "trial_type",
        "trial_outcome",
        "inferred_choice",
        "correct_flag",
        "trial_smoothed_perf_pct",
        "trial_smoothed_bias_score",
    ]
    for name in trial_dataset_names:
        result[name] = np.asarray(parent_result.get(name, []))[trial_mask]
    result["trial_time_s"] = (trial_epoch_ms[trial_mask].astype(np.float64) - start_ms) / 1000.0

    parent_hourly_time_s = np.asarray(parent_result.get("hourly_time_s", []), dtype=np.float64)
    if parent_hourly_time_s.size:
        parent_hour_abs_ms = parent_start_ms + parent_hourly_time_s * 1000.0
        hour_mask = (parent_hour_abs_ms >= start_ms) & (parent_hour_abs_ms < end_ms)
        hour_indices = np.flatnonzero(hour_mask)
    else:
        hour_indices = np.asarray([], dtype=np.int64)

    hourly_dataset_names = [
        "hourly_rule",
        "hourly_perf_pct",
        "hourly_perf_state",
        "hourly_bias_score",
        "hourly_bias_state",
        "hourly_trial_count",
        "hourly_valid_perf_count",
        "hourly_choice_count",
        "hourly_fill_source",
    ]
    if hour_indices.size:
        first = int(hour_indices[0])
        last = int(hour_indices[-1])
        result["hourly_time_s"] = parent_hourly_time_s[hour_indices] - offset_s
        parent_edges = np.asarray(parent_result.get("hourly_edges_s", []), dtype=np.float64)
        if parent_edges.size >= last + 2:
            edges = parent_edges[first:last + 2] - offset_s
            edges = edges.astype(np.float64, copy=True)
            edges[0] = max(edges[0], 0.0)
            edges[-1] = min(edges[-1], duration_s)
            result["hourly_edges_s"] = edges
        else:
            result["hourly_edges_s"] = _make_hour_edges(duration_s, float(result["config"].get("hour_bin_s", 3600.0)))
        for name in hourly_dataset_names:
            result[name] = np.asarray(parent_result.get(name, []))[hour_indices]
    else:
        result["hourly_time_s"] = np.asarray([], dtype=np.float64)
        result["hourly_edges_s"] = np.asarray([0.0, duration_s], dtype=np.float64)
        empty_specs = {
            "hourly_rule": np.int16,
            "hourly_perf_pct": np.float32,
            "hourly_perf_state": np.int8,
            "hourly_bias_score": np.float32,
            "hourly_bias_state": np.int8,
            "hourly_trial_count": np.int32,
            "hourly_valid_perf_count": np.int32,
            "hourly_choice_count": np.int32,
            "hourly_fill_source": np.int8,
        }
        for name in hourly_dataset_names:
            result[name] = np.asarray([], dtype=empty_specs[name])
    return result


def write_behavior_group(h5_path_or_file, behavior_result, compression="gzip", source_metadata=None):
    should_close = False
    if isinstance(h5_path_or_file, h5py.File):
        h5f = h5_path_or_file
    else:
        h5f = h5py.File(h5_path_or_file, "a")
        should_close = True

    try:
        if "behavior" in h5f:
            del h5f["behavior"]
        group = h5f.create_group("behavior")
        dataset_names = [
            "trial_time_s",
            "trial_epoch_ms",
            "trial_num",
            "protocol_index",
            "rule",
            "trial_type",
            "trial_outcome",
            "inferred_choice",
            "correct_flag",
            "trial_smoothed_perf_pct",
            "trial_smoothed_bias_score",
            "hourly_time_s",
            "hourly_edges_s",
            "hourly_rule",
            "hourly_perf_pct",
            "hourly_perf_state",
            "hourly_bias_score",
            "hourly_bias_state",
            "hourly_trial_count",
            "hourly_valid_perf_count",
            "hourly_choice_count",
            "hourly_fill_source",
        ]
        for name in dataset_names:
            _create_dataset(group, name, behavior_result.get(name, []), compression=compression)

        cfg = dict(DEFAULT_BEHAVIOR_CONFIG)
        cfg.update(behavior_result.get("config", {}))
        metadata = dict(source_metadata or {})
        source_files = sorted(
            set(behavior_result.get("source_trial_files", []))
            | set(metadata.get("source_trial_files", []))
        )
        group.attrs["rule_labels_json"] = json.dumps(RULE_LABELS, ensure_ascii=False)
        group.attrs["perf_state_labels_json"] = json.dumps(PERF_STATE_LABELS, ensure_ascii=False)
        group.attrs["bias_state_labels_json"] = json.dumps(BIAS_STATE_LABELS, ensure_ascii=False)
        group.attrs["choice_labels_json"] = json.dumps(CHOICE_LABELS, ensure_ascii=False)
        group.attrs["fill_source_labels_json"] = json.dumps(FILL_SOURCE_LABELS, ensure_ascii=False)
        group.attrs["hour_bin_s"] = float(cfg["hour_bin_s"])
        group.attrs["min_trials_per_hour"] = int(cfg["min_trials_per_hour"])
        group.attrs["gaussian_window_trials"] = int(cfg["gaussian_window_trials"])
        group.attrs["gaussian_sigma_trials"] = float(cfg["gaussian_sigma_trials"])
        group.attrs["bias_neutral_threshold"] = float(cfg["bias_neutral_threshold"])
        group.attrs["source_trial_files_json"] = json.dumps(source_files, ensure_ascii=False)
        group.attrs["source_metadata_json"] = json.dumps(metadata, ensure_ascii=False)
        return group
    finally:
        if should_close:
            h5f.close()


def _trial_to_record(trial, trial_file, line_no, global_idx, epoch_ms):
    perf = trial.extra_metadata.get("curr_protocol_perf_percent")
    protocol_trials = trial.extra_metadata.get("curr_protocol_trials")
    rule = _resolve_rule(trial.rule, trial.protocol_index)
    choice = infer_choice_from_trial(trial.trial_type, trial.trial_outcome)
    correct_flag = -1
    if int(trial.trial_outcome) == 1:
        correct_flag = 1
    elif int(trial.trial_outcome) == 2:
        correct_flag = 0
    return {
        "source_file": str(Path(trial_file).resolve()),
        "line_no": int(line_no),
        "global_trial_index": int(global_idx),
        "trial_num": int(trial.trial_num),
        "time_epoch_ms": int(epoch_ms),
        "time_iso": epoch_ms_to_iso(epoch_ms),
        "protocol_index": int(trial.protocol_index),
        "rule": int(rule),
        "trial_type": int(trial.trial_type),
        "trial_choice": trial_type_label(trial.trial_type),
        "trial_outcome": int(trial.trial_outcome),
        "outcome_label": outcome_label(trial.trial_outcome),
        "inferred_choice": int(choice),
        "inferred_choice_label": CHOICE_LABELS.get(int(choice), "no_choice"),
        "correct_flag": int(correct_flag),
        "performance_pct": float(perf) if perf is not None else None,
        "curr_protocol_trials": int(protocol_trials) if protocol_trials is not None else None,
        "trial_start_timestamp_ms": (
            float(trial.trial_start_timestamp_ms)
            if trial.trial_start_timestamp_ms is not None
            else None
        ),
    }


def _resolve_rule(rule_value, protocol_index):
    rule = _safe_int(rule_value, default=-1)
    if rule in (0, 1, 2):
        return rule
    protocol = _safe_int(protocol_index, default=-1)
    if 0 <= protocol <= 6:
        return 0
    if protocol in (7, 8):
        return 1
    if protocol in (9, 10):
        return 2
    return -1


def _smooth_series_by_trial_index(values, window_trials, sigma_trials):
    values = np.asarray(values, dtype=np.float64)
    out = np.full(values.shape, np.nan, dtype=np.float64)
    n = int(values.size)
    if n == 0:
        return out
    window = max(1, int(window_trials))
    sigma = max(1e-6, float(sigma_trials))
    half = max(0, window // 2)
    for idx in range(n):
        start = max(0, idx - half)
        end = min(n, idx + half + 1)
        local = values[start:end]
        valid = np.isfinite(local)
        if not np.any(valid):
            continue
        distances = np.arange(start, end, dtype=np.float64) - float(idx)
        weights = np.exp(-0.5 * (distances / sigma) ** 2)
        weights = weights[valid]
        out[idx] = float(np.sum(local[valid] * weights) / np.sum(weights))
    return out


def _make_hour_edges(duration_s, hour_bin_s):
    duration_s = max(0.0, float(duration_s))
    hour_bin_s = max(1.0, float(hour_bin_s))
    edges = [0.0]
    while edges[-1] + hour_bin_s < duration_s:
        edges.append(edges[-1] + hour_bin_s)
    if duration_s > edges[-1]:
        edges.append(duration_s)
    elif len(edges) == 1:
        edges.append(hour_bin_s)
    return np.asarray(edges, dtype=np.float64)


def _mode_int(values, valid_values=None):
    values = np.asarray(values, dtype=np.int64)
    if valid_values is not None:
        values = values[np.isin(values, list(valid_values))]
    if values.size == 0:
        return -1
    unique, counts = np.unique(values, return_counts=True)
    max_count = np.max(counts)
    winners = unique[counts == max_count]
    return int(winners[-1])


def _safe_int(value, default=-1):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _create_dataset(group, name, data, compression="gzip"):
    array = np.asarray(data)
    use_compression = compression if compression and array.ndim > 0 and array.size > 0 else None
    return group.create_dataset(name, data=array, compression=use_compression)


def epoch_ms_to_iso(epoch_ms, timezone=TIMEZONE):
    if epoch_ms is None:
        return None
    return _dt.datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone).isoformat()


def _child_dir_case_insensitive(parent, expected_name):
    if not parent or not os.path.isdir(parent):
        return None
    expected = expected_name.lower()
    for name in os.listdir(parent):
        path = os.path.join(parent, name)
        if os.path.isdir(path) and name.lower() == expected:
            return os.path.realpath(os.path.abspath(path))
    return None


def _find_single_guibw_child(parent):
    if not parent or not os.path.isdir(parent):
        return None
    matches = []
    for name in os.listdir(parent):
        path = os.path.join(parent, name)
        if os.path.isdir(path) and name.upper().startswith("GUIBW"):
            matches.append(path)
    if len(matches) == 1:
        return os.path.realpath(os.path.abspath(matches[0]))
    return None
