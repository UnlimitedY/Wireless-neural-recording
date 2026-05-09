import math

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .config import resolve_config
from .qt_async import run_callable_in_thread
from .state_classification import (
    ANALYSIS_STATE_NAMES,
    OCCUPANCY_STATE_NAMES,
    STATE_CODES,
    _compute_lfhf_score_from_band_powers,
    _gaussian_smooth_nan,
    commit_reviewed_states,
    compute_state_review_inputs,
    load_review_progress,
    merge_saved_review_state,
    preview_bimodal_threshold,
    recalculate_review_state,
    refresh_review_state_after_working,
    save_review_progress,
)


class ClockAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        out = []
        for value in values:
            total_seconds = int(round(float(value)))
            hours = (total_seconds // 3600) % 24
            minutes = (total_seconds % 3600) // 60
            out.append(f"{hours:02d}:{minutes:02d}")
        return out


def _state_color_map():
    return {
        "Wake": "#2f80ed",
        "MiniWake": "#7f7f7f",
        "NREM": "#f2c94c",
        "REM": "#eb5757",
        "Working": "#9b51e0",
    }


def _make_vertical_region(values, movable=False, brush=None, pen=None):
    try:
        return pg.LinearRegionItem(values=values, orientation="vertical", movable=movable, brush=brush, pen=pen)
    except TypeError:
        pass
    orientation_enum = getattr(getattr(pg.LinearRegionItem, "Orientation", None), "Vertical", None)
    if orientation_enum is not None:
        return pg.LinearRegionItem(values=values, orientation=orientation_enum, movable=movable, brush=brush, pen=pen)
    item = pg.LinearRegionItem(values=values, movable=movable, brush=brush, pen=pen)
    try:
        item.setOrientation("vertical")
    except Exception:
        pass
    return item


def _connect_line_interaction(line, move_callback=None, finish_callback=None):
    if move_callback is not None:
        line.sigPositionChanged.connect(lambda *_: move_callback())
    if finish_callback is not None:
        signal = getattr(line, "sigPositionChangeFinished", None)
        if signal is not None:
            signal.connect(finish_callback)
        elif move_callback is None:
            line.sigPositionChanged.connect(lambda *_: finish_callback())


def _connect_line_release(line, callback):
    _connect_line_interaction(line, finish_callback=callback)


def _configure_signed_threshold_spin(spinbox, decimals=6, step=0.01, limit=1_000_000_000.0):
    spinbox.setDecimals(int(decimals))
    spinbox.setSingleStep(float(step))
    spinbox.setRange(-float(limit), float(limit))


def _downsample_line(time_s, values, max_points=12000):
    arr_t = np.asarray(time_s, dtype=np.float64)
    arr_v = np.asarray(values, dtype=np.float64)
    if arr_t.size <= max_points:
        return arr_t, arr_v
    step = max(1, int(math.ceil(arr_t.size / max_points)))
    return arr_t[::step], arr_v[::step]


def _downsample_mask(mask, fs, total_duration_s, target_points=5000):
    mask_arr = np.asarray(mask, dtype=np.float64)
    if mask_arr.size == 0:
        return np.zeros((0,), dtype=np.float64), np.zeros((0,), dtype=np.float64)
    bin_size = max(1, int(math.ceil(mask_arr.size / target_points)))
    trimmed = mask_arr[: (mask_arr.size // bin_size) * bin_size]
    if trimmed.size == 0:
        trimmed = mask_arr
        bin_size = mask_arr.size
    reshaped = trimmed.reshape(-1, bin_size)
    y = np.mean(reshaped, axis=1)
    x = (np.arange(y.size, dtype=np.float64) * bin_size + bin_size / 2.0) / fs
    if x.size == 0:
        x = np.linspace(0.0, total_duration_s, 1, dtype=np.float64)
        y = np.zeros((1,), dtype=np.float64)
    return x, y


def _mask_tracks_from_labels(labels, state_names):
    labels_arr = np.asarray(labels, dtype=np.int16)
    return {name: labels_arr == STATE_CODES[name] for name in state_names}


def _build_bout_summary(mask, fs):
    mask_arr = np.asarray(mask, dtype=bool)
    padded = np.concatenate([[False], mask_arr, [False]])
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    if starts.size == 0:
        return "0 bouts"
    durations = (ends - starts) / float(fs)
    return (
        f"{starts.size} bouts | "
        f"mean {np.mean(durations):.1f}s | "
        f"median {np.median(durations):.1f}s | "
        f"max {np.max(durations):.1f}s"
    )


class BaseReviewDialog(QDialog):
    def __init__(self, review_state, config, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1320, 860)
        self.review_state = review_state
        self.cfg = resolve_config(config)
        self.state_colors = _state_color_map()
        self.layout_root = QVBoxLayout(self)
        self.local_progress_widget = QWidget()
        local_progress_layout = QHBoxLayout(self.local_progress_widget)
        local_progress_layout.setContentsMargins(0, 0, 0, 0)
        local_progress_layout.setSpacing(8)
        self.local_progress_title = QLabel("Step Progress")
        self.local_progress_title.setStyleSheet("font-weight: bold;")
        self.local_progress_label = QLabel("Idle")
        self.local_progress_bar = QProgressBar()
        self.local_progress_bar.setMinimum(0)
        self.local_progress_bar.setMaximum(100)
        self.local_progress_bar.setValue(0)
        self.local_progress_bar.setVisible(False)
        local_progress_layout.addWidget(self.local_progress_title)
        local_progress_layout.addWidget(self.local_progress_label, 1)
        local_progress_layout.addWidget(self.local_progress_bar, 2)
        self.layout_root.addWidget(self.local_progress_widget)

    def _make_time_plot(self, title, y_label):
        axis = ClockAxisItem(orientation="bottom")
        plot = pg.PlotWidget(title=title, axisItems={"bottom": axis})
        plot.setLabel("bottom", "Time")
        plot.setLabel("left", y_label)
        plot.showGrid(x=True, y=True, alpha=0.25)
        return plot

    def _make_mask_plot(self, title="State Preview"):
        plot = self._make_time_plot(title, "Track")
        return plot

    def _set_track_ticks(self, plot_widget, labels):
        ticks = [(idx, label) for idx, label in enumerate(labels)]
        plot_widget.getAxis("left").setTicks([ticks])

    def _plot_boolean_tracks(self, plot_widget, time_s, track_items, clear=True):
        if clear:
            plot_widget.clear()
        labels = []
        for idx, (label, values, color) in enumerate(track_items):
            labels.append(label)
            plot_widget.plot(
                x=np.asarray(time_s, dtype=np.float64),
                y=np.asarray(values, dtype=np.float64) + idx,
                pen=pg.mkPen(color, width=2),
                name=label,
            )
        if labels:
            self._set_track_ticks(plot_widget, labels)
            plot_widget.setYRange(-0.5, len(labels) - 0.5, padding=0.05)

    def _plot_locked_state_tracks(self, plot_widget, extra_tracks=None, clear=True):
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        tracks = [
            ("Working", np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Working"]),
        ]
        if extra_tracks:
            tracks.extend(extra_tracks)
        self._plot_boolean_tracks(plot_widget, state_time, tracks, clear=clear)
        plot_widget.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _show_error(self, title, message):
        QMessageBox.critical(self, title, str(message))

    def _start_local_progress(self, text, busy=True):
        self.local_progress_label.setText(str(text))
        self.local_progress_bar.setVisible(True)
        if busy:
            self.local_progress_bar.setRange(0, 0)
        else:
            self.local_progress_bar.setRange(0, 100)
            self.local_progress_bar.setValue(0)

    def _finish_local_progress(self, text="Idle"):
        self.local_progress_label.setText(str(text))
        self.local_progress_bar.setVisible(False)
        self.local_progress_bar.setRange(0, 100)
        self.local_progress_bar.setValue(0)

    def _run_local_task(self, text, func):
        self._start_local_progress(text, busy=True)
        try:
            result = run_callable_in_thread(func)
        except Exception:
            self._finish_local_progress("Failed")
            raise
        self._finish_local_progress("Done")
        return result


class WorkingReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, parent=None):
        super().__init__(review_state, config, "Step 0 - Review Working / Mode3", parent=parent)

        self.overview_plot = self._make_time_plot("24h Mode3 / Working Overview", "Track")
        self.detail_plot = self._make_time_plot("Editable Detail", "Track")
        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(110)
        self.region_status = QLabel()

        self.overview_region = _make_vertical_region((0.0, min(1800.0, self.review_state["total_duration_s"])), movable=True)
        self.overview_plot.addItem(self.overview_region)
        self.overview_region.sigRegionChanged.connect(self._update_region_status)
        self.edit_region = _make_vertical_region((0.0, min(60.0, self.review_state["total_duration_s"])), movable=True, brush=pg.mkBrush(50, 150, 255, 30), pen=pg.mkPen("#1f77b4", width=2))
        self.detail_plot.addItem(self.edit_region)

        buttons_row = QHBoxLayout()
        btn_render = QPushButton("Render Current Selection")
        btn_add = QPushButton("Add Selected To Working")
        btn_remove = QPushButton("Remove Selected From Working")
        btn_reset = QPushButton("Reset To Auto")
        buttons_row.addWidget(btn_render)
        buttons_row.addWidget(btn_add)
        buttons_row.addWidget(btn_remove)
        buttons_row.addWidget(btn_reset)
        btn_render.clicked.connect(self._render_detail)
        btn_add.clicked.connect(self._add_region)
        btn_remove.clicked.connect(self._remove_region)
        btn_reset.clicked.connect(self._reset_auto)

        self.layout_root.addWidget(self.overview_plot)
        self.layout_root.addWidget(self.detail_plot)
        self.layout_root.addLayout(buttons_row)
        self.layout_root.addWidget(self.region_status)
        self.layout_root.addWidget(self.info_box)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self.layout_root.addWidget(self.button_box)

        self._render_all()

    def _apply_region(self, add=True):
        start_s, end_s = self.edit_region.getRegion()
        start_s = max(0.0, float(start_s))
        end_s = min(float(self.review_state["total_duration_s"]), float(end_s))
        if end_s <= start_s:
            return
        start = int(round(start_s * self.review_state["lfp_fs"]))
        end = int(round(end_s * self.review_state["lfp_fs"]))
        mask = np.asarray(self.review_state["working_mask_confirmed"], dtype=bool).copy()
        mask[start:end] = add
        self.review_state["working_mask_confirmed"] = mask
        self._render_all()

    def _add_region(self):
        self._apply_region(add=True)

    def _remove_region(self):
        self._apply_region(add=False)

    def _reset_auto(self):
        self.review_state["working_mask_confirmed"] = np.asarray(self.review_state["working_mask_auto"], dtype=bool).copy()
        self._render_all()

    def _render_overview(self):
        self.overview_plot.clear()
        self.overview_plot.addItem(self.overview_region)
        total_duration_s = float(self.review_state["total_duration_s"])
        x_mode3, y_mode3 = _downsample_mask(self.review_state["mode3_mask"], self.review_state["lfp_fs"], total_duration_s)
        x_auto, y_auto = _downsample_mask(self.review_state["working_mask_auto"], self.review_state["lfp_fs"], total_duration_s)
        x_confirm, y_confirm = _downsample_mask(self.review_state["working_mask_confirmed"], self.review_state["lfp_fs"], total_duration_s)
        self._plot_boolean_tracks(
            self.overview_plot,
            x_mode3,
            [
                ("Mode3", y_mode3, "#4c78a8"),
                ("Working Auto", y_auto, "#f58518"),
                ("Working Confirmed", y_confirm, self.state_colors["Working"]),
            ],
            clear=False,
        )
        self.overview_plot.setXRange(0.0, total_duration_s, padding=0)

    def _render_detail(self):
        self.detail_plot.clear()
        self.detail_plot.addItem(self.edit_region)
        region_start, region_end = self.overview_region.getRegion()
        region_start = max(0.0, float(region_start))
        region_end = min(float(self.review_state["total_duration_s"]), float(region_end))
        fs = float(self.review_state["lfp_fs"])
        s0 = int(round(region_start * fs))
        s1 = int(round(region_end * fs))
        x = np.arange(s0, s1, dtype=np.float64) / fs
        if x.size == 0:
            return
        self._plot_boolean_tracks(
            self.detail_plot,
            x,
            [
                ("Mode3", np.asarray(self.review_state["mode3_mask"][s0:s1], dtype=bool).astype(float), "#4c78a8"),
                ("Working Auto", np.asarray(self.review_state["working_mask_auto"][s0:s1], dtype=bool).astype(float), "#f58518"),
                ("Working Confirmed", np.asarray(self.review_state["working_mask_confirmed"][s0:s1], dtype=bool).astype(float), self.state_colors["Working"]),
            ],
            clear=False,
        )
        self.detail_plot.setXRange(region_start, region_end, padding=0)
        current_start, current_end = self.edit_region.getRegion()
        if current_end <= region_start or current_start >= region_end:
            self.edit_region.setRegion((region_start, min(region_end, region_start + 60.0)))
        self._update_region_status()

    def _render_info(self):
        text = (
            f"Mode3: {_build_bout_summary(self.review_state['mode3_mask'], self.review_state['lfp_fs'])}\n"
            f"Working Auto: {_build_bout_summary(self.review_state['working_mask_auto'], self.review_state['lfp_fs'])}\n"
            f"Working Confirmed: {_build_bout_summary(self.review_state['working_mask_confirmed'], self.review_state['lfp_fs'])}"
        )
        self.info_box.setPlainText(text)

    def _update_region_status(self):
        start_s, end_s = self.overview_region.getRegion()
        self.region_status.setText(
            "Overview range selected: "
            f"{float(start_s) / 3600.0:.2f}h - {float(end_s) / 3600.0:.2f}h. "
            "Drag the overview region, then click 'Render Current Selection' to refresh the detail plot."
        )

    def _render_all(self):
        self._render_overview()
        self._render_detail()
        self._render_info()


class ThresholdReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, title, score_key, threshold_key, threshold_label, hist_title, parent=None):
        super().__init__(review_state, config, title, parent=parent)
        self.score_key = score_key
        self.threshold_key = threshold_key
        self.threshold_label = threshold_label
        self.pending_threshold = float(self.review_state[self.threshold_key])
        self._threshold_syncing = False
        self.current_hist_diag = {}
        self._hist_bins_initialized = False

        plots_widget = QWidget()
        plots_layout = QVBoxLayout(plots_widget)
        self.score_plot = self._make_time_plot(hist_title.replace("Histogram", "24h Score"), threshold_label)
        self.preview_plot = self._make_mask_plot("24h Preview")
        plots_layout.addWidget(self.score_plot)
        self.hist_plot = pg.PlotWidget(title=hist_title)
        self.hist_plot.setLabel("bottom", threshold_label)
        self.hist_plot.setLabel("left", "Count")
        self.hist_plot.showGrid(x=True, y=True, alpha=0.25)
        plots_layout.addWidget(self.hist_plot)
        plots_layout.addWidget(self.preview_plot)

        controls = QGroupBox("Threshold")
        controls_layout = QFormLayout(controls)
        self.threshold_spin = QDoubleSpinBox()
        _configure_signed_threshold_spin(self.threshold_spin)
        self.threshold_spin.valueChanged.connect(self._on_spin_changed)
        controls_layout.addRow(QLabel(threshold_label), self.threshold_spin)
        self.hist_bins_spin = QSpinBox()
        self.hist_bins_spin.setRange(10, 1000)
        self.hist_bins_spin.valueChanged.connect(self._on_hist_bins_changed)
        controls_layout.addRow(QLabel("Histogram bins"), self.hist_bins_spin)
        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply Threshold")
        self.reset_pending_button = QPushButton("Reset Pending")
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.reset_pending_button)
        button_widget = QWidget()
        button_widget.setLayout(button_row)
        controls_layout.addRow(button_widget)
        self.apply_button.clicked.connect(self._apply_pending_threshold)
        self.reset_pending_button.clicked.connect(self._reset_pending_threshold)

        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(100)

        self.layout_root.addWidget(plots_widget)
        self.layout_root.addWidget(controls)
        self.layout_root.addWidget(self.info_box)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._accept_with_apply)
        self.button_box.rejected.connect(self.reject)
        self.layout_root.addWidget(self.button_box)

        self._render_all()

    def _candidate_mask(self):
        return np.asarray(self.review_state["score_valid_mask"], dtype=bool)

    def _preview_tracks(self):
        return []

    def _hist_values(self):
        return np.asarray(self.review_state[self.score_key], dtype=np.float64)[self._candidate_mask()]

    def _initial_hist_bins(self):
        _, diag = preview_bimodal_threshold(self._hist_values(), self.cfg)
        bins = int(diag.get("hist_bins", self.cfg.get("states.threshold_hist_bins", 200)))
        return max(10, bins)

    def _current_hist_bins(self):
        return int(self.hist_bins_spin.value())

    def _compute_hist_preview(self):
        threshold, diag = preview_bimodal_threshold(
            self._hist_values(),
            self.cfg,
            bins_override=self._current_hist_bins(),
        )
        self.current_hist_diag = diag if isinstance(diag, dict) else {}
        return threshold, self.current_hist_diag

    def _threshold_line_angle(self):
        return 90

    def _score_line_angle(self):
        return 0

    def _on_spin_changed(self, value):
        self._set_pending_threshold(float(value), source="spin")

    def _on_hist_bins_changed(self, _value):
        self._render_hist()
        self._render_info()

    def _set_pending_threshold(self, value, source=None):
        if self._threshold_syncing:
            return
        self.pending_threshold = float(value)
        self._threshold_syncing = True
        try:
            if source != "spin":
                self.threshold_spin.blockSignals(True)
                self.threshold_spin.setValue(self.pending_threshold)
                self.threshold_spin.blockSignals(False)
            if source != "score" and hasattr(self, "threshold_line"):
                self.threshold_line.blockSignals(True)
                self.threshold_line.setValue(self.pending_threshold)
                self.threshold_line.blockSignals(False)
            if source != "hist" and hasattr(self, "hist_threshold_line"):
                self.hist_threshold_line.blockSignals(True)
                self.hist_threshold_line.setValue(self.pending_threshold)
                self.hist_threshold_line.blockSignals(False)
        finally:
            self._threshold_syncing = False
        self._render_info()

    def _apply_pending_threshold(self):
        next_state = dict(self.review_state)
        next_state[self.threshold_key] = float(self.pending_threshold)
        self.review_state = self._run_local_task(
            f"Applying {self.threshold_label}...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self.pending_threshold = float(self.review_state[self.threshold_key])
        self._render_all()

    def _reset_pending_threshold(self):
        self._set_pending_threshold(float(self.review_state[self.threshold_key]))

    def _accept_with_apply(self):
        self._apply_pending_threshold()
        self.accept()

    def _threshold_from_score_line(self):
        self._set_pending_threshold(float(self.threshold_line.value()), source="score")

    def _render_score_plot(self):
        self.score_plot.clear()
        score = np.asarray(self.review_state[self.score_key], dtype=np.float64)
        x, y = _downsample_line(self.review_state["state_time_s"], score)
        self.score_plot.plot(x=x, y=y, pen=pg.mkPen("#1f77b4", width=1.5))
        threshold_value = float(self.pending_threshold)
        self.threshold_line = pg.InfiniteLine(pos=threshold_value, angle=self._score_line_angle(), pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.threshold_line, move_callback=self._threshold_from_score_line, finish_callback=self._threshold_from_score_line)
        self.score_plot.addItem(self.threshold_line)
        self.score_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_hist(self):
        self.hist_plot.clear()
        values = np.asarray(self._hist_values(), dtype=np.float64)
        values = values[np.isfinite(values)]
        if values.size == 0:
            self.current_hist_diag = {}
            return
        _, diag_item = self._compute_hist_preview()
        hist, edges = np.histogram(values, bins=self._current_hist_bins())
        centers = (edges[:-1] + edges[1:]) / 2.0
        bar = pg.BarGraphItem(x=centers, height=hist, width=max(np.diff(edges).min(), 1e-6), brush=pg.mkBrush(120, 120, 160, 150), pen=pg.mkPen("#666666"))
        self.hist_plot.addItem(bar)
        if isinstance(diag_item, dict):
            for peak in diag_item.get("peak_positions", []):
                self.hist_plot.addItem(pg.InfiniteLine(pos=float(peak), angle=self._threshold_line_angle(), pen=pg.mkPen("#2ca02c", width=1, style=Qt.PenStyle.DashLine)))
            trough_position = diag_item.get("trough_position", None)
            if trough_position is not None and np.isfinite(trough_position):
                self.hist_plot.addItem(
                    pg.InfiniteLine(
                        pos=float(trough_position),
                        angle=self._threshold_line_angle(),
                        pen=pg.mkPen("#ff7f0e", width=1, style=Qt.PenStyle.DotLine),
                    )
                )
        self.hist_threshold_line = pg.InfiniteLine(
            pos=float(self.pending_threshold),
            angle=self._threshold_line_angle(),
            pen=pg.mkPen("#d62728", width=2),
            movable=True,
        )
        _connect_line_interaction(self.hist_threshold_line, move_callback=self._threshold_from_hist, finish_callback=self._threshold_from_hist)
        self.hist_plot.addItem(self.hist_threshold_line)

    def _threshold_from_hist(self):
        self._set_pending_threshold(float(self.hist_threshold_line.value()), source="hist")

    def _render_preview(self):
        tracks = [("Working", self.review_state["working_epoch_mask_confirmed"].astype(float), self.state_colors["Working"])]
        tracks.extend(self._preview_tracks())
        self._plot_boolean_tracks(
            self.preview_plot,
            self.review_state["state_time_s"],
            tracks,
            clear=True,
        )
        self.preview_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_info(self):
        if not self.current_hist_diag:
            _ = self._compute_hist_preview()
        self.info_box.setPlainText(
            f"Committed threshold: {float(self.review_state[self.threshold_key]):.6g}\n"
            f"Pending threshold: {float(self.pending_threshold):.6g}\n"
            f"Histogram bins: {self._current_hist_bins()}\n"
            "Move the threshold line or spinbox, then click 'Apply Threshold' to refresh the preview.\n"
            f"Current histogram diagnostic: {self.current_hist_diag}"
        )

    def _render_all(self):
        self.pending_threshold = float(self.review_state[self.threshold_key])
        self.threshold_spin.blockSignals(True)
        self.threshold_spin.setValue(float(self.pending_threshold))
        self.threshold_spin.blockSignals(False)
        if not self._hist_bins_initialized:
            self.hist_bins_spin.blockSignals(True)
            self.hist_bins_spin.setValue(self._initial_hist_bins())
            self.hist_bins_spin.blockSignals(False)
            self._hist_bins_initialized = True
        self._render_score_plot()
        self._render_hist()
        self._render_preview()
        self._render_info()


class NREMRatioThresholdReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, parent=None):
        super().__init__(review_state, config, "Step 1 - Review NREM Thresholds", parent=parent)
        self.pending_nrem_threshold = float(self.review_state["nrem_lfhf_threshold"])
        self.pending_sleep_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_imu_smoothing_sigma_s = float(
            self.review_state.get("imu_smoothing_sigma_s", self.cfg.get("states.imu_smoothing_sigma_s", 5.0))
        )
        self._syncing = False
        self.current_imu_diag = {}
        self.current_ratio_diag = {}
        self._hist_bins_initialized = False

        self.imu_score_plot = self._make_time_plot("24h IMU Score (smoothed)", "IMU")
        self.imu_hist_plot = pg.PlotWidget(title="Sleep-like IMU Histogram")
        self.imu_hist_plot.setLabel("bottom", "IMU score")
        self.imu_hist_plot.setLabel("left", "Count")
        self.imu_hist_plot.showGrid(x=True, y=True, alpha=0.25)
        self.ratio_score_plot = self._make_time_plot("24h NREM LF/HF Score", "LF/HF score")
        self.ratio_hist_plot = pg.PlotWidget(title="NREM LF/HF Score Histogram")
        self.ratio_hist_plot.setLabel("bottom", "LF/HF score")
        self.ratio_hist_plot.setLabel("left", "Count")
        self.ratio_hist_plot.showGrid(x=True, y=True, alpha=0.25)
        self.preview_plot = self._make_mask_plot("24h NREM Preview")

        controls = QGroupBox("NREM thresholds")
        controls_layout = QFormLayout(controls)
        self.imu_smoothing_spin = QDoubleSpinBox()
        self.imu_smoothing_spin.setDecimals(2)
        self.imu_smoothing_spin.setSingleStep(0.5)
        self.imu_smoothing_spin.setRange(0.0, 60.0)
        self.imu_smoothing_spin.valueChanged.connect(self._on_imu_smoothing_changed)
        controls_layout.addRow(QLabel("Shared Gaussian smoothing (s)"), self.imu_smoothing_spin)
        self.hist_bins_spin = QSpinBox()
        self.hist_bins_spin.setRange(10, 1000)
        self.hist_bins_spin.valueChanged.connect(self._on_hist_bins_changed)
        controls_layout.addRow(QLabel("Histogram bins"), self.hist_bins_spin)
        self.imu_threshold_spin = QDoubleSpinBox()
        _configure_signed_threshold_spin(self.imu_threshold_spin)
        self.imu_threshold_spin.valueChanged.connect(self._on_imu_threshold_changed)
        controls_layout.addRow(QLabel("Sleep-like IMU threshold"), self.imu_threshold_spin)
        self.ratio_threshold_spin = QDoubleSpinBox()
        _configure_signed_threshold_spin(self.ratio_threshold_spin)
        self.ratio_threshold_spin.valueChanged.connect(self._on_ratio_threshold_changed)
        controls_layout.addRow(QLabel("LF/HF score threshold"), self.ratio_threshold_spin)
        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply NREM Thresholds")
        self.reset_pending_button = QPushButton("Reset Pending")
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.reset_pending_button)
        button_widget = QWidget()
        button_widget.setLayout(button_row)
        controls_layout.addRow(button_widget)
        self.apply_button.clicked.connect(self._apply_pending)
        self.reset_pending_button.clicked.connect(self._reset_pending)

        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(120)

        self.layout_root.addWidget(self.imu_score_plot)
        self.layout_root.addWidget(self.imu_hist_plot)
        self.layout_root.addWidget(self.ratio_score_plot)
        self.layout_root.addWidget(self.ratio_hist_plot)
        self.layout_root.addWidget(self.preview_plot)
        self.layout_root.addWidget(controls)
        self.layout_root.addWidget(self.info_box)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._accept_with_apply)
        self.button_box.rejected.connect(self.reject)
        self.layout_root.addWidget(self.button_box)
        self._render_all()

    def _non_working_mask(self):
        return np.asarray(self.review_state["score_valid_mask"], dtype=bool) & (~np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool))

    def _pending_smoothed_imu(self):
        sigma_epochs = max(0.0, float(self.pending_imu_smoothing_sigma_s) / max(float(self.review_state["scoring_step_s"]), 1e-12))
        return _gaussian_smooth_nan(np.asarray(self.review_state["imu_score"], dtype=np.float64), sigma_epochs)

    def _low_imu_mask(self):
        imu_smoothed = self._pending_smoothed_imu()
        return self._non_working_mask() & np.isfinite(imu_smoothed) & (imu_smoothed <= float(self.pending_sleep_imu_threshold))

    def _pending_lfhf_score(self):
        return _compute_lfhf_score_from_band_powers(
            np.asarray(self.review_state["lf_band_power"], dtype=np.float64),
            np.asarray(self.review_state["hf_band_power"], dtype=np.float64),
            self._low_imu_mask(),
        )

    def _current_hist_bins(self):
        return int(self.hist_bins_spin.value())

    def _initial_hist_bins(self):
        _, imu_diag = preview_bimodal_threshold(self._pending_smoothed_imu()[self._non_working_mask()], self.cfg)
        _, ratio_diag = preview_bimodal_threshold(self._pending_lfhf_score()[self._low_imu_mask()], self.cfg)
        return max(10, int(max(imu_diag.get("hist_bins", 100), ratio_diag.get("hist_bins", 100))))

    def _set_pending_controls(self):
        self._syncing = True
        try:
            self.imu_threshold_spin.blockSignals(True)
            self.imu_threshold_spin.setValue(float(self.pending_sleep_imu_threshold))
            self.imu_threshold_spin.blockSignals(False)
            self.ratio_threshold_spin.blockSignals(True)
            self.ratio_threshold_spin.setValue(float(self.pending_nrem_threshold))
            self.ratio_threshold_spin.blockSignals(False)
            self.imu_smoothing_spin.blockSignals(True)
            self.imu_smoothing_spin.setValue(float(self.pending_imu_smoothing_sigma_s))
            self.imu_smoothing_spin.blockSignals(False)
            if hasattr(self, "imu_threshold_line"):
                self.imu_threshold_line.blockSignals(True)
                self.imu_threshold_line.setValue(float(self.pending_sleep_imu_threshold))
                self.imu_threshold_line.blockSignals(False)
            if hasattr(self, "imu_hist_threshold_line"):
                self.imu_hist_threshold_line.blockSignals(True)
                self.imu_hist_threshold_line.setValue(float(self.pending_sleep_imu_threshold))
                self.imu_hist_threshold_line.blockSignals(False)
            if hasattr(self, "ratio_threshold_line"):
                self.ratio_threshold_line.blockSignals(True)
                self.ratio_threshold_line.setValue(float(self.pending_nrem_threshold))
                self.ratio_threshold_line.blockSignals(False)
            if hasattr(self, "ratio_hist_threshold_line"):
                self.ratio_hist_threshold_line.blockSignals(True)
                self.ratio_hist_threshold_line.setValue(float(self.pending_nrem_threshold))
                self.ratio_hist_threshold_line.blockSignals(False)
        finally:
            self._syncing = False

    def _on_hist_bins_changed(self, _value):
        self._render_histograms()
        self._render_info()

    def _on_imu_smoothing_changed(self, value):
        if self._syncing:
            return
        self.pending_imu_smoothing_sigma_s = float(value)
        self._render_imu_score_plot()
        self._render_histograms()
        self._render_info()

    def _on_imu_threshold_changed(self, value):
        if self._syncing:
            return
        self.pending_sleep_imu_threshold = float(value)
        self._set_pending_controls()
        self._render_histograms()
        self._render_info()

    def _on_ratio_threshold_changed(self, value):
        if self._syncing:
            return
        self.pending_nrem_threshold = float(value)
        self._set_pending_controls()
        self._render_info()

    def _threshold_from_imu_line(self):
        self.pending_sleep_imu_threshold = float(self.imu_threshold_line.value())
        self._set_pending_controls()
        self._render_histograms()
        self._render_info()

    def _threshold_from_imu_hist_line(self):
        self.pending_sleep_imu_threshold = float(self.imu_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_histograms()
        self._render_info()

    def _sync_from_imu_line(self):
        self.pending_sleep_imu_threshold = float(self.imu_threshold_line.value())
        self._set_pending_controls()
        self._render_info()

    def _sync_from_imu_hist_line(self):
        self.pending_sleep_imu_threshold = float(self.imu_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_info()

    def _threshold_from_ratio_line(self):
        self.pending_nrem_threshold = float(self.ratio_threshold_line.value())
        self._set_pending_controls()
        self._render_info()

    def _threshold_from_ratio_hist_line(self):
        self.pending_nrem_threshold = float(self.ratio_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_info()

    def _apply_pending(self):
        next_state = dict(self.review_state)
        next_state["imu_smoothing_sigma_s"] = float(self.pending_imu_smoothing_sigma_s)
        next_state["sleep_imu_threshold"] = float(self.pending_sleep_imu_threshold)
        next_state["nrem_lfhf_threshold"] = float(self.pending_nrem_threshold)
        self.review_state = self._run_local_task(
            "Applying NREM thresholds...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self.pending_sleep_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_nrem_threshold = float(self.review_state["nrem_lfhf_threshold"])
        self.pending_imu_smoothing_sigma_s = float(self.review_state["imu_smoothing_sigma_s"])
        self._render_all()

    def _reset_pending(self):
        self.pending_sleep_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_nrem_threshold = float(self.review_state["nrem_lfhf_threshold"])
        self.pending_imu_smoothing_sigma_s = float(self.review_state["imu_smoothing_sigma_s"])
        self._render_all()

    def _accept_with_apply(self):
        self._apply_pending()
        self.accept()

    def _render_imu_score_plot(self):
        self.imu_score_plot.clear()
        imu_smoothed = self._pending_smoothed_imu()
        x, y = _downsample_line(self.review_state["state_time_s"], imu_smoothed)
        self.imu_score_plot.plot(x=x, y=y, pen=pg.mkPen("#ff7f0e", width=1.5))
        self.imu_threshold_line = pg.InfiniteLine(pos=float(self.pending_sleep_imu_threshold), angle=0, pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.imu_threshold_line, move_callback=self._sync_from_imu_line, finish_callback=self._threshold_from_imu_line)
        self.imu_score_plot.addItem(self.imu_threshold_line)
        self.imu_score_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_ratio_score_plot(self):
        self.ratio_score_plot.clear()
        ratio = self._pending_lfhf_score()
        x, y = _downsample_line(self.review_state["state_time_s"], ratio)
        self.ratio_score_plot.plot(x=x, y=y, pen=pg.mkPen("#8e44ad", width=1.5))
        self.ratio_threshold_line = pg.InfiniteLine(pos=float(self.pending_nrem_threshold), angle=0, pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.ratio_threshold_line, move_callback=self._threshold_from_ratio_line, finish_callback=self._threshold_from_ratio_line)
        self.ratio_score_plot.addItem(self.ratio_threshold_line)
        self.ratio_score_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_histograms(self):
        self.imu_hist_plot.clear()
        self.ratio_hist_plot.clear()
        imu_values = self._pending_smoothed_imu()[self._non_working_mask()]
        imu_values = imu_values[np.isfinite(imu_values)]
        ratio_values = self._pending_lfhf_score()[self._low_imu_mask()]
        ratio_values = ratio_values[np.isfinite(ratio_values)]

        if imu_values.size:
            _, self.current_imu_diag = preview_bimodal_threshold(imu_values, self.cfg, bins_override=self._current_hist_bins())
            hist, edges = np.histogram(imu_values, bins=self._current_hist_bins())
            centers = (edges[:-1] + edges[1:]) / 2.0
            self.imu_hist_plot.addItem(pg.BarGraphItem(x=centers, height=hist, width=max(np.diff(edges).min(), 1e-6), brush=pg.mkBrush(120, 120, 160, 150), pen=pg.mkPen("#666666")))
            for peak in self.current_imu_diag.get("peak_positions", []):
                self.imu_hist_plot.addItem(pg.InfiniteLine(pos=float(peak), angle=90, pen=pg.mkPen("#2ca02c", width=1, style=Qt.PenStyle.DashLine)))
            self.imu_hist_threshold_line = pg.InfiniteLine(pos=float(self.pending_sleep_imu_threshold), angle=90, pen=pg.mkPen("#d62728", width=2), movable=True)
            _connect_line_interaction(self.imu_hist_threshold_line, move_callback=self._sync_from_imu_hist_line, finish_callback=self._threshold_from_imu_hist_line)
            self.imu_hist_plot.addItem(self.imu_hist_threshold_line)
        else:
            self.current_imu_diag = {}

        if ratio_values.size:
            _, self.current_ratio_diag = preview_bimodal_threshold(ratio_values, self.cfg, bins_override=self._current_hist_bins())
            hist, edges = np.histogram(ratio_values, bins=self._current_hist_bins())
            centers = (edges[:-1] + edges[1:]) / 2.0
            self.ratio_hist_plot.addItem(pg.BarGraphItem(x=centers, height=hist, width=max(np.diff(edges).min(), 1e-6), brush=pg.mkBrush(120, 120, 160, 150), pen=pg.mkPen("#666666")))
            for peak in self.current_ratio_diag.get("peak_positions", []):
                self.ratio_hist_plot.addItem(pg.InfiniteLine(pos=float(peak), angle=90, pen=pg.mkPen("#2ca02c", width=1, style=Qt.PenStyle.DashLine)))
            self.ratio_hist_threshold_line = pg.InfiniteLine(pos=float(self.pending_nrem_threshold), angle=90, pen=pg.mkPen("#d62728", width=2), movable=True)
            _connect_line_interaction(self.ratio_hist_threshold_line, move_callback=self._threshold_from_ratio_hist_line, finish_callback=self._threshold_from_ratio_hist_line)
            self.ratio_hist_plot.addItem(self.ratio_hist_threshold_line)
        else:
            self.current_ratio_diag = {}

    def _render_preview(self):
        self._plot_boolean_tracks(
            self.preview_plot,
            self.review_state["state_time_s"],
            [
                ("Working", np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Working"]),
                ("Low IMU", np.asarray(self.review_state["low_imu_mask_confirmed"], dtype=bool).astype(float), "#ff7f0e"),
                ("NREM", np.asarray(self.review_state["nrem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["NREM"]),
                ("REM", np.asarray(self.review_state["rem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["REM"]),
                ("Wake Candidate", np.asarray(self.review_state["wake_candidate_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Wake"]),
            ],
            clear=True,
        )
        self.preview_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_info(self):
        self.info_box.setPlainText(
            f"Pending shared smoothing: {float(self.pending_imu_smoothing_sigma_s):.2f}s\n"
            f"Pending sleep-like IMU threshold: {float(self.pending_sleep_imu_threshold):.6g}\n"
            f"Pending LF/HF score threshold: {float(self.pending_nrem_threshold):.6g}\n"
            f"Histogram bins: {self._current_hist_bins()}\n"
            f"IMU histogram diagnostic: {self.current_imu_diag}\n"
            f"LF/HF histogram diagnostic: {self.current_ratio_diag}"
        )

    def _render_all(self):
        self._set_pending_controls()
        if not self._hist_bins_initialized:
            self.hist_bins_spin.blockSignals(True)
            self.hist_bins_spin.setValue(self._initial_hist_bins())
            self.hist_bins_spin.blockSignals(False)
            self._hist_bins_initialized = True
        self._render_imu_score_plot()
        self._render_ratio_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()


class REMThresholdReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, parent=None):
        super().__init__(review_state, config, "Step 2 - Review REM Thresholds", parent=parent)
        self.pending_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_highfreq_threshold = float(self.review_state["rem_highfreq_threshold"])
        self.pending_imu_smoothing_sigma_s = float(self.review_state.get("imu_smoothing_sigma_s", self.cfg.get("states.imu_smoothing_sigma_s", 5.0)))
        self._syncing = False
        self.current_imu_diag = {}
        self.current_highfreq_diag = {}
        self._hist_bins_initialized = False

        self.imu_score_plot = self._make_time_plot("24h IMU Score (smoothed)", "IMU")
        self.imu_hist_plot = pg.PlotWidget(title="REM IMU Histogram")
        self.imu_hist_plot.setLabel("bottom", "IMU score")
        self.imu_hist_plot.setLabel("left", "Count")
        self.imu_hist_plot.showGrid(x=True, y=True, alpha=0.25)
        self.highfreq_score_plot = self._make_time_plot(
            "24h 80-250 Hz Score (low-IMU non-NREM pool)",
            "80-250 Hz score",
        )
        self.highfreq_hist_plot = pg.PlotWidget(title="REM 80-250 Hz Score Histogram (low-IMU non-NREM pool)")
        self.highfreq_hist_plot.setLabel("bottom", "80-250 Hz score")
        self.highfreq_hist_plot.setLabel("left", "Count")
        self.highfreq_hist_plot.showGrid(x=True, y=True, alpha=0.25)
        self.preview_plot = self._make_mask_plot("24h REM Preview")

        controls = QGroupBox("REM thresholds")
        controls_layout = QFormLayout(controls)
        self.imu_smoothing_spin = QDoubleSpinBox()
        self.imu_smoothing_spin.setDecimals(2)
        self.imu_smoothing_spin.setSingleStep(0.5)
        self.imu_smoothing_spin.setRange(0.0, 60.0)
        self.imu_smoothing_spin.valueChanged.connect(self._on_imu_smoothing_changed)
        controls_layout.addRow(QLabel("Shared Gaussian smoothing (s)"), self.imu_smoothing_spin)
        self.hist_bins_spin = QSpinBox()
        self.hist_bins_spin.setRange(10, 1000)
        self.hist_bins_spin.valueChanged.connect(self._on_hist_bins_changed)
        controls_layout.addRow(QLabel("Histogram bins"), self.hist_bins_spin)
        self.imu_threshold_spin = QDoubleSpinBox()
        _configure_signed_threshold_spin(self.imu_threshold_spin)
        self.imu_threshold_spin.valueChanged.connect(self._on_imu_threshold_changed)
        controls_layout.addRow(QLabel("Shared sleep-like IMU threshold"), self.imu_threshold_spin)
        self.highfreq_threshold_spin = QDoubleSpinBox()
        _configure_signed_threshold_spin(self.highfreq_threshold_spin)
        self.highfreq_threshold_spin.valueChanged.connect(self._on_highfreq_threshold_changed)
        controls_layout.addRow(QLabel("80-250 Hz score threshold"), self.highfreq_threshold_spin)
        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply REM Thresholds")
        self.reset_pending_button = QPushButton("Reset Pending")
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.reset_pending_button)
        button_widget = QWidget()
        button_widget.setLayout(button_row)
        controls_layout.addRow(button_widget)
        self.apply_button.clicked.connect(self._apply_pending)
        self.reset_pending_button.clicked.connect(self._reset_pending)

        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(120)

        self.layout_root.addWidget(self.imu_score_plot)
        self.layout_root.addWidget(self.imu_hist_plot)
        self.layout_root.addWidget(self.highfreq_score_plot)
        self.layout_root.addWidget(self.highfreq_hist_plot)
        self.layout_root.addWidget(self.preview_plot)
        self.layout_root.addWidget(controls)
        self.layout_root.addWidget(self.info_box)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._accept_with_apply)
        self.button_box.rejected.connect(self.reject)
        self.layout_root.addWidget(self.button_box)
        self._render_all()

    def _candidate_mask(self):
        base = self._rem_base_mask()
        imu_smoothed = self._pending_smoothed_imu()
        low_imu = base & np.isfinite(imu_smoothed) & (imu_smoothed <= float(self.pending_imu_threshold))
        return low_imu & (~np.asarray(self.review_state["nrem_mask_confirmed"], dtype=bool))

    def _highfreq_review_mask(self):
        score = np.asarray(self.review_state["rem_highfreq_power"], dtype=np.float64)
        return self._candidate_mask() & np.isfinite(score)

    def _rem_base_mask(self):
        return (
            np.asarray(self.review_state["score_valid_mask"], dtype=bool)
            & (~np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool))
            & (~np.asarray(self.review_state["nrem_mask_confirmed"], dtype=bool))
        )

    def _current_hist_bins(self):
        return int(self.hist_bins_spin.value())

    def _initial_hist_bins(self):
        _, imu_diag = preview_bimodal_threshold(self._pending_smoothed_imu()[self._rem_base_mask()], self.cfg)
        _, hf_diag = preview_bimodal_threshold(
            np.asarray(self.review_state["rem_highfreq_power"], dtype=np.float64)[self._highfreq_review_mask()],
            self.cfg,
        )
        return max(10, int(max(imu_diag.get("hist_bins", 100), hf_diag.get("hist_bins", 100))))

    def _pending_smoothed_imu(self):
        sigma_epochs = max(0.0, float(self.pending_imu_smoothing_sigma_s) / max(float(self.review_state["scoring_step_s"]), 1e-12))
        return _gaussian_smooth_nan(np.asarray(self.review_state["imu_score"], dtype=np.float64), sigma_epochs)

    def _set_pending_controls(self):
        self._syncing = True
        try:
            self.imu_threshold_spin.blockSignals(True)
            self.imu_threshold_spin.setValue(float(self.pending_imu_threshold))
            self.imu_threshold_spin.blockSignals(False)
            self.highfreq_threshold_spin.blockSignals(True)
            self.highfreq_threshold_spin.setValue(float(self.pending_highfreq_threshold))
            self.highfreq_threshold_spin.blockSignals(False)
            self.imu_smoothing_spin.blockSignals(True)
            self.imu_smoothing_spin.setValue(float(self.pending_imu_smoothing_sigma_s))
            self.imu_smoothing_spin.blockSignals(False)
            if hasattr(self, "imu_threshold_line"):
                self.imu_threshold_line.blockSignals(True)
                self.imu_threshold_line.setValue(float(self.pending_imu_threshold))
                self.imu_threshold_line.blockSignals(False)
            if hasattr(self, "imu_hist_threshold_line"):
                self.imu_hist_threshold_line.blockSignals(True)
                self.imu_hist_threshold_line.setValue(float(self.pending_imu_threshold))
                self.imu_hist_threshold_line.blockSignals(False)
            if hasattr(self, "highfreq_threshold_line"):
                self.highfreq_threshold_line.blockSignals(True)
                self.highfreq_threshold_line.setValue(float(self.pending_highfreq_threshold))
                self.highfreq_threshold_line.blockSignals(False)
            if hasattr(self, "highfreq_hist_threshold_line"):
                self.highfreq_hist_threshold_line.blockSignals(True)
                self.highfreq_hist_threshold_line.setValue(float(self.pending_highfreq_threshold))
                self.highfreq_hist_threshold_line.blockSignals(False)
        finally:
            self._syncing = False

    def _on_hist_bins_changed(self, _value):
        self._render_histograms()
        self._render_info()

    def _on_imu_smoothing_changed(self, value):
        if self._syncing:
            return
        self.pending_imu_smoothing_sigma_s = float(value)
        self._render_imu_score_plot()
        self._render_highfreq_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()

    def _on_imu_threshold_changed(self, value):
        if self._syncing:
            return
        self.pending_imu_threshold = float(value)
        self._set_pending_controls()
        self._render_highfreq_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()

    def _on_highfreq_threshold_changed(self, value):
        if self._syncing:
            return
        self.pending_highfreq_threshold = float(value)
        self._set_pending_controls()
        self._render_preview()
        self._render_info()

    def _threshold_from_imu_line(self):
        self.pending_imu_threshold = float(self.imu_threshold_line.value())
        self._set_pending_controls()
        self._render_highfreq_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()

    def _threshold_from_imu_hist_line(self):
        self.pending_imu_threshold = float(self.imu_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_highfreq_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()

    def _sync_from_imu_line(self):
        self.pending_imu_threshold = float(self.imu_threshold_line.value())
        self._set_pending_controls()
        self._render_highfreq_score_plot()
        self._render_preview()
        self._render_info()

    def _sync_from_imu_hist_line(self):
        self.pending_imu_threshold = float(self.imu_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_highfreq_score_plot()
        self._render_preview()
        self._render_info()

    def _threshold_from_highfreq_line(self):
        self.pending_highfreq_threshold = float(self.highfreq_threshold_line.value())
        self._set_pending_controls()
        self._render_preview()
        self._render_info()

    def _threshold_from_highfreq_hist_line(self):
        self.pending_highfreq_threshold = float(self.highfreq_hist_threshold_line.value())
        self._set_pending_controls()
        self._render_preview()
        self._render_info()

    def _apply_pending(self):
        next_state = dict(self.review_state)
        next_state["imu_smoothing_sigma_s"] = float(self.pending_imu_smoothing_sigma_s)
        next_state["sleep_imu_threshold"] = float(self.pending_imu_threshold)
        next_state["rem_highfreq_threshold"] = float(self.pending_highfreq_threshold)
        self.review_state = self._run_local_task(
            "Applying REM thresholds...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self.pending_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_highfreq_threshold = float(self.review_state["rem_highfreq_threshold"])
        self.pending_imu_smoothing_sigma_s = float(self.review_state["imu_smoothing_sigma_s"])
        self._render_all()

    def _reset_pending(self):
        self.pending_imu_threshold = float(self.review_state["sleep_imu_threshold"])
        self.pending_highfreq_threshold = float(self.review_state["rem_highfreq_threshold"])
        self.pending_imu_smoothing_sigma_s = float(self.review_state["imu_smoothing_sigma_s"])
        self._render_all()

    def _accept_with_apply(self):
        self._apply_pending()
        self.accept()

    def _render_imu_score_plot(self):
        self.imu_score_plot.clear()
        imu_smoothed = self._pending_smoothed_imu()
        x, y = _downsample_line(self.review_state["state_time_s"], imu_smoothed)
        self.imu_score_plot.plot(x=x, y=y, pen=pg.mkPen("#ff7f0e", width=1.5))
        self.imu_threshold_line = pg.InfiniteLine(pos=float(self.pending_imu_threshold), angle=0, pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.imu_threshold_line, move_callback=self._sync_from_imu_line, finish_callback=self._threshold_from_imu_line)
        self.imu_score_plot.addItem(self.imu_threshold_line)
        self.imu_score_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_highfreq_score_plot(self):
        self.highfreq_score_plot.clear()
        score = np.asarray(self.review_state["rem_highfreq_power"], dtype=np.float64)
        candidate_mask = self._highfreq_review_mask()
        visible_score = score.copy()
        visible_score[~candidate_mask] = np.nan
        x, y = _downsample_line(self.review_state["state_time_s"], visible_score)
        self.highfreq_score_plot.plot(x=x, y=y, pen=pg.mkPen("#8c564b", width=1.5))
        self.highfreq_threshold_line = pg.InfiniteLine(pos=float(self.pending_highfreq_threshold), angle=0, pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.highfreq_threshold_line, move_callback=self._threshold_from_highfreq_line, finish_callback=self._threshold_from_highfreq_line)
        self.highfreq_score_plot.addItem(self.highfreq_threshold_line)
        self.highfreq_score_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)
        n_candidate = int(np.sum(candidate_mask))
        n_base = int(np.sum(self._rem_base_mask()))
        self.highfreq_score_plot.setTitle(
            "24h 80-250 Hz Score (shown only for low-IMU non-NREM pool; "
            f"{n_candidate}/{n_base} epochs)"
        )

    def _render_histograms(self):
        self.imu_hist_plot.clear()
        self.highfreq_hist_plot.clear()
        candidate_mask = self._highfreq_review_mask()
        imu_values = self._pending_smoothed_imu()[self._rem_base_mask()]
        imu_values = imu_values[np.isfinite(imu_values)]
        highfreq_values = np.asarray(self.review_state["rem_highfreq_power"], dtype=np.float64)[candidate_mask]

        if imu_values.size:
            _, self.current_imu_diag = preview_bimodal_threshold(imu_values, self.cfg, bins_override=self._current_hist_bins())
            hist, edges = np.histogram(imu_values, bins=self._current_hist_bins())
            centers = (edges[:-1] + edges[1:]) / 2.0
            self.imu_hist_plot.addItem(pg.BarGraphItem(x=centers, height=hist, width=max(np.diff(edges).min(), 1e-6), brush=pg.mkBrush(120, 120, 160, 150), pen=pg.mkPen("#666666")))
            for peak in self.current_imu_diag.get("peak_positions", []):
                self.imu_hist_plot.addItem(pg.InfiniteLine(pos=float(peak), angle=90, pen=pg.mkPen("#2ca02c", width=1, style=Qt.PenStyle.DashLine)))
            self.imu_hist_threshold_line = pg.InfiniteLine(pos=float(self.pending_imu_threshold), angle=90, pen=pg.mkPen("#d62728", width=2), movable=True)
            _connect_line_interaction(self.imu_hist_threshold_line, move_callback=self._sync_from_imu_hist_line, finish_callback=self._threshold_from_imu_hist_line)
            self.imu_hist_plot.addItem(self.imu_hist_threshold_line)
        else:
            self.current_imu_diag = {}

        if highfreq_values.size:
            _, self.current_highfreq_diag = preview_bimodal_threshold(highfreq_values, self.cfg, bins_override=self._current_hist_bins())
            hist, edges = np.histogram(highfreq_values, bins=self._current_hist_bins())
            centers = (edges[:-1] + edges[1:]) / 2.0
            self.highfreq_hist_plot.addItem(pg.BarGraphItem(x=centers, height=hist, width=max(np.diff(edges).min(), 1e-6), brush=pg.mkBrush(120, 120, 160, 150), pen=pg.mkPen("#666666")))
            for peak in self.current_highfreq_diag.get("peak_positions", []):
                self.highfreq_hist_plot.addItem(pg.InfiniteLine(pos=float(peak), angle=90, pen=pg.mkPen("#2ca02c", width=1, style=Qt.PenStyle.DashLine)))
            self.highfreq_hist_threshold_line = pg.InfiniteLine(pos=float(self.pending_highfreq_threshold), angle=90, pen=pg.mkPen("#d62728", width=2), movable=True)
            _connect_line_interaction(self.highfreq_hist_threshold_line, move_callback=self._threshold_from_highfreq_hist_line, finish_callback=self._threshold_from_highfreq_hist_line)
            self.highfreq_hist_plot.addItem(self.highfreq_hist_threshold_line)
        else:
            self.current_highfreq_diag = {}

    def _render_preview(self):
        highfreq_candidate = self._highfreq_review_mask()
        highfreq_low = (
            highfreq_candidate
            & (np.asarray(self.review_state["rem_highfreq_power"], dtype=np.float64) <= float(self.pending_highfreq_threshold))
        )
        self._plot_boolean_tracks(
            self.preview_plot,
            self.review_state["state_time_s"],
            [
                ("Working", np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Working"]),
                ("Low IMU", np.asarray(self.review_state["low_imu_mask_confirmed"], dtype=bool).astype(float), "#ff7f0e"),
                ("NREM", np.asarray(self.review_state["nrem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["NREM"]),
                ("REM", np.asarray(self.review_state["rem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["REM"]),
                ("HF low candidate", highfreq_low.astype(float), "#8c564b"),
                ("Wake Candidate", np.asarray(self.review_state["wake_candidate_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Wake"]),
            ],
            clear=True,
        )
        self.preview_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_info(self):
        n_base = int(np.sum(self._rem_base_mask()))
        n_highfreq = int(np.sum(self._highfreq_review_mask()))
        self.info_box.setPlainText(
            f"Pending shared smoothing: {float(self.pending_imu_smoothing_sigma_s):.2f}s\n"
            f"Pending shared sleep-like IMU threshold: {float(self.pending_imu_threshold):.6g}\n"
            f"Pending 80-250 Hz score threshold: {float(self.pending_highfreq_threshold):.6g}\n"
            f"80-250 Hz review pool: {n_highfreq}/{n_base} epochs after IMU threshold and NREM exclusion\n"
            f"Histogram bins: {self._current_hist_bins()}\n"
            f"IMU histogram diagnostic: {self.current_imu_diag}\n"
            f"80-250 Hz histogram diagnostic: {self.current_highfreq_diag}"
        )

    def _render_all(self):
        self._set_pending_controls()
        if not self._hist_bins_initialized:
            self.hist_bins_spin.blockSignals(True)
            self.hist_bins_spin.setValue(self._initial_hist_bins())
            self.hist_bins_spin.blockSignals(False)
            self._hist_bins_initialized = True
        self._render_imu_score_plot()
        self._render_highfreq_score_plot()
        self._render_histograms()
        self._render_preview()
        self._render_info()


class WakeMiniWakeReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, parent=None):
        super().__init__(review_state, config, "Step 3 - Review Wake / MiniWake Split", parent=parent)
        self.duration_plot = self._make_time_plot("Wake Bout Durations", "Duration (s)")
        self.preview_plot = self._make_mask_plot("24h Wake / MiniWake Preview")
        self.pending_wake_min_duration_s = float(self.review_state["wake_min_duration_s"])
        self._duration_syncing = False
        self.threshold_spin = QSpinBox()
        self.threshold_spin.setRange(1, 24 * 3600)
        self.threshold_spin.setValue(int(round(self.pending_wake_min_duration_s)))
        self.threshold_spin.valueChanged.connect(self._on_threshold_changed)
        controls = QGroupBox("Wake threshold")
        controls_layout = QFormLayout(controls)
        controls_layout.addRow(QLabel("Wake minimum duration (s)"), self.threshold_spin)
        button_row = QHBoxLayout()
        self.apply_button = QPushButton("Apply Wake Threshold")
        self.reset_pending_button = QPushButton("Reset Pending")
        button_row.addWidget(self.apply_button)
        button_row.addWidget(self.reset_pending_button)
        button_widget = QWidget()
        button_widget.setLayout(button_row)
        controls_layout.addRow(button_widget)
        self.apply_button.clicked.connect(self._apply_pending_threshold)
        self.reset_pending_button.clicked.connect(self._reset_pending_threshold)
        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(90)
        self.layout_root.addWidget(self.duration_plot)
        self.layout_root.addWidget(self.preview_plot)
        self.layout_root.addWidget(controls)
        self.layout_root.addWidget(self.info_box)
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.accepted.connect(self._accept_with_apply)
        self.button_box.rejected.connect(self.reject)
        self.layout_root.addWidget(self.button_box)
        self._render_all()

    def _wake_bouts(self):
        mask = np.asarray(self.review_state["wake_candidate_mask_confirmed"], dtype=bool)
        step_s = float(self.review_state["scoring_step_s"])
        bouts = []
        padded = np.concatenate([[False], mask, [False]])
        edges = np.diff(padded.astype(np.int8))
        starts = np.flatnonzero(edges == 1)
        ends = np.flatnonzero(edges == -1)
        for start, end in zip(starts, ends):
            center = ((start + end) / 2.0) * step_s
            duration = (end - start) * step_s
            bouts.append((center, duration))
        return bouts

    def _on_threshold_changed(self, value):
        self._set_pending_duration(float(value), source="spin")

    def _set_pending_duration(self, value, source=None):
        if self._duration_syncing:
            return
        self.pending_wake_min_duration_s = max(1.0, float(value))
        self._duration_syncing = True
        try:
            if source != "spin":
                self.threshold_spin.blockSignals(True)
                self.threshold_spin.setValue(int(round(self.pending_wake_min_duration_s)))
                self.threshold_spin.blockSignals(False)
            if source != "line" and hasattr(self, "threshold_line"):
                self.threshold_line.blockSignals(True)
                self.threshold_line.setValue(self.pending_wake_min_duration_s)
                self.threshold_line.blockSignals(False)
        finally:
            self._duration_syncing = False
        self._render_info()

    def _apply_pending_threshold(self):
        next_state = dict(self.review_state)
        next_state["wake_min_duration_s"] = float(self.pending_wake_min_duration_s)
        self.review_state = self._run_local_task(
            "Applying Wake / MiniWake split...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self.pending_wake_min_duration_s = float(self.review_state["wake_min_duration_s"])
        self._render_all()

    def _reset_pending_threshold(self):
        self._set_pending_duration(float(self.review_state["wake_min_duration_s"]))

    def _accept_with_apply(self):
        self._apply_pending_threshold()
        self.accept()

    def _render_duration_plot(self):
        self.duration_plot.clear()
        bouts = self._wake_bouts()
        if bouts:
            x = np.asarray([item[0] for item in bouts], dtype=np.float64)
            y = np.asarray([item[1] for item in bouts], dtype=np.float64)
            scatter = pg.ScatterPlotItem(x=x, y=y, size=8, brush=pg.mkBrush(self.state_colors["Wake"]))
            self.duration_plot.addItem(scatter)
        self.threshold_line = pg.InfiniteLine(pos=float(self.pending_wake_min_duration_s), angle=0, pen=pg.mkPen("#d62728", width=2), movable=True)
        _connect_line_interaction(self.threshold_line, move_callback=self._threshold_from_line, finish_callback=self._threshold_from_line)
        self.duration_plot.addItem(self.threshold_line)
        self.duration_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _threshold_from_line(self):
        self._set_pending_duration(float(self.threshold_line.value()), source="line")

    def _render_preview(self):
        self._plot_boolean_tracks(
            self.preview_plot,
            self.review_state["state_time_s"],
            [
                ("Working", np.asarray(self.review_state["working_epoch_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Working"]),
                ("NREM", np.asarray(self.review_state["nrem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["NREM"]),
                ("REM", np.asarray(self.review_state["rem_mask_confirmed"], dtype=bool).astype(float), self.state_colors["REM"]),
                ("Wake", np.asarray(self.review_state["wake_mask_confirmed"], dtype=bool).astype(float), self.state_colors["Wake"]),
                ("MiniWake", np.asarray(self.review_state["miniwake_mask_confirmed"], dtype=bool).astype(float), self.state_colors["MiniWake"]),
            ],
            clear=True,
        )
        self.preview_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_info(self):
        self.info_box.setPlainText(
            f"Wake candidate bouts: {_build_bout_summary(self.review_state['wake_candidate_mask_confirmed'], 1.0 / self.review_state['scoring_step_s'])}\n"
            f"Committed wake threshold: {float(self.review_state['wake_min_duration_s']):.1f}s\n"
            f"Pending wake threshold: {float(self.pending_wake_min_duration_s):.1f}s\n"
            "Adjust the threshold, then click 'Apply Wake Threshold' to refresh the Wake / MiniWake preview."
        )

    def _render_all(self):
        self.pending_wake_min_duration_s = float(self.review_state["wake_min_duration_s"])
        self.threshold_spin.blockSignals(True)
        self.threshold_spin.setValue(int(round(self.pending_wake_min_duration_s)))
        self.threshold_spin.blockSignals(False)
        self._render_duration_plot()
        self._render_preview()
        self._render_info()


class FinalHourlyStateReviewDialog(BaseReviewDialog):
    def __init__(self, review_state, config, parent=None):
        super().__init__(review_state, config, "Step 4 - Final 1h Manual Review", parent=parent)
        self.buffer_s = float(self.cfg.get("states.final_review_hour_buffer_s", 300.0))
        self.n_hours = int(math.ceil(float(self.review_state["total_duration_s"]) / 3600.0))
        self.current_hour = 0

        self.hour_label = QLabel()
        self.hour_label.setStyleSheet("font-weight: bold;")
        self.day_plot = self._make_mask_plot("24h Final Label Preview")
        self.hour_plot = self._make_mask_plot("Current Hour Detail")
        self.lfhf_plot = self._make_time_plot("LF/HF Score", "LF/HF score")
        self.imu_plot = self._make_time_plot("IMU Score (smoothed)", "IMU")
        self.highfreq_plot = self._make_time_plot("80-250 Hz Score", "80-250 Hz score")
        self.spectrogram_plot = self._make_time_plot("Representative Channel Spectrogram (<=250 Hz)", "log2(Hz)")
        self.edit_region = _make_vertical_region((0.0, 60.0), movable=True, brush=pg.mkBrush(50, 150, 255, 30), pen=pg.mkPen("#1f77b4", width=2))
        self.hour_plot.addItem(self.edit_region)

        feature_grid_widget = QWidget()
        feature_grid = QGridLayout(feature_grid_widget)
        feature_grid.setContentsMargins(0, 0, 0, 0)
        feature_grid.setSpacing(8)
        feature_grid.addWidget(self.hour_plot, 0, 0, 1, 2)
        feature_grid.addWidget(self.lfhf_plot, 1, 0)
        feature_grid.addWidget(self.spectrogram_plot, 1, 1)
        feature_grid.addWidget(self.imu_plot, 2, 0)
        feature_grid.addWidget(self.highfreq_plot, 2, 1)

        buttons_group = QGroupBox("Assign Selected Region")
        buttons_layout = QGridLayout(buttons_group)
        for idx, state_name in enumerate(OCCUPANCY_STATE_NAMES):
            button = QPushButton(state_name)
            button.clicked.connect(lambda _, name=state_name: self._assign_state(name))
            buttons_layout.addWidget(button, 0, idx)
        clear_button = QPushButton("Clear Override")
        clear_button.clicked.connect(self._clear_override)
        buttons_layout.addWidget(clear_button, 1, 0, 1, 2)
        reset_button = QPushButton("Reset All Overrides")
        reset_button.clicked.connect(self._reset_overrides)
        buttons_layout.addWidget(reset_button, 1, 2, 1, 2)

        nav_row = QHBoxLayout()
        self.btn_prev = QPushButton("Previous Hour")
        self.btn_next = QPushButton("Confirm & Next Hour")
        self.btn_finish = QPushButton("Finish Review")
        nav_row.addWidget(self.btn_prev)
        nav_row.addWidget(self.btn_next)
        nav_row.addWidget(self.btn_finish)
        self.btn_prev.clicked.connect(self._prev_hour)
        self.btn_next.clicked.connect(self._next_hour)
        self.btn_finish.clicked.connect(self._finish)

        self.info_box = QTextEdit()
        self.info_box.setReadOnly(True)
        self.info_box.setMaximumHeight(90)

        self.layout_root.addWidget(self.hour_label)
        self.layout_root.addWidget(self.day_plot)
        self.layout_root.addWidget(feature_grid_widget)
        self.layout_root.addWidget(buttons_group)
        self.layout_root.addLayout(nav_row)
        self.layout_root.addWidget(self.info_box)
        self._render_all()

    def _auto_labels(self):
        return np.asarray(self.review_state["auto_labels"], dtype=np.int16)

    def _current_labels(self):
        return np.asarray(self.review_state["final_labels"], dtype=np.int16)

    def _set_region_override(self, code):
        start_s, end_s = self.edit_region.getRegion()
        start_s = max(0.0, float(start_s))
        end_s = min(float(self.review_state["total_duration_s"]), float(end_s))
        if end_s <= start_s:
            return
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        mask = (state_time >= start_s) & (state_time < end_s)
        overrides = np.asarray(self.review_state["final_override_labels"], dtype=np.int16).copy()
        overrides[mask] = int(code)
        next_state = dict(self.review_state)
        next_state["final_override_labels"] = overrides
        self.review_state = self._run_local_task(
            "Updating final hourly labels...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self._render_all()

    def _assign_state(self, state_name):
        self._set_region_override(STATE_CODES[state_name])

    def _clear_override(self):
        start_s, end_s = self.edit_region.getRegion()
        start_s = max(0.0, float(start_s))
        end_s = min(float(self.review_state["total_duration_s"]), float(end_s))
        if end_s <= start_s:
            return
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        mask = (state_time >= start_s) & (state_time < end_s)
        overrides = np.asarray(self.review_state["final_override_labels"], dtype=np.int16).copy()
        overrides[mask] = -1
        next_state = dict(self.review_state)
        next_state["final_override_labels"] = overrides
        self.review_state = self._run_local_task(
            "Clearing hourly override...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self._render_all()

    def _reset_overrides(self):
        next_state = dict(self.review_state)
        next_state["final_override_labels"] = np.full_like(self.review_state["final_override_labels"], -1, dtype=np.int16)
        self.review_state = self._run_local_task(
            "Resetting all hourly overrides...",
            lambda: recalculate_review_state(next_state, self.cfg),
        )
        self._render_all()

    def _mark_current_hour_confirmed(self):
        confirmed = np.asarray(self.review_state["final_review_confirmed_hours"], dtype=bool).copy()
        confirmed[self.current_hour] = True
        self.review_state["final_review_confirmed_hours"] = confirmed

    def _prev_hour(self):
        self.current_hour = max(0, self.current_hour - 1)
        self._render_all()

    def _next_hour(self):
        self._mark_current_hour_confirmed()
        if self.current_hour < self.n_hours - 1:
            self.current_hour += 1
        self._render_all()

    def _finish(self):
        self._mark_current_hour_confirmed()
        if not np.all(self.review_state["final_review_confirmed_hours"]):
            remaining = np.flatnonzero(~np.asarray(self.review_state["final_review_confirmed_hours"], dtype=bool))
            self._show_error("Review Incomplete", f"Please confirm all hours before finishing. Remaining hours: {remaining.tolist()}")
            return
        self.accept()

    def _render_day_plot(self):
        self.day_plot.clear()
        label_masks = _mask_tracks_from_labels(self._current_labels(), OCCUPANCY_STATE_NAMES)
        tracks = [(name, mask.astype(float), self.state_colors[name]) for name, mask in label_masks.items()]
        self._plot_boolean_tracks(self.day_plot, self.review_state["state_time_s"], tracks, clear=False)
        hour_start = self.current_hour * 3600.0
        hour_end = min(float(self.review_state["total_duration_s"]), (self.current_hour + 1) * 3600.0)
        current_region = _make_vertical_region((hour_start, hour_end), movable=False, brush=pg.mkBrush(0, 0, 0, 30), pen=pg.mkPen("#333333", width=1))
        self.day_plot.addItem(current_region)
        self.day_plot.setXRange(0.0, float(self.review_state["total_duration_s"]), padding=0)

    def _render_hour_plot(self):
        self.hour_plot.clear()
        self.hour_plot.addItem(self.edit_region)
        hour_start = self.current_hour * 3600.0
        hour_end = min(float(self.review_state["total_duration_s"]), (self.current_hour + 1) * 3600.0)
        x0 = max(0.0, hour_start - self.buffer_s)
        x1 = min(float(self.review_state["total_duration_s"]), hour_end + self.buffer_s)
        labels = self._current_labels()
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        mask = (state_time >= x0) & (state_time <= x1)
        if np.any(mask):
            tracks = []
            for state_name in OCCUPANCY_STATE_NAMES:
                tracks.append((state_name, (labels[mask] == STATE_CODES[state_name]).astype(float), self.state_colors[state_name]))
            self._plot_boolean_tracks(self.hour_plot, state_time[mask], tracks, clear=False)
        editable_region = _make_vertical_region((hour_start, hour_end), movable=False, brush=pg.mkBrush(0, 0, 0, 20), pen=pg.mkPen("#444444", width=1, style=Qt.PenStyle.DashLine))
        self.hour_plot.addItem(editable_region)
        self.hour_plot.setXRange(x0, x1, padding=0)

    def _render_feature_plot(self, plot_widget, values, threshold, color, title, y_label):
        plot_widget.clear()
        plot_widget.setTitle(title)
        plot_widget.setLabel("left", y_label)
        hour_start = self.current_hour * 3600.0
        hour_end = min(float(self.review_state["total_duration_s"]), (self.current_hour + 1) * 3600.0)
        x0 = max(0.0, hour_start - self.buffer_s)
        x1 = min(float(self.review_state["total_duration_s"]), hour_end + self.buffer_s)
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        mask = (state_time >= x0) & (state_time <= x1)
        if np.any(mask):
            x, y = _downsample_line(state_time[mask], np.asarray(values, dtype=np.float64)[mask], max_points=2500)
            plot_widget.plot(x=x, y=y, pen=pg.mkPen(color, width=1.5))
        if np.isfinite(float(threshold)):
            plot_widget.addItem(
                pg.InfiniteLine(
                    pos=float(threshold),
                    angle=0,
                    pen=pg.mkPen("#d62728", width=2, style=Qt.PenStyle.DashLine),
                    movable=False,
                )
            )
        current_hour_region = _make_vertical_region(
            (hour_start, hour_end),
            movable=False,
            brush=pg.mkBrush(0, 0, 0, 20),
            pen=pg.mkPen("#444444", width=1, style=Qt.PenStyle.DashLine),
        )
        plot_widget.addItem(current_hour_region)
        plot_widget.setXRange(x0, x1, padding=0)

    def _render_feature_plots(self):
        self._render_feature_plot(
            self.lfhf_plot,
            self.review_state["lfhf_ratio_score"],
            self.review_state["nrem_lfhf_threshold"],
            "#8e44ad",
            "LF/HF Score (NREM feature)",
            "LF/HF score",
        )
        self._render_feature_plot(
            self.imu_plot,
            self.review_state["imu_score_smoothed"],
            self.review_state["sleep_imu_threshold"],
            "#e67e22",
            "IMU Score (shared sleep-like feature, smoothed)",
            "IMU",
        )
        self._render_feature_plot(
            self.highfreq_plot,
            self.review_state["rem_highfreq_power"],
            self.review_state["rem_highfreq_threshold"],
            "#8c564b",
            "80-250 Hz Score (REM raw-channel feature)",
            "80-250 Hz score",
        )
        self._render_spectrogram_plot()

    def _render_spectrogram_plot(self):
        self.spectrogram_plot.clear()
        hour_start = self.current_hour * 3600.0
        hour_end = min(float(self.review_state["total_duration_s"]), (self.current_hour + 1) * 3600.0)
        x0 = max(0.0, hour_start - self.buffer_s)
        x1 = min(float(self.review_state["total_duration_s"]), hour_end + self.buffer_s)
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        freqs_hz = np.asarray(self.review_state.get("review_spectrogram_freqs_hz", np.zeros((0,), dtype=np.float32)), dtype=np.float64)
        zspec = np.asarray(self.review_state.get("review_spectrogram_z", np.zeros((0, 0), dtype=np.float32)), dtype=np.float64)
        mask = (state_time >= x0) & (state_time <= x1)
        if np.any(mask) and freqs_hz.size and zspec.ndim == 2 and zspec.shape[0] == state_time.size:
            segment = zspec[mask].T
            try:
                image_item = pg.ImageItem(axisOrder="row-major")
            except TypeError:
                image_item = pg.ImageItem()
            image_item.setImage(np.asarray(segment, dtype=np.float32), autoLevels=False)
            try:
                cmap = pg.colormap.get("viridis")
                if cmap is not None:
                    image_item.setLookupTable(cmap.getLookupTable())
            except Exception:
                pass
            finite = segment[np.isfinite(segment)]
            if finite.size:
                low, high = np.nanpercentile(finite, [5.0, 95.0])
                if np.isfinite(low) and np.isfinite(high) and high > low:
                    try:
                        image_item.setLevels((float(low), float(high)))
                    except Exception:
                        pass
            y0 = float(np.log2(max(freqs_hz[0], 1e-6)))
            y1 = float(np.log2(max(freqs_hz[-1], 1e-6)))
            image_item.setRect(QRectF(float(x0), y0, float(max(x1 - x0, 1e-6)), float(max(y1 - y0, 1e-6))))
            self.spectrogram_plot.addItem(image_item)
            tick_freqs = []
            freq = 2.0
            max_freq = float(freqs_hz[-1])
            while freq <= max_freq + 1e-6:
                if freq >= freqs_hz[0] - 1e-6:
                    tick_freqs.append((float(np.log2(freq)), f"{int(freq)}"))
                freq *= 2.0
            if tick_freqs:
                self.spectrogram_plot.getAxis("left").setTicks([tick_freqs])
        current_hour_region = _make_vertical_region(
            (hour_start, hour_end),
            movable=False,
            brush=pg.mkBrush(0, 0, 0, 20),
            pen=pg.mkPen("#444444", width=1, style=Qt.PenStyle.DashLine),
        )
        self.spectrogram_plot.addItem(current_hour_region)
        self.spectrogram_plot.setXRange(x0, x1, padding=0)

    def _render_info(self):
        confirmed = np.asarray(self.review_state["final_review_confirmed_hours"], dtype=bool)
        self.hour_label.setText(f"Hour {self.current_hour + 1}/{self.n_hours}")
        hour_start = self.current_hour * 3600.0
        hour_end = min(float(self.review_state["total_duration_s"]), (self.current_hour + 1) * 3600.0)
        state_time = np.asarray(self.review_state["state_time_s"], dtype=np.float64)
        in_hour = (state_time >= hour_start) & (state_time < hour_end)
        current_labels = self._current_labels()
        label_summary = {}
        for state_name in OCCUPANCY_STATE_NAMES:
            code = STATE_CODES[state_name]
            label_summary[state_name] = int(np.sum(in_hour & (current_labels == code)))
        self.info_box.setPlainText(
            f"Confirmed hours: {int(np.sum(confirmed))}/{confirmed.size}\n"
            f"Current hour confirmed: {bool(confirmed[self.current_hour])}\n"
            f"Override count: {int(np.sum(np.asarray(self.review_state['final_override_labels'], dtype=np.int16) >= 0))}\n"
            f"Hour label counts: {label_summary}\n"
            f"Shared sleep-like IMU threshold: {float(self.review_state['sleep_imu_threshold']):.6g}\n"
            f"NREM LF/HF score threshold: {float(self.review_state['nrem_lfhf_threshold']):.6g}\n"
            f"REM 80-250 Hz score threshold: {float(self.review_state['rem_highfreq_threshold']):.6g}\n"
            f"Shared smoothing sigma: {float(self.review_state['imu_smoothing_sigma_s']):.2f}s"
        )
        self.btn_prev.setEnabled(self.current_hour > 0)

    def _render_all(self):
        self._render_day_plot()
        self._render_hour_plot()
        self._render_feature_plots()
        self._render_info()


def run_state_review_wizard(parent, h5_path, config=None):
    cfg = resolve_config(config)

    def _parent_progress_start(text):
        if parent is not None and hasattr(parent, "_start_progress"):
            parent._start_progress(text, busy=True)

    def _parent_progress_update(text):
        if parent is not None and hasattr(parent, "_update_progress"):
            parent._update_progress(text=text, busy=True)

    def _parent_progress_finish(text):
        if parent is not None and hasattr(parent, "_finish_progress"):
            parent._finish_progress(text)

    _parent_progress_start("Preparing state review inputs...")
    saved = run_callable_in_thread(lambda: load_review_progress(h5_path))
    working_override = None
    if saved and "working_mask_confirmed" in saved:
        working_override = np.asarray(saved["working_mask_confirmed"], dtype=bool)
    review_state = run_callable_in_thread(
        lambda: compute_state_review_inputs(h5_path, cfg, working_mask_override=working_override)
    )
    review_state = run_callable_in_thread(lambda: merge_saved_review_state(review_state, saved, cfg))
    _parent_progress_finish("State review ready")

    working_dialog = WorkingReviewDialog(review_state, cfg, parent=parent)
    if working_dialog.exec() != QDialog.DialogCode.Accepted:
        return False
    review_state = working_dialog.review_state
    _parent_progress_start("Refreshing state review after Working confirmation...")
    review_state = run_callable_in_thread(
        lambda: refresh_review_state_after_working(review_state, cfg)
    )
    run_callable_in_thread(
        lambda: save_review_progress(
            h5_path,
            review_state,
            cfg,
            stage="working_review",
            review_complete=False,
            recalculate_before_save=False,
        )
    )
    _parent_progress_finish("Working review saved")

    steps = [
        ("nrem_ratio_review", NREMRatioThresholdReviewDialog),
        ("rem_review", REMThresholdReviewDialog),
        ("wake_split_review", WakeMiniWakeReviewDialog),
        ("final_review", FinalHourlyStateReviewDialog),
    ]
    for stage_name, dialog_cls in steps:
        dialog = dialog_cls(review_state, cfg, parent=parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        review_state = dialog.review_state
        _parent_progress_start(f"Saving {stage_name}...")
        run_callable_in_thread(
            lambda stage_name=stage_name: save_review_progress(
                h5_path,
                review_state,
                cfg,
                stage=stage_name,
                review_complete=False,
                recalculate_before_save=False,
            )
        )
        _parent_progress_finish(f"{stage_name} saved")

    _parent_progress_start("Committing final reviewed states...")
    run_callable_in_thread(lambda: commit_reviewed_states(h5_path, review_state, cfg))
    _parent_progress_finish("State review committed")
    return True
