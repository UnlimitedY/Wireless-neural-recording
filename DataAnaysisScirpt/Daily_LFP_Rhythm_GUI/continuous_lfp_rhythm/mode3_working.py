import json

import h5py
import numpy as np

from .config import resolve_config


def _replace_dataset(group, name, data, **kwargs):
    if name in group:
        del group[name]
    return group.create_dataset(name, data=data, **kwargs)


def mask_to_bouts(mask):
    mask = np.asarray(mask, dtype=bool)
    padded = np.concatenate([[False], mask, [False]])
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def merge_close_bouts(bouts, max_gap_samples):
    if not bouts:
        return []
    merged = [list(bouts[0])]
    for start, end in bouts[1:]:
        if start - merged[-1][1] <= max_gap_samples:
            merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(int(s), int(e)) for s, e in merged]


def build_working_mask_from_mode3(h5_path, config=None):
    cfg = resolve_config(config)
    with h5py.File(h5_path, "a") as h5f:
        lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
        mode3_mask = np.asarray(h5f["task/mode3_active_mask"][:], dtype=bool)

        merge_gap = int(round(cfg.get("working.working_merge_gap_s", 5.0) * lfp_fs))
        min_bout = int(round(cfg.get("working.working_min_bout_s", 120.0) * lfp_fs))
        tail_exclude = int(round(cfg.get("working.working_tail_exclude_s", 30.0) * lfp_fs))

        bouts = merge_close_bouts(mask_to_bouts(mode3_mask), merge_gap)
        filtered = []
        for start, end in bouts:
            if (end - start) < min_bout:
                continue
            end_adj = max(start, end - tail_exclude)
            if end_adj > start:
                filtered.append((start, end_adj))

        working_mask = np.zeros_like(mode3_mask, dtype=bool)
        for start, end in filtered:
            working_mask[start:end] = True

        task_grp = h5f["task"]
        _replace_dataset(task_grp, "working_mask", data=working_mask, compression="gzip")
        bouts_s = np.asarray(filtered, dtype=np.float64) / lfp_fs if filtered else np.zeros((0, 2))
        _replace_dataset(task_grp, "working_bouts_seconds", data=bouts_s)
        task_grp.attrs["working_bout_summary_json"] = json.dumps(
            [{"start_s": float(s), "end_s": float(e)} for s, e in bouts_s],
            ensure_ascii=False,
        )

    return h5_path


def get_working_window_mask(h5_path, config=None):
    cfg = resolve_config(config)
    with h5py.File(h5_path, "r") as h5f:
        if "feature_time_s" not in h5f["time"]:
            raise ValueError("Feature windows have not been computed yet.")
        if "working_mask" not in h5f["task"]:
            raise ValueError("task/working_mask is missing. Run build_working_mask_from_mode3 first.")

        feature_time_s = np.asarray(h5f["time/feature_time_s"][:], dtype=np.float64)
        lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
        working_mask = np.asarray(h5f["task/working_mask"][:], dtype=bool)
        window_s = float(cfg.get("features.feature_window_s", 60.0))
        threshold = float(cfg.get("working.working_window_min_fraction", 0.5))

        window_half = int(round(window_s * lfp_fs / 2.0))
        result = np.zeros(feature_time_s.shape, dtype=bool)
        for idx, center_s in enumerate(feature_time_s):
            center = int(round(center_s * lfp_fs))
            start = max(0, center - window_half)
            end = min(working_mask.size, center + window_half)
            if end <= start:
                continue
            frac = np.mean(working_mask[start:end])
            result[idx] = frac >= threshold
        return result


def get_mode3_working_mask(h5_path, config=None):
    cfg = resolve_config(config)
    with h5py.File(h5_path, "r") as h5f:
        if "working_mask" in h5f["task"]:
            return np.asarray(h5f["task/working_mask"][:], dtype=bool)
    build_working_mask_from_mode3(h5_path, cfg)
    with h5py.File(h5_path, "r") as h5f:
        return np.asarray(h5f["task/working_mask"][:], dtype=bool)
