import datetime
import os
import sys
import traceback

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from Common_Analysis.behavior_analysis import (
    compute_hourly_behavior_states,
    filter_behavior_trials_for_slice,
    parse_behavior_trials,
    resolve_sd_card_dir_from_source_root,
    slice_behavior_result_for_window,
)

from .config import resolve_config
from .file_index import generate_file_coverage, index_files
from .h5_schema import build_slice_h5, expected_slice_h5_path
from .slice_workflow import filter_records_for_slice, resolve_neural_dirs_from_slice_payload
from .tz_compat import ZoneInfo


def split_slice_into_natural_days(slice_item, timezone_name="Asia/Shanghai"):
    tz = ZoneInfo(timezone_name)
    start_ms = int(round(float(slice_item["start_epoch_ms"])))
    end_ms = int(round(float(slice_item["end_epoch_ms"])))
    if end_ms <= start_ms:
        raise ValueError("slice end must be later than slice start.")

    start_dt = datetime.datetime.fromtimestamp(start_ms / 1000.0, tz=datetime.timezone.utc).astimezone(tz)
    end_dt = datetime.datetime.fromtimestamp(end_ms / 1000.0, tz=datetime.timezone.utc).astimezone(tz)
    current_day = start_dt.date()
    final_day = end_dt.date()
    segments = []
    parent_label = str(slice_item.get("label") or f"slice_{slice_item.get('id', 1)}")
    parent_id = int(slice_item.get("id", 0))
    day_index = 1

    while current_day <= final_day:
        day_start_dt = datetime.datetime.combine(current_day, datetime.time.min, tzinfo=tz)
        next_day_dt = day_start_dt + datetime.timedelta(days=1)
        seg_start_ms = max(start_ms, int(round(day_start_dt.timestamp() * 1000.0)))
        seg_end_ms = min(end_ms, int(round(next_day_dt.timestamp() * 1000.0)))
        if seg_end_ms > seg_start_ms:
            date_str = current_day.strftime("%Y-%m-%d")
            segments.append(
                {
                    **slice_item,
                    "id": (parent_id * 1000 + day_index) if parent_id else day_index,
                    "parent_slice_id": parent_id,
                    "parent_slice_label": parent_label,
                    "label": f"{parent_label}_{date_str}",
                    "date_str": date_str,
                    "day_index": day_index,
                    "start_epoch_ms": seg_start_ms,
                    "end_epoch_ms": seg_end_ms,
                    "duration_s": float((seg_end_ms - seg_start_ms) / 1000.0),
                }
            )
            day_index += 1
        current_day += datetime.timedelta(days=1)
    return segments


def prefilter_records_for_slice_by_filename(records, slice_item, margin_ms):
    start_ms = float(slice_item["start_epoch_ms"]) - float(margin_ms)
    end_ms = float(slice_item["end_epoch_ms"]) + float(margin_ms)
    filtered = []
    for record in records:
        filename_time_ms = float(record.get("filename_time_ms", 0.0) or 0.0)
        if filename_time_ms <= 0:
            filtered.append(record)
            continue
        if start_ms <= filename_time_ms <= end_ms:
            filtered.append(record)
    return filtered


def partition_missing_slice_segments(slice_payload, segments, output_dir, config=None, existing_h5_paths=None):
    existing_by_name = {}
    for path in existing_h5_paths or []:
        if not path:
            continue
        basename = os.path.basename(str(path))
        if basename and os.path.exists(str(path)) and basename not in existing_by_name:
            existing_by_name[basename] = os.path.abspath(str(path))

    missing = []
    existing = []
    for segment in segments or []:
        path = expected_slice_h5_path(slice_payload, segment, output_dir, config=config)
        basename = os.path.basename(path)
        if os.path.exists(path):
            item = {"segment": segment, "path": path}
            existing.append(item)
        elif basename in existing_by_name:
            item = {"segment": segment, "path": existing_by_name[basename]}
            existing.append(item)
        else:
            item = {"segment": segment, "path": path}
            missing.append(item)
    return missing, existing


def build_natural_day_slice_h5s_worker(
    slice_payload,
    slice_item,
    output_dir,
    source_slice_json,
    config_data,
    queue,
    existing_h5_paths=None,
):
    try:
        cfg = resolve_config(config_data)
        timezone_name = slice_payload.get("timezone") or cfg.get("general.timezone", "Asia/Shanghai")
        all_segments = split_slice_into_natural_days(slice_item, timezone_name=timezone_name)
        if not all_segments:
            raise ValueError("Selected slice does not contain any natural-day segment.")
        missing_items, existing_items = partition_missing_slice_segments(
            slice_payload,
            all_segments,
            output_dir,
            config=cfg,
            existing_h5_paths=existing_h5_paths,
        )
        segments = [item["segment"] for item in missing_items]
        existing_paths = [item["path"] for item in existing_items]

        _emit(
            queue,
            "started",
            total=len(segments),
            value=0,
            maximum=100,
            message=(
                f"Slice build plan: {len(segments)} missing / {len(all_segments)} natural-day H5 files "
                f"({len(existing_paths)} already exist)."
            ),
        )
        if existing_paths:
            _emit(
                queue,
                "progress",
                message=f"Skipping {len(existing_paths)} existing natural-day H5 files.",
                value=0,
                maximum=max(len(segments), 1),
            )
        if not segments:
            _emit(
                queue,
                "done",
                built=[],
                skipped_existing=existing_paths,
                total=len(all_segments),
                value=1,
                maximum=1,
                message="All required natural-day H5 files already exist; no build was needed.",
            )
            return

        _emit(queue, "progress", message="Resolving slice source data...")
        source_dirs = resolve_neural_dirs_from_slice_payload(slice_payload)
        mode0_dir = source_dirs.get("mode0_dir")
        mode3_dir = source_dirs.get("mode3_dir")
        if not mode0_dir and not mode3_dir:
            raise ValueError(f"No Mode0/Mode3 folders found under {source_dirs.get('neural_dir')}")

        def on_index_progress(stage, done, total, payload):
            if stage == "scan_dir":
                _emit(
                    queue,
                    "progress",
                    message=(
                        f"Scanning EDF folders: {done} folders checked, "
                        f"{payload.get('found', 0)} candidate EDF files found"
                    ),
                )
            elif stage == "discover_file":
                _emit(
                    queue,
                    "progress",
                    message=f"Discovered EDF candidates: {payload.get('found', done)} files",
                )
            elif stage == "discover_done":
                _emit(
                    queue,
                    "progress",
                    message=(
                        f"EDF discovery complete: {done} files across "
                        f"{payload.get('directories', 0)} folders."
                    ),
                    value=0,
                    maximum=max(int(done or 1), 1),
                )

        _emit(queue, "progress", message="Discovering EDF files from slice source...")
        records = index_files(mode0_dir=mode0_dir, mode3_dir=mode3_dir, progress_callback=on_index_progress)
        if not records:
            raise ValueError("No EDF files found under the slice source Neural folder.")
        _emit(queue, "progress", message=f"Indexed {len(records)} EDF files.")

        prefilter_margin_hours = float(cfg.get("slice_build.filename_prefilter_margin_hours", 36.0))
        prefilter_margin_ms = max(0.0, prefilter_margin_hours * 3600.0 * 1000.0)
        coverage_records = prefilter_records_for_slice_by_filename(records, slice_item, prefilter_margin_ms)
        if coverage_records:
            skipped = len(records) - len(coverage_records)
            if skipped > 0:
                _emit(
                    queue,
                    "progress",
                    message=(
                        f"Filename-time prefilter kept {len(coverage_records)}/{len(records)} EDF files "
                        f"for coverage (margin +/-{prefilter_margin_hours:g} h); skipped {skipped} far-away files."
                    ),
                )
        else:
            coverage_records = records
            _emit(
                queue,
                "progress",
                message=(
                    "Filename-time prefilter found no EDF candidates; falling back to full coverage scan "
                    "to avoid missing data."
                ),
            )

        coverage_units = len(coverage_records)
        behavior_units = 1
        provisional_maximum = max(coverage_units + behavior_units + len(segments), 1)
        _emit(
            queue,
            "started",
            total=len(segments),
            value=0,
            maximum=provisional_maximum,
            message=(
                f"Planned {len(segments)} natural-day H5 files; "
                f"{coverage_units} nearby EDF files need coverage indexing."
            ),
        )

        def on_coverage_progress(done, total, record):
            _emit(
                queue,
                "progress",
                message=f"Computing EDF coverage {done}/{total}: {record.get('basename', '')}",
                value=int(done),
                maximum=provisional_maximum,
            )

        _emit(
            queue,
            "progress",
            message=f"Computing EDF coverage 0/{coverage_units}...",
            value=0,
            maximum=provisional_maximum,
        )
        records = generate_file_coverage(coverage_records, progress_callback=on_coverage_progress)
        day_rows_by_index = [filter_records_for_slice(records, day_slice) for day_slice in segments]
        day_units = [max(len(rows), 1) for rows in day_rows_by_index]
        maximum = max(coverage_units + behavior_units + sum(day_units), 1)
        progress_value = coverage_units
        _emit(
            queue,
            "progress",
            message=f"EDF coverage complete: {coverage_units}/{coverage_units} files.",
            value=progress_value,
            maximum=maximum,
        )

        behavior_trials = []
        full_behavior_result = None
        behavior_source_metadata = None
        try:
            sd_card_dir = resolve_sd_card_dir_from_source_root(
                source_dirs.get("source_root") or slice_payload.get("source_root", "")
            )
            all_behavior_trials, behavior_warnings = parse_behavior_trials(sd_card_dir)
            behavior_trials = filter_behavior_trials_for_slice(
                all_behavior_trials,
                slice_item["start_epoch_ms"],
                slice_item["end_epoch_ms"],
            )
            full_behavior_result = compute_hourly_behavior_states(
                behavior_trials,
                slice_item["start_epoch_ms"],
                slice_item["end_epoch_ms"],
            )
            behavior_source_metadata = {
                "sd_card_dir": sd_card_dir,
                "warnings": behavior_warnings[:50],
                "n_warnings": len(behavior_warnings),
                "behavior_smoothing_scope": "parent_slice",
                "parent_slice_start_epoch_ms": slice_item["start_epoch_ms"],
                "parent_slice_end_epoch_ms": slice_item["end_epoch_ms"],
            }
            _emit(
                queue,
                "progress",
                message=(
                    f"Behavior trials: {len(behavior_trials)} in selected slice "
                    f"({len(all_behavior_trials)} total)."
                ),
                value=progress_value + behavior_units,
                maximum=maximum,
            )
            if behavior_warnings:
                _emit(
                    queue,
                    "progress",
                    message=f"Behavior parse warnings: {len(behavior_warnings)}",
                    value=progress_value + behavior_units,
                    maximum=maximum,
                )
        except Exception as exc:
            behavior_trials = None
            full_behavior_result = None
            _emit(
                queue,
                "progress",
                message=f"Behavior parsing skipped: {exc}",
                value=progress_value + behavior_units,
                maximum=maximum,
            )

        progress_value += behavior_units

        built = []
        completed_build_units = 0
        for idx, (day_slice, day_rows) in enumerate(zip(segments, day_rows_by_index), start=1):
            date_str = day_slice.get("date_str", f"day_{idx}")
            day_unit = day_units[idx - 1]
            day_base_value = progress_value + completed_build_units
            _emit(
                queue,
                "progress",
                message=(
                    f"Building natural-day H5 {idx}/{len(segments)}: {date_str} "
                    f"({len(day_rows)} EDF files)"
                ),
                value=day_base_value,
                maximum=maximum,
            )
            day_behavior = (
                filter_behavior_trials_for_slice(
                    behavior_trials,
                    day_slice["start_epoch_ms"],
                    day_slice["end_epoch_ms"],
                )
                if behavior_trials is not None
                else None
            )
            day_behavior_result = (
                slice_behavior_result_for_window(
                    full_behavior_result,
                    slice_item["start_epoch_ms"],
                    day_slice["start_epoch_ms"],
                    day_slice["end_epoch_ms"],
                )
                if full_behavior_result is not None
                else None
            )

            def on_h5_progress(stage, done, total, row, date_str=date_str, day_base_value=day_base_value, day_unit=day_unit):
                if stage not in {"row_start", "all_rows_complete"}:
                    return
                local_done = min(max(int(done), 0), day_unit)
                if stage == "all_rows_complete":
                    local_done = day_unit if int(total) == 0 else min(int(total), day_unit)
                basename = row.get("basename", "") if isinstance(row, dict) else ""
                message = f"Writing {date_str}: {local_done}/{day_unit} EDF files"
                if basename:
                    message += f" | {basename}"
                _emit(
                    queue,
                    "progress",
                    message=message,
                    value=day_base_value + local_done,
                    maximum=maximum,
                )

            built_path = build_slice_h5(
                slice_payload,
                day_slice,
                day_rows,
                output_dir,
                source_slice_json=source_slice_json,
                config=cfg,
                behavior_result=day_behavior_result,
                behavior_source_metadata=behavior_source_metadata,
                progress_callback=on_h5_progress,
            )
            built.append(built_path)
            completed_build_units += day_unit
            _emit(
                queue,
                "day_complete",
                path=built_path,
                date=date_str,
                index=idx,
                total=len(segments),
                edf_files=len(day_rows),
                behavior_trials=len(day_behavior or []),
                value=progress_value + completed_build_units,
                maximum=maximum,
            )
        _emit(
            queue,
            "done",
            built=built,
            skipped_existing=existing_paths,
            total=len(all_segments),
            value=maximum,
            maximum=maximum,
        )
    except Exception as exc:
        _emit(queue, "error", error=str(exc), traceback=traceback.format_exc())


def _emit(queue, msg_type, **payload):
    if queue is None:
        return
    queue.put({"type": msg_type, **payload})
