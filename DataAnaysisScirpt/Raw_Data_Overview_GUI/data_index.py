import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common_Analysis.edf_io import get_hardware_end_ms
from Common_Analysis.behavior_analysis import (
    iter_trial_files,
    outcome_label,
    parse_behavior_trials,
    trial_type_label,
)


TIMEZONE_NAME = "Asia/Shanghai"
TIMEZONE = _dt.timezone(_dt.timedelta(hours=8), name=TIMEZONE_NAME)
MISSING_THRESHOLD = -1000.0
INDEX_CACHE_VERSION = 5
MIN_LOADABLE_CACHE_VERSION = 4
SLICE_EXPORT_VERSION = 1
FILENAME_TIME_PATTERN = re.compile(r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".mpg", ".mpeg", ".wmv"}


class DatasetLayoutError(ValueError):
    pass


@dataclass
class DatasetLayout:
    selected_root: Path
    source_root: Path
    gui_root: Path
    bw_id: str
    sd_card_dir: Path
    neural_dir: Path
    mode0_dir: Optional[Path]
    mode3_dir: Optional[Path]
    video_dir: Optional[Path]
    warnings: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "selected_root": str(self.selected_root),
            "source_root": str(self.source_root),
            "gui_root": str(self.gui_root),
            "bw_id": self.bw_id,
            "sd_card_dir": str(self.sd_card_dir),
            "neural_dir": str(self.neural_dir),
            "mode0_dir": str(self.mode0_dir) if self.mode0_dir else "",
            "mode3_dir": str(self.mode3_dir) if self.mode3_dir else "",
            "video_dir": str(self.video_dir) if self.video_dir else "",
            "warnings": list(self.warnings),
        }


def parse_filename_epoch_ms(name, timezone=TIMEZONE):
    match = FILENAME_TIME_PATTERN.search(str(name))
    if not match:
        return None
    try:
        dt = _dt.datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
    except ValueError:
        return None
    return int(round(dt.replace(tzinfo=timezone).timestamp() * 1000.0))


def epoch_ms_to_iso(epoch_ms, timezone=TIMEZONE):
    if epoch_ms is None:
        return None
    return _dt.datetime.fromtimestamp(float(epoch_ms) / 1000.0, tz=timezone).isoformat()


def now_iso(timezone=TIMEZONE):
    return _dt.datetime.now(timezone).isoformat()


def extract_bw_id(path):
    match = re.search(r"(BW\d+)", str(path), re.IGNORECASE)
    if not match:
        return "Unknown"
    return match.group(1).upper()


def resolve_dataset_layout(selected_path):
    selected_root = Path(selected_path).expanduser().resolve()
    if not selected_root.exists() or not selected_root.is_dir():
        raise DatasetLayoutError(f"Selected path is not a directory: {selected_root}")

    source_root, gui_root = _resolve_source_and_gui_roots(selected_root)
    sd_card_dir = gui_root / "SD_card_files"
    neural_dir = _first_existing_child_ci(source_root, "Neural") or (gui_root / "Neural")
    video_dir = (
        _first_existing_child_ci(source_root, "Video")
        or _first_existing_child_ci(source_root, "video")
        or _first_existing_child_ci(gui_root, "Video")
        or _first_existing_child_ci(gui_root, "video")
        or (source_root / "Video")
    )
    missing_required = []
    if not sd_card_dir.exists():
        missing_required.append("SD_card_files")
    if not neural_dir.exists():
        missing_required.append("Neural")
    if missing_required:
        raise DatasetLayoutError(
            f"Dataset root {source_root} is missing required folder(s): {', '.join(missing_required)}"
        )

    mode0_dir = _first_existing_child_ci(neural_dir, "Mode0")
    mode3_dir = _first_existing_child_ci(neural_dir, "Mode3")
    warnings = []
    if mode0_dir is None:
        warnings.append(f"Mode0 folder not found under {neural_dir}.")
    if mode3_dir is None:
        warnings.append(f"Mode3 folder not found under {neural_dir}.")
    if not video_dir.exists():
        warnings.append(f"Video folder not found under {source_root} or {gui_root}; video track will be empty.")
        video_dir = None

    return DatasetLayout(
        selected_root=selected_root,
        source_root=source_root,
        gui_root=gui_root,
        bw_id=extract_bw_id(source_root),
        sd_card_dir=sd_card_dir,
        neural_dir=neural_dir,
        mode0_dir=mode0_dir,
        mode3_dir=mode3_dir,
        video_dir=video_dir,
        warnings=warnings,
    )


def _resolve_source_and_gui_roots(selected_root):
    if selected_root.name.upper().startswith("GUIBW"):
        gui_root = selected_root
        parent = selected_root.parent
        if parent.name.upper().startswith("BW"):
            return parent.resolve(), gui_root.resolve()
        return selected_root.resolve(), gui_root.resolve()

    gui_children = [
        item for item in selected_root.iterdir()
        if item.is_dir() and item.name.upper().startswith("GUIBW")
    ]
    if selected_root.name.upper().startswith("BW"):
        if len(gui_children) != 1:
            raise DatasetLayoutError(
                f"Expected exactly one GUIBW* folder under {selected_root}; found {len(gui_children)}."
            )
        return selected_root.resolve(), gui_children[0].resolve()

    if len(gui_children) == 1:
        return selected_root.resolve(), gui_children[0].resolve()
    return selected_root.resolve(), selected_root.resolve()


def _first_existing_child_ci(parent, expected_name):
    if not parent.exists():
        return None
    expected = expected_name.lower()
    for child in parent.iterdir():
        if child.is_dir() and child.name.lower() == expected:
            return child.resolve()
    return None


def collect_edf_paths(layout):
    records = []
    if layout.mode0_dir:
        for path in sorted(layout.mode0_dir.rglob("*.edf")):
            if path.name.lower().endswith("lfp.edf"):
                records.append(("mode0_lfp", path.resolve()))
    if layout.mode3_dir:
        for path in sorted(layout.mode3_dir.rglob("*.edf")):
            lower = path.name.lower()
            if lower.endswith("lfp&esa.edf"):
                records.append(("mode3_lfp_esa", path.resolve()))
            elif lower.endswith("mode3_raw.edf") or lower.endswith("raw_data.edf"):
                records.append(("mode3_raw", path.resolve()))
    return records


def scan_edf_records(
    layout,
    reader_factory=None,
    progress_callback=None,
    progress_start=0,
    progress_end=100,
):
    raw_records = []
    warnings = []
    paths = collect_edf_paths(layout)
    if not paths:
        _emit_progress(progress_callback, progress_end, "No EDF files found.")
        return [], warnings

    for idx, (rec_type, path) in enumerate(paths):
        file_start = progress_start + (progress_end - progress_start) * idx / len(paths)
        file_end = progress_start + (progress_end - progress_start) * (idx + 1) / len(paths)
        _emit_progress(progress_callback, file_start, f"Reading EDF {idx + 1}/{len(paths)}: {path.name}")
        filename_time_ms = parse_filename_epoch_ms(path.name)
        if filename_time_ms is None:
            warnings.append(f"EDF filename has no timestamp and was skipped: {path}")
            _emit_progress(progress_callback, file_end, f"Skipped EDF {idx + 1}/{len(paths)}: missing filename timestamp")
            continue
        try:
            record = _read_edf_record(
                rec_type,
                path,
                filename_time_ms,
                reader_factory,
                progress_callback=progress_callback,
                progress_start=file_start,
                progress_end=file_end,
                progress_label=f"EDF {idx + 1}/{len(paths)}",
            )
        except Exception as exc:
            warnings.append(f"Failed to read EDF {path}: {exc}")
            _emit_progress(progress_callback, file_end, f"Failed EDF {idx + 1}/{len(paths)}: {path.name}")
            continue
        raw_records.append(record)
        _emit_progress(progress_callback, file_end, f"Finished EDF {idx + 1}/{len(paths)}: {path.name}")

    _share_hw_end_for_same_filename_time(raw_records)
    return _align_edf_records(raw_records), warnings


def _read_edf_record(
    rec_type,
    path,
    filename_time_ms,
    reader_factory=None,
    progress_callback=None,
    progress_start=0,
    progress_end=100,
    progress_label="EDF",
):
    if reader_factory is None:
        import pyedflib
        reader_factory = pyedflib.EdfReader

    reader = reader_factory(str(path))
    try:
        labels = list(reader.getSignalLabels()) if hasattr(reader, "getSignalLabels") else []
        missing_channel_idx = select_missing_channel_index(rec_type, labels)
        nsamples = list(reader.getNSamples())
        fs = float(reader.getSampleFrequency(missing_channel_idx))
        n_samples = int(nsamples[missing_channel_idx])
        missing_summary = compute_reader_missing_summary(
            reader,
            fs=fs,
            n_samples=n_samples,
            channel_index=missing_channel_idx,
            missing_threshold=MISSING_THRESHOLD,
            progress_callback=progress_callback,
            progress_start=progress_start,
            progress_end=progress_end,
            progress_label=f"{progress_label} {labels[missing_channel_idx] if labels else missing_channel_idx}",
        )
    finally:
        reader.close()

    duration_s = n_samples / fs if fs > 0 else 0.0
    hw_end_ms = get_hardware_end_ms(str(path))
    if hw_end_ms <= 0:
        hw_start_ms = 0.0
    else:
        hw_start_ms = hw_end_ms - duration_s * 1000.0

    return {
        "file_path": str(path),
        "basename": path.name,
        "type": rec_type,
        "filename_time_ms": int(filename_time_ms),
        "filename_time_iso": epoch_ms_to_iso(filename_time_ms),
        "hw_end_ms": float(hw_end_ms),
        "hw_start_ms": float(hw_start_ms),
        "fs": float(fs),
        "n_samples": int(n_samples),
        "duration_s": float(duration_s),
        "labels": labels,
        "missing_channel_index": int(missing_channel_idx),
        "missing_channel_label": str(labels[missing_channel_idx]) if labels else str(missing_channel_idx),
        "valid_samples": int(missing_summary["valid_samples"]),
        "missing_samples": int(missing_summary["missing_samples"]),
        "valid_fraction": float(missing_summary["valid_fraction"]),
        "missing_fraction": float(missing_summary["missing_fraction"]),
        "hw_session_id": -1,
        "boot_offset": 0.0,
        "start_epoch_ms": int(filename_time_ms),
        "end_epoch_ms": int(round(filename_time_ms + duration_s * 1000.0)),
        "time_source": "filename_fallback",
    }


def select_missing_channel_index(rec_type, labels):
    normalized = [_normalize_signal_label(label) for label in labels]
    if rec_type in {"mode0_lfp", "mode3_lfp_esa"}:
        for idx, label in enumerate(normalized):
            if _is_ch0_label(label):
                return idx
        raise ValueError(
            "Missing Ch0-like channel for missing-data summary "
            f"({rec_type}). Labels: {_format_labels_for_error(labels)}"
        )

    if rec_type == "mode3_raw":
        for idx, label in enumerate(normalized):
            if _is_raw_data_label(label):
                return idx
        raise ValueError(
            "Missing RawData-like channel for missing-data summary "
            f"({rec_type}). Labels: {_format_labels_for_error(labels)}"
        )

    raise ValueError(f"Unknown EDF record type for missing-data summary: {rec_type}")


def _normalize_signal_label(label):
    return re.sub(r"[^a-z0-9]+", "", str(label).strip().lower())


def _is_ch0_label(normalized_label):
    if normalized_label == "ch0":
        return True
    return normalized_label.startswith("ch0") and not normalized_label[3:4].isdigit()


def _is_raw_data_label(normalized_label):
    if "align" in normalized_label:
        return False
    if normalized_label in {"raw", "raw0", "rawdata", "rawdata0"}:
        return True
    if normalized_label.startswith("rawdata"):
        return True
    return normalized_label.startswith("raw0") and not normalized_label[4:5].isdigit()


def _format_labels_for_error(labels, max_labels=12):
    labels = [str(label) for label in labels]
    shown = labels[:max_labels]
    suffix = "" if len(labels) <= max_labels else f", ... ({len(labels)} total)"
    return "[" + ", ".join(repr(label) for label in shown) + suffix + "]"


def compute_reader_missing_summary(
    reader,
    fs,
    n_samples,
    channel_index=0,
    missing_threshold=MISSING_THRESHOLD,
    progress_callback=None,
    progress_start=0,
    progress_end=100,
    progress_label="EDF",
):
    fs = float(fs)
    n_samples = int(n_samples)
    if fs <= 0 or n_samples <= 0:
        return {
            "valid_samples": 0,
            "missing_samples": 0,
            "valid_fraction": 0.0,
            "missing_fraction": 0.0,
        }

    chunk_samples = max(1, int(min(max(fs * 120.0, 100000.0), 5000000.0)))
    valid_samples = 0
    processed = 0
    while processed < n_samples:
        start_sample = processed
        sample_count = min(n_samples - start_sample, chunk_samples)
        chunk = np.asarray(reader.readSignal(channel_index, start_sample, sample_count), dtype=np.float64)
        if chunk.size == 0:
            break
        valid = np.isfinite(chunk) & (chunk > missing_threshold)
        valid_samples += int(np.count_nonzero(valid))
        processed += int(chunk.size)
        _emit_progress(
            progress_callback,
            progress_start + (progress_end - progress_start) * min(processed, n_samples) / max(n_samples, 1),
            f"{progress_label}: missing summary {min(processed, n_samples)}/{n_samples} samples",
        )

    missing_samples = int(n_samples - valid_samples)
    valid_fraction = float(valid_samples) / float(n_samples)
    return {
        "valid_samples": int(valid_samples),
        "missing_samples": missing_samples,
        "valid_fraction": valid_fraction,
        "missing_fraction": 1.0 - valid_fraction,
    }


def _share_hw_end_for_same_filename_time(records):
    grouped = {}
    for record in records:
        grouped.setdefault(record["filename_time_ms"], []).append(record)
    for same_time_records in grouped.values():
        valid_hw = [item["hw_end_ms"] for item in same_time_records if item["hw_end_ms"] > 0]
        if not valid_hw:
            continue
        shared = valid_hw[0]
        for item in same_time_records:
            item["hw_end_ms"] = shared
            item["hw_start_ms"] = shared - item["duration_s"] * 1000.0


def _align_edf_records(records):
    if not records:
        return []
    enriched = []
    for item in sorted(records, key=lambda rec: rec["filename_time_ms"]):
        item = dict(item)
        if item["hw_start_ms"] > 0:
            item["boot_offset"] = float(item["filename_time_ms"] - item["hw_start_ms"])
        enriched.append(item)

    session_id = 0
    last_valid_offset = None
    for item in enriched:
        if item["hw_start_ms"] <= 0:
            continue
        current_offset = item["boot_offset"]
        if last_valid_offset is not None and abs(current_offset - last_valid_offset) > 5000.0:
            session_id += 1
        item["hw_session_id"] = session_id
        last_valid_offset = current_offset

    session_offsets = {}
    for item in enriched:
        if item["hw_session_id"] != -1:
            session_offsets.setdefault(item["hw_session_id"], []).append(item["boot_offset"])

    for item in enriched:
        if item["hw_session_id"] != -1:
            offsets = sorted(session_offsets[item["hw_session_id"]])
            median_offset = offsets[len(offsets) // 2]
            item["start_epoch_ms"] = int(round(item["hw_start_ms"] + median_offset))
            item["end_epoch_ms"] = int(round(item["hw_end_ms"] + median_offset))
            item["time_source"] = "hardware_timestamp_session_aligned"
        item["start_iso"] = epoch_ms_to_iso(item["start_epoch_ms"])
        item["end_iso"] = epoch_ms_to_iso(item["end_epoch_ms"])
    return sorted(enriched, key=lambda rec: rec["start_epoch_ms"])


def iter_video_files(video_dir):
    if not video_dir or not Path(video_dir).exists():
        return []
    return sorted(
        path.resolve()
        for path in Path(video_dir).rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
    )


def ffprobe_duration_s(video_path, ffprobe_path="ffprobe"):
    executable = shutil.which(ffprobe_path) if os.path.basename(ffprobe_path) == ffprobe_path else ffprobe_path
    if not executable:
        raise FileNotFoundError("ffprobe not found")
    completed = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)
    duration = payload.get("format", {}).get("duration")
    if duration is None:
        return None
    return float(duration)


def opencv_duration_s(video_path, ffprobe_path="ffprobe"):
    try:
        import cv2
    except Exception as exc:
        raise FileNotFoundError("OpenCV video reader not available") from exc
    cap = cv2.VideoCapture(str(video_path))
    try:
        if not cap.isOpened():
            return None
        frame_count = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if frame_count <= 0.0 or fps <= 0.0:
            return None
        return frame_count / fps
    finally:
        cap.release()


def video_duration_s(video_path, ffprobe_path="ffprobe"):
    errors = []
    for reader in (ffprobe_duration_s, opencv_duration_s):
        try:
            duration = reader(video_path, ffprobe_path=ffprobe_path)
        except Exception as exc:
            errors.append(exc)
            continue
        if duration is not None and duration > 0:
            return float(duration)
    if errors and all(isinstance(exc, FileNotFoundError) for exc in errors):
        raise FileNotFoundError("; ".join(str(exc) for exc in errors))
    if errors:
        raise RuntimeError("; ".join(str(exc) for exc in errors))
    return None


def scan_video_records(
    layout,
    ffprobe_path="ffprobe",
    duration_reader=None,
    progress_callback=None,
    progress_start=0,
    progress_end=100,
):
    duration_reader = duration_reader or video_duration_s
    records = []
    warnings = []
    ffprobe_missing_warned = False
    video_files = iter_video_files(layout.video_dir)
    if not video_files:
        _emit_progress(progress_callback, progress_end, "No video files found.")
        return records, warnings
    for idx, path in enumerate(video_files):
        _emit_progress(
            progress_callback,
            progress_start + (progress_end - progress_start) * idx / len(video_files),
            f"Reading video {idx + 1}/{len(video_files)}: {path.name}",
        )
        start_ms = parse_filename_epoch_ms(path.name)
        if start_ms is None:
            start_ms = parse_filename_epoch_ms(str(path))
        if start_ms is None:
            warnings.append(f"Video filename has no timestamp and was skipped: {path}")
            continue
        duration_s = None
        try:
            duration_s = duration_reader(path, ffprobe_path=ffprobe_path)
        except FileNotFoundError:
            if not ffprobe_missing_warned:
                warnings.append(
                    "Video duration reader not found; durations are marked as unknown. "
                    "Install ffmpeg/ffprobe or OpenCV to enable video coverage lengths."
                )
                ffprobe_missing_warned = True
        except Exception as exc:
            warnings.append(f"Failed to read video duration for {path}: {exc}")
        if duration_s is not None and duration_s <= 0:
            duration_s = None
        end_ms = int(round(start_ms + duration_s * 1000.0)) if duration_s is not None else int(start_ms)
        records.append(
            {
                "file_path": str(path),
                "basename": path.name,
                "start_epoch_ms": int(start_ms),
                "end_epoch_ms": end_ms,
                "start_iso": epoch_ms_to_iso(start_ms),
                "end_iso": epoch_ms_to_iso(end_ms) if duration_s is not None else None,
                "duration_s": float(duration_s) if duration_s is not None else None,
                "duration_known": duration_s is not None,
            }
        )
        _emit_progress(
            progress_callback,
            progress_start + (progress_end - progress_start) * (idx + 1) / len(video_files),
            f"Finished video {idx + 1}/{len(video_files)}: {path.name}",
        )
    return sorted(records, key=lambda rec: rec["start_epoch_ms"]), warnings


def build_dataset_index(
    selected_path,
    use_cache=True,
    ffprobe_path="ffprobe",
    reader_factory=None,
    duration_reader=None,
    progress_callback=None,
):
    _emit_progress(progress_callback, 1, "Resolving dataset layout...")
    layout = resolve_dataset_layout(selected_path)
    _emit_progress(progress_callback, 5, f"Dataset root: {layout.source_root}")
    candidate_paths = _collect_candidate_source_paths(layout)
    _emit_progress(progress_callback, 8, f"Found {len(candidate_paths)} candidate source files.")
    source_signature = _source_signature(candidate_paths)
    _emit_progress(progress_callback, 9, _format_source_counts(source_signature))
    ffprobe_state = _ffprobe_cache_state(ffprobe_path, duration_reader)
    if use_cache:
        cached, loaded_from = _load_valid_cache(
            get_cache_lookup_paths(layout.source_root),
            layout,
            source_signature,
            ffprobe_state,
        )
        if cached is not None:
            cached.setdefault("logs", []).append(f"Loaded saved analysis: {loaded_from}")
            _emit_progress(progress_callback, 100, f"Loaded saved analysis: {loaded_from}")
            return cached

    logs = list(layout.warnings)
    _emit_progress(progress_callback, 12, "Parsing behavior Trial.txt files...")
    behavior_trials, behavior_warnings = parse_behavior_trials(layout.sd_card_dir)
    logs.extend(behavior_warnings)
    _emit_progress(progress_callback, 20, f"Parsed {len(behavior_trials)} behavior trials.")
    edf_records, edf_warnings = scan_edf_records(
        layout,
        reader_factory=reader_factory,
        progress_callback=progress_callback,
        progress_start=20,
        progress_end=86,
    )
    logs.extend(edf_warnings)
    _emit_progress(progress_callback, 86, f"Indexed {len(edf_records)} EDF files.")
    video_records, video_warnings = scan_video_records(
        layout,
        ffprobe_path=ffprobe_path,
        duration_reader=duration_reader,
        progress_callback=progress_callback,
        progress_start=86,
        progress_end=96,
    )
    logs.extend(video_warnings)
    _emit_progress(progress_callback, 97, "Building timeline index...")

    start_ms, end_ms = _compute_global_range(behavior_trials, edf_records, video_records)
    index = {
        "version": INDEX_CACHE_VERSION,
        "bw_id": layout.bw_id,
        "source_root": str(layout.source_root),
        "selected_root": str(layout.selected_root),
        "timezone": TIMEZONE_NAME,
        "created_at": now_iso(),
        "edf_missing_metric": "per_file_total_fraction_named_channel",
        "global_start_epoch_ms": start_ms,
        "global_end_epoch_ms": end_ms,
        "global_start_iso": epoch_ms_to_iso(start_ms),
        "global_end_iso": epoch_ms_to_iso(end_ms),
        "layout": layout.to_dict(),
        "behavior_trials": behavior_trials,
        "edf_records": edf_records,
        "video_records": video_records,
        "logs": logs,
        "source_signature": source_signature,
        "source_counts": source_signature.get("counts", {}),
        "ffprobe_state": ffprobe_state,
    }
    if use_cache:
        cache_paths = get_cache_paths(layout.source_root)
        index["analysis_cache_paths"] = [str(path) for path in cache_paths]
        saved_paths = _save_cache(cache_paths, index)
        index["analysis_cache_paths"] = saved_paths
    _emit_progress(progress_callback, 100, "Scan complete.")
    return index


def load_cached_dataset_index(selected_path, ffprobe_path="ffprobe", duration_reader=None):
    layout = resolve_dataset_layout(selected_path)
    candidate_paths = _collect_candidate_source_paths(layout)
    source_signature = _source_signature(candidate_paths)
    ffprobe_state = _ffprobe_cache_state(ffprobe_path, duration_reader)
    cached, loaded_from = _load_valid_cache(
        get_cache_lookup_paths(layout.source_root),
        layout,
        source_signature,
        ffprobe_state,
    )
    if cached is None:
        return None, source_signature, ""
    cached.setdefault("logs", []).append(f"Loaded saved analysis: {loaded_from}")
    return cached, source_signature, loaded_from


def load_saved_analysis_file(cache_path, selected_path=None):
    cache_path = Path(cache_path).expanduser().resolve()
    with open(cache_path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    _validate_saved_analysis_payload(payload, cache_path)
    _migrate_saved_analysis_payload(payload)
    if selected_path:
        layout = resolve_dataset_layout(selected_path)
        _adapt_cached_payload_to_layout(payload, layout)
        payload.setdefault("logs", []).append(
            f"Manually loaded saved analysis from {cache_path}; source_root adapted to {layout.source_root}."
        )
    else:
        payload.setdefault("logs", []).append(f"Manually loaded saved analysis from {cache_path}.")
    payload["loaded_analysis_path"] = str(cache_path)
    return payload


def _validate_saved_analysis_payload(payload, cache_path):
    if not isinstance(payload, dict):
        raise ValueError(f"Saved analysis file is not a JSON object: {cache_path}")
    version = payload.get("version")
    if version is None:
        raise ValueError(f"Saved analysis file has no version field: {cache_path}")
    try:
        version_int = int(version)
    except Exception:
        raise ValueError(f"Saved analysis version {version} is invalid: {cache_path}")
    if version_int < MIN_LOADABLE_CACHE_VERSION or version_int > INDEX_CACHE_VERSION:
        raise ValueError(
            f"Saved analysis version {version} is not supported by current version {INDEX_CACHE_VERSION}."
        )
    required = ["behavior_trials", "edf_records", "video_records"]
    missing = [key for key in required if key not in payload]
    if missing:
        raise ValueError(f"Saved analysis file is missing required field(s): {', '.join(missing)}")


def _compute_global_range(behavior_trials, edf_records, video_records):
    starts = []
    ends = []
    for trial in behavior_trials:
        starts.append(trial["time_epoch_ms"])
        ends.append(trial["time_epoch_ms"])
    for record in edf_records:
        starts.append(record["start_epoch_ms"])
        ends.append(record["end_epoch_ms"])
    for record in video_records:
        starts.append(record["start_epoch_ms"])
        ends.append(record["end_epoch_ms"])
    if not starts:
        return None, None
    return int(min(starts)), int(max(ends))


def _emit_progress(progress_callback, value, message):
    if progress_callback is None:
        return
    try:
        progress_callback(int(round(float(value))), str(message))
    except Exception:
        pass


def _collect_candidate_source_paths(layout):
    paths = []
    paths.extend(iter_trial_files(layout.sd_card_dir))
    paths.extend(path for _, path in collect_edf_paths(layout))
    paths.extend(iter_video_files(layout.video_dir))
    return sorted(set(Path(path).resolve() for path in paths))


def _source_signature(paths):
    counts = {"trial": 0, "edf": 0, "video": 0, "other": 0}
    total_size = 0
    latest_mtime_ns = 0
    path_hash = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        category = _source_category(path)
        counts[category] = counts.get(category, 0) + 1
        size = int(stat.st_size)
        mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)))
        total_size += size
        latest_mtime_ns = max(latest_mtime_ns, mtime_ns)
        path_hash.update(str(Path(path).resolve()).encode("utf-8", errors="replace"))
        path_hash.update(b"\0")
    return {
        "counts": counts,
        "total_files": int(sum(counts.values())),
        "total_size": int(total_size),
        "latest_mtime_ns": int(latest_mtime_ns),
        "path_hash": path_hash.hexdigest(),
    }


def _source_category(path):
    lower_name = Path(path).name.lower()
    suffix = Path(path).suffix.lower()
    if lower_name == "trial.txt":
        return "trial"
    if suffix == ".edf":
        return "edf"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return "other"


def _format_source_counts(source_signature):
    counts = source_signature.get("counts", {})
    return (
        "Source counts: "
        f"Trial.txt={counts.get('trial', 0)}, "
        f"EDF={counts.get('edf', 0)}, "
        f"Video={counts.get('video', 0)}"
    )


def _cache_dir_for_root(source_root):
    digest = hashlib.sha256(str(Path(source_root).resolve()).encode("utf-8")).hexdigest()[:20]
    return _user_cache_root() / "wireless_24_7" / "raw_data_overview" / digest


def _user_cache_root():
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache"))


def get_cache_path(source_root):
    return get_cache_paths(source_root)[0]


def get_cache_paths(source_root):
    source_root = Path(source_root).resolve()
    return [
        source_root / ".raw_data_overview_cache" / "index_cache.json",
        _cache_dir_for_root(source_root) / "index_cache.json",
    ]


def get_cache_lookup_paths(source_root):
    paths = list(get_cache_paths(source_root))
    search_root = _user_cache_root() / "wireless_24_7" / "raw_data_overview"
    try:
        paths.extend(sorted(search_root.glob("*/index_cache.json")))
    except Exception:
        pass
    deduped = []
    seen = set()
    for path in paths:
        key = str(Path(path).expanduser())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(Path(path))
    return deduped


def _ffprobe_cache_state(ffprobe_path, duration_reader):
    if duration_reader is not None:
        return {"custom_duration_reader": True}
    executable = shutil.which(ffprobe_path) if os.path.basename(ffprobe_path) == ffprobe_path else ffprobe_path
    return {
        "custom_duration_reader": False,
        "ffprobe_path": str(executable or ffprobe_path),
        "available": bool(executable and Path(executable).exists()),
    }


def _load_valid_cache(cache_paths, layout, source_signature, ffprobe_state):
    for cache_path in cache_paths:
        if not cache_path.exists():
            continue
        try:
            with open(cache_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            continue
        version = payload.get("version")
        try:
            version_int = int(version)
        except Exception:
            continue
        if version is None or version_int < MIN_LOADABLE_CACHE_VERSION or version_int > INDEX_CACHE_VERSION:
            continue
        try:
            _migrate_saved_analysis_payload(payload)
        except Exception:
            continue
        payload_source_root = payload.get("source_root")
        payload_bw_id = payload.get("bw_id")
        same_source_root = payload_source_root == str(layout.source_root)
        same_bw_id = payload_bw_id == layout.bw_id
        if not same_source_root and not same_bw_id:
            continue
        if payload.get("edf_missing_metric") != "per_file_total_fraction_named_channel":
            continue
        payload_signature = payload.get("source_signature") or {"counts": payload.get("source_counts", {})}
        if payload_signature == source_signature and payload.get("ffprobe_state") == ffprobe_state:
            _adapt_cached_payload_to_layout(payload, layout)
            return payload, str(cache_path)
        if not _source_counts_match(payload_signature, source_signature):
            continue
        _adapt_cached_payload_to_layout(payload, layout)
        changed = _describe_signature_changes(payload_signature, source_signature)
        if payload.get("ffprobe_state") != ffprobe_state:
            changed.append("ffprobe state changed")
        if not same_source_root:
            changed.append("source root path changed")
        note = (
            "Loaded saved analysis by file-count match. "
            "Raw files were not reprocessed; click Scan Dataset if you need to refresh changed file contents."
        )
        if changed:
            note += " Differences: " + ", ".join(changed) + "."
        payload.setdefault("logs", []).append(note)
        return payload, str(cache_path)
    return None, ""


def _migrate_saved_analysis_payload(payload):
    original_version = int(payload.get("version", INDEX_CACHE_VERSION))
    if original_version < INDEX_CACHE_VERSION:
        payload["original_cache_version"] = original_version
        payload["version"] = INDEX_CACHE_VERSION
        payload.setdefault("logs", []).append(
            f"Loaded legacy saved analysis version {original_version}; migrated in memory to version {INDEX_CACHE_VERSION}."
        )
    payload.setdefault("timezone", TIMEZONE_NAME)
    payload.setdefault("edf_missing_metric", "per_file_total_fraction_named_channel")
    payload.setdefault("behavior_trials", [])
    payload.setdefault("edf_records", [])
    payload.setdefault("video_records", [])
    payload.setdefault("logs", [])

    for record in payload.get("edf_records", []):
        if "start_epoch_ms" not in record and "start_ts" in record:
            record["start_epoch_ms"] = int(round(float(record["start_ts"])))
        if "end_epoch_ms" not in record and "end_ts" in record:
            record["end_epoch_ms"] = int(round(float(record["end_ts"])))
        if record.get("type") == "mode3_lfp":
            record["type"] = "mode3_lfp_esa"
        if "missing_fraction" not in record and "valid_fraction" in record:
            record["missing_fraction"] = 1.0 - float(record.get("valid_fraction", 0.0))
        if "valid_fraction" not in record and "missing_fraction" in record:
            record["valid_fraction"] = 1.0 - float(record.get("missing_fraction", 0.0))
        record.setdefault("valid_fraction", 1.0)
        record.setdefault("missing_fraction", 1.0 - float(record.get("valid_fraction", 1.0)))

    for record in payload.get("video_records", []):
        if "duration_known" not in record:
            record["duration_known"] = record.get("duration_s") is not None and record.get("end_epoch_ms") is not None

    if payload.get("global_start_epoch_ms") is None or payload.get("global_end_epoch_ms") is None:
        start_ms, end_ms = _compute_global_range(
            payload.get("behavior_trials", []),
            payload.get("edf_records", []),
            payload.get("video_records", []),
        )
        payload["global_start_epoch_ms"] = start_ms
        payload["global_end_epoch_ms"] = end_ms
        payload["global_start_iso"] = epoch_ms_to_iso(start_ms)
        payload["global_end_iso"] = epoch_ms_to_iso(end_ms)

    if "source_counts" not in payload:
        payload["source_counts"] = _counts_from_payload(payload)
    if "source_signature" not in payload:
        payload["source_signature"] = {"counts": dict(payload.get("source_counts", {}))}
    return payload


def _counts_from_payload(payload):
    trial_files = set()
    for trial in payload.get("behavior_trials", []):
        source_file = trial.get("source_file")
        if source_file:
            trial_files.add(source_file)
    return {
        "trial": len(trial_files) if trial_files else (1 if payload.get("behavior_trials") else 0),
        "edf": len(payload.get("edf_records", [])),
        "video": len(payload.get("video_records", [])),
        "other": 0,
    }


def _source_counts_match(cached_signature, current_signature):
    cached_counts = (cached_signature or {}).get("counts", {})
    current_counts = (current_signature or {}).get("counts", {})
    for key in ("trial", "edf", "video"):
        if int(cached_counts.get(key, 0)) != int(current_counts.get(key, 0)):
            return False
    return True


def _describe_signature_changes(cached_signature, current_signature):
    changed = []
    for key, label in [
        ("total_size", "total size changed"),
        ("latest_mtime_ns", "latest mtime changed"),
        ("path_hash", "path hash changed"),
    ]:
        if (cached_signature or {}).get(key) != (current_signature or {}).get(key):
            changed.append(label)
    return changed


def _adapt_cached_payload_to_layout(payload, layout):
    payload["source_root"] = str(layout.source_root)
    payload["selected_root"] = str(layout.selected_root)
    payload["bw_id"] = layout.bw_id
    payload["layout"] = layout.to_dict()
    return payload


def _save_cache(cache_paths, index):
    saved_paths = []
    for cache_path in cache_paths:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as handle:
                json.dump(index, handle, ensure_ascii=False, indent=2)
        except Exception:
            continue
        saved_paths.append(str(cache_path))
    return saved_paths


def build_slice_export(index, slices):
    normalized = []
    for idx, item in enumerate(sorted(slices, key=lambda rec: rec["start_epoch_ms"]), start=1):
        start_ms = int(round(item["start_epoch_ms"]))
        end_ms = int(round(item["end_epoch_ms"]))
        if end_ms < start_ms:
            start_ms, end_ms = end_ms, start_ms
        normalized.append(
            {
                "id": int(item.get("id", idx)),
                "label": str(item.get("label") or f"slice_{idx:03d}"),
                "start_epoch_ms": start_ms,
                "end_epoch_ms": end_ms,
                "start_iso": epoch_ms_to_iso(start_ms),
                "end_iso": epoch_ms_to_iso(end_ms),
                "duration_s": float((end_ms - start_ms) / 1000.0),
            }
        )
    return {
        "version": SLICE_EXPORT_VERSION,
        "bw_id": index.get("bw_id", "Unknown"),
        "source_root": index.get("source_root", ""),
        "timezone": index.get("timezone", TIMEZONE_NAME),
        "created_at": now_iso(),
        "global_start_iso": index.get("global_start_iso"),
        "global_end_iso": index.get("global_end_iso"),
        "slices": normalized,
    }
