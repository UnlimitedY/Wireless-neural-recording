import sys
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QDialog, QVBoxLayout, QHBoxLayout,
    QPushButton, QComboBox, QLabel, QWidget, QTabWidget, QGroupBox,
    QGridLayout, QSizePolicy, QCheckBox, QFrame, QFileDialog, 
    QSpinBox, QDoubleSpinBox, QMessageBox, QLineEdit, QSplitter,
    QProgressBar, QTextBrowser
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QDateTime
from PyQt6.QtGui import QPalette, QColor, QFont, QIcon, QImage, QPixmap
import cv2
import os
try:
    from ..hardware.camera_module import CameraModule, get_available_cameras
    from ..monitoring.battery_history_manager import BatteryHistoryManager
    from .optimized_habits_panel import OptimizedHabitsPanel
    from ..services.health_metrics import decode_bq25176_stat
    from ..support.path_utils import get_default_save_path, get_data_directory, get_mouse_data_directory
    from ..widgets.shared_ui_sections import CONTROL_SURFACE_STYLESHEET, format_log_html
except ImportError:
    from hardware.camera_module import CameraModule, get_available_cameras
    from monitoring.battery_history_manager import BatteryHistoryManager
    from recorder_app.optimized_habits_panel import OptimizedHabitsPanel
    from services.health_metrics import decode_bq25176_stat
    from support.path_utils import get_default_save_path, get_data_directory, get_mouse_data_directory
    from widgets.shared_ui_sections import CONTROL_SURFACE_STYLESHEET, format_log_html
import pyqtgraph as pg
import time
import numpy as np
import threading
from datetime import datetime

class TimelineWidget(QWidget):
    """
    Widget to display 24-hour timeline of recording modes and battery level.
    """
    anomalyClicked = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        
        # Plot Widget
        self.plot_widget = pg.PlotWidget()
        self.layout.addWidget(self.plot_widget)
        
        # Set background to white
        self.plot_widget.setBackground('w')
        
        # Style axes for white background
        styles = {'color': 'k', 'font-size': '10pt'}
        self.plot_widget.setLabel('left', 'Battery', units='%', **styles)
        self.plot_widget.setLabel('bottom', 'Time', **styles)
        # self.plot_widget.setTitle("24-Hour Timeline: Battery & Recording Modes", color='k', size='12pt')
        
        # Axis pens
        self.plot_widget.getAxis('bottom').setPen(pg.mkPen('k'))
        self.plot_widget.getAxis('bottom').setTextPen(pg.mkPen('k'))
        self.plot_widget.getAxis('left').setPen(pg.mkPen('k'))
        self.plot_widget.getAxis('left').setTextPen(pg.mkPen('k'))

        self.plot_widget.setYRange(0, 105)
        
        # Use DateAxisItem for X axis
        self.date_axis = pg.DateAxisItem(orientation='bottom')
        self.date_axis.setPen(pg.mkPen('k'))
        self.date_axis.setTextPen(pg.mkPen('k'))
        self.plot_widget.setAxisItems({'bottom': self.date_axis})
        self.plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self.legend = self.plot_widget.addLegend()
        self.legend.setBrush(pg.mkBrush(255, 255, 255, 200))
        self.legend.setLabelTextColor('k')
        
        # Data storage
        # Battery: list of (timestamp, value)
        self.battery_timestamps = []
        self.battery_values = []
        # Battery curve will be updated dynamically based on level
        self.battery_curve = self.plot_widget.plot(pen=pg.mkPen(color='k', width=2), name='Battery')
        
        # RF Power: list of (timestamp, value)
        self.rf_timestamps = []
        self.rf_values = []
        # Magenta for RF Power
        self.rf_curve = self.plot_widget.plot(pen=pg.mkPen(color='k', width=1, style=Qt.PenStyle.DashLine), name='RF Power')

        self.current_viewbox = pg.ViewBox()
        self.plot_widget.showAxis('right')
        self.plot_widget.scene().addItem(self.current_viewbox)
        right_axis = self.plot_widget.getAxis('right')
        right_axis.setPen(pg.mkPen('#7C2D12'))
        right_axis.setTextPen(pg.mkPen('#7C2D12'))
        right_axis.setLabel('Est. current', units='mA', color='#7C2D12')
        right_axis.linkToView(self.current_viewbox)
        self.current_viewbox.setXLink(self.plot_widget)
        self.plot_widget.getViewBox().sigResized.connect(self._update_current_view_geometry)
        self.current_timestamps = []
        self.current_values = []
        self.discharge_current_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color='#B91C1C', width=2),
            name='Use mA',
        )
        self.charge_current_curve = pg.PlotCurveItem(
            pen=pg.mkPen(color='#2563EB', width=2),
            name='Charge mA',
        )
        self.current_viewbox.addItem(self.discharge_current_curve)
        self.current_viewbox.addItem(self.charge_current_curve)
        try:
            self.legend.addItem(self.discharge_current_curve, 'Use mA')
            self.legend.addItem(self.charge_current_curve, 'Charge mA')
        except Exception:
            pass
        self.anomaly_scatter = pg.ScatterPlotItem(
            size=10,
            pen=pg.mkPen(color='#7F1D1D', width=1.5),
            brush=pg.mkBrush('#EF4444'),
        )
        self.anomaly_scatter.sigClicked.connect(self._on_anomaly_scatter_clicked)
        self.plot_widget.addItem(self.anomaly_scatter)

        # Trial-trigger pause overlay regions (battery policy gate)
        self.pause_regions = []
        self.current_pause_region = None
        self.current_pause_start = None
        self.current_pause_active = False
        # Legend proxy for pause intervals.
        self.pause_legend_curve = self.plot_widget.plot(
            [], [], pen=pg.mkPen(color=(220, 20, 60), width=3), name='Trial Paused'
        )
        self.anomaly_legend_curve = self.plot_widget.plot(
            [], [],
            pen=None,
            symbol='o',
            symbolPen=pg.mkPen(color='#7F1D1D', width=1.5),
            symbolBrush=pg.mkBrush('#EF4444'),
            symbolSize=8,
            name='Battery Anomaly',
        )
        
        # Modes: list of LinearRegionItem
        self.mode_regions = [] 
        self.current_mode_region = None
        self.current_mode_start = None
        self.current_mode = None
        
        # Colors for different modes (Increased opacity for better visibility)
        self.mode_colors = {
            "Idle": (220, 220, 220, 255),         # Solid Light Grey
            "Mode0 LFP+MAND+Raw": (100, 149, 237, 255),
            "16 channels LFP": (100, 149, 237, 255),   # Cornflower Blue
            "single channel Spike": (255, 99, 71, 255), # Tomato Red
            "16 channels Spike": (60, 179, 113, 255),  # Medium Sea Green
            "ESA&MUA": (255, 165, 0, 255)          # Orange
        }
        self.rebuild_from_history([], [])

    def _update_current_view_geometry(self):
        try:
            viewbox = self.plot_widget.getViewBox()
            self.current_viewbox.setGeometry(viewbox.sceneBoundingRect())
            self.current_viewbox.linkedViewChanged(viewbox, self.current_viewbox.XAxis)
        except Exception:
            pass

    @staticmethod
    def _record_battery_capacity_mAh(record):
        try:
            value = float(record.get("capacity_mAh", record.get("battery_capacity_mAh", 24.0)) or 24.0)
        except Exception:
            value = 24.0
        return value if value > 0.0 else 24.0

    @classmethod
    def _infer_current_from_points(cls, previous_record, current_record):
        try:
            previous_ts = float(previous_record.get("timestamp_epoch", 0.0) or 0.0)
            current_ts = float(current_record.get("timestamp_epoch", 0.0) or 0.0)
            previous_rsoc = float(previous_record.get("rsoc", 0.0) or 0.0)
            current_rsoc = float(current_record.get("rsoc", 0.0) or 0.0)
        except Exception:
            return np.nan
        dt_hours = (current_ts - previous_ts) / 3600.0
        if dt_hours <= 0.0:
            return np.nan
        capacity_mAh = cls._record_battery_capacity_mAh(current_record)
        # Positive means net battery discharge/load. Negative means net charging.
        return -((current_rsoc - previous_rsoc) / 100.0) * capacity_mAh / dt_hours

    @classmethod
    def _infer_current_series(cls, records):
        values = []
        previous = None
        for record in records:
            if previous is None:
                values.append(np.nan)
            else:
                values.append(cls._infer_current_from_points(previous, record))
            previous = record
        return values

    @staticmethod
    def _paired_curve_arrays(x_values, y_values):
        try:
            x_arr = np.asarray(x_values, dtype=np.float64).reshape(-1)
            y_arr = np.asarray(y_values, dtype=np.float64).reshape(-1)
        except Exception:
            return np.asarray([], dtype=np.float64), np.asarray([], dtype=np.float64)
        length = min(int(x_arr.size), int(y_arr.size))
        if length <= 0:
            return x_arr[:0], y_arr[:0]
        return x_arr[:length], y_arr[:length]

    def _set_current_curve_data(self):
        timestamps, values = self._paired_curve_arrays(self.current_timestamps, self.current_values)
        if len(self.current_timestamps) != len(self.current_values):
            self.current_timestamps = timestamps.tolist()
            self.current_values = values.tolist()
        if len(values) == 0 or len(timestamps) == 0:
            self.discharge_current_curve.setData([], [])
            self.charge_current_curve.setData([], [])
            return
        discharge = np.where(values >= 0.0, values, np.nan)
        charge = np.where(values < 0.0, values, np.nan)
        self.discharge_current_curve.setData(timestamps, discharge)
        self.charge_current_curve.setData(timestamps, charge)
        finite_values = values[np.isfinite(values)]
        if finite_values.size > 0:
            max_abs = max(1.0, float(np.nanmax(np.abs(finite_values))))
            self.current_viewbox.setYRange(-max_abs * 1.15, max_abs * 1.15, padding=0.02)
        self._update_current_view_geometry()

    def get_battery_color(self, level):
        """
        Return color based on battery level (5 levels, 20% intervals)
        Ensure high contrast with mode colors.
        """
        if level > 80:
            return '#006400'  # Dark Green (High)
        elif level > 60:
            return '#228B22'  # Forest Green
        elif level > 40:
            return '#DAA520'  # Goldenrod (Medium)
        elif level > 20:
            return '#D2691E'  # Chocolate
        else:
            return '#8B0000'  # Dark Red (Low)

    def _create_region_item(self, start_ts, end_ts, brush_color, z_value):
        region = pg.LinearRegionItem(values=[start_ts, end_ts], brush=brush_color, movable=False)
        for line in region.lines:
            line.setPen(pg.mkPen(None))
        self.plot_widget.addItem(region)
        region.setZValue(z_value)
        return region

    def _clear_regions(self):
        for region in list(self.mode_regions):
            try:
                self.plot_widget.removeItem(region)
            except Exception:
                pass
        for region in list(self.pause_regions):
            try:
                self.plot_widget.removeItem(region)
            except Exception:
                pass
        self.mode_regions = []
        self.pause_regions = []
        self.current_mode_region = None
        self.current_mode_start = None
        self.current_mode = None
        self.current_pause_region = None
        self.current_pause_start = None
        self.current_pause_active = False

    def _apply_mode_regions(self, records):
        if not records:
            return
        current_mode = None
        region_start = None
        last_region = None
        for record in records:
            timestamp = float(record["timestamp_epoch"])
            mode = str(record.get("mode", "Idle") or "Idle")
            if current_mode is None:
                current_mode = mode
                region_start = timestamp
                continue
            if mode != current_mode:
                color = self.mode_colors.get(current_mode, (200, 200, 200, 255))
                last_region = self._create_region_item(region_start, timestamp, color, -10)
                self.mode_regions.append(last_region)
                current_mode = mode
                region_start = timestamp
        color = self.mode_colors.get(current_mode, (200, 200, 200, 255))
        last_region = self._create_region_item(region_start, float(records[-1]["timestamp_epoch"]), color, -10)
        self.mode_regions.append(last_region)
        self.current_mode = current_mode
        self.current_mode_start = region_start
        self.current_mode_region = last_region

    def _apply_pause_regions(self, records):
        if not records:
            return
        pause_active = None
        region_start = None
        last_region = None
        for record in records:
            timestamp = float(record["timestamp_epoch"])
            current_pause = bool(record.get("trial_paused", False))
            if pause_active is None:
                pause_active = current_pause
                region_start = timestamp
                continue
            if current_pause != pause_active:
                if pause_active:
                    last_region = self._create_region_item(
                        region_start, timestamp, (220, 20, 60, 90), -5
                    )
                    self.pause_regions.append(last_region)
                pause_active = current_pause
                region_start = timestamp
        if pause_active:
            last_region = self._create_region_item(
                region_start, float(records[-1]["timestamp_epoch"]), (220, 20, 60, 90), -5
            )
            self.pause_regions.append(last_region)
        self.current_pause_active = bool(pause_active)
        self.current_pause_start = region_start if pause_active else None
        self.current_pause_region = last_region if pause_active else None

    def _on_anomaly_scatter_clicked(self, *args):
        points = []
        if len(args) >= 2 and isinstance(args[1], (list, tuple)):
            points = list(args[1])
        elif len(args) >= 1 and isinstance(args[0], (list, tuple)):
            points = list(args[0])
        for point in points:
            try:
                payload = point.data()
            except Exception:
                payload = None
            if isinstance(payload, dict):
                self.anomalyClicked.emit(dict(payload))
                return

    def rebuild_from_history(self, records, anomalies):
        sorted_records = sorted(
            [dict(record) for record in (records or []) if "timestamp_epoch" in record],
            key=lambda item: float(item["timestamp_epoch"])
        )
        self._clear_regions()

        self.battery_timestamps = [float(record["timestamp_epoch"]) for record in sorted_records]
        self.battery_values = [float(record.get("rsoc", 0.0)) for record in sorted_records]
        self.rf_timestamps = list(self.battery_timestamps)
        self.rf_values = [100 if int(record.get("rf_status", 0) or 0) == 2 else 0 for record in sorted_records]
        self.current_timestamps = list(self.battery_timestamps)
        self.current_values = self._infer_current_series(sorted_records)

        battery_color = self.get_battery_color(self.battery_values[-1]) if self.battery_values else '#000000'
        battery_x, battery_y = self._paired_curve_arrays(self.battery_timestamps, self.battery_values)
        rf_x, rf_y = self._paired_curve_arrays(self.rf_timestamps, self.rf_values)
        self.battery_curve.setData(
            battery_x,
            battery_y,
            pen=pg.mkPen(color=battery_color, width=3),
        )
        self.rf_curve.setData(rf_x, rf_y)
        self._set_current_curve_data()

        self._apply_mode_regions(sorted_records)
        self._apply_pause_regions(sorted_records)

        spots = []
        for anomaly in anomalies or []:
            try:
                x_pos = float(anomaly.get("point_time"))
                y_pos = float(anomaly.get("point_y", np.nan))
            except Exception:
                continue
            if not np.isfinite(x_pos) or not np.isfinite(y_pos):
                continue
            spots.append({
                "pos": (x_pos, y_pos),
                "data": dict(anomaly),
                "brush": pg.mkBrush('#EF4444'),
                "pen": pg.mkPen(color='#7F1D1D', width=1.5),
                "size": 10,
            })
        self.anomaly_scatter.setData(spots=spots)

        now_ts = time.time()
        self.plot_widget.setXRange(now_ts - 24 * 3600, now_ts, padding=0.01)
        self.plot_widget.setYRange(0, 105, padding=0)

    def update_data(self, timestamp, battery_level, mode, rf_status, trial_paused=False, battery_capacity_mAh=24.0):
        """
        Update the timeline with new data.
        timestamp: unix timestamp (float)
        battery_level: percentage (0-100)
        mode: string (current recording mode)
        rf_status: int (1 for OFF, 2 for ON) -> mapped to 0 or 100
        trial_paused: bool, whether trial trigger is paused by battery policy
        """
        
        # Optimize updates: only update if data changed or enough time passed (e.g., 1 min)
        # However, for battery curve we want continuous points for plot. 
        # But we can limit the resolution if needed.
        # For now, let's keep adding points but ensure efficient memory management.

        # 1. Update Battery Data
        previous_record = None
        if self.battery_timestamps and self.battery_values:
            previous_record = {
                "timestamp_epoch": self.battery_timestamps[-1],
                "rsoc": self.battery_values[-1],
                "capacity_mAh": battery_capacity_mAh,
            }
        self.battery_timestamps.append(timestamp)
        self.battery_values.append(battery_level)
        
        # 2. Update RF Data
        self.rf_timestamps.append(timestamp)
        # Map RF status: 2 (ON) -> 100, 1 (OFF) -> 0, others -> 0
        rf_value = 100 if rf_status == 2 else 0
        self.rf_values.append(rf_value)

        self.current_timestamps.append(timestamp)
        current_record = {
            "timestamp_epoch": timestamp,
            "rsoc": battery_level,
            "capacity_mAh": battery_capacity_mAh,
        }
        inferred_current = (
            np.nan
            if previous_record is None
            else self._infer_current_from_points(previous_record, current_record)
        )
        self.current_values.append(inferred_current)
        
        # Remove old data (> 24 hours)
        cutoff_time = timestamp - 24 * 3600
        
        # Efficiently remove old battery data
        # Use bisect or simpler method if array is sorted (it is sorted by time)
        # Simple loop popping from front is O(N) but N is small (1 point per update)
        # However, pop(0) on list is O(N). If array grows large (e.g. 1s update -> 86400 points), this is slow.
        # Optimization: Use deque or just slice when it grows too large.
        # Or better: batch remove.
        
        # Check if we need to cleanup (e.g. every 100 updates)
        if len(self.battery_timestamps) > 1000 and self.battery_timestamps[0] < cutoff_time:
             # Find index to slice
             # Since timestamps are sorted, we can find the first index >= cutoff_time
             import bisect
             idx = bisect.bisect_left(self.battery_timestamps, cutoff_time)
             if idx > 0:
                 # Slice lists to remove old data
                 # This creates new list objects but releases old ones
                 self.battery_timestamps = self.battery_timestamps[idx:]
                 self.battery_values = self.battery_values[idx:]
                 
                 # Sync RF data (assuming sync updates)
                 if len(self.rf_timestamps) >= idx:
                     self.rf_timestamps = self.rf_timestamps[idx:]
                     self.rf_values = self.rf_values[idx:]
                 if len(self.current_timestamps) >= idx:
                     self.current_timestamps = self.current_timestamps[idx:]
                     self.current_values = self.current_values[idx:]
                 
                 # Force garbage collection occasionally if needed
                 # import gc
                 # gc.collect()
        
        # Update curves
        # Optimization: Downsample for display if too many points?
        # PyQtGraph handles large datasets relatively well, but 24h at 1s resolution is ~86k points.
        # It should be fine.
        
        # Update battery curve color based on current level
        battery_color = self.get_battery_color(battery_level)
        battery_x, battery_y = self._paired_curve_arrays(self.battery_timestamps, self.battery_values)
        self.battery_curve.setData(battery_x, battery_y, pen=pg.mkPen(color=battery_color, width=3))
        
        # Update RF curve
        rf_x, rf_y = self._paired_curve_arrays(self.rf_timestamps, self.rf_values)
        self.rf_curve.setData(rf_x, rf_y)
        self._set_current_curve_data()
        
        # 3. Update Mode Regions
        if self.current_mode != mode:
            # Finish previous mode region
            if self.current_mode_region:
                self.current_mode_region.setRegion([self.current_mode_start, timestamp])
                
            # Start new mode region
            self.current_mode = mode
            self.current_mode_start = timestamp
            
            # Use get() with default but ensure color is tuple
            color = self.mode_colors.get(mode, (200, 200, 200, 255))
            
            # LinearRegionItem for background color
            region = self._create_region_item(timestamp, timestamp, color, -10)
            self.current_mode_region = region
            self.mode_regions.append(region)
            
        else:
            # Extend current region
            if self.current_mode_region:
                self.current_mode_region.setRegion([self.current_mode_start, timestamp])
            elif mode: # First time initialization if mode is set
                 self.current_mode = mode
                 self.current_mode_start = timestamp
                 color = self.mode_colors.get(mode, (200, 200, 200, 255))
                 region = self._create_region_item(timestamp, timestamp, color, -10)
                 self.current_mode_region = region
                 self.mode_regions.append(region)

        # Cleanup old mode regions
        # Optimization: only check periodically or if many regions exist
        if len(self.mode_regions) > 10: 
            regions_to_remove = []
            # Check only the oldest few
            for i in range(min(len(self.mode_regions), 5)):
                region = self.mode_regions[i]
                r_start, r_end = region.getRegion()
                if r_end < cutoff_time:
                    self.plot_widget.removeItem(region)
                    regions_to_remove.append(region)
                elif r_start < cutoff_time:
                    # Truncate start
                    region.setRegion([cutoff_time, r_end])
            
            for region in regions_to_remove:
                self.mode_regions.remove(region)
            if region == self.current_mode_region:
                # This shouldn't happen for current region unless it's > 24 hours long and ends before cutoff? No.
                # If current region started > 24h ago, r_start < cutoff_time, so we truncate it.
                pass

        # 4. Update trial-pause overlay regions
        trial_paused = bool(trial_paused)
        if self.current_pause_active != trial_paused:
            if self.current_pause_active and self.current_pause_region:
                self.current_pause_region.setRegion([self.current_pause_start, timestamp])
            self.current_pause_active = trial_paused
            if trial_paused:
                self.current_pause_start = timestamp
                pause_region = self._create_region_item(timestamp, timestamp, (220, 20, 60, 90), -5)
                self.current_pause_region = pause_region
                self.pause_regions.append(pause_region)
            else:
                self.current_pause_start = None
                self.current_pause_region = None
        elif trial_paused and self.current_pause_region and self.current_pause_start is not None:
            self.current_pause_region.setRegion([self.current_pause_start, timestamp])

        # Cleanup old pause regions with 24h window.
        if len(self.pause_regions) > 10:
            pause_regions_to_remove = []
            for i in range(min(len(self.pause_regions), 5)):
                pause_region = self.pause_regions[i]
                p_start, p_end = pause_region.getRegion()
                if p_end < cutoff_time:
                    self.plot_widget.removeItem(pause_region)
                    pause_regions_to_remove.append(pause_region)
                elif p_start < cutoff_time:
                    pause_region.setRegion([cutoff_time, p_end])
            for pause_region in pause_regions_to_remove:
                self.pause_regions.remove(pause_region)

class SerialConnectionDialog(QDialog):
    connection_established = pyqtSignal(str) # Signal to emit when connection is made

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Serial port connection")
        self.setMinimumWidth(350)
        self.setMinimumHeight(180)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(15)

        # 标题
        title_label = QLabel("Please select a serial port")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_font = QFont()
        title_font.setPointSize(12)
        title_font.setBold(True)
        title_label.setFont(title_font)
        layout.addWidget(title_label)
        
        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

        # Serial port selection
        self.serial_ports_combobox = QComboBox()
        self.serial_ports_combobox.setMinimumHeight(30)
        self.refresh_serial_ports() # Initial population

        refresh_button = QPushButton("Read ports")
        refresh_button.setMinimumHeight(30)
        refresh_button.clicked.connect(self.refresh_serial_ports)

        serial_layout = QHBoxLayout()
        serial_layout.addWidget(QLabel("Serial ports:"))
        serial_layout.addWidget(self.serial_ports_combobox, 1)
        serial_layout.addWidget(refresh_button)
        layout.addLayout(serial_layout)

        # Connect/Disconnect buttons
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        self.connect_button = QPushButton("Connect")
        self.connect_button.setMinimumHeight(35)
        self.connect_button.clicked.connect(self.attempt_connection)
        
        self.disconnect_button = QPushButton("disconnect")
        self.disconnect_button.setMinimumHeight(35)
        self.disconnect_button.clicked.connect(self.attempt_disconnection)
        self.disconnect_button.setEnabled(False)

        button_layout.addWidget(self.connect_button)
        button_layout.addWidget(self.disconnect_button)
        layout.addLayout(button_layout)

        self.setLayout(layout)
        self.selected_port = None

    def refresh_serial_ports(self):
        self.serial_ports_combobox.clear()
        try:
            from serial.tools.list_ports import comports
            ports = [port.device for port in comports()]
            if not ports:
                self.serial_ports_combobox.addItem("There are no available serial ports")
            else:
                self.serial_ports_combobox.addItems(ports)
        except ImportError:
            print("simulated ports!!!!")
            mock_ports = [f"COM{i}" for i in range(1, 5)]  # 模拟数据
            if not mock_ports:
                self.serial_ports_combobox.addItem("There are no available serial ports")
                self.connect_button.setEnabled(False)
            else:
                self.serial_ports_combobox.addItems(mock_ports)
                self.connect_button.setEnabled(True)


    def attempt_connection(self):
        port = self.serial_ports_combobox.currentText()
        if port and port != "There are no available serial ports":
            # Simulate connection success
            print(f"Attempting connection to {port}...")
            self.selected_port = port
            self.connect_button.setEnabled(False)
            self.disconnect_button.setEnabled(True)
            self.serial_ports_combobox.setEnabled(False)
            self.connection_established.emit(self.selected_port) # Emit signal
            self.accept() # Close dialog with QDialog.Accepted status
        else:
            print("No serial port selected or no ports available")
            self.accept()
            # Optionally show a QMessageBox error

    def attempt_disconnection(self):
        if self.selected_port:
            print(f"disconnect {self.selected_port} ...")
            # In a real app, you'd close the serial connection here
            self.selected_port = None
            if hasattr(self, 'connect_button'):
                self.connect_button.setEnabled(True)
            if hasattr(self, 'disconnect_button'):
                self.disconnect_button.setEnabled(False)
            if hasattr(self, 'serial_ports_combobox'):
                self.serial_ports_combobox.setEnabled(True)
            print("Disconnected")
            # self.reject() # Or handle as needed, maybe just update UI

    def closeEvent(self, event):
        # 添加关闭确认对话框
        from PyQt6.QtWidgets import QMessageBox
        
        reply = QMessageBox.question(
            self, 
            'check quit', 
            'Are you sure to quit?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # 处理关闭前的清理工作
            print("Main window closed.")
            if self.selected_port:  # 修改这里，使用 selected_port 而不是 connected_port
                print(f"Ensured the connection to {self.selected_port} is closed.")
            event.accept()
        else:
            event.ignore()
        # Ensure disconnection if window is closed while connected
        if self.selected_port:
            self.attempt_disconnection()
        super().closeEvent(event)





class BaseDisplayTab(QWidget):
    """Base class for all display tabs"""
    def __init__(self, title="show displayTab", parent=None):
        super().__init__(parent)
        self.title = title
        self.setup_ui()
        self.populate_controls()
        
    def setup_ui(self):
        """Set up base UI layout"""
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(5, 5, 5, 5)
        main_layout.setSpacing(5)
        
        # 使用QSplitter分割控制面板和图表区域
        splitter = QSplitter(Qt.Orientation.Vertical)
        
        # 控制面板
        self.control_panel = QGroupBox("Control panel")
        self.control_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.control_panel.setMaximumHeight(220)
        self.control_panel_layout = QGridLayout(self.control_panel)
        self.control_panel_layout.setContentsMargins(8, 20, 8, 8)
        self.control_panel_layout.setSpacing(6)
        splitter.addWidget(self.control_panel)
        
        # 图表区域 - 增大图表区域
        self.chart_area = QWidget()
        self.chart_area.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.chart_layout = QVBoxLayout(self.chart_area)
        self.chart_layout.setContentsMargins(0, 0, 0, 0)
        
        # 添加图表容器
        self.chart_container = QFrame()
        self.chart_container.setFrameShape(QFrame.Shape.StyledPanel)
        self.chart_container.setStyleSheet("background-color: #FFFFFF;") # #F5F5F5
        self.chart_container.setMinimumHeight(300)
        self.chart_container_layout = QVBoxLayout(self.chart_container)
        
        self.chart_layout.addWidget(self.chart_container)
        splitter.addWidget(self.chart_area)
        
        # Set initial splitter proportions
        splitter.setSizes([200, 600])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        
        main_layout.addWidget(splitter)
        
    def populate_controls(self):
        """Populate control panel (override in subclasses)"""
        pass
        
    def update_chart(self, data):
        """Update chart (override in subclasses)"""
        pass
        
    def select_file(self):
        """Select save path"""
        # 使用exe所在目录下的Data文件夹作为默认路径
        default_dir = get_data_directory()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save data file", default_dir, "EDF Files (*.edf);;All Files (*)"
        )
        if file_path:
            self.file_path_label.setText(file_path)
            
    def start_save(self):
        """Start saving data"""
        if hasattr(self, 'file_path_label'): # and self.file_path_label.text() != "File path don't selected"
            self.start_save_button.setEnabled(False)
            self.stop_save_button.setEnabled(True)
            print(f"Start saving data to {self.file_path_label.text()}")
        else:
             # This base method might need to access main window for logging, 
             # but standard QMessageBox is used in base class. 
             # We can leave it or try to find parent main window.
             # For now, let's assume the subclasses handle specific logic or we pass parent correctly.
             # But wait, BaseDisplayTab is a QWidget. 
             # Let's try to find if it's main window
             window = self.window()
             if hasattr(window, "log_message"):
                 window.log_message("Warning: Select file path first!", level="warning")
             else:
                 QMessageBox.warning(self, "warning", "Select file path first!")
            
    def stop_save(self):
        """Stop saving data"""
        self.start_save_button.setEnabled(True)
        self.stop_save_button.setEnabled(False)
        print("Stop saving data")

class LfpTab(BaseDisplayTab):
    def __init__(self, parent=None):
        super().__init__("Mode0 LFP+MAND+Raw / Mode3 LFP+ESA", parent)

    def populate_controls(self):
        # Row 0: Mode0 File Selection + Save Controls
        lfp_layout = QHBoxLayout()
        lfp_layout.addWidget(QLabel("Mode0 File:"))
        self.lfp_file_path_label = QLabel("File path don't selected")
        lfp_layout.addWidget(self.lfp_file_path_label, 1)
        self.lfp_select_file_button = QPushButton("Choice")
        self.lfp_select_file_button.setMaximumWidth(60)
        self.lfp_select_file_button.clicked.connect(self.select_lfp_file)
        lfp_layout.addWidget(self.lfp_select_file_button)
        lfp_layout.addSpacing(10)
        self.start_save_button = QPushButton("Mode0 Save Start")
        lfp_layout.addWidget(self.start_save_button)
        self.stop_save_button = QPushButton("Mode0 Save Stop")
        self.stop_save_button.setEnabled(False)
        lfp_layout.addWidget(self.stop_save_button)
        self.lfp_progress_bar = QProgressBar()
        self.lfp_progress_bar.setRange(0, 100)
        self.lfp_progress_bar.setValue(0)
        self.lfp_progress_bar.setTextVisible(True)
        self.lfp_progress_bar.setFormat("%p%")
        self.lfp_progress_bar.setFixedWidth(100)
        lfp_layout.addWidget(self.lfp_progress_bar)
        self.control_panel_layout.addLayout(lfp_layout, 0, 0, 1, 2)

        # Row 1: Mode3 File Selection + Save Controls
        mode3_layout = QHBoxLayout()
        mode3_layout.addWidget(QLabel("Mode3 File:"))
        self.mode3_file_path_label = QLabel("File path don't selected")
        mode3_layout.addWidget(self.mode3_file_path_label, 1)
        self.mode3_select_file_button = QPushButton("Choice")
        self.mode3_select_file_button.setMaximumWidth(60)
        self.mode3_select_file_button.clicked.connect(self.select_mode3_file)
        mode3_layout.addWidget(self.mode3_select_file_button)
        mode3_layout.addSpacing(10)
        self.start_save_mode3_button = QPushButton("Mode3 Save Start")
        mode3_layout.addWidget(self.start_save_mode3_button)
        self.stop_save_mode3_button = QPushButton("Mode3 Save Stop")
        self.stop_save_mode3_button.setEnabled(False)
        mode3_layout.addWidget(self.stop_save_mode3_button)
        self.mode3_progress_bar = QProgressBar()
        self.mode3_progress_bar.setRange(0, 100)
        self.mode3_progress_bar.setValue(0)
        self.mode3_progress_bar.setTextVisible(True)
        self.mode3_progress_bar.setFormat("%p%")
        self.mode3_progress_bar.setFixedWidth(100)
        mode3_layout.addWidget(self.mode3_progress_bar)
        self.control_panel_layout.addLayout(mode3_layout, 1, 0, 1, 2)

        # Row 2: Filter + Scale + Spectrum (merged into one row)
        self.lfp_combined_row = QHBoxLayout()

        self.lfp_combined_row.addWidget(QLabel("Low:"))
        self.low_cutoff = QComboBox()
        self.low_cutoff.addItems(["0.5", "4", "8", "13", "30", "50", "100", "150", "250", "300", "None"])
        self.low_cutoff.setMaximumWidth(75)
        self.lfp_combined_row.addWidget(self.low_cutoff)

        self.lfp_combined_row.addWidget(QLabel("High:"))
        self.high_cutoff = QComboBox()
        self.high_cutoff.addItems(["4", "8", "13", "30", "50", "100", "150", "250", "300", "None"])
        self.high_cutoff.setMaximumWidth(75)
        self.lfp_combined_row.addWidget(self.high_cutoff)

        self.enable_filter_button = QPushButton("Filter On")
        self.enable_filter_button.clicked.connect(self.enable_filter)
        self.lfp_combined_row.addWidget(self.enable_filter_button)

        self.disable_filter_button = QPushButton("Filter Off")
        self.disable_filter_button.clicked.connect(self.disable_filter)
        self.disable_filter_button.setEnabled(False)
        self.lfp_combined_row.addWidget(self.disable_filter_button)

        # Separator
        line_sep = QFrame()
        line_sep.setFrameShape(QFrame.Shape.VLine)
        line_sep.setFrameShadow(QFrame.Shadow.Sunken)
        self.lfp_combined_row.addWidget(line_sep)

        self.lfp_combined_row.addWidget(QLabel("LFP Scale:"))
        self.scale_combo = QComboBox()
        self.scale_combo.addItems(["100", "200", "500", "1000", "2000", "3000", "5000", "10000"])
        self.scale_combo.setCurrentText("2000")
        self.scale_combo.setMaximumWidth(80)
        self.lfp_combined_row.addWidget(self.scale_combo)

        self.lfp_combined_row.addWidget(QLabel("ESA&MAND Scale:"))
        self.esa_scale_combo = QComboBox()
        self.esa_scale_combo.addItems([str(value) for value in range(0, 101, 10)])
        self.esa_scale_combo.setCurrentText("10")
        self.esa_scale_combo.setMaximumWidth(70)
        self.lfp_combined_row.addWidget(self.esa_scale_combo)

        self.lfp_combined_row.addWidget(QLabel("MAND Smooth:"))
        self.mand_smooth_combo = QComboBox()
        self.mand_smooth_combo.addItems(["4", "8", "20", "40", "100", "200"])
        self.mand_smooth_combo.setCurrentText("4")
        self.mand_smooth_combo.setMaximumWidth(70)
        self.lfp_combined_row.addWidget(self.mand_smooth_combo)
        self.lfp_combined_row.addWidget(QLabel("ms"))

        self.update_scale_button = QPushButton("Update Scale")
        self.lfp_combined_row.addWidget(self.update_scale_button)

        self.lfp_combined_row.addStretch(1)

        # Spectrum button placeholder - will be populated by ESBMainWindow
        # (ESBMainWindow adds spectrum_window_button here)
        self.control_panel_layout.addLayout(self.lfp_combined_row, 2, 0, 1, 2)

        # Override chart container layout to horizontal for LFP|ESA side-by-side
        # Remove existing VBoxLayout items if any, and replace with HBox
        old_layout = self.chart_container.layout()
        if old_layout is not None:
            # Clear the existing layout
            from PyQt6.QtWidgets import QWidget as _QW
            _temp = _QW()
            _temp.setLayout(old_layout)
            del _temp
        self.chart_container_layout = QHBoxLayout(self.chart_container)
        self.chart_container_layout.setContentsMargins(2, 2, 2, 2)
        self.chart_container_layout.setSpacing(4)
    
    def enable_filter(self):
        """Enable filter"""
        low = self.low_cutoff.currentText()
        high = self.high_cutoff.currentText()
        print(f"Enable filter: {low} - {high}")
        self.enable_filter_button.setEnabled(False)
        self.disable_filter_button.setEnabled(True)
        
    def disable_filter(self):
        """Disable filter"""
        print("Disable filter")
        self.enable_filter_button.setEnabled(True)
        self.disable_filter_button.setEnabled(False)

    def select_lfp_file(self):
        """Select Mode0 save path"""
        default_dir = get_data_directory()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Mode0 data file", default_dir, "EDF Files (*.edf);;All Files (*)"
        )
        if file_path:
            self.lfp_file_path_label.setText(file_path)

    def select_mode3_file(self):
        """Select Mode3 save path"""
        default_dir = get_data_directory()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Mode3 data file", default_dir, "EDF Files (*.edf);;All Files (*)"
        )
        if file_path:
            self.mode3_file_path_label.setText(file_path)

class Spike4ChTab(BaseDisplayTab):
    def __init__(self, parent=None):
        super().__init__("16 channels Spike signal", parent)

    def populate_controls(self):
        # 文件保存功能 - 简化布局
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("File:"))
        self.file_path_label = QLabel("file path don't selected")
        file_layout.addWidget(self.file_path_label, 1)
        
        self.select_file_button = QPushButton("Choice")
        self.select_file_button.setMaximumWidth(60)
        self.select_file_button.clicked.connect(self.select_file)
        file_layout.addWidget(self.select_file_button)
        
        self.control_panel_layout.addLayout(file_layout, 0, 0, 1, 4)
        
        # 文件保存控制按钮
        button_layout = QHBoxLayout()
        self.start_save_button = QPushButton("Save Start")
        self.start_save_button.clicked.connect(self.start_save)
        button_layout.addWidget(self.start_save_button)
        
        self.stop_save_button = QPushButton("Save Stop")
        self.stop_save_button.clicked.connect(self.stop_save)
        self.stop_save_button.setEnabled(False)
        button_layout.addWidget(self.stop_save_button)
        
        # Mode2 Progress Bar
        self.mode2_progress_bar = QProgressBar()
        self.mode2_progress_bar.setRange(0, 100)
        self.mode2_progress_bar.setValue(0)
        self.mode2_progress_bar.setTextVisible(True)
        self.mode2_progress_bar.setFormat("%p%")
        button_layout.addWidget(self.mode2_progress_bar)
        
        # Mode2 v2 is fixed to all 16 channels; keep compatibility attrs for old callers.
        self.channel_combos = []
        self.fixed_channels_label = QLabel("Mode2 v2: fixed RHD Ch0-Ch15, 10.417 kHz, 8-bit raw")
        self.fixed_channels_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        button_layout.addWidget(self.fixed_channels_label, 1)

        self.send_channels_button = QPushButton("Fixed")
        self.send_channels_button.clicked.connect(self.send_channels)
        self.send_channels_button.setEnabled(False)
        self.send_channels_button.setVisible(False)
        
        self.control_panel_layout.addLayout(button_layout, 1, 0, 1, 4)
        
    def send_channels(self):
        print("Mode2 v2 uses fixed 16-channel streaming (RHD Ch0-Ch15).")

class ImuTab(BaseDisplayTab):
    def __init__(self, parent=None):
        super().__init__("IMU signal", parent)

    def populate_controls(self):
        # 控制面板设置为空
        empty_label = QLabel("6-axis IMU signal")
        empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.control_panel_layout.addWidget(empty_label, 0, 0)

class RasterTab(BaseDisplayTab):
    def __init__(self, parent=None):
        super().__init__("Mode3 Raster / Mode0 MAND", parent)

    def populate_controls(self):
        # 通道阈值设置 - 简化布局
        control_layout = QHBoxLayout()
        
        control_layout.addWidget(QLabel("Channel:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems([f"RHD Ch{i}" for i in range(16)])
        self.channel_combo.setMaximumWidth(100)
        self.channel_combo.currentIndexChanged.connect(self.channel_changed)
        control_layout.addWidget(self.channel_combo)
        
        control_layout.addWidget(QLabel("Mode3 threshold:"))
        self.threshold_combo = QComboBox()
        self.threshold_combo.addItems(["50", "60", "100", "150", "200", "500" ,"600", "700"])
        self.threshold_combo.setEditable(True)
        self.threshold_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.threshold_combo.setMaximumWidth(80)
        self.threshold_combo.setCurrentText("60")
        control_layout.addWidget(self.threshold_combo)

        self.send_threshold_button = QPushButton("Send threshold")
        control_layout.addWidget(self.send_threshold_button)
        
        self.auto_threshold_button = QPushButton("Auto threshold update")
        self.auto_threshold_button.clicked.connect(self.auto_threshold)
        control_layout.addWidget(self.auto_threshold_button)

        self.mode0_mand_fixed_label = QLabel("Mode0 MAND: n=7, window=4 ms")
        control_layout.addWidget(self.mode0_mand_fixed_label)
        
        # 添加弹性空间
        control_layout.addStretch(1)
        
        self.control_panel_layout.addLayout(control_layout, 0, 0)
        
    def channel_changed(self, index):
        print(f"Select RHD Ch{index}")
        
    def auto_threshold(self):
        channel = self.channel_combo.currentText()
        threshold = self.threshold_combo.currentText()
        print(f"Set auto-threshold update for {channel}: {threshold}")

class Spike1ChTab(BaseDisplayTab):
    def __init__(self, parent=None):
        super().__init__("Single-Channel Spike Signal", parent)

    def populate_controls(self):
        # 顶部文件保存布局（Mode1）
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("Mode1 File:"))
        self.mode1_file_path_label = QLabel("File path don't selected")
        file_layout.addWidget(self.mode1_file_path_label, 1)
        self.mode1_select_file_button = QPushButton("Choice")
        self.mode1_select_file_button.setMaximumWidth(60)
        self.mode1_select_file_button.clicked.connect(self.select_mode1_file)
        file_layout.addWidget(self.mode1_select_file_button)
        self.control_panel_layout.addLayout(file_layout, 0, 0, 1, 1)

        # 控制面板布局（滤波与通道控制）
        control_layout = QHBoxLayout()

        # 通道选择
        control_layout.addWidget(QLabel("Mode1 channel:"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItems([f"RHD Ch{i}" for i in range(16)])
        self.channel_combo.setMaximumWidth(100)
        control_layout.addWidget(self.channel_combo)

        self.send_command_button = QPushButton("Send")
        self.send_command_button.clicked.connect(self.send_command)
        control_layout.addWidget(self.send_command_button)

        control_layout.addSpacing(10)
        control_layout.addWidget(QLabel("Mode0/3 raw channel:"))
        self.mode3_channel_combo = QComboBox()
        self.mode3_channel_combo.addItems([f"RHD Ch{i}" for i in range(16)])
        self.mode3_channel_combo.setMaximumWidth(120)
        control_layout.addWidget(self.mode3_channel_combo)

        self.send_mode3_command_button = QPushButton("Send Raw Ch")
        self.send_mode3_command_button.clicked.connect(self.send_mode3_command)
        control_layout.addWidget(self.send_mode3_command_button)

        # 分隔线
        control_layout.addSpacing(15)

        # 滤波器设置
        self.filter_enable_checkbox = QCheckBox("Enable filter")
        control_layout.addWidget(self.filter_enable_checkbox)

        control_layout.addWidget(QLabel("Low cutoff (Hz):"))
        self.low_cut_spin = QDoubleSpinBox()
        self.low_cut_spin.setRange(1.0, 9999.0)
        self.low_cut_spin.setDecimals(1)
        self.low_cut_spin.setSingleStep(10.0)
        self.low_cut_spin.setValue(300.0)
        self.low_cut_spin.setMaximumWidth(100)
        control_layout.addWidget(self.low_cut_spin)

        control_layout.addWidget(QLabel("High cutoff (Hz):"))
        self.high_cut_spin = QDoubleSpinBox()
        self.high_cut_spin.setRange(10.0, 10000.0)
        self.high_cut_spin.setDecimals(1)
        self.high_cut_spin.setSingleStep(10.0)
        self.high_cut_spin.setValue(3000.0)
        self.high_cut_spin.setMaximumWidth(100)
        control_layout.addWidget(self.high_cut_spin)

        # 采样率选择
        control_layout.addWidget(QLabel("Sample rate:"))
        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["12500 Hz", "20000 Hz"])  # 对应 12.5k 与 20k
        self.sample_rate_combo.setCurrentIndex(1)  # 默认 20000 Hz
        self.sample_rate_combo.setMaximumWidth(110)
        control_layout.addWidget(self.sample_rate_combo)

        # 频谱按钮
        self.open_spectrum_button = QPushButton("Open Spectrum")
        control_layout.addWidget(self.open_spectrum_button)

        # Mode1保存按钮
        self.start_save_button = QPushButton("Mode1 Save Start")
        control_layout.addWidget(self.start_save_button)
        self.stop_save_button = QPushButton("Mode1 Save Stop")
        self.stop_save_button.setEnabled(False)
        control_layout.addWidget(self.stop_save_button)

        # Mode1 Progress Bar
        self.mode1_progress_bar = QProgressBar()
        self.mode1_progress_bar.setRange(0, 100)
        self.mode1_progress_bar.setValue(0)
        self.mode1_progress_bar.setTextVisible(True)
        self.mode1_progress_bar.setFormat("%p%")
        control_layout.addWidget(self.mode1_progress_bar)

        control_layout.addWidget(QLabel("RMS:"))
        self.rms_label = QLabel("-- uVrms")
        self.rms_label.setMinimumWidth(90)
        control_layout.addWidget(self.rms_label)

        # 添加弹性空间
        control_layout.addStretch(1)

        self.control_panel_layout.addLayout(control_layout, 1, 0)

    def select_mode1_file(self):
        """Select Mode1 save path"""
        default_dir = get_data_directory()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Mode1 data file", default_dir, "EDF Files (*.edf);;All Files (*)"
        )
        if file_path:
            self.mode1_file_path_label.setText(file_path)
        
    def send_command(self):
        channel = self.channel_combo.currentText()
        print(f"Send single-channel selection command: {channel}")
        # 在实际应用中，这里会发送命令到设备

    def send_mode3_command(self):
        channel = self.mode3_channel_combo.currentText()
        print(f"Send Mode3 raw-channel selection command: {channel}")

class HabitsTab(BaseDisplayTab):
    """Habits tracking panel tab"""
    def __init__(self, parent=None):
        super().__init__("Habits Tracking", parent)

    def populate_controls(self):
        """Populate control panel"""
        # Simplified control panel, main functionality is in HabitsPanel
        control_layout = QHBoxLayout()
        
        info_label = QLabel("Habits Tracking System - Data Recording & Analysis")
        info_label.setStyleSheet("font-weight: bold; color: #2E7D32;")
        control_layout.addWidget(info_label)
        
        # Add flexible space
        control_layout.addStretch(1)
        
        self.control_panel_layout.addLayout(control_layout, 0, 0)
        
        # 在图表容器中添加OptimizedHabitsPanel
        self.habits_panel = OptimizedHabitsPanel()
        self.chart_container_layout.addWidget(self.habits_panel)

class ImpedanceTab(BaseDisplayTab):
    """Impedance test panel tab"""
    RAW_DISPLAY_MODE = "Raw"
    RESTORED_DISPLAY_MODE = "Restored"

    def __init__(self, parent=None):
        self._history_records = []
        self._history_view_mode = "Average"
        self._impedance_display_mode = self.RESTORED_DISPLAY_MODE
        self._latest_record = None
        self.channel_colors = [
            "#0F4C81", "#3FA34D", "#E36414", "#D1495B",
            "#6A4C93", "#0081A7", "#5F0F40", "#2A9D8F",
            "#BC6C25", "#3D405B", "#457B9D", "#7F5539",
            "#6D597A", "#1D3557", "#B56576", "#4D908E",
        ]
        super().__init__("Impedance Test", parent)
        self.control_panel.setMaximumHeight(210)
        self._build_impedance_chart_area()
        self._refresh_recent_values([None] * 16)
        self.refresh_history([])

    def populate_controls(self):
        action_layout = QHBoxLayout()

        self.start_test_button = QPushButton("Start Impedance Test")
        self.start_test_button.setMinimumHeight(38)
        self.start_test_button.setStyleSheet(
            "QPushButton { background-color: #1565C0; color: white; border: none; border-radius: 6px; padding: 8px 14px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #B0BEC5; color: #ECEFF1; }"
            "QPushButton:pressed { background-color: #0D47A1; }"
        )
        action_layout.addWidget(self.start_test_button)

        self.display_toggle_button = QPushButton(self._display_mode_button_text())
        self.display_toggle_button.setCheckable(True)
        self.display_toggle_button.setMinimumHeight(34)
        self.display_toggle_button.setStyleSheet(
            "QPushButton { background-color: #F1F5F9; color: #0F172A; border: 1px solid #CBD5E1; border-radius: 6px; padding: 6px 12px; font-weight: 600; }"
            "QPushButton:checked { background-color: #E2E8F0; color: #0F172A; border: 1px solid #94A3B8; }"
            "QPushButton:pressed { background-color: #CBD5E1; }"
        )
        self.display_toggle_button.toggled.connect(self._on_display_mode_toggled)
        action_layout.addWidget(self.display_toggle_button)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.progress_bar.setFixedWidth(180)
        action_layout.addWidget(self.progress_bar)

        self.progress_status_label = QLabel("Ready")
        self.progress_status_label.setMinimumWidth(220)
        self.progress_status_label.setStyleSheet("color: #334155; font-weight: 600;")
        action_layout.addWidget(self.progress_status_label)

        action_layout.addStretch(1)
        self.control_panel_layout.addLayout(action_layout, 0, 0, 1, 2)

        summary_layout = QHBoxLayout()
        summary_layout.setSpacing(12)

        self.last_test_time_label = QLabel("Last Test: --")
        self.last_test_time_label.setMinimumWidth(220)
        self.last_test_time_label.setStyleSheet("color: #0F172A; font-weight: 600;")
        summary_layout.addWidget(self.last_test_time_label)

        self.avg_impedance_label = QLabel("Avg: --")
        self.avg_impedance_label.setMinimumWidth(120)
        summary_layout.addWidget(self.avg_impedance_label)

        self.min_impedance_label = QLabel("Min: --")
        self.min_impedance_label.setMinimumWidth(120)
        summary_layout.addWidget(self.min_impedance_label)

        self.max_impedance_label = QLabel("Max: --")
        self.max_impedance_label.setMinimumWidth(120)
        summary_layout.addWidget(self.max_impedance_label)

        self.avg_phase_label = QLabel("Phase: --")
        self.avg_phase_label.setMinimumWidth(140)
        summary_layout.addWidget(self.avg_phase_label)

        summary_layout.addStretch(1)
        self.control_panel_layout.addLayout(summary_layout, 1, 0, 1, 2)

        compensation_layout = QHBoxLayout()
        compensation_layout.setSpacing(10)
        compensation_layout.addWidget(QLabel("RC Compensation:"))

        compensation_layout.addWidget(QLabel("R (kOhm)"))
        self.series_resistor_spin = QDoubleSpinBox()
        self.series_resistor_spin.setRange(0.0, 10000.0)
        self.series_resistor_spin.setDecimals(2)
        self.series_resistor_spin.setSingleStep(1.0)
        self.series_resistor_spin.setValue(220.0)
        self.series_resistor_spin.setMaximumWidth(100)
        compensation_layout.addWidget(self.series_resistor_spin)

        compensation_layout.addWidget(QLabel("C (pF)"))
        self.shunt_cap_spin = QDoubleSpinBox()
        self.shunt_cap_spin.setRange(0.0, 100000.0)
        self.shunt_cap_spin.setDecimals(2)
        self.shunt_cap_spin.setSingleStep(10.0)
        self.shunt_cap_spin.setValue(47.0)
        self.shunt_cap_spin.setMaximumWidth(100)
        compensation_layout.addWidget(self.shunt_cap_spin)

        self.compensation_info_label = QLabel("Displayed values use channel-to-REF impedance after removing board RC")
        self.compensation_info_label.setStyleSheet("color: #64748B;")
        compensation_layout.addWidget(self.compensation_info_label)

        compensation_layout.addStretch(1)
        self.control_panel_layout.addLayout(compensation_layout, 2, 0, 1, 2)

    def _build_impedance_chart_area(self):
        content = QWidget()
        content_layout = QHBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(12)

        history_panel = QFrame()
        history_panel.setStyleSheet(
            "QFrame { background: #FFFFFF; border: 1px solid #D9E2EC; border-radius: 12px; }"
            "QLabel { color: #0F172A; }"
        )
        history_layout = QVBoxLayout(history_panel)
        history_layout.setContentsMargins(14, 14, 14, 14)
        history_layout.setSpacing(10)

        history_header = QHBoxLayout()
        history_title = QLabel("History Trend")
        history_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        history_header.addWidget(history_title)
        history_header.addStretch(1)
        history_header.addWidget(QLabel("View:"))

        self.history_view_combo = QComboBox()
        self.history_view_combo.addItem("Average")
        for channel_idx in range(16):
            self.history_view_combo.addItem(f"RHD Ch{channel_idx}")
        self.history_view_combo.currentTextChanged.connect(self._on_history_view_changed)
        history_header.addWidget(self.history_view_combo)
        history_layout.addLayout(history_header)

        self.history_meta_label = QLabel("No impedance tests saved yet")
        self.history_meta_label.setStyleSheet("color: #64748B;")
        history_layout.addWidget(self.history_meta_label)

        self.history_plot = pg.PlotWidget(axisItems={'bottom': pg.DateAxisItem(orientation='bottom')})
        self.history_plot.setBackground('w')
        self.history_plot.showGrid(x=True, y=True, alpha=0.2)
        self.history_plot.setLabel('left', 'Impedance', units='kOhm', color='k')
        self.history_plot.setLabel('bottom', 'Test Time', color='k')
        self.history_plot.getAxis('left').setPen(pg.mkPen('#475569'))
        self.history_plot.getAxis('left').setTextPen(pg.mkPen('#475569'))
        self.history_plot.getAxis('bottom').setPen(pg.mkPen('#475569'))
        self.history_plot.getAxis('bottom').setTextPen(pg.mkPen('#475569'))
        self.history_plot.setMinimumHeight(420)
        history_layout.addWidget(self.history_plot, 1)

        recent_panel = QFrame()
        recent_panel.setStyleSheet(
            "QFrame { background: #F8FAFC; border: 1px solid #D9E2EC; border-radius: 12px; }"
            "QLabel { color: #0F172A; }"
        )
        recent_layout = QVBoxLayout(recent_panel)
        recent_layout.setContentsMargins(14, 14, 14, 14)
        recent_layout.setSpacing(10)

        recent_title = QLabel("Latest 16-Channel Result")
        recent_title.setStyleSheet("font-size: 15px; font-weight: 700;")
        recent_layout.addWidget(recent_title)

        self.latest_summary_label = QLabel("Waiting for the first impedance test")
        self.latest_summary_label.setWordWrap(True)
        self.latest_summary_label.setStyleSheet("color: #475569;")
        recent_layout.addWidget(self.latest_summary_label)

        grid_container = QWidget()
        grid_layout = QGridLayout(grid_container)
        grid_layout.setContentsMargins(0, 0, 0, 0)
        grid_layout.setHorizontalSpacing(6)
        grid_layout.setVerticalSpacing(6)

        self.channel_value_labels = []
        self.channel_phase_labels = []
        for channel_idx in range(16):
            cell = QFrame()
            cell.setStyleSheet(
                "QFrame { background: white; border: 1px solid #D9E2EC; border-radius: 8px; }"
            )
            cell.setMinimumWidth(100)
            cell_layout = QVBoxLayout(cell)
            cell_layout.setContentsMargins(8, 5, 8, 5)
            cell_layout.setSpacing(1)

            channel_label = QLabel(f"CH {channel_idx:02d}")
            channel_label.setStyleSheet("color: #64748B; font-size: 10px; font-weight: 600;")
            cell_layout.addWidget(channel_label)

            value_label = QLabel("--")
            value_label.setStyleSheet("font-size: 13px; font-weight: 700; color: #0F172A;")
            cell_layout.addWidget(value_label)

            phase_label = QLabel("Phase: --")
            phase_label.setStyleSheet("font-size: 10px; color: #475569; font-weight: 600;")
            cell_layout.addWidget(phase_label)

            row = channel_idx // 4
            col = channel_idx % 4
            grid_layout.addWidget(cell, row, col)
            self.channel_value_labels.append(value_label)
            self.channel_phase_labels.append(phase_label)

        recent_layout.addWidget(grid_container, 1)

        content_layout.addWidget(history_panel, 3)
        content_layout.addWidget(recent_panel, 2)
        self.chart_container_layout.addWidget(content)

    OPEN_CIRCUIT_MARKER = 0xFFFFFFFF

    @staticmethod
    def format_impedance_value(value_ohm):
        try:
            value = float(value_ohm)
        except (TypeError, ValueError):
            return "--"
        if value >= ImpedanceTab.OPEN_CIRCUIT_MARKER:
            return "\u26a0 OPEN"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.2f} MOhm"
        if value >= 1_000:
            return f"{value / 1_000:.1f} kOhm"
        return f"{value:.0f} Ohm"

    def _value_color(self, value_ohm):
        try:
            value = float(value_ohm)
        except (TypeError, ValueError):
            return "#0F172A"
        if value >= self.OPEN_CIRCUIT_MARKER:
            return "#D32F2F"
        if value < 100_000:
            return "#2E7D32"
        if value < 500_000:
            return "#C62828"
        return "#6A1B9A"

    def _refresh_recent_values(self, impedance_values):
        values = list(impedance_values or [])
        if len(values) < 16:
            values.extend([None] * (16 - len(values)))
        for label, value in zip(self.channel_value_labels, values[:16]):
            label.setText(self.format_impedance_value(value))
            label.setStyleSheet(
                f"font-size: 13px; font-weight: 700; color: {self._value_color(value)};"
            )

    @staticmethod
    def format_phase_value(phase_deg):
        try:
            phase_deg = float(phase_deg)
        except (TypeError, ValueError):
            return "--"
        return f"{phase_deg:.1f} deg"

    def _refresh_recent_phases(self, phase_values):
        values = list(phase_values or [])
        if len(values) < 16:
            values.extend([None] * (16 - len(values)))
        for label, value in zip(self.channel_phase_labels, values[:16]):
            label.setText(f"Phase: {self.format_phase_value(value)}")

    def _on_display_mode_toggled(self, checked):
        self._impedance_display_mode = self.RAW_DISPLAY_MODE if checked else self.RESTORED_DISPLAY_MODE
        self.display_toggle_button.setText(self._display_mode_button_text())
        if self._latest_record:
            self.update_result(self._latest_record)
        self._refresh_history_plot()

    def _display_mode_button_text(self):
        if self._impedance_display_mode == self.RAW_DISPLAY_MODE:
            return "Display: Raw (Device diff)"
        return "Display: Restored (Channel-REF)"

    def _display_mode_description(self):
        if self._impedance_display_mode == self.RAW_DISPLAY_MODE:
            return "Raw device-differential"
        return "Restored channel-to-REF"

    def _extract_display_values(self, record):
        if self._impedance_display_mode == self.RAW_DISPLAY_MODE:
            values = list(record.get("raw_impedance_magnitude_ohm", record.get("impedance_ohm", [])))
            phase_values = list(record.get("raw_impedance_phase_deg", record.get("impedance_phase_deg", [])))
            return values, phase_values, self._display_mode_description()
        values = list(record.get("corrected_impedance_magnitude_ohm", record.get("impedance_ohm", [])))
        phase_values = list(record.get("corrected_impedance_phase_deg", record.get("impedance_phase_deg", [])))
        return values, phase_values, self._display_mode_description()

    def _on_history_view_changed(self, text):
        self._history_view_mode = text
        self._refresh_history_plot()

    def update_progress(self, percent, status_text, running):
        self.progress_bar.setValue(int(max(0, min(100, percent))))
        self.progress_status_label.setText(status_text)
        self.start_test_button.setEnabled(not running)

    def update_result(self, record):
        self._latest_record = dict(record or {})
        values, phase_values, mode_text = self._extract_display_values(record)
        self._refresh_recent_values(values)
        self._refresh_recent_phases(phase_values)

        if values:
            # Exclude open-circuit channels from statistics
            valid_values = [v for v in values if float(v) < self.OPEN_CIRCUIT_MARKER]
            open_count = len(values) - len(valid_values)
            if valid_values:
                avg_value = float(np.mean(valid_values))
                min_value = float(np.min(valid_values))
                max_value = float(np.max(valid_values))
            else:
                avg_value = min_value = max_value = 0.0
            if phase_values:
                valid_phases = [p for v, p in zip(values, phase_values) if float(v) < self.OPEN_CIRCUIT_MARKER]
                avg_phase_deg = float(np.mean(np.asarray(valid_phases or [0.0], dtype=np.float32)))
                phase_text = f"{avg_phase_deg:.1f} deg"
            else:
                phase_text = "--"
            self.avg_impedance_label.setText(f"Avg: {self.format_impedance_value(avg_value)}")
            self.min_impedance_label.setText(f"Min: {self.format_impedance_value(min_value)}")
            self.max_impedance_label.setText(f"Max: {self.format_impedance_value(max_value)}")
            self.avg_phase_label.setText(f"Phase: {phase_text}")
            open_text = f" | {open_count} OPEN" if open_count > 0 else ""
            self.latest_summary_label.setText(
                f"{mode_text} average {self.format_impedance_value(avg_value)} across {len(valid_values)} channels, mean phase {phase_text}{open_text}"
            )
        else:
            self.avg_impedance_label.setText("Avg: --")
            self.min_impedance_label.setText("Min: --")
            self.max_impedance_label.setText("Max: --")
            self.avg_phase_label.setText("Phase: --")
            self.latest_summary_label.setText("No impedance values available")

        last_test_text = record.get("local_time_display") or record.get("timestamp") or "--"
        self.last_test_time_label.setText(f"Last Test: {last_test_text}")
        self.update_progress(100, "Completed", False)

    def refresh_history(self, records):
        self._history_records = list(records or [])
        if self._history_records:
            self.history_meta_label.setText(f"{len(self._history_records)} tests saved")
        else:
            self.history_meta_label.setText("No impedance tests saved yet")
        self._refresh_history_plot()

    def set_compensation_summary(self, text):
        self.compensation_info_label.setText(text)

    def _refresh_history_plot(self):
        self.history_plot.clear()
        if not self._history_records:
            return

        timestamps = []
        values_by_test = []
        for record in self._history_records:
            timestamp_epoch = record.get("timestamp_epoch")
            values, _, _ = self._extract_display_values(record)
            if timestamp_epoch is None or not values:
                continue
            timestamps.append(float(timestamp_epoch))
            values_by_test.append(list(values))

        if not timestamps:
            return

        if self._history_view_mode == "Average":
            open_marker = float(self.OPEN_CIRCUIT_MARKER)
            y_values = []
            for values in values_by_test:
                valid = [v for v in values if float(v) < open_marker]
                y_values.append(float(np.mean(valid)) / 1000.0 if valid else 0.0)
            pen = pg.mkPen('#1565C0', width=2)
            self.history_plot.plot(timestamps, y_values, pen=pen, symbol='o', symbolBrush='#1565C0', symbolPen=pen)
            self.history_meta_label.setText(
                f"{len(y_values)} tests saved | View: average impedance | Data: {self._display_mode_description()}"
            )
        else:
            channel_idx = max(0, self.history_view_combo.currentIndex() - 1)
            y_values = []
            for values in values_by_test:
                if channel_idx < len(values):
                    y_values.append(float(values[channel_idx]) / 1000.0)
                else:
                    y_values.append(0.0)
            color = self.channel_colors[channel_idx % len(self.channel_colors)]
            pen = pg.mkPen(color, width=2)
            self.history_plot.plot(timestamps, y_values, pen=pen, symbol='o', symbolBrush=color, symbolPen=pen)
            self.history_meta_label.setText(
                f"{len(y_values)} tests saved | View: RHD Ch{channel_idx} | Data: {self._display_mode_description()}"
            )

        self.history_plot.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=True)

class CameraWindow(QWidget):
    """Separate window for camera display"""
    closed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Camera View")
        self.resize(640, 480)
        
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        
        self.video_label = QLabel("Camera not opened")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: black; color: white;")
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.video_label)

        self.detection_label = QLabel("Charging Guard: not running")
        self.detection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detection_label.setMinimumHeight(24)
        self.detection_label.setStyleSheet("background-color: #111827; color: #E5E7EB; padding: 4px;")
        layout.addWidget(self.detection_label)

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

class MainWindow(QMainWindow):
    rf_connect_result = pyqtSignal(object, str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Neural Signal Recorder")
        self.setGeometry(100, 100, 1400, 1000) # Default size
        
        self.current_recording_mode = "Idle"

        # ── Global QSS Theme ──────────────────────────────────────────
        self.setStyleSheet("""
            /* ── Base ─────────────────────────────────────────────── */
            QMainWindow {
                background-color: #F8FAFC;
            }
            QWidget {
                font-family: -apple-system, "Segoe UI", "Helvetica Neue", Arial, sans-serif;
                font-size: 13px;
                color: #1E293B;
            }

            /* ── Group Boxes ──────────────────────────────────────── */
            QGroupBox {
                font-weight: 600;
                font-size: 13px;
                color: #334155;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                margin-top: 10px;
                padding-top: 14px;
                background-color: #FFFFFF;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 2px 10px;
                background-color: #FFFFFF;
                border-radius: 4px;
            }

            /* ── Buttons ──────────────────────────────────────────── */
            QPushButton {
                background-color: #E2E8F0;
                color: #334155;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 4px 10px;
                font-weight: 600;
                font-size: 12px;
                min-height: 24px;
            }
            QPushButton:hover {
                background-color: #CBD5E1;
                border-color: #94A3B8;
            }
            QPushButton:pressed {
                background-color: #94A3B8;
            }
            QPushButton:disabled {
                background-color: #F1F5F9;
                color: #94A3B8;
                border-color: #E2E8F0;
            }

            /* ── ComboBox ─────────────────────────────────────────── */
            QComboBox {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 6px;
                padding: 3px 8px;
                font-size: 12px;
                min-height: 24px;
                color: #1E293B;
            }
            QComboBox:hover {
                border-color: #3B82F6;
            }
            QComboBox::drop-down {
                border: none;
                width: 24px;
            }
            QComboBox QAbstractItemView {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 4px;
                selection-background-color: #DBEAFE;
                selection-color: #1E40AF;
            }

            /* ── Tabs ─────────────────────────────────────────────── */
            QTabWidget::pane {
                border: 1px solid #E2E8F0;
                border-radius: 8px;
                background-color: #FFFFFF;
                top: -1px;
            }
            QTabBar::tab {
                background-color: #F1F5F9;
                color: #64748B;
                border: 1px solid #E2E8F0;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                padding: 8px 16px;
                margin-right: 2px;
                font-weight: 600;
                font-size: 12px;
            }
            QTabBar::tab:selected {
                background-color: #FFFFFF;
                color: #1E40AF;
                border-bottom: 2px solid #3B82F6;
            }
            QTabBar::tab:hover:!selected {
                background-color: #E2E8F0;
                color: #334155;
            }

            /* ── Labels ───────────────────────────────────────────── */
            QLabel {
                color: #334155;
                font-size: 12px;
            }

            /* ── SpinBoxes ────────────────────────────────────────── */
            QSpinBox, QDoubleSpinBox {
                background-color: #FFFFFF;
                border: 1px solid #CBD5E1;
                border-radius: 5px;
                padding: 2px 6px;
                font-size: 12px;
                min-height: 22px;
            }
            QSpinBox:focus, QDoubleSpinBox:focus {
                border-color: #3B82F6;
            }

            /* ── ProgressBar ──────────────────────────────────────── */
            QProgressBar {
                border: 1px solid #CBD5E1;
                border-radius: 5px;
                background-color: #F1F5F9;
                text-align: center;
                font-size: 11px;
                font-weight: 600;
                color: #334155;
                min-height: 18px;
            }
            QProgressBar::chunk {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #3B82F6, stop:1 #60A5FA);
                border-radius: 4px;
            }

            /* ── Text Browser (Log) ──────────────────────────────── */
            QTextBrowser {
                background-color: #FAFBFC;
                border: 1px solid #E2E8F0;
                border-radius: 6px;
                font-family: "SF Mono", "Menlo", "Monaco", "Consolas", monospace;
                font-size: 11px;
                color: #334155;
                padding: 4px;
            }

            /* ── Scrollbar ────────────────────────────────────────── */
            QScrollBar:vertical {
                background: #F1F5F9;
                width: 8px;
                border-radius: 4px;
            }
            QScrollBar::handle:vertical {
                background: #94A3B8;
                border-radius: 4px;
                min-height: 20px;
            }
            QScrollBar::handle:vertical:hover {
                background: #64748B;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }

            /* ── Status Bar ───────────────────────────────────────── */
            QStatusBar {
                background-color: #F1F5F9;
                color: #64748B;
                font-size: 12px;
                border-top: 1px solid #E2E8F0;
            }

            /* ── Frame Panels ─────────────────────────────────────── */
            QFrame[frameShape="6"] {  /* StyledPanel */
                background-color: #FFFFFF;
                border: 1px solid #E2E8F0;
                border-radius: 8px;
            }

            /* ── CheckBox ─────────────────────────────────────────── */
            QCheckBox {
                spacing: 6px;
                font-size: 12px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
                border: 1px solid #CBD5E1;
                border-radius: 4px;
                background-color: #FFFFFF;
            }
            QCheckBox::indicator:checked {
                background-color: #3B82F6;
                border-color: #3B82F6;
            }

            /* ── Splitter ─────────────────────────────────────────── */
            QSplitter::handle {
                background-color: #E2E8F0;
            }
            QSplitter::handle:horizontal {
                width: 2px;
            }
            QSplitter::handle:vertical {
                height: 2px;
            }
        """)

        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        main_layout = QVBoxLayout(self.central_widget)
        main_layout.setContentsMargins(15, 15, 15, 15)
        main_layout.setSpacing(10)

        # 初始化摄像头相关属性
        self.is_camera_on = False
        self.is_camera_display_on = False
        self.is_recording = False

        self.video_save_path = None
        self.camera_module = CameraModule()  # 实例化CameraModule
        self.camera_timer = QTimer()  # 添加定时器用于更新摄像头画面
        self.camera_timer.timeout.connect(self.update_camera_frame)
        self.camera_health_timer = QTimer()
        self.camera_health_timer.timeout.connect(self._poll_camera_health)
        self.camera_health_timer.start(1500)
        self._camera_recovery_in_progress = False

        self.main_serial_control_bar = QFrame()
        self.main_serial_control_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.main_serial_control_bar.setStyleSheet(CONTROL_SURFACE_STYLESHEET)
        serial_control_layout = QHBoxLayout(self.main_serial_control_bar)
        serial_control_layout.setContentsMargins(12, 8, 12, 8)
        serial_control_layout.setSpacing(10)

        self.main_serial_label = QLabel("Neural serial:")
        serial_control_layout.addWidget(self.main_serial_label)

        self.main_serial_combo = QComboBox()
        self.main_serial_combo.setMinimumWidth(260)
        self.main_serial_combo.setMinimumHeight(35)
        serial_control_layout.addWidget(self.main_serial_combo, 1)

        self.main_serial_refresh_button = QPushButton("Refresh")
        self.main_serial_refresh_button.setMinimumHeight(35)
        serial_control_layout.addWidget(self.main_serial_refresh_button)

        self.main_serial_connect_button = QPushButton("Connect")
        self.main_serial_connect_button.setMinimumHeight(35)
        self.main_serial_connect_button.setMinimumWidth(95)
        serial_control_layout.addWidget(self.main_serial_connect_button)

        self.restart_relay_button = QPushButton("Restart Relay")
        self.restart_relay_button.setMinimumHeight(35)
        self.restart_relay_button.setMinimumWidth(110)
        self.restart_relay_button.setEnabled(False)
        self.restart_relay_button.setToolTip("Reboot the relay device and reconnect this GUI")
        self.restart_relay_button.setStyleSheet("""
            QPushButton {
                background-color: #F59E0B; color: white;
                border: none; border-radius: 6px; padding: 6px; font-weight: 700;
            }
            QPushButton:hover { background-color: #D97706; }
            QPushButton:disabled {
                background-color: #E2E8F0; color: #94A3B8;
            }
        """)
        serial_control_layout.addWidget(self.restart_relay_button)

        self.relay_esb_channel_label = QLabel("ESB Channel:")
        serial_control_layout.addWidget(self.relay_esb_channel_label)

        self.relay_esb_channel_combo = QComboBox()
        self.relay_esb_channel_combo.setMinimumWidth(90)
        self.relay_esb_channel_combo.setEnabled(False)
        self.relay_esb_channel_combo.addItem("CH84", 84)
        self.relay_esb_channel_combo.addItem("CH78", 78)
        self.relay_esb_channel_combo.addItem("CH67", 67)
        self.relay_esb_channel_combo.addItem("CH50", 50)
        self.relay_esb_channel_combo.addItem("CH33", 33)
        self.relay_esb_channel_combo.addItem("CH17", 17)
        self.relay_esb_channel_combo.addItem("CH2", 2)
        serial_control_layout.addWidget(self.relay_esb_channel_combo)

        self.apply_relay_esb_channel_button = QPushButton("Apply ESB CH")
        self.apply_relay_esb_channel_button.setMinimumHeight(35)
        self.apply_relay_esb_channel_button.setMinimumWidth(118)
        self.apply_relay_esb_channel_button.setEnabled(False)
        self.apply_relay_esb_channel_button.setToolTip(
            "Apply the selected recommended ESB channel to both the relay and the peripheral"
        )
        self.apply_relay_esb_channel_button.setStyleSheet("""
            QPushButton {
                background-color: #2563EB; color: white;
                border: none; border-radius: 6px; padding: 6px; font-weight: 700;
            }
            QPushButton:hover { background-color: #1D4ED8; }
            QPushButton:disabled {
                background-color: #E2E8F0; color: #94A3B8;
            }
        """)
        serial_control_layout.addWidget(self.apply_relay_esb_channel_button)

        self.main_serial_status_indicator = QLabel("Disconnected")
        self.main_serial_status_indicator.setFixedWidth(120)
        self.main_serial_status_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        serial_control_layout.addWidget(self.main_serial_status_indicator)

        main_layout.addWidget(self.main_serial_control_bar)

        # 添加摄像头控制按钮
        self.camera_control_bar = QFrame()
        self.camera_control_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.camera_control_bar.setStyleSheet(CONTROL_SURFACE_STYLESHEET)
        camera_control_layout = QHBoxLayout(self.camera_control_bar)
        camera_control_layout.setContentsMargins(12, 6, 12, 6)
        camera_control_layout.setSpacing(10)
        
        self.camera_label = QLabel("camera:")
        camera_control_layout.addWidget(self.camera_label)
        
        # 摄像头选择下拉框
        self.camera_selection_combo = QComboBox()
        self.camera_selection_combo.setMinimumWidth(150)
        self.camera_selection_combo.setMinimumHeight(35)
        camera_control_layout.addWidget(self.camera_selection_combo)
        
        # 刷新摄像头列表按钮
        self.refresh_cameras_button = QPushButton("Refresh")
        self.refresh_cameras_button.setMinimumHeight(35)
        self.refresh_cameras_button.clicked.connect(self.refresh_camera_list)
        camera_control_layout.addWidget(self.refresh_cameras_button)
        
        self.toggle_camera_button = QPushButton("Open camera")
        self.toggle_camera_button.setMinimumHeight(35)
        self.toggle_camera_button.clicked.connect(self.toggle_camera)
        camera_control_layout.addWidget(self.toggle_camera_button)
        
        # 新增：独立的摄像头显示切换按钮
        self.toggle_camera_display_button = QPushButton("Show camera")
        self.toggle_camera_display_button.setMinimumHeight(35)
        self.toggle_camera_display_button.setEnabled(False)
        self.toggle_camera_display_button.clicked.connect(self.toggle_camera_display)
        camera_control_layout.addWidget(self.toggle_camera_display_button)
        
        self.toggle_recording_button = QPushButton("Start recording")
        self.toggle_recording_button.setMinimumHeight(35)
        self.toggle_recording_button.setEnabled(False)
        self.toggle_recording_button.clicked.connect(self.toggle_recording)
        camera_control_layout.addWidget(self.toggle_recording_button)

        self.charging_guard_enable_checkbox = QCheckBox("Charging Guard")
        self.charging_guard_enable_checkbox.setChecked(False)
        self.charging_guard_enable_checkbox.setToolTip("Warn the mouse when it stays on the platform while RF power is on.")
        camera_control_layout.addWidget(self.charging_guard_enable_checkbox)
        self.charging_guard_config_button = QPushButton("Config")
        self.charging_guard_config_button.setMinimumHeight(28)
        self.charging_guard_config_button.setMaximumWidth(70)
        self.charging_guard_config_button.setToolTip("Configure charging guard parameters for each cage.")
        camera_control_layout.addWidget(self.charging_guard_config_button)

        self.charging_guard_status_label = QLabel("Guard: Off")
        self.charging_guard_status_label.setMinimumWidth(110)
        self.charging_guard_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.charging_guard_status_label.setStyleSheet("background-color: #E5E7EB; color: #111827; border-radius: 4px; padding: 5px;")
        camera_control_layout.addWidget(self.charging_guard_status_label)

        self.mouse_detection_status_label = QLabel("Mouse: --")
        self.mouse_detection_status_label.setMinimumWidth(120)
        self.mouse_detection_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mouse_detection_status_label.setStyleSheet("background-color: #F3F4F6; color: #111827; border-radius: 4px; padding: 5px;")
        camera_control_layout.addWidget(self.mouse_detection_status_label)
        
        self.select_save_path_button = QPushButton("Choice save path")
        self.select_save_path_button.setMinimumHeight(35)
        self.select_save_path_button.clicked.connect(self.select_video_save_path)
        camera_control_layout.addWidget(self.select_save_path_button)
        
        self.save_path_label = QLabel("File path don't selected")
        camera_control_layout.addWidget(self.save_path_label, 1)
        
        main_layout.addWidget(self.camera_control_bar)

        # ── Row 1: Sampling Control Bar ─────────────────────────────
        self.sampling_control_bar = QFrame()
        self.sampling_control_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.sampling_control_bar.setStyleSheet(CONTROL_SURFACE_STYLESHEET)
        control_group_layout = QHBoxLayout(self.sampling_control_bar)
        control_group_layout.setContentsMargins(12, 8, 12, 8)
        control_group_layout.setSpacing(10)

        self.sampling_mode_label = QLabel("Mode:")
        control_group_layout.addWidget(self.sampling_mode_label)

        self.sampling_mode_combo = QComboBox()
        self.sampling_mode_combo.addItems(["Mode0 LFP+MAND+Raw", "single channel Spike", "16 channels Spike", "ESA&MUA"])
        self.sampling_mode_combo.setCurrentIndex(0)
        self.sampling_mode_combo.setMinimumWidth(210)
        control_group_layout.addWidget(self.sampling_mode_combo)

        self.start_sampling_button = QPushButton("Sample Start")
        self.start_sampling_button.setMinimumHeight(32)
        self.start_sampling_button.setMinimumWidth(100)
        self.start_sampling_button.clicked.connect(self.start_sampling)
        control_group_layout.addWidget(self.start_sampling_button)

        self.stop_sampling_button = QPushButton("Sample Stop")
        self.stop_sampling_button.setMinimumHeight(32)
        self.stop_sampling_button.setMinimumWidth(100)
        self.stop_sampling_button.clicked.connect(self.stop_sampling)
        control_group_layout.addWidget(self.stop_sampling_button)

        self.mode3_reref_mode_combo = QComboBox()
        self.mode3_reref_mode_combo.addItems(["ReRef: OFF", "ReRef: FAST", "ReRef: STABLE"])
        self.mode3_reref_mode_combo.setCurrentIndex(0)
        self.mode3_reref_mode_combo.setMinimumHeight(32)
        self.mode3_reref_mode_combo.setMinimumWidth(120)
        self.mode3_reref_mode_combo.setStyleSheet("""
            QComboBox {
                background-color: #DBEAFE; color: #1E40AF;
                border: 1px solid #93C5FD; border-radius: 6px;
                font-weight: 600;
            }
        """)
        control_group_layout.addWidget(self.mode3_reref_mode_combo)

        self.global_save_enable_button = QPushButton("Save: ON")
        self.global_save_enable_button.setCheckable(True)
        self.global_save_enable_button.setChecked(True)
        self.global_save_enable_button.setMinimumHeight(32)
        self.global_save_enable_button.setMinimumWidth(80)
        self.global_save_enable_button.clicked.connect(self.toggle_global_save)
        self.global_save_enable_button.setStyleSheet("""
            QPushButton {
                background-color: #22C55E; color: white;
                border: none; border-radius: 6px; font-weight: 700;
            }
            QPushButton:hover { background-color: #16A34A; }
        """)
        control_group_layout.addWidget(self.global_save_enable_button)

        # Status indicators on the right side of control bar
        control_group_layout.addSpacing(10)

        self.battery_status_combo = QComboBox()
        self.battery_status_combo.addItems(["low-power", "Accel", "Accel+gyro"])
        self.battery_status_combo.setCurrentIndex(1)
        self.battery_status_combo.setMinimumWidth(100)
        self.battery_status_combo.currentIndexChanged.connect(self.IMU_status_changed)
        control_group_layout.addWidget(self.battery_status_combo)

        self.update_battery_button = QPushButton("Batt: --")
        self.update_battery_button.setMinimumWidth(95)
        control_group_layout.addWidget(self.update_battery_button)

        self.rssi_indicator = QLabel("RSSI: -- dBm")
        self.rssi_indicator.setFixedWidth(110)
        self.rssi_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rssi_indicator.setStyleSheet("""
            background-color: #DCFCE7; color: #166534;
            border: 1px solid #86EFAC; border-radius: 6px; padding: 4px;
            font-weight: 600; font-size: 11px;
        """)
        control_group_layout.addWidget(self.rssi_indicator)

        self.packet_loss_indicator = QLabel("Loss: 0%")
        self.packet_loss_indicator.setFixedWidth(90)
        self.packet_loss_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.packet_loss_indicator.setStyleSheet("""
            background-color: #DCFCE7; color: #166534;
            border: 1px solid #86EFAC; border-radius: 6px; padding: 4px;
            font-weight: 600; font-size: 11px;
        """)
        control_group_layout.addWidget(self.packet_loss_indicator)

        self.run_time_label = QLabel("Run: 0m")
        self.run_time_label.setFixedWidth(85)
        self.run_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.run_time_label.setStyleSheet("""
            background-color: #DBEAFE; color: #1E40AF;
            border: 1px solid #93C5FD; border-radius: 6px; padding: 4px;
            font-weight: 600; font-size: 11px;
        """)
        control_group_layout.addWidget(self.run_time_label)

        main_layout.addWidget(self.sampling_control_bar)

        # ── Row 2: RF Control Bar ─────────────────────────────────────
        self.rf_control_bar = QFrame()
        self.rf_control_bar.setFrameShape(QFrame.Shape.StyledPanel)
        self.rf_control_bar.setStyleSheet(CONTROL_SURFACE_STYLESHEET)
        rf_control_layout = QHBoxLayout(self.rf_control_bar)
        rf_control_layout.setContentsMargins(12, 6, 12, 6)
        rf_control_layout.setSpacing(10)

        rf_control_layout.addWidget(QLabel("RF Control:"))

        self.rf_serial_combo = QComboBox()
        self.rf_serial_combo.setMinimumWidth(160)
        self.rf_serial_combo.addItem("Select RF Port")
        rf_control_layout.addWidget(self.rf_serial_combo, 1)

        self.rf_channel_combo = QComboBox()
        self.rf_channel_combo.setMinimumWidth(65)
        self.rf_channel_combo.addItems(["CH1", "CH2", "CH3", "CH4"])
        rf_control_layout.addWidget(self.rf_channel_combo)

        self.rf_refresh_button = QPushButton("Refresh")
        self.rf_refresh_button.setMinimumWidth(70)
        self.rf_refresh_button.clicked.connect(self.refresh_rf_serial_ports)
        rf_control_layout.addWidget(self.rf_refresh_button)

        self.rf_connect_button = QPushButton("Connect RF")
        self.rf_connect_button.setMinimumWidth(90)
        self.rf_connect_button.setStyleSheet("""
            QPushButton {
                background-color: #22C55E; color: white;
                border: none; border-radius: 6px; padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #16A34A; }
        """)
        self.rf_connect_button.clicked.connect(self.toggle_rf_connection)
        rf_control_layout.addWidget(self.rf_connect_button)

        self.rf_power_on_button = QPushButton("RF On")
        self.rf_power_on_button.setMinimumWidth(65)
        self.rf_power_on_button.setEnabled(False)
        self.rf_power_on_button.setStyleSheet("""
            QPushButton {
                background-color: #3B82F6; color: white;
                border: none; border-radius: 6px; padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #2563EB; }
            QPushButton:disabled { background-color: #E2E8F0; color: #94A3B8; }
        """)
        rf_control_layout.addWidget(self.rf_power_on_button)

        self.rf_power_off_button = QPushButton("RF Off")
        self.rf_power_off_button.setMinimumWidth(65)
        self.rf_power_off_button.setEnabled(False)
        self.rf_power_off_button.setStyleSheet("""
            QPushButton {
                background-color: #EF4444; color: white;
                border: none; border-radius: 6px; padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #DC2626; }
            QPushButton:disabled { background-color: #E2E8F0; color: #94A3B8; }
        """)
        rf_control_layout.addWidget(self.rf_power_off_button)

        self.rf_status_button = QPushButton("Query")
        self.rf_status_button.setMinimumWidth(65)
        self.rf_status_button.setEnabled(False)
        self.rf_status_button.setStyleSheet("""
            QPushButton {
                background-color: #F59E0B; color: white;
                border: none; border-radius: 6px; padding: 6px 10px;
                font-weight: 700;
            }
            QPushButton:hover { background-color: #D97706; }
            QPushButton:disabled { background-color: #E2E8F0; color: #94A3B8; }
        """)
        rf_control_layout.addWidget(self.rf_status_button)

        self.rf_status_indicator = QLabel("RF: --")
        self.rf_status_indicator.setFixedWidth(75)
        self.rf_status_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rf_status_indicator.setStyleSheet("""
            background-color: #F1F5F9; color: #64748B;
            border: 1px solid #E2E8F0; border-radius: 6px; padding: 4px;
            font-weight: 600;
        """)
        rf_control_layout.addWidget(self.rf_status_indicator)

        main_layout.addWidget(self.rf_control_bar)
        
        # 分隔线
        self.top_controls_separator = QFrame()
        self.top_controls_separator.setFrameShape(QFrame.Shape.HLine)
        self.top_controls_separator.setFrameShadow(QFrame.Shadow.Sunken)
        main_layout.addWidget(self.top_controls_separator)
        
        # Tab Widget
        self.tab_widget = QTabWidget()
        self.tab_widget.setTabPosition(QTabWidget.TabPosition.North)
        self.tab_widget.setDocumentMode(True)
        
        self.lfp_tab = LfpTab()
        self.spike4ch_tab = Spike4ChTab()
        self.imu_tab = ImuTab()
        self.raster_tab = RasterTab()
        self.spike1ch_tab = Spike1ChTab()
        self.habits_tab = HabitsTab()
        self.impedance_tab = ImpedanceTab()
        
        self.tab_widget.addTab(self.lfp_tab, "Mode0 LFP+MAND")
        self.tab_widget.addTab(self.spike4ch_tab, "16 channels Spike")
        self.tab_widget.addTab(self.imu_tab, "IMU")
        self.tab_widget.addTab(self.raster_tab, "Raster/MAND")
        self.tab_widget.addTab(self.spike1ch_tab, "single channel Spike")
        self.tab_widget.addTab(self.habits_tab, "Habits Tracking")
        self.tab_widget.addTab(self.impedance_tab, "Impedance Test")
        
        self.tab_widget.currentChanged.connect(self.tab_changed)
        
        main_layout.addWidget(self.tab_widget)

        # Log Box Area
        self.log_group = QGroupBox("System Log")
        self.log_group.setMaximumHeight(80)
        log_layout = QHBoxLayout(self.log_group)
        log_layout.setContentsMargins(5, 2, 5, 2)
        
        self.clear_log_button = QPushButton("Clear")
        self.clear_log_button.setFixedWidth(60)
        self.clear_log_button.clicked.connect(self.clear_log)
        log_layout.addWidget(self.clear_log_button)
        
        self.log_box = QTextBrowser()
        self.log_box.setOpenExternalLinks(True)
        self.log_box.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        log_layout.addWidget(self.log_box)
        
        main_layout.addWidget(self.log_group)

        # Timeline Widget
        self.timeline_group = QGroupBox("24-Hour Timeline")
        self.timeline_group.setMaximumHeight(100)
        timeline_layout = QVBoxLayout(self.timeline_group)
        timeline_layout.setContentsMargins(5, 5, 5, 5)
        
        self.timeline_widget = TimelineWidget()
        self.timeline_widget.anomalyClicked.connect(self._handle_timeline_anomaly_clicked)
        timeline_layout.addWidget(self.timeline_widget)
        
        main_layout.addWidget(self.timeline_group)

        if hasattr(self.habits_tab, 'habits_panel') and hasattr(self.habits_tab.habits_panel, 'DataDirectoryChanged'):
            self.habits_tab.habits_panel.DataDirectoryChanged.connect(
                self._refresh_battery_timeline_from_history
            )
        self.battery_history_manager = BatteryHistoryManager(
            storage_path_resolver=self._resolve_battery_history_path
        )
        self._battery_timeline_refresh_timer = QTimer(self)
        self._battery_timeline_refresh_timer.setSingleShot(True)
        self._battery_timeline_refresh_timer.timeout.connect(self._refresh_battery_timeline_from_history)
        self._refresh_battery_timeline_from_history()

        # 状态栏
        self.statusBar().showMessage("Ready")
        
        self._skip_local_hardware_enumeration = bool(
            getattr(self, "_skip_local_hardware_enumeration", False)
        )
        # 初始化本地设备列表。Remote detail viewer 会显式跳过这一步，
        # 避免打开 chart 时重新探测本机 camera/serial 设备。
        if not self._skip_local_hardware_enumeration:
            self.refresh_camera_list()
            self.refresh_main_serial_ports()
        self.set_main_serial_ui_connected(False)
        
        # 初始化RF控制相关属性
        self.rf_serial_connection = None
        self.rf_connected = False
        
        # 初始化RF串口列表
        if not self._skip_local_hardware_enumeration:
            self.refresh_rf_serial_ports()
    
    def toggle_global_save(self, checked):
        """Toggle global save state"""
        if checked:
            self.global_save_enable_button.setText("Save: ON")
            self.global_save_enable_button.setStyleSheet("""
                QPushButton {
                    background-color: #22C55E; color: white;
                    border: none; border-radius: 6px; font-weight: 700;
                }
                QPushButton:hover { background-color: #16A34A; }
            """)
        else:
            self.global_save_enable_button.setText("Save: OFF")
            self.global_save_enable_button.setStyleSheet("""
                QPushButton {
                    background-color: #EF4444; color: white;
                    border: none; border-radius: 6px; font-weight: 700;
                }
                QPushButton:hover { background-color: #DC2626; }
            """)

    def apply_profile_configuration(self, config, auto_connect_default=False, log_success=True):
        """Apply a loaded cage/profile config to the current GUI controls."""
        try:
            if not isinstance(config, dict):
                raise ValueError("Profile config must be a JSON object")

            def _log(message, level="info"):
                if log_success or level in {"warning", "error"}:
                    self.log_message(message, level=level)

            if 'lfp_save_path' in config:
                self.lfp_tab.lfp_file_path_label.setText(str(config['lfp_save_path']))
                _log(f"LFP path set to: {config['lfp_save_path']}", level="success")

            if 'mode3_save_path' in config:
                self.lfp_tab.mode3_file_path_label.setText(str(config['mode3_save_path']))
                _log(f"Mode3 path set to: {config['mode3_save_path']}", level="success")

            if 'mode1_save_path' in config:
                self.spike1ch_tab.mode1_file_path_label.setText(str(config['mode1_save_path']))
                _log(f"Mode1 path set to: {config['mode1_save_path']}", level="success")

            if 'mode2_save_path' in config:
                self.spike4ch_tab.file_path_label.setText(str(config['mode2_save_path']))
                _log(f"Mode2 path set to: {config['mode2_save_path']}", level="success")

            if 'video_save_path' in config:
                self.video_save_path = str(config['video_save_path'])
                self.save_path_label.setText(self.video_save_path)
                _log(f"Video path set to: {self.video_save_path}", level="success")

            if 'mice_id_directory' in config:
                if hasattr(self.habits_tab, 'habits_panel'):
                    directory = str(config['mice_id_directory'])
                    self.habits_tab.habits_panel.set_data_directory(directory)
                    _log(f"Mice ID directory set to: {directory}", level="success")
            elif 'mice_id_file_path' in config:
                if hasattr(self.habits_tab, 'habits_panel'):
                    directory = str(config['mice_id_file_path'])
                    if os.path.isfile(directory):
                        directory = os.path.dirname(directory)
                    self.habits_tab.habits_panel.set_data_directory(directory)
                    _log(f"Mice ID directory set to: {directory}", level="success")

            if 'habits_serial_port' in config and hasattr(self.habits_tab, 'habits_panel'):
                habits_port = str(config['habits_serial_port'])
                combo = self.habits_tab.habits_panel.serial_combo
                index = combo.findText(habits_port, Qt.MatchFlag.MatchContains)
                if index >= 0:
                    combo.setCurrentIndex(index)
                    _log(f"Habits serial port selected: {habits_port}", level="success")
                else:
                    _log(f"Warning: Habits serial port {habits_port} not found", level="warning")

            if 'rf_serial_port' in config:
                rf_port = str(config['rf_serial_port'])
                index = self.rf_serial_combo.findText(rf_port, Qt.MatchFlag.MatchContains)
                if index >= 0:
                    self.rf_serial_combo.setCurrentIndex(index)
                    _log(f"RF serial port selected: {rf_port}", level="success")
                else:
                    _log(f"Warning: RF serial port {rf_port} not found", level="warning")

            if 'charging_guard' in config and hasattr(self, 'configure_charging_guard'):
                self.configure_charging_guard(config['charging_guard'])

            main_serial_port = (
                config.get('main_serial_port')
                or config.get('neural_serial_port')
                or config.get('serial_port')
            )
            if main_serial_port:
                self.refresh_main_serial_ports()
                main_serial_port = str(main_serial_port)
                index = self.main_serial_combo.findData(main_serial_port)
                if index < 0:
                    index = self.main_serial_combo.findText(main_serial_port, Qt.MatchFlag.MatchContains)
                if index >= 0:
                    self.main_serial_combo.setCurrentIndex(index)
                    _log(f"Neural serial port selected: {main_serial_port}", level="success")
                    should_auto_connect = bool(config.get('auto_connect_main_serial', auto_connect_default))
                    if should_auto_connect and hasattr(self, 'connect_main_serial'):
                        self.connect_main_serial()
                else:
                    _log(f"Warning: Neural serial port {main_serial_port} not found", level="warning")

            _log("Profile configuration applied", level="success")
            self._refresh_battery_timeline_from_history()

        except Exception as e:
            self.log_message(f"Error loading profile config: {str(e)}", level="error")

    def tab_changed(self, index):
        """Handle tab switching event"""
        tab_titles = ["Mode0 LFP+MAND", "16 channels Spike", "IMU", "Raster/MAND", "single channel Spike", "Habits Tracking", "Impedance Test"]
        if 0 <= index < len(tab_titles):
            self.statusBar().showMessage(f"Current displayTab: {tab_titles[index]}")
        # print(f"Switched to tab: {index}")
    
    def start_sampling(self):
        """Start sampling"""
        # print("Sample begaining...")
        pass
        # self.start_sampling_button.setEnabled(False)
        # self.stop_sampling_button.setEnabled(True)
        
        self.statusBar().showMessage(f"Sampling")
    
    def stop_sampling(self):
        """Stop sampling"""
        # print("Stop sampling...")
        # self.start_sampling_button.setEnabled(True)
        # self.stop_sampling_button.setEnabled(False)
        self.statusBar().showMessage("Sample stop")
    
    
    def IMU_status_changed(self):
        """Update IMU status"""
        print(f"Send IMU status update command: {self.battery_status_combo.currentText()}")

    def _resolve_battery_history_path(self):
        panel = getattr(getattr(self, 'habits_tab', None), 'habits_panel', None)
        if panel is not None:
            selected_data_dir = getattr(panel, 'selected_data_dir', None)
            if selected_data_dir:
                return os.path.join(selected_data_dir, "battery_history.json")
            mouse_id_widget = getattr(panel, 'mouse_id_edit', None)
            if mouse_id_widget is not None:
                mouse_id = mouse_id_widget.text().strip()
                if mouse_id:
                    return os.path.join(
                        get_mouse_data_directory(mouse_id),
                        "battery_history.json",
                    )
        return os.path.join(get_data_directory(), "battery_history.json")

    def _refresh_battery_timeline_from_history(self, *_args):
        if not hasattr(self, 'battery_history_manager') or not hasattr(self, 'timeline_widget'):
            return
        recent_records = self.battery_history_manager.get_recent_records(hours=24)
        cutoff_ts = time.time() - 24 * 3600
        anomalies = []
        anomalies.extend(self.battery_history_manager.compute_mode0_hourly_anomalies())
        anomalies.extend(self.battery_history_manager.compute_mode3_stitched_anomalies())
        recent_anomalies = [
            anomaly for anomaly in anomalies
            if float(anomaly.get("point_time", 0.0)) >= cutoff_ts
        ]
        recent_anomalies.sort(key=lambda item: float(item.get("point_time", 0.0)))
        self.timeline_widget.rebuild_from_history(recent_records, recent_anomalies)

    def _schedule_battery_timeline_refresh(self, delay_ms=250):
        timer = getattr(self, '_battery_timeline_refresh_timer', None)
        if timer is None:
            self._refresh_battery_timeline_from_history()
            return
        timer.start(max(0, int(delay_ms)))

    def _format_battery_anomaly_message(self, anomaly):
        class_label = str(anomaly.get("class_label", "") or "unknown")
        real_start = float(anomaly.get("real_start", anomaly.get("bin_real_start", 0.0)) or 0.0)
        real_end = float(anomaly.get("real_end", anomaly.get("bin_real_end", 0.0)) or 0.0)
        start_text = datetime.fromtimestamp(real_start).strftime("%Y-%m-%d %H:%M")
        end_text = datetime.fromtimestamp(real_end).strftime("%Y-%m-%d %H:%M")
        observed_key = str(anomaly.get("metric_name", "") or "")
        if observed_key == "delta_rsoc_per_10min":
            observed_value = float(anomaly.get("delta_rsoc_per_10min", 0.0))
            observed_suffix = "%/10min"
        else:
            observed_value = float(anomaly.get("delta_rsoc_per_hour", 0.0))
            observed_suffix = "%/h"
        baseline_value = float(anomaly.get("baseline_median", 0.0))
        baseline_count = int(anomaly.get("baseline_count", 0) or 0)
        sample_count = int(anomaly.get("sample_count", 0) or 0)
        message = (
            f"Battery anomaly [{class_label}] {start_text} -> {end_text}: "
            f"{observed_value:+.2f} {observed_suffix} vs median {baseline_value:+.2f} "
            f"(baseline n={baseline_count}, samples={sample_count})"
        )
        if class_label == "mode3":
            segment_count = int(anomaly.get("segment_count", 0) or 0)
            message += f", stitched from {segment_count} segments"
        return message

    def _handle_timeline_anomaly_clicked(self, anomaly):
        message = self._format_battery_anomaly_message(anomaly)
        self.statusBar().showMessage(message, 10000)
        self.log_message(message, level="warning")
    
    def update_battery_indicator(self, level, Charging_STAT, Battery_Voltage):
        """Update battery indicator using device-reported RSOC."""
        decoded_stat = decode_bq25176_stat(Charging_STAT)
        color = "w"
        if decoded_stat is None:
            charge_text = "unknown charge"
            power_text = "unknown power"
        elif not decoded_stat.power_good:
            color = "#FF5252"  # 红色
            charge_text = decoded_stat.charge_state_text.lower()
            power_text = decoded_stat.power_state_text.lower()
        elif decoded_stat.stat_reports_charging:
            color = "#66BB6A"  # green
            charge_text = decoded_stat.charge_state_text.lower()
            power_text = decoded_stat.power_state_text.lower()
        else:
            color = "#d96c58"  # 橙色
            charge_text = decoded_stat.charge_state_text.lower()
            power_text = decoded_stat.power_state_text.lower()

        self.update_battery_button.setStyleSheet(f"background-color: {color}; color: black; border-radius: 4px; padding: 5px;")

        RSOC = f"{int(level)}%"

        # Keep a numeric mV value for logging/history, but support display
        # strings such as "3.90V" or "3900mV" coming from upstream code.
        voltage_mv = 0
        try:
            if isinstance(Battery_Voltage, str):
                v = Battery_Voltage.strip().lower()
                if v.endswith("mv"):
                    voltage_mv = float(v[:-2])
                elif v.endswith("v"):
                    voltage_mv = float(v[:-1]) * 1000.0
                else:
                    voltage_mv = float(v)
            else:
                voltage_mv = float(Battery_Voltage)
        except Exception:
            voltage_mv = 0

        if not np.isfinite(voltage_mv):
            voltage_mv = 0

        battery_voltage_text = f"{voltage_mv / 1000:.2f}V"

        self.update_battery_button.setText(RSOC + " " + battery_voltage_text)
        if decoded_stat is None:
            stat_tooltip = f"BQ25176 raw stat={Charging_STAT}: unknown PG/STAT"
        else:
            stat_tooltip = (
                f"BQ25176 raw stat={decoded_stat.raw_stat}: PG={decoded_stat.pg_raw}, "
                f"STAT={decoded_stat.stat_raw}; {charge_text}; {power_text}"
            )
        self.update_battery_button.setToolTip(
            "Device-reported RSOC with battery voltage\n" + stat_tooltip
        )
        
        if hasattr(self, 'battery_history_manager'):
            battery_capacity_mAh = float(getattr(self, "battery_capacity_mAh", 24.0) or 24.0)
            sample = {
                "timestamp_epoch": time.time(),
                "timestamp_iso": datetime.now().isoformat(),
                "rsoc": float(level),
                "battery_stat": int(Charging_STAT),
                "voltage_mv": int(round(voltage_mv)),
                "capacity_mAh": battery_capacity_mAh,
                "mode": getattr(self, 'current_recording_mode', "Idle"),
                "rf_status": getattr(self, 'rf_power_status', 1),
                "trial_paused": bool(getattr(self, 'mode3_trial_trigger_paused', False)),
            }
            accepted = self.battery_history_manager.record_sample(sample)
            if accepted:
                self._schedule_battery_timeline_refresh()
        elif hasattr(self, 'timeline_widget'):
            self.timeline_widget.update_data(
                time.time(),
                level,
                getattr(self, 'current_recording_mode', "Idle"),
                getattr(self, 'rf_power_status', 1),
                bool(getattr(self, 'mode3_trial_trigger_paused', False)),
                battery_capacity_mAh=float(getattr(self, "battery_capacity_mAh", 24.0) or 24.0),
            )
    
    def update_rssi(self, value):
        """Update RSSI indicator"""
        self.rssi_indicator.setText(f"RSSI: {-1 * value} dBm")
        if value < -90:
            self.rssi_indicator.setStyleSheet("""
                background-color: #FEE2E2; color: #991B1B;
                border: 1px solid #FCA5A5; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
        elif value < -70:
            self.rssi_indicator.setStyleSheet("""
                background-color: #FEF3C7; color: #92400E;
                border: 1px solid #FCD34D; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
        else:
            self.rssi_indicator.setStyleSheet("""
                background-color: #DCFCE7; color: #166534;
                border: 1px solid #86EFAC; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
    
    def update_packet_loss(self, value):
        """Update packet loss indicator"""
        self.packet_loss_indicator.setText(f"Loss: {value}%")
        if value > 10:
            self.packet_loss_indicator.setStyleSheet("""
                background-color: #FEE2E2; color: #991B1B;
                border: 1px solid #FCA5A5; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
        elif value > 5:
            self.packet_loss_indicator.setStyleSheet("""
                background-color: #FEF3C7; color: #92400E;
                border: 1px solid #FCD34D; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
        else:
            self.packet_loss_indicator.setStyleSheet("""
                background-color: #DCFCE7; color: #166534;
                border: 1px solid #86EFAC; border-radius: 6px; padding: 5px; font-weight: 600;
            """)
    
    def refresh_camera_list(self):
        """Refresh camera list"""
        previous_camera_id = self.camera_selection_combo.currentData()
        self.camera_selection_combo.clear()
        available_cameras = get_available_cameras()
        
        if not available_cameras:
            self.camera_selection_combo.addItem("No cameras found")
            self.toggle_camera_button.setEnabled(False)
            self.toggle_camera_display_button.setEnabled(False)
        else:
            selected_index = -1
            for camera in available_cameras:
                display_text = f"{camera['name']} ({camera['resolution']}, {camera['fps']:.1f}fps)"
                self.camera_selection_combo.addItem(display_text, camera['id'])
                if previous_camera_id == camera['id']:
                    selected_index = self.camera_selection_combo.count() - 1
            if selected_index >= 0:
                self.camera_selection_combo.setCurrentIndex(selected_index)
            self.toggle_camera_button.setEnabled(True)

    def refresh_main_serial_ports(self):
        self.main_serial_combo.clear()
        self.main_serial_combo.addItem("Select Neural Port", "")

        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            if ports:
                for port in ports:
                    display_text = f"{port.device} - {port.description}"
                    self.main_serial_combo.addItem(display_text, port.device)
            else:
                self.main_serial_combo.addItem("No ports available", "")
        except Exception:
            mock_ports = [f"COM{i}" for i in range(1, 10)]
            for port in mock_ports:
                self.main_serial_combo.addItem(port, port)

    def set_main_serial_ui_connected(self, connected, port_text=""):
        if connected:
            shown_port = port_text if port_text else "Connected"
            self.main_serial_connect_button.setText("Disconnect")
            self.main_serial_connect_button.setStyleSheet("""
                QPushButton {
                    background-color: #EF4444; color: white;
                    border: none; border-radius: 6px; padding: 6px; font-weight: 700;
                }
                QPushButton:hover { background-color: #DC2626; }
            """)
            self.main_serial_combo.setEnabled(False)
            if hasattr(self, "restart_relay_button"):
                self.restart_relay_button.setEnabled(True)
            if hasattr(self, "relay_esb_channel_combo"):
                self.relay_esb_channel_combo.setEnabled(True)
            if hasattr(self, "apply_relay_esb_channel_button"):
                self.apply_relay_esb_channel_button.setEnabled(True)
            self.main_serial_status_indicator.setText(f"● {shown_port}")
            self.main_serial_status_indicator.setStyleSheet("""
                background-color: #DCFCE7; color: #166534;
                border: 1px solid #86EFAC; border-radius: 6px; padding: 5px;
                font-weight: 700;
            """)
        else:
            self.main_serial_connect_button.setText("Connect")
            self.main_serial_connect_button.setStyleSheet("""
                QPushButton {
                    background-color: #3B82F6; color: white;
                    border: none; border-radius: 6px; padding: 6px; font-weight: 700;
                }
                QPushButton:hover { background-color: #2563EB; }
            """)
            self.main_serial_combo.setEnabled(True)
            if hasattr(self, "restart_relay_button"):
                self.restart_relay_button.setEnabled(False)
            if hasattr(self, "relay_esb_channel_combo"):
                self.relay_esb_channel_combo.setEnabled(False)
            if hasattr(self, "apply_relay_esb_channel_button"):
                self.apply_relay_esb_channel_button.setEnabled(False)
            self.main_serial_status_indicator.setText("○ Disconnected")
            self.main_serial_status_indicator.setStyleSheet("""
                background-color: #F1F5F9; color: #94A3B8;
                border: 1px solid #E2E8F0; border-radius: 6px; padding: 5px;
                font-weight: 600;
            """)
    
    def get_selected_camera_id(self):
        """Get selected camera ID"""
        current_data = self.camera_selection_combo.currentData()
        return current_data if current_data is not None else 0
    
    def refresh_rf_serial_ports(self):
        """Refresh RF serial port list"""
        self.rf_serial_combo.clear()
        self.rf_serial_combo.addItem("Select RF Port")
        
        try:
            import serial.tools.list_ports
            ports = list(serial.tools.list_ports.comports())
            if ports:
                for port in ports:
                    display_text = f"{port.device} - {port.description}"
                    self.rf_serial_combo.addItem(display_text, port.device)
            else:
                self.rf_serial_combo.addItem("No ports available")
        except ImportError:
            # 如果没有安装pyserial，添加模拟串口用于测试
            mock_ports = [f"COM{i}" for i in range(1, 10)]
            for port in mock_ports:
                self.rf_serial_combo.addItem(port, port)
    
    def toggle_rf_connection(self):
        """Toggle RF serial connection"""
        if self.rf_connected:
            self.disconnect_rf_serial()
        else:
            self.connect_rf_serial()
    

    
    def connect_rf_serial(self):
        """Connect RF control serial"""
        if getattr(self, "_rf_connecting", False):
            return
        if not getattr(self, "_rf_connect_result_connected", False):
            try:
                self.rf_connect_result.connect(self._finish_rf_serial_connect)
                self._rf_connect_result_connected = True
            except Exception:
                pass
        selected_port = self.rf_serial_combo.currentData()
        if selected_port is None:
            selected_port = self.rf_serial_combo.currentText()
            if " - " in selected_port:
                selected_port = selected_port.split(" - ", 1)[0]

        if selected_port == "Select RF Port" or selected_port == "No ports available" or not selected_port:
            self.log_message("Warning: Please select a valid RF serial port first", level="warning")
            return
        try:
            neural_port = getattr(self, "curr_active_ports", None)
            if getattr(self, "main_serial_connected", False) and neural_port and str(neural_port) == str(selected_port):
                self.log_message(
                    f"Error: RF port {selected_port} is already occupied by Neural serial. "
                    "Please disconnect Neural serial or choose another RF port.",
                    level="error"
                )
                return
        except Exception:
            pass
        
        self._rf_connecting = True
        self.rf_connect_button.setEnabled(False)
        self.rf_connect_button.setText("RF Connecting...")

        def _open_rf_port():
            conn = None
            error_text = ""
            try:
                import serial
                conn = serial.Serial(
                    selected_port,
                    9600,
                    timeout=0.1,
                    write_timeout=0.2
                )
            except Exception as e:
                error_text = str(e)
            try:
                self.rf_connect_result.emit(conn, str(selected_port), error_text)
            except RuntimeError:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass

        threading.Thread(target=_open_rf_port, name="rf-control-open", daemon=True).start()

    def _finish_rf_serial_connect(self, connection, selected_port, error_text):
        if not getattr(self, "_rf_connecting", False):
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
            return
        self._rf_connecting = False
        self.rf_connect_button.setEnabled(True)
        if error_text:
            self.log_message(f"Error: Failed to connect RF control serial: {str(error_text)}", level="error")
            self.rf_connected = False
            self.disconnect_rf_serial()
            return
        self.rf_serial_connection = connection
        self.rf_connected = True
        self.rf_power_on_button.setEnabled(True)
        self.rf_power_off_button.setEnabled(True)
        if hasattr(self, 'rf_status_button'):
            self.rf_status_button.setEnabled(True)
        self.rf_connect_button.setText("Disconnect RF")
        self.rf_connect_button.setStyleSheet("""
            QPushButton {
                background-color: #EF4444; color: white;
                border: none; border-radius: 6px; padding: 5px;
                font-weight: 700; font-size: 11px;
            }
            QPushButton:hover { background-color: #DC2626; }
        """)
        self.log_message(f"Success: RF control serial connected: {selected_port}", level="success")
    
    def disconnect_rf_serial(self):
        """Disconnect RF control serial"""
        self._rf_connecting = False
        if self.rf_serial_connection:
            try:
                self.rf_serial_connection.close()
            except Exception:
                pass
            self.rf_serial_connection = None
        self.rf_connected = False
        self.rf_connect_button.setEnabled(True)
        self.rf_power_on_button.setEnabled(False)
        self.rf_power_off_button.setEnabled(False)
        if hasattr(self, 'rf_status_button'):
            self.rf_status_button.setEnabled(False)
        self.rf_connect_button.setText("Connect RF")
        self.rf_connect_button.setStyleSheet("""
            QPushButton {
                background-color: #22C55E; color: white;
                border: none; border-radius: 6px; padding: 5px;
                font-weight: 700; font-size: 11px;
            }
            QPushButton:hover { background-color: #16A34A; }
        """)
        if hasattr(self, 'rf_status_indicator'):
            self.rf_status_indicator.setText("RF: --")
            self.rf_status_indicator.setStyleSheet("""
                background-color: #F1F5F9; color: #64748B;
                border: 1px solid #E2E8F0; border-radius: 6px; padding: 5px;
                font-weight: 600;
            """)
        self.log_message("Info: RF control serial disconnected", level="info")

    def closeEvent(self, event):
        """Window close event"""
        reply = QMessageBox.question(
            self, 
            'check quit', 
            'Are you sure to quit?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            
                # 处理关闭前的清理工作
                print("Main window closed.")
                event.accept()
        else:
            event.ignore()


    def toggle_camera(self):
        """Toggle camera on/off"""
        if not self.is_camera_on:
            # 获取选择的摄像头ID
            camera_id = self.get_selected_camera_id()
            
            # 打开摄像头
            if self.camera_module.open_camera(camera_id):
                self.is_camera_on = True
                self.toggle_camera_button.setText("Close camera")
                self.toggle_recording_button.setEnabled(True)
                self.toggle_camera_display_button.setEnabled(True)
                
                # 初始化独立摄像头窗口
                if not hasattr(self, 'camera_window') or self.camera_window is None:
                    self.camera_window = CameraWindow()
                    self.camera_window.closed.connect(self.on_camera_window_closed)
                
                # 默认不显示窗口，等待点击“Show camera”
            else:
                self.log_message("Error: Cannot open camera", level="error")
        else:
            if self.is_recording:
                try:
                    self.toggle_recording()
                except Exception:
                    try:
                        self.camera_module.stop_recording()
                    except Exception:
                        pass
                    self.is_recording = False
            # 关闭摄像头
            # 若显示开启，先停止更新并隐藏
            if self.is_camera_display_on:
                self.camera_timer.stop()
                self.is_camera_display_on = False
                self.toggle_camera_display_button.setText("Show camera")
            
            self.camera_module.close_camera()
            self.is_camera_on = False
            self.toggle_camera_button.setText("Open camera")
            self.toggle_recording_button.setEnabled(False)
            self.toggle_camera_display_button.setEnabled(False)
            
            # 关闭摄像头窗口
            if hasattr(self, 'camera_window') and self.camera_window is not None:
                self.camera_window.close()

    def on_camera_window_closed(self):
        """Handle camera window closed by user"""
        self.camera_timer.stop()
        self.is_camera_display_on = False
        self.toggle_camera_display_button.setText("Show camera")

    def toggle_camera_display(self):
        """Toggle camera display show/hide"""
        if not self.is_camera_on:
            self.log_message("Warning: Please open the camera first", level="warning")
            return
        
        if not self.is_camera_display_on:
            # Show camera window
            if hasattr(self, 'camera_window') and self.camera_window:
                self.camera_window.show()
                self.camera_timer.start(16)
                self.is_camera_display_on = True
                self.toggle_camera_display_button.setText("Hide camera")
        else:
            # Hide camera window
            if hasattr(self, 'camera_window') and self.camera_window:
                self.camera_window.hide()
            self.camera_timer.stop()
            self.is_camera_display_on = False
            self.toggle_camera_display_button.setText("Show camera")

    def clear_log(self):
        self.log_box.clear()

    def log_message(self, message, level="info"):
        timestamp = QDateTime.currentDateTime().toString("yyyy-MM-dd HH:mm:ss")
        formatted_message = format_log_html(message, level=level, timestamp=timestamp)
        self.log_box.append(formatted_message)
        
        # Limit log size to prevent memory issues
        doc = self.log_box.document()
        if doc.blockCount() > 1000:
            cursor = self.log_box.textCursor()
            cursor.movePosition(cursor.MoveOperation.Start)
            cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor, 100) # Remove top 100 lines
            cursor.removeSelectedText()
            
        # Scroll to bottom
        self.log_box.moveCursor(self.log_box.textCursor().MoveOperation.End)
            
        
    def update_camera_frame(self):
        """Update camera frame"""
        if not self.is_camera_display_on:
            return
        if hasattr(self, 'camera_window') and self.camera_window and self.camera_window.isVisible():
            # 当录制开启时，使用录制线程更新的最新帧，避免双线程抓帧
            if self.is_recording and self.camera_module.frame is not None:
                frame = self.camera_module.frame
            else:
                frame = self.camera_module.get_frame()
            if frame is not None:
                if hasattr(self, 'annotate_charging_guard_frame'):
                    frame = self.annotate_charging_guard_frame(frame)
                # 转换OpenCV图像为Qt图像
                # Reuse existing buffer if possible to avoid reallocation
                rgb_image = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_image.shape
                bytes_per_line = ch * w
                # Create QImage pointing to data (careful with lifetime)
                qt_image = QImage(rgb_image.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                # setPixmap creates a copy, which is fine, but make sure to not leak references
                self.camera_window.video_label.setPixmap(QPixmap.fromImage(qt_image))
                if hasattr(self.camera_window, 'detection_label') and hasattr(self, 'get_charging_guard_detection_text'):
                    self.camera_window.detection_label.setText(self.get_charging_guard_detection_text())
                
                # Force garbage collection if memory usage is high? Not usually recommended in Python unless critical.
                # Just ensure rgb_image and qt_image go out of scope.

    def _poll_camera_health(self):
        if not self.is_camera_on or self._camera_recovery_in_progress:
            return
        healthy, reason = self.camera_module.check_health(max_stale_seconds=4.0)
        if healthy:
            return
        self.log_message(f"Warning: Camera stream unhealthy: {reason}", level="warning")
        self._attempt_camera_recovery(reason)

    def _camera_restart_save_path(self):
        source_path = getattr(self, 'video_recording_source_path', None) or self.video_save_path
        if hasattr(self, '_build_video_recording_path'):
            try:
                return self._build_video_recording_path(source_path)
            except Exception:
                return source_path
        return source_path

    def _reset_camera_ui_after_failure(self):
        self.is_camera_on = False
        self.is_recording = False
        self.is_camera_display_on = False
        self.camera_timer.stop()
        self.toggle_camera_button.setText("Open camera")
        self.toggle_recording_button.setText("Start recording")
        self.toggle_recording_button.setEnabled(False)
        self.toggle_camera_display_button.setEnabled(False)
        self.toggle_camera_display_button.setText("Show camera")
        self.select_save_path_button.setEnabled(True)
        if hasattr(self, 'camera_window') and self.camera_window is not None:
            try:
                self.camera_window.hide()
                self.camera_window.video_label.setText("Camera recovering failed")
            except Exception:
                pass

    def _attempt_camera_recovery(self, reason):
        if self._camera_recovery_in_progress:
            return False
        self._camera_recovery_in_progress = True
        try:
            selected_camera_id = self.get_selected_camera_id()
            was_display_on = bool(self.is_camera_display_on)
            was_recording = bool(self.is_recording)
            restart_save_path = self._camera_restart_save_path() if was_recording else None

            self.camera_timer.stop()
            self.is_camera_display_on = False
            try:
                self.camera_module.close_camera()
            except Exception:
                pass

            reopen_ok = self.camera_module.open_camera(selected_camera_id)
            if not reopen_ok:
                self._reset_camera_ui_after_failure()
                self.refresh_camera_list()
                self.log_message(f"Error: Camera auto-recovery failed: {reason}", level="error")
                return False

            self.is_camera_on = True
            self.toggle_camera_button.setText("Close camera")
            self.toggle_recording_button.setEnabled(True)
            self.toggle_camera_display_button.setEnabled(True)

            if was_display_on and hasattr(self, 'camera_window') and self.camera_window is not None:
                try:
                    self.camera_window.show()
                except Exception:
                    pass
                self.camera_timer.start(16)
                self.is_camera_display_on = True
                self.toggle_camera_display_button.setText("Hide camera")
            else:
                self.toggle_camera_display_button.setText("Show camera")

            self.log_message(f"Success: Camera recovered automatically after failure: {reason}", level="success")

            if was_recording:
                restart_ok = self.camera_module.start_recording(restart_save_path)
                if restart_ok:
                    self.is_recording = True
                    self.toggle_recording_button.setText("Stop recording")
                    self.select_save_path_button.setEnabled(False)
                    if hasattr(self, 'video_segment_start_time'):
                        self.video_segment_start_time = time.time()
                    if hasattr(self, 'video_segment_timer'):
                        self.video_segment_timer.start()
                    self.log_message("Info: Video recording resumed after camera recovery", level="info")
                else:
                    self.is_recording = False
                    self.toggle_recording_button.setText("Start recording")
                    self.select_save_path_button.setEnabled(True)
                    if hasattr(self, 'video_segment_timer'):
                        self.video_segment_timer.stop()
                    if hasattr(self, 'video_segment_start_time'):
                        self.video_segment_start_time = None
                    self.log_message("Error: Camera recovered but recording could not resume", level="error")
            return True
        finally:
            self._camera_recovery_in_progress = False
        
    def toggle_recording(self):
        """Toggle recording"""
        if not self.is_recording:
            # Global Save Check
            if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
                self.log_message("Save Disabled: Global saving is disabled.", level="warning")
                QMessageBox.warning(self, "Save Disabled", "Global saving is disabled. Please enable it in the top control bar.")
                return

            # 开始录制
            if self.camera_module.start_recording(self.video_save_path):
                self.is_recording = True
                self.toggle_recording_button.setText("Stop recording")
                self.select_save_path_button.setEnabled(False)
        else:
            # 停止录制
            if self.camera_module.stop_recording():
                self.is_recording = False
                self.toggle_recording_button.setText("Start recording")
                self.select_save_path_button.setEnabled(True)
        
    def select_video_save_path(self):
        """Select video save path"""
        # 使用exe所在目录下的recordings文件夹作为默认路径
        from ..support.path_utils import get_recordings_directory
        default_dir = get_recordings_directory()
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save video file", default_dir, "AVI file (*.avi);;All files (*)"
        )
        if file_path:
            self.video_save_path = file_path
            self.save_path_label.setText(file_path)
    
    def closeEvent(self, event):
        """Window close event"""
        reply = QMessageBox.question(
            self, 
            'check quit', 
            'Are you sure to quit?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, 
            QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            # 确保关闭摄像头
            if self.is_recording:
                self.camera_module.stop_recording()
            if self.is_camera_on:
                self.camera_timer.stop()
                self.camera_module.close_camera()
            if hasattr(self, 'battery_history_manager'):
                try:
                    self.battery_history_manager.flush()
                except Exception:
                    pass
            # 关闭摄像头窗口
            if hasattr(self, 'camera_window') and self.camera_window:
                self.camera_window.close()
            
            # 断开RF串口连接
            if self.rf_connected:
                self.disconnect_rf_serial()
            
            # 处理关闭前的清理工作
            print("Main window closed.")
            event.accept()
        else:
            event.ignore()
