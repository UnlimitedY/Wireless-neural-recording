import datetime
import csv
import json
import multiprocessing
import queue
import os
import sys
import webbrowser

import h5py
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from scipy import signal

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from Common_Analysis.behavior_analysis import (
    BIAS_STATE_LABELS,
    FILL_SOURCE_LABELS,
    PERF_STATE_LABELS,
    RULE_LABELS,
    filter_behavior_trials_for_slice,
    parse_behavior_trials,
    resolve_sd_card_dir_from_source_root,
)
from Common_Analysis.behavior_rt import RT_CLASS_LABELS, compute_hourly_rt_features, parse_rt_trials

from .config import resolve_config
from .connectivity_features import load_connectivity_sidecar
from .direct_behavior_statistics import (
    expected_direct_behavior_statistics_summary_path,
    run_direct_behavior_state_statistics,
)
from .features import extract_features
from .file_index import generate_file_coverage, index_files
from .h5_schema import build_daily_h5, build_slice_h5, slice_h5_basename
from .longitudinal_dynamics import run_longitudinal_dynamics_analysis
from .paired_trial_context import (
    expected_paired_trial_context_summary_path,
    run_paired_trial_context_analysis,
)
from .qt_async import run_callable_in_thread
from .reports import generate_html_report
from .rhythm_stats import summarize_rhythm
from .slice_workflow import (
    filter_records_for_slice,
    format_slice_option,
    load_slice_json,
    resolve_neural_dirs_from_slice_payload,
)
from .slice_background_builder import build_natural_day_slice_h5s_worker, split_slice_into_natural_days
from .slice_statistics import expected_slice_statistics_summary_path, run_slice_statistical_analysis
from .state_classification import STATE_CODES, classify_states
from .state_review_wizard import run_state_review_wizard
from .timeline import group_by_day
from .tz_compat import ZoneInfo

pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")
pg.setConfigOptions(antialias=True)
try:
    pg.setConfigOption("imageAxisOrder", "row-major")
except Exception:
    pass


def _build_fallback_lut(color_stops, n=256):
    positions = np.linspace(0.0, 1.0, len(color_stops))
    cmap = pg.ColorMap(positions, color_stops)
    return cmap.getLookupTable(nPts=n)


def _get_lookup_table(name, fallback_colors):
    colormap_module = getattr(pg, "colormap", None)
    if colormap_module is not None:
        try:
            cmap = colormap_module.get(name)
            if cmap is not None:
                return cmap.getLookupTable()
        except Exception:
            pass
    return _build_fallback_lut(fallback_colors)


def _is_hdf5_lock_error(exc):
    text = str(exc).lower()
    lock_markers = [
        "unable to lock file",
        "win32 getlasterror() = 33",
        "resource temporarily unavailable",
        "errno = 11",
        "file is already open",
    ]
    return any(marker in text for marker in lock_markers)


def _make_image_item(image, lut, levels=None):
    try:
        item = pg.ImageItem(axisOrder="row-major")
    except TypeError:
        item = pg.ImageItem()
    image = np.asarray(image, dtype=np.float32)
    try:
        item.setImage(image, autoLevels=False)
    except TypeError:
        item.setImage(image)
    item.setLookupTable(lut)
    if levels is not None:
        try:
            item.setLevels(levels)
        except Exception:
            pass
    return item


def _make_masked_image_item(image, lut, levels=None):
    image = np.asarray(image, dtype=np.float64)
    finite_mask = np.isfinite(image)
    if levels is None:
        levels = _robust_levels(image, default=(0.0, 1.0))
    if levels is None:
        levels = (0.0, 1.0)
    low, high = [float(v) for v in levels]
    if not np.isfinite(low) or not np.isfinite(high) or high <= low:
        high = low + 1.0

    lut_array = np.asarray(lut)
    if lut_array.ndim != 2 or lut_array.shape[0] == 0 or lut_array.shape[1] < 3:
        return _make_image_item(np.where(finite_mask, image, np.nan), lut, levels=levels)
    if np.issubdtype(lut_array.dtype, np.floating) and np.nanmax(lut_array) <= 1.0:
        lut_array = lut_array * 255.0
    lut_array = np.clip(lut_array, 0, 255).astype(np.uint8)

    normalized = np.zeros_like(image, dtype=np.float64)
    normalized[finite_mask] = np.clip((image[finite_mask] - low) / (high - low), 0.0, 1.0)
    lut_index = np.rint(normalized * (lut_array.shape[0] - 1)).astype(np.int32)

    rgba = np.zeros(image.shape + (4,), dtype=np.uint8)
    rgba[..., :3] = lut_array[lut_index, :3]
    if lut_array.shape[1] >= 4:
        rgba[..., 3] = lut_array[lut_index, 3]
    else:
        rgba[..., 3] = 255
    rgba[..., 3] = np.where(finite_mask, rgba[..., 3], 0).astype(np.uint8)

    try:
        item = pg.ImageItem(axisOrder="row-major")
    except TypeError:
        item = pg.ImageItem()
    try:
        item.setImage(rgba, autoLevels=False)
    except TypeError:
        item.setImage(rgba)
    return item


def _add_masked_heatmap_items(plot_widget, image, x_hours, lut, levels=None, expected_step=None):
    image = np.asarray(image, dtype=np.float64)
    x_hours = np.asarray(x_hours, dtype=np.float64)
    if image.ndim != 2 or x_hours.ndim != 1 or image.shape[1] != x_hours.size:
        return 0
    finite_x = np.isfinite(x_hours)
    if not np.all(finite_x):
        image = image[:, finite_x]
        x_hours = x_hours[finite_x]
    if x_hours.size == 0:
        return 0

    diffs = np.diff(x_hours)
    positive_diffs = diffs[np.isfinite(diffs) & (diffs > 0)]
    if expected_step is None:
        expected_step = float(np.nanmin(positive_diffs)) if positive_diffs.size else 1.0
    expected_step = max(float(expected_step), 1e-6)
    split_points = np.where(diffs > expected_step * 1.5)[0] + 1

    added = 0
    start = 0
    for stop in list(split_points) + [x_hours.size]:
        segment_x = x_hours[start:stop]
        segment_image = image[:, start:stop]
        start = stop
        if segment_x.size == 0 or not np.isfinite(segment_image).any():
            continue
        segment_diffs = np.diff(segment_x)
        segment_positive_diffs = segment_diffs[np.isfinite(segment_diffs) & (segment_diffs > 0)]
        step = float(np.nanmin(segment_positive_diffs)) if segment_positive_diffs.size else expected_step
        step = max(step, 1e-6)
        x0 = float(segment_x[0] - step / 2.0)
        x1 = float(segment_x[-1] + step / 2.0)
        image_item = _make_masked_image_item(segment_image, lut, levels=levels)
        image_item.setRect(
            QRectF(
                x0,
                -0.5,
                max(x1 - x0, 1e-6),
                max(segment_image.shape[0], 1),
            )
        )
        plot_widget.addItem(image_item)
        added += 1
    return added


def _robust_levels(image, default=None):
    finite = np.asarray(image, dtype=np.float64)[np.isfinite(image)]
    if finite.size == 0:
        return default
    low, high = np.nanpercentile(finite, [5.0, 95.0])
    if not np.isfinite(low) or not np.isfinite(high):
        return default
    if high <= low:
        center = float(low)
        span = max(1e-3, abs(center) * 0.1)
        return (center - span, center + span)
    return (float(low), float(high))


def _decode_scalar_string(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.ndarray) and value.shape == ():
        return _decode_scalar_string(value.item())
    return str(value)


def _make_vertical_region(values, movable=False, brush=None, pen=None):
    try:
        return pg.LinearRegionItem(
            values=values,
            orientation="vertical",
            movable=movable,
            brush=brush,
            pen=pen,
        )
    except TypeError:
        pass
    orientation_enum = getattr(getattr(pg.LinearRegionItem, "Orientation", None), "Vertical", None)
    if orientation_enum is not None:
        return pg.LinearRegionItem(
            values=values,
            orientation=orientation_enum,
            movable=movable,
            brush=brush,
            pen=pen,
        )
    item = pg.LinearRegionItem(values=values, movable=movable, brush=brush, pen=pen)
    try:
        item.setOrientation("vertical")
    except Exception:
        pass
    return item


class TimeAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.date_str = "2000-01-01"
        self.timezone_name = "Asia/Shanghai"
        self.origin_epoch_ms = None

    def set_date(self, date_str):
        self.date_str = date_str
        self.origin_epoch_ms = None

    def set_time_origin(self, origin_epoch_ms, timezone_name="Asia/Shanghai", date_str=None):
        self.origin_epoch_ms = float(origin_epoch_ms) if origin_epoch_ms is not None else None
        self.timezone_name = timezone_name or "Asia/Shanghai"
        if date_str:
            self.date_str = str(date_str)

    def tickStrings(self, values, scale, spacing):
        strings = []
        tz = None
        if self.origin_epoch_ms is not None:
            try:
                tz = ZoneInfo(self.timezone_name)
            except Exception:
                tz = datetime.timezone.utc
        else:
            try:
                base_dt = datetime.datetime.strptime(self.date_str, "%Y-%m-%d")
            except Exception:
                base_dt = datetime.datetime(2000, 1, 1)
        for value in values:
            try:
                if self.origin_epoch_ms is not None:
                    dt = datetime.datetime.fromtimestamp(
                        (self.origin_epoch_ms / 1000.0) + float(value),
                        tz=datetime.timezone.utc,
                    ).astimezone(tz)
                else:
                    dt = base_dt + datetime.timedelta(seconds=float(value))
                strings.append(dt.strftime("%H:%M:%S"))
            except Exception:
                strings.append("")
        return strings


class SummaryHourAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.origin_epoch_ms = None
        self.timezone_name = "Asia/Shanghai"

    def set_time_origin(self, origin_epoch_ms, timezone_name="Asia/Shanghai"):
        self.origin_epoch_ms = float(origin_epoch_ms) if origin_epoch_ms is not None else None
        self.timezone_name = timezone_name or "Asia/Shanghai"

    def tickStrings(self, values, scale, spacing):
        strings = []
        try:
            tz = ZoneInfo(self.timezone_name)
        except Exception:
            tz = datetime.timezone.utc
        for value in values:
            try:
                if self.origin_epoch_ms is None or not np.isfinite(float(self.origin_epoch_ms)):
                    strings.append(f"{float(value):.1f}h")
                    continue
                dt = datetime.datetime.fromtimestamp(
                    (float(self.origin_epoch_ms) / 1000.0) + float(value) * 3600.0,
                    tz=datetime.timezone.utc,
                ).astimezone(tz)
                if spacing >= 22:
                    strings.append(dt.strftime("%m-%d"))
                elif spacing >= 1:
                    strings.append(dt.strftime("%m-%d\n%H:00"))
                else:
                    strings.append(dt.strftime("%m-%d\n%H:%M"))
            except Exception:
                strings.append("")
        return strings


class H5ViewerApp(QMainWindow):
    def __init__(self, default_h5_file=None, default_config=None):
        super().__init__()
        self.setWindowTitle("Continuous LFP Rhythm - Unified GUI")
        self.resize(1680, 980)

        self.h5_path = default_h5_file
        self.config_path = default_config
        self.report_path = None
        self.h5_file = None
        self.trial_dict = {}
        self.slice_payload = None
        self.slice_json_path = ""
        self.slice_build_process = None
        self.slice_build_queue = None
        self.slice_build_poll_timer = QTimer(self)
        self.slice_build_poll_timer.timeout.connect(self._poll_slice_build_queue)
        self.slice_build_first_loaded = False
        self.slice_build_running = False
        self.h5_operation_running = False
        self.auto_analyze_after_slice_build = True
        self.auto_h5_analysis_queue = []
        self.auto_h5_analysis_running = False
        self.auto_h5_analysis_paused = False
        self.daily_start_ts_ms = 0.0
        self.current_timezone_name = "Asia/Shanghai"
        self.summary_time_origin_epoch_ms = None
        self.spectrogram_global_level_cache = {}
        self.spectrogram_freq_min_hz = 1.0
        self.spectrogram_freq_options_hz = [20, 50, 100, 150, 250]
        self.slice_statistics_summary_path = ""
        self.slice_statistics_top_rows = []
        self.slice_statistics_hourly_rows = []
        self.direct_behavior_summary_path = ""
        self.direct_behavior_all_rows = []
        self.direct_behavior_top_rows = []
        self.direct_behavior_pairwise_rows = []
        self.direct_behavior_hourly_rows = []
        self.direct_distribution_plots = []
        self.paired_context_summary_path = ""
        self.paired_context_top_rows = []
        self.paired_context_trial_rows = []

        self.lfp_fs = 1000
        self.imu_fs = 100
        self.total_seconds = 86400
        self.channel_offset = 500

        self.lfp_channels_visible = [True] * 16
        self.imu_channels_visible = [True] * 3

        self.update_timer = QTimer()
        self.update_timer.setSingleShot(True)
        self.update_timer.timeout.connect(self._fetch_and_draw_detail)

        self._init_ui()
        self._apply_default_paths()

        if self.h5_path and os.path.exists(self.h5_path):
            self.load_h5(self.h5_path)

    def _normalize_path(self, path, base_dir=None):
        if path is None:
            return ""
        text = str(path).strip()
        if not text:
            return ""
        expanded = os.path.expanduser(text)
        if os.path.isabs(expanded):
            return os.path.abspath(expanded)
        root = base_dir or os.getcwd()
        return os.path.abspath(os.path.join(root, expanded))

    def _config_dir(self):
        config_path = self.config_edit.text().strip() if hasattr(self, "config_edit") else (self.config_path or "")
        if config_path:
            normalized = self._normalize_path(config_path)
            if normalized:
                return os.path.dirname(normalized)
        return os.getcwd()

    def _normalize_dir_field(self, line_edit, base_dir=None):
        raw = line_edit.text().strip()
        if not raw:
            return ""
        normalized = self._normalize_path(raw, base_dir=base_dir)
        if normalized != raw:
            line_edit.setText(normalized)
        return normalized

    def _safe_existing_directory(self, path, fallback=None):
        candidate = self._normalize_path(path, base_dir=self._config_dir()) if path else ""
        if candidate and os.path.isdir(candidate):
            return candidate
        if candidate and os.path.isfile(candidate):
            parent = os.path.dirname(candidate)
            if os.path.isdir(parent):
                return parent
        fb = self._normalize_path(fallback) if fallback else ""
        if fb and os.path.isdir(fb):
            return fb
        return os.getcwd()

    def _resolve_output_dir(self, allow_create=False):
        cfg = self._get_config()
        output_dir = (
            self._normalize_dir_field(self.output_edit, base_dir=self._config_dir())
            or self._normalize_path(cfg.get("general.output_dir", ""), base_dir=self._config_dir())
            or os.getcwd()
        )
        if os.path.exists(output_dir):
            if not os.path.isdir(output_dir):
                raise ValueError(f"Output Dir is not a directory: {output_dir}")
            return output_dir

        parent_dir = os.path.dirname(output_dir) or os.getcwd()
        if not os.path.isdir(parent_dir):
            raise ValueError(f"Output Dir parent does not exist: {parent_dir}")

        if allow_create:
            os.makedirs(output_dir, exist_ok=True)
            return output_dir

        return output_dir

    def _iter_h5_list_paths(self):
        if not hasattr(self, "h5_list"):
            return []
        paths = []
        for idx in range(self.h5_list.count()):
            item = self.h5_list.item(idx)
            if item is None:
                continue
            path = item.data(Qt.ItemDataRole.UserRole)
            if path:
                paths.append(str(path))
        return paths

    def _scan_matching_h5_paths_in_dir(self, directory, expected_names):
        if not directory or not os.path.isdir(directory):
            return []
        matches = []
        for dirpath, _, filenames in os.walk(directory):
            for fname in filenames:
                if fname in expected_names:
                    matches.append(os.path.join(dirpath, fname))
        return matches

    def _resolve_slice_build_output_dir_and_existing_paths(self, slice_payload, slice_item, output_dir, cfg):
        timezone_name = slice_payload.get("timezone") or cfg.get("general.timezone", "Asia/Shanghai")
        segments = split_slice_into_natural_days(slice_item, timezone_name=timezone_name)
        expected_names = {
            slice_h5_basename(slice_payload, segment, config=cfg)
            for segment in segments
        }
        if not expected_names:
            return output_dir, []

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bw_id = str(slice_payload.get("bw_id") or cfg.get("general.animal_id", "") or "").strip()
        candidate_dirs = []

        def add_candidate(path):
            normalized = self._normalize_path(path, base_dir=self._config_dir()) if path else ""
            if normalized and normalized not in candidate_dirs:
                candidate_dirs.append(normalized)

        add_candidate(output_dir)
        add_candidate(cfg.get("general.output_dir", ""))
        if bw_id:
            add_candidate(os.path.join(base_dir, "outputs", bw_id))
        add_candidate(os.path.join(base_dir, "outputs"))

        existing_paths = []
        for path in self._iter_h5_list_paths():
            if os.path.basename(path) in expected_names and os.path.exists(path):
                existing_paths.append(os.path.abspath(path))
                add_candidate(os.path.dirname(path))

        for directory in list(candidate_dirs):
            existing_paths.extend(self._scan_matching_h5_paths_in_dir(directory, expected_names))

        unique_existing = []
        seen_paths = set()
        for path in existing_paths:
            normalized = os.path.abspath(path)
            if normalized not in seen_paths and os.path.basename(normalized) in expected_names and os.path.exists(normalized):
                seen_paths.add(normalized)
                unique_existing.append(normalized)

        current_dir = os.path.abspath(output_dir)
        current_matches = sum(1 for path in unique_existing if os.path.dirname(path) == current_dir)
        dir_counts = {}
        for path in unique_existing:
            parent = os.path.dirname(path)
            dir_counts[parent] = dir_counts.get(parent, 0) + 1
        best_dir = current_dir
        best_count = current_matches
        for directory, count in sorted(dir_counts.items(), key=lambda item: (-item[1], item[0])):
            if count > best_count:
                best_dir = directory
                best_count = count
                break

        if best_dir != current_dir:
            self.append_log(
                f"Existing slice H5 files were found in {best_dir}; using that output dir "
                f"for skip/build ({best_count} matching files)."
            )
            output_dir = best_dir
            self.output_edit.setText(output_dir)
        elif unique_existing:
            self.append_log(f"Found {len(unique_existing)} existing natural-day H5 files for this slice.")

        return output_dir, unique_existing

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        root_layout = QVBoxLayout(main_widget)
        root_layout.setContentsMargins(6, 6, 6, 6)
        root_layout.setSpacing(6)

        root_layout.addWidget(self._build_progress_group())

        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        root_layout.addLayout(layout, 1)

        left_panel = QWidget()
        left_panel.setFixedWidth(430)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_widget = QWidget()
        controls_layout = QVBoxLayout(controls_widget)
        controls_layout.addWidget(self._build_paths_group())
        controls_layout.addWidget(self._build_workflow_group())
        controls_layout.addWidget(self._build_files_group())
        controls_layout.addWidget(self._build_channels_group())
        controls_layout.addWidget(self._build_info_group())
        controls_layout.addStretch()
        controls_scroll.setWidget(controls_widget)
        left_layout.addWidget(controls_scroll)

        self.right_tabs = QTabWidget()
        self.right_tabs.addTab(self._build_detail_tab(), "Detail View")
        self.right_tabs.addTab(self._build_summary_tab(), "Recording Summary")
        self.right_tabs.addTab(self._build_slice_statistics_tab(), "Slice Statistics")
        self.right_tabs.addTab(self._build_direct_behavior_stats_tab(), "Direct Behavior Stats")
        self.right_tabs.addTab(self._build_paired_context_tab(), "Paired Trial Context")

        layout.addWidget(left_panel)
        layout.addWidget(self.right_tabs)

        self.statusBar = QStatusBar()
        self.setStatusBar(self.statusBar)

    def _build_progress_group(self):
        widget = QWidget()
        layout = QGridLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(2)

        build_title = QLabel("Build Slice")
        build_title.setStyleSheet("font-weight: bold;")
        self.build_progress_label = QLabel("Idle")
        self.build_progress_bar = QProgressBar()
        self._configure_progress_bar(self.build_progress_bar)

        op_title = QLabel("Current H5")
        op_title.setStyleSheet("font-weight: bold;")
        self.progress_label = QLabel("Idle")
        self.progress_bar = QProgressBar()
        self._configure_progress_bar(self.progress_bar)

        layout.addWidget(build_title, 0, 0)
        layout.addWidget(self.build_progress_label, 0, 1)
        layout.addWidget(self.build_progress_bar, 0, 2)
        layout.addWidget(op_title, 1, 0)
        layout.addWidget(self.progress_label, 1, 1)
        layout.addWidget(self.progress_bar, 1, 2)
        layout.setColumnStretch(1, 1)
        layout.setColumnStretch(2, 2)
        return widget

    def _configure_progress_bar(self, bar):
        bar.setMinimum(0)
        bar.setMaximum(100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFixedHeight(18)

    def _process_ui_events(self):
        QApplication.processEvents()

    def _set_progress(self, label, bar, text=None, value=None, maximum=None, busy=None):
        if text is not None:
            label.setText(str(text))
        if busy is not None:
            if busy:
                bar.setRange(0, 0)
            else:
                max_value = int(maximum) if maximum is not None else max(bar.maximum(), 1)
                bar.setRange(0, max_value)
        elif maximum is not None and bar.maximum() != 0:
            bar.setRange(bar.minimum(), int(maximum))
        if value is not None and bar.maximum() != 0:
            bar.setValue(int(value))
        self._process_ui_events()

    def _start_progress(self, text, busy=True, maximum=100, value=0):
        self.progress_label.setText(str(text))
        if busy:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, int(maximum))
            self.progress_bar.setValue(int(value))
        self._process_ui_events()

    def _update_progress(self, text=None, value=None, maximum=None, busy=None):
        self._set_progress(self.progress_label, self.progress_bar, text=text, value=value, maximum=maximum, busy=busy)

    def _finish_progress(self, text="Idle"):
        self.progress_label.setText(str(text))
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self._process_ui_events()

    def _start_build_progress(self, text, busy=True, maximum=100, value=0):
        self.build_progress_label.setText(str(text))
        if busy:
            self.build_progress_bar.setRange(0, 0)
        else:
            self.build_progress_bar.setRange(0, int(maximum))
            self.build_progress_bar.setValue(int(value))
        self._process_ui_events()

    def _update_build_progress(self, text=None, value=None, maximum=None, busy=None):
        self._set_progress(
            self.build_progress_label,
            self.build_progress_bar,
            text=text,
            value=value,
            maximum=maximum,
            busy=busy,
        )

    def _finish_build_progress(self, text="Idle"):
        self.build_progress_label.setText(str(text))
        self.build_progress_bar.setRange(0, 100)
        self.build_progress_bar.setValue(0)
        self._process_ui_events()

    def _run_background_callable(self, text, func):
        self._update_progress(text=text, busy=True)
        return run_callable_in_thread(func)

    def _run_build_background_callable(self, text, func):
        self._update_build_progress(text=text, busy=True)
        return run_callable_in_thread(func)

    def _build_detail_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(QLabel("Spectrogram Range"))
        self.spectrogram_freq_combo = QComboBox()
        for max_hz in self.spectrogram_freq_options_hz:
            self.spectrogram_freq_combo.addItem(f"1-{int(max_hz)} Hz", float(max_hz))
        default_max_hz = 100.0
        default_index = next(
            (idx for idx, max_hz in enumerate(self.spectrogram_freq_options_hz) if float(max_hz) == default_max_hz),
            0,
        )
        self.spectrogram_freq_combo.setCurrentIndex(default_index)
        self.spectrogram_freq_combo.currentIndexChanged.connect(self._on_spectrogram_freq_changed)
        controls_layout.addWidget(self.spectrogram_freq_combo)
        controls_layout.addStretch()
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_overview_plot())
        splitter.addWidget(self._build_lfp_plot())
        splitter.addWidget(self._build_spectrogram_plot())
        splitter.addWidget(self._build_imu_plot())
        splitter.addWidget(self._build_masks_plot())
        splitter.setSizes([160, 320, 240, 180, 110])
        layout.addWidget(splitter)
        return widget

    def _build_summary_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(QLabel("Band"))
        self.summary_band_combo = QComboBox()
        self.summary_band_combo.currentIndexChanged.connect(lambda _: self._render_rhythm_summary())
        controls_layout.addWidget(self.summary_band_combo)
        controls_layout.addWidget(QLabel("Channel Group"))
        self.summary_group_combo = QComboBox()
        self.summary_group_combo.currentIndexChanged.connect(lambda _: self._render_rhythm_summary())
        controls_layout.addWidget(self.summary_group_combo)
        controls_layout.addWidget(QLabel("Reference"))
        self.summary_reference_combo = QComboBox()
        self.summary_reference_combo.addItem("Daily mean", "daily")
        self.summary_reference_combo.addItem("Whole-slice mean", "slice")
        self.summary_reference_combo.setToolTip(
            "Daily mean uses each natural-day H5 as its own reference. "
            "Whole-slice mean uses all summarized H5 files as one reference pool."
        )
        self.summary_reference_combo.currentIndexChanged.connect(lambda _: self._render_rhythm_summary())
        controls_layout.addWidget(self.summary_reference_combo)
        controls_layout.addStretch()
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._build_hourly_band_plot())
        splitter.addWidget(self._build_state_normalized_plot())
        splitter.addWidget(self._build_state_occupancy_plot())
        splitter.addWidget(self._build_behavior_state_plot())
        splitter.addWidget(self._build_connectivity_summary_plot())
        splitter.addWidget(self._build_summary_info_widget())
        splitter.setSizes([240, 300, 190, 200, 190, 170])
        layout.addWidget(splitter)
        return widget

    def _build_slice_statistics_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(QLabel("Reference"))
        self.stats_reference_combo = QComboBox()
        self.stats_reference_combo.addItem("Daily mean", "daily")
        self.stats_reference_combo.addItem("Whole-slice mean", "whole_slice")
        controls_layout.addWidget(self.stats_reference_combo)
        controls_layout.addWidget(QLabel("Permutations"))
        self.stats_permutation_spin = QSpinBox()
        self.stats_permutation_spin.setRange(0, 10000)
        self.stats_permutation_spin.setSingleStep(100)
        self.stats_permutation_spin.setValue(1000)
        controls_layout.addWidget(self.stats_permutation_spin)
        run_button = QPushButton("Run Slice Statistics")
        run_button.clicked.connect(lambda: self._run_slice_statistics_when_ready(auto=False))
        controls_layout.addWidget(run_button)
        reload_button = QPushButton("Reload Results")
        reload_button.clicked.connect(self.reload_slice_statistics_results)
        controls_layout.addWidget(reload_button)
        open_button = QPushButton("Open Output Dir")
        open_button.clicked.connect(self.open_slice_statistics_output_dir)
        controls_layout.addWidget(open_button)
        controls_layout.addStretch()
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.stats_readiness_txt = QTextEdit()
        self.stats_readiness_txt.setReadOnly(True)
        self.stats_readiness_txt.setPlaceholderText("Same-slice H5 readiness and slice-level statistics status.")
        self.stats_table = QTableWidget(0, 8)
        self.stats_table.setHorizontalHeaderLabels(
            ["target", "family", "delta R2", "p", "q", "top term", "sign", "n"]
        )
        self.stats_table.itemSelectionChanged.connect(self._render_slice_statistics_selected_target)
        top_splitter.addWidget(self.stats_readiness_txt)
        top_splitter.addWidget(self.stats_table)
        top_splitter.setSizes([420, 720])
        splitter.addWidget(top_splitter)

        plot_splitter = QSplitter(Qt.Orientation.Vertical)
        self.stats_effect_plot = pg.PlotWidget(title="Slice Statistics Effect Ranking")
        self.stats_effect_plot.showGrid(x=True, y=True, alpha=0.25)
        self.stats_heatmap_plot = pg.PlotWidget(title="Top Target Day x ZT Heatmap")
        self.stats_heatmap_plot.setLabel("bottom", "ZT hour")
        self.stats_heatmap_plot.setLabel("left", "Elapsed day")
        self.stats_overlay_plot = pg.PlotWidget(title="Selected Target + Behavior Overlay")
        self.stats_overlay_plot.showGrid(x=True, y=True, alpha=0.25)
        plot_splitter.addWidget(self.stats_effect_plot)
        plot_splitter.addWidget(self.stats_heatmap_plot)
        plot_splitter.addWidget(self.stats_overlay_plot)
        plot_splitter.setSizes([240, 260, 240])
        splitter.addWidget(plot_splitter)
        splitter.setSizes([230, 740])
        layout.addWidget(splitter)
        return widget

    def _build_direct_behavior_stats_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(QLabel("Reference"))
        self.direct_reference_combo = QComboBox()
        self.direct_reference_combo.addItem("Daily mean", "daily")
        self.direct_reference_combo.addItem("Whole-slice mean", "whole_slice")
        controls_layout.addWidget(self.direct_reference_combo)
        controls_layout.addWidget(QLabel("Permutations"))
        self.direct_permutation_spin = QSpinBox()
        self.direct_permutation_spin.setRange(0, 10000)
        self.direct_permutation_spin.setSingleStep(100)
        self.direct_permutation_spin.setValue(1000)
        controls_layout.addWidget(self.direct_permutation_spin)
        run_button = QPushButton("Run Direct Stats")
        run_button.clicked.connect(self.run_direct_behavior_stats)
        controls_layout.addWidget(run_button)
        reload_button = QPushButton("Reload Results")
        reload_button.clicked.connect(self.reload_direct_behavior_stats_results)
        controls_layout.addWidget(reload_button)
        open_button = QPushButton("Open Output Dir")
        open_button.clicked.connect(self.open_direct_behavior_stats_output_dir)
        controls_layout.addWidget(open_button)
        controls_layout.addStretch()
        layout.addWidget(controls)

        filter_group = QGroupBox("Filters")
        filter_group.setMinimumHeight(74)
        filters_layout = QGridLayout(filter_group)
        filters_layout.setContentsMargins(8, 6, 8, 6)
        filters_layout.setHorizontalSpacing(8)
        filters_layout.setVerticalSpacing(4)
        self.direct_region_filter = QComboBox()
        self.direct_state_filter = QComboBox()
        self.direct_behavior_filter = QComboBox()
        filter_widgets = [
            ("Region", self.direct_region_filter, "Filter by channel group / brain region."),
            ("State", self.direct_state_filter, "LFP means global LFP; Wake/NREM/REM/Working mean state-stratified LFP; connectivity means PLV/PAC."),
            ("Behavior", self.direct_behavior_filter, "Filter by rule, performance state, bias state, or RT class comparison."),
        ]
        for col, (label, combo, tooltip) in enumerate(filter_widgets):
            combo.addItem("None", "")
            combo.setToolTip(tooltip)
            combo.setMinimumWidth(120)
            combo.currentIndexChanged.connect(lambda _: self._apply_direct_behavior_filters())
            filters_layout.addWidget(QLabel(label), 0, col * 2)
            filters_layout.addWidget(combo, 0, col * 2 + 1)
        self.direct_filter_status_label = QLabel("Filters: None")
        filters_layout.addWidget(self.direct_filter_status_label, 1, 0, 1, 6)
        filters_layout.setColumnStretch(5, 1)
        layout.addWidget(filter_group)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.direct_readiness_txt = QTextEdit()
        self.direct_readiness_txt.setReadOnly(True)
        self.direct_readiness_txt.setPlaceholderText(
            "Direct, non-regression behavior-state comparisons for same-slice hourly LFP data."
        )
        self.direct_table = QTableWidget(0, 8)
        self.direct_table.setHorizontalHeaderLabels(
            ["target", "behavior", "effect", "eta2", "p", "q", "top contrast", "n"]
        )
        self.direct_table.itemSelectionChanged.connect(self._render_direct_behavior_selected_target)
        top_splitter.addWidget(self.direct_readiness_txt)
        top_splitter.addWidget(self.direct_table)
        top_splitter.setSizes([430, 720])
        splitter.addWidget(top_splitter)

        plot_splitter = QSplitter(Qt.Orientation.Vertical)
        self.direct_effect_plot = pg.PlotWidget(title="Direct Behavior Group Differences")
        self.direct_effect_plot.showGrid(x=True, y=True, alpha=0.25)
        self.direct_distribution_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.direct_distribution_plots = []
        self._ensure_direct_distribution_plots(3)
        plot_splitter.addWidget(self.direct_effect_plot)
        plot_splitter.addWidget(self.direct_distribution_splitter)
        plot_splitter.setSizes([300, 430])
        splitter.addWidget(plot_splitter)
        splitter.setSizes([260, 680])
        layout.addWidget(splitter)
        return widget

    def _build_paired_context_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        controls = QWidget()
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(8, 4, 8, 4)
        controls_layout.addWidget(QLabel("Reference"))
        self.paired_reference_combo = QComboBox()
        self.paired_reference_combo.addItem("Daily mean", "daily")
        self.paired_reference_combo.addItem("Whole-slice mean", "whole_slice")
        controls_layout.addWidget(self.paired_reference_combo)
        controls_layout.addWidget(QLabel("Window min"))
        self.paired_window_spin = QSpinBox()
        self.paired_window_spin.setRange(5, 360)
        self.paired_window_spin.setSingleStep(5)
        self.paired_window_spin.setValue(60)
        controls_layout.addWidget(self.paired_window_spin)
        controls_layout.addWidget(QLabel("Permutations"))
        self.paired_permutation_spin = QSpinBox()
        self.paired_permutation_spin.setRange(0, 10000)
        self.paired_permutation_spin.setSingleStep(100)
        self.paired_permutation_spin.setValue(1000)
        controls_layout.addWidget(self.paired_permutation_spin)
        run_button = QPushButton("Run Paired Context")
        run_button.clicked.connect(self.run_paired_trial_context)
        controls_layout.addWidget(run_button)
        reload_button = QPushButton("Reload Results")
        reload_button.clicked.connect(self.reload_paired_trial_context_results)
        controls_layout.addWidget(reload_button)
        open_button = QPushButton("Open Output Dir")
        open_button.clicked.connect(self.open_paired_trial_context_output_dir)
        controls_layout.addWidget(open_button)
        controls_layout.addStretch()
        layout.addWidget(controls)

        splitter = QSplitter(Qt.Orientation.Vertical)
        top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.paired_readiness_txt = QTextEdit()
        self.paired_readiness_txt.setReadOnly(True)
        self.paired_readiness_txt.setPlaceholderText(
            "Trial-level paired pre/post context analysis readiness and outputs."
        )
        self.paired_table = QTableWidget(0, 8)
        self.paired_table.setHorizontalHeaderLabels(
            ["feature", "family", "behavior", "effect", "p", "q", "contrast", "n"]
        )
        self.paired_table.itemSelectionChanged.connect(self._render_paired_context_selected_target)
        top_splitter.addWidget(self.paired_readiness_txt)
        top_splitter.addWidget(self.paired_table)
        top_splitter.setSizes([430, 720])
        splitter.addWidget(top_splitter)

        plot_splitter = QSplitter(Qt.Orientation.Vertical)
        self.paired_effect_plot = pg.PlotWidget(title="Paired Trial Context Effects")
        self.paired_effect_plot.showGrid(x=True, y=True, alpha=0.25)
        self.paired_state_plot = pg.PlotWidget(title="Pre/Post Natural State Occupancy")
        self.paired_state_plot.showGrid(x=True, y=True, alpha=0.25)
        self.paired_distribution_plot = pg.PlotWidget(title="Selected Feature by Trial Behavior")
        self.paired_distribution_plot.showGrid(x=True, y=True, alpha=0.25)
        plot_splitter.addWidget(self.paired_effect_plot)
        plot_splitter.addWidget(self.paired_state_plot)
        plot_splitter.addWidget(self.paired_distribution_plot)
        plot_splitter.setSizes([240, 230, 260])
        splitter.addWidget(plot_splitter)
        splitter.setSizes([260, 680])
        layout.addWidget(splitter)
        return widget

    def _build_paths_group(self):
        group = QGroupBox("Workflow Paths")
        layout = QGridLayout(group)

        self.config_edit = QLineEdit()
        self.mode0_edit = QLineEdit()
        self.mode3_edit = QLineEdit()
        self.slice_json_edit = QLineEdit()
        self.slice_combo = QComboBox()
        self.output_edit = QLineEdit()
        self.h5_edit = QLineEdit()
        self.report_edit = QLineEdit()
        self.report_edit.setReadOnly(True)

        rows = [
            ("Config", self.config_edit, self.browse_config, "Browse"),
            ("Slice JSON", self.slice_json_edit, self.browse_slice_json, "Browse"),
            ("Output Dir", self.output_edit, lambda: self._browse_dir_into(self.output_edit), "Browse"),
            ("Current H5", self.h5_edit, self.prompt_load_h5, "Browse"),
            ("Report", self.report_edit, self.open_report, "Open"),
        ]
        for row, (label, edit, callback, button_text) in enumerate(rows):
            layout.addWidget(QLabel(label), row, 0)
            layout.addWidget(edit, row, 1)
            button = QPushButton(button_text)
            button.clicked.connect(callback)
            layout.addWidget(button, row, 2)
        slice_row = len(rows)
        layout.addWidget(QLabel("Slice"), slice_row, 0)
        layout.addWidget(self.slice_combo, slice_row, 1, 1, 2)
        layout.setColumnStretch(1, 1)
        return group

    def _build_workflow_group(self):
        group = QGroupBox("Build / Analyze")
        layout = QVBoxLayout(group)

        buttons = [
            ("1. Build Natural-Day H5s From Slice", self.build_slice_h5_file),
            ("2. Load Selected H5", self.load_selected_h5),
            ("3. Extract Features", self.run_extract_features),
            ("4. Classify States", self.run_classify_states),
            ("5. Summarize Rhythm", self.run_summarize_rhythm),
            ("6. Longitudinal Dynamics", self.run_longitudinal_dynamics),
            ("7. Generate Report", self.run_generate_report),
            ("Run Full Analysis (Current H5)", self.run_full_analysis_current),
            ("Run Full Analysis (All Listed H5)", self.run_full_analysis_all),
            ("Resume Auto Feature/State Queue", self.resume_auto_h5_analysis_queue),
            ("Render Current Selection", self.on_btn_render_clicked),
        ]
        for text, callback in buttons:
            button = QPushButton(text)
            if "Full Analysis" in text:
                button.setStyleSheet("font-weight: bold; padding: 8px;")
            button.clicked.connect(callback)
            layout.addWidget(button)
        return group

    def _build_files_group(self):
        group = QGroupBox("H5 Files")
        group.setMinimumHeight(390)
        layout = QVBoxLayout(group)
        self.h5_list = QListWidget()
        self.h5_list.setMinimumHeight(310)
        self.h5_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.h5_list.setAlternatingRowColors(True)
        self.h5_list.itemDoubleClicked.connect(lambda _: self.load_selected_h5())
        layout.addWidget(self.h5_list)

        row = QHBoxLayout()
        refresh_button = QPushButton("Refresh List")
        refresh_button.clicked.connect(self.refresh_h5_list)
        row.addWidget(refresh_button)
        load_button = QPushButton("Load")
        load_button.clicked.connect(self.load_selected_h5)
        row.addWidget(load_button)
        layout.addLayout(row)
        return group

    def _build_channels_group(self):
        group = QGroupBox("Channels")
        layout = QVBoxLayout(group)

        lfp_group = QGroupBox("LFP Channels")
        lfp_layout = QGridLayout(lfp_group)
        self.lfp_checkboxes = []
        for idx in range(16):
            cb = QCheckBox(f"Ch {idx}")
            cb.setChecked(True)
            cb.stateChanged.connect(self.on_channel_toggled)
            self.lfp_checkboxes.append(cb)
            lfp_layout.addWidget(cb, idx // 4, idx % 4)
        layout.addWidget(lfp_group)

        imu_group = QGroupBox("IMU Channels")
        imu_layout = QHBoxLayout(imu_group)
        self.imu_checkboxes = []
        for label in ["Accel X", "Accel Y", "Accel Z"]:
            cb = QCheckBox(label)
            cb.setChecked(True)
            cb.stateChanged.connect(self.on_channel_toggled)
            self.imu_checkboxes.append(cb)
            imu_layout.addWidget(cb)
        layout.addWidget(imu_group)
        return group

    def _build_info_group(self):
        group = QGroupBox("Info / Logs")
        layout = QVBoxLayout(group)

        self.lbl_info = QTextEdit()
        self.lbl_info.setReadOnly(True)
        self.lbl_info.setMinimumHeight(220)
        self.lbl_info.setPlaceholderText("H5 metadata and structure will appear here.")
        layout.addWidget(QLabel("H5 Metadata / Structure"))
        layout.addWidget(self.lbl_info)

        self.trial_info_txt = QTextEdit()
        self.trial_info_txt.setReadOnly(True)
        self.trial_info_txt.setMaximumHeight(130)
        self.trial_info_txt.setPlaceholderText("Trial / working overlap summary.")
        layout.addWidget(QLabel("Trial / Working Summary"))
        layout.addWidget(self.trial_info_txt)

        self.log_txt = QTextEdit()
        self.log_txt.setReadOnly(True)
        self.log_txt.setMinimumHeight(140)
        self.log_txt.setPlaceholderText("Workflow logs will appear here.")
        layout.addWidget(QLabel("Workflow Log"))
        layout.addWidget(self.log_txt)
        return group

    def _build_overview_plot(self):
        self.overview_axis = TimeAxisItem(orientation="bottom")
        self.plot_overview = pg.PlotWidget(
            title="Recording Overview",
            axisItems={"bottom": self.overview_axis},
        )
        self.plot_overview.setMaximumHeight(220)
        self.plot_overview.setXRange(0, self.total_seconds, padding=0)
        self.plot_overview.setLabel("bottom", "Absolute Time")
        self.region = pg.LinearRegionItem([0, 30])
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self.on_region_drag_update)
        self.plot_overview.addItem(self.region)
        return self.plot_overview

    def _build_lfp_plot(self):
        self.lfp_axis = TimeAxisItem(orientation="bottom")
        self.plot_lfp = pg.PlotWidget(
            title="LFP Detail",
            axisItems={"bottom": self.lfp_axis},
        )
        self.plot_lfp.setLabel("left", "Voltage", units="uV")
        self.plot_lfp.setLabel("bottom", "Absolute Time")
        self.plot_lfp.showGrid(x=True, y=True, alpha=0.3)
        self.plot_lfp.setLimits(xMin=0, xMax=self.total_seconds)
        return self.plot_lfp

    def _build_imu_plot(self):
        self.imu_axis = TimeAxisItem(orientation="bottom")
        self.plot_imu = pg.PlotWidget(
            title="IMU Detail",
            axisItems={"bottom": self.imu_axis},
        )
        self.plot_imu.setMaximumHeight(220)
        self.plot_imu.setLabel("left", "Acceleration")
        self.plot_imu.setLabel("bottom", "Absolute Time")
        self.plot_imu.showGrid(x=True, y=True, alpha=0.3)
        self.plot_imu.setLimits(xMin=0, xMax=self.total_seconds)
        self.plot_imu.setXLink(self.plot_lfp)
        return self.plot_imu

    def _build_spectrogram_plot(self):
        self.spec_axis = TimeAxisItem(orientation="bottom")
        self.plot_spec = pg.PlotWidget(
            title="Spectrogram (First Visible LFP Channel)",
            axisItems={"bottom": self.spec_axis},
        )
        self.plot_spec.setMaximumHeight(260)
        self.plot_spec.setLabel("left", "Frequency", units="Hz")
        self.plot_spec.setLabel("bottom", "Absolute Time")
        self.plot_spec.showGrid(x=True, y=True, alpha=0.25)
        self.plot_spec.setLimits(xMin=0, xMax=self.total_seconds)
        self.plot_spec.setXLink(self.plot_lfp)
        return self.plot_spec

    def _build_masks_plot(self):
        self.masks_axis = TimeAxisItem(orientation="bottom")
        self.plot_masks = pg.PlotWidget(
            title="Masks Detail",
            axisItems={"bottom": self.masks_axis},
        )
        self.plot_masks.setMaximumHeight(130)
        self.plot_masks.setLabel("left", "Status")
        self.plot_masks.setLabel("bottom", "Absolute Time")
        self.plot_masks.setYRange(-0.5, 4.5, padding=0)
        self.plot_masks.showGrid(x=True, y=True, alpha=0.3)
        self.plot_masks.setLimits(xMin=0, xMax=self.total_seconds)
        self.plot_masks.setXLink(self.plot_lfp)
        return self.plot_masks

    def _build_hourly_band_plot(self):
        self.summary_hourly_axis = SummaryHourAxisItem(orientation="bottom")
        self.plot_hourly = pg.PlotWidget(
            title="Hourly Band Power By Channel (normalized)",
            axisItems={"bottom": self.summary_hourly_axis},
        )
        self.plot_hourly.setLabel("left", "Normalized Power")
        self.plot_hourly.setLabel("bottom", "Local Date / Hour")
        self.plot_hourly.showGrid(x=True, y=True, alpha=0.3)
        self.plot_hourly.setLimits(xMin=0)
        return self.plot_hourly

    def _build_state_occupancy_plot(self):
        self.summary_state_occ_axis = SummaryHourAxisItem(orientation="bottom")
        self.plot_state_occ = pg.PlotWidget(
            title="Hourly State Occupancy",
            axisItems={"bottom": self.summary_state_occ_axis},
        )
        self.plot_state_occ.setLabel("left", "Occupancy")
        self.plot_state_occ.setLabel("bottom", "Local Date / Hour")
        self.plot_state_occ.showGrid(x=True, y=True, alpha=0.3)
        self.plot_state_occ.setYRange(0.0, 1.0, padding=0.05)
        self.plot_state_occ.setLimits(xMin=0, yMin=0, yMax=1.0)
        return self.plot_state_occ

    def _build_state_normalized_plot(self):
        self.summary_state_norm_axis = SummaryHourAxisItem(orientation="bottom")
        self.plot_state_norm = pg.PlotWidget(
            title="State-Normalized Hourly Power",
            axisItems={"bottom": self.summary_state_norm_axis},
        )
        self.plot_state_norm.setLabel("left", "State | Band")
        self.plot_state_norm.setLabel("bottom", "Local Date / Hour")
        self.plot_state_norm.showGrid(x=True, y=True, alpha=0.2)
        self.plot_state_norm.setLimits(xMin=0)
        return self.plot_state_norm

    def _build_behavior_state_plot(self):
        self.summary_behavior_axis = SummaryHourAxisItem(orientation="bottom")
        self.plot_behavior = pg.PlotWidget(
            title="Hourly Behavior State",
            axisItems={"bottom": self.summary_behavior_axis},
        )
        self.plot_behavior.setLabel("left", "Behavior")
        self.plot_behavior.setLabel("bottom", "Local Date / Hour")
        self.plot_behavior.showGrid(x=True, y=True, alpha=0.2)
        self.plot_behavior.setYRange(-0.6, 3.6, padding=0)
        self.plot_behavior.setLimits(xMin=0)
        self.plot_behavior.getAxis("left").setTicks([[(0, "Rule"), (1, "Perf"), (2, "Bias"), (3, "RT")]])
        return self.plot_behavior

    def _build_connectivity_summary_plot(self):
        self.summary_connectivity_axis = SummaryHourAxisItem(orientation="bottom")
        self.plot_connectivity = pg.PlotWidget(
            title="Hourly PLV/PAC Summary",
            axisItems={"bottom": self.summary_connectivity_axis},
        )
        self.plot_connectivity.setLabel("left", "Mean PLV / PAC")
        self.plot_connectivity.setLabel("bottom", "Local Date / Hour")
        self.plot_connectivity.showGrid(x=True, y=True, alpha=0.25)
        self.plot_connectivity.setLimits(xMin=0)
        return self.plot_connectivity

    def _build_summary_info_widget(self):
        group = QGroupBox("Rhythm / State Summary")
        layout = QVBoxLayout(group)
        self.summary_txt = QTextEdit()
        self.summary_txt.setReadOnly(True)
        self.summary_txt.setPlaceholderText(
            "Hourly rhythm summary, normalized state-band power, and state thresholds will appear here."
        )
        layout.addWidget(self.summary_txt)
        return group

    def _apply_default_paths(self):
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        config_path = self._normalize_path(self.config_path or os.path.join(base_dir, "config_default.yaml"))
        self.config_edit.setText(config_path)
        cfg = resolve_config(config_path)
        output_dir = cfg.get("general.output_dir", "")
        if output_dir:
            self.output_edit.setText(self._normalize_path(output_dir, base_dir=os.path.dirname(config_path)))
        elif self.h5_path:
            self.output_edit.setText(self._normalize_path(os.path.dirname(self.h5_path)))
        if self.h5_path:
            self.h5_edit.setText(self._normalize_path(self.h5_path))
        self.refresh_h5_list()

    def append_log(self, text):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_txt.append(f"[{timestamp}] {text}")
        self.statusBar.showMessage(text, 5000)
        self._process_ui_events()

    def _show_error(self, title, error):
        self.append_log(f"{title}: {error}")
        QMessageBox.critical(self, title, str(error))

    def _browse_dir_into(self, line_edit):
        start_dir = self._safe_existing_directory(
            line_edit.text().strip(),
            fallback=self.output_edit.text().strip() if line_edit is not self.output_edit else self._config_dir(),
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Directory",
            start_dir,
        )
        if selected:
            line_edit.setText(self._normalize_path(selected))
            if line_edit is self.output_edit:
                self.refresh_h5_list()

    def browse_slice_json(self):
        start_dir = self._safe_existing_directory(
            self.slice_json_edit.text().strip() or self.output_edit.text().strip(),
            fallback=self.output_edit.text().strip() or self._config_dir(),
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open Raw Overview Slice JSON",
            start_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if not selected:
            return
        selected = self._normalize_path(selected)
        self.slice_json_edit.setText(selected)
        self.load_slice_json_file(selected)

    def load_slice_json_file(self, path):
        payload = load_slice_json(path)
        self.slice_payload = payload
        self.slice_json_path = path
        self.slice_combo.clear()
        timezone_name = payload.get("timezone", "Asia/Shanghai")
        for item in payload.get("slices", []):
            self.slice_combo.addItem(format_slice_option(item, timezone_name), item)
        self.append_log(
            f"Loaded slice JSON: {path} | slices={self.slice_combo.count()} | source_root={payload.get('source_root', '')}"
        )

    def browse_config(self):
        start_dir = self._safe_existing_directory(self.config_edit.text().strip(), fallback=self._config_dir())
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open Config",
            start_dir,
            "YAML Files (*.yaml *.yml);;All Files (*)",
        )
        if selected:
            selected = self._normalize_path(selected)
            self.config_edit.setText(selected)
            cfg = resolve_config(selected)
            config_output_dir = cfg.get("general.output_dir", "")
            if config_output_dir and not self.output_edit.text().strip():
                self.output_edit.setText(self._normalize_path(config_output_dir, base_dir=os.path.dirname(selected)))
            self.refresh_h5_list()

    def prompt_load_h5(self):
        start_dir = self._safe_existing_directory(
            self.h5_edit.text().strip() or self.output_edit.text().strip(),
            fallback=self.output_edit.text().strip(),
        )
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "Open Daily H5",
            start_dir,
            "HDF5 Files (*.h5)",
        )
        if selected:
            selected = self._normalize_path(selected)
            self.h5_edit.setText(selected)
            self.load_h5(selected)

    def _get_config(self):
        return resolve_config(self.config_edit.text().strip() or None)

    def _get_current_h5_path(self):
        item = self.h5_list.currentItem()
        if item is not None:
            path = item.data(Qt.ItemDataRole.UserRole)
            return path or item.text()
        return self.h5_edit.text().strip() or self.h5_path

    def _set_current_h5_path(self, path):
        self.h5_path = path
        self.h5_edit.setText(path or "")

    def _close_h5_handle(self):
        if self.h5_file is not None:
            try:
                self.h5_file.close()
            except Exception:
                pass
            self.h5_file = None
        self.spectrogram_global_level_cache = {}

    def _get_selected_spectrogram_freq_range(self):
        freq_min_hz = float(self.spectrogram_freq_min_hz)
        freq_max_hz = 100.0
        combo = getattr(self, "spectrogram_freq_combo", None)
        if combo is not None and combo.count():
            try:
                selected = combo.currentData()
                if selected is None:
                    selected = combo.currentText().split("-")[-1].replace("Hz", "").strip()
                freq_max_hz = float(selected)
            except Exception:
                freq_max_hz = 100.0
        if freq_max_hz < freq_min_hz:
            freq_max_hz = freq_min_hz
        return freq_min_hz, freq_max_hz

    def _format_spectrogram_freq_range(self):
        freq_min_hz, freq_max_hz = self._get_selected_spectrogram_freq_range()
        return f"{int(round(freq_min_hz))}-{int(round(freq_max_hz))} Hz"

    def _on_spectrogram_freq_changed(self, _index):
        if self.h5_file is not None:
            self._fetch_and_draw_detail()

    def _estimate_global_spectrogram_levels(self, channel_idx, freq_min_hz, freq_max_hz):
        if self.h5_file is None or "raw_lfp" not in self.h5_file["lfp"]:
            return None
        cache_key = (int(channel_idx), float(freq_min_hz), float(freq_max_hz))
        if cache_key in self.spectrogram_global_level_cache:
            return self.spectrogram_global_level_cache[cache_key]

        raw_lfp = self.h5_file["lfp/raw_lfp"]
        if channel_idx < 0 or channel_idx >= raw_lfp.shape[0]:
            return None

        chunk_duration_s = 120.0
        n_chunks = 24
        chunk_samples = max(128, int(round(chunk_duration_s * self.lfp_fs)))
        total_samples = int(raw_lfp.shape[1])
        if total_samples <= 0:
            return None

        if total_samples <= chunk_samples:
            starts = np.asarray([0], dtype=np.int64)
        else:
            starts = np.linspace(
                0,
                max(total_samples - chunk_samples, 0),
                num=n_chunks,
                dtype=np.int64,
            )
            starts = np.unique(starts)

        all_values = []
        for start in starts.tolist():
            end = min(total_samples, int(start) + chunk_samples)
            chunk = np.asarray(raw_lfp[channel_idx, start:end], dtype=np.float64)
            finite = np.isfinite(chunk)
            if chunk.size < 128 or not np.any(finite):
                continue
            fill_value = float(np.nanmedian(chunk[finite]))
            chunk = np.nan_to_num(chunk, nan=fill_value)
            nperseg = min(1024, chunk.size)
            if nperseg < 64:
                continue
            noverlap = int(nperseg * 0.75)
            freqs, _, sxx = signal.spectrogram(
                chunk,
                fs=self.lfp_fs,
                nperseg=nperseg,
                noverlap=noverlap,
                scaling="density",
                mode="psd",
            )
            freq_mask = (freqs >= float(freq_min_hz)) & (freqs <= float(freq_max_hz))
            if not np.any(freq_mask):
                continue
            sxx_db = 10.0 * np.log10(np.maximum(sxx[freq_mask], 1e-12))
            finite_vals = sxx_db[np.isfinite(sxx_db)]
            if finite_vals.size:
                all_values.append(finite_vals)

        if all_values:
            combined = np.concatenate(all_values)
            levels = _robust_levels(combined, default=None)
        else:
            levels = None
        if levels is None:
            levels = (-100.0, -20.0)
        self.spectrogram_global_level_cache[cache_key] = levels
        return levels

    def refresh_h5_list(self, preferred_path=None):
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = ""
        self.h5_list.clear()
        if not output_dir or not os.path.isdir(output_dir):
            return
        found = []
        for dirpath, _, filenames in os.walk(output_dir):
            for fname in filenames:
                if fname.endswith(".h5"):
                    found.append(os.path.join(dirpath, fname))
        current = preferred_path or self.h5_path or self.h5_edit.text().strip()
        for idx, path in enumerate(sorted(found)):
            status, tooltip = self._h5_analysis_status(path)
            item = QListWidgetItem(f"{status} {os.path.basename(path)}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(f"{path}\n{tooltip}")
            self.h5_list.addItem(item)
            if current and os.path.abspath(path) == os.path.abspath(current):
                self.h5_list.setCurrentRow(idx)
        if hasattr(self, "stats_readiness_txt"):
            self._update_slice_statistics_readiness()
        if hasattr(self, "direct_readiness_txt"):
            self._update_direct_behavior_stats_readiness()
        if hasattr(self, "paired_readiness_txt"):
            self._update_paired_trial_context_readiness()

    def _h5_analysis_status(self, path):
        flags = []
        try:
            with h5py.File(path, "r") as h5f:
                if "features" in h5f and "band_power" in h5f["features"]:
                    flags.append("FEATURES")
                if "states" in h5f and "state_label" in h5f["states"]:
                    flags.append("STATE")
                    review = h5f["states"].get("review", None)
                    if review is not None and bool(review.attrs.get("review_complete", False)):
                        flags.append("REVIEW")
                if "rhythm" in h5f and "hourly_feature_table" in h5f["rhythm"]:
                    flags.append("SUMMARY")
                if "longitudinal" in h5f:
                    flags.append("LONG")
        except Exception as exc:
            if _is_hdf5_lock_error(exc):
                return (
                    "[BUSY]",
                    "This H5 is currently locked, usually because the background build "
                    "or another analysis step is still writing it. Refresh after it completes.",
                )
            return "[ERR]", f"Could not read status: {exc}"
        if "LONG" in flags and self._slice_statistics_summary_exists_for_h5(path):
            flags.append("STATS")
        if "STATS" in flags:
            status = "[STATS]"
        elif "LONG" in flags:
            status = "[LONG]"
        elif "SUMMARY" in flags:
            status = "[SUMMARY]"
        elif "REVIEW" in flags:
            status = "[REVIEW]"
        elif "STATE" in flags:
            status = "[STATE]"
        elif "FEATURES" in flags:
            status = "[FEATURES]"
        else:
            status = "[BUILT]"
        return status, "Analysis flags: " + (", ".join(flags) if flags else "built only")

    def build_h5_files(self):
        try:
            cfg = self._get_config()
            mode0_dir = self._normalize_dir_field(self.mode0_edit, base_dir=self._config_dir()) or None
            mode3_dir = self._normalize_dir_field(self.mode3_edit, base_dir=self._config_dir()) or None
            output_dir = self._resolve_output_dir(allow_create=True)
            self._start_build_progress("Scanning EDF files...", busy=True)
            self.append_log("Scanning EDF files...")
            records = self._run_build_background_callable(
                "Scanning EDF files...",
                lambda: index_files(mode0_dir=mode0_dir, mode3_dir=mode3_dir),
            )
            if not records:
                raise ValueError("No EDF files found in the selected Mode0 / Mode3 directories.")
            self.append_log(f"Indexed {len(records)} EDF files.")
            records = self._run_build_background_callable(
                "Computing file coverage...",
                lambda: generate_file_coverage(records),
            )
            daily_groups = group_by_day(records, timezone=cfg.get("general.timezone", "Asia/Shanghai"))
            built = []
            daily_items = sorted(daily_groups.items())
            total_steps = max(len(daily_items) + 2, 1)
            self._update_build_progress("Preparing daily H5 build...", busy=False, maximum=total_steps, value=1)
            for idx, (date_str, rows) in enumerate(daily_items, start=2):
                self._update_build_progress(f"Building daily H5 for {date_str} ({idx-1}/{len(daily_items)})", value=idx)
                self.append_log(f"Building daily H5 for {date_str}...")
                built.append(
                    self._run_build_background_callable(
                        f"Building daily H5 for {date_str} ({idx-1}/{len(daily_items)})",
                        lambda date_str=date_str, rows=rows: build_daily_h5(date_str, rows, output_dir, config=cfg),
                    )
                )
            self.output_edit.setText(output_dir)
            self._update_build_progress("Refreshing H5 file list...", value=total_steps)
            self.refresh_h5_list()
            if built:
                self._update_build_progress("Loading first built H5...", value=total_steps)
                self.load_h5(built[0], manage_progress=False)
            self.append_log(f"Built {len(built)} daily H5 files.")
            self._finish_build_progress("Build complete")
        except Exception as exc:
            self._finish_build_progress("Build failed")
            self._show_error("Build H5 Failed", exc)

    def build_slice_h5_file(self):
        try:
            if self.slice_build_running and self.slice_build_process is not None and self.slice_build_process.is_alive():
                raise ValueError("A natural-day slice build is already running in the background.")
            cfg = self._get_config()
            if self.slice_payload is None:
                slice_path = self.slice_json_edit.text().strip()
                if not slice_path:
                    raise ValueError("Please select a Raw Data Overview slice JSON first.")
                self.load_slice_json_file(self._normalize_path(slice_path))
            if self.slice_combo.count() <= 0:
                raise ValueError("Slice JSON does not contain selectable slices.")
            slice_item = self.slice_combo.currentData()
            if not slice_item:
                raise ValueError("Please select one slice.")

            output_dir = self._resolve_output_dir(allow_create=True)
            output_dir, existing_h5_paths = self._resolve_slice_build_output_dir_and_existing_paths(
                dict(self.slice_payload),
                dict(slice_item),
                output_dir,
                cfg,
            )
            if not os.path.isdir(output_dir):
                os.makedirs(output_dir, exist_ok=True)
            self.output_edit.setText(output_dir)
            self._start_natural_day_slice_build(
                slice_payload=dict(self.slice_payload),
                slice_item=dict(slice_item),
                output_dir=output_dir,
                source_slice_json=self.slice_json_path or self.slice_json_edit.text().strip(),
                config_data=cfg.as_dict(),
                existing_h5_paths=existing_h5_paths,
            )
        except Exception as exc:
            self._finish_build_progress("Start slice build failed")
            self._show_error("Build Natural-Day H5s Failed", exc)

    def _start_natural_day_slice_build(
        self,
        slice_payload,
        slice_item,
        output_dir,
        source_slice_json,
        config_data,
        existing_h5_paths=None,
    ):
        ctx = multiprocessing.get_context("spawn")
        self.slice_build_queue = ctx.Queue()
        self.slice_build_process = ctx.Process(
            target=build_natural_day_slice_h5s_worker,
            args=(
                slice_payload,
                slice_item,
                output_dir,
                source_slice_json,
                config_data,
                self.slice_build_queue,
                list(existing_h5_paths or []),
            ),
            daemon=True,
        )
        self.slice_build_first_loaded = False
        self.slice_build_running = True
        self.slice_build_process.start()
        self.slice_build_poll_timer.start(500)
        self._start_build_progress(
            "Background natural-day H5 build started; completed H5 files will auto-run features and state review.",
            busy=False,
            maximum=100,
            value=0,
        )
        self.append_log("Background natural-day H5 build started. Auto feature extraction/state review queue is enabled.")

    def _poll_slice_build_queue(self):
        if self.slice_build_queue is not None:
            while True:
                try:
                    message = self.slice_build_queue.get_nowait()
                except queue.Empty:
                    break
                self._handle_slice_build_message(message)
                if message.get("type") in {"done", "error"}:
                    return

        process = self.slice_build_process
        if self.slice_build_running and process is not None and not process.is_alive():
            exitcode = process.exitcode
            if exitcode not in (0, None):
                self.append_log(f"Background natural-day build process exited with code {exitcode}.")
                self._finish_background_slice_build("Background natural-day build failed")

    def _handle_slice_build_message(self, message):
        msg_type = message.get("type")
        if msg_type == "progress":
            text = message.get("message", "")
            if text:
                self.append_log(text)
                value = message.get("value")
                maximum = message.get("maximum")
                if value is not None and maximum is not None:
                    self._update_build_progress(
                        text=f"Background build: {text}",
                        busy=False,
                        maximum=max(int(maximum), 1),
                        value=int(value),
                    )
                else:
                    self._update_build_progress(text=f"Background build: {text}")
            return
        if msg_type == "started":
            total = int(message.get("total", 0))
            maximum = max(int(message.get("maximum", total or 1)), 1)
            value = int(message.get("value", 0))
            message_text = message.get("message") or f"Background natural-day build: 0/{total} days complete"
            if message.get("message"):
                self.append_log(str(message.get("message")))
            self._update_build_progress(
                text=message_text,
                busy=False,
                maximum=maximum,
                value=value,
            )
            return
        if msg_type == "day_complete":
            index = int(message.get("index", 0))
            total = int(message.get("total", 0))
            path = message.get("path", "")
            date_str = message.get("date", "")
            self.append_log(
                f"Natural-day H5 complete {index}/{total}: {date_str} | "
                f"EDF files={message.get('edf_files', 0)} | "
                f"behavior trials={message.get('behavior_trials', 0)}"
            )
            self.refresh_h5_list()
            if path and self.auto_analyze_after_slice_build:
                self._enqueue_auto_h5_analysis(path)
            value = int(message.get("value", index))
            maximum = max(int(message.get("maximum", total or 1)), 1)
            self._update_build_progress(
                text=f"Background natural-day build: {index}/{total} days complete",
                busy=False,
                maximum=maximum,
                value=value,
            )
            if (
                path
                and not self.auto_analyze_after_slice_build
                and not self.slice_build_first_loaded
                and not self.h5_operation_running
                and self.h5_file is None
            ):
                self.slice_build_first_loaded = True
                self.load_h5(path, manage_progress=False)
                self.append_log("Loaded first completed natural-day H5; remaining days continue in background.")
            elif path and not self.auto_analyze_after_slice_build and not self.slice_build_first_loaded:
                self.slice_build_first_loaded = True
                self.append_log(
                    "First natural-day H5 is complete. It was not auto-loaded because a current H5 "
                    "is already loaded or an H5 operation is running."
                )
            return
        if msg_type == "done":
            built = message.get("built", [])
            skipped_existing = message.get("skipped_existing", [])
            value = message.get("value")
            maximum = message.get("maximum")
            if value is not None and maximum is not None:
                self._update_build_progress(
                    text="Background natural-day build: all work complete",
                    busy=False,
                    maximum=max(int(maximum), 1),
                    value=int(value),
                )
            self.refresh_h5_list()
            if skipped_existing:
                self.append_log(f"Skipped {len(skipped_existing)} existing natural-day H5 files.")
            done_message = message.get("message", "")
            if done_message:
                self.append_log(str(done_message))
            self.append_log(f"Background natural-day build complete: {len(built)} newly built H5 files.")
            self._finish_background_slice_build("Background natural-day build complete")
            return
        if msg_type == "error":
            self.append_log(f"Background natural-day build failed: {message.get('error', '')}")
            tb = message.get("traceback", "")
            if tb:
                self.append_log(tb.splitlines()[-1])
            self._finish_background_slice_build("Background natural-day build failed")

    def _finish_background_slice_build(self, text):
        self.slice_build_poll_timer.stop()
        if self.slice_build_process is not None:
            try:
                self.slice_build_process.join(timeout=0.2)
            except Exception:
                pass
        if self.slice_build_queue is not None:
            try:
                self.slice_build_queue.close()
            except Exception:
                pass
        self.slice_build_process = None
        self.slice_build_queue = None
        self.slice_build_running = False
        self._finish_build_progress(text)

    def _enqueue_auto_h5_analysis(self, h5_path):
        if not h5_path:
            return
        normalized = self._normalize_path(h5_path)
        queued_paths = {item["path"] for item in self.auto_h5_analysis_queue}
        if normalized in queued_paths:
            return
        self.auto_h5_analysis_queue.append({"path": normalized, "attempts": 0})
        self.append_log(
            f"Queued auto feature/state review: {os.path.basename(normalized)} "
            f"(queue={len(self.auto_h5_analysis_queue)})"
        )
        if not self.auto_h5_analysis_paused:
            QTimer.singleShot(0, self._process_auto_h5_analysis_queue)

    def resume_auto_h5_analysis_queue(self):
        self.auto_h5_analysis_paused = False
        self.append_log(f"Auto feature/state queue resumed. Pending H5 files: {len(self.auto_h5_analysis_queue)}")
        QTimer.singleShot(0, self._process_auto_h5_analysis_queue)

    def _h5_dataset_exists(self, h5_path, dataset_path):
        try:
            with h5py.File(h5_path, "r") as h5f:
                return dataset_path in h5f
        except Exception:
            return False

    def _process_auto_h5_analysis_queue(self):
        if self.auto_h5_analysis_paused or self.auto_h5_analysis_running or self.h5_operation_running:
            return
        if not self.auto_h5_analysis_queue:
            return

        item = self.auto_h5_analysis_queue.pop(0)
        h5_path = item["path"]
        if not os.path.exists(h5_path):
            self.append_log(f"Auto feature/state skipped missing H5: {h5_path}")
            QTimer.singleShot(0, self._process_auto_h5_analysis_queue)
            return

        status, tooltip = self._h5_analysis_status(h5_path)
        if status == "[BUSY]":
            item["attempts"] = int(item.get("attempts", 0)) + 1
            self.auto_h5_analysis_queue.insert(0, item)
            self._update_progress(
                text=f"Waiting for H5 lock to release -> {os.path.basename(h5_path)}",
                busy=False,
                maximum=max(len(self.auto_h5_analysis_queue), 1),
                value=0,
            )
            QTimer.singleShot(1500, self._process_auto_h5_analysis_queue)
            return

        self.auto_h5_analysis_running = True
        started_operation = False
        label = f"Auto features + state review -> {os.path.basename(h5_path)}"
        try:
            cfg = self._get_config()
            self._begin_h5_operation(label, h5_path)
            started_operation = True
            self._set_current_h5_path(h5_path)
            if self.h5_path and os.path.abspath(self.h5_path) == os.path.abspath(h5_path):
                self._close_h5_handle()

            self._start_progress(label, busy=False, maximum=2, value=0)
            if self._h5_dataset_exists(h5_path, "features/band_power"):
                self.append_log(f"Auto extract skipped, features already exist: {os.path.basename(h5_path)}")
            else:
                self._update_progress(f"Auto step 3/4: Extract features -> {os.path.basename(h5_path)}", value=0)
                self.append_log(f"Auto extract features -> {os.path.basename(h5_path)}")
                run_callable_in_thread(lambda: extract_features(h5_path, cfg))
                self.refresh_h5_list()

            if self._h5_dataset_exists(h5_path, "states/review"):
                try:
                    with h5py.File(h5_path, "r") as h5f:
                        review_done = bool(h5f["states/review"].attrs.get("review_complete", False))
                except Exception:
                    review_done = False
                if review_done:
                    self.append_log(f"Auto state review skipped, review already complete: {os.path.basename(h5_path)}")
                    completed = True
                else:
                    completed = None
            else:
                completed = None

            if completed is None:
                self._update_progress(
                    f"Auto step 4/4: Classify states review -> {os.path.basename(h5_path)}",
                    value=1,
                    busy=False,
                    maximum=2,
                )
                self.append_log(f"Auto state review popup -> {os.path.basename(h5_path)}")
                completed = run_state_review_wizard(self, h5_path, cfg)

            self.refresh_h5_list()
            if completed:
                self.append_log(f"Auto feature/state review completed: {os.path.basename(h5_path)}")
                if not self.auto_h5_analysis_queue:
                    self.load_h5(h5_path, manage_progress=False)
                self._finish_progress(
                    f"Auto feature/state completed ({len(self.auto_h5_analysis_queue)} queued)"
                )
            else:
                self.auto_h5_analysis_paused = True
                self.append_log(
                    "Auto feature/state queue paused because state review was cancelled. "
                    "Use 'Resume Auto Feature/State Queue' to continue with remaining H5 files."
                )
                self._finish_progress("Auto queue paused")
        except Exception as exc:
            self.append_log(f"Auto feature/state failed for {os.path.basename(h5_path)}: {exc}")
            self._finish_progress("Auto feature/state failed")
            self._show_error("Auto Feature/State Failed", exc)
        finally:
            if started_operation:
                self._end_h5_operation(label)
            self.auto_h5_analysis_running = False
            if self.auto_h5_analysis_queue and not self.auto_h5_analysis_paused:
                QTimer.singleShot(0, self._process_auto_h5_analysis_queue)

    def load_selected_h5(self):
        if self.h5_operation_running:
            self._show_error(
                "Load H5 Failed",
                "A current-H5 operation is running. Please wait for it to finish before loading another H5.",
            )
            return
        current = self._get_current_h5_path()
        if not current:
            self._show_error("Load H5 Failed", "Please choose a daily H5 file first.")
            return
        self.load_h5(current)

    def _begin_h5_operation(self, label, h5_path=None):
        if self.h5_operation_running:
            raise ValueError("Another current-H5 operation is already running. Please wait for it to finish.")
        if h5_path:
            status, tooltip = self._h5_analysis_status(h5_path)
            if status == "[BUSY]":
                raise ValueError(
                    f"Selected H5 is still locked by another process: {os.path.basename(h5_path)}\n{tooltip}"
                )
        self.h5_operation_running = True
        self.append_log(f"Current-H5 operation started: {label}")

    def _end_h5_operation(self, label):
        self.h5_operation_running = False
        self.append_log(f"Current-H5 operation ended: {label}")

    def _run_write_operation(self, h5_path, label, func, manage_progress=True, track_operation=True):
        if not h5_path:
            raise ValueError("No H5 file selected.")
        started_operation = False
        if track_operation:
            self._begin_h5_operation(label, h5_path)
            started_operation = True
        try:
            reopen = self.h5_path and os.path.abspath(self.h5_path) == os.path.abspath(h5_path)
            if reopen:
                self._close_h5_handle()
            if manage_progress:
                self._start_progress(f"{label} -> {os.path.basename(h5_path)}", busy=True)
            self.append_log(f"{label} -> {os.path.basename(h5_path)}")
            result = run_callable_in_thread(func)
            if manage_progress:
                self._update_progress(f"{label} completed, reloading H5..." if reopen else f"{label} completed", busy=True)
            if reopen and os.path.exists(h5_path):
                self.load_h5(h5_path, manage_progress=manage_progress)
            if manage_progress:
                self._finish_progress(f"{label} completed")
            return result
        finally:
            if started_operation:
                self._end_h5_operation(label)

    def run_extract_features(self):
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            self._run_write_operation(h5_path, "Extract features", lambda: extract_features(h5_path, cfg))
            self.append_log("Feature extraction completed.")
        except Exception as exc:
            self._finish_progress("Extract features failed")
            self._show_error("Extract Features Failed", exc)

    def run_classify_states(self, manage_progress=True, track_operation=True):
        started_operation = False
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            if not h5_path:
                raise ValueError("No H5 file selected.")
            if track_operation:
                self._begin_h5_operation("Classify states", h5_path)
                started_operation = True
            reopen = self.h5_path and os.path.abspath(self.h5_path) == os.path.abspath(h5_path)
            if reopen:
                self._close_h5_handle()
            if manage_progress:
                self._start_progress(f"Preparing state review -> {os.path.basename(h5_path)}", busy=True)
            self.append_log(f"Classify states (interactive review) -> {os.path.basename(h5_path)}")
            completed = run_state_review_wizard(self, h5_path, cfg)
            if completed:
                self.append_log("State classification review completed.")
                if manage_progress:
                    self._update_progress("State review completed, reloading H5..." if reopen else "State review completed", busy=True)
                if reopen and os.path.exists(h5_path):
                    self.load_h5(h5_path, manage_progress=manage_progress)
                if manage_progress:
                    self._finish_progress("State review completed")
                return True
            else:
                self.append_log("State classification review cancelled.")
                if reopen and os.path.exists(h5_path):
                    self.load_h5(h5_path, manage_progress=manage_progress)
                if manage_progress:
                    self._finish_progress("State review cancelled")
                return False
        except Exception as exc:
            if manage_progress:
                self._finish_progress("State classification failed")
            self._show_error("State Classification Failed", exc)
            return False
        finally:
            if started_operation:
                self._end_h5_operation("Classify states")

    def run_summarize_rhythm(self):
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            self._run_write_operation(h5_path, "Summarize rhythm", lambda: summarize_rhythm(h5_path, cfg))
            self.append_log("Rhythm summary completed.")
        except Exception as exc:
            self._finish_progress("Summarize rhythm failed")
            self._show_error("Summarize Rhythm Failed", exc)

    def run_longitudinal_dynamics(self):
        started_operation = False
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            if not h5_path:
                raise ValueError("No H5 file selected.")
            self._begin_h5_operation("Longitudinal dynamics", h5_path)
            started_operation = True
            output_dir = self._resolve_output_dir(allow_create=True)
            self._close_h5_handle()
            self._start_progress(f"Longitudinal dynamics -> {os.path.basename(h5_path)}", busy=True)
            self.append_log(f"Longitudinal dynamics -> {os.path.basename(h5_path)}")
            summary = self._run_background_callable(
                f"Longitudinal dynamics -> {os.path.basename(h5_path)}",
                lambda: run_longitudinal_dynamics_analysis(h5_path, output_dir=output_dir, config=cfg),
            )
            self.load_h5(h5_path, manage_progress=False)
            self.append_log(f"Longitudinal hourly table: {summary.get('hourly_csv', '')}")
            self.append_log(f"Longitudinal effect summary: {summary.get('effect_csv', '')}")
            self._finish_progress("Longitudinal dynamics completed")
        except Exception as exc:
            self._finish_progress("Longitudinal dynamics failed")
            self._show_error("Longitudinal Dynamics Failed", exc)
        finally:
            if started_operation:
                self._end_h5_operation("Longitudinal dynamics")

    def run_generate_report(self):
        started_operation = False
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            if not h5_path:
                raise ValueError("No H5 file selected.")
            self._begin_h5_operation("Generate report", h5_path)
            started_operation = True
            self._close_h5_handle()
            self._start_progress(f"Generate report -> {os.path.basename(h5_path)}", busy=True)
            self.append_log(f"Generate report -> {os.path.basename(h5_path)}")
            self.report_path = self._run_background_callable(
                f"Generate report -> {os.path.basename(h5_path)}",
                lambda: generate_html_report(h5_path, config=cfg),
            )
            self.report_edit.setText(self.report_path)
            self._update_progress("Report generated, reloading H5...", busy=True)
            self.load_h5(h5_path, manage_progress=False)
            self.append_log(f"Report generated: {self.report_path}")
            self._finish_progress("Report generated")
        except Exception as exc:
            self._finish_progress("Generate report failed")
            self._show_error("Generate Report Failed", exc)
        finally:
            if started_operation:
                self._end_h5_operation("Generate report")

    def run_full_analysis_current(self):
        started_operation = False
        try:
            cfg = self._get_config()
            h5_path = self._get_current_h5_path()
            if not h5_path:
                raise ValueError("No H5 file selected.")
            self._begin_h5_operation("Full analysis", h5_path)
            started_operation = True
            self._start_progress(f"Full analysis -> {os.path.basename(h5_path)}", busy=False, maximum=5, value=0)
            self._update_progress("Step 1/5: Extract features", value=1)
            self._run_write_operation(
                h5_path,
                "Extract features",
                lambda: extract_features(h5_path, cfg),
                manage_progress=False,
                track_operation=False,
            )
            self._update_progress("Step 2/5: Interactive state review", value=2)
            if not self.run_classify_states(manage_progress=False, track_operation=False):
                self.append_log("Full analysis stopped because state review was cancelled.")
                self._finish_progress("Full analysis cancelled")
                return
            self._update_progress("Step 3/5: Summarize rhythm", value=3)
            self._run_write_operation(
                h5_path,
                "Summarize rhythm",
                lambda: summarize_rhythm(h5_path, cfg),
                manage_progress=False,
                track_operation=False,
            )
            self._close_h5_handle()
            self._update_progress("Step 4/5: Longitudinal dynamics", value=4)
            output_dir = self._resolve_output_dir(allow_create=True)
            summary = self._run_background_callable(
                f"Longitudinal dynamics -> {os.path.basename(h5_path)}",
                lambda: run_longitudinal_dynamics_analysis(h5_path, output_dir=output_dir, config=cfg),
            )
            self.append_log(f"Longitudinal effect summary: {summary.get('effect_csv', '')}")
            self._update_progress("Step 5/5: Generate report", value=5)
            self.append_log(f"Generate report -> {os.path.basename(h5_path)}")
            self.report_path = self._run_background_callable(
                f"Generate report -> {os.path.basename(h5_path)}",
                lambda: generate_html_report(h5_path, config=cfg),
            )
            self.report_edit.setText(self.report_path)
            self.load_h5(h5_path, manage_progress=False)
            self.append_log("Full analysis completed for current H5.")
            self._finish_progress("Full analysis completed")
        except Exception as exc:
            self._finish_progress("Full analysis failed")
            self._show_error("Full Analysis Failed", exc)
        finally:
            if started_operation:
                self._end_h5_operation("Full analysis")

    def run_full_analysis_all(self):
        started_operation = False
        try:
            if self.slice_build_running:
                raise ValueError("Background slice build is still running. Wait for all natural-day H5 files to finish first.")
            if self.auto_h5_analysis_running:
                raise ValueError("Auto feature/state queue is currently running. Wait for it or pause it before batch full analysis.")
            cfg = self._get_config()
            paths = self._find_related_slice_h5_paths()
            if not paths:
                raise ValueError("No same-slice H5 files were found. Load or select one slice H5 first.")
            busy = []
            for path in paths:
                status, tooltip = self._h5_analysis_status(path)
                if status == "[BUSY]":
                    busy.append(f"{os.path.basename(path)}: {tooltip}")
            if busy:
                raise ValueError("Some H5 files are still locked:\n" + "\n".join(busy[:8]))

            output_dir = self._resolve_output_dir(allow_create=True)
            self._begin_h5_operation("Batch full analysis", None)
            started_operation = True
            self.auto_h5_analysis_paused = True
            total_steps = max(len(paths) * 4 + 1, 1)
            progress_value = 0
            self._start_progress(
                f"Batch full analysis: 0/{len(paths)} H5 files complete",
                busy=False,
                maximum=total_steps,
                value=progress_value,
            )
            self.append_log(f"Batch full analysis started for same-slice H5 files: {len(paths)}")

            for file_idx, h5_path in enumerate(paths, start=1):
                basename = os.path.basename(h5_path)
                self._set_current_h5_path(h5_path)
                self._close_h5_handle()

                progress_value += 1
                if self._h5_dataset_exists(h5_path, "features/band_power"):
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 1/4: Features already exist -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch skip features: {basename}")
                else:
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 1/4: Extract features -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch extract features -> {basename}")
                    run_callable_in_thread(lambda h5_path=h5_path: extract_features(h5_path, cfg))
                    self.refresh_h5_list()

                progress_value += 1
                if self._is_state_review_complete(h5_path):
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 2/4: State review already complete -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch skip state review: {basename}")
                else:
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 2/4: State review popup -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch state review popup -> {basename}")
                    completed = run_state_review_wizard(self, h5_path, cfg)
                    self.refresh_h5_list()
                    if not completed:
                        self.append_log("Batch full analysis stopped because state review was cancelled.")
                        self._finish_progress("Batch full analysis cancelled")
                        return

                progress_value += 1
                if self._h5_dataset_exists(h5_path, "rhythm/hourly_feature_table"):
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 3/4: Rhythm summary already exists -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch skip rhythm summary: {basename}")
                else:
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 3/4: Summarize rhythm -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch summarize rhythm -> {basename}")
                    run_callable_in_thread(lambda h5_path=h5_path: summarize_rhythm(h5_path, cfg))
                    self.refresh_h5_list()

                progress_value += 1
                if self._h5_dataset_exists(h5_path, "longitudinal/effect_numeric_matrix"):
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 4/4: Longitudinal dynamics already exists -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch skip longitudinal dynamics: {basename}")
                else:
                    self._update_progress(
                        f"Batch {file_idx}/{len(paths)} step 4/4: Longitudinal dynamics -> {basename}",
                        value=progress_value,
                    )
                    self.append_log(f"Batch longitudinal dynamics -> {basename}")
                    run_callable_in_thread(
                        lambda h5_path=h5_path: run_longitudinal_dynamics_analysis(
                            h5_path,
                            output_dir=output_dir,
                            config=cfg,
                        )
                    )
                    self.refresh_h5_list()

                self._update_progress(
                    f"Batch full analysis: {file_idx}/{len(paths)} H5 files complete",
                    value=progress_value,
                )

            self.refresh_h5_list()
            self._update_progress("Batch final step: Slice statistics", value=total_steps)
            summary = self._run_slice_statistics_when_ready(auto=True)
            if summary is None:
                raise ValueError("Daily H5 analysis completed, but slice statistics did not finish.")
            if paths:
                self.load_h5(paths[-1], manage_progress=False)
            self.append_log("Batch full analysis completed for all same-slice H5 files.")
            self._finish_progress("Batch full analysis completed")
        except Exception as exc:
            self._finish_progress("Batch full analysis failed")
            self._show_error("Batch Full Analysis Failed", exc)
        finally:
            if started_operation:
                self._end_h5_operation("Batch full analysis")

    def _is_state_review_complete(self, h5_path):
        try:
            with h5py.File(h5_path, "r") as h5f:
                return bool(
                    "states" in h5f
                    and "review" in h5f["states"]
                    and h5f["states/review"].attrs.get("review_complete", False)
                )
        except Exception:
            return False

    def _full_analysis_pipeline(self, h5_path, cfg):
        extract_features(h5_path, cfg)
        classify_states(h5_path, cfg)
        summarize_rhythm(h5_path, cfg)
        self.report_path = generate_html_report(h5_path, config=cfg)
        self.report_edit.setText(self.report_path)
        return h5_path

    def open_report(self):
        report_path = self.report_edit.text().strip() or self.report_path
        if report_path and os.path.exists(report_path):
            webbrowser.open(f"file://{os.path.abspath(report_path)}")
        elif report_path:
            self._show_error("Open Report Failed", "Report path is set, but the file does not exist yet.")

    def load_h5(self, filepath, manage_progress=True):
        self._close_h5_handle()
        try:
            if manage_progress:
                self._start_progress(f"Loading H5 -> {os.path.basename(filepath)}", busy=False, maximum=6, value=0)
            self.h5_file = h5py.File(filepath, "r")
            if manage_progress:
                self._update_progress("Reading H5 metadata...", value=1)
            self._set_current_h5_path(filepath)
            self.refresh_h5_list(preferred_path=filepath)

            meta = self.h5_file["metadata"]
            self.lfp_fs = int(meta.attrs.get("lfp_sample_rate_hz", 1000))
            self.imu_fs = int(meta.attrs.get("imu_target_rate_hz", 100))
            date_str = str(meta.attrs.get("date", "Unknown Date"))
            tz_str = str(meta.attrs.get("timezone", "Asia/Shanghai"))
            self.current_timezone_name = tz_str or "Asia/Shanghai"
            self.total_seconds = float(self.h5_file["time"].attrs.get("lfp_time_s_extent", [0.0, 86400.0])[1])

            try:
                origin_epoch_ms = self.h5_file["time"].attrs.get(
                    "time_origin_epoch_ms",
                    meta.attrs.get("time_origin_epoch_ms", np.nan),
                )
                if np.isfinite(float(origin_epoch_ms)):
                    self.daily_start_ts_ms = float(origin_epoch_ms)
                else:
                    tz = ZoneInfo(tz_str)
                    start_dt = datetime.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz)
                    self.daily_start_ts_ms = start_dt.timestamp() * 1000.0
            except Exception:
                self.daily_start_ts_ms = 0.0

            try:
                self.trial_dict = json.loads(self.h5_file["task"].attrs.get("trial_dict", "{}"))
            except Exception:
                self.trial_dict = {}

            self.report_path = self._guess_report_path(filepath)
            self.report_edit.setText(self.report_path or "")
            if manage_progress:
                self._update_progress("Refreshing available bands...", value=2)
            band_info = self._get_available_band_info()
            self._refresh_summary_band_combo(band_info["active"])

            for axis in [self.overview_axis, self.lfp_axis, self.spec_axis, self.imu_axis, self.masks_axis]:
                axis.set_time_origin(self.daily_start_ts_ms, tz_str, date_str=date_str)
            self._set_summary_time_origin(self.daily_start_ts_ms, tz_str)
            if manage_progress:
                self._update_progress("Rendering overview...", value=3)
            self._render_overview()
            if manage_progress:
                self._update_progress("Rendering detail view...", value=4)
            self._fetch_and_draw_detail()
            if manage_progress:
                self._update_progress("Rendering recording summary...", value=5)
            self._render_rhythm_summary()

            info = (
                f"Date: {date_str}\n"
                f"LFP fs: {self.lfp_fs} Hz\n"
                f"IMU fs: {self.imu_fs} Hz\n"
                f"H5: {filepath}\n\n"
                "=== Analysis Summary ===\n"
                f"{self._get_analysis_summary()}\n"
                "=== H5 Structure ===\n"
                f"{self._get_h5_structure(self.h5_file)}"
            )
            self.lbl_info.setPlainText(info)
            self.append_log(f"Loaded H5: {filepath}")
            if manage_progress:
                self._update_progress("Load complete", value=6)
                self._finish_progress("H5 loaded")
        except Exception as exc:
            self._close_h5_handle()
            if manage_progress:
                self._finish_progress("Load H5 failed")
            if _is_hdf5_lock_error(exc):
                self.append_log(
                    "Selected H5 is currently locked by a writer. Wait until the build/analysis step "
                    "finishes, then refresh and load it again."
                )
                self._show_error(
                    "Load H5 Failed",
                    "This H5 file is still being written or locked by another process. "
                    "Please wait for the current build/analysis step to finish, then refresh the H5 list.",
                )
                return
            self._show_error("Load H5 Failed", exc)

    def _guess_report_path(self, h5_path):
        cfg = self._get_config()
        report_dir = cfg.get("report.report_output_dir", "") or os.path.dirname(h5_path)
        basename = os.path.splitext(os.path.basename(h5_path))[0]
        return os.path.join(report_dir, f"{basename}_report.html")

    def _get_analysis_summary(self):
        if self.h5_file is None:
            return "No H5 loaded."
        lines = []
        band_info = self._get_available_band_info()
        lines.append(
            f"Bands ({band_info['source']}): {band_info['active'] if band_info['active'] else 'none'}"
        )
        if band_info["mismatch"]:
            lines.append(
                "Band mismatch detected: current config and H5 analysis bands differ. "
                "Please rerun Extract Features -> Classify States -> Summarize Rhythm."
            )
        if "feature_time_s" in self.h5_file["time"]:
            lines.append(f"Feature windows: {self.h5_file['time/feature_time_s'].shape[0]}")
        if "state_label" in self.h5_file["states"]:
            labels = np.asarray(self.h5_file["states/state_label"][:], dtype=np.int16)
            state_names = (
                [str(v) for v in self.h5_file["states/state_names"][:].astype(str)]
                if "state_names" in self.h5_file["states"]
                else []
            )
            unique, counts = np.unique(labels, return_counts=True)
            label_summary = {}
            for code, count in zip(unique.tolist(), counts.tolist()):
                if 0 <= int(code) < len(state_names):
                    label_summary[state_names[int(code)]] = int(count)
                else:
                    label_summary[int(code)] = int(count)
            lines.append(f"State counts: {label_summary}")
        if "hourly_feature_table" in self.h5_file["rhythm"]:
            lines.append(f"Hourly bins: {self.h5_file['rhythm/hourly_feature_table'].shape[0]}")
        if "state_stratified_feature_table_normalized" in self.h5_file["rhythm"]:
            lines.append("State-normalized hourly power: available")
        elif "hourly_feature_table" in self.h5_file["rhythm"]:
            lines.append("State-normalized hourly power: missing, please rerun Summarize Rhythm")
        if "working_mask" in self.h5_file["task"]:
            lines.append(f"Working fraction: {np.mean(self.h5_file['task/working_mask'][:]) * 100.0:.2f}%")
        return "\n".join(lines) if lines else "No analysis outputs found yet."

    def _get_h5_structure(self, group, prefix=""):
        struct = ""
        for key, value in group.attrs.items():
            struct += f"{prefix} • {key}: {value}\n"
        for key, item in group.items():
            if isinstance(item, h5py.Dataset):
                struct += f"{prefix}📄 {key}: {item.shape} ({item.dtype})\n"
            else:
                struct += f"{prefix}📁 {key}/\n"
                struct += self._get_h5_structure(item, prefix + "  ")
        return struct

    def _state_color_map(self):
        return {
            "Wake": "#2f80ed",
            "MiniWake": "#7f7f7f",
            "NREM": "#f2c94c",
            "REM": "#eb5757",
            "Working": "#9b51e0",
        }

    def _band_colors(self):
        return [
            "#1f77b4",
            "#ff7f0e",
            "#2ca02c",
            "#d62728",
            "#9467bd",
            "#8c564b",
            "#e377c2",
            "#7f7f7f",
        ]

    def _mean_sem(self, values):
        values = np.asarray(values, dtype=np.float64)
        valid_count = np.sum(np.isfinite(values), axis=1)
        mean = np.divide(
            np.nansum(values, axis=1),
            valid_count,
            out=np.full(values.shape[0], np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        centered = values - mean[:, np.newaxis]
        variance = np.divide(
            np.nansum(centered ** 2, axis=1),
            np.maximum(valid_count - 1, 1),
            out=np.full(values.shape[0], np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        sem = np.divide(
            np.sqrt(np.maximum(variance, 0.0)),
            np.sqrt(np.maximum(valid_count, 1)),
            out=np.full(values.shape[0], np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
        return mean, sem

    def _get_average_channel_groups(self, n_channels):
        attrs = self.h5_file["rhythm"].attrs if self.h5_file is not None and "rhythm" in self.h5_file else {}
        groups_json = attrs.get("average_channel_groups_json", "")
        names_json = attrs.get("average_group_names_json", "")
        try:
            groups = json.loads(groups_json) if groups_json else None
        except Exception:
            groups = None
        try:
            names = json.loads(names_json) if names_json else None
        except Exception:
            names = None
        if groups is None:
            cfg = self._get_config()
            groups = cfg.get("rhythm.average_channel_groups", [list(range(n_channels))])
            names = cfg.get("rhythm.average_group_names", ["All channels"])

        normalized_groups = []
        for group in groups or []:
            if not isinstance(group, (list, tuple)):
                continue
            valid = []
            for value in group:
                try:
                    idx = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < n_channels and idx not in valid:
                    valid.append(idx)
            if valid:
                normalized_groups.append(valid)
        if not normalized_groups:
            normalized_groups = [list(range(n_channels))]

        normalized_names = []
        for idx, group in enumerate(normalized_groups):
            if idx < len(names or []) and str(names[idx]).strip():
                normalized_names.append(str(names[idx]).strip())
            else:
                normalized_names.append(f"group_{idx}")
        return normalized_groups, normalized_names

    def _refresh_summary_band_combo(self, band_names):
        if not hasattr(self, "summary_band_combo"):
            return
        current_text = self.summary_band_combo.currentText()
        self.summary_band_combo.blockSignals(True)
        self.summary_band_combo.clear()
        self.summary_band_combo.addItems(list(band_names))
        if current_text in band_names:
            self.summary_band_combo.setCurrentText(current_text)
        self.summary_band_combo.blockSignals(False)

    def _get_state_norm_group_options(self, n_channels):
        group_defs, group_names = self._get_average_channel_groups(n_channels)
        options = [("All channels", list(range(n_channels)))]
        for name, group in zip(group_names, group_defs):
            normalized_group = []
            for value in group:
                try:
                    idx = int(value)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < n_channels and idx not in normalized_group:
                    normalized_group.append(idx)
            if not normalized_group:
                continue
            if normalized_group == options[0][1]:
                continue
            options.append((str(name), normalized_group))
        return options

    def _refresh_summary_group_combo(self, n_channels):
        if not hasattr(self, "summary_group_combo"):
            return []
        options = self._get_state_norm_group_options(n_channels)
        current_text = self.summary_group_combo.currentText()
        self.summary_group_combo.blockSignals(True)
        self.summary_group_combo.clear()
        self.summary_group_combo.addItems([label for label, _ in options])
        if current_text:
            idx = next((i for i, (label, _) in enumerate(options) if label == current_text), -1)
            if idx >= 0:
                self.summary_group_combo.setCurrentIndex(idx)
        self.summary_group_combo.blockSignals(False)
        return options

    def _get_available_band_info(self):
        config_band_names = list(self._get_config().get("features.bands", {}).keys())
        feature_band_names = []
        rhythm_band_names = []
        if self.h5_file is not None:
            if "features" in self.h5_file and "feature_band_names" in self.h5_file["features"]:
                feature_band_names = [
                    str(v) for v in self.h5_file["features/feature_band_names"][:].astype(str)
                ]
            if "rhythm" in self.h5_file and "band_names" in self.h5_file["rhythm"]:
                rhythm_band_names = [
                    str(v) for v in self.h5_file["rhythm/band_names"][:].astype(str)
                ]

        if rhythm_band_names:
            active_band_names = rhythm_band_names
            active_source = "rhythm"
        elif feature_band_names:
            active_band_names = feature_band_names
            active_source = "features"
        else:
            active_band_names = config_band_names
            active_source = "config"

        mismatch = False
        if config_band_names:
            if feature_band_names and feature_band_names != config_band_names:
                mismatch = True
            if rhythm_band_names and rhythm_band_names != config_band_names:
                mismatch = True

        return {
            "active": active_band_names,
            "source": active_source,
            "config": config_band_names,
            "features": feature_band_names,
            "rhythm": rhythm_band_names,
            "mismatch": mismatch,
        }

    def _plot_channel_bundle(self, plot_widget, x_hours, values, channel_labels, group_names, groups):
        n_channels = values.shape[1]
        for ch_idx in range(n_channels):
            channel_values = values[:, ch_idx]
            if not np.isfinite(channel_values).any():
                continue
            color = pg.intColor(ch_idx, hues=max(n_channels, 1), alpha=128)
            plot_widget.plot(
                x=x_hours,
                y=channel_values,
                pen=pg.mkPen(color, width=1),
            )

        mean_values, sem_values = self._mean_sem(values)
        if np.isfinite(mean_values).any():
            upper = mean_values + sem_values
            lower = mean_values - sem_values
            upper_item = plot_widget.plot(x=x_hours, y=upper, pen=pg.mkPen((0, 0, 0, 0)))
            lower_item = plot_widget.plot(x=x_hours, y=lower, pen=pg.mkPen((0, 0, 0, 0)))
            fill = pg.FillBetweenItem(upper_item, lower_item, brush=pg.mkBrush(0, 0, 0, 45))
            plot_widget.addItem(fill)
            plot_widget.plot(
                x=x_hours,
                y=mean_values,
                pen=pg.mkPen("#000000", width=3),
                name="Mean across channels",
            )

        line_styles = [
            Qt.PenStyle.SolidLine,
            Qt.PenStyle.DashLine,
            Qt.PenStyle.DotLine,
            Qt.PenStyle.DashDotLine,
            Qt.PenStyle.DashDotDotLine,
        ]
        group_colors = ["#111111", "#aa3377", "#117733", "#4477aa", "#cc7722"]
        all_channels = list(range(n_channels))
        for idx, group in enumerate(groups):
            if group == all_channels and len(groups) == 1:
                continue
            group_values = values[:, group]
            group_mean, _ = self._mean_sem(group_values)
            if not np.isfinite(group_mean).any():
                continue
            plot_widget.plot(
                x=x_hours,
                y=group_mean,
                pen=pg.mkPen(
                    group_colors[idx % len(group_colors)],
                    width=3,
                    style=line_styles[idx % len(line_styles)],
                ),
                name=group_names[idx],
            )

    def _reset_legend(self, plot_widget):
        legend = plot_widget.plotItem.legend
        if legend is not None:
            legend.scene().removeItem(legend)
            plot_widget.plotItem.legend = None
        plot_widget.addLegend(offset=(12, 12))

    def _plot_state_occupancy_stacked(self, plot_widget, x_hours, state_occupancy, state_names):
        colors = self._state_color_map()
        if x_hours.size > 1:
            width = max(0.2, float(np.nanmedian(np.diff(x_hours))) * 0.85)
        else:
            width = 0.8
        bottom = np.zeros_like(x_hours, dtype=np.float64)
        for state_idx, state_name in enumerate(state_names):
            if state_idx >= state_occupancy.shape[1]:
                continue
            values = np.nan_to_num(state_occupancy[:, state_idx], nan=0.0)
            brush = pg.mkBrush(colors.get(state_name, "#666666"))
            bar_item = pg.BarGraphItem(x=x_hours, y0=bottom, height=values, width=width, brush=brush, pen=pg.mkPen(None))
            plot_widget.addItem(bar_item)
            plot_widget.plot([], [], pen=pg.mkPen(colors.get(state_name, "#666666"), width=6), name=state_name)
            bottom = bottom + values

    def _load_rt_summary_for_paths(self, paths, global_origin_ms=None, x_hours=None):
        paths = [os.path.abspath(str(path)) for path in paths or [] if path]
        if not paths:
            return None
        source_root = ""
        origins = []
        end_candidates = []
        for path in paths:
            try:
                with h5py.File(path, "r") as h5f:
                    meta = h5f["metadata"].attrs if "metadata" in h5f else {}
                    if not source_root:
                        source_root = str(meta.get("slice_source_root", ""))
                    origin = float(h5f["time"].attrs.get("time_origin_epoch_ms", meta.get("time_origin_epoch_ms", np.nan)))
                    if np.isfinite(origin):
                        origins.append(origin)
                        if "time" in h5f and "hourly_time_s" in h5f["time"]:
                            centers = np.asarray(h5f["time/hourly_time_s"][:], dtype=np.float64)
                            if centers.size:
                                end_candidates.append(origin + (float(np.nanmax(centers)) + 1800.0) * 1000.0)
            except Exception:
                continue
        if not source_root:
            return None
        try:
            sd_card_dir = resolve_sd_card_dir_from_source_root(source_root)
        except Exception:
            return None
        if global_origin_ms is None or not np.isfinite(float(global_origin_ms)):
            if not origins:
                return None
            start_ms = float(min(origins))
        else:
            start_ms = float(global_origin_ms)
        if x_hours is not None:
            x = np.asarray(x_hours, dtype=np.float64)
            if x.size and np.isfinite(x).any():
                end_ms = start_ms + (float(np.nanmax(x)) + 0.5) * 3600000.0
            else:
                end_ms = max(end_candidates) if end_candidates else start_ms + 3600000.0
        else:
            end_ms = max(end_candidates) if end_candidates else start_ms + 3600000.0
        if end_ms <= start_ms:
            return None
        try:
            records, fit, warnings = parse_rt_trials(
                sd_card_dir,
                slice_start_ms=start_ms,
                slice_end_ms=end_ms,
                config=self._get_config().get("behavior.rt", {}),
            )
            hourly = compute_hourly_rt_features(records, start_ms, end_ms)
        except Exception:
            return None
        out = dict(hourly)
        out["x_hours"] = np.asarray(hourly["hourly_time_s"], dtype=np.float64) / 3600.0
        out["records"] = records
        out["fit"] = fit
        out["warnings"] = warnings
        out["source_root"] = source_root
        out["n_valid_rt"] = int(fit.get("n_valid_rt", 0)) if isinstance(fit, dict) else 0
        return out

    def _connectivity_candidate_dirs_for_h5(self, h5_path):
        candidates = []
        for value in [
            self.output_edit.text().strip() if hasattr(self, "output_edit") else "",
            os.path.dirname(os.path.abspath(str(h5_path))) if h5_path else "",
        ]:
            if value:
                value = os.path.abspath(value)
                if value not in candidates:
                    candidates.append(value)
        return candidates

    def _load_connectivity_summary_for_paths(self, paths, global_origin_ms=None):
        paths = [os.path.abspath(str(path)) for path in paths or [] if path]
        summaries = []
        skipped = 0
        band_names = []
        pac_labels = []
        for path in paths:
            sidecar = None
            for output_dir in self._connectivity_candidate_dirs_for_h5(path):
                try:
                    sidecar = load_connectivity_sidecar(path, output_dir)
                except Exception:
                    sidecar = None
                if sidecar is not None:
                    break
            if sidecar is None:
                skipped += 1
                continue
            meta = sidecar.get("metadata", {})
            if not band_names:
                band_names = [str(name) for name in meta.get("band_names", [])]
            if not pac_labels:
                pac_labels = [
                    f"{pair.get('phase_band', 'phase')}->{pair.get('amplitude_band', 'amp')}"
                    for pair in meta.get("pac_band_pairs", [])
                ]
            x_epoch = np.asarray(sidecar.get("hour_center_epoch_ms", []), dtype=np.float64)
            if global_origin_ms is None or not np.isfinite(float(global_origin_ms)):
                origin = float(np.nanmin(x_epoch)) if x_epoch.size and np.isfinite(x_epoch).any() else np.nan
            else:
                origin = float(global_origin_ms)
            x_hours = (x_epoch - origin) / 3600000.0
            plv = np.asarray(sidecar.get("plv", np.empty((0, 0, 0, 0))), dtype=np.float64)
            pac = np.asarray(sidecar.get("pac", np.empty((0, 0, 0, 0))), dtype=np.float64)
            plv_mean = self._connectivity_pair_mean(plv, off_diagonal=True)
            pac_mean = self._connectivity_pair_mean(pac, off_diagonal=False)
            summaries.append(
                {
                    "x_hours": x_hours,
                    "plv_mean": plv_mean,
                    "pac_mean": pac_mean,
                    "valid_fraction": np.asarray(sidecar.get("valid_fraction", []), dtype=np.float64),
                }
            )
        if not summaries:
            return {"available": False, "n_loaded": 0, "n_skipped": skipped}
        x_hours = np.concatenate([item["x_hours"] for item in summaries])
        order = np.argsort(x_hours)
        out = {
            "available": True,
            "x_hours": x_hours[order],
            "band_names": band_names,
            "pac_labels": pac_labels,
            "n_loaded": len(summaries),
            "n_skipped": skipped,
        }
        if summaries[0]["plv_mean"].size:
            out["plv_mean"] = np.concatenate([item["plv_mean"] for item in summaries], axis=0)[order]
        if summaries[0]["pac_mean"].size:
            out["pac_mean"] = np.concatenate([item["pac_mean"] for item in summaries], axis=0)[order]
        return out

    def _connectivity_pair_mean(self, values, off_diagonal=False):
        arr = np.asarray(values, dtype=np.float64)
        if arr.ndim != 4 or arr.shape[0] == 0:
            return np.empty((0, 0), dtype=np.float64)
        mask = np.ones(arr.shape[-2:], dtype=bool)
        if off_diagonal and mask.shape[0] == mask.shape[1]:
            np.fill_diagonal(mask, False)
        flat = arr[..., mask]
        valid = np.isfinite(flat)
        counts = np.sum(valid, axis=-1)
        return np.divide(
            np.nansum(flat, axis=-1),
            counts,
            out=np.full(counts.shape, np.nan, dtype=np.float64),
            where=counts > 0,
        )

    def _plot_connectivity_summary(self, connectivity_summary, x_max=None):
        self.plot_connectivity.clear()
        legend = self.plot_connectivity.plotItem.legend
        if legend is not None:
            legend.scene().removeItem(legend)
            self.plot_connectivity.plotItem.legend = None
        if not connectivity_summary or not connectivity_summary.get("available"):
            skipped = int((connectivity_summary or {}).get("n_skipped", 0))
            title = "Hourly PLV/PAC Summary (sidecar missing"
            title += f"; skipped {skipped} H5" if skipped else ""
            title += ")"
            self.plot_connectivity.setTitle(title)
            self.plot_connectivity.getAxis("left").setTicks([])
            return
        self._reset_legend(self.plot_connectivity)
        x_hours = np.asarray(connectivity_summary.get("x_hours", []), dtype=np.float64)
        plv_mean = np.asarray(connectivity_summary.get("plv_mean", np.empty((0, 0))), dtype=np.float64)
        pac_mean = np.asarray(connectivity_summary.get("pac_mean", np.empty((0, 0))), dtype=np.float64)
        colors = ["#1f77b4", "#2ca02c", "#d62728", "#9467bd", "#8c564b", "#17becf"]
        for band_idx, band_name in enumerate(connectivity_summary.get("band_names", [])):
            if band_idx >= plv_mean.shape[1]:
                continue
            self.plot_connectivity.plot(
                x=x_hours,
                y=plv_mean[:, band_idx],
                pen=pg.mkPen(colors[band_idx % len(colors)], width=2),
                name=f"PLV {band_name}",
            )
        for pair_idx, label in enumerate(connectivity_summary.get("pac_labels", [])):
            if pair_idx >= pac_mean.shape[1]:
                continue
            self.plot_connectivity.plot(
                x=x_hours,
                y=pac_mean[:, pair_idx],
                pen=pg.mkPen(
                    colors[(pair_idx + 3) % len(colors)],
                    width=2,
                    style=Qt.PenStyle.DashLine,
                ),
                name=f"PAC {label}",
            )
        if x_max is None:
            x_max = max(24.0, float(np.nanmax(x_hours)) + 0.5 if x_hours.size else 24.0)
        self._add_night_shading(self.plot_connectivity, x_max, x_min=0.0)
        self.plot_connectivity.setXRange(0, x_max, padding=0)
        self.plot_connectivity.setTitle(
            "Hourly PLV/PAC Summary (PLV off-diagonal mean; PAC all-pair mean)"
        )

    def _render_behavior_summary_plot(self, behavior_bundle=None):
        self.plot_behavior.clear()
        legend = self.plot_behavior.plotItem.legend
        if legend is not None:
            legend.scene().removeItem(legend)
            self.plot_behavior.plotItem.legend = None
        self.plot_behavior.getAxis("left").setTicks([[(0, "Rule"), (1, "Perf"), (2, "Bias"), (3, "RT")]])
        self.plot_behavior.setYRange(-0.6, 3.6, padding=0)
        if behavior_bundle is not None:
            x_hours = np.asarray(behavior_bundle.get("x_hours", np.asarray([])), dtype=np.float64)
            if x_hours.size == 0:
                self.plot_behavior.setTitle("Hourly Behavior State (continuous slice; empty)")
                return
            edges_h = behavior_bundle.get("edges_h")
            rule = np.asarray(behavior_bundle.get("hourly_rule", np.full(x_hours.shape, -1)), dtype=np.int16)
            perf_state = np.asarray(behavior_bundle.get("hourly_perf_state", np.full(x_hours.shape, -1)), dtype=np.int8)
            bias_state = np.asarray(behavior_bundle.get("hourly_bias_state", np.full(x_hours.shape, -1)), dtype=np.int8)
            perf_pct = np.asarray(behavior_bundle.get("hourly_perf_pct", np.full(x_hours.shape, np.nan)), dtype=np.float64)
            bias_score = np.asarray(behavior_bundle.get("hourly_bias_score", np.full(x_hours.shape, np.nan)), dtype=np.float64)
            valid_behavior_hours = np.asarray(
                behavior_bundle.get("hour_valid_mask", np.ones(x_hours.shape, dtype=bool)),
                dtype=bool,
            )
            rt_summary = behavior_bundle.get("rt_summary")
            title = "Hourly Behavior State (continuous slice)"
        elif self.h5_file is None or "behavior" not in self.h5_file:
            self.plot_behavior.setTitle("Hourly Behavior State (no behavior data)")
            return
        else:
            behavior = self.h5_file["behavior"]
            if "hourly_time_s" not in behavior:
                self.plot_behavior.setTitle("Hourly Behavior State (missing hourly datasets)")
                return
            x_hours = np.asarray(behavior["hourly_time_s"][:], dtype=np.float64) / 3600.0
            if x_hours.size == 0:
                self.plot_behavior.setTitle("Hourly Behavior State (empty)")
                return
            edges_h = (
                np.asarray(behavior["hourly_edges_s"][:], dtype=np.float64) / 3600.0
                if "hourly_edges_s" in behavior
                else None
            )
            rule = np.asarray(behavior["hourly_rule"][:], dtype=np.int16) if "hourly_rule" in behavior else np.full(x_hours.shape, -1)
            perf_state = (
                np.asarray(behavior["hourly_perf_state"][:], dtype=np.int8)
                if "hourly_perf_state" in behavior
                else np.full(x_hours.shape, -1)
            )
            bias_state = (
                np.asarray(behavior["hourly_bias_state"][:], dtype=np.int8)
                if "hourly_bias_state" in behavior
                else np.full(x_hours.shape, -1)
            )
            perf_pct = (
                np.asarray(behavior["hourly_perf_pct"][:], dtype=np.float64)
                if "hourly_perf_pct" in behavior
                else np.full(x_hours.shape, np.nan)
            )
            bias_score = (
                np.asarray(behavior["hourly_bias_score"][:], dtype=np.float64)
                if "hourly_bias_score" in behavior
                else np.full(x_hours.shape, np.nan)
            )
            valid_behavior_hours = np.ones(x_hours.shape, dtype=bool)
            if "rhythm" in self.h5_file and "hour_valid_mask" in self.h5_file["rhythm"]:
                valid_behavior_hours = np.asarray(self.h5_file["rhythm/hour_valid_mask"][:], dtype=bool)
            origin_ms = float(
                self.h5_file["time"].attrs.get(
                    "time_origin_epoch_ms",
                    self.h5_file["metadata"].attrs.get("time_origin_epoch_ms", self.daily_start_ts_ms)
                    if "metadata" in self.h5_file
                    else self.daily_start_ts_ms,
                )
            )
            rt_summary = self._load_rt_summary_for_paths([self.h5_path], global_origin_ms=origin_ms, x_hours=x_hours)
            title = "Hourly Behavior State"

        if valid_behavior_hours.shape != x_hours.shape:
            valid_behavior_hours = np.ones(x_hours.shape, dtype=bool)
        rule = np.array(rule, copy=True)
        perf_state = np.array(perf_state, copy=True)
        bias_state = np.array(bias_state, copy=True)
        perf_pct = np.array(perf_pct, copy=True)
        bias_score = np.array(bias_score, copy=True)
        rule[~valid_behavior_hours] = -1
        perf_state[~valid_behavior_hours] = -1
        bias_state[~valid_behavior_hours] = -1
        perf_pct[~valid_behavior_hours] = np.nan
        bias_score[~valid_behavior_hours] = np.nan

        self._reset_legend(self.plot_behavior)
        if edges_h is not None and len(edges_h) == x_hours.size + 1:
            widths = np.maximum(np.diff(edges_h) * 0.9, 0.05)
        elif x_hours.size > 1:
            widths = np.full(x_hours.shape, max(0.2, float(np.nanmedian(np.diff(x_hours))) * 0.85))
        else:
            widths = np.full(x_hours.shape, 0.8)

        row_specs = [
            (0, rule, {-1: "#d0d0d0", 0: "#4c78a8", 1: "#f58518", 2: "#54a24b"}, RULE_LABELS),
            (1, perf_state, {-1: "#d0d0d0", 0: "#d64f4f", 1: "#e6b450", 2: "#4daf7c"}, PERF_STATE_LABELS),
            (2, bias_state, {-1: "#d0d0d0", 0: "#4c78a8", 1: "#909090", 2: "#b45ac9"}, BIAS_STATE_LABELS),
        ]
        for row_y, values, color_map, labels in row_specs:
            for x, width, value, is_valid in zip(x_hours, widths, values, valid_behavior_hours):
                if not is_valid:
                    continue
                code = int(value)
                brush = pg.mkBrush(color_map.get(code, "#d0d0d0"))
                bar = pg.BarGraphItem(
                    x=[float(x)],
                    y0=[row_y - 0.35],
                    height=[0.7],
                    width=[float(width)],
                    brush=brush,
                    pen=pg.mkPen("#ffffff", width=0.5),
                )
                self.plot_behavior.addItem(bar)
            for code, label in labels.items():
                if code == -1:
                    continue
                self.plot_behavior.plot(
                    [],
                    [],
                    pen=pg.mkPen(color_map.get(code, "#666666"), width=6),
                    name=str(label).replace("_", " "),
                )

        if np.isfinite(perf_pct).any():
            y_perf = 1.0 + ((np.clip(perf_pct, 0.0, 100.0) / 100.0) - 0.5) * 0.7
            self.plot_behavior.plot(
                x=x_hours,
                y=y_perf,
                pen=pg.mkPen("#1b7837", width=2),
                name="perf %",
            )
        if np.isfinite(bias_score).any():
            y_bias = 2.0 + np.clip(bias_score, -1.0, 1.0) * 0.35
            self.plot_behavior.plot(
                x=x_hours,
                y=y_bias,
                pen=pg.mkPen("#762a83", width=2),
                name="bias score",
            )
        if rt_summary is not None:
            rt_x = np.asarray(rt_summary.get("x_hours", []), dtype=np.float64)
            rt_class = np.asarray(rt_summary.get("hourly_dominant_rt_class", np.full(rt_x.shape, -1)), dtype=np.int8)
            rt_median = np.asarray(rt_summary.get("hourly_rt_median_ms", np.full(rt_x.shape, np.nan)), dtype=np.float64)
            rt_widths = np.full(rt_x.shape, 0.8)
            if rt_x.size > 1:
                rt_widths[:] = max(0.2, float(np.nanmedian(np.diff(rt_x))) * 0.85)
            color_map = {-1: "#d0d0d0", 0: "#4daf7c", 1: "#f0c35a", 2: "#d6604d"}
            for x, width, value in zip(rt_x, rt_widths, rt_class):
                code = int(value)
                if code < 0:
                    continue
                self.plot_behavior.addItem(
                    pg.BarGraphItem(
                        x=[float(x)],
                        y0=[2.65],
                        height=[0.7],
                        width=[float(width)],
                        brush=pg.mkBrush(color_map.get(code, "#d0d0d0")),
                        pen=pg.mkPen("#ffffff", width=0.5),
                    )
                )
            for code in [0, 1, 2]:
                self.plot_behavior.plot(
                    [],
                    [],
                    pen=pg.mkPen(color_map.get(code, "#666666"), width=6),
                    name=f"RT {RT_CLASS_LABELS.get(code, code)}",
                )
            finite = rt_median[np.isfinite(rt_median)]
            if finite.size:
                lo, hi = np.nanpercentile(finite, [5.0, 95.0])
                if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
                    lo, hi = float(np.nanmin(finite)), float(np.nanmax(finite))
                denom = hi - lo if hi > lo else 1.0
                y_rt = 3.0 + ((np.clip(rt_median, lo, hi) - lo) / denom - 0.5) * 0.7
                self.plot_behavior.plot(
                    x=rt_x,
                    y=y_rt,
                    pen=pg.mkPen("#8c510a", width=2),
                    name="RT median",
                )

        x_max = max(24.0, float(np.nanmax(x_hours)) + 0.5 if x_hours.size else 24.0)
        self._add_night_shading(self.plot_behavior, x_max, x_min=0.0)
        self.plot_behavior.setXRange(0, x_max, padding=0)
        self.plot_behavior.setTitle(title)

    def _behavior_summary_lines(self, behavior_bundle=None):
        if behavior_bundle is not None:
            lines = [
                (
                    "Behavior (continuous slice): "
                    f"h5_days={behavior_bundle.get('n_files', 0)}, "
                    f"trials={int(behavior_bundle.get('trial_count', 0))}, "
                    f"valid_perf_trials={int(behavior_bundle.get('valid_perf_count', 0))}, "
                    f"choice_trials={int(behavior_bundle.get('choice_count', 0))}"
                )
            ]
            if "hourly_rule" in behavior_bundle:
                lines.append(f"Behavior rule hours: {self._label_counts(behavior_bundle['hourly_rule'], RULE_LABELS)}")
            if "hourly_perf_state" in behavior_bundle:
                lines.append(f"Behavior perf hours: {self._label_counts(behavior_bundle['hourly_perf_state'], PERF_STATE_LABELS)}")
            if "hourly_bias_state" in behavior_bundle:
                lines.append(f"Behavior bias hours: {self._label_counts(behavior_bundle['hourly_bias_state'], BIAS_STATE_LABELS)}")
            rt_summary = behavior_bundle.get("rt_summary")
            if rt_summary is not None:
                fit = rt_summary.get("fit", {}) if isinstance(rt_summary.get("fit", {}), dict) else {}
                thresholds = fit.get("thresholds_ms", [])
                threshold_text = ", ".join(f"{float(value):.0f} ms" for value in thresholds) if thresholds else "not fitted"
                lines.append(
                    "Behavior RT: "
                    f"valid_rt={int(rt_summary.get('n_valid_rt', 0))}, "
                    f"class_thresholds={threshold_text}, "
                    f"class_hours={self._label_counts(rt_summary.get('hourly_dominant_rt_class', []), RT_CLASS_LABELS)}"
                )
            return lines
        if self.h5_file is None or "behavior" not in self.h5_file:
            return ["Behavior: no behavior data in this H5."]
        behavior = self.h5_file["behavior"]
        trial_count = int(behavior["trial_time_s"].shape[0]) if "trial_time_s" in behavior else 0
        correct_flag = (
            np.asarray(behavior["correct_flag"][:], dtype=np.int8)
            if "correct_flag" in behavior
            else np.zeros((0,), dtype=np.int8)
        )
        inferred_choice = (
            np.asarray(behavior["inferred_choice"][:], dtype=np.int16)
            if "inferred_choice" in behavior
            else np.zeros((0,), dtype=np.int16)
        )
        lines = [
            (
                "Behavior: "
                f"trials={trial_count}, "
                f"valid_perf_trials={int(np.sum(correct_flag >= 0))}, "
                f"choice_trials={int(np.sum(np.isin(inferred_choice, [1, 2])))}"
            )
        ]
        if "hourly_rule" in behavior:
            lines.append(f"Behavior rule hours: {self._label_counts(behavior['hourly_rule'][:], RULE_LABELS)}")
        if "hourly_perf_state" in behavior:
            lines.append(f"Behavior perf hours: {self._label_counts(behavior['hourly_perf_state'][:], PERF_STATE_LABELS)}")
        if "hourly_bias_state" in behavior:
            lines.append(f"Behavior bias hours: {self._label_counts(behavior['hourly_bias_state'][:], BIAS_STATE_LABELS)}")
        if "hourly_fill_source" in behavior:
            lines.append(f"Behavior fill source: {self._label_counts(behavior['hourly_fill_source'][:], FILL_SOURCE_LABELS)}")
        rt_summary = None
        try:
            origin_ms = float(
                self.h5_file["time"].attrs.get(
                    "time_origin_epoch_ms",
                    self.h5_file["metadata"].attrs.get("time_origin_epoch_ms", self.daily_start_ts_ms)
                    if "metadata" in self.h5_file
                    else self.daily_start_ts_ms,
                )
            )
            x_hours = (
                np.asarray(behavior["hourly_time_s"][:], dtype=np.float64) / 3600.0
                if "hourly_time_s" in behavior
                else None
            )
            rt_summary = self._load_rt_summary_for_paths([self.h5_path], global_origin_ms=origin_ms, x_hours=x_hours)
        except Exception:
            rt_summary = None
        if rt_summary is not None:
            fit = rt_summary.get("fit", {}) if isinstance(rt_summary.get("fit", {}), dict) else {}
            thresholds = fit.get("thresholds_ms", [])
            threshold_text = ", ".join(f"{float(value):.0f} ms" for value in thresholds) if thresholds else "not fitted"
            lines.append(
                "Behavior RT: "
                f"valid_rt={int(rt_summary.get('n_valid_rt', 0))}, "
                f"class_thresholds={threshold_text}, "
                f"class_hours={self._label_counts(rt_summary.get('hourly_dominant_rt_class', []), RT_CLASS_LABELS)}"
            )
        return lines

    def _label_counts(self, values, labels):
        values = np.asarray(values, dtype=np.int64)
        if values.size == 0:
            return {}
        unique, counts = np.unique(values, return_counts=True)
        return {
            str(labels.get(int(code), int(code))): int(count)
            for code, count in zip(unique.tolist(), counts.tolist())
        }

    def _summary_reference_scope(self):
        combo = getattr(self, "summary_reference_combo", None)
        if combo is None or combo.count() <= 0:
            return "daily"
        value = combo.currentData()
        return str(value or "daily")

    def _normalize_hourly_feature_table(self, hourly_feature, hourly_count):
        feature = np.asarray(hourly_feature, dtype=np.float64)
        count = np.asarray(hourly_count, dtype=np.float64)
        weighted_sum = np.nansum(feature * count, axis=0)
        total_count = np.sum(count, axis=0)
        reference = np.divide(
            weighted_sum,
            total_count,
            out=np.full(weighted_sum.shape, np.nan, dtype=np.float64),
            where=total_count > 0,
        )
        return np.divide(
            feature,
            reference[np.newaxis, ...],
            out=np.full_like(feature, np.nan, dtype=np.float64),
            where=np.isfinite(reference[np.newaxis, ...]) & (reference[np.newaxis, ...] > 0),
        )

    def _normalize_state_stratified_table(self, state_stratified, state_count):
        values = np.asarray(state_stratified, dtype=np.float64)
        count = np.asarray(state_count, dtype=np.float64)
        weighted_sum = np.nansum(values * count, axis=0)
        total_count = np.sum(count, axis=0)
        reference = np.divide(
            weighted_sum,
            total_count,
            out=np.full(weighted_sum.shape, np.nan, dtype=np.float64),
            where=total_count > 0,
        )
        return np.divide(
            values,
            reference[np.newaxis, ...],
            out=np.full_like(values, np.nan, dtype=np.float64),
            where=np.isfinite(reference[np.newaxis, ...]) & (reference[np.newaxis, ...] > 0),
        )

    def _get_light_schedule(self):
        cfg = self._get_config()
        return (
            float(cfg.get("general.light_on_hour", 6.0)),
            float(cfg.get("general.light_off_hour", 18.0)),
        )

    def _set_summary_time_origin(self, origin_epoch_ms=None, timezone_name=None):
        if origin_epoch_ms is not None:
            try:
                origin_value = float(origin_epoch_ms)
                if np.isfinite(origin_value):
                    self.summary_time_origin_epoch_ms = origin_value
            except Exception:
                pass
        if timezone_name:
            self.current_timezone_name = str(timezone_name)
        for axis_name in [
            "summary_hourly_axis",
            "summary_state_occ_axis",
            "summary_state_norm_axis",
            "summary_behavior_axis",
            "summary_connectivity_axis",
        ]:
            axis = getattr(self, axis_name, None)
            if axis is not None:
                axis.set_time_origin(self.summary_time_origin_epoch_ms, self.current_timezone_name)

    def _local_datetime_for_summary_hour(self, hour_value):
        origin = self.summary_time_origin_epoch_ms
        if origin is None or not np.isfinite(float(origin)):
            return None
        try:
            tz = ZoneInfo(self.current_timezone_name)
        except Exception:
            tz = datetime.timezone.utc
        return datetime.datetime.fromtimestamp(
            (float(origin) / 1000.0) + float(hour_value) * 3600.0,
            tz=datetime.timezone.utc,
        ).astimezone(tz)

    def _local_hour_to_summary_x(self, dt):
        origin = self.summary_time_origin_epoch_ms
        if origin is None or not np.isfinite(float(origin)):
            return None
        return (dt.astimezone(datetime.timezone.utc).timestamp() * 1000.0 - float(origin)) / 3600000.0

    def _add_night_shading(self, plot_widget, x_max, x_min=0.0):
        light_on_hour, light_off_hour = self._get_light_schedule()
        start_dt = self._local_datetime_for_summary_hour(x_min)
        end_dt = self._local_datetime_for_summary_hour(x_max)
        intervals = []
        if start_dt is not None and end_dt is not None:
            try:
                tz = ZoneInfo(self.current_timezone_name)
            except Exception:
                tz = datetime.timezone.utc
            current_day = start_dt.date() - datetime.timedelta(days=1)
            final_day = end_dt.date() + datetime.timedelta(days=1)
            while current_day <= final_day:
                day_start_dt = datetime.datetime.combine(current_day, datetime.time.min, tzinfo=tz)
                next_day_dt = day_start_dt + datetime.timedelta(days=1)
                light_on_dt = day_start_dt + datetime.timedelta(hours=float(light_on_hour))
                light_off_dt = day_start_dt + datetime.timedelta(hours=float(light_off_hour))
                for interval_start_dt, interval_end_dt in [(day_start_dt, light_on_dt), (light_off_dt, next_day_dt)]:
                    start_x = self._local_hour_to_summary_x(interval_start_dt)
                    end_x = self._local_hour_to_summary_x(interval_end_dt)
                    if start_x is not None and end_x is not None:
                        intervals.append((start_x, end_x))
                current_day += datetime.timedelta(days=1)
        else:
            day_start = np.floor(float(x_min) / 24.0) * 24.0
            while day_start < x_max:
                day_end = day_start + 24.0
                intervals.append((day_start, day_start + light_on_hour))
                intervals.append((day_start + light_off_hour, day_end))
                day_start += 24.0
        for start, end in intervals:
            start = max(float(start), float(x_min))
            end = min(float(end), float(x_max))
            if end <= start:
                continue
            region = _make_vertical_region(
                values=(start, end),
                movable=False,
                brush=pg.mkBrush(0, 0, 0, 76),
                pen=pg.mkPen(None),
            )
            region.setZValue(-20)
            plot_widget.addItem(region)

    def _render_overview(self):
        self.plot_overview.clear()
        self.plot_overview.addItem(self.region)
        if self.h5_file is None:
            return
        coverage = np.asarray(self.h5_file["lfp/file_coverage_mask"][:], dtype=bool)
        missing = np.asarray(self.h5_file["lfp/missing_mask"][:], dtype=bool)
        mode3 = np.asarray(self.h5_file["task/mode3_active_mask"][:], dtype=bool)
        working = (
            np.asarray(self.h5_file["task/working_mask"][:], dtype=bool)
            if "working_mask" in self.h5_file["task"]
            else np.zeros_like(mode3)
        )

        ds_factor = max(1, self.lfp_fs * 5)

        def downsample_bool(arr):
            pad_len = (ds_factor - (len(arr) % ds_factor)) % ds_factor
            if pad_len > 0:
                arr = np.concatenate([arr, np.zeros(pad_len, dtype=bool)])
            return np.any(arr.reshape(-1, ds_factor), axis=1).astype(float)

        cov_ds = downsample_bool(coverage)
        miss_ds = downsample_bool(missing)
        m3_ds = downsample_bool(mode3)
        work_ds = downsample_bool(working)
        time_axis = np.arange(len(cov_ds)) * (ds_factor / self.lfp_fs)

        self.plot_overview.plot(time_axis, cov_ds, fillLevel=0, brush=(50, 200, 50, 100), pen=None)
        self.plot_overview.plot(time_axis, miss_ds * 0.8, fillLevel=0, brush=(255, 50, 50, 150), pen=None)
        self.plot_overview.plot(time_axis, m3_ds * 0.6, fillLevel=0, brush=(50, 50, 255, 120), pen=None)
        self.plot_overview.plot(time_axis, work_ds * 0.4, fillLevel=0, brush=(160, 70, 200, 120), pen=None)
        self.plot_overview.setTitle(
            "Recording Overview | "
            f"Coverage: {np.mean(coverage) * 100.0:.2f}% | "
            f"Missing: {np.mean(missing) * 100.0:.2f}% | "
            f"Mode3: {np.mean(mode3) * 100.0:.2f}% | "
            f"Working: {np.mean(working) * 100.0:.2f}%"
        )

    def _read_slice_group_meta(self, path):
        with h5py.File(path, "r") as h5f:
            meta = h5f["metadata"].attrs
            time_attrs = h5f["time"].attrs if "time" in h5f else {}
            origin = time_attrs.get("time_origin_epoch_ms", meta.get("time_origin_epoch_ms", np.nan))
            return {
                "path": path,
                "source_root": str(meta.get("slice_source_root", "")),
                "slice_json_path": str(meta.get("slice_json_path", "")),
                "parent_slice_id": str(meta.get("parent_slice_id", meta.get("slice_id", ""))),
                "parent_slice_label": str(meta.get("parent_slice_label", meta.get("slice_label", ""))),
                "origin_epoch_ms": float(origin) if np.isfinite(float(origin)) else np.nan,
            }

    def _same_slice_meta(self, meta, current_meta):
        current_parent_id = str(current_meta.get("parent_slice_id", "")).strip()
        parent_id = str(meta.get("parent_slice_id", "")).strip()
        current_parent_label = str(current_meta.get("parent_slice_label", "")).strip()
        parent_label = str(meta.get("parent_slice_label", "")).strip()

        if current_parent_id and parent_id:
            same_parent = parent_id == current_parent_id
        elif current_parent_label and parent_label:
            same_parent = parent_label == current_parent_label
        else:
            same_parent = False

        current_source = self._normalize_meta_path(current_meta.get("source_root", ""))
        source = self._normalize_meta_path(meta.get("source_root", ""))
        same_source = not current_source or not source or source == current_source

        current_json = self._normalize_meta_path(current_meta.get("slice_json_path", ""))
        slice_json = self._normalize_meta_path(meta.get("slice_json_path", ""))
        same_json = not current_json or not slice_json or slice_json == current_json
        return bool(same_parent and same_source and same_json)

    def _normalize_meta_path(self, value):
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return os.path.realpath(os.path.abspath(os.path.expanduser(text)))
        except Exception:
            return text

    def _find_related_slice_h5_paths(self):
        current = self._get_current_h5_path()
        if not current or not os.path.exists(current):
            return []
        try:
            current_meta = self._read_slice_group_meta(current)
        except Exception:
            return [current]
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = os.path.dirname(current)
        candidates = []
        scan_dirs = []
        if output_dir and os.path.isdir(output_dir):
            scan_dirs.append(os.path.abspath(output_dir))
        current_dir = os.path.dirname(os.path.abspath(current))
        if current_dir and os.path.isdir(current_dir):
            scan_dirs.append(current_dir)
        for scan_dir in dict.fromkeys(scan_dirs):
            for dirpath, _, filenames in os.walk(scan_dir):
                for fname in filenames:
                    if fname.endswith(".h5"):
                        candidates.append(os.path.join(dirpath, fname))
        if current not in candidates:
            candidates.append(current)

        related = []
        for path in candidates:
            try:
                meta = self._read_slice_group_meta(path)
            except Exception:
                continue
            if self._same_slice_meta(meta, current_meta):
                related.append((meta["origin_epoch_ms"], path))
        related = sorted(related, key=lambda item: (np.inf if not np.isfinite(item[0]) else item[0], item[1]))
        return [path for _, path in related]

    def _slice_statistics_summary_exists_for_h5(self, h5_path):
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = os.path.dirname(os.path.abspath(h5_path))
        try:
            summary_path = expected_slice_statistics_summary_path([h5_path], output_dir)
        except Exception:
            return False
        return bool(summary_path and os.path.exists(summary_path))

    def _slice_statistics_summary_path_for_current_slice(self, paths=None):
        paths = list(paths or self._find_related_slice_h5_paths())
        if not paths:
            return ""
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = os.path.dirname(os.path.abspath(paths[0]))
        return expected_slice_statistics_summary_path(paths, output_dir)

    def _find_same_slice_ready_h5_paths(self):
        paths = self._find_related_slice_h5_paths()
        readiness = []
        all_ready = bool(paths)
        for path in paths:
            status, tooltip = self._h5_analysis_status(path)
            readiness.append({"path": path, "status": status, "tooltip": tooltip})
            if status not in {"[LONG]", "[STATS]"}:
                all_ready = False
        return paths, readiness, all_ready

    def _update_slice_statistics_readiness(self):
        if not hasattr(self, "stats_readiness_txt"):
            return
        paths, readiness, all_ready = self._find_same_slice_ready_h5_paths()
        summary_path = self._slice_statistics_summary_path_for_current_slice(paths) if paths else ""
        lines = [
            "Slice Statistics Readiness",
            f"Same-slice H5 files: {len(paths)}",
            f"All reached longitudinal dynamics: {'yes' if all_ready else 'no'}",
        ]
        if summary_path:
            lines.append(f"Expected summary: {summary_path}")
            lines.append(f"Statistics sidecar: {'available' if os.path.exists(summary_path) else 'not generated yet'}")
        lines.append("")
        for item in readiness:
            lines.append(f"{item['status']} {os.path.basename(item['path'])}")
        if not readiness:
            lines.append("Load or select a slice H5 first.")
        self.stats_readiness_txt.setPlainText("\n".join(lines))

    def _stats_reference_scope(self):
        combo = getattr(self, "stats_reference_combo", None)
        if combo is None or combo.count() <= 0:
            return "daily"
        return str(combo.currentData() or "daily")

    def _run_slice_statistics_when_ready(self, auto=False):
        current = self._get_current_h5_path()
        try:
            paths, readiness, all_ready = self._find_same_slice_ready_h5_paths()
            self._update_slice_statistics_readiness()
            if not paths:
                raise ValueError("No same-slice H5 files were found. Load a slice H5 first.")
            if not all_ready:
                missing = [
                    f"{item['status']} {os.path.basename(item['path'])}"
                    for item in readiness
                    if item["status"] not in {"[LONG]", "[STATS]"}
                ]
                raise ValueError(
                    "Slice statistics requires every same-slice H5 to reach Longitudinal Dynamics first.\n"
                    + "\n".join(missing[:12])
                )
            cfg = self._get_config()
            output_dir = self._resolve_output_dir(allow_create=True)
            reference_scope = self._stats_reference_scope()
            n_permutations = int(self.stats_permutation_spin.value()) if hasattr(self, "stats_permutation_spin") else 1000
            label = "Auto slice statistics" if auto else "Slice statistics"
            self._start_progress(f"{label}: combining {len(paths)} H5 files", busy=True)
            self.append_log(
                f"{label} -> files={len(paths)}, reference={reference_scope}, permutations={n_permutations}"
            )
            self._close_h5_handle()
            summary = self._run_background_callable(
                f"{label}: running permutation/FDR models",
                lambda: run_slice_statistical_analysis(
                    paths,
                    output_dir,
                    config=cfg,
                    reference_scope=reference_scope,
                    n_permutations=n_permutations,
                ),
            )
            self.slice_statistics_summary_path = summary.get("summary_json", "")
            self.refresh_h5_list()
            if current and os.path.exists(current):
                self.load_h5(current, manage_progress=False)
            self._render_slice_statistics_summary(self.slice_statistics_summary_path)
            self._finish_progress(f"{label} completed")
            self.append_log(f"Slice statistics summary: {self.slice_statistics_summary_path}")
            return summary
        except Exception as exc:
            if not auto:
                self._finish_progress("Slice statistics failed")
                self._show_error("Slice Statistics Failed", exc)
            else:
                self._finish_progress("Auto slice statistics failed")
                self.append_log(f"Auto slice statistics failed: {exc}")
            return None

    def reload_slice_statistics_results(self):
        try:
            paths = self._find_related_slice_h5_paths()
            summary_path = self._slice_statistics_summary_path_for_current_slice(paths)
            if not summary_path or not os.path.exists(summary_path):
                raise ValueError("No slice statistics summary JSON was found for the current slice.")
            self._render_slice_statistics_summary(summary_path)
            self.slice_statistics_summary_path = summary_path
            self.append_log(f"Reloaded slice statistics: {summary_path}")
        except Exception as exc:
            self._show_error("Reload Slice Statistics Failed", exc)

    def open_slice_statistics_output_dir(self):
        try:
            path = self.slice_statistics_summary_path or self._slice_statistics_summary_path_for_current_slice()
            output_dir = os.path.dirname(path) if path else self._resolve_output_dir(allow_create=False)
            if output_dir and os.path.isdir(output_dir):
                webbrowser.open(f"file://{os.path.abspath(output_dir)}")
            else:
                raise ValueError("Slice statistics output directory does not exist yet.")
        except Exception as exc:
            self._show_error("Open Slice Statistics Output Failed", exc)

    def _direct_reference_scope(self):
        combo = getattr(self, "direct_reference_combo", None)
        if combo is None or combo.count() <= 0:
            return "daily"
        return str(combo.currentData() or "daily")

    def _direct_behavior_summary_path_for_current_slice(self, paths=None):
        paths = list(paths or self._find_related_slice_h5_paths())
        if not paths:
            return ""
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = os.path.dirname(os.path.abspath(paths[0]))
        return expected_direct_behavior_statistics_summary_path(paths, output_dir)

    def _find_same_slice_summary_ready_h5_paths(self):
        paths = self._find_related_slice_h5_paths()
        readiness = []
        all_ready = bool(paths)
        ready_statuses = {"[SUMMARY]", "[LONG]", "[STATS]"}
        for path in paths:
            status, tooltip = self._h5_analysis_status(path)
            readiness.append({"path": path, "status": status, "tooltip": tooltip})
            if status not in ready_statuses:
                all_ready = False
        return paths, readiness, all_ready

    def _update_direct_behavior_stats_readiness(self):
        if not hasattr(self, "direct_readiness_txt"):
            return
        paths, readiness, all_ready = self._find_same_slice_summary_ready_h5_paths()
        summary_path = self._direct_behavior_summary_path_for_current_slice(paths) if paths else ""
        lines = [
            "Direct Behavior Stats Readiness",
            f"Same-slice H5 files: {len(paths)}",
            f"All have rhythm summary: {'yes' if all_ready else 'no'}",
            "Method: direct hourly group comparisons, no regression covariates.",
        ]
        if summary_path:
            lines.append(f"Expected summary: {summary_path}")
            lines.append(f"Direct stats sidecar: {'available' if os.path.exists(summary_path) else 'not generated yet'}")
        lines.append("")
        for item in readiness:
            lines.append(f"{item['status']} {os.path.basename(item['path'])}")
        if not readiness:
            lines.append("Load or select a slice H5 first.")
        self.direct_readiness_txt.setPlainText("\n".join(lines))

    def run_direct_behavior_stats(self):
        current = self._get_current_h5_path()
        try:
            paths, readiness, all_ready = self._find_same_slice_summary_ready_h5_paths()
            self._update_direct_behavior_stats_readiness()
            if not paths:
                raise ValueError("No same-slice H5 files were found. Load a slice H5 first.")
            if not all_ready:
                missing = [
                    f"{item['status']} {os.path.basename(item['path'])}"
                    for item in readiness
                    if item["status"] not in {"[SUMMARY]", "[LONG]", "[STATS]"}
                ]
                raise ValueError(
                    "Direct behavior stats requires every same-slice H5 to have Summarize Rhythm first.\n"
                    + "\n".join(missing[:12])
                )
            cfg = self._get_config()
            output_dir = self._resolve_output_dir(allow_create=True)
            reference_scope = self._direct_reference_scope()
            n_permutations = int(self.direct_permutation_spin.value()) if hasattr(self, "direct_permutation_spin") else 1000
            self._start_progress(f"Direct stats: comparing {len(paths)} H5 files", busy=True)
            self.append_log(
                f"Direct behavior stats -> files={len(paths)}, reference={reference_scope}, permutations={n_permutations}"
            )
            self._close_h5_handle()
            summary = self._run_background_callable(
                "Direct stats: running behavior-group permutation tests",
                lambda: run_direct_behavior_state_statistics(
                    paths,
                    output_dir,
                    config=cfg,
                    reference_scope=reference_scope,
                    n_permutations=n_permutations,
                ),
            )
            self.direct_behavior_summary_path = summary.get("summary_json", "")
            if current and os.path.exists(current):
                self.load_h5(current, manage_progress=False)
            self._render_direct_behavior_stats_summary(self.direct_behavior_summary_path)
            self._finish_progress("Direct behavior stats completed")
            self.append_log(f"Direct behavior stats summary: {self.direct_behavior_summary_path}")
        except Exception as exc:
            self._finish_progress("Direct behavior stats failed")
            self._show_error("Direct Behavior Stats Failed", exc)

    def reload_direct_behavior_stats_results(self):
        try:
            paths = self._find_related_slice_h5_paths()
            summary_path = self._direct_behavior_summary_path_for_current_slice(paths)
            if not summary_path or not os.path.exists(summary_path):
                raise ValueError("No direct behavior statistics summary JSON was found for the current slice.")
            self._render_direct_behavior_stats_summary(summary_path)
            self.direct_behavior_summary_path = summary_path
            self.append_log(f"Reloaded direct behavior stats: {summary_path}")
        except Exception as exc:
            self._show_error("Reload Direct Behavior Stats Failed", exc)

    def open_direct_behavior_stats_output_dir(self):
        try:
            path = self.direct_behavior_summary_path or self._direct_behavior_summary_path_for_current_slice()
            output_dir = os.path.dirname(path) if path else self._resolve_output_dir(allow_create=False)
            if output_dir and os.path.isdir(output_dir):
                webbrowser.open(f"file://{os.path.abspath(output_dir)}")
            else:
                raise ValueError("Direct behavior stats output directory does not exist yet.")
        except Exception as exc:
            self._show_error("Open Direct Behavior Stats Output Failed", exc)

    def _paired_reference_scope(self):
        combo = getattr(self, "paired_reference_combo", None)
        if combo is None or combo.count() <= 0:
            return "daily"
        return str(combo.currentData() or "daily")

    def _paired_trial_context_summary_path_for_current_slice(self, paths=None):
        paths = list(paths or self._find_related_slice_h5_paths())
        if not paths:
            return ""
        try:
            output_dir = self._resolve_output_dir(allow_create=False)
        except Exception:
            output_dir = os.path.dirname(os.path.abspath(paths[0]))
        return expected_paired_trial_context_summary_path(paths, output_dir)

    def _update_paired_trial_context_readiness(self):
        if not hasattr(self, "paired_readiness_txt"):
            return
        paths, readiness, all_ready = self._find_same_slice_summary_ready_h5_paths()
        summary_path = self._paired_trial_context_summary_path_for_current_slice(paths) if paths else ""
        lines = [
            "Paired Trial Context Readiness",
            f"Same-slice H5 files: {len(paths)}",
            f"All have rhythm summary/state/behavior context: {'yes' if all_ready else 'no'}",
            "Method: trial-level pre/post context, default +/-1 hour, sidecar outputs only.",
        ]
        if summary_path:
            lines.append(f"Expected summary: {summary_path}")
            lines.append(f"Paired sidecar: {'available' if os.path.exists(summary_path) else 'not generated yet'}")
        lines.append("")
        for item in readiness:
            lines.append(f"{item['status']} {os.path.basename(item['path'])}")
        if not readiness:
            lines.append("Load or select a slice H5 first.")
        self.paired_readiness_txt.setPlainText("\n".join(lines))

    def run_paired_trial_context(self):
        current = self._get_current_h5_path()
        try:
            paths, readiness, all_ready = self._find_same_slice_summary_ready_h5_paths()
            self._update_paired_trial_context_readiness()
            if not paths:
                raise ValueError("No same-slice H5 files were found. Load a slice H5 first.")
            if not all_ready:
                missing = [
                    f"{item['status']} {os.path.basename(item['path'])}"
                    for item in readiness
                    if item["status"] not in {"[SUMMARY]", "[LONG]", "[STATS]"}
                ]
                raise ValueError(
                    "Paired trial context requires every same-slice H5 to have Summarize Rhythm first.\n"
                    + "\n".join(missing[:12])
                )
            cfg = self._get_config()
            output_dir = self._resolve_output_dir(allow_create=True)
            reference_scope = self._paired_reference_scope()
            context_window_s = float(self.paired_window_spin.value() * 60) if hasattr(self, "paired_window_spin") else 3600.0
            n_permutations = int(self.paired_permutation_spin.value()) if hasattr(self, "paired_permutation_spin") else 1000
            self._start_progress(f"Paired context: {len(paths)} H5 files", busy=True)
            self.append_log(
                f"Paired trial context -> files={len(paths)}, reference={reference_scope}, "
                f"window={context_window_s / 60.0:g} min, permutations={n_permutations}"
            )
            self._close_h5_handle()

            summary = self._run_background_callable(
                "Paired context: RT + PLV/PAC + paired tests",
                lambda: run_paired_trial_context_analysis(
                    paths,
                    output_dir,
                    config=cfg,
                    reference_scope=reference_scope,
                    context_window_s=context_window_s,
                    n_permutations=n_permutations,
                ),
            )
            self.paired_context_summary_path = summary.get("summary_json", "")
            if current and os.path.exists(current):
                self.load_h5(current, manage_progress=False)
            self._render_paired_trial_context_summary(self.paired_context_summary_path)
            self._finish_progress("Paired context completed")
            self.append_log(f"Paired trial context summary: {self.paired_context_summary_path}")
        except Exception as exc:
            self._finish_progress("Paired context failed")
            self._show_error("Paired Trial Context Failed", exc)

    def reload_paired_trial_context_results(self):
        try:
            paths = self._find_related_slice_h5_paths()
            summary_path = self._paired_trial_context_summary_path_for_current_slice(paths)
            if not summary_path or not os.path.exists(summary_path):
                raise ValueError("No paired trial context summary JSON was found for the current slice.")
            self._render_paired_trial_context_summary(summary_path)
            self.paired_context_summary_path = summary_path
            self.append_log(f"Reloaded paired trial context: {summary_path}")
        except Exception as exc:
            self._show_error("Reload Paired Trial Context Failed", exc)

    def open_paired_trial_context_output_dir(self):
        try:
            path = self.paired_context_summary_path or self._paired_trial_context_summary_path_for_current_slice()
            output_dir = os.path.dirname(path) if path else self._resolve_output_dir(allow_create=False)
            if output_dir and os.path.isdir(output_dir):
                webbrowser.open(f"file://{os.path.abspath(output_dir)}")
            else:
                raise ValueError("Paired trial context output directory does not exist yet.")
        except Exception as exc:
            self._show_error("Open Paired Trial Context Output Failed", exc)

    def _render_paired_trial_context_summary(self, summary_json_path):
        if not summary_json_path or not os.path.exists(summary_json_path):
            self._update_paired_trial_context_readiness()
            return
        with open(summary_json_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
        self.paired_context_summary_path = summary_json_path
        pre_rows = self._read_csv_rows(summary.get("pre_context_tests_csv", "")) or list(summary.get("top_pre_context_effects", []))
        post_rows = self._read_csv_rows(summary.get("post_context_tests_csv", "")) or list(summary.get("top_post_context_effects", []))
        delta_rows = self._read_csv_rows(summary.get("pre_post_delta_tests_csv", "")) or list(summary.get("top_pre_post_delta_effects", []))
        self.paired_context_top_rows = (pre_rows[:25] + post_rows[:25] + delta_rows[:25])[:75]
        self.paired_context_trial_rows = self._read_csv_rows(summary.get("paired_context_csv", ""))
        self._populate_paired_context_table(self.paired_context_top_rows)
        self._plot_paired_context_effects(self.paired_context_top_rows)
        self._plot_paired_context_state_summary(self.paired_context_trial_rows)
        self._render_paired_context_selected_target()

        lines = [
            "Paired Trial Context Results",
            f"Summary: {summary_json_path}",
            f"Reference: {summary.get('reference_scope', '')}",
            f"Context window: {float(summary.get('context_window_s', 0.0)) / 60.0:g} min",
            f"Permutations: {summary.get('n_permutations', 0)}",
            f"H5 files: {summary.get('n_h5_files', 0)} | trials: {summary.get('n_trials', 0)} | RT records: {summary.get('n_rt_trials', 0)}",
            f"RT features: {summary.get('rt_features_csv', '')}",
            f"Paired context table: {summary.get('paired_context_csv', '')}",
            f"Pre-context tests: {summary.get('pre_context_tests_csv', '')}",
            f"Post-context tests: {summary.get('post_context_tests_csv', '')}",
            f"Pre/post delta tests: {summary.get('pre_post_delta_tests_csv', '')}",
            "",
            "Top pre-context effects:",
        ]
        for row in pre_rows[:6]:
            lines.append(
                f"{row.get('feature', '')} | {row.get('behavior_label', '')} | "
                f"effect={self._fmt_stat(row.get('effect_size'))} | "
                f"p={self._fmt_stat(row.get('permutation_p'))} | q={self._fmt_stat(row.get('fdr_q'))}"
            )
        lines.append("")
        lines.append("Top post-context effects:")
        for row in post_rows[:6]:
            lines.append(
                f"{row.get('feature', '')} | {row.get('behavior_label', '')} | "
                f"effect={self._fmt_stat(row.get('effect_size'))} | "
                f"p={self._fmt_stat(row.get('permutation_p'))} | q={self._fmt_stat(row.get('fdr_q'))}"
            )
        if summary.get("warnings"):
            lines.append("")
            lines.append("Warnings:")
            lines.extend([str(item) for item in summary.get("warnings", [])[:8]])
        self.paired_readiness_txt.setPlainText("\n".join(lines))

    def _populate_paired_context_table(self, rows):
        self.paired_table.setRowCount(0)
        for row_idx, row in enumerate(rows or []):
            self.paired_table.insertRow(row_idx)
            values = [
                row.get("feature", ""),
                row.get("analysis_family", ""),
                row.get("behavior_label", ""),
                self._fmt_stat(row.get("effect_size")),
                self._fmt_stat(row.get("permutation_p")),
                self._fmt_stat(row.get("fdr_q")),
                row.get("top_contrast_label", row.get("top_contrast", "")),
                str(row.get("n_obs", "")),
            ]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_idx in {3, 4, 5, 7}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.paired_table.setItem(row_idx, col_idx, item)
        self.paired_table.resizeColumnsToContents()
        if rows:
            self.paired_table.selectRow(0)

    def _plot_paired_context_effects(self, rows):
        self.paired_effect_plot.clear()
        rows = list(rows or [])[:30]
        if not rows:
            self.paired_effect_plot.setTitle("Paired Trial Context Effects (empty)")
            return
        x = np.arange(len(rows), dtype=np.float64)
        heights = np.asarray([abs(self._to_float(row.get("effect_size"))) for row in rows], dtype=np.float64)
        heights = np.nan_to_num(heights, nan=0.0)
        self.paired_effect_plot.addItem(
            pg.BarGraphItem(x=x, height=heights, width=0.75, brush=pg.mkBrush("#aa4499"))
        )
        ticks = [
            (idx, f"{str(row.get('feature', ''))[:18]}\n{str(row.get('behavior_label', row.get('analysis_family', '')))[:10]}")
            for idx, row in enumerate(rows)
        ]
        self.paired_effect_plot.getAxis("bottom").setTicks([ticks])
        self.paired_effect_plot.setLabel("left", "|effect size|")
        self.paired_effect_plot.setTitle("Paired Trial Context Effects")

    def _plot_paired_context_state_summary(self, rows):
        self.paired_state_plot.clear()
        if not rows:
            self.paired_state_plot.setTitle("Pre/Post Natural State Occupancy (empty)")
            return
        states = ["wake", "nrem", "rem", "working"]
        pre = np.asarray([
            np.nanmean([self._to_float(row.get(f"pre_occ_{state}")) for row in rows])
            for state in states
        ], dtype=np.float64)
        post = np.asarray([
            np.nanmean([self._to_float(row.get(f"post_occ_{state}")) for row in rows])
            for state in states
        ], dtype=np.float64)
        x = np.arange(len(states), dtype=np.float64)
        self.paired_state_plot.addItem(pg.BarGraphItem(x=x - 0.18, height=np.nan_to_num(pre), width=0.32, brush=pg.mkBrush("#4477aa")))
        self.paired_state_plot.addItem(pg.BarGraphItem(x=x + 0.18, height=np.nan_to_num(post), width=0.32, brush=pg.mkBrush("#cc6677")))
        self.paired_state_plot.getAxis("bottom").setTicks([[(idx, state) for idx, state in enumerate(states)]])
        self.paired_state_plot.setLabel("left", "occupancy fraction")
        self.paired_state_plot.setTitle("Pre/Post Natural State Occupancy (blue=pre, red=post)")

    def _render_paired_context_selected_target(self):
        if not hasattr(self, "paired_distribution_plot"):
            return
        rows = getattr(self, "paired_context_top_rows", [])
        trial_rows = getattr(self, "paired_context_trial_rows", [])
        selected = self.paired_table.currentRow() if hasattr(self, "paired_table") else 0
        if selected < 0 or selected >= len(rows):
            selected = 0
        result_row = rows[selected] if rows else {}
        self._plot_paired_context_distribution(trial_rows, result_row)

    def _plot_paired_context_distribution(self, trial_rows, result_row):
        self.paired_distribution_plot.clear()
        feature = str(result_row.get("feature", "") or "")
        variable = str(result_row.get("behavior_variable", "") or "")
        if not trial_rows or not feature or not variable:
            self.paired_distribution_plot.setTitle("Selected Feature by Trial Behavior (empty)")
            return
        values = np.asarray([self._to_float(row.get(feature)) for row in trial_rows], dtype=np.float64)
        groups = np.asarray([self._to_float(row.get(variable)) for row in trial_rows], dtype=np.float64)
        codes = sorted({int(code) for code in groups[np.isfinite(groups)].astype(int)})
        colors = ["#4477aa", "#117733", "#cc6677", "#88ccee", "#ddcc77"]
        for idx, code in enumerate(codes):
            selected = values[(groups == code) & np.isfinite(values)]
            if selected.size == 0:
                continue
            if selected.size > 500:
                selected = selected[np.linspace(0, selected.size - 1, 500).astype(int)]
            jitter = np.linspace(-0.18, 0.18, selected.size) if selected.size > 1 else np.zeros(selected.size)
            self.paired_distribution_plot.plot(
                x=np.full(selected.size, idx, dtype=np.float64) + jitter,
                y=selected,
                pen=None,
                symbol="o",
                symbolSize=5,
                symbolBrush=pg.mkBrush(colors[idx % len(colors)]),
                symbolPen=None,
            )
            median = float(np.nanmedian(selected))
            self.paired_distribution_plot.plot(
                x=np.asarray([idx - 0.25, idx + 0.25], dtype=np.float64),
                y=np.asarray([median, median], dtype=np.float64),
                pen=pg.mkPen("#111111", width=3),
            )
        self.paired_distribution_plot.getAxis("bottom").setTicks([[(idx, str(code)) for idx, code in enumerate(codes)]])
        self.paired_distribution_plot.setLabel("left", feature)
        self.paired_distribution_plot.setTitle(f"{feature} by {result_row.get('behavior_label', variable)}")

    def _render_direct_behavior_stats_summary(self, summary_json_path):
        if not summary_json_path or not os.path.exists(summary_json_path):
            self._update_direct_behavior_stats_readiness()
            return
        with open(summary_json_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
        self.direct_behavior_summary_path = summary_json_path
        self.direct_behavior_all_rows = (
            self._read_csv_rows(summary.get("lfp_group_tests_csv", ""))
            or list(summary.get("top_lfp_group_differences", []))
        )
        self.direct_behavior_pairwise_rows = self._read_csv_rows(summary.get("lfp_pairwise_tests_csv", ""))
        self.direct_behavior_hourly_rows = self._read_csv_rows(summary.get("hourly_csv", ""))
        self._refresh_direct_behavior_filter_options(self.direct_behavior_all_rows)
        self._apply_direct_behavior_filters()

        lines = [
            "Direct Behavior Stats Results",
            f"Summary: {summary_json_path}",
            f"Reference: {summary.get('reference_scope', '')}",
            f"Permutations: {summary.get('n_permutations', 0)}",
            f"H5 files: {summary.get('n_h5_files', 0)} | hours: {summary.get('n_hours', 0)}",
            f"Neural feature group tests: {summary.get('lfp_group_tests_csv', '')}",
            f"Neural feature pairwise tests: {summary.get('lfp_pairwise_tests_csv', '')}",
            f"State occupancy tests: {summary.get('state_occupancy_tests_csv', '')}",
            f"Continuous associations: {summary.get('continuous_associations_csv', '')}",
            "",
            "This is a direct distribution comparison: it does not control elapsed day, ZT, or state occupancy.",
            "",
            "Top direct LFP group differences:",
        ]
        for row in self.direct_behavior_all_rows[:10]:
            lines.append(
                f"{row.get('target', '')} | {row.get('behavior_label', '')} | "
                f"effect={self._fmt_stat(row.get('effect_size'))} | "
                f"eta2={self._fmt_stat(row.get('eta2'))} | "
                f"p={self._fmt_stat(row.get('permutation_p'))} | q={self._fmt_stat(row.get('fdr_q'))} | "
                f"{row.get('top_contrast_label', '')}"
            )
        occupancy_rows = list(summary.get("top_state_occupancy_differences", []))
        if occupancy_rows:
            lines.append("")
            lines.append("Top direct state-occupancy differences:")
            for row in occupancy_rows[:6]:
                lines.append(
                    f"{row.get('natural_state', row.get('target', ''))} | {row.get('behavior_label', '')} | "
                    f"effect={self._fmt_stat(row.get('effect_size'))} | q={self._fmt_stat(row.get('fdr_q'))}"
                )
        self.direct_readiness_txt.setPlainText("\n".join(lines))

    def _refresh_direct_behavior_filter_options(self, rows):
        combos = [
            (getattr(self, "direct_region_filter", None), "channel_group", lambda row: str(row.get("channel_group", "") or "")),
            (getattr(self, "direct_state_filter", None), "state", self._direct_behavior_state_label),
            (getattr(self, "direct_behavior_filter", None), "behavior", lambda row: str(row.get("behavior_label", "") or "")),
        ]
        for combo, _, getter in combos:
            if combo is None:
                continue
            current = str(combo.currentData() or "")
            values = sorted({str(getter(row)).strip() for row in rows or [] if str(getter(row)).strip()})
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("None", "")
            for value in values:
                combo.addItem(value, value)
            idx = combo.findData(current)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
            combo.blockSignals(False)

    def _direct_behavior_state_label(self, row):
        target_class = str(row.get("target_class", "") or "")
        natural_state = str(row.get("natural_state", "") or "").strip()
        if target_class == "global_lfp" or natural_state.lower() == "global":
            return "LFP"
        return natural_state or target_class or "unknown"

    def _direct_filter_value(self, attr):
        combo = getattr(self, attr, None)
        if combo is None:
            return ""
        return str(combo.currentData() or "")

    def _direct_behavior_row_matches_filters(self, row):
        region = self._direct_filter_value("direct_region_filter")
        state = self._direct_filter_value("direct_state_filter")
        behavior = self._direct_filter_value("direct_behavior_filter")
        if region and str(row.get("channel_group", "") or "") != region:
            return False
        if state and self._direct_behavior_state_label(row) != state:
            return False
        if behavior and str(row.get("behavior_label", "") or "") != behavior:
            return False
        return True

    def _apply_direct_behavior_filters(self):
        if not hasattr(self, "direct_table") or not hasattr(self, "direct_effect_plot"):
            return
        rows = list(getattr(self, "direct_behavior_all_rows", []) or [])
        filtered = [row for row in rows if self._direct_behavior_row_matches_filters(row)]
        self.direct_behavior_top_rows = filtered
        self._populate_direct_behavior_table(filtered)
        self._plot_direct_behavior_effects(filtered)
        self._render_direct_behavior_selected_target()
        if hasattr(self, "direct_filter_status_label"):
            filters = []
            for label, attr in [
                ("Region", "direct_region_filter"),
                ("State", "direct_state_filter"),
                ("Behavior", "direct_behavior_filter"),
            ]:
                value = self._direct_filter_value(attr)
                if value:
                    filters.append(f"{label}={value}")
            text = " | ".join(filters) if filters else "None"
            self.direct_filter_status_label.setText(f"Filters: {text} | showing {len(filtered)}/{len(rows)}")

    def _populate_direct_behavior_table(self, rows):
        self.direct_table.setRowCount(0)
        for row_idx, row in enumerate(rows or []):
            self.direct_table.insertRow(row_idx)
            values = [
                row.get("target", ""),
                row.get("behavior_label", ""),
                self._fmt_stat(row.get("effect_size")),
                self._fmt_stat(row.get("eta2")),
                self._fmt_stat(row.get("permutation_p")),
                self._fmt_stat(row.get("fdr_q")),
                row.get("top_contrast_label", ""),
                str(row.get("n_obs", "")),
            ]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_idx in {2, 3, 4, 5, 7}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.direct_table.setItem(row_idx, col_idx, item)
        self.direct_table.resizeColumnsToContents()
        if rows:
            self.direct_table.selectRow(0)

    def _plot_direct_behavior_effects(self, rows):
        self.direct_effect_plot.clear()
        rows = list(rows or [])[:30]
        if not rows:
            self.direct_effect_plot.setTitle("Direct Behavior Group Differences (empty)")
            return
        x = np.arange(len(rows), dtype=np.float64)
        heights = np.asarray([self._to_float(row.get("effect_size")) for row in rows], dtype=np.float64)
        heights = np.nan_to_num(heights, nan=0.0)
        self.direct_effect_plot.addItem(
            pg.BarGraphItem(x=x, height=heights, width=0.75, brush=pg.mkBrush("#117733"))
        )
        ticks = [
            (idx, f"{str(row.get('target', ''))[:16]}\n{str(row.get('behavior_label', ''))[:10]}")
            for idx, row in enumerate(rows)
        ]
        self.direct_effect_plot.getAxis("bottom").setTicks([ticks])
        self.direct_effect_plot.setLabel("left", "Median diff / pooled IQR")
        self.direct_effect_plot.setTitle("Direct Behavior Group Differences")

    def _render_direct_behavior_selected_target(self):
        rows = getattr(self, "direct_behavior_top_rows", [])
        hourly_rows = getattr(self, "direct_behavior_hourly_rows", [])
        if not hasattr(self, "direct_distribution_splitter"):
            return
        selected = self.direct_table.currentRow() if hasattr(self, "direct_table") else 0
        if selected < 0 or selected >= len(rows):
            selected = 0
        row = rows[selected] if rows else {}
        self._plot_direct_behavior_distribution(hourly_rows, row)

    def _ensure_direct_distribution_plots(self, count):
        if not hasattr(self, "direct_distribution_splitter"):
            return
        count = max(1, int(count or 1))
        while len(self.direct_distribution_plots) < count:
            plot = pg.PlotWidget(title="Hourly neural feature value")
            plot.showGrid(x=True, y=True, alpha=0.25)
            self.direct_distribution_splitter.addWidget(plot)
            self.direct_distribution_plots.append(plot)
        for idx, plot in enumerate(self.direct_distribution_plots):
            plot.setVisible(idx < count)

    def _direct_behavior_band_rows_for_distribution(self, result_row):
        if not result_row:
            return []
        region = str(result_row.get("channel_group", "") or "")
        state_label = self._direct_behavior_state_label(result_row)
        behavior_variable = str(result_row.get("behavior_variable", "") or "")
        candidates = []
        seen_targets = set()
        for row in getattr(self, "direct_behavior_all_rows", []) or []:
            if str(row.get("channel_group", "") or "") != region:
                continue
            if self._direct_behavior_state_label(row) != state_label:
                continue
            if str(row.get("behavior_variable", "") or "") != behavior_variable:
                continue
            target = str(row.get("target", "") or "")
            if not target or target in seen_targets:
                continue
            seen_targets.add(target)
            candidates.append(row)
        return sorted(candidates, key=self._direct_band_sort_key)

    def _direct_band_sort_key(self, row):
        band = str(row.get("band", "") or "").lower()
        order = {
            "delta": 0,
            "theta": 1,
            "alpha": 2,
            "beta": 3,
            "low_gamma": 4,
            "gamma": 5,
            "high_gamma": 6,
            "ripple": 7,
        }
        return (order.get(band, 100), band, str(row.get("target", "")))

    def _plot_direct_behavior_distribution(self, hourly_rows, result_row):
        band_rows = self._direct_behavior_band_rows_for_distribution(result_row)
        if not band_rows and result_row:
            band_rows = [result_row]
        self._ensure_direct_distribution_plots(max(1, len(band_rows)))
        for plot in getattr(self, "direct_distribution_plots", []):
            plot.clear()
        target = result_row.get("target", "")
        variable = result_row.get("behavior_variable", "")
        if not hourly_rows or not target or not variable or not band_rows:
            if self.direct_distribution_plots:
                self.direct_distribution_plots[0].setTitle("Hourly neural feature grouped by behavior (empty)")
            return
        for idx, band_row in enumerate(band_rows):
            if idx >= len(self.direct_distribution_plots):
                break
            self._plot_direct_behavior_distribution_one(self.direct_distribution_plots[idx], hourly_rows, band_row)
        if hasattr(self, "direct_distribution_splitter") and band_rows:
            self.direct_distribution_splitter.setSizes([1] * min(len(band_rows), len(self.direct_distribution_plots)))

    def _plot_direct_behavior_distribution_one(self, plot, hourly_rows, result_row):
        plot.clear()
        target = result_row.get("target", "")
        variable = result_row.get("behavior_variable", "")
        if not hourly_rows or not target or not variable:
            plot.setTitle("Hourly neural feature grouped by behavior (empty)")
            return
        label_maps = {
            "rule_code": RULE_LABELS,
            "perf_state_code": PERF_STATE_LABELS,
            "bias_state_code": BIAS_STATE_LABELS,
            "rt_class_code": RT_CLASS_LABELS,
        }
        label_map = label_maps.get(variable, {})
        values = np.asarray([self._to_float(row.get(target)) for row in hourly_rows], dtype=np.float64)
        groups = np.asarray([self._to_float(row.get(variable)) for row in hourly_rows], dtype=np.float64)
        codes = [code for code in sorted(label_map.keys()) if np.any((groups == int(code)) & np.isfinite(values))]
        group_in_codes = np.zeros(groups.shape, dtype=bool)
        if codes:
            code_arr = np.asarray(codes, dtype=np.int16)
            valid_group = np.isfinite(groups)
            group_in_codes[valid_group] = np.isin(groups[valid_group].astype(np.int16), code_arr, assume_unique=False)
        finite_for_codes = np.isfinite(values) & group_in_codes
        colors = ["#4477aa", "#117733", "#cc6677", "#88ccee", "#ddcc77"]
        for idx, code in enumerate(codes):
            selected = values[(groups == int(code)) & np.isfinite(values)]
            if selected.size == 0:
                continue
            if selected.size > 450:
                selected = selected[np.linspace(0, selected.size - 1, 450).astype(int)]
            jitter = np.linspace(-0.18, 0.18, selected.size) if selected.size > 1 else np.zeros(selected.size)
            x = np.full(selected.size, idx, dtype=np.float64) + jitter
            plot.plot(
                x=x,
                y=selected,
                pen=None,
                symbol="o",
                symbolSize=5,
                symbolBrush=pg.mkBrush(colors[idx % len(colors)]),
                symbolPen=None,
            )
            median = float(np.nanmedian(selected))
            plot.plot(
                x=np.asarray([idx - 0.28, idx + 0.28], dtype=np.float64),
                y=np.asarray([median, median], dtype=np.float64),
                pen=pg.mkPen("#111111", width=3),
            )
            text = pg.TextItem(str(label_map.get(int(code), int(code))), color=colors[idx % len(colors)], anchor=(0.5, 1.0))
            text.setPos(float(idx), median)
            plot.addItem(text)
        ticks = [(idx, str(label_map.get(int(code), int(code)))) for idx, code in enumerate(codes)]
        plot.getAxis("bottom").setTicks([ticks])
        plot.setLabel("left", "Hourly neural feature value")
        band = str(result_row.get("band", "") or "")
        title_prefix = f"{band} | " if band else ""
        plot.setTitle(f"{title_prefix}{target} grouped by {result_row.get('behavior_label', variable)}")
        self._add_direct_behavior_pairwise_significance(plot, result_row, codes, values[finite_for_codes])

    def _add_direct_behavior_pairwise_significance(self, plot, result_row, codes, visible_values):
        if len(codes) < 2:
            return
        target = str(result_row.get("target", "") or "")
        variable = str(result_row.get("behavior_variable", "") or "")
        code_to_x = {int(code): idx for idx, code in enumerate(codes)}
        rows = []
        for row in getattr(self, "direct_behavior_pairwise_rows", []) or []:
            if str(row.get("target", "") or "") != target:
                continue
            if str(row.get("behavior_variable", "") or "") != variable:
                continue
            parsed = self._parse_direct_contrast_codes(row.get("contrast", ""))
            if parsed is None:
                continue
            code_b, code_a = parsed
            if code_a in code_to_x and code_b in code_to_x:
                rows.append((code_a, code_b, row))
        if not rows:
            return
        finite = np.asarray(visible_values, dtype=np.float64)
        finite = finite[np.isfinite(finite)]
        if finite.size == 0:
            return
        y_min = float(np.nanmin(finite))
        y_max = float(np.nanmax(finite))
        span = y_max - y_min
        if not np.isfinite(span) or span <= 1e-12:
            span = max(abs(y_max), 1.0) * 0.1
        step = 0.12 * span
        tick = 0.025 * span
        start_y = y_max + 0.10 * span
        used = 0
        for code_a, code_b, row in rows[:6]:
            p_value = self._to_float(row.get("permutation_p"))
            label = self._p_value_stars(p_value)
            if not label:
                continue
            x1 = float(min(code_to_x[code_a], code_to_x[code_b]))
            x2 = float(max(code_to_x[code_a], code_to_x[code_b]))
            y = start_y + used * step
            plot.plot(
                x=np.asarray([x1, x1, x2, x2], dtype=np.float64),
                y=np.asarray([y - tick, y, y, y - tick], dtype=np.float64),
                pen=pg.mkPen("#333333", width=1),
            )
            text = pg.TextItem(label, color="#111111", anchor=(0.5, 1.0))
            text.setPos((x1 + x2) / 2.0, y + 0.015 * span)
            text.setToolTip(f"p={self._fmt_stat(p_value)}")
            plot.addItem(text)
            used += 1
        if used:
            plot.setYRange(y_min - 0.08 * span, start_y + used * step + 0.12 * span, padding=0.02)

    def _parse_direct_contrast_codes(self, contrast):
        text = str(contrast or "").strip()
        if "-vs-" not in text:
            return None
        left, right = text.split("-vs-", 1)
        try:
            return int(left), int(right)
        except Exception:
            return None

    def _p_value_stars(self, p_value):
        value = self._to_float(p_value)
        if not np.isfinite(value):
            return ""
        if value < 0.001:
            return "***"
        if value < 0.01:
            return "**"
        if value < 0.05:
            return "*"
        return "ns"

    def _render_slice_statistics_summary(self, summary_json_path):
        if not summary_json_path or not os.path.exists(summary_json_path):
            self._update_slice_statistics_readiness()
            return
        with open(summary_json_path, "r", encoding="utf-8") as handle:
            summary = json.load(handle)
        self.slice_statistics_summary_path = summary_json_path
        self.slice_statistics_top_rows = list(
            summary.get("top_lfp_behavior_modulations")
            or summary.get("top_effects", [])
        )
        self.slice_statistics_hourly_rows = self._read_csv_rows(summary.get("hourly_csv", ""))
        self._populate_slice_statistics_table(self.slice_statistics_top_rows)
        self._plot_slice_statistics_effects(self.slice_statistics_top_rows)
        self._render_slice_statistics_selected_target()

        lines = [
            "Slice Statistics Results",
            f"Summary: {summary_json_path}",
            f"Reference: {summary.get('reference_scope', '')}",
            f"Permutations: {summary.get('n_permutations', 0)}",
            f"H5 files: {summary.get('n_h5_files', 0)} | hours: {summary.get('n_hours', 0)} | days: {summary.get('n_days', 0)}",
            f"Low-confidence significance: {'yes' if summary.get('low_confidence') else 'no'}",
            f"Effect summary: {summary.get('effect_csv', '')}",
            f"Term tests: {summary.get('term_tests_csv', '')}",
            f"LFP behavior modulation: {summary.get('lfp_behavior_modulation_csv', '')}",
            f"State-specific LFP summary: {summary.get('state_specific_lfp_summary_csv', '')}",
            f"State occupancy tests: {summary.get('state_occupancy_tests_csv', '')}",
            f"Behavior x circadian tests: {summary.get('interaction_tests_csv', '')}",
            f"Partial correlations: {summary.get('partial_correlations_csv', '')}",
        ]
        if summary.get("warnings"):
            lines.append("")
            lines.append("Warnings:")
            lines.extend([str(item) for item in summary.get("warnings", [])[:8]])
        lines.append("")
        lines.append("Top LFP behavior modulation:")
        for row in self.slice_statistics_top_rows[:8]:
            lines.append(
                f"{row.get('target', '')} | {row.get('test_family', '')} | "
                f"{row.get('natural_state', '')} {row.get('band', '')} | "
                f"deltaR2={self._fmt_stat(row.get('delta_r2'))} | "
                f"p={self._fmt_stat(row.get('permutation_p'))} | q={self._fmt_stat(row.get('fdr_q'))}"
            )
        state_specific = list(summary.get("top_state_specific_lfp", []))
        if state_specific:
            lines.append("")
            lines.append("Top state-specific LFP modulation:")
            for row in state_specific[:6]:
                lines.append(
                    f"{row.get('channel_group', '')} {row.get('band', '')} | {row.get('test_family', '')} | "
                    f"top={row.get('top_state', '')} | "
                    f"deltaR2={self._fmt_stat(row.get('top_state_delta_r2'))} | "
                    f"q={self._fmt_stat(row.get('top_state_q'))} | {row.get('interpretation', '')}"
                )
        state_occupancy = list(summary.get("top_state_occupancy_effects", []))
        if state_occupancy:
            lines.append("")
            lines.append("Top natural-state occupancy modulation:")
            for row in state_occupancy[:6]:
                lines.append(
                    f"{row.get('natural_state', '')} | {row.get('test_family', '')} | "
                    f"deltaR2={self._fmt_stat(row.get('delta_r2'))} | "
                    f"q={self._fmt_stat(row.get('fdr_q'))}"
                )
        self.stats_readiness_txt.setPlainText("\n".join(lines))

    def _populate_slice_statistics_table(self, rows):
        self.stats_table.setRowCount(0)
        for row_idx, row in enumerate(rows or []):
            self.stats_table.insertRow(row_idx)
            values = [
                row.get("target", ""),
                row.get("test_family", ""),
                self._fmt_stat(row.get("delta_r2")),
                self._fmt_stat(row.get("permutation_p")),
                self._fmt_stat(row.get("fdr_q")),
                row.get("top_term", ""),
                row.get("effect_sign", ""),
                str(row.get("n_obs", "")),
            ]
            for col_idx, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col_idx in {2, 3, 4, 7}:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.stats_table.setItem(row_idx, col_idx, item)
        self.stats_table.resizeColumnsToContents()
        if rows:
            self.stats_table.selectRow(0)

    def _plot_slice_statistics_effects(self, rows):
        self.stats_effect_plot.clear()
        rows = list(rows or [])[:25]
        if not rows:
            self.stats_effect_plot.setTitle("Slice Statistics Effect Ranking (empty)")
            return
        x = np.arange(len(rows), dtype=np.float64)
        heights = np.asarray([self._to_float(row.get("delta_r2")) for row in rows], dtype=np.float64)
        heights = np.nan_to_num(heights, nan=0.0)
        self.stats_effect_plot.addItem(
            pg.BarGraphItem(x=x, height=heights, width=0.75, brush=pg.mkBrush("#4477aa"))
        )
        ticks = [(idx, str(row.get("target", ""))[:22]) for idx, row in enumerate(rows)]
        self.stats_effect_plot.getAxis("bottom").setTicks([ticks])
        self.stats_effect_plot.setLabel("left", "Delta R2")
        self.stats_effect_plot.setTitle("Slice Statistics Effect Ranking")

    def _render_slice_statistics_selected_target(self):
        rows = getattr(self, "slice_statistics_top_rows", [])
        hourly_rows = getattr(self, "slice_statistics_hourly_rows", [])
        if not hasattr(self, "stats_heatmap_plot"):
            return
        selected = self.stats_table.currentRow() if hasattr(self, "stats_table") else 0
        if selected < 0 or selected >= len(rows):
            selected = 0
        target = rows[selected].get("target", "") if rows else ""
        self._plot_slice_statistics_heatmap(hourly_rows, target)
        self._plot_slice_statistics_overlay(hourly_rows, target)

    def _plot_slice_statistics_heatmap(self, hourly_rows, target):
        self.stats_heatmap_plot.clear()
        if not hourly_rows or not target:
            self.stats_heatmap_plot.setTitle("Top Target Day x ZT Heatmap (empty)")
            return
        max_day = int(max(self._to_float(row.get("day_index")) for row in hourly_rows))
        grid = np.full((max_day + 1, 24), np.nan, dtype=np.float64)
        count = np.zeros((max_day + 1, 24), dtype=np.int32)
        for row in hourly_rows:
            day = int(self._to_float(row.get("day_index")))
            hour = int(np.floor(self._to_float(row.get("zt_hour")))) % 24
            value = self._to_float(row.get(target))
            if np.isfinite(value):
                if np.isnan(grid[day, hour]):
                    grid[day, hour] = 0.0
                grid[day, hour] += value
                count[day, hour] += 1
        grid = np.divide(grid, count, out=np.full_like(grid, np.nan), where=count > 0)
        lut = _get_lookup_table("viridis", [(68, 1, 84), (59, 82, 139), (33, 145, 140), (253, 231, 37)])
        levels = _robust_levels(grid, default=(0.5, 1.5))
        item = _make_image_item(grid, lut=lut, levels=levels)
        item.setRect(QRectF(0, -0.5, 24, grid.shape[0]))
        self.stats_heatmap_plot.addItem(item)
        self.stats_heatmap_plot.setXRange(0, 24, padding=0)
        self.stats_heatmap_plot.setYRange(-0.5, max_day + 0.5, padding=0)
        self.stats_heatmap_plot.setTitle(f"Day x ZT Heatmap | {target}")

    def _plot_slice_statistics_overlay(self, hourly_rows, target):
        self.stats_overlay_plot.clear()
        if not hourly_rows or not target:
            self.stats_overlay_plot.setTitle("Selected Target + Behavior Overlay (empty)")
            return
        x = np.asarray([self._to_float(row.get("elapsed_day")) for row in hourly_rows], dtype=np.float64)
        y = np.asarray([self._to_float(row.get(target)) for row in hourly_rows], dtype=np.float64)
        y_min, y_max = self._slice_statistics_target_range(y)
        if np.isfinite(y).any():
            self.stats_overlay_plot.plot(x=x, y=y, pen=pg.mkPen("#1f77b4", width=2), name=target)
            self._add_slice_statistics_overlay_label(x, y, f"Target raw: {target}", "#1f77b4", y_offset=(y_max - y_min) * 0.04)
        perf = np.asarray([self._to_float(row.get("perf_pct")) for row in hourly_rows], dtype=np.float64)
        if np.isfinite(perf).any() and np.isfinite(y_min) and np.isfinite(y_max):
            perf_display = self._scale_to_target_range(np.clip(perf, 0, 100) / 100.0, y_min, y_max)
            self.stats_overlay_plot.plot(
                x=x,
                y=perf_display,
                pen=pg.mkPen("#1b7837", width=2),
                name="perf %",
            )
            self._add_slice_statistics_overlay_label(x, perf_display, "Performance % (scaled)", "#1b7837", y_offset=(y_max - y_min) * 0.03)
        bias = np.asarray([self._to_float(row.get("bias_score")) for row in hourly_rows], dtype=np.float64)
        if np.isfinite(bias).any() and np.isfinite(y_min) and np.isfinite(y_max):
            bias_display = self._scale_to_target_range((np.clip(bias, -1, 1) + 1.0) / 2.0, y_min, y_max)
            self.stats_overlay_plot.plot(
                x=x,
                y=bias_display,
                pen=pg.mkPen("#762a83", width=2),
                name="bias score",
            )
            self._add_slice_statistics_overlay_label(x, bias_display, "Bias score (scaled)", "#762a83", y_offset=-(y_max - y_min) * 0.03)
        rule = np.asarray([self._to_float(row.get("rule_code")) for row in hourly_rows], dtype=np.float64)
        if np.isfinite(rule).any() and np.isfinite(y_min) and np.isfinite(y_max):
            rule_display = self._scale_to_target_range(np.clip(rule, 0, 2) / 2.0, y_min, y_max, low=0.05, high=0.25)
            self.stats_overlay_plot.plot(
                x=x,
                y=rule_display,
                pen=pg.mkPen("#4c78a8", width=1),
                name="rule",
            )
            self._add_slice_statistics_overlay_label(x, rule_display, "Rule code (scaled)", "#4c78a8", y_offset=-(y_max - y_min) * 0.04)
        if np.isfinite(y_min) and np.isfinite(y_max):
            self.stats_overlay_plot.setYRange(y_min, y_max, padding=0.02)
        self.stats_overlay_plot.setLabel("left", "Raw selected target value")
        self.stats_overlay_plot.setLabel("bottom", "Elapsed day")
        self.stats_overlay_plot.setTitle(f"Selected Target + Behavior Overlay | {target}")

    def _slice_statistics_target_range(self, values):
        arr = np.asarray(values, dtype=np.float64)
        finite = arr[np.isfinite(arr)]
        if finite.size == 0:
            return np.nan, np.nan
        y_min = float(np.nanmin(finite))
        y_max = float(np.nanmax(finite))
        if not np.isfinite(y_min) or not np.isfinite(y_max):
            return np.nan, np.nan
        if abs(y_max - y_min) <= 1e-12:
            pad = max(abs(y_min) * 0.05, 1.0)
        else:
            pad = (y_max - y_min) * 0.08
        return y_min - pad, y_max + pad

    def _scale_to_target_range(self, fraction, y_min, y_max, low=0.05, high=0.95):
        frac = np.asarray(fraction, dtype=np.float64)
        span = float(y_max) - float(y_min)
        return float(y_min) + (float(low) + np.clip(frac, 0.0, 1.0) * (float(high) - float(low))) * span

    def _add_slice_statistics_overlay_label(self, x, y, text, color, y_offset=0.0):
        x_arr = np.asarray(x, dtype=np.float64)
        y_arr = np.asarray(y, dtype=np.float64)
        valid = np.isfinite(x_arr) & np.isfinite(y_arr)
        if not np.any(valid):
            return
        idx = int(np.flatnonzero(valid)[-1])
        label = str(text or "")
        if len(label) > 58:
            label = label[:55] + "..."
        item = pg.TextItem(label, color=color, anchor=(1.0, 0.5))
        item.setPos(float(x_arr[idx]), float(y_arr[idx]) + float(y_offset))
        item.setZValue(50)
        self.stats_overlay_plot.addItem(item)

    def _read_csv_rows(self, path):
        if not path or not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def _fmt_stat(self, value):
        value = self._to_float(value)
        if not np.isfinite(value):
            return "NA"
        if abs(value) >= 100:
            return f"{value:.1f}"
        if abs(value) >= 1:
            return f"{value:.3f}"
        return f"{value:.3g}"

    def _to_float(self, value):
        try:
            if value is None or value == "":
                return np.nan
            return float(value)
        except Exception:
            return np.nan

    def _load_continuous_slice_summary_bundle(self):
        paths = self._find_related_slice_h5_paths()
        if len(paths) <= 1:
            return None
        pieces = []
        behavior_pieces = []
        skipped = []
        reference_bands = None
        reference_states = None
        reference_occ_states = None

        for path in paths:
            try:
                with h5py.File(path, "r") as h5f:
                    if "rhythm" not in h5f or "time" not in h5f:
                        skipped.append(path)
                        continue
                    if "hourly_feature_table" not in h5f["rhythm"] or "hourly_time_s" not in h5f["time"]:
                        skipped.append(path)
                        continue
                    origin = float(h5f["time"].attrs.get("time_origin_epoch_ms", np.nan))
                    if not np.isfinite(origin):
                        skipped.append(path)
                        continue
                    band_names = (
                        [str(v) for v in h5f["rhythm/band_names"][:].astype(str)]
                        if "band_names" in h5f["rhythm"]
                        else []
                    )
                    state_names = (
                        [str(v) for v in h5f["rhythm/state_names"][:].astype(str)]
                        if "state_names" in h5f["rhythm"]
                        else []
                    )
                    occ_names = (
                        [str(v) for v in h5f["rhythm/state_names_occupancy"][:].astype(str)]
                        if "state_names_occupancy" in h5f["rhythm"]
                        else state_names
                    )
                    if reference_bands is None:
                        reference_bands = band_names
                        reference_states = state_names
                        reference_occ_states = occ_names
                    elif band_names != reference_bands or state_names != reference_states:
                        skipped.append(path)
                        continue

                    hourly_feature = np.asarray(h5f["rhythm/hourly_feature_table"][:], dtype=np.float64)
                    hourly_count = (
                        np.asarray(h5f["rhythm/hourly_feature_count"][:], dtype=np.float64)
                        if "hourly_feature_count" in h5f["rhythm"]
                        else np.isfinite(hourly_feature).astype(np.float64)
                    )
                    hour_valid_mask = (
                        np.asarray(h5f["rhythm/hour_valid_mask"][:], dtype=bool)
                        if "hour_valid_mask" in h5f["rhythm"]
                        else np.isfinite(hourly_feature).any(axis=tuple(range(1, hourly_feature.ndim)))
                    )
                    if hour_valid_mask.shape[0] != hourly_feature.shape[0]:
                        hour_valid_mask = np.ones((hourly_feature.shape[0],), dtype=bool)
                    hourly_feature = np.array(hourly_feature, copy=True)
                    hourly_count = np.array(hourly_count, copy=True)
                    hourly_feature[~hour_valid_mask] = np.nan
                    hourly_count[~hour_valid_mask] = 0
                    state_occupancy = np.asarray(h5f["rhythm/state_occupancy_table"][:], dtype=np.float64)
                    state_epoch_count = (
                        np.asarray(h5f["rhythm/state_epoch_count"][:], dtype=np.float64)
                        if "state_epoch_count" in h5f["rhythm"]
                        else np.isfinite(state_occupancy).astype(np.float64)
                    )
                    state_occupancy = np.array(state_occupancy, copy=True)
                    state_epoch_count = np.array(state_epoch_count, copy=True)
                    state_occupancy[~hour_valid_mask] = np.nan
                    state_epoch_count[~hour_valid_mask] = 0
                    state_stratified = (
                        np.asarray(h5f["rhythm/state_stratified_feature_table"][:], dtype=np.float64)
                        if "state_stratified_feature_table" in h5f["rhythm"]
                        else None
                    )
                    state_stratified_count = (
                        np.asarray(h5f["rhythm/state_stratified_count"][:], dtype=np.float64)
                        if "state_stratified_count" in h5f["rhythm"]
                        else None
                    )
                    if state_stratified is not None:
                        state_stratified = np.array(state_stratified, copy=True)
                        state_stratified[~hour_valid_mask] = np.nan
                    if state_stratified_count is not None:
                        state_stratified_count = np.array(state_stratified_count, copy=True)
                        state_stratified_count[~hour_valid_mask] = 0
                    hourly_norm_daily = self._normalize_hourly_feature_table(hourly_feature, hourly_count)
                    state_norm_daily = (
                        self._normalize_state_stratified_table(state_stratified, state_stratified_count)
                        if state_stratified is not None and state_stratified_count is not None
                        else None
                    )
                    pieces.append(
                        {
                            "path": path,
                            "origin": origin,
                            "hourly_time_s": np.asarray(h5f["time/hourly_time_s"][:], dtype=np.float64),
                            "hourly_feature": hourly_feature,
                            "hourly_count": hourly_count,
                            "hour_valid_mask": hour_valid_mask,
                            "hourly_norm_daily": hourly_norm_daily,
                            "state_occupancy": state_occupancy,
                            "state_epoch_count": state_epoch_count,
                            "state_stratified": state_stratified,
                            "state_stratified_count": state_stratified_count,
                            "state_norm_daily": state_norm_daily,
                        }
                    )

                    if "behavior" in h5f and "hourly_time_s" in h5f["behavior"]:
                        behavior = h5f["behavior"]
                        bx = (origin - 0.0) / 3600000.0 + np.asarray(behavior["hourly_time_s"][:], dtype=np.float64) / 3600.0
                        correct_flag = (
                            np.asarray(behavior["correct_flag"][:], dtype=np.int8)
                            if "correct_flag" in behavior
                            else np.zeros((0,), dtype=np.int8)
                        )
                        inferred_choice = (
                            np.asarray(behavior["inferred_choice"][:], dtype=np.int16)
                            if "inferred_choice" in behavior
                            else np.zeros((0,), dtype=np.int16)
                        )
                        behavior_pieces.append(
                            {
                                "origin": origin,
                                "x_raw": bx,
                                "hour_valid_mask": hour_valid_mask
                                if hour_valid_mask.shape == bx.shape
                                else np.ones(bx.shape, dtype=bool),
                                "trial_count": int(behavior["trial_time_s"].shape[0]) if "trial_time_s" in behavior else 0,
                                "valid_perf_count": int(np.sum(correct_flag >= 0)),
                                "choice_count": int(np.sum(np.isin(inferred_choice, [1, 2]))),
                                "hourly_rule": np.asarray(behavior["hourly_rule"][:], dtype=np.int16) if "hourly_rule" in behavior else None,
                                "hourly_perf_state": np.asarray(behavior["hourly_perf_state"][:], dtype=np.int8) if "hourly_perf_state" in behavior else None,
                                "hourly_bias_state": np.asarray(behavior["hourly_bias_state"][:], dtype=np.int8) if "hourly_bias_state" in behavior else None,
                                "hourly_perf_pct": np.asarray(behavior["hourly_perf_pct"][:], dtype=np.float64) if "hourly_perf_pct" in behavior else None,
                                "hourly_bias_score": np.asarray(behavior["hourly_bias_score"][:], dtype=np.float64) if "hourly_bias_score" in behavior else None,
                            }
                        )
            except Exception:
                skipped.append(path)

        if not pieces:
            return None
        pieces = sorted(pieces, key=lambda item: item["origin"])
        global_origin = min(piece["origin"] for piece in pieces)
        x_hours = np.concatenate([
            (piece["origin"] - global_origin) / 3600000.0 + piece["hourly_time_s"] / 3600.0
            for piece in pieces
        ])
        hourly_feature = np.concatenate([piece["hourly_feature"] for piece in pieces], axis=0)
        hourly_count = np.concatenate([piece["hourly_count"] for piece in pieces], axis=0)
        hour_valid_mask = np.concatenate([piece["hour_valid_mask"] for piece in pieces], axis=0)
        hourly_norm_daily = np.concatenate([piece["hourly_norm_daily"] for piece in pieces], axis=0)
        state_occupancy = np.concatenate([piece["state_occupancy"] for piece in pieces], axis=0)
        state_epoch_count = np.concatenate([piece["state_epoch_count"] for piece in pieces], axis=0)
        order = np.argsort(x_hours)
        x_hours = x_hours[order]
        hourly_feature = hourly_feature[order]
        hourly_count = hourly_count[order]
        hour_valid_mask = hour_valid_mask[order]
        hourly_norm_daily = hourly_norm_daily[order]
        state_occupancy = state_occupancy[order]
        state_epoch_count = state_epoch_count[order]

        hourly_norm_slice = self._normalize_hourly_feature_table(hourly_feature, hourly_count)

        state_norm_daily = None
        state_norm_slice = None
        if all(piece["state_stratified"] is not None and piece["state_stratified_count"] is not None for piece in pieces):
            state_stratified = np.concatenate([piece["state_stratified"] for piece in pieces], axis=0)[order]
            state_count = np.concatenate([piece["state_stratified_count"] for piece in pieces], axis=0)[order]
            state_norm_daily = np.concatenate(
                [piece["state_norm_daily"] for piece in pieces],
                axis=0,
            )[order]
            state_norm_slice = self._normalize_state_stratified_table(state_stratified, state_count)

        behavior_bundle = None
        if behavior_pieces:
            behavior_pieces = sorted(behavior_pieces, key=lambda item: item["origin"])
            bx = np.concatenate([piece["x_raw"] for piece in behavior_pieces]) - (global_origin / 3600000.0)
            border = np.argsort(bx)
            behavior_bundle = {
                "x_hours": bx[border],
                "hour_valid_mask": np.concatenate([piece["hour_valid_mask"] for piece in behavior_pieces])[border],
                "n_files": len(behavior_pieces),
                "trial_count": sum(piece["trial_count"] for piece in behavior_pieces),
                "valid_perf_count": sum(piece["valid_perf_count"] for piece in behavior_pieces),
                "choice_count": sum(piece["choice_count"] for piece in behavior_pieces),
            }
            for key in ["hourly_rule", "hourly_perf_state", "hourly_bias_state", "hourly_perf_pct", "hourly_bias_score"]:
                arrays = []
                for piece in behavior_pieces:
                    arr = piece.get(key)
                    if arr is None:
                        fill_value = np.nan if key in {"hourly_perf_pct", "hourly_bias_score"} else -1
                        arr = np.full(piece["x_raw"].shape, fill_value)
                    arrays.append(arr)
                behavior_bundle[key] = np.concatenate(arrays)[border]
            behavior_bundle["rt_summary"] = self._load_rt_summary_for_paths(
                [piece["path"] for piece in pieces],
                global_origin_ms=global_origin,
                x_hours=x_hours,
            )
        connectivity_summary = self._load_connectivity_summary_for_paths(
            [piece["path"] for piece in pieces],
            global_origin_ms=global_origin,
        )

        return {
            "paths": [piece["path"] for piece in pieces],
            "all_related_paths": paths,
            "skipped_paths": skipped,
            "x_hours": x_hours,
            "hourly_feature": hourly_feature,
            "hour_valid_mask": hour_valid_mask,
            "hourly_feature_norm_daily": hourly_norm_daily,
            "hourly_feature_norm_slice": hourly_norm_slice,
            "state_occupancy": state_occupancy,
            "state_norm_daily": state_norm_daily,
            "state_norm_slice": state_norm_slice,
            "band_names": reference_bands or [],
            "state_names": reference_states or [],
            "state_names_occupancy": reference_occ_states or reference_states or [],
            "behavior": behavior_bundle,
            "connectivity": connectivity_summary,
            "global_origin": global_origin,
        }

    def _render_rhythm_summary_from_bundle(self, bundle, band_info):
        self._set_summary_time_origin(bundle.get("global_origin", self.daily_start_ts_ms), self.current_timezone_name)
        self._render_behavior_summary_plot(bundle.get("behavior"))
        self._reset_legend(self.plot_hourly)
        self._reset_legend(self.plot_state_occ)
        reference_scope = self._summary_reference_scope()
        reference_label = "daily mean" if reference_scope == "daily" else "whole-slice mean"
        hourly_feature = bundle["hourly_feature"]
        hourly_plot_source = (
            bundle.get("hourly_feature_norm_daily")
            if reference_scope == "daily"
            else bundle.get("hourly_feature_norm_slice")
        )
        if hourly_plot_source is None:
            hourly_plot_source = hourly_feature
        x_hours = bundle["x_hours"]
        state_occupancy = bundle["state_occupancy"]
        state_norm = (
            bundle.get("state_norm_daily")
            if reference_scope == "daily"
            else bundle.get("state_norm_slice")
        )
        band_names = bundle.get("band_names") or band_info["active"]
        state_names = bundle.get("state_names") or []
        state_names_occupancy = bundle.get("state_names_occupancy") or state_names
        self._refresh_summary_band_combo(band_names)
        band_idx = self.summary_band_combo.currentIndex() if self.summary_band_combo.count() else 0
        if band_idx < 0 or band_idx >= len(band_names):
            band_idx = 0
        selected_band = band_names[band_idx] if band_names else "band"

        channel_values = np.asarray(hourly_plot_source[:, :, band_idx], dtype=np.float64)
        group_defs, group_names = self._get_average_channel_groups(channel_values.shape[1])
        self._plot_channel_bundle(
            self.plot_hourly,
            x_hours,
            channel_values,
            [f"Ch {idx}" for idx in range(channel_values.shape[1])],
            group_names,
            group_defs,
        )
        x_max = max(24.0, np.nanmax(x_hours) + 0.5 if x_hours.size else 24.0)
        for plot in [self.plot_hourly, self.plot_state_occ, self.plot_state_norm, self.plot_behavior]:
            self._add_night_shading(plot, x_max, x_min=0.0)
            plot.setXRange(0, x_max, padding=0)
        self._plot_connectivity_summary(bundle.get("connectivity"), x_max=x_max)
        self.plot_hourly.addLine(y=1.0, pen=pg.mkPen("#666666", width=1, style=Qt.PenStyle.DashLine))
        self.plot_hourly.setTitle(
            f"Continuous Slice Hourly {selected_band} Power (ratio to {reference_label})"
        )

        self._plot_state_occupancy_stacked(self.plot_state_occ, x_hours, state_occupancy, state_names_occupancy)
        self.plot_state_occ.setTitle("Continuous Slice Hourly State Occupancy (stacked)")

        state_norm_status = "missing"
        if state_norm is not None and np.isfinite(state_norm).any():
            state_norm_status = "available"
            selected_group_name = "All channels"
            group_options = self._refresh_summary_group_combo(state_norm.shape[2])
            group_idx = self.summary_group_combo.currentIndex() if hasattr(self, "summary_group_combo") else 0
            if group_idx < 0 or group_idx >= len(group_options):
                group_idx = 0
            selected_group_name, selected_group_channels = group_options[group_idx]
            group_data = np.asarray(state_norm[:, :, selected_group_channels, :], dtype=np.float64)
            valid_count = np.sum(np.isfinite(group_data), axis=2)
            state_norm_display = np.divide(
                np.nansum(group_data, axis=2),
                valid_count,
                out=np.full((state_norm.shape[0], state_norm.shape[1], state_norm.shape[3]), np.nan, dtype=np.float64),
                where=valid_count > 0,
            )
            row_labels = []
            rows = []
            for state_idx, state_name in enumerate(state_names):
                for band_idx2, band_name in enumerate(band_names):
                    row_labels.append(f"{state_name} | {band_name}")
                    rows.append(state_norm_display[:, state_idx, band_idx2])
            if rows:
                image = np.asarray(rows, dtype=np.float64)
                lut = _get_lookup_table(
                    "coolwarm",
                    [(59, 76, 192), (180, 205, 255), (245, 245, 245), (245, 167, 132), (180, 4, 38)],
                )
                levels = _robust_levels(image, default=(0.5, 1.5))
                _add_masked_heatmap_items(self.plot_state_norm, image, x_hours, lut, levels=levels)
                self.plot_state_norm.setYRange(-0.5, len(row_labels) - 0.5, padding=0)
                self.plot_state_norm.getAxis("left").setTicks([[(idx, label) for idx, label in enumerate(row_labels)]])
                self.plot_state_norm.setTitle(
                    f"Continuous Slice State-Normalized Power ({reference_label}) | {selected_group_name}"
                )
        else:
            self._refresh_summary_group_combo(hourly_feature.shape[1] if hourly_feature.ndim >= 2 else 1)
            self.plot_state_norm.getAxis("left").setTicks([])
            self.plot_state_norm.setTitle("Continuous Slice State-Normalized Power (missing)")

        lines = [
            "Continuous slice summary",
            f"H5 files with rhythm summary: {len(bundle['paths'])}/{len(bundle['all_related_paths'])}",
            f"Displayed hourly bins: {len(x_hours)} | X-axis labels: local date/hour",
            "Internal x coordinate remains hours from the first summarized H5 for fast plotting.",
            f"Current band: {selected_band}",
            f"Reference mode: {reference_label}",
            f"Hourly normalization: ratio to {reference_label} band mean",
            f"State normalization: ratio to {reference_label} state-band mean ({state_norm_status})",
            f"Average groups: {group_names}",
        ]
        if bundle.get("skipped_paths"):
            lines.append(f"H5 files not included yet: {len(bundle['skipped_paths'])}")
        connectivity = bundle.get("connectivity") or {}
        if connectivity.get("available"):
            lines.append(
                "Connectivity sidecars: "
                f"loaded={connectivity.get('n_loaded', 0)}, skipped={connectivity.get('n_skipped', 0)}; "
                "summary shows mean PLV/PAC only."
            )
        else:
            lines.append(
                "Connectivity sidecars: missing. Run Paired Trial Context or connectivity feature extraction first."
            )
        lines.append("")
        if bundle.get("behavior") is not None:
            lines.extend(self._behavior_summary_lines(bundle.get("behavior")))
        else:
            lines.append("Behavior: no behavior data in summarized slice H5 files.")
        self.summary_txt.setPlainText("\n".join(lines).strip())

    def _render_rhythm_summary(self):
        self.plot_hourly.clear()
        self.plot_state_norm.clear()
        self.plot_state_occ.clear()
        self.plot_behavior.clear()
        if hasattr(self, "plot_connectivity"):
            self.plot_connectivity.clear()
        self.summary_txt.clear()
        if self.h5_file is None:
            return
        self._render_behavior_summary_plot()
        band_info = self._get_available_band_info()
        self._refresh_summary_band_combo(band_info["active"])
        continuous_bundle = self._load_continuous_slice_summary_bundle()
        if continuous_bundle is not None:
            self._render_rhythm_summary_from_bundle(continuous_bundle, band_info)
            return

        if "hourly_feature_table" not in self.h5_file["rhythm"] or "hourly_time_s" not in self.h5_file["time"]:
            message = [
                "No rhythm summary datasets found yet.",
                "Please run Extract Features -> Classify States -> Summarize Rhythm.",
                "",
                f"Active bands ({band_info['source']}): {band_info['active'] if band_info['active'] else 'none'}",
                "",
                *self._behavior_summary_lines(),
            ]
            if band_info["mismatch"]:
                message.extend(
                    [
                        "",
                        "Config / H5 band mismatch detected.",
                        "Rerun the downstream analysis to refresh the displayed bands.",
                    ]
                )
            self.summary_txt.setPlainText("\n".join(message))
            return

        self._reset_legend(self.plot_hourly)
        self._reset_legend(self.plot_state_occ)

        hourly_feature = np.asarray(self.h5_file["rhythm/hourly_feature_table"][:], dtype=np.float64)
        hourly_feature_norm = (
            np.asarray(
                self.h5_file["rhythm/hourly_feature_table_normalized"][:],
                dtype=np.float64,
            )
            if "hourly_feature_table_normalized" in self.h5_file["rhythm"]
            else None
        )
        if hourly_feature_norm is None and "hourly_feature_count" in self.h5_file["rhythm"]:
            hourly_count = np.asarray(self.h5_file["rhythm/hourly_feature_count"][:], dtype=np.float64)
            weighted_sum = np.nansum(hourly_feature * hourly_count, axis=0)
            total_count = np.sum(hourly_count, axis=0)
            hourly_reference = np.divide(
                weighted_sum,
                total_count,
                out=np.full_like(weighted_sum, np.nan, dtype=np.float64),
                where=total_count > 0,
            )
            hourly_feature_norm = np.divide(
                hourly_feature,
                hourly_reference[np.newaxis, ...],
                out=np.full_like(hourly_feature, np.nan, dtype=np.float64),
                where=np.isfinite(hourly_reference[np.newaxis, ...]) & (hourly_reference[np.newaxis, ...] > 0),
            )
        hourly_time_s = np.asarray(self.h5_file["time/hourly_time_s"][:], dtype=np.float64)
        hour_valid_mask = (
            np.asarray(self.h5_file["rhythm/hour_valid_mask"][:], dtype=bool)
            if "hour_valid_mask" in self.h5_file["rhythm"]
            else np.ones(hourly_time_s.shape, dtype=bool)
        )
        if hour_valid_mask.shape != hourly_time_s.shape:
            hour_valid_mask = np.ones(hourly_time_s.shape, dtype=bool)
        hourly_feature = np.array(hourly_feature, copy=True)
        hourly_feature[~hour_valid_mask] = np.nan
        if hourly_feature_norm is not None:
            hourly_feature_norm = np.array(hourly_feature_norm, copy=True)
            hourly_feature_norm[~hour_valid_mask] = np.nan
        state_occupancy = np.asarray(self.h5_file["rhythm/state_occupancy_table"][:], dtype=np.float64)
        state_occupancy = np.array(state_occupancy, copy=True)
        state_occupancy[~hour_valid_mask] = np.nan
        state_norm = (
            np.asarray(
                self.h5_file["rhythm/state_stratified_feature_table_normalized"][:],
                dtype=np.float64,
            )
            if "state_stratified_feature_table_normalized" in self.h5_file["rhythm"]
            else None
        )
        if state_norm is not None:
            state_norm = np.array(state_norm, copy=True)
            state_norm[~hour_valid_mask] = np.nan
        band_names = (
            [str(v) for v in self.h5_file["rhythm/band_names"][:].astype(str)]
            if "band_names" in self.h5_file["rhythm"]
            else band_info["active"]
        )
        state_names = [str(v) for v in self.h5_file["rhythm/state_names"][:].astype(str)]
        state_names_occupancy = (
            [str(v) for v in self.h5_file["rhythm/state_names_occupancy"][:].astype(str)]
            if "state_names_occupancy" in self.h5_file["rhythm"]
            else state_names
        )
        self._refresh_summary_band_combo(band_names)
        x_hours = hourly_time_s / 3600.0
        band_idx = self.summary_band_combo.currentIndex() if self.summary_band_combo.count() else 0
        if band_idx < 0 or band_idx >= len(band_names):
            band_idx = 0
        selected_band = band_names[band_idx] if band_names else "band"
        reference_scope = self._summary_reference_scope()
        reference_label = "daily mean" if reference_scope == "daily" else "whole-slice mean (current H5 only)"

        hourly_plot_source = hourly_feature_norm if hourly_feature_norm is not None else hourly_feature
        if hourly_plot_source.ndim == 3:
            channel_values = np.asarray(hourly_plot_source[:, :, band_idx], dtype=np.float64)
        else:
            channel_values = np.asarray(hourly_plot_source[:, band_idx], dtype=np.float64)[:, np.newaxis]

        group_defs, group_names = self._get_average_channel_groups(channel_values.shape[1])
        self._plot_channel_bundle(
            self.plot_hourly,
            x_hours,
            channel_values,
            [f"Ch {idx}" for idx in range(channel_values.shape[1])],
            group_names,
            group_defs,
        )
        x_max = max(24.0, np.nanmax(x_hours) + 0.5 if x_hours.size else 24.0)
        self._add_night_shading(self.plot_hourly, x_max, x_min=0.0)
        self._add_night_shading(self.plot_state_occ, x_max, x_min=0.0)
        self._add_night_shading(self.plot_state_norm, x_max, x_min=0.0)
        self.plot_hourly.setXRange(0, x_max, padding=0)
        self.plot_behavior.setXRange(0, x_max, padding=0)
        single_connectivity = self._load_connectivity_summary_for_paths(
            [self.h5_path],
            global_origin_ms=float(
                self.h5_file["time"].attrs.get(
                    "time_origin_epoch_ms",
                    self.h5_file["metadata"].attrs.get("time_origin_epoch_ms", self.daily_start_ts_ms)
                    if "metadata" in self.h5_file
                    else self.daily_start_ts_ms,
                )
            ),
        )
        self._plot_connectivity_summary(single_connectivity, x_max=x_max)
        self.plot_hourly.addLine(y=1.0, pen=pg.mkPen("#666666", width=1, style=Qt.PenStyle.DashLine))
        self.plot_hourly.setTitle(f"Hourly {selected_band} Power By Channel (ratio to {reference_label})")

        self._plot_state_occupancy_stacked(
            self.plot_state_occ,
            x_hours,
            state_occupancy,
            state_names_occupancy,
        )
        self.plot_state_occ.setXRange(0, x_max, padding=0)
        self.plot_state_occ.setTitle("Hourly State Occupancy (stacked)")

        state_norm_status = "available"
        if state_norm is not None and np.isfinite(state_norm).any():
            selected_group_name = "All channels"
            if state_norm.ndim == 4:
                group_options = self._refresh_summary_group_combo(state_norm.shape[2])
                group_idx = self.summary_group_combo.currentIndex() if hasattr(self, "summary_group_combo") else 0
                if group_idx < 0 or group_idx >= len(group_options):
                    group_idx = 0
                selected_group_name, selected_group_channels = group_options[group_idx]
                group_data = np.asarray(state_norm[:, :, selected_group_channels, :], dtype=np.float64)
                valid_count = np.sum(np.isfinite(group_data), axis=2)
                state_norm_display = np.divide(
                    np.nansum(group_data, axis=2),
                    valid_count,
                    out=np.full((state_norm.shape[0], state_norm.shape[1], state_norm.shape[3]), np.nan, dtype=np.float64),
                    where=valid_count > 0,
                )
            else:
                self._refresh_summary_group_combo(1)
                state_norm_display = state_norm
            row_labels = []
            rows = []
            separator_positions = []
            for state_idx, state_name in enumerate(state_names):
                for band_idx, band_name in enumerate(band_names):
                    row_labels.append(f"{state_name} | {band_name}")
                    rows.append(state_norm_display[:, state_idx, band_idx])
                if state_idx < len(state_names) - 1:
                    row_labels.append("")
                    rows.append(np.full(state_norm_display.shape[0], np.nan, dtype=np.float64))
                    separator_positions.append(len(row_labels) - 1.5)
            if rows:
                image = np.asarray(rows, dtype=np.float64)
                lut = _get_lookup_table(
                    "coolwarm",
                    [
                        (59, 76, 192),
                        (180, 205, 255),
                        (245, 245, 245),
                        (245, 167, 132),
                        (180, 4, 38),
                    ],
                )
                levels = _robust_levels(image, default=(0.5, 1.5))
                _add_masked_heatmap_items(self.plot_state_norm, image, x_hours, lut, levels=levels)
                self.plot_state_norm.setYRange(-0.5, len(row_labels) - 0.5, padding=0)
                self.plot_state_norm.setXRange(0, x_max, padding=0)
                for pos in separator_positions:
                    self.plot_state_norm.addItem(
                        pg.InfiniteLine(
                            pos=pos,
                            angle=0,
                            pen=pg.mkPen("#333333", width=1, style=Qt.PenStyle.DashLine),
                        )
                    )
                ticks = [(idx, label) for idx, label in enumerate(row_labels) if label]
                self.plot_state_norm.getAxis("left").setTicks([ticks])
                self.plot_state_norm.setTitle(
                    f"State-Normalized Hourly Power ({reference_label}) | {selected_group_name}"
                )
        elif state_norm is not None:
            self._refresh_summary_group_combo(state_norm.shape[2] if state_norm.ndim == 4 else 1)
            state_norm_status = "dataset_exists_but_all_nan"
            self.plot_state_norm.getAxis("left").setTicks([])
            self.plot_state_norm.setTitle(
                "State-Normalized Hourly Power (dataset exists but all values are NaN)"
            )
        else:
            self._refresh_summary_group_combo(hourly_feature.shape[1] if hourly_feature.ndim >= 2 else 1)
            state_norm_status = "missing"
            self.plot_state_norm.getAxis("left").setTicks([])
            self.plot_state_norm.setTitle(
                "State-Normalized Hourly Power (missing; please rerun Summarize Rhythm)"
            )

        lines = []
        if "state_label" in self.h5_file["states"]:
            labels = np.asarray(self.h5_file["states/state_label"][:], dtype=np.int16)
            valid_mask = (
                np.asarray(self.h5_file["states/state_valid_mask"][:], dtype=bool)
                if "state_valid_mask" in self.h5_file["states"]
                else np.ones_like(labels, dtype=bool)
            )
            state_names_all = (
                [str(v) for v in self.h5_file["states/state_names"][:].astype(str)]
                if "state_names" in self.h5_file["states"]
                else []
            )
            unique, counts = np.unique(labels[valid_mask], return_counts=True)
            label_summary = {}
            for code, count in zip(unique.tolist(), counts.tolist()):
                if 0 <= int(code) < len(state_names_all):
                    label_summary[state_names_all[int(code)]] = int(count)
                else:
                    label_summary[int(code)] = int(count)
            lines.append(f"State counts: {label_summary}")
            lines.append(f"State-valid epochs: {np.mean(valid_mask) * 100.0:.2f}%")
        norm_method = self.h5_file["rhythm"].attrs.get("state_reference_normalization_method", None)
        if norm_method is None and state_norm is not None:
            norm_method = "divide_by_daily_state_band_mean"
        elif norm_method is None:
            norm_method = "missing; please rerun Summarize Rhythm"
        norm_method = str(norm_method)
        hourly_norm_method = str(
            self.h5_file["rhythm"].attrs.get(
                "hourly_reference_normalization_method",
                "divide_by_daily_band_mean" if hourly_feature_norm is not None else "missing; please rerun Summarize Rhythm",
            )
        )
        lines.append(f"Current band: {selected_band}")
        lines.append(f"Reference mode: {reference_label}")
        lines.append(f"Band source: {band_info['source']}")
        if band_info["mismatch"]:
            lines.append("Band mismatch: config and H5 analysis bands are different.")
        lines.append(f"Hourly normalization: {hourly_norm_method}")
        lines.append("")
        lines.append(f"State normalization: {norm_method}")
        lines.append(f"Normalized hourly dataset: {state_norm_status}")
        lines.append(f"Average groups: {group_names}")
        if hasattr(self, "summary_group_combo") and self.summary_group_combo.count():
            lines.append(f"State-normalized display group: {self.summary_group_combo.currentText()}")
        if single_connectivity.get("available"):
            lines.append(
                "Connectivity sidecar: "
                f"loaded={single_connectivity.get('n_loaded', 0)}, skipped={single_connectivity.get('n_skipped', 0)}; "
                "summary shows mean PLV/PAC only."
            )
        else:
            lines.append("Connectivity sidecar: missing. Run Paired Trial Context or connectivity feature extraction first.")
        lines.append("")
        lines.extend(self._behavior_summary_lines())
        states_grp = self.h5_file["states"]
        representative_channel = (
            int(states_grp["representative_channel_index"][()])
            if "representative_channel_index" in states_grp
            else -1
        )
        nrem_lfhf_threshold = float(states_grp["nrem_lfhf_threshold"][()]) if "nrem_lfhf_threshold" in states_grp else np.nan
        rem_highfreq_threshold = float(states_grp["rem_highfreq_threshold"][()]) if "rem_highfreq_threshold" in states_grp else np.nan
        sleep_imu_threshold = float(states_grp["sleep_imu_threshold"][()]) if "sleep_imu_threshold" in states_grp else np.nan
        imu_smoothing_sigma_s = float(states_grp["imu_smoothing_sigma_s"][()]) if "imu_smoothing_sigma_s" in states_grp else np.nan
        threshold_method = (
            _decode_scalar_string(states_grp["threshold_method_json"][()])
            if "threshold_method_json" in states_grp
            else "{}"
        )
        miniwake_fraction = (
            float(np.mean(labels[valid_mask] == STATE_CODES["MiniWake"]) * 100.0)
            if "state_label" in self.h5_file["states"] and np.any(valid_mask)
            else 0.0
        )
        lines.append("")
        lines.append(
            f"Representative channel: {representative_channel} | MiniWake: {miniwake_fraction:.2f}%"
        )
        lines.append(
            "Thresholds: "
            f"Sleep-IMU={sleep_imu_threshold:.4g} | "
            f"NREM LF/HF score={nrem_lfhf_threshold:.4g} | "
            f"REM 80-250Hz score={rem_highfreq_threshold:.4g}"
        )
        lines.append(f"Shared smoothing sigma: {imu_smoothing_sigma_s:.2f}s")
        lines.append(f"Threshold methods: {threshold_method}")
        if "review" in self.h5_file["states"]:
            lines.append(f"Review complete: {bool(self.h5_file['states/review'].attrs.get('review_complete', False))}")
        if self.report_path:
            lines.append(f"Report path: {self.report_path}")
        self.summary_txt.setPlainText("\n".join(lines).strip())

    def on_channel_toggled(self):
        for idx, cb in enumerate(self.lfp_checkboxes):
            self.lfp_channels_visible[idx] = cb.isChecked()
        for idx, cb in enumerate(self.imu_checkboxes):
            self.imu_channels_visible[idx] = cb.isChecked()
        self._fetch_and_draw_detail()

    def on_region_drag_update(self):
        min_x, max_x = self.region.getRegion()
        self.statusBar.showMessage(
            f"Selected region: {datetime.timedelta(seconds=int(min_x))} to {datetime.timedelta(seconds=int(max_x))}",
            2000,
        )

    def on_btn_render_clicked(self):
        min_x, max_x = self.region.getRegion()
        min_x = max(0.0, min_x)
        max_x = min(float(self.total_seconds), max_x)
        self.plot_lfp.setXRange(min_x, max_x, padding=0)
        self._fetch_and_draw_detail()

    def _fetch_and_draw_detail(self):
        if self.h5_file is None:
            return

        min_x, max_x = self.region.getRegion()
        min_x = max(0.0, min_x)
        max_x = min(float(self.total_seconds), max_x)
        if max_x <= min_x:
            return

        self.plot_lfp.clear()
        self.plot_spec.clear()
        freq_min_hz, freq_max_hz = self._get_selected_spectrogram_freq_range()
        freq_label = self._format_spectrogram_freq_range()
        self.plot_spec.setTitle(f"Spectrogram ({freq_label}, First Visible LFP Channel)")
        self.plot_spec.setYRange(freq_min_hz, freq_max_hz, padding=0)
        self.plot_imu.clear()
        self.plot_masks.clear()

        if "raw_lfp" in self.h5_file["lfp"]:
            lfp_start = int(min_x * self.lfp_fs)
            lfp_end = min(int(max_x * self.lfp_fs), self.h5_file["lfp/raw_lfp"].shape[1])
            lfp_slice = np.asarray(self.h5_file["lfp/raw_lfp"][:, lfp_start:lfp_end], dtype=np.float64)
            time_lfp = np.linspace(min_x, max_x, max(1, lfp_slice.shape[1]))
            step = max(1, lfp_slice.shape[1] // 200000) if lfp_slice.shape[1] > 0 else 1
            colors = [(20, 120, 200), (40, 160, 40), (200, 50, 50), (200, 150, 20), (120, 20, 200)]
            for idx in range(min(16, lfp_slice.shape[0])):
                if self.lfp_channels_visible[idx]:
                    self.plot_lfp.plot(
                        x=time_lfp[::step],
                        y=lfp_slice[idx][::step] - idx * self.channel_offset,
                        pen=pg.mkColor(colors[idx % len(colors)]),
                    )

            cov = np.asarray(self.h5_file["lfp/file_coverage_mask"][lfp_start:lfp_end], dtype=bool)
            miss = np.asarray(self.h5_file["lfp/missing_mask"][lfp_start:lfp_end], dtype=bool)
            mode3 = np.asarray(self.h5_file["task/mode3_active_mask"][lfp_start:lfp_end], dtype=bool)
            working = (
                np.asarray(self.h5_file["task/working_mask"][lfp_start:lfp_end], dtype=bool)
                if "working_mask" in self.h5_file["task"]
                else np.zeros_like(mode3)
            )
            mask_step = max(1, len(cov) // 50000) if len(cov) > 0 else 1
            x_mask = time_lfp[::mask_step]
            self.plot_masks.plot(x=x_mask, y=cov[::mask_step].astype(float) + 0.0, pen=(50, 200, 50))
            self.plot_masks.plot(x=x_mask, y=miss[::mask_step].astype(float) + 1.0, pen=(255, 50, 50))
            self.plot_masks.plot(x=x_mask, y=mode3[::mask_step].astype(float) + 2.0, pen=(50, 50, 255))
            self.plot_masks.plot(x=x_mask, y=working[::mask_step].astype(float) + 3.0, pen=(160, 70, 200))

            visible_indices = [idx for idx, visible in enumerate(self.lfp_channels_visible) if visible]
            if visible_indices:
                spec_idx = visible_indices[0]
                global_levels = self._estimate_global_spectrogram_levels(spec_idx, freq_min_hz, freq_max_hz)
                spec_signal = np.asarray(lfp_slice[spec_idx], dtype=np.float64)
                finite = np.isfinite(spec_signal)
                if np.any(finite):
                    fill_value = float(np.nanmedian(spec_signal[finite]))
                    spec_signal = np.nan_to_num(spec_signal, nan=fill_value)
                    max_spec_samples = 250000
                    decim = max(1, int(np.ceil(spec_signal.size / max_spec_samples)))
                    spec_signal = spec_signal[::decim]
                    spec_fs = self.lfp_fs / decim
                    if spec_signal.size >= 128:
                        nperseg = min(1024, spec_signal.size)
                        noverlap = int(nperseg * 0.75)
                        freqs, times, sxx = signal.spectrogram(
                            spec_signal,
                            fs=spec_fs,
                            nperseg=nperseg,
                            noverlap=noverlap,
                            scaling="density",
                            mode="psd",
                        )
                        freq_mask = (freqs >= float(freq_min_hz)) & (freqs <= float(freq_max_hz))
                        freqs = freqs[freq_mask]
                        sxx = sxx[freq_mask]
                        if freqs.size and sxx.size:
                            sxx_db = 10.0 * np.log10(np.maximum(sxx, 1e-12))
                            img = _make_image_item(
                                sxx_db,
                                _get_lookup_table(
                                    "viridis",
                                    [
                                        (68, 1, 84),
                                        (59, 82, 139),
                                        (33, 145, 140),
                                        (94, 201, 98),
                                        (253, 231, 37),
                                    ],
                                ),
                                levels=global_levels if global_levels is not None else _robust_levels(sxx_db),
                            )
                            t0 = float(min_x + (times[0] if times.size else 0.0))
                            t1 = float(min_x + (times[-1] if times.size else (max_x - min_x)))
                            f0 = float(freqs[0])
                            f1 = float(freqs[-1]) if freqs.size > 1 else float(freqs[0] + 1.0)
                            img.setRect(QRectF(t0, f0, max(t1 - t0, 1e-6), max(f1 - f0, 1e-6)))
                            self.plot_spec.addItem(img)
                            self.plot_spec.setYRange(freq_min_hz, freq_max_hz, padding=0)
                            self.plot_spec.setTitle(
                                f"Spectrogram | Ch {spec_idx} | {freq_label} (H5 global color scale)"
                            )
                        else:
                            self.plot_spec.setTitle(
                                f"Spectrogram | Ch {spec_idx} | {freq_label} (insufficient spectral bins)"
                            )
                    else:
                        self.plot_spec.setTitle(f"Spectrogram | Ch {spec_idx} | {freq_label} (window too short)")
                else:
                    self.plot_spec.setTitle(f"Spectrogram | {freq_label} | Selected channel is all-NaN")
            else:
                self.plot_spec.setTitle(f"Spectrogram | {freq_label} | No visible LFP channel selected")
        else:
            self.plot_spec.setTitle(f"Spectrogram | {freq_label} | No raw LFP dataset found in this H5")

        imu_start = int(min_x * self.imu_fs)
        imu_end = min(int(max_x * self.imu_fs), self.h5_file["imu/accel_xyz"].shape[1])
        imu_slice = np.asarray(self.h5_file["imu/accel_xyz"][:, imu_start:imu_end], dtype=np.float64)
        if imu_slice.shape[1] > 0:
            time_imu = np.linspace(min_x, max_x, max(1, imu_slice.shape[1]))
            step = max(1, imu_slice.shape[1] // 200000)
            colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255)]
            for idx in range(min(3, imu_slice.shape[0])):
                if self.imu_channels_visible[idx]:
                    self.plot_imu.plot(x=time_imu[::step], y=imu_slice[idx][::step], pen=pg.mkColor(colors[idx]))

        self._update_trial_info(min_x, max_x)
        self.statusBar.showMessage(
            f"Showing {datetime.timedelta(seconds=int(min_x))} to {datetime.timedelta(seconds=int(max_x))}",
            5000,
        )

    def _update_trial_info(self, min_x, max_x):
        start_ms = min_x * 1000.0
        end_ms = max_x * 1000.0
        text = (
            f"Region: {datetime.timedelta(seconds=int(min_x))} "
            f"to {datetime.timedelta(seconds=int(max_x))}\n"
        )
        found = False
        for key, value in self.trial_dict.items():
            block_start = value.get("start_ts", 0) - self.daily_start_ts_ms
            block_end = value.get("end_ts", 0) - self.daily_start_ts_ms
            if block_start <= end_ms and block_end >= start_ms:
                found = True
                trials = value.get("trials", [])
                text += f"\nWorking Block:\n{key}\n{len(trials)} trials: {trials[:12]}\n"
        if "working_bouts_auto_seconds" in self.h5_file["task"]:
            bouts = np.asarray(self.h5_file["task/working_bouts_auto_seconds"][:], dtype=np.float64)
            overlaps = bouts[(bouts[:, 0] <= max_x) & (bouts[:, 1] >= min_x)] if bouts.size else np.zeros((0, 2))
            text += f"\nAuto working bouts in region: {len(overlaps)}"
            if overlaps.size:
                text += "\n" + "\n".join([f"  {s:.1f}s -> {e:.1f}s" for s, e in overlaps[:8]])
        if "working_bouts_confirmed_seconds" in self.h5_file["task"]:
            bouts = np.asarray(self.h5_file["task/working_bouts_confirmed_seconds"][:], dtype=np.float64)
            overlaps = bouts[(bouts[:, 0] <= max_x) & (bouts[:, 1] >= min_x)] if bouts.size else np.zeros((0, 2))
            text += f"\nConfirmed working bouts in region: {len(overlaps)}"
            if overlaps.size:
                text += "\n" + "\n".join([f"  {s:.1f}s -> {e:.1f}s" for s, e in overlaps[:8]])
        if not found and "working_bouts_auto_seconds" not in self.h5_file["task"] and "working_bouts_confirmed_seconds" not in self.h5_file["task"]:
            text += "\nNo Mode3 / working block overlaps this region."
        self.trial_info_txt.setPlainText(text)

    def closeEvent(self, event):
        if self.slice_build_process is not None and self.slice_build_process.is_alive():
            try:
                self.append_log("Stopping background natural-day H5 build before closing GUI.")
                self.slice_build_process.terminate()
                self.slice_build_process.join(timeout=1.0)
            except Exception:
                pass
        if self.slice_build_poll_timer.isActive():
            self.slice_build_poll_timer.stop()
        self._close_h5_handle()
        super().closeEvent(event)


def launch_gui(default_h5_file=None, default_config=None):
    app = QApplication.instance()
    owns_app = app is None
    if app is None:
        app = QApplication(sys.argv)
    window = H5ViewerApp(default_h5_file=default_h5_file, default_config=default_config)
    window.show()
    if owns_app:
        return app.exec()
    return window


if __name__ == "__main__":
    multiprocessing.freeze_support()
    default_path = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(launch_gui(default_h5_file=default_path))
