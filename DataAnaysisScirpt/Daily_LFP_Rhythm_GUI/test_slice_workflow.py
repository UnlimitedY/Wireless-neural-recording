import datetime
import json
import tempfile
from pathlib import Path

import h5py

from continuous_lfp_rhythm.h5_schema import build_slice_h5, expected_slice_h5_path
from continuous_lfp_rhythm.file_index import index_files
from continuous_lfp_rhythm.slice_background_builder import (
    partition_missing_slice_segments,
    prefilter_records_for_slice_by_filename,
    split_slice_into_natural_days,
)
from continuous_lfp_rhythm.slice_workflow import (
    filter_records_for_slice,
    load_slice_json,
    resolve_neural_dirs_from_slice_payload,
)


def test_load_slice_json_and_resolve_neural_dirs_from_guibw_source():
    with tempfile.TemporaryDirectory() as tmpdir:
        bw = Path(tmpdir) / "BW10"
        gui = bw / "GUIBW10"
        (gui / "SD_card_files").mkdir(parents=True)
        (bw / "Neural" / "Mode0").mkdir(parents=True)
        (bw / "Neural" / "Mode3").mkdir(parents=True)

        slice_json = bw / "slices.json"
        payload = {
            "version": 1,
            "bw_id": "BW10",
            "source_root": str(gui),
            "timezone": "Asia/Shanghai",
            "slices": [
                {
                    "id": 1,
                    "label": "test",
                    "start_epoch_ms": 1000,
                    "end_epoch_ms": 5000,
                }
            ],
        }
        slice_json.write_text(json.dumps(payload), encoding="utf-8")

        loaded = load_slice_json(slice_json)
        assert loaded["slices"][0]["duration_s"] == 4.0

        resolved = resolve_neural_dirs_from_slice_payload(loaded)
        assert resolved["source_root"] == str(bw.resolve())
        assert resolved["mode0_dir"] == str((bw / "Neural" / "Mode0").resolve())
        assert resolved["mode3_dir"] == str((bw / "Neural" / "Mode3").resolve())


def test_filter_records_for_slice_uses_time_overlap():
    slice_item = {"start_epoch_ms": 1000, "end_epoch_ms": 5000}
    records = [
        {"basename": "before", "start_ts": 0, "end_ts": 999},
        {"basename": "left_overlap", "start_ts": 0, "end_ts": 1001},
        {"basename": "inside", "start_ts": 2000, "end_ts": 3000},
        {"basename": "right_overlap", "start_ts": 4999, "end_ts": 8000},
        {"basename": "after", "start_ts": 5000, "end_ts": 9000},
    ]
    filtered = filter_records_for_slice(records, slice_item)
    assert [item["basename"] for item in filtered] == ["left_overlap", "inside", "right_overlap"]


def test_index_files_is_lightweight_and_reports_discovery_progress():
    with tempfile.TemporaryDirectory() as tmpdir:
        mode0 = Path(tmpdir) / "Neural" / "Mode0"
        mode3 = Path(tmpdir) / "Neural" / "Mode3"
        mode0.mkdir(parents=True)
        mode3.mkdir(parents=True)
        (mode0 / "2026-05-01-00-00-00_lfp.edf").touch()
        (mode3 / "2026-05-01-01-00-00_LFP&ESA.edf").touch()
        (mode3 / "2026-05-01-01-00-00_mode3_raw.edf").touch()

        progress_events = []
        records = index_files(
            mode0_dir=str(mode0),
            mode3_dir=str(mode3),
            progress_callback=lambda *args: progress_events.append(args),
        )

        assert sorted(record["type"] for record in records) == ["mode0_lfp", "mode3_lfp", "mode3_raw"]
        assert all(record["hw_end_ms"] == 0.0 for record in records)
        assert any(event[0] == "discover_done" and event[1] == 3 for event in progress_events)


def test_build_slice_h5_uses_slice_time_origin_and_duration():
    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "version": 1,
            "bw_id": "BW10",
            "source_root": str(Path(tmpdir) / "BW10" / "GUIBW10"),
            "timezone": "Asia/Shanghai",
        }
        slice_item = {
            "id": 3,
            "label": "short_check",
            "start_epoch_ms": 1700000000000,
            "end_epoch_ms": 1700000002000,
            "duration_s": 2.0,
        }
        progress_events = []
        h5_path = build_slice_h5(
            payload,
            slice_item,
            [],
            tmpdir,
            source_slice_json=str(Path(tmpdir) / "slices.json"),
            config={
                "io": {
                    "lfp_sample_rate_hz": 1000,
                    "imu_target_rate_hz": 100,
                    "compression": None,
                }
            },
            progress_callback=lambda *args: progress_events.append(args),
        )

        with h5py.File(h5_path, "r") as f:
            assert f["metadata"].attrs["recording_scope"] == "slice"
            assert f["metadata"].attrs["slice_label"] == "short_check"
            assert f["metadata"].attrs["parent_slice_id"] == 3
            assert f["metadata"].attrs["parent_slice_label"] == "short_check"
            assert f["metadata"].attrs["slice_day_index"] == 0
            assert f["metadata"].attrs["slice_start_epoch_ms"] == 1700000000000.0
            assert f["time"].attrs["time_origin_epoch_ms"] == 1700000000000.0
            assert list(f["time"].attrs["lfp_time_s_extent"]) == [0.0, 2.0]
            assert f["lfp/raw_lfp"].shape == (16, 2000)
            assert f["imu/accel_xyz"].shape == (3, 200)
        assert progress_events[-1][:3] == ("all_rows_complete", 0, 0)


def test_split_slice_into_natural_days_uses_local_day_boundaries():
    shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))

    def epoch_ms(dt):
        return int(round(dt.timestamp() * 1000.0))

    slice_item = {
        "id": 7,
        "label": "long_slice",
        "start_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 1, 12, 0, 0, tzinfo=shanghai_tz)),
        "end_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 3, 6, 0, 0, tzinfo=shanghai_tz)),
    }

    segments = split_slice_into_natural_days(slice_item, timezone_name="Asia/Shanghai")

    assert [segment["date_str"] for segment in segments] == ["2026-05-01", "2026-05-02", "2026-05-03"]
    assert [segment["duration_s"] for segment in segments] == [12 * 3600.0, 24 * 3600.0, 6 * 3600.0]
    assert segments[0]["start_epoch_ms"] == slice_item["start_epoch_ms"]
    assert segments[-1]["end_epoch_ms"] == slice_item["end_epoch_ms"]
    assert all(segment["parent_slice_id"] == 7 for segment in segments)


def test_filename_prefilter_limits_coverage_to_slice_neighborhood():
    shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))

    def epoch_ms(dt):
        return int(round(dt.timestamp() * 1000.0))

    slice_item = {
        "start_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 10, 0, 0, 0, tzinfo=shanghai_tz)),
        "end_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 11, 0, 0, 0, tzinfo=shanghai_tz)),
    }
    records = [
        {"basename": "far_before", "filename_time_ms": epoch_ms(datetime.datetime(2026, 5, 1, 0, 0, 0, tzinfo=shanghai_tz))},
        {"basename": "near_before", "filename_time_ms": epoch_ms(datetime.datetime(2026, 5, 9, 23, 30, 0, tzinfo=shanghai_tz))},
        {"basename": "inside", "filename_time_ms": epoch_ms(datetime.datetime(2026, 5, 10, 12, 0, 0, tzinfo=shanghai_tz))},
        {"basename": "far_after", "filename_time_ms": epoch_ms(datetime.datetime(2026, 5, 20, 0, 0, 0, tzinfo=shanghai_tz))},
        {"basename": "no_timestamp", "filename_time_ms": 0},
    ]

    filtered = prefilter_records_for_slice_by_filename(records, slice_item, margin_ms=3600 * 1000)

    assert [record["basename"] for record in filtered] == ["near_before", "inside", "no_timestamp"]


def test_partition_missing_slice_segments_skips_existing_expected_h5():
    shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))

    def epoch_ms(dt):
        return int(round(dt.timestamp() * 1000.0))

    with tempfile.TemporaryDirectory() as tmpdir:
        payload = {
            "version": 1,
            "bw_id": "BW10",
            "source_root": str(Path(tmpdir) / "BW10" / "GUIBW10"),
            "timezone": "Asia/Shanghai",
        }
        slice_item = {
            "id": 8,
            "label": "slice_001",
            "start_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 1, 12, 0, 0, tzinfo=shanghai_tz)),
            "end_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 3, 6, 0, 0, tzinfo=shanghai_tz)),
        }
        segments = split_slice_into_natural_days(slice_item, timezone_name="Asia/Shanghai")
        existing_path = expected_slice_h5_path(payload, segments[0], tmpdir)
        Path(existing_path).touch()

        missing, existing = partition_missing_slice_segments(payload, segments, tmpdir)

        assert [Path(item["path"]).name for item in existing] == [Path(existing_path).name]
        assert existing[0]["segment"]["date_str"] == "2026-05-01"
        assert [item["segment"]["date_str"] for item in missing] == ["2026-05-02", "2026-05-03"]


def test_partition_missing_slice_segments_uses_existing_paths_outside_output_dir():
    shanghai_tz = datetime.timezone(datetime.timedelta(hours=8))

    def epoch_ms(dt):
        return int(round(dt.timestamp() * 1000.0))

    with tempfile.TemporaryDirectory() as tmpdir:
        output_dir = Path(tmpdir) / "new_output"
        existing_dir = Path(tmpdir) / "outputs" / "BW10"
        output_dir.mkdir()
        existing_dir.mkdir(parents=True)
        payload = {
            "version": 1,
            "bw_id": "BW10",
            "source_root": str(Path(tmpdir) / "BW10" / "GUIBW10"),
            "timezone": "Asia/Shanghai",
        }
        slice_item = {
            "id": 9,
            "label": "slice_001",
            "start_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 1, 0, 0, 0, tzinfo=shanghai_tz)),
            "end_epoch_ms": epoch_ms(datetime.datetime(2026, 5, 2, 0, 0, 0, tzinfo=shanghai_tz)),
        }
        segments = split_slice_into_natural_days(slice_item, timezone_name="Asia/Shanghai")
        expected_name = Path(expected_slice_h5_path(payload, segments[0], output_dir)).name
        existing_path = existing_dir / expected_name
        existing_path.touch()

        missing, existing = partition_missing_slice_segments(
            payload,
            segments,
            output_dir,
            existing_h5_paths=[str(existing_path)],
        )

        assert missing == []
        assert [Path(item["path"]) for item in existing] == [existing_path]


if __name__ == "__main__":
    test_load_slice_json_and_resolve_neural_dirs_from_guibw_source()
    test_filter_records_for_slice_uses_time_overlap()
    test_index_files_is_lightweight_and_reports_discovery_progress()
    test_build_slice_h5_uses_slice_time_origin_and_duration()
    test_split_slice_into_natural_days_uses_local_day_boundaries()
    test_filename_prefilter_limits_coverage_to_slice_neighborhood()
    test_partition_missing_slice_segments_skips_existing_expected_h5()
    test_partition_missing_slice_segments_uses_existing_paths_outside_output_dir()
    print("daily slice workflow tests passed")
