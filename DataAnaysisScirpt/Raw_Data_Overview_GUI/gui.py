import json
import os
import sys
import traceback
from datetime import datetime

import pyqtgraph as pg
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from data_index import (
    TIMEZONE,
    TIMEZONE_NAME,
    build_dataset_index,
    build_slice_export,
    epoch_ms_to_iso,
    load_cached_dataset_index,
    load_saved_analysis_file,
)


pg.setConfigOptions(antialias=False)

MAX_PROTOCOL_LABELS = 120
MAX_EDF_TEXT_LABELS_PER_TRACK = 80


class ShanghaiDateAxisItem(pg.AxisItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timezone = TIMEZONE

    def tickStrings(self, values, scale, spacing):
        labels = []
        for value in values:
            dt = datetime.fromtimestamp(float(value), tz=self.timezone)
            if spacing >= 86400:
                labels.append(dt.strftime("%m-%d"))
            elif spacing >= 3600:
                labels.append(dt.strftime("%m-%d %H:%M"))
            else:
                labels.append(dt.strftime("%H:%M:%S"))
        return labels


class IndexWorker(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    progress = pyqtSignal(int, str)
    finished = pyqtSignal()

    def __init__(self, root_path):
        super().__init__()
        self.root_path = root_path

    @pyqtSlot()
    def run(self):
        try:
            self.progress.emit(0, "Starting scan...")

            def _progress(value, message):
                self.progress.emit(max(0, min(100, int(value))), str(message))

            index = build_dataset_index(
                self.root_path,
                use_cache=True,
                progress_callback=_progress,
            )
        except Exception:
            self.error.emit(traceback.format_exc())
        else:
            self.result.emit(index)
        finally:
            self.finished.emit()


class RawDataOverviewWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Raw Data Overview")
        self.resize(1550, 950)

        self.dataset_index = None
        self.source_root = ""
        self.slices = []
        self.worker_thread = None
        self.worker = None

        root = QWidget()
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)

        controls = self._build_controls()
        controls.setFixedWidth(360)
        layout.addWidget(controls)

        self.plot_panel = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_panel, stretch=1)
        self._build_plots()

    def _build_controls(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)

        source_group = QGroupBox("Dataset")
        source_layout = QVBoxLayout(source_group)
        self.lbl_root = QLabel("No BW/GUIBW folder selected")
        self.lbl_root.setWordWrap(True)
        self.btn_select_root = QPushButton("Select BW/GUIBW Folder")
        self.btn_select_root.clicked.connect(self.select_root)
        self.btn_scan = QPushButton("Scan Dataset")
        self.btn_scan.clicked.connect(self.scan_dataset)
        self.btn_load_analysis = QPushButton("Load Saved Analysis JSON")
        self.btn_load_analysis.clicked.connect(self.load_saved_analysis)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.lbl_progress = QLabel("Idle")
        self.lbl_progress.setWordWrap(True)
        source_layout.addWidget(self.lbl_root)
        source_layout.addWidget(self.btn_select_root)
        source_layout.addWidget(self.btn_scan)
        source_layout.addWidget(self.btn_load_analysis)
        source_layout.addWidget(self.progress_bar)
        source_layout.addWidget(self.lbl_progress)
        layout.addWidget(source_group)

        settings_group = QGroupBox("Coverage")
        settings_layout = QFormLayout(settings_group)
        self.lbl_missing_metric = QLabel("Per EDF total missing fraction from named Ch0/RawData channel (<= -1000).")
        self.lbl_missing_metric.setWordWrap(True)
        settings_layout.addRow("Missing metric:", self.lbl_missing_metric)
        layout.addWidget(settings_group)

        slice_group = QGroupBox("Slices")
        slice_layout = QVBoxLayout(slice_group)
        self.lbl_region = QLabel("Drag the region on the behavior plot, then add a slice.")
        self.lbl_region.setWordWrap(True)
        self.btn_add_slice = QPushButton("Add Slice")
        self.btn_add_slice.clicked.connect(self.add_slice)
        self.btn_delete_slice = QPushButton("Delete Selected Slice")
        self.btn_delete_slice.clicked.connect(self.delete_selected_slice)
        self.btn_clear_slices = QPushButton("Clear Slices")
        self.btn_clear_slices.clicked.connect(self.clear_slices)
        self.btn_save_slices = QPushButton("Save Slices JSON")
        self.btn_save_slices.clicked.connect(self.save_slices)
        self.slice_list = QListWidget()
        slice_layout.addWidget(self.lbl_region)
        slice_layout.addWidget(self.btn_add_slice)
        slice_layout.addWidget(self.btn_delete_slice)
        slice_layout.addWidget(self.btn_clear_slices)
        slice_layout.addWidget(self.btn_save_slices)
        slice_layout.addWidget(self.slice_list, stretch=1)
        layout.addWidget(slice_group, stretch=1)

        log_group = QGroupBox("Log")
        log_layout = QVBoxLayout(log_group)
        self.log_console = QTextEdit()
        self.log_console.setReadOnly(True)
        log_layout.addWidget(self.log_console)
        layout.addWidget(log_group, stretch=1)

        return panel

    def _build_plots(self):
        self.p_behavior = self._add_plot("Behavior: protocol, performance, trials")
        self.region = pg.LinearRegionItem([0, 1], movable=True, brush=(60, 120, 255, 45))
        self.region.setZValue(20)
        self.p_behavior.addItem(self.region)
        self.region.sigRegionChanged.connect(self.update_region_label)
        self.plot_panel.nextRow()

        self.p_mode0 = self._add_plot("Mode0 LFP coverage")
        self.p_mode0.setXLink(self.p_behavior)
        self.plot_panel.nextRow()

        self.p_mode3_lfp = self._add_plot("Mode3 LFP&ESA coverage")
        self.p_mode3_lfp.setXLink(self.p_behavior)
        self.plot_panel.nextRow()

        self.p_mode3_raw = self._add_plot("Mode3 raw coverage")
        self.p_mode3_raw.setXLink(self.p_behavior)
        self.plot_panel.nextRow()

        self.p_video = self._add_plot("Video coverage")
        self.p_video.setXLink(self.p_behavior)

    def _add_plot(self, title):
        plot = self.plot_panel.addPlot(
            title=title,
            axisItems={"bottom": ShanghaiDateAxisItem(orientation="bottom")},
        )
        plot.showGrid(x=True, y=True, alpha=0.25)
        plot.setMenuEnabled(True)
        plot.setMouseEnabled(x=True, y=False)
        return plot

    def select_root(self):
        path = QFileDialog.getExistingDirectory(self, "Select BW or GUIBW folder")
        if not path:
            return
        self.source_root = path
        self.lbl_root.setText(path)
        self.progress_bar.setValue(0)
        self.lbl_progress.setText("Checking saved analysis...")
        QApplication.processEvents()
        try:
            cached, signature, loaded_from = load_cached_dataset_index(path)
        except Exception as exc:
            self.log_console.append(f"<span style='color:orange;'>[cache]</span> cache check failed: {exc}")
            self.lbl_progress.setText("No saved analysis loaded.")
            return
        counts = signature.get("counts", {}) if signature else {}
        self.log_console.append(
            "[cache] source counts "
            f"Trial.txt={counts.get('trial', 0)}, EDF={counts.get('edf', 0)}, Video={counts.get('video', 0)}"
        )
        if cached is None:
            self.lbl_progress.setText("No matching saved analysis. Click Scan Dataset.")
            return
        self.dataset_index = cached
        self.log_console.append(f"[cache] loaded saved analysis: {loaded_from}")
        for line in cached.get("logs", []):
            if "file-count match" in line or "Loaded saved analysis" in line:
                self.log_console.append(f"<span style='color:orange;'>[cache]</span> {line}")
        self.draw_index(cached)
        self.progress_bar.setValue(100)
        self.lbl_progress.setText("Loaded saved analysis.")

    def load_saved_analysis(self):
        start_dir = self.source_root if self.source_root and os.path.isdir(self.source_root) else os.path.expanduser("~")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Saved Raw Data Overview Analysis",
            start_dir,
            "Raw overview cache (index_cache.json *.json);;JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            index = load_saved_analysis_file(path, selected_path=self.source_root or None)
        except Exception as exc:
            self.log_console.append(f"<span style='color:red;'>[cache]</span> failed to load saved analysis: {exc}")
            QMessageBox.critical(self, "Load Saved Analysis Failed", str(exc))
            return

        self.dataset_index = index
        loaded_source = (
            index.get("source_root")
            or (index.get("layout") or {}).get("source_root")
            or self.source_root
        )
        if loaded_source:
            self.source_root = str(loaded_source)
            self.lbl_root.setText(str(loaded_source))
        self.slices = []
        self.slice_list.clear()
        self.draw_index(index)
        self.progress_bar.setValue(100)
        self.lbl_progress.setText("Loaded saved analysis JSON.")
        self.log_console.append(f"[cache] manually loaded saved analysis: {path}")
        for line in index.get("logs", []):
            if "Manually loaded saved analysis" in line:
                self.log_console.append(f"<span style='color:orange;'>[cache]</span> {line}")

    def scan_dataset(self):
        if not self.source_root:
            QMessageBox.warning(self, "Missing Dataset", "Please select a BW or GUIBW folder first.")
            return
        self.btn_scan.setEnabled(False)
        self.btn_select_root.setEnabled(False)
        self.progress_bar.setValue(0)
        self.lbl_progress.setText("Starting scan...")
        self.log_console.append(f"[scan] {self.source_root}")
        self.log_console.append("[coverage] per EDF total missing fraction from named Ch0/RawData channel")
        QApplication.processEvents()
        self._start_worker(self.source_root)

    def _start_worker(self, root_path):
        self.worker_thread = QThread()
        self.worker = IndexWorker(root_path)
        self.worker.moveToThread(self.worker_thread)
        self.worker_thread.started.connect(self.worker.run)
        self.worker.result.connect(self.on_index_ready)
        self.worker.error.connect(self.on_index_error)
        self.worker.progress.connect(self.on_scan_progress)
        self.worker.finished.connect(self.worker_thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.worker_thread.finished.connect(self.worker_thread.deleteLater)
        self.worker_thread.finished.connect(self.on_scan_finished)
        self.worker_thread.start()

    def on_scan_progress(self, value, message):
        self.progress_bar.setValue(max(0, min(100, int(value))))
        self.lbl_progress.setText(message)
        if value == 0 or value == 100 or value % 10 == 0:
            self.log_console.append(f"[progress {value}%] {message}")

    def on_index_error(self, message):
        self.log_console.append(f"<span style='color:red;'>[error]</span><pre>{message}</pre>")
        QMessageBox.critical(self, "Scan Failed", message.splitlines()[-1] if message else "Unknown error")

    def on_index_ready(self, index):
        self.dataset_index = index
        self.log_console.append(
            f"[ready] {index.get('bw_id')} | trials={len(index.get('behavior_trials', []))} | "
            f"edf={len(index.get('edf_records', []))} | video={len(index.get('video_records', []))}"
        )
        for line in index.get("logs", []):
            self.log_console.append(f"<span style='color:orange;'>[warn]</span> {line}")
        self.draw_index(index)
        self.progress_bar.setValue(100)
        self.lbl_progress.setText("Scan complete.")

    def on_scan_finished(self):
        self.btn_scan.setEnabled(True)
        self.btn_select_root.setEnabled(True)

    def draw_index(self, index):
        self._clear_plots()
        self._draw_behavior(index.get("behavior_trials", []))
        self._draw_edf_track(self.p_mode0, index.get("edf_records", []), "mode0_lfp", (70, 140, 255))
        self._draw_edf_track(self.p_mode3_lfp, index.get("edf_records", []), "mode3_lfp_esa", (80, 210, 150))
        self._draw_edf_track(self.p_mode3_raw, index.get("edf_records", []), "mode3_raw", (255, 170, 80))
        self._draw_video_track(index.get("video_records", []))

        start_ms = index.get("global_start_epoch_ms")
        end_ms = index.get("global_end_epoch_ms")
        if start_ms is not None and end_ms is not None and end_ms >= start_ms:
            start_s = start_ms / 1000.0
            end_s = end_ms / 1000.0
            if end_s <= start_s:
                end_s = start_s + 60.0
            self.p_behavior.setXRange(start_s, end_s, padding=0.02)
            width = max(60.0, (end_s - start_s) * 0.03)
            self.region.setRegion([start_s, min(end_s, start_s + width)])
            self.update_region_label()

    def _clear_plots(self):
        for plot in [self.p_behavior, self.p_mode0, self.p_mode3_lfp, self.p_mode3_raw, self.p_video]:
            plot.clear()
        self.p_behavior.addItem(self.region)

    def _draw_behavior(self, trials):
        self.p_behavior.setLabel("left", "Performance / trials")
        if not trials:
            self.p_behavior.setYRange(0, 100)
            return

        perf_x = []
        perf_y = []
        for trial in trials:
            if trial.get("performance_pct") is not None:
                perf_x.append(trial["time_epoch_ms"] / 1000.0)
                perf_y.append(float(trial["performance_pct"]))
        if perf_x:
            item = self.p_behavior.plot(perf_x, perf_y, pen=pg.mkPen(120, 220, 120, width=2), name="Performance %")
            _optimize_plot_item(item, downsample=True)

        groups = {}
        for trial in trials:
            key = (trial.get("outcome_label", "unknown"), trial.get("trial_choice", "unknown"))
            groups.setdefault(key, {"x": [], "y": []})
            groups[key]["x"].append(trial["time_epoch_ms"] / 1000.0)
            groups[key]["y"].append(_choice_y(trial.get("trial_choice")))

        for (outcome, choice), points in sorted(groups.items()):
            x_vals, y_vals = _tick_segments(points["x"], _choice_y(choice), half_height=2.4)
            item = self.p_behavior.plot(
                x_vals,
                y_vals,
                pen=pg.mkPen(color=_outcome_color(outcome), width=1.2),
                name=f"{choice}/{outcome}",
            )
            _optimize_plot_item(item, downsample=False)

        protocol_changes = []
        last_protocol = None
        for trial in trials:
            protocol = trial.get("protocol_index")
            if protocol == last_protocol:
                continue
            x = trial["time_epoch_ms"] / 1000.0
            protocol_changes.append((x, protocol))
            last_protocol = protocol

        if protocol_changes:
            x_vals, y_vals = _vertical_segments([item[0] for item in protocol_changes], 0.0, 105.0)
            item = self.p_behavior.plot(x_vals, y_vals, pen=pg.mkPen(180, 180, 255, 140))
            _optimize_plot_item(item, downsample=False)

        label_y = 98
        for x, protocol in _sample_evenly(protocol_changes, MAX_PROTOCOL_LABELS):
            label = pg.TextItem(f"P{protocol}", color=(210, 210, 255), anchor=(0, 1))
            label.setPos(x, label_y)
            self.p_behavior.addItem(label)

        self.p_behavior.setYRange(0, 105)

    def _draw_edf_track(self, plot, records, rec_type, color):
        plot.setLabel("left", "file valid fraction")
        selected = [record for record in records if record.get("type") == rec_type]
        plot.setYRange(-0.05, 1.15)
        buckets = {}
        for record in selected:
            start_s = record["start_epoch_ms"] / 1000.0
            end_s = record["end_epoch_ms"] / 1000.0
            valid_fraction = float(record.get("valid_fraction", 0.0))
            missing_fraction = float(record.get("missing_fraction", 1.0 - valid_fraction))
            bucket = _missing_bucket(missing_fraction)
            buckets.setdefault(bucket, []).append((start_s, end_s, valid_fraction))

        for bucket, segments in sorted(buckets.items()):
            line_color = _missing_bucket_color(bucket, color)
            x_vals, y_vals = _horizontal_segments(segments)
            item = plot.plot(x_vals, y_vals, pen=pg.mkPen(color=line_color, width=8))
            _optimize_plot_item(item, downsample=False)

        for record in _sample_evenly(selected, MAX_EDF_TEXT_LABELS_PER_TRACK):
            start_s = record["start_epoch_ms"] / 1000.0
            end_s = record["end_epoch_ms"] / 1000.0
            valid_fraction = float(record.get("valid_fraction", 0.0))
            missing_fraction = float(record.get("missing_fraction", 1.0 - valid_fraction))
            line_color = _missing_color(missing_fraction, color)
            midpoint = start_s + (end_s - start_s) * 0.5
            label = pg.TextItem(f"{missing_fraction * 100:.1f}% missing", color=line_color[:3], anchor=(0.5, 1.0))
            label.setPos(midpoint, min(1.08, valid_fraction + 0.06))
            plot.addItem(label)

    def _draw_video_track(self, records):
        self.p_video.setLabel("left", "video")
        self.p_video.setYRange(0, 1.4)
        known_segments = []
        unknown_x = []
        for record in records:
            start_s = record["start_epoch_ms"] / 1000.0
            if record.get("duration_known"):
                end_s = record["end_epoch_ms"] / 1000.0
                known_segments.append((start_s, end_s, 1.0))
            else:
                unknown_x.append(start_s)
        if known_segments:
            x_vals, y_vals = _horizontal_segments(known_segments)
            item = self.p_video.plot(x_vals, y_vals, pen=pg.mkPen(color=(210, 130, 255, 180), width=8))
            _optimize_plot_item(item, downsample=False)
        if unknown_x:
            x_vals, y_vals = _vertical_segments(unknown_x, 0.25, 1.25)
            item = self.p_video.plot(x_vals, y_vals, pen=pg.mkPen(color=(210, 130, 255, 220), width=2))
            _optimize_plot_item(item, downsample=False)

    def update_region_label(self):
        start_s, end_s = self.region.getRegion()
        start_ms = int(round(min(start_s, end_s) * 1000.0))
        end_ms = int(round(max(start_s, end_s) * 1000.0))
        duration_s = (end_ms - start_ms) / 1000.0
        self.lbl_region.setText(
            f"{epoch_ms_to_iso(start_ms)}\n{epoch_ms_to_iso(end_ms)}\nDuration: {duration_s:.1f} s"
        )

    def add_slice(self):
        if self.dataset_index is None:
            QMessageBox.warning(self, "No Dataset", "Scan a dataset before adding slices.")
            return
        start_s, end_s = self.region.getRegion()
        start_ms = int(round(min(start_s, end_s) * 1000.0))
        end_ms = int(round(max(start_s, end_s) * 1000.0))
        if end_ms <= start_ms:
            QMessageBox.warning(self, "Invalid Slice", "Slice duration must be greater than zero.")
            return
        slice_id = len(self.slices) + 1
        item = {
            "id": slice_id,
            "label": f"slice_{slice_id:03d}",
            "start_epoch_ms": start_ms,
            "end_epoch_ms": end_ms,
        }
        self.slices.append(item)
        self._refresh_slice_list()

    def delete_selected_slice(self):
        row = self.slice_list.currentRow()
        if row < 0 or row >= len(self.slices):
            return
        del self.slices[row]
        for idx, item in enumerate(self.slices, start=1):
            item["id"] = idx
            item["label"] = f"slice_{idx:03d}"
        self._refresh_slice_list()

    def clear_slices(self):
        self.slices = []
        self._refresh_slice_list()

    def _refresh_slice_list(self):
        self.slice_list.clear()
        export_preview = build_slice_export(self.dataset_index or {}, self.slices)
        self.slices = [
            {
                "id": item["id"],
                "label": item["label"],
                "start_epoch_ms": item["start_epoch_ms"],
                "end_epoch_ms": item["end_epoch_ms"],
            }
            for item in export_preview["slices"]
        ]
        for item in export_preview["slices"]:
            text = (
                f"{item['label']} | {item['start_iso']} -> {item['end_iso']} "
                f"({item['duration_s']:.1f} s)"
            )
            list_item = QListWidgetItem(text)
            list_item.setData(Qt.ItemDataRole.UserRole, item)
            self.slice_list.addItem(list_item)

    def save_slices(self):
        if self.dataset_index is None:
            QMessageBox.warning(self, "No Dataset", "Scan a dataset before saving slices.")
            return
        if not self.slices:
            QMessageBox.warning(self, "No Slices", "Add at least one slice before saving.")
            return
        default_name = f"{self.dataset_index.get('bw_id', 'BW')}_raw_data_slices.json"
        default_dir = self.dataset_index.get("source_root") or os.getcwd()
        default_path = os.path.join(default_dir, default_name)
        out_path, _ = QFileDialog.getSaveFileName(self, "Save slices JSON", default_path, "JSON Files (*.json)")
        if not out_path:
            return
        payload = build_slice_export(self.dataset_index, self.slices)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        self.log_console.append(f"[slices] saved {len(payload['slices'])} slices to {out_path}")


def _choice_y(choice):
    return {"left": 18.0, "right": 32.0, "middle": 25.0}.get(choice, 8.0)


def _outcome_color(outcome):
    return {
        "correct": (70, 210, 110, 210),
        "error": (245, 80, 80, 220),
        "no_response": (150, 150, 150, 190),
        "earlylick_or_other": (240, 170, 70, 210),
        "other": (240, 170, 70, 210),
    }.get(outcome, (200, 200, 200, 180))


def _horizontal_segments(segments):
    x_vals = []
    y_vals = []
    for start_s, end_s, y in segments:
        x_vals.extend([start_s, end_s, float("nan")])
        y_vals.extend([y, y, float("nan")])
    return x_vals, y_vals


def _vertical_segments(xs, y_min, y_max):
    x_vals = []
    y_vals = []
    for x in xs:
        x_vals.extend([x, x, float("nan")])
        y_vals.extend([y_min, y_max, float("nan")])
    return x_vals, y_vals


def _tick_segments(xs, y_center, half_height=2.0):
    x_vals = []
    y_vals = []
    y_min = y_center - half_height
    y_max = y_center + half_height
    for x in xs:
        x_vals.extend([x, x, float("nan")])
        y_vals.extend([y_min, y_max, float("nan")])
    return x_vals, y_vals


def _sample_evenly(items, max_items):
    items = list(items)
    if len(items) <= max_items:
        return items
    if max_items <= 0:
        return []
    if max_items == 1:
        return [items[0]]
    last_idx = len(items) - 1
    selected = []
    used = set()
    for idx in range(max_items):
        source_idx = int(round(idx * last_idx / float(max_items - 1)))
        if source_idx not in used:
            selected.append(items[source_idx])
            used.add(source_idx)
    return selected


def _optimize_plot_item(item, downsample=False):
    if hasattr(item, "setClipToView"):
        item.setClipToView(True)
    if downsample and hasattr(item, "setDownsampling"):
        try:
            item.setDownsampling(auto=True, method="subsample")
        except TypeError:
            item.setDownsampling(auto=True)


def _missing_bucket(missing_fraction):
    fraction = max(0.0, min(1.0, float(missing_fraction)))
    if fraction <= 0.01:
        return "good"
    if fraction <= 0.10:
        return "warn"
    return "bad"


def _missing_bucket_color(bucket, base_color):
    if bucket == "good":
        return (base_color[0], base_color[1], base_color[2], 230)
    if bucket == "warn":
        return (245, 190, 60, 235)
    return (245, 80, 80, 235)


def _missing_color(missing_fraction, base_color):
    return _missing_bucket_color(_missing_bucket(missing_fraction), base_color)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = RawDataOverviewWindow()
    window.show()
    sys.exit(app.exec())
