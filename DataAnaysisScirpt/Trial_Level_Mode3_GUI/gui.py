import sys
import os
import warnings
import numpy as np
import scipy.signal as signal
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                             QHBoxLayout, QPushButton, QLabel, QFileDialog, 
                             QComboBox, QGroupBox, QSpinBox, QFormLayout, QCheckBox, 
                             QTabWidget, QTextEdit, QDialog, QListWidget, QListWidgetItem,
                             QMessageBox)

from data_processing import (
    ALIGNMENT_CHIP_PATTERN,
    ALIGNMENT_RESIDUAL_WARNING_MS,
    fit_alignment_anchors,
    make_alignment_sidecar_entry,
)


def _fill_nan_for_display(values, fill_value=0.0):
    arr = np.asarray(values, dtype=float).copy()
    if arr.size == 0:
        return arr
    finite = np.isfinite(arr)
    if np.all(finite):
        return arr
    if np.any(finite):
        x = np.arange(arr.size)
        arr[~finite] = np.interp(x[~finite], x[finite], arr[finite])
    else:
        arr[:] = fill_value
    return arr


def _finite_abs_max(values, default=1.0):
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return default
    peak = float(np.max(np.abs(finite)))
    return peak if peak > 0 else default


def _nanmedian_axis0(values):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        out = np.nanmedian(values, axis=0)
    return np.where(np.isfinite(out), out, 0.0)


def _nanmean_axis0(values):
    arr = np.asarray(values, dtype=float)
    valid = np.isfinite(arr)
    counts = np.sum(valid, axis=0)
    summed = np.nansum(arr, axis=0)
    out = np.divide(summed, counts, out=np.zeros_like(summed, dtype=float), where=counts > 0)
    return out


class AlignmentReviewDialog(QDialog):
    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.review = review
        self.anchors_by_candidate = {}
        self.sidecar_entry = None
        self.current_candidate_index = 0

        self.setWindowTitle("Manual Mode3 Alignment Anchors")
        self.resize(1150, 760)

        root = QHBoxLayout(self)

        left = QVBoxLayout()
        self.lbl_file = QLabel(os.path.basename(review["raw_path"]))
        self.lbl_file.setWordWrap(True)
        left.addWidget(self.lbl_file)

        self.candidate_list = QListWidget()
        self.candidate_list.currentRowChanged.connect(self.on_candidate_changed)
        left.addWidget(self.candidate_list, stretch=1)

        self.lbl_fit = QLabel("")
        self.lbl_fit.setWordWrap(True)
        left.addWidget(self.lbl_fit)

        root.addLayout(left, stretch=0)

        right = QVBoxLayout()
        self.lbl_roi = QLabel("")
        self.lbl_roi.setWordWrap(True)
        right.addWidget(self.lbl_roi)

        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Time from alignment edge", units="ms")
        self.plot.setLabel("left", "Neural / alignment")
        self.plot.scene().sigMouseClicked.connect(self.on_plot_clicked)
        right.addWidget(self.plot, stretch=1)

        buttons = QHBoxLayout()
        self.btn_prev_roi = QPushButton("< Prev ROI")
        self.btn_next_roi = QPushButton("Next ROI >")
        self.btn_clear = QPushButton("Clear Marker")
        self.btn_ok = QPushButton("Confirm Anchors")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_prev_roi.clicked.connect(self.prev_roi)
        self.btn_next_roi.clicked.connect(self.next_roi)
        self.btn_clear.clicked.connect(self.clear_current_marker)
        self.btn_ok.clicked.connect(self.confirm_anchors)
        self.btn_cancel.clicked.connect(self.reject)
        for btn in [self.btn_prev_roi, self.btn_next_roi, self.btn_clear, self.btn_ok, self.btn_cancel]:
            buttons.addWidget(btn)
        right.addLayout(buttons)
        root.addLayout(right, stretch=1)

        self.populate_candidates()
        if self.candidate_list.count() > 0:
            self.candidate_list.setCurrentRow(0)
        self.update_fit_label()

    def populate_candidates(self):
        self.candidate_list.clear()
        for idx, candidate in enumerate(self.review["candidates"]):
            item = QListWidgetItem(self._candidate_label(idx))
            if not candidate.get("has_timestamp"):
                item.setForeground(QBrush(QColor("gray")))
            self.candidate_list.addItem(item)

    def _candidate_label(self, idx):
        candidate = self.review["candidates"][idx]
        ts = candidate.get("trial_start_timestamp_ms")
        ts_text = "no TrialStartTimestampMs" if ts is None else f"{ts:.0f} ms"
        anchor = self.anchors_by_candidate.get(idx)
        marker = "anchor set" if anchor else "no anchor"
        return f"{idx + 1:03d} | Trial {candidate.get('trial_id')} | {ts_text} | {marker}"

    def refresh_candidate_label(self, idx):
        item = self.candidate_list.item(idx)
        if item is not None:
            item.setText(self._candidate_label(idx))

    def on_candidate_changed(self, row):
        if row < 0:
            return
        self.current_candidate_index = row
        self.draw_current_roi()

    def prev_roi(self):
        row = self.candidate_list.currentRow()
        if row > 0:
            self.candidate_list.setCurrentRow(row - 1)

    def next_roi(self):
        row = self.candidate_list.currentRow()
        if row < self.candidate_list.count() - 1:
            self.candidate_list.setCurrentRow(row + 1)

    def clear_current_marker(self):
        idx = self.current_candidate_index
        if idx in self.anchors_by_candidate:
            del self.anchors_by_candidate[idx]
            self.refresh_candidate_label(idx)
            self.update_fit_label()
            self.draw_current_roi()

    def _current_candidate(self):
        if not self.review["candidates"]:
            return None
        return self.review["candidates"][self.current_candidate_index]

    def draw_current_roi(self):
        candidate = self._current_candidate()
        if candidate is None:
            return

        self.plot.clear()
        fs_raw = float(self.review["fs_raw"])
        roi_start = int(candidate["roi_start_sample"])
        roi_end = int(candidate["roi_end_sample"])
        align_sample = int(candidate["alignment_sample"])
        x_ms = (np.arange(roi_start, roi_end) - align_sample) * 1000.0 / fs_raw

        raw = np.asarray(self.review["raw_filtered"][roi_start:roi_end], dtype=float)
        align = np.asarray(self.review["raw_align"][roi_start:roi_end], dtype=float)
        self.plot.plot(x_ms, raw, pen=pg.mkPen("y", width=1), name="3.5-4.5 kHz neural")

        raw_amp = _finite_abs_max(raw)
        align_max = _finite_abs_max(align, default=0.0)
        if align_max > 0:
            self.plot.plot(
                x_ms,
                align / align_max * raw_amp * 0.7,
                pen=pg.mkPen(0, 210, 255, 160),
                name="alignment trace",
            )

        anchor = self.anchors_by_candidate.get(self.current_candidate_index)
        if anchor:
            anchor_ms = (anchor["pulse_sample"] - align_sample) * 1000.0 / fs_raw
            marker = pg.InfiniteLine(pos=anchor_ms, angle=90, movable=False, pen=pg.mkPen("r", width=2))
            self.plot.addItem(marker)
            self._draw_pattern_overlay(anchor_ms, raw_amp)

        self.plot.setXRange(
            float(self.review["roi_window_ms"][0]),
            float(self.review["roi_window_ms"][1]),
            padding=0.01,
        )
        timestamp = candidate.get("trial_start_timestamp_ms")
        timestamp_text = "missing timestamp" if timestamp is None else f"TrialStartTimestampMs={timestamp:.0f} ms"
        self.lbl_roi.setText(
            f"Trial {candidate.get('trial_id')} | alignment sample {align_sample} | {timestamp_text}. "
            "Click within the ROI to mark the 9-chip neural pulse start."
        )

    def _draw_pattern_overlay(self, anchor_ms, raw_amp):
        top = raw_amp * 0.92
        step = raw_amp * 0.18
        for idx, bit in enumerate(ALIGNMENT_CHIP_PATTERN):
            y = top if bit == "1" else top - step
            pen = pg.mkPen((255, 80, 80) if bit == "1" else (180, 180, 180), width=3)
            self.plot.plot([anchor_ms + idx, anchor_ms + idx + 1], [y, y], pen=pen)

    def on_plot_clicked(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        candidate = self._current_candidate()
        if candidate is None or not candidate.get("has_timestamp"):
            QMessageBox.information(self, "Cannot Anchor", "This ROI has no TrialStartTimestampMs and cannot be used as an anchor.")
            return
        vb = self.plot.plotItem.vb
        if not self.plot.sceneBoundingRect().contains(event.scenePos()):
            return
        pos = vb.mapSceneToView(event.scenePos())
        x_ms = float(pos.x())
        roi_start_ms, roi_end_ms = self.review["roi_window_ms"]
        if x_ms < roi_start_ms or x_ms > roi_end_ms:
            return

        fs_raw = float(self.review["fs_raw"])
        pulse_sample = float(candidate["alignment_sample"] + (x_ms * fs_raw / 1000.0))
        self.anchors_by_candidate[self.current_candidate_index] = {
            "trial_id": int(candidate["trial_id"]),
            "trial_start_timestamp_ms": float(candidate["trial_start_timestamp_ms"]),
            "alignment_sample": int(candidate["alignment_sample"]),
            "pulse_sample": pulse_sample,
        }
        self.refresh_candidate_label(self.current_candidate_index)
        self.update_fit_label()
        self.draw_current_roi()

    def current_anchors(self):
        return [self.anchors_by_candidate[idx] for idx in sorted(self.anchors_by_candidate)]

    def update_fit_label(self):
        anchors = self.current_anchors()
        if not anchors:
            self.lbl_fit.setText("Anchors: 0. At least one anchor is required for this raw file.")
            return
        try:
            fit = fit_alignment_anchors(anchors, self.review["fs_raw"])
        except Exception as exc:
            self.lbl_fit.setText(f"Fit error: {exc}")
            return
        warning = ""
        if fit["max_abs_residual_ms"] > ALIGNMENT_RESIDUAL_WARNING_MS:
            warning = " | WARNING: residual > 5 ms"
        self.lbl_fit.setText(
            f"Anchors: {fit['n_anchors']} | {fit['method']} | "
            f"RMS {fit['rms_residual_ms']:.3f} ms | max {fit['max_abs_residual_ms']:.3f} ms{warning}"
        )

    def confirm_anchors(self):
        anchors = self.current_anchors()
        if not anchors:
            QMessageBox.warning(self, "Missing Anchors", "Please mark at least one anchor for this raw file.")
            return
        try:
            fit = fit_alignment_anchors(anchors, self.review["fs_raw"])
        except Exception as exc:
            QMessageBox.warning(self, "Fit Failed", str(exc))
            return
        if fit["max_abs_residual_ms"] > ALIGNMENT_RESIDUAL_WARNING_MS:
            reply = QMessageBox.question(
                self,
                "Large Residual",
                f"Max residual is {fit['max_abs_residual_ms']:.3f} ms, above 5 ms. Continue?",
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.sidecar_entry = make_alignment_sidecar_entry(self.review, anchors, fit)
        self.accept()

    def get_entry(self):
        return self.sidecar_entry

class MainWindow(QMainWindow):
    def __init__(self, dp_instance, verifier_instance):
        super().__init__()
        self.setWindowTitle("Mode 3 Data Analysis & Verification")
        self.setGeometry(100, 100, 1500, 1000)
        self.dp = dp_instance
        self.verifier = verifier_instance
        
        self.file_paths = {
            'lfp': '', 'raw': '', 'sensor': '', 'trial': '', 'tevent': ''
        }
        self.dir_paths = {
            'neural': '', 'trial': '', 'tevent': '', 'output': ''
        }
        
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        
        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs)
        
        # --- TAB 1: Single Trial Mode ---
        self.tab_single = QWidget()
        single_layout = QHBoxLayout(self.tab_single)
        
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_panel.setFixedWidth(300)
        
        # Files Group
        file_group = QGroupBox("1. Upload Files")
        file_layout = QVBoxLayout()
        for ftype in ['lfp', 'raw', 'sensor', 'trial', 'tevent']:
            btn = QPushButton(f"Select {ftype.capitalize()} File")
            lbl = QLabel("Not selected")
            setattr(self, f"btn_{ftype}", btn)
            setattr(self, f"lbl_{ftype}", lbl)
            btn.clicked.connect(lambda checked, t=ftype: self.select_file(t))
            file_layout.addWidget(btn); file_layout.addWidget(lbl)
            
        self.btn_process = QPushButton("Process Raw Files")
        self.btn_process.setStyleSheet("background-color: #2b5c8f; color: white;")
        self.btn_process.clicked.connect(self.process_files)
        file_layout.addWidget(self.btn_process)
        
        # Add H5 Quick Loader
        self.btn_load_h5 = QPushButton("Load HDF5 (.h5) Pack")
        self.btn_load_h5.setStyleSheet("background-color: #1a756b; color: white; margin-top: 5px;")
        self.btn_load_h5.clicked.connect(self.process_h5)
        file_layout.addWidget(self.btn_load_h5)
        
        file_group.setLayout(file_layout)
        
        # Navigation Group
        nav_group = QGroupBox("2. Trial View")
        nav_layout = QVBoxLayout()
        self.cb_trials = QComboBox()
        self.cb_trials.currentIndexChanged.connect(self.on_trial_select)
        nav_layout.addWidget(QLabel("Select Trial:"))
        nav_layout.addWidget(self.cb_trials)
        
        nav_btns = QHBoxLayout()
        self.btn_prev = QPushButton("< Prev")
        self.btn_prev.clicked.connect(self.prev_trial)
        self.btn_next = QPushButton("Next >")
        self.btn_next.clicked.connect(self.next_trial)
        nav_btns.addWidget(self.btn_prev)
        nav_btns.addWidget(self.btn_next)
        nav_layout.addLayout(nav_btns)
        nav_group.setLayout(nav_layout)
        
        # Verify Group
        verify_group = QGroupBox("3. Verification Analysis")
        verify_layout = QVBoxLayout()
        self.btn_verify = QPushButton("Run Decoding Analysis")
        self.btn_verify.clicked.connect(self.run_decoding)
        self.lbl_verify = QLabel("Metrics will appear here")
        self.lbl_verify.setWordWrap(True)
        verify_layout.addWidget(self.btn_verify)
        verify_layout.addWidget(self.lbl_verify)
        verify_group.setLayout(verify_layout)
        
        # Spectrogram Config Group
        spec_group = QGroupBox("4. Spectrogram Config")
        spec_layout = QFormLayout()
        
        self.chk_median_ref = QCheckBox("Apply Median Re-Referencing")
        self.chk_median_ref.setChecked(True)
        self.chk_median_ref.stateChanged.connect(self.redraw_current_trial)
        
        self.cb_lfp_channel = QComboBox()
        self.cb_lfp_channel.addItem("Average (All)")
        for i in range(16):
            self.cb_lfp_channel.addItem(f"Ch {i}")
        self.cb_lfp_channel.currentIndexChanged.connect(self.redraw_current_trial)
        
        self.spin_nperseg = QSpinBox()
        self.spin_nperseg.setRange(10, 5000)
        self.spin_nperseg.setValue(500)
        self.spin_nperseg.setSuffix(" ms")
        self.spin_nperseg.valueChanged.connect(self.redraw_current_trial)
        
        self.spin_noverlap = QSpinBox()
        self.spin_noverlap.setRange(0, 4999)
        self.spin_noverlap.setValue(250)
        self.spin_noverlap.setSuffix(" ms")
        self.spin_noverlap.valueChanged.connect(self.redraw_current_trial)
        
        self.spin_band_bin = QSpinBox()
        self.spin_band_bin.setRange(10, 5000)
        self.spin_band_bin.setValue(250)
        self.spin_band_bin.setSuffix(" ms")
        self.spin_band_bin.valueChanged.connect(self.redraw_current_trial)
        
        self.spin_band_overlap = QSpinBox()
        self.spin_band_overlap.setRange(0, 4999)
        self.spin_band_overlap.setValue(125)
        self.spin_band_overlap.setSuffix(" ms")
        self.spin_band_overlap.valueChanged.connect(self.redraw_current_trial)
        
        spec_layout.addRow("", self.chk_median_ref)
        spec_layout.addRow("Select Channel:", self.cb_lfp_channel)
        spec_layout.addRow("Window (nperseg):", self.spin_nperseg)
        spec_layout.addRow("Overlap:", self.spin_noverlap)
        spec_layout.addRow("Band Bin Length:", self.spin_band_bin)
        spec_layout.addRow("Band Overlap:", self.spin_band_overlap)
        spec_group.setLayout(spec_layout)

        self.lbl_phase_status = QLabel("")
        self.lbl_phase_status.setWordWrap(True)
        self.lbl_phase_status.setStyleSheet("color: #334455; font-size: 11px;")
        
        left_layout.addWidget(file_group)
        left_layout.addWidget(nav_group)
        left_layout.addWidget(verify_group)
        left_layout.addWidget(spec_group)
        left_layout.addWidget(self.lbl_phase_status)
        left_layout.addStretch()
        
        # Right Panel - Plot
        self.plot_panel = pg.GraphicsLayoutWidget()
        
        # Raw Sync (3.5k-4.5k)
        self.p_raw = self.plot_panel.addPlot(title="Neural Sync Pulse (3.5-4.5 kHz Filtered)")
        self.plot_panel.nextRow()
        
        # LFP Spectrogram
        self.p_lfp_spec = self.plot_panel.addPlot(title="LFP Average Spectrogram (0-150Hz)", xLink=self.p_raw)
        self.img_lfp = pg.ImageItem()
        self.p_lfp_spec.addItem(self.img_lfp)
        self.img_lfp.setLookupTable(self._get_safe_lut())
        self.plot_panel.nextRow()
        
        # Band Power Plot
        self.p_band = self.plot_panel.addPlot(title="LFP Band Power (Theta vs Gamma)", xLink=self.p_raw)
        self.p_band.addLegend()
        self.plot_panel.nextRow()
        
        # Behavior Events
        self.p_event = self.plot_panel.addPlot(title="Behavior Lick Events (Left=8, Right=10)", xLink=self.p_raw)
        self.p_event.addLegend()
        self.p_event.getAxis('left').setTicks([[(1, 'L-Lick'), (2, 'R-Lick')]])
        self.plot_panel.nextRow()
        
        # Raster
        self.p_raster = self.plot_panel.addPlot(title="Raster (Channels 0-15)", xLink=self.p_raw)
        self.plot_panel.nextRow()
        
        # ESA Raw
        self.p_esa = self.plot_panel.addPlot(title="ESA Raw (Channels 0-15)", xLink=self.p_raw)
        self.p_esa.addLegend()
        self.plot_panel.nextRow()
        
        self.p_sen = self.plot_panel.addPlot(title="IMU Accelerometer (X, Y, Z)", xLink=self.p_raw)
        
        single_layout.addWidget(left_panel)
        single_layout.addWidget(self.plot_panel, stretch=1)
        
        # --- TAB 2: Batch Processing Mode ---
        self.tab_batch = QWidget()
        batch_layout = QVBoxLayout(self.tab_batch)
        
        folder_group = QGroupBox("1. Setup Batch Directories")
        flayout = QFormLayout()
        
        for dtype, label in [
            ('neural', 'Mode3 Neural Dir'),
            ('trial', 'Trial.txt'),
            ('tevent', 'Tevent.txt'),
            ('output', 'H5 Output Dir'),
        ]:
            btn = QPushButton(f"Select {label}")
            lbl = QLabel("Not selected")
            setattr(self, f"btn_b_{dtype}", btn)
            setattr(self, f"lbl_b_{dtype}", lbl)
            btn.clicked.connect(lambda checked, t=dtype: self.select_batch_path(t))
            flayout.addRow(btn, lbl)
            
        folder_group.setLayout(flayout)
        
        action_group = QGroupBox("2. Batch Process & Export")
        alayout = QVBoxLayout()
        self.btn_b_run = QPushButton("Run Batch Pipeline & Export to H5")
        self.btn_b_run.setStyleSheet("background-color: #8f2b2b; color: white; padding: 10px; font-weight: bold;")
        self.btn_b_run.clicked.connect(self.run_batch_pipeline)
        alayout.addWidget(self.btn_b_run)
        action_group.setLayout(alayout)
        
        log_group = QGroupBox("Missing Trials & Parsing Log")
        log_layout = QVBoxLayout()
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        log_layout.addWidget(self.log_console)
        log_group.setLayout(log_layout)
        
        batch_layout.addWidget(folder_group)
        batch_layout.addWidget(action_group)
        batch_layout.addWidget(log_group, stretch=1)
        
        # Assemble Tabs
        self.tabs.addTab(self.tab_single, "Visualizer & Tuning")
        self.tabs.addTab(self.tab_batch, "Batch H5 Exporter")
        self._refresh_phase_status_label()

    def _get_safe_lut(self):
        # Avoid hard dependency on optional colormap files like "jet".
        for name in ("turbo", "viridis", "CET-L17"):
            try:
                return pg.colormap.get(name).getLookupTable()
            except Exception:
                pass

        positions = np.array([0.0, 0.25, 0.5, 0.75, 1.0])
        colors = np.array([
            [0, 0, 0, 255],
            [0, 0, 255, 255],
            [0, 255, 255, 255],
            [255, 255, 0, 255],
            [255, 0, 0, 255]
        ], dtype=np.ubyte)
        cmap = pg.ColorMap(positions, colors)
        return cmap.getLookupTable(0.0, 1.0, 256)

    def _get_phase_status_text(self, trial_data=None):
        phase_meta = {}
        if trial_data is not None:
            phase_meta = trial_data.get('lfp_phase_compensation', {}) or {}
        if not phase_meta:
            phase_meta = getattr(self.dp, 'lfp_phase_compensation_info', {}) or {}

        if not phase_meta.get('applied', False):
            return "LFP phase compensation: disabled"

        mean_delay_ms = float(phase_meta.get('mean_group_delay_ms', 0.0))
        return f"LFP phase compensation: enabled (firmware IIR, mean delay {mean_delay_ms:.3f} ms)"

    def _refresh_phase_status_label(self, trial_data=None):
        if hasattr(self, 'lbl_phase_status'):
            self.lbl_phase_status.setText(self._get_phase_status_text(trial_data))
        
    def select_file(self, ftype):
        fname, _ = QFileDialog.getOpenFileName(self, f"Select {ftype} file")
        if fname:
            self.file_paths[ftype] = fname
            getattr(self, f"lbl_{ftype}").setText(os.path.basename(fname))
            
    def select_directory(self, dtype):
        dname = QFileDialog.getExistingDirectory(self, f"Select {dtype} directory")
        if dname:
            self.dir_paths[dtype] = dname
            getattr(self, f"lbl_b_{dtype}").setText(dname)

    def select_batch_path(self, dtype):
        if dtype in ('trial', 'tevent'):
            fname, _ = QFileDialog.getOpenFileName(self, f"Select {dtype} file", filter="Text Files (*.txt);;All Files (*)")
            if fname:
                self.dir_paths[dtype] = fname
                getattr(self, f"lbl_b_{dtype}").setText(fname)
            return
        self.select_directory(dtype)

    def _request_alignment_annotation(self, review):
        candidates = review.get("candidates", [])
        if not candidates:
            QMessageBox.warning(
                self,
                "No Alignment ROI",
                "No alignment rising edge was found in this raw file.",
            )
            return None
        if not any(candidate.get("has_timestamp") for candidate in candidates):
            QMessageBox.warning(
                self,
                "No Timestamped Trial",
                "Alignment ROIs were found, but none match a trial with TrialStartTimestampMs.",
            )
            return None
        dialog = AlignmentReviewDialog(review, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.get_entry()
        return None
            
    def process_files(self):
        if not all(self.file_paths.values()):
            self.lbl_verify.setText("Please select all 5 files!")
            return
            
        self.lbl_verify.setText("Preparing manual alignment review...")
        QApplication.processEvents()

        try:
            review = self.dp.prepare_alignment_review(self.file_paths['raw'], trial_file=self.file_paths['trial'])
            entry = self._request_alignment_annotation(review)
            if entry is None:
                self.lbl_verify.setText("Manual alignment cancelled.")
                return

            self.lbl_verify.setText("Processing... Please wait.")
            QApplication.processEvents()
            self.dp.process_files(
                self.file_paths['lfp'], self.file_paths['raw'], self.file_paths['sensor'],
                self.file_paths['trial'], self.file_paths['tevent'],
                alignment_fit=entry["fit"],
            )
        except Exception as exc:
            self.lbl_verify.setText(f"Processing failed: {exc}")
            return
        
        trials = sorted(list(self.dp.trial_dict.keys()))
        self.cb_trials.clear()
        for t in trials:
            self.cb_trials.addItem(f"Trial {t}", t)
            
        self._refresh_phase_status_label()
        self.lbl_verify.setText(
            f"Processed {len(trials)} valid raw trials. {self._get_phase_status_text()}"
        )
        
    def process_h5(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select H5 Pack", filter="H5 Files (*.h5)")
        if not fname: return
            
        self.lbl_verify.setText("Loading H5 Pack... Please wait.")
        QApplication.processEvents()
        
        try:
            keys = self.dp.load_from_h5(fname)
            self.cb_trials.clear()
            for k in keys:
                self.cb_trials.addItem(k, k)
            first_trial = self.dp.get_extracted_data(keys[0]) if keys else None
            self._refresh_phase_status_label(first_trial)
            self.lbl_verify.setText(
                f"Loaded {len(keys)} trials from H5 pack. {self._get_phase_status_text(first_trial)}"
            )
        except Exception as e:
            self.lbl_verify.setText(f"Failed to load H5 file: {str(e)}")
            
    def run_batch_pipeline(self):
        ndir = self.dir_paths['neural']
        trial_file = self.dir_paths['trial']
        tevent_file = self.dir_paths['tevent']
        odir = self.dir_paths['output']
        
        if not all([ndir, trial_file, tevent_file, odir]):
            self.log_console.append("<b>Error:</b> Please select Mode3 neural dir, Trial.txt, Tevent.txt, and output dir.")
            return
            
        self.btn_b_run.setEnabled(False)
        self.log_console.append(f"<b>[Batch Started]</b> Scanning {ndir}")
        self.log_console.append(self._get_phase_status_text())
        QApplication.processEvents()

        try:
            output_files, stats, logs = self.dp.batch_process_directories(
                ndir,
                trial_file,
                tevent_file,
                output_dir=odir,
                annotation_provider=self._request_alignment_annotation,
            )
        except Exception as exc:
            self.log_console.append(f"<b><span style='color:red;'>[FAILED]</span></b> {exc}")
            self.btn_b_run.setEnabled(True)
            return
        
        # Output Logging
        for log in logs:
            if "CRITICAL" in log or "Error" in log:
                self.log_console.append(f"<span style='color:red;'>{log}</span>")
            else:
                self.log_console.append(f"<span style='color:orange;'>{log}</span>")
                
        if output_files:
            self.log_console.append(f"Successfully compiled {stats['total_trials_aligned']} trials out of {stats['valid_groups_processed']} blocks.")
            for date_key, out_file in output_files.items():
                self.log_console.append(f"<b><span style='color:green;'>[SUCCESS]</span></b> {date_key}: {out_file}")
        else:
            self.log_console.append("<b><span style='color:red;'>[FAILED]</span></b> No valid trials compiled. H5 export aborted.")
            
        self.btn_b_run.setEnabled(True)
        
    def on_trial_select(self, index):
        if index < 0: return
        trial_id = self.cb_trials.itemData(index)
        self.draw_plots(trial_id)
        
    def prev_trial(self):
        i = self.cb_trials.currentIndex()
        if i > 0: self.cb_trials.setCurrentIndex(i-1)
        
    def next_trial(self):
        i = self.cb_trials.currentIndex()
        if i < self.cb_trials.count() - 1: self.cb_trials.setCurrentIndex(i+1)
        
    def redraw_current_trial(self):
        index = self.cb_trials.currentIndex()
        if index >= 0:
            self.on_trial_select(index)
            
    def _draw_state_boundaries(self, plot, trial_start_ts, states):
        # Draw vertical lines for specific state events mapping to absolute t offsets
        colors = {
            20: 'g',            # TrialStart (Green)
            2: 'r',             # TrialEnd (Red)
            6: 'c',             # Reward (Cyan)
            9: 'm'              # Error (Magenta)
        }
        labels = {20:'Start', 2:'End', 6:'Reward', 9:'Error'}
        
        for st, ts in states:
            if st in colors:
                offset_ms = ts - trial_start_ts
                vline = pg.InfiniteLine(angle=90, movable=False, pen=colors[st])
                vline.setPos(offset_ms)
                # Ensure we don't have overlapping labels by just checking
                label = pg.TextItem(labels[st], color=colors[st], anchor=(0, 1))
                label.setPos(offset_ms, 0) # Top
                plot.addItem(vline)
                plot.addItem(label)

    def draw_plots(self, trial_id):
        data = self.dp.get_extracted_data(trial_id)
        if not data: return
        self._refresh_phase_status_label(data)
        
        self.p_raw.clear()
        self.p_lfp_spec.clear()
        self.p_lfp_spec.addItem(self.img_lfp) # Re-add image to plot 
        self.p_band.clear()
        self.p_event.clear()
        self.p_raster.clear()
        self.p_esa.clear()
        self.p_sen.clear()
        
        t_start_ts = data['trial_start_ts']
        states = data['states']
        
        # --- RAW (Filtered 4KHz) & Alignment ---
        raw_t, raw_v, raw_align_v = data['raw']
        self.p_raw.plot(raw_t, raw_v, pen='y', name='Neural Sync (4 kHz)')
        
        # Scale the alignment pulse intelligently into the raw plot view so it doesn't crush the y-axis
        align_peak = _finite_abs_max(raw_align_v, default=0.0)
        if align_peak > 0:
            scale_factor = _finite_abs_max(raw_v) / align_peak
            scaled_align = raw_align_v * scale_factor * 0.8  # Plot up to 80% height matching peak
            self.p_raw.plot(raw_t, scaled_align, pen=(0, 255, 255, 180), name='Aligned Index (Scaled)')
            
        # --- LFP Spectrogram (Re-referenced) ---
        lfp_t, lfp_v = data['lfp']
        if len(lfp_v) > 0 and lfp_v.shape[0] > 0 and lfp_v.shape[1] > 1:
            active_lfp = np.array(lfp_v, dtype=float, copy=True)
            
            # Apply dynamic median re-reference
            if self.chk_median_ref.isChecked():
                median_line = _nanmedian_axis0(active_lfp)
                active_lfp = active_lfp - median_line
                
            # Channel slicing
            ch_idx = self.cb_lfp_channel.currentIndex()
            if ch_idx == 0:
                lfp_target = _nanmean_axis0(active_lfp)
            else:
                lfp_target = active_lfp[ch_idx - 1] if ch_idx - 1 < active_lfp.shape[0] else _nanmean_axis0(active_lfp)
            lfp_target = _fill_nan_for_display(lfp_target)
                
            # In raw mode we have fs_lfp in trial_dict; in H5 mode infer from lfp_t spacing.
            if trial_id in self.dp.trial_dict and 'fs_lfp' in self.dp.trial_dict[trial_id]:
                fs_lfp = self.dp.trial_dict[trial_id]['fs_lfp']
            elif len(lfp_t) > 1:
                dt_sec = (lfp_t[1] - lfp_t[0]) / 1000.0
                fs_lfp = (1.0 / dt_sec) if dt_sec > 0 else 1000.0
            else:
                fs_lfp = 1000.0
            
            # Read Spectrogram Config from UI
            nperseg_ms = self.spin_nperseg.value()
            noverlap_ms = self.spin_noverlap.value()
            if noverlap_ms >= nperseg_ms:
                noverlap_ms = nperseg_ms - 1  # prevent overflow crash
                self.spin_noverlap.setValue(noverlap_ms)
                
            nperseg_samples = max(2, int((nperseg_ms / 1000.0) * fs_lfp))
            nperseg_samples = min(nperseg_samples, len(lfp_target))
            noverlap_samples = int((noverlap_ms / 1000.0) * fs_lfp)
            noverlap_samples = min(noverlap_samples, max(0, nperseg_samples - 1))
            
            # Scipy Spectrogram
            f, t_spec, Sxx = signal.spectrogram(
                lfp_target, fs=fs_lfp, 
                nperseg=nperseg_samples, 
                noverlap=noverlap_samples
            )
            
            # Bound 0 - 150 Hz
            f_mask = f <= 150
            f_bounded = f[f_mask]
            Sxx_bounded = Sxx[f_mask, :]
            
            # Log compress
            Sxx_log = 10 * np.log10(Sxx_bounded + 1e-10)
            
            # Update ImageItem
            self.img_lfp.setImage(Sxx_log.T, autoLevels=True)
            
            # Set scale and position to map to actual time axis
            # x0, y0 is the coordinate of (0,0) in image.
            # dx is spacing. t_spec gives offsets in seconds from lfp_t[0].
            if len(t_spec) > 1 and len(f_bounded) > 1:
                t_offset_ms = lfp_t[0] + (t_spec[0] * 1000)
                dt_ms = (t_spec[1] - t_spec[0]) * 1000
                df = f_bounded[1] - f_bounded[0]
                
                # Reshape coordinates
                tr = pg.QtGui.QTransform()
                tr.translate(t_offset_ms, 0)
                tr.scale(dt_ms, df)
                self.img_lfp.setTransform(tr)
                
            # --- LFP Band Power (Theta/Gamma) ---
            bin_ms = self.spin_band_bin.value()
            band_overlap_ms = self.spin_band_overlap.value()
            
            if bin_ms < 10: bin_ms = 10
            if band_overlap_ms >= bin_ms:
                band_overlap_ms = bin_ms - 1
                self.spin_band_overlap.setValue(band_overlap_ms)
                
            bin_samples = max(2, int((bin_ms / 1000.0) * fs_lfp))
            bin_samples = min(bin_samples, len(lfp_target))
            overlap_samples_b = int((band_overlap_ms / 1000.0) * fs_lfp)
            overlap_samples_b = min(overlap_samples_b, max(0, bin_samples - 1))
            
            fb, tb, Sxx_b = signal.spectrogram(
                lfp_target, fs=fs_lfp, 
                nperseg=bin_samples, 
                noverlap=overlap_samples_b
            )
            
            # Map time blocks back to absolute window parameters
            if len(tb) > 0:
                tb_abs = lfp_t[0] + (tb * 1000.0)
                
                # Theta (4 - 8 Hz)
                theta_mask = (fb >= 4) & (fb <= 8)
                theta_power = np.mean(Sxx_b[theta_mask, :], axis=0) if np.any(theta_mask) else np.zeros_like(tb)
                
                # Gamma (30 - 80 Hz)
                gamma_mask = (fb >= 30) & (fb <= 80)
                gamma_power = np.mean(Sxx_b[gamma_mask, :], axis=0) if np.any(gamma_mask) else np.zeros_like(tb)
                
                # Scale for visual aesthetics
                self.p_band.plot(tb_abs, theta_power, pen='g', name='Theta (4-8 Hz)')
                self.p_band.plot(tb_abs, gamma_power, pen='r', name='Gamma (30-80 Hz)')
                
        # --- BEHAVIOR EVENTS Plot ---
        tevent = data.get('tevent')
        if tevent is not None:
            left_licks = []
            right_licks = []
            for ev_id, ev_ts in tevent.events:
                rel_ts = ev_ts - t_start_ts
                if ev_id == 8:
                    left_licks.append(rel_ts)
                elif ev_id == 10:
                    right_licks.append(rel_ts)
            
            if left_licks:
                self.p_event.plot(left_licks, [1]*len(left_licks), pen=None, symbol='o', symbolSize=8, symbolBrush=(0, 100, 255, 200), name='Left Lick')
            if right_licks:
                self.p_event.plot(right_licks, [2]*len(right_licks), pen=None, symbol='o', symbolSize=8, symbolBrush=(255, 50, 0, 200), name='Right Lick')
        
        self.p_event.setYRange(0, 3)
                
        # --- RASTER Plot ---
        lfp_t_r, raster_v = data['raster']
        if raster_v is not None:
            scatter_pts = []
            for ch in range(min(16, raster_v.shape[0])):
                # Threshold for a spike event could be >0.5
                spikes_mask = raster_v[ch] > 0.5
                spike_times = lfp_t_r[spikes_mask]
                for st in spike_times:
                    scatter_pts.append({'pos': (st, ch), 'size': 5, 'brush': pg.mkBrush(255, 255, 255, 150)})
            
            scatter = pg.ScatterPlotItem(spots=scatter_pts)
            self.p_raster.addItem(scatter)
            self.p_raster.setYRange(-1, 16)
        
        # --- ESA Raw Plot ---
        esa_entry = data.get('esa', (None, None))
        esa_t_e, esa_v = esa_entry if esa_entry is not None else (None, None)
        if esa_v is not None and esa_v.shape[0] > 0:
            n_esa_ch = min(16, esa_v.shape[0])
            # Compute offset spacing from overall data range
            global_std = np.nanstd(esa_v)
            offset_step = global_std * 4 if np.isfinite(global_std) and global_std > 0 else 1.0
            # 16 distinct colors with strong contrast
            esa_colors = [
                (255,  50,  50), ( 50, 200,  50), ( 50, 100, 255), (255, 200,  50),
                (200,  50, 255), ( 50, 255, 200), (255, 130,  50), (150, 150, 255),
                (255,  50, 150), ( 50, 255, 100), (200, 200,  50), (100,  50, 200),
                (255, 180, 180), ( 50, 200, 200), (200, 100,  50), (180,  50, 100),
            ]
            for i in range(n_esa_ch):
                color = esa_colors[i % len(esa_colors)]
                offset = i * offset_step
                self.p_esa.plot(esa_t_e, esa_v[i, :] + offset, pen=pg.mkPen(color, width=1), name=f'ESA{i}')
            
        # --- SENSOR Plot (AcclX, Y, Z) ---
        sen_t, sen_v, labels = data['sen']
        colors = [(255,100,100), (100,255,100), (100,100,255)] # R G B
        # Plot only max first 3 dimensions assuming IMU Accl mapping
        for i in range(min(3, sen_v.shape[0])):
             self.p_sen.plot(sen_t, sen_v[i, :], pen=colors[i], name=f'Accl{i}')
             
        # Add State V-Lines on all plots
        for p in [self.p_raw, self.p_lfp_spec, self.p_band, self.p_event, self.p_raster, self.p_esa, self.p_sen]:
            self._draw_state_boundaries(p, t_start_ts, states)

    def run_decoding(self):
        self.lbl_verify.setText("Decoding requires Verifier execution. (Placeholder update)")
        
if __name__ == '__main__':
    from data_processing import DataProcessor
    from verification import VerificationAnalyzer
    app = QApplication(sys.argv)
    dp = DataProcessor()
    verifier = VerificationAnalyzer(dp)
    window = MainWindow(dp, verifier)
    window.show()
    sys.exit(app.exec())
