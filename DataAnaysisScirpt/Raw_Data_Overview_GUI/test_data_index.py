import json
import os
import tempfile
from pathlib import Path

import numpy as np

from data_index import (
    DatasetLayoutError,
    build_dataset_index,
    build_slice_export,
    compute_reader_missing_summary,
    load_cached_dataset_index,
    load_saved_analysis_file,
    parse_behavior_trials,
    parse_filename_epoch_ms,
    resolve_dataset_layout,
    scan_edf_records,
    scan_video_records,
    select_missing_channel_index,
)


class FakeEdfReader:
    def __init__(self, data, fs=2.0, labels=None):
        if (
            isinstance(data, (list, tuple))
            and data
            and isinstance(data[0], (list, tuple, np.ndarray))
        ):
            self.channels = [np.asarray(channel, dtype=float) for channel in data]
        else:
            self.channels = [np.asarray(data, dtype=float)]
        self.fs = float(fs)
        self.labels = labels or ["Ch0"] + [f"Ch{idx}" for idx in range(1, len(self.channels))]

    def getSampleFrequency(self, _channel):
        return self.fs

    def getNSamples(self):
        return [channel.size for channel in self.channels]

    def getSignalLabels(self):
        return list(self.labels)

    def readSignal(self, channel, start=0, n=None):
        data = self.channels[int(channel)]
        if n is None:
            return data[int(start):]
        return data[int(start):int(start + n)]

    def close(self):
        return None


def make_layout(root):
    gui = root / "GUIBW01"
    (gui / "SD_card_files").mkdir(parents=True)
    (root / "Neural" / "Mode0").mkdir(parents=True)
    (root / "Neural" / "Mode3").mkdir(parents=True)
    (root / "video").mkdir(parents=True)
    return gui


def test_resolve_bw_and_guibw_roots():
    with tempfile.TemporaryDirectory() as tmpdir:
        bw = Path(tmpdir) / "BW01"
        gui = make_layout(bw)
        from_bw = resolve_dataset_layout(bw)
        from_gui = resolve_dataset_layout(gui)
        assert from_bw.source_root == bw.resolve()
        assert from_bw.gui_root == gui.resolve()
        assert from_gui.source_root == bw.resolve()
        assert from_gui.gui_root == gui.resolve()
        assert from_gui.bw_id == "BW01"
        assert from_gui.neural_dir == (bw / "Neural").resolve()
        assert from_gui.video_dir == (bw / "video").resolve()


def test_missing_required_dirs_raise_clear_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir) / "GUIBW02"
        root.mkdir()
        try:
            resolve_dataset_layout(root)
        except DatasetLayoutError as exc:
            message = str(exc)
            assert "SD_card_files" in message
            assert "Neural" in message
            return
        raise AssertionError("Expected DatasetLayoutError")


def test_parse_behavior_trials_new_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        sd = Path(tmpdir)
        trial_file = sd / "Trial.txt"
        base = [
            "1713000000", "7", "5", "1", "1", "100", "200",
            "0", "1", "3.5", "4.5", "0",
        ]
        metadata = ["10", "87", "1", "0", "1", "2", "3", "0", "4", "1", "255"]
        tail = ["12345", "2", "20", "1000", "2", "2500"]
        trial_file.write_text(" ".join(base + metadata + tail) + "\n", encoding="utf-8")
        trials, warnings = parse_behavior_trials(sd)
        assert warnings == []
        assert len(trials) == 1
        assert trials[0]["time_epoch_ms"] == 1713000000000
        assert trials[0]["protocol_index"] == 5
        assert trials[0]["performance_pct"] == 87.0
        assert trials[0]["trial_choice"] == "left"
        assert trials[0]["outcome_label"] == "correct"


def test_compute_missing_summary_total_fraction():
    reader = FakeEdfReader([0, -1000, 3, 4, -1001], fs=2.0)
    summary = compute_reader_missing_summary(reader, fs=2.0, n_samples=5)
    assert summary["valid_samples"] == 3
    assert summary["missing_samples"] == 2
    assert np.isclose(summary["valid_fraction"], 0.6)
    assert np.isclose(summary["missing_fraction"], 0.4)


def test_select_missing_channel_by_label():
    assert select_missing_channel_index("mode0_lfp", ["ESA0", "Ch_0", "Ch_1"]) == 1
    assert select_missing_channel_index("mode3_lfp_esa", ["ESA0", "Ch 0", "Ch 1"]) == 1
    assert select_missing_channel_index("mode3_raw", ["Alignment", "Raw Data"]) == 1
    assert select_missing_channel_index("mode3_raw", ["Alignment", "RawData"]) == 1


def test_scan_lfp_edf_records_use_ch0_label():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW03")
        edf = gui.parent / "Neural" / "Mode0" / "2026-04-24-12-00-00lfp.edf"
        edf.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)
        records, warnings = scan_edf_records(
            layout,
            reader_factory=lambda _path: FakeEdfReader(
                [[-1000, -1000, -1000, -1000], [1, -1000, 2, 3]],
                fs=2.0,
                labels=["ESA0", "Ch0"],
            ),
        )
        assert warnings == []
        assert len(records) == 1
        assert records[0]["type"] == "mode0_lfp"
        assert records[0]["missing_channel_index"] == 1
        assert records[0]["missing_channel_label"] == "Ch0"
        assert np.isclose(records[0]["valid_fraction"], 0.75)
        assert np.isclose(records[0]["missing_fraction"], 0.25)
        assert records[0]["valid_samples"] == 3
        assert records[0]["missing_samples"] == 1
        assert records[0]["start_epoch_ms"] == parse_filename_epoch_ms(edf.name)


def test_scan_raw_edf_records_use_rawdata_label():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW06")
        edf = gui.parent / "Neural" / "Mode3" / "2026-04-24-12-00-00mode3_raw.edf"
        edf.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)
        records, warnings = scan_edf_records(
            layout,
            reader_factory=lambda _path: FakeEdfReader(
                [[-1000, -1000, -1000, -1000], [1, 2, -1000, 4]],
                fs=2.0,
                labels=["Alignment", "RawData"],
            ),
        )
        assert warnings == []
        assert len(records) == 1
        assert records[0]["type"] == "mode3_raw"
        assert records[0]["missing_channel_index"] == 1
        assert records[0]["missing_channel_label"] == "RawData"
        assert np.isclose(records[0]["valid_fraction"], 0.75)
        assert np.isclose(records[0]["missing_fraction"], 0.25)


def test_scan_edf_records_warn_when_expected_label_missing():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW07")
        edf = gui.parent / "Neural" / "Mode0" / "2026-04-24-12-00-00lfp.edf"
        edf.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)
        records, warnings = scan_edf_records(
            layout,
            reader_factory=lambda _path: FakeEdfReader(
                [[1, 2, 3, 4]],
                fs=2.0,
                labels=["NotCh0"],
            ),
        )
        assert records == []
        assert len(warnings) == 1
        assert "Missing Ch0-like channel" in warnings[0]


def test_scan_video_records_with_mock_duration():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW04")
        video = gui.parent / "video" / "2026-04-24-12-00-00front.mp4"
        video.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)
        records, warnings = scan_video_records(
            layout,
            duration_reader=lambda _path, ffprobe_path="ffprobe": 12.5,
        )
        assert warnings == []
        assert len(records) == 1
        assert records[0]["duration_s"] == 12.5
        assert records[0]["end_epoch_ms"] - records[0]["start_epoch_ms"] == 12500


def test_scan_video_records_uses_parent_timestamp():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW08")
        video_dir = gui.parent / "video" / "2026-04-24-12-00-00"
        video_dir.mkdir(parents=True)
        video = video_dir / "front.mp4"
        video.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)
        records, warnings = scan_video_records(
            layout,
            duration_reader=lambda _path, ffprobe_path="ffprobe": 8.0,
        )
        assert warnings == []
        assert len(records) == 1
        assert records[0]["start_epoch_ms"] == parse_filename_epoch_ms(str(video))
        assert records[0]["duration_known"] is True


def test_scan_video_records_ffprobe_missing_warns_once():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW05")
        for idx in range(2):
            video = gui.parent / "video" / f"2026-04-24-12-00-0{idx}front.mp4"
            video.write_text("placeholder", encoding="utf-8")
        layout = resolve_dataset_layout(gui)

        def missing(_path, ffprobe_path="ffprobe"):
            raise FileNotFoundError("ffprobe not found")

        records, warnings = scan_video_records(layout, duration_reader=missing)
        assert len(records) == 2
        assert len(warnings) == 1
        assert records[0]["duration_known"] is False


def test_build_dataset_index_reuses_saved_analysis_when_sources_unchanged():
    with tempfile.TemporaryDirectory() as tmpdir:
        gui = make_layout(Path(tmpdir) / "BW09")
        trial_file = gui / "SD_card_files" / "Trial.txt"
        trial_file.write_text(
            "1713000000 7 5 1 1 100 200 0 1 3.5 4.5 0 10 87 1 0 1 2 3 0 4 1 255 12345 2 20 1000 2 2500\n",
            encoding="utf-8",
        )
        edf = gui.parent / "Neural" / "Mode0" / "2026-04-24-12-00-00lfp.edf"
        edf.write_text("placeholder", encoding="utf-8")

        first = build_dataset_index(
            gui,
            reader_factory=lambda _path: FakeEdfReader([1, -1000, 2, 3], fs=2.0, labels=["Ch0"]),
            duration_reader=lambda _path, ffprobe_path="ffprobe": 1.0,
            use_cache=True,
        )
        assert len(first["edf_records"]) == 1
        manual = load_saved_analysis_file(first["analysis_cache_paths"][0])
        assert len(manual["edf_records"]) == 1
        assert manual["loaded_analysis_path"] == first["analysis_cache_paths"][0]
        legacy = dict(first)
        legacy["version"] = 4
        legacy.pop("edf_missing_metric", None)
        legacy.pop("source_signature", None)
        legacy_path = Path(tmpdir) / "legacy_v4_index_cache.json"
        legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
        legacy_loaded = load_saved_analysis_file(legacy_path)
        assert legacy_loaded["version"] == 5
        assert legacy_loaded["original_cache_version"] == 4
        assert len(legacy_loaded["edf_records"]) == 1
        assert legacy_loaded["edf_missing_metric"] == "per_file_total_fraction_named_channel"

        def fail_reader(_path):
            raise AssertionError("EDF reader should not be called when saved analysis is valid")

        second = build_dataset_index(
            gui,
            reader_factory=fail_reader,
            duration_reader=lambda _path, ffprobe_path="ffprobe": 1.0,
            use_cache=True,
        )
        assert len(second["edf_records"]) == 1
        assert any("Loaded saved analysis" in line for line in second.get("logs", []))

        cached, signature, loaded_from = load_cached_dataset_index(
            gui,
            duration_reader=lambda _path, ffprobe_path="ffprobe": 1.0,
        )
        assert cached is not None
        assert loaded_from
        assert signature["counts"]["edf"] == 1

        os.utime(edf, (2000000000, 2000000000))
        third = build_dataset_index(
            gui,
            reader_factory=fail_reader,
            duration_reader=lambda _path, ffprobe_path="ffprobe": 1.0,
            use_cache=True,
        )
        assert len(third["edf_records"]) == 1
        assert any("file-count match" in line for line in third.get("logs", []))


def test_slice_export_schema_and_sorting():
    index = {
        "bw_id": "BW01",
        "source_root": "/data/BW01/GUIBW01",
        "timezone": "Asia/Shanghai",
        "global_start_iso": "2026-04-24T00:00:00+08:00",
        "global_end_iso": "2026-04-25T00:00:00+08:00",
    }
    payload = build_slice_export(
        index,
        [
            {"label": "late", "start_epoch_ms": 2000, "end_epoch_ms": 3000},
            {"label": "early", "start_epoch_ms": 1000, "end_epoch_ms": 1500},
        ],
    )
    assert payload["version"] == 1
    assert payload["slices"][0]["label"] == "early"
    assert payload["slices"][0]["duration_s"] == 0.5
    json.dumps(payload, ensure_ascii=False)


if __name__ == "__main__":
    test_resolve_bw_and_guibw_roots()
    test_missing_required_dirs_raise_clear_error()
    test_parse_behavior_trials_new_format()
    test_compute_missing_summary_total_fraction()
    test_select_missing_channel_by_label()
    test_scan_lfp_edf_records_use_ch0_label()
    test_scan_raw_edf_records_use_rawdata_label()
    test_scan_edf_records_warn_when_expected_label_missing()
    test_scan_video_records_with_mock_duration()
    test_scan_video_records_uses_parent_timestamp()
    test_scan_video_records_ffprobe_missing_warns_once()
    test_build_dataset_index_reuses_saved_analysis_when_sources_unchanged()
    test_slice_export_schema_and_sorting()
    print("raw data overview index tests passed")
