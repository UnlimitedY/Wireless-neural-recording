import json
import os
import re
import sys
from datetime import datetime, timezone

import h5py
import numpy as np

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from Common_Analysis.behavior_analysis import (
    compute_hourly_behavior_states,
    write_behavior_group,
)

from .config import resolve_config
from .edf_io import read_edf_file
from .timeline import get_mapping_indices
from .tz_compat import ZoneInfo

MODE0_DEEP_TO_SHALLOW_CHANNELS = [3, 12, 0, 15, 1, 14, 2, 13, 4, 11, 5, 10, 6, 9, 7, 8]
MODE3_DEEP_TO_SHALLOW_CHANNELS = [5, 14, 2, 1, 3, 0, 4, 15, 6, 13, 7, 12, 8, 11, 9, 10]
MODE0_SHALLOW_TO_DEEP_CHANNELS = list(reversed(MODE0_DEEP_TO_SHALLOW_CHANNELS))
MODE3_SHALLOW_TO_DEEP_CHANNELS = list(reversed(MODE3_DEEP_TO_SHALLOW_CHANNELS))
UNIFIED_PROBE_CHANNEL_COUNT = 16


def _round_half_up(value):
    return int(np.floor(float(value) + 0.5))


def _replace_dataset(group, name, **kwargs):
    if name in group:
        del group[name]
    return group.create_dataset(name, **kwargs)


def _get_shallow_to_deep_source_order(file_type):
    if file_type == "mode0_lfp":
        return MODE0_SHALLOW_TO_DEEP_CHANNELS
    if file_type == "mode3_lfp":
        return MODE3_SHALLOW_TO_DEEP_CHANNELS
    return list(range(UNIFIED_PROBE_CHANNEL_COUNT))


def _reorder_lfp_to_unified_probe_order(data_matrix, file_type):
    data = np.asarray(data_matrix, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("LFP data must be 2D [channels, samples] before channel reordering.")
    source_order = _get_shallow_to_deep_source_order(file_type)
    reordered = np.full(
        (UNIFIED_PROBE_CHANNEL_COUNT, data.shape[1]),
        np.nan,
        dtype=data.dtype,
    )
    for dst_idx, src_idx in enumerate(source_order):
        if src_idx < data.shape[0]:
            reordered[dst_idx] = data[src_idx]
    return reordered


def _resample_sensor_to_target_grid(data_matrix, source_fs, target_fs):
    """
    Resample sensor channels onto the target IMU grid using time-based nearest
    neighbor mapping. This preserves the original absolute timing better than
    length-based linspace indexing.
    """
    data = np.asarray(data_matrix, dtype=np.float64)
    if data.ndim != 2:
        raise ValueError("Sensor data must be 2D [channels, samples].")
    if data.shape[1] == 0 or source_fs <= 0 or target_fs <= 0:
        return np.empty((data.shape[0], 0), dtype=data.dtype)

    if np.isclose(float(source_fs), float(target_fs), rtol=0.0, atol=1e-6):
        return np.array(data, copy=True)

    target_len = _round_half_up((data.shape[1] / float(source_fs)) * float(target_fs))
    if target_len <= 0:
        return np.empty((data.shape[0], 0), dtype=data.dtype)

    target_times_s = np.arange(target_len, dtype=np.float64) / float(target_fs)
    source_indices = np.floor((target_times_s * float(source_fs)) + 0.5).astype(np.int64)
    source_indices = np.clip(source_indices, 0, data.shape[1] - 1)
    return data[:, source_indices]


def _resolve_time_origin_epoch_ms(date_str, timezone_name, explicit_origin_ms=None):
    if explicit_origin_ms is not None:
        return float(explicit_origin_ms)
    try:
        tz = ZoneInfo(timezone_name)
        start_dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
        return float(start_dt.timestamp() * 1000.0)
    except Exception:
        return 0.0


def _get_absolute_mapping_indices(row_start_ts, n_samples, fs, target_origin_ts_ms, target_duration_s):
    fs = float(fs)
    n_samples = int(n_samples)
    if fs <= 0 or n_samples <= 0 or target_duration_s <= 0:
        return None

    abs_start_s = (float(row_start_ts) - float(target_origin_ts_ms)) / 1000.0
    start_idx = _round_half_up(abs_start_s * fs)
    end_idx = start_idx + n_samples
    target_max_samples = _round_half_up(float(target_duration_s) * fs)

    write_start = max(0, start_idx)
    write_end = min(target_max_samples, end_idx)
    if write_start >= target_max_samples or write_end <= 0 or write_start >= write_end:
        return None

    return {
        "write_start": write_start,
        "write_end": write_end,
        "read_start": write_start - start_idx,
        "read_end": write_end - start_idx,
        "daily_max": target_max_samples,
    }


def _safe_chunk_samples(total_samples, requested_samples):
    total = max(1, int(total_samples))
    requested = max(1, int(requested_samples))
    return min(total, requested)


def initialize_daily_h5(
    h5_path,
    date_str,
    lfp_fs,
    imu_fs,
    animal_id="Unknown",
    config=None,
    total_duration_s=86400.0,
    time_origin_epoch_ms=None,
    recording_scope="daily",
    extra_metadata=None,
):
    """
    Create or overwrite a daily H5 container for the 24/7 rhythm pipeline.
    """
    cfg = resolve_config(config)
    compression = cfg.get("io.compression", "gzip")
    store_raw_lfp = cfg.get("io.store_raw_lfp", True)
    lfp_chunk_seconds = max(1, int(round(cfg.get("io.lfp_chunk_seconds", 10))))
    imu_chunk_seconds = max(1, int(round(cfg.get("io.imu_chunk_seconds", 10))))

    total_duration_s = float(total_duration_s)
    if total_duration_s <= 0:
        raise ValueError("H5 duration must be greater than zero seconds.")

    timezone_name = cfg.get("general.timezone", "Asia/Shanghai")
    time_origin_epoch_ms = _resolve_time_origin_epoch_ms(
        date_str,
        timezone_name,
        explicit_origin_ms=time_origin_epoch_ms,
    )

    n_lfp_samples = max(1, _round_half_up(total_duration_s * lfp_fs))
    n_imu_samples = max(1, _round_half_up(total_duration_s * imu_fs))
    lfp_chunk_samples = _safe_chunk_samples(n_lfp_samples, lfp_fs * lfp_chunk_seconds)
    imu_chunk_samples = _safe_chunk_samples(n_imu_samples, imu_fs * imu_chunk_seconds)

    with h5py.File(h5_path, "w") as f:
        meta_grp = f.create_group("metadata")
        meta_grp.attrs["date"] = date_str
        meta_grp.attrs["animal_id"] = animal_id
        meta_grp.attrs["timezone"] = timezone_name
        meta_grp.attrs["recording_scope"] = str(recording_scope)
        meta_grp.attrs["duration_s"] = float(total_duration_s)
        meta_grp.attrs["time_origin_epoch_ms"] = float(time_origin_epoch_ms)
        meta_grp.attrs["lfp_sample_rate_hz"] = lfp_fs
        meta_grp.attrs["imu_target_rate_hz"] = imu_fs
        meta_grp.attrs["lfp_phase_compensated"] = bool(
            cfg.get("preprocess.apply_lfp_phase_compensation", True)
        )
        meta_grp.attrs["config_json"] = json.dumps(cfg.as_dict(), ensure_ascii=False)
        for key, value in (extra_metadata or {}).items():
            if value is None:
                continue
            if isinstance(value, (dict, list, tuple)):
                meta_grp.attrs[key] = json.dumps(value, ensure_ascii=False)
            else:
                meta_grp.attrs[key] = value

        time_grp = f.create_group("time")
        time_grp.attrs["lfp_time_s_extent"] = [0.0, float(total_duration_s)]
        time_grp.attrs["time_origin_epoch_ms"] = float(time_origin_epoch_ms)
        try:
            origin_dt = datetime.fromtimestamp(time_origin_epoch_ms / 1000.0, tz=timezone.utc).astimezone(
                ZoneInfo(timezone_name)
            )
            time_grp.attrs["time_origin_iso"] = origin_dt.isoformat()
        except Exception:
            time_grp.attrs["time_origin_iso"] = ""

        lfp_grp = f.create_group("lfp")
        lfp_grp.attrs["channel_order_semantics"] = "probe_shallow_to_deep_renumbered_0_to_15"
        lfp_grp.attrs["channel_0_note"] = "most_superficial_site"
        lfp_grp.attrs["channel_15_note"] = "deepest_site"
        lfp_grp.attrs["mode0_source_channels_shallow_to_deep"] = np.asarray(
            MODE0_SHALLOW_TO_DEEP_CHANNELS,
            dtype=np.int16,
        )
        lfp_grp.attrs["mode3_source_channels_shallow_to_deep"] = np.asarray(
            MODE3_SHALLOW_TO_DEEP_CHANNELS,
            dtype=np.int16,
        )
        if store_raw_lfp:
            lfp_grp.create_dataset(
                "raw_lfp",
                shape=(UNIFIED_PROBE_CHANNEL_COUNT, n_lfp_samples),
                dtype=np.float32,
                fillvalue=np.nan,
                chunks=(1, lfp_chunk_samples),
                compression=compression,
            )
        lfp_grp.create_dataset(
            "missing_mask",
            shape=(n_lfp_samples,),
            dtype=np.bool_,
            fillvalue=True,
            chunks=(lfp_chunk_samples,),
            compression=compression,
        )
        lfp_grp.create_dataset(
            "file_coverage_mask",
            shape=(n_lfp_samples,),
            dtype=np.bool_,
            fillvalue=False,
            chunks=(lfp_chunk_samples,),
            compression=compression,
        )

        imu_grp = f.create_group("imu")
        imu_grp.create_dataset(
            "accel_xyz",
            shape=(3, n_imu_samples),
            dtype=np.float32,
            fillvalue=np.nan,
            chunks=(1, imu_chunk_samples),
            compression=compression,
        )
        imu_grp.create_dataset(
            "update_flag",
            shape=(n_imu_samples,),
            dtype=np.bool_,
            fillvalue=False,
            chunks=(imu_chunk_samples,),
            compression=compression,
        )
        imu_grp.create_dataset(
            "missing_mask",
            shape=(n_imu_samples,),
            dtype=np.bool_,
            fillvalue=True,
            chunks=(imu_chunk_samples,),
            compression=compression,
        )

        task_grp = f.create_group("task")
        task_grp.create_dataset(
            "mode3_active_mask",
            shape=(n_lfp_samples,),
            dtype=np.bool_,
            fillvalue=False,
            compression=compression,
        )
        task_grp.create_dataset(
            "working_mask",
            shape=(n_lfp_samples,),
            dtype=np.bool_,
            fillvalue=False,
            compression=compression,
        )
        task_grp.attrs["trial_dict"] = "{}"

        f.create_group("features")
        f.create_group("states")
        f.create_group("rhythm")
        f.create_group("qc")

    return h5_path


def build_daily_h5(
    date_str,
    day_rows,
    output_dir,
    apply_phase_comp=None,
    config=None,
    h5_basename=None,
    time_origin_epoch_ms=None,
    total_duration_s=86400.0,
    recording_scope="daily",
    extra_metadata=None,
    progress_callback=None,
):
    """
    Construct a daily H5 file and map EDF content onto the absolute 24h timeline.
    """
    cfg = resolve_config(config)
    os.makedirs(output_dir, exist_ok=True)
    h5_path = os.path.join(output_dir, h5_basename or f"{date_str}_continuous_lfp_rhythm.h5")

    lfp_fs = int(cfg.get("io.lfp_sample_rate_hz", 1000))
    imu_fs = int(cfg.get("io.imu_target_rate_hz", 100))
    if apply_phase_comp is None:
        apply_phase_comp = bool(cfg.get("preprocess.apply_lfp_phase_compensation", True))

    animal_id = cfg.get("general.animal_id", "Unknown")
    match = re.search(r"(BW\d{2})", output_dir, re.IGNORECASE)
    if animal_id == "Unknown" and match:
        animal_id = match.group(1).upper()

    timezone_name = cfg.get("general.timezone", "Asia/Shanghai")
    target_origin_epoch_ms = _resolve_time_origin_epoch_ms(
        date_str,
        timezone_name,
        explicit_origin_ms=time_origin_epoch_ms,
    )
    total_duration_s = float(total_duration_s)

    initialize_daily_h5(
        h5_path,
        date_str,
        lfp_fs,
        imu_fs,
        animal_id=animal_id,
        config=cfg,
        total_duration_s=total_duration_s,
        time_origin_epoch_ms=target_origin_epoch_ms,
        recording_scope=recording_scope,
        extra_metadata=extra_metadata,
    )

    with h5py.File(h5_path, "a") as f:
        lfp_dataset = f["lfp/raw_lfp"] if "raw_lfp" in f["lfp"] else None
        lfp_cov = f["lfp/file_coverage_mask"]
        lfp_missing = f["lfp/missing_mask"]
        mode3_mask = f["task/mode3_active_mask"]
        working_mask = f["task/working_mask"]
        accel_dataset = f["imu/accel_xyz"]
        imu_missing = f["imu/missing_mask"]

        trial_dict = {}
        if "trial_dict" in f["task"].attrs:
            try:
                trial_dict = json.loads(f["task"].attrs["trial_dict"])
            except Exception:
                pass

        def find_ch(name, labels):
            for idx, label in enumerate(labels):
                if name in label:
                    return idx
            return -1

        total_rows = len(day_rows)
        for row_idx, row in enumerate(day_rows, start=1):
            file_type = row["type"]
            file_path = row["file_path"]
            print(f"[{date_str}] Processing {file_type}: {row['basename']}...")
            if progress_callback is not None:
                progress_callback("row_start", row_idx - 1, total_rows, row)

            if "lfp" in file_type:
                data_matrix, metadata = read_edf_file(
                    file_path,
                    apply_phase_compensation=apply_phase_comp,
                    is_lfp=True,
                    missing_threshold=cfg.get("missing_data.lfp_missing_threshold", -1000),
                )
                if data_matrix is None:
                    continue

                mapping = get_mapping_indices(
                    row["start_ts"],
                    metadata["n_samples"],
                    lfp_fs,
                    date_str,
                    timezone=timezone_name,
                )
                if recording_scope != "daily" or not np.isclose(total_duration_s, 86400.0):
                    mapping = _get_absolute_mapping_indices(
                        row["start_ts"],
                        metadata["n_samples"],
                        lfp_fs,
                        target_origin_epoch_ms,
                        total_duration_s,
                    )
                if mapping is None:
                    continue

                w_start, w_end = mapping["write_start"], mapping["write_end"]
                r_start, r_end = mapping["read_start"], mapping["read_end"]
                reordered_lfp = _reorder_lfp_to_unified_probe_order(data_matrix, file_type)

                if lfp_dataset is not None:
                    lfp_dataset[:, w_start:w_end] = reordered_lfp[:, r_start:r_end]

                lfp_cov[w_start:w_end] = True
                first_ch_slice = reordered_lfp[0, r_start:r_end]
                valid_pts = ~np.isnan(first_ch_slice)
                lfp_missing[w_start:w_end] = ~(valid_pts)

                if "mode3" in file_type and file_type != "mode3_raw_data":
                    duration_s = (row["end_ts"] - row["start_ts"]) / 1000.0
                    effective_end_ts = row["end_ts"]
                    if duration_s > cfg.get("working.working_tail_exclude_s", 30.0):
                        effective_end_ts -= int(
                            round(cfg.get("working.working_tail_exclude_s", 30.0) * 1000.0)
                        )
                    eff_map = get_mapping_indices(
                        row["start_ts"],
                        _round_half_up((effective_end_ts - row["start_ts"]) / 1000.0 * lfp_fs),
                        lfp_fs,
                        date_str,
                        timezone=timezone_name,
                    )
                    if recording_scope != "daily" or not np.isclose(total_duration_s, 86400.0):
                        eff_map = _get_absolute_mapping_indices(
                            row["start_ts"],
                            _round_half_up((effective_end_ts - row["start_ts"]) / 1000.0 * lfp_fs),
                            lfp_fs,
                            target_origin_epoch_ms,
                            total_duration_s,
                        )
                    if eff_map:
                        ew_start, ew_end = eff_map["write_start"], eff_map["write_end"]
                        mode3_mask[ew_start:ew_end] = True

            elif "mode3_raw" in file_type:
                data_matrix, metadata = read_edf_file(
                    file_path,
                    apply_phase_compensation=False,
                    is_lfp=False,
                    missing_threshold=-1.0,
                )
                if data_matrix is None:
                    continue

                labels_str = [label.strip().lower() for label in metadata["labels"]]
                ali_ch = find_ch("alignment", labels_str)
                if ali_ch == -1:
                    continue

                raw_mapping = None
                if recording_scope != "daily" or not np.isclose(total_duration_s, 86400.0):
                    raw_mapping = _get_absolute_mapping_indices(
                        row["start_ts"],
                        metadata["n_samples"],
                        float(metadata.get("fs", lfp_fs)),
                        target_origin_epoch_ms,
                        total_duration_s,
                    )
                if raw_mapping is not None:
                    channel_data = data_matrix[ali_ch, raw_mapping["read_start"]:raw_mapping["read_end"]]
                else:
                    channel_data = data_matrix[ali_ch]
                trials = np.unique(channel_data[channel_data > 0])
                trials_list = [int(t) for t in trials if not np.isnan(t)]
                duration_s = (row["end_ts"] - row["start_ts"]) / 1000.0
                effective_end_ts = row["end_ts"]
                if duration_s > cfg.get("working.working_tail_exclude_s", 30.0):
                    effective_end_ts -= int(
                        round(cfg.get("working.working_tail_exclude_s", 30.0) * 1000.0)
                    )
                block_start_ts = float(row["start_ts"])
                if recording_scope != "daily" or not np.isclose(total_duration_s, 86400.0):
                    slice_end_ts = target_origin_epoch_ms + total_duration_s * 1000.0
                    block_start_ts = max(block_start_ts, target_origin_epoch_ms)
                    effective_end_ts = min(float(effective_end_ts), slice_end_ts)
                start_dt = datetime.fromtimestamp(
                    block_start_ts / 1000.0, tz=timezone.utc
                ).astimezone(ZoneInfo(timezone_name))
                start_str = start_dt.strftime("%H:%M:%S")
                eff_duration_s = max(0.0, (effective_end_ts - block_start_ts) / 1000.0)
                key = f"{start_str} | {eff_duration_s:.1f}s"
                trial_dict[key] = {
                    "trials": trials_list,
                    "start_ts": block_start_ts,
                    "end_ts": effective_end_ts,
                }
                f["task"].attrs["trial_dict"] = json.dumps(trial_dict)

            elif "sensor" in file_type:
                data_matrix, metadata = read_edf_file(
                    file_path,
                    apply_phase_compensation=False,
                    is_lfp=False,
                    missing_threshold=cfg.get("missing_data.sensor_missing_threshold", -10),
                )
                if data_matrix is None:
                    continue

                source_fs = float(metadata.get("fs", 0.0))
                data_matrix_sync = _resample_sensor_to_target_grid(data_matrix, source_fs, imu_fs)
                target_len = int(data_matrix_sync.shape[1])
                if target_len <= 0:
                    continue

                map_imu = get_mapping_indices(
                    row["start_ts"],
                    target_len,
                    imu_fs,
                    date_str,
                    timezone=timezone_name,
                )
                if recording_scope != "daily" or not np.isclose(total_duration_s, 86400.0):
                    map_imu = _get_absolute_mapping_indices(
                        row["start_ts"],
                        target_len,
                        imu_fs,
                        target_origin_epoch_ms,
                        total_duration_s,
                    )
                if map_imu is None:
                    continue

                r_start, r_end = map_imu["read_start"], map_imu["read_end"]
                w_start, w_end = map_imu["write_start"], map_imu["write_end"]

                labels_str = [label.strip().lower() for label in metadata["labels"]]
                ch_x = find_ch("acclx", labels_str)
                ch_y = find_ch("accly", labels_str)
                ch_z = find_ch("acclz", labels_str)
                ch_up = find_ch("updateflag", labels_str)

                available_axes = []
                for axis_idx, source_ch in enumerate([ch_x, ch_y, ch_z]):
                    if source_ch != -1:
                        accel_dataset[axis_idx, w_start:w_end] = data_matrix_sync[source_ch, r_start:r_end]
                        available_axes.append(data_matrix_sync[source_ch, r_start:r_end])

                if ch_up != -1:
                    f["imu/update_flag"][w_start:w_end] = data_matrix_sync[ch_up, r_start:r_end] > 0

                if available_axes:
                    axis_stack = np.vstack(available_axes)
                    valid_pts = np.any(~np.isnan(axis_stack), axis=0)
                    imu_missing[w_start:w_end] = ~valid_pts

        if progress_callback is not None:
            progress_callback("all_rows_complete", total_rows, total_rows, None)
        working_mask[:] = False
        return h5_path


def _sanitize_filename_component(text):
    value = str(text or "slice").strip()
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    value = value.strip("._-")
    return value or "slice"


def slice_h5_basename(slice_payload, slice_item, config=None):
    cfg = resolve_config(config)
    timezone_name = slice_payload.get("timezone") or cfg.get("general.timezone", "Asia/Shanghai")
    start_ms = float(slice_item["start_epoch_ms"])
    start_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    bw_id = slice_payload.get("bw_id") or cfg.get("general.animal_id", "Unknown")
    label = slice_item.get("label") or f"slice_{slice_item.get('id', 1)}"
    start_token = start_dt.strftime("%Y%m%d-%H%M%S")
    return (
        f"{_sanitize_filename_component(bw_id)}_"
        f"{_sanitize_filename_component(label)}_"
        f"{start_token}_slice_lfp_rhythm.h5"
    )


def expected_slice_h5_path(slice_payload, slice_item, output_dir, config=None):
    return os.path.join(
        os.path.abspath(str(output_dir)),
        slice_h5_basename(slice_payload, slice_item, config=config),
    )


def build_slice_h5(
    slice_payload,
    slice_item,
    slice_rows,
    output_dir,
    source_slice_json=None,
    apply_phase_comp=None,
    config=None,
    behavior_trials=None,
    behavior_result=None,
    behavior_source_metadata=None,
    progress_callback=None,
):
    """
    Build a rhythm H5 whose zero-time is the selected Raw Data Overview slice start.
    """
    cfg = resolve_config(config)
    timezone_name = slice_payload.get("timezone") or cfg.get("general.timezone", "Asia/Shanghai")
    start_ms = float(slice_item["start_epoch_ms"])
    end_ms = float(slice_item["end_epoch_ms"])
    if end_ms <= start_ms:
        raise ValueError("Selected slice end time must be later than start time.")

    start_dt = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    end_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).astimezone(ZoneInfo(timezone_name))
    date_str = start_dt.strftime("%Y-%m-%d")
    duration_s = (end_ms - start_ms) / 1000.0
    label = slice_item.get("label") or f"slice_{slice_item.get('id', 1)}"
    basename = slice_h5_basename(slice_payload, slice_item, config=cfg)

    extra_metadata = {
        "slice_label": str(label),
        "slice_id": int(slice_item.get("id", 0)),
        "parent_slice_id": int(slice_item.get("parent_slice_id", slice_item.get("id", 0))),
        "parent_slice_label": str(slice_item.get("parent_slice_label", label)),
        "slice_day_index": int(slice_item.get("day_index", 0)),
        "slice_day_date": str(slice_item.get("date_str", date_str)),
        "slice_start_epoch_ms": float(start_ms),
        "slice_end_epoch_ms": float(end_ms),
        "slice_start_iso": start_dt.isoformat(),
        "slice_end_iso": end_dt.isoformat(),
        "slice_duration_s": float(duration_s),
        "slice_source_root": str(slice_payload.get("source_root", "")),
        "slice_json_path": str(source_slice_json or ""),
        "slice_payload_version": int(slice_payload.get("version", 0)),
    }

    h5_path = build_daily_h5(
        date_str,
        slice_rows,
        output_dir,
        apply_phase_comp=apply_phase_comp,
        config=cfg,
        h5_basename=basename,
        time_origin_epoch_ms=start_ms,
        total_duration_s=duration_s,
        recording_scope="slice",
        extra_metadata=extra_metadata,
        progress_callback=progress_callback,
    )
    if behavior_result is not None or behavior_trials is not None:
        if progress_callback is not None:
            progress_callback("behavior_start", 0, 1, None)
        if behavior_result is None:
            behavior_result = compute_hourly_behavior_states(
                behavior_trials,
                start_ms,
                end_ms,
            )
        write_behavior_group(
            h5_path,
            behavior_result,
            compression=cfg.get("io.compression", "gzip"),
            source_metadata=behavior_source_metadata,
        )
        if progress_callback is not None:
            progress_callback("behavior_complete", 1, 1, None)
    return h5_path
