import sys
import os
import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                             QPushButton, QLabel, QFileDialog, QTableWidget, QTableWidgetItem, 
                             QHeaderView, QGroupBox, QLineEdit, QProgressBar, QMessageBox, QSplitter, QComboBox,
                             QTreeWidget, QTreeWidgetItem, QTreeWidgetItemIterator, QListView, QTreeView, QAbstractItemView,
                             QTabWidget)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QBrush, QFileSystemModel
import re
import gc

import analysis

import datetime
from scipy import signal

class SensorTimelineWindow(QMainWindow):
    def __init__(self, sensor_files, threshold_map, replacement_val, method, window_size):
        super().__init__()
        self.setWindowTitle("Sensor Timeline Analysis")
        self.resize(1200, 800)
        self.sensor_files = sensor_files
        self.threshold_map = threshold_map
        self.replacement_val = replacement_val
        self.method = method
        self.window_size = window_size
        self.data_bundle = None
        
        self.init_ui()
        self.load_data()
        
    def init_ui(self):
        # Enable optimizations for PyQtGraph
        pg.setConfigOptions(antialias=False) # Disable AA for speed
        
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        # Controls
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Smoothing Window (s):"))
        self.input_smooth = QLineEdit("1")
        self.input_smooth.returnPressed.connect(self.update_plots)
        control_layout.addWidget(self.input_smooth)
        
        btn_refresh = QPushButton("Refresh")
        btn_refresh.clicked.connect(self.update_plots)
        control_layout.addWidget(btn_refresh)
        
        layout.addLayout(control_layout)
        
        # Plot Layout
        self.plot_layout = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_layout)
        
        # Date Axis
        axis = pg.DateAxisItem(orientation='bottom')
        
        # Plot 1: Battery
        self.p_batt = self.plot_layout.addPlot(title="Battery Level", axisItems={'bottom': axis})
        self.p_batt.setLabel('left', 'Level')
        self.p_batt.showGrid(x=True, y=True)
        self.p_batt.addLegend()
        self.p_batt.setDownsampling(auto=True, mode='peak')
        self.p_batt.setClipToView(True)
        
        self.plot_layout.nextRow()
        
        # Plot 2: IMU
        # Share X axis with Battery
        axis2 = pg.DateAxisItem(orientation='bottom')
        self.p_imu = self.plot_layout.addPlot(title="IMU Acceleration", axisItems={'bottom': axis2})
        self.p_imu.setLabel('left', 'Acc (g)')
        self.p_imu.showGrid(x=True, y=True)
        self.p_imu.setXLink(self.p_batt)
        self.p_imu.addLegend()
        self.p_imu.setDownsampling(auto=True, mode='peak')
        self.p_imu.setClipToView(True)
        
    def load_data(self):
        self.data_bundle = analysis.get_sensor_timeline_data(self.sensor_files, self.threshold_map, self.replacement_val, self.method, self.window_size)
        if not self.data_bundle:
            QMessageBox.warning(self, "Warning", "No valid sensor data found.")
            return
            
        self.update_plots()
        
    def fast_moving_average(self, data, window_size, step=1, ignore_zeros=False):
        """
        Fast moving average using cumsum.
        
        Args:
            data (np.array): Input data (1D)
            window_size (int): Size of the window in samples
            step (int): Step size for downsampling (overlap = window_size - step)
            ignore_zeros (bool): If True, zeros are treated as invalid and ignored in mean calculation.
            
        Returns:
            tuple: (result_data, indices) - where indices are the original indices corresponding to result_data
        """
        n = len(data)
        if n < window_size:
            return data, np.arange(n)
            
        if ignore_zeros:
            # Mask zeros
            is_valid = (data != 0)
            valid_data = np.where(is_valid, data, 0.0)
            
            # Cumsum of values and counts
            # Use float64 to avoid overflow if needed, but data usually float
            cs_data = np.cumsum(np.insert(valid_data, 0, 0.0))
            cs_count = np.cumsum(np.insert(is_valid.astype(float), 0, 0.0))
            
            # Calculate window sums: sum[i:i+w] = cs[i+w] - cs[i]
            # Valid range for i is 0 to n-window_size
            window_sums = cs_data[window_size:] - cs_data[:-window_size]
            window_counts = cs_count[window_size:] - cs_count[:-window_size]
            
            # Avoid division by zero
            window_counts[window_counts == 0] = 1.0
            
            mavg = window_sums / window_counts
        else:
            # Standard moving average
            cs_data = np.cumsum(np.insert(data, 0, 0.0))
            window_sums = cs_data[window_size:] - cs_data[:-window_size]
            mavg = window_sums / window_size
            
        # The result corresponds to the window starting at i.
        # The center of the window [i, i+w] is i + w//2
        # So the time point should be t[i + w//2]
        center_indices = np.arange(len(mavg)) + window_size // 2
        
        # Downsample
        if step > 1:
            mavg = mavg[::step]
            center_indices = center_indices[::step]
            
        return mavg, center_indices

    def update_plots(self):
        if not self.data_bundle:
            return
            
        self.p_batt.clear()
        self.p_imu.clear()
        
        # Draw Dark Cycles
        min_ts = self.data_bundle['min_time']
        max_ts = self.data_bundle['max_time']
        
        # Iterate days from min_ts to max_ts
        start_dt = datetime.datetime.fromtimestamp(min_ts)
        end_dt = datetime.datetime.fromtimestamp(max_ts)
        
        # Start checking from the day before to cover cross-midnight
        curr_dt = start_dt.replace(hour=19, minute=0, second=0, microsecond=0) - datetime.timedelta(days=1)
        
        while curr_dt.timestamp() < max_ts:
            # Dark cycle start: 19:00
            # Dark cycle end: Next day 07:00
            dc_start = curr_dt
            dc_end = curr_dt + datetime.timedelta(hours=12) # 19:00 + 12h = 07:00
            
            # Draw Region
            # Use LinearRegionItem or just a rectangular ROI?
            # Or use a background fill.
            # LinearRegionItem is interactive, maybe just a semi-transparent box.
            # But plotting multiple items is easier.
            
            # Only draw if within range
            if dc_end.timestamp() > min_ts and dc_start.timestamp() < max_ts:
                # Add LinearRegionItem but static
                region = pg.LinearRegionItem([dc_start.timestamp(), dc_end.timestamp()], movable=False, brush=pg.mkBrush(0, 0, 0, 50))
                self.p_batt.addItem(region)
                
                region2 = pg.LinearRegionItem([dc_start.timestamp(), dc_end.timestamp()], movable=False, brush=pg.mkBrush(0, 0, 0, 50))
                self.p_imu.addItem(region2)
            
            curr_dt += datetime.timedelta(days=1)
            
        # Plot Battery
        # We plot each file as a separate segment but same color? Or different?
        # Requirement: "maintain a timeline... plot battery change"
        # Continuous line if gaps are small? Or segments.
        # Files might be discontinuous.
        
        try:
            smooth_win_s = float(self.input_smooth.text())
        except:
            smooth_win_s = 60.0
        
        for i, (t, v, fname) in enumerate(self.data_bundle['battery']):
            # Smooth Battery - ignore zeros
            if len(t) > 1:
                dt = (t[-1] - t[0]) / (len(t) - 1)
                if dt <= 0: dt = 1.0 # Fallback
                win_samples = int(smooth_win_s / dt)
                if win_samples < 1: win_samples = 1
                
                # Step size: reduce overlap to improve performance
                # If window is large, we can step more.
                # E.g., step = win_samples // 4 (75% overlap)
                step = max(1, win_samples // 4)
                
                v_s, indices = self.fast_moving_average(v, win_samples, step=step, ignore_zeros=True)
                
                # Slice time array
                # Ensure indices are within bounds
                valid_idx = indices[indices < len(t)]
                t_s = t[valid_idx]
                v_s = v_s[:len(valid_idx)]
                
            else:
                t_s, v_s = t, v
                
            # Ensure length match
            n_t = len(t_s)
            n_d = len(v_s)
            if n_t != n_d:
                n = min(n_t, n_d)
                t_s = t_s[:n]
                v_s = v_s[:n]

            # Color by file index to distinguish? Or single color?
            # "change on this axis" -> Single curve usually better, but maybe gaps.
            # Let's use green for battery.
            self.p_batt.plot(t_s, v_s, pen='g', name=fname if i==0 else None)
            
        # Plot IMU (Smoothed)
        # smooth_win_s already parsed above
            
        for i, (t, ax, ay, az, fname) in enumerate(self.data_bundle['imu']):
            # Calculate window size in samples
            # Avg dt
            if len(t) > 1:
                dt = (t[-1] - t[0]) / (len(t) - 1)
                if dt <= 0: dt = 0.001 # Safety
                win_samples = int(smooth_win_s / dt)
                if win_samples < 1: win_samples = 1
                
                step = max(1, win_samples // 4)
                
                ax_s, idx_x = self.fast_moving_average(ax, win_samples, step=step)
                ay_s, idx_y = self.fast_moving_average(ay, win_samples, step=step)
                az_s, idx_z = self.fast_moving_average(az, win_samples, step=step)
                
                # Indices should be same for all axes
                valid_idx = idx_x[idx_x < len(t)]
                t_s = t[valid_idx]
                
                # Ensure lengths match
                n = len(t_s)
                ax_s = ax_s[:n]
                ay_s = ay_s[:n]
                az_s = az_s[:n]
            else:
                t_s, ax_s, ay_s, az_s = t, ax, ay, az
            
            # Ensure lengths match before plotting
            n_t = len(t_s)
            n_d = len(ax_s)
            
            if n_t != n_d:
                # Truncate to shorter
                n = min(n_t, n_d)
                t_s = t_s[:n]
                ax_s = ax_s[:n]
                ay_s = ay_s[:n]
                az_s = az_s[:n]
                
            self.p_imu.plot(t_s, ax_s, pen='r', name="X" if i==0 else None)
            self.p_imu.plot(t_s, ay_s, pen='g', name="Y" if i==0 else None)
            self.p_imu.plot(t_s, az_s, pen='b', name="Z" if i==0 else None)

class LFPSpectrumWindow(QMainWindow):
    def __init__(self, files, threshold_map, replacement_val, method, window_size):
        super().__init__()
        self.setWindowTitle("LFP Spectrum Analysis (All Files)")
        self.resize(1000, 700)
        self.files = files
        self.threshold_map = threshold_map
        self.replacement_val = replacement_val
        self.method = method
        self.window_size = window_size
        
        self.psd_results = {'Mode0': {}, 'Mode3': {}} # mode -> {channel: (sum_psd, count)}
        self.freqs = None
        
        self.init_ui()
        self.load_data()

    def init_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        # Controls
        control_layout = QHBoxLayout()
        control_layout.addWidget(QLabel("Max Frequency (Hz):"))
        self.input_max_freq = QLineEdit("100")
        self.input_max_freq.returnPressed.connect(self.update_plot)
        control_layout.addWidget(self.input_max_freq)
        
        btn_refresh = QPushButton("Refresh Plot")
        btn_refresh.clicked.connect(self.update_plot)
        control_layout.addWidget(btn_refresh)
        
        layout.addLayout(control_layout)
        
        self.status_label = QLabel("Initializing...")
        layout.addWidget(self.status_label)
        
        # Plot
        self.plot_widget = pg.PlotWidget(title="LFP Power Spectral Density")
        self.plot_widget.setLabel('left', 'PSD (dB/Hz)')
        self.plot_widget.setLabel('bottom', 'Frequency (Hz)')
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        layout.addWidget(self.plot_widget)

    def load_data(self):
        total = len(self.files)
        
        for i, fpath in enumerate(self.files):
            self.status_label.setText(f"Processing {i+1}/{total}: {os.path.basename(fpath)}")
            QApplication.processEvents()
            
            # Determine Mode
            mode = "Mode0" # Default?
            if "mode3" in fpath.lower() or "mode 3" in fpath.lower():
                mode = "Mode3"
            elif "mode0" in fpath.lower() or "mode 0" in fpath.lower():
                mode = "Mode0"
            else:
                # If unknown, maybe group into Mode0 or separate? 
                # User asked for Mode0 and Mode3. Let's assume Mode0 if not specified or check path?
                # Let's keep separate "Unknown" or just map to Mode0?
                # User said: "For data from Mode0 and Mode3... separate average".
                # If file is neither, maybe skip or add to Unknown?
                # Let's add to Mode0 for now or treat as Unknown.
                mode = "Mode0" 

            bundle, msg = analysis.get_lfp_spectrum_data(fpath, self.threshold_map, self.replacement_val, self.method, self.window_size)
            
            if not bundle:
                print(f"Skipping {fpath}: {msg}")
                continue
                
            fs = bundle['fs']
            data = bundle['data']
            labels = bundle['labels']
            
            for ch_idx, ch_data in enumerate(data):
                f, Pxx = signal.welch(ch_data, fs, nperseg=1024)
                
                if self.freqs is None:
                    self.freqs = f
                
                # Check frequency consistency? usually same fs means same f
                
                label = labels[ch_idx]
                
                if label not in self.psd_results[mode]:
                    self.psd_results[mode][label] = [Pxx, 1]
                else:
                    self.psd_results[mode][label][0] += Pxx
                    self.psd_results[mode][label][1] += 1
                    
        self.status_label.setText("Processing Complete. Generating Plot...")
        self.update_plot()
        
    def update_plot(self):
        self.plot_widget.clear()
        
        try:
            max_freq = float(self.input_max_freq.text())
        except ValueError:
            max_freq = 100.0
            
        if self.freqs is None:
            return
            
        mask = self.freqs <= max_freq
        plot_freqs = self.freqs[mask]
        
        # Helper to get color
        # We want same color for same channel across modes
        # Collect all unique channels
        all_channels = set()
        for mode in self.psd_results:
            all_channels.update(self.psd_results[mode].keys())
        
        sorted_channels = sorted(list(all_channels))
        color_map = {}
        for idx, ch in enumerate(sorted_channels):
            color_map[ch] = pg.intColor(idx, hues=len(sorted_channels))
            
        # Plot Mode0
        for ch, (psd_sum, count) in self.psd_results['Mode0'].items():
            avg_psd = psd_sum / count
            avg_psd_db = 10 * np.log10(avg_psd + 1e-10)
            
            # Mode0: Solid, Thick
            pen = pg.mkPen(color_map[ch], width=3, style=Qt.PenStyle.SolidLine)
            self.plot_widget.plot(plot_freqs, avg_psd_db[mask], pen=pen, name=f"{ch} (Mode0)")
            
        # Plot Mode3
        for ch, (psd_sum, count) in self.psd_results['Mode3'].items():
            avg_psd = psd_sum / count
            avg_psd_db = 10 * np.log10(avg_psd + 1e-10)
            
            # Mode3: Dash, Thin? User said "different thickness and style"
            # Mode3: Dash, width 2
            pen = pg.mkPen(color_map[ch], width=2, style=Qt.PenStyle.DashLine)
            self.plot_widget.plot(plot_freqs, avg_psd_db[mask], pen=pen, name=f"{ch} (Mode3)")
            
        self.status_label.setText("Plot Updated.")

class ESADataWindow(QMainWindow):
    def __init__(self, files, threshold_map, replacement_val, method, window_size):
        super().__init__()
        self.setWindowTitle("ESA Data Global Analysis")
        self.resize(1200, 800)
        self.files = files
        self.threshold_map = threshold_map
        self.replacement_val = replacement_val
        self.method = method
        self.window_size = window_size
        
        # Data storage for global analysis
        self.corr_matrices = [] # List of matrices
        self.corr_labels = None
        
        self.psd_accum = {} # channel -> (sum_psd, count)
        self.freqs = None
        
        self.baselines = {} # channel -> list of (time, baseline_value, mode)
        
        self.init_ui()
        self.load_data()

    def init_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        self.status_label = QLabel("Initializing...")
        layout.addWidget(self.status_label)
        
        # Tabs for different analyses
        tabs = QTabWidget()
        layout.addWidget(tabs)
        
        # Tab 1: Correlation Heatmap
        self.tab_corr = QWidget()
        t1_layout = QVBoxLayout(self.tab_corr)
        self.plot_corr = pg.PlotItem(title="Average ESA Correlation Matrix")
        self.heatmap_view = pg.ImageView(view=self.plot_corr)
        
        # Color bar on top? 
        # pyqtgraph ImageView histogram is usually on the right or bottom.
        # Moving it to top requires custom layout.
        # But we can just use the histogram widget and add it to layout manually.
        # ImageView creates its own layout. Let's customize it.
        # Actually, let's just use the default right side for now as moving to top is complex in ImageView.
        # Wait, user explicitly asked "color bar 画在上面" (top).
        # We can hide the default histogram and create a separate HistogramLUTWidget.
        
        self.heatmap_view.ui.histogram.hide() # Hide default
        self.heatmap_view.ui.roiBtn.hide()
        self.heatmap_view.ui.menuBtn.hide()
        
        # Custom Histogram at Top
        self.hist_widget = pg.HistogramLUTWidget(orientation='horizontal')
        self.hist_widget.setImageItem(self.heatmap_view.getImageItem())
        self.hist_widget.setFixedHeight(60)
        t1_layout.addWidget(self.hist_widget)
        
        t1_layout.addWidget(self.heatmap_view)
        tabs.addTab(self.tab_corr, "Correlation Matrix")
        
        # Tab 2: Spectrum (0-150Hz)
        self.tab_spec = QWidget()
        t2_layout = QVBoxLayout(self.tab_spec)
        self.plot_spec = pg.PlotWidget(title="ESA Spectrum (All Files, <150Hz)")
        self.plot_spec.setLabel('left', 'PSD (dB/Hz)')
        self.plot_spec.setLabel('bottom', 'Frequency (Hz)')
        self.plot_spec.showGrid(x=True, y=True)
        self.plot_spec.addLegend()
        t2_layout.addWidget(self.plot_spec)
        tabs.addTab(self.tab_spec, "Spectrum Analysis")
        
        # Tab 3: Baseline Trend
        self.tab_base = QWidget()
        t3_layout = QVBoxLayout(self.tab_base)
        self.plot_base = pg.PlotWidget(title="ESA Baseline Trend")
        self.plot_base.setLabel('left', 'Baseline Amplitude')
        self.plot_base.setLabel('bottom', 'Time')
        self.plot_base.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        self.plot_base.showGrid(x=True, y=True)
        self.plot_base.addLegend()
        t3_layout.addWidget(self.plot_base)
        tabs.addTab(self.tab_base, "Baseline Trend")

    def load_data(self):
        total = len(self.files)
        date_pattern = r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})"
        
        for i, fpath in enumerate(self.files):
            self.status_label.setText(f"Processing {i+1}/{total}: {os.path.basename(fpath)}")
            QApplication.processEvents()
            
            # Timestamp
            fname = os.path.basename(fpath)
            match = re.search(date_pattern, fname)
            ts = 0
            if match:
                try:
                    dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
                    ts = dt.timestamp()
                except: pass
                
            # Mode
            mode = "Mode0"
            if "mode3" in fname.lower() or "mode 3" in fname.lower():
                mode = "Mode3"
            
            # Get Data
            bundle, msg = analysis.get_esa_data(fpath, self.threshold_map, self.replacement_val, self.method, self.window_size)
            if not bundle:
                print(f"Skipping {fname}: {msg}")
                continue
                
            fs = bundle['fs']
            data = bundle['data']
            labels = bundle['labels']
            
            # 1. Correlation
            corr, _ = analysis.calculate_esa_correlation(data, labels)
            self.corr_matrices.append(corr)
            if self.corr_labels is None:
                self.corr_labels = labels
                
            # 2. Spectrum (No Re-reference)
            for ch_idx, ch_data in enumerate(data):
                f, Pxx = signal.welch(ch_data, fs, nperseg=1024)
                
                if self.freqs is None:
                    self.freqs = f
                    
                label = labels[ch_idx]
                if label not in self.psd_accum:
                    self.psd_accum[label] = [Pxx, 1]
                else:
                    self.psd_accum[label][0] += Pxx
                    self.psd_accum[label][1] += 1
                    
            # 3. Baseline
            baselines, _ = analysis.get_esa_baseline(data, labels)
            for ch_idx, val in enumerate(baselines):
                label = labels[ch_idx]
                if label not in self.baselines:
                    self.baselines[label] = []
                self.baselines[label].append((ts, val, mode))
                
        self.status_label.setText("Processing Complete. Generating Plots...")
        self.update_plots()
        
    def update_plots(self):
        # 1. Heatmap
        if self.corr_matrices:
            avg_corr = np.mean(np.array(self.corr_matrices), axis=0)
            self.heatmap_view.setImage(avg_corr)
            self.hist_widget.setImageItem(self.heatmap_view.getImageItem()) # Update hist
            
            # Add labels to axes?
            if self.corr_labels:
                ticks = [(i, label) for i, label in enumerate(self.corr_labels)]
                self.plot_corr.getAxis('bottom').setTicks([ticks])
                self.plot_corr.getAxis('left').setTicks([ticks])
                
            # Add Text Labels for Correlation Values
            # Remove old text items first
            for item in self.plot_corr.items[:]:
                 if isinstance(item, pg.TextItem):
                     self.plot_corr.removeItem(item)
            
            n_rows, n_cols = avg_corr.shape
            for r in range(n_rows):
                for c in range(n_cols):
                    val = avg_corr[r, c]
                    # Color logic: black text on light bg, white on dark?
                    # Simple: White text with black border
                    text = pg.TextItem(f"{val:.2f}", color=(255, 255, 255), anchor=(0.5, 0.5))
                    # text.setHtml(f'<div style="text-align: center"><span style="color: #FFF; background-color: #000;">{val:.2f}</span></div>')
                    text.setPos(c, r) # x=col, y=row. Note ImageView transpose? 
                    # Usually image is (x, y). row is y? 
                    # Let's check orientation.
                    self.plot_corr.addItem(text)
        
        # 2. Spectrum
        self.plot_spec.clear()
        if self.freqs is not None:
            mask = self.freqs <= 150.0
            plot_freqs = self.freqs[mask]
            
            sorted_channels = sorted(self.psd_accum.keys())
            for idx, ch in enumerate(sorted_channels):
                psd_sum, count = self.psd_accum[ch]
                avg_psd = psd_sum / count
                avg_psd_db = 10 * np.log10(avg_psd + 1e-10)
                
                color = pg.intColor(idx, hues=len(sorted_channels))
                self.plot_spec.plot(plot_freqs, avg_psd_db[mask], pen=color, name=ch)
                
        # 3. Baseline Trend
        self.plot_base.clear()
        sorted_channels = sorted(self.baselines.keys())
        
        for idx, ch in enumerate(sorted_channels):
            data = self.baselines[ch]
            # Sort by time
            data.sort(key=lambda x: x[0])
            
            t = [x[0] for x in data]
            v = [x[1] for x in data]
            modes = [x[2] for x in data]
            
            # Base line color
            color = pg.intColor(idx, hues=len(sorted_channels))
            self.plot_base.plot(t, v, pen=color, name=ch)
            
            # Add scatter for modes? Or just line?
            # User didn't specify mode distinction for baseline, but "show changes".
            # Just line is cleaner if many channels.
            
        self.status_label.setText("Analysis Loaded.")

class RawHighPassWindow(QMainWindow):
    def __init__(self, files, threshold_map, replacement_val, method, window_size):
        super().__init__()
        self.setWindowTitle("Raw HighPass RMS Trend Analysis")
        self.resize(1000, 600)
        self.files = files
        self.threshold_map = threshold_map
        self.replacement_val = replacement_val
        self.method = method
        self.window_size = window_size
        
        self.init_ui()
        self.load_data()
        
    def init_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        self.info_label = QLabel("Calculating RMS for all files...")
        layout.addWidget(self.info_label)
        
        self.plot_widget = pg.PlotWidget(title="RMS Trend (>300Hz HighPass)")
        self.plot_widget.setLabel('left', 'RMS Amplitude (uV)')
        self.plot_widget.setLabel('bottom', 'Time')
        self.plot_widget.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        self.plot_widget.showGrid(x=True, y=True)
        self.plot_widget.addLegend()
        layout.addWidget(self.plot_widget)
        
    def load_data(self):
        # This might take time, should ideally be in a thread.
        # For now, run in main thread but update UI.
        
        rms_data = []
        total = len(self.files)
        
        date_pattern = r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})"
        
        for i, fpath in enumerate(self.files):
            self.info_label.setText(f"Processing {i+1}/{total}: {os.path.basename(fpath)}")
            QApplication.processEvents()
            
            # Extract timestamp from filename
            fname = os.path.basename(fpath)
            match = re.search(date_pattern, fname)
            ts = 0
            if match:
                try:
                    dt = datetime.datetime.strptime(match.group(1), "%Y-%m-%d-%H-%M-%S")
                    ts = dt.timestamp()
                except:
                    pass
            
            # Calculate RMS
            rms, msg = analysis.get_raw_highpass_rms(fpath, self.threshold_map, self.replacement_val, 300.0, self.method, self.window_size)
            
            if rms is not None:
                rms_data.append((ts, rms, fname))
            else:
                print(f"Skipping {fname}: {msg}")
                
        if not rms_data:
            self.info_label.setText("No valid data found.")
            return
            
        # Sort by timestamp
        rms_data.sort(key=lambda x: x[0])
        
        t = [x[0] for x in rms_data]
        v = [x[1] for x in rms_data]
        
        self.plot_widget.plot(t, v, pen=pg.mkPen('w', width=2), symbol='o', name="RMS")
        self.info_label.setText(f"Processed {len(rms_data)} files.")

class PulseVisualizationWindow(QMainWindow):
    def __init__(self, file_path, pulse_freq_hz=5000.0):
        super().__init__()
        self.setWindowTitle(f"Pulse Analysis - {os.path.basename(file_path)}")
        self.resize(1000, 600)
        self.file_path = file_path
        self.pulse_freq_hz = float(pulse_freq_hz)
        self.delay_labels = []
        
        self.init_ui()
        self.load_data()
        
    def init_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        # Graphs
        self.plot_widget = pg.GraphicsLayoutWidget()
        layout.addWidget(self.plot_widget)
        
        # Plot 1: Neural (Filtered)
        self.p1 = self.plot_widget.addPlot(title="Neural Signal (Filtered)")
        self.p1.setLabel('left', 'Amplitude', units='uV')
        self.p1.showGrid(x=True, y=True)
        self.curve1 = self.p1.plot(pen='w')
        self.scatter_neural = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None), brush=pg.mkBrush(255, 255, 0, 150))
        self.p1.addItem(self.scatter_neural)
        
        self.plot_widget.nextRow()
        
        # Plot 2: Alignment
        self.p2 = self.plot_widget.addPlot(title="Alignment Signal")
        self.p2.setLabel('left', 'Amplitude')
        self.p2.setLabel('bottom', 'Time', units='s')
        self.p2.showGrid(x=True, y=True)
        self.p2.setXLink(self.p1) # Link X axes
        
        self.curve2 = self.p2.plot(pen='c')
        
        # Markers for Alignment
        # Matched -> Green
        self.scatter_matched = pg.ScatterPlotItem(size=12, symbol='o', pen=pg.mkPen('g'), brush=pg.mkBrush(0, 255, 0, 200))
        self.p2.addItem(self.scatter_matched)
        
        # Missed -> Red
        self.scatter_missed = pg.ScatterPlotItem(size=12, symbol='x', pen=pg.mkPen('r'), brush=pg.mkBrush(255, 0, 0, 200))
        self.p2.addItem(self.scatter_missed)

        # Legend
        legend = pg.LegendItem((100,60), offset=(70,30))
        legend.setParentItem(self.p2.graphicsItem())
        legend.addItem(self.scatter_matched, 'Matched Pulse')
        legend.addItem(self.scatter_missed, 'Missed Pulse')
        
    def load_data(self):
        # Run analysis to get details
        details, msg = analysis.get_pulse_detection_details(
            self.file_path,
            pulse_freq_hz=self.pulse_freq_hz
        )
        
        if not details:
            QMessageBox.critical(self, "Error", f"Failed to load pulse data: {msg}")
            self.close()
            return
            
        fs = details['fs']
        t = np.arange(len(details['filtered_data'])) / fs
        
        # Plot Neural
        self.curve1.setData(t, details['filtered_data'])
        
        # Neural Pulses (Matched Only)
        # matches is a dict: align_idx (int) -> neural_idx (int)
        matched_neural_indices = list(details['matches'].values())
        
        if len(matched_neural_indices) > 0:
            matched_neural_indices = np.array(matched_neural_indices)
            neural_t = matched_neural_indices / fs
            neural_y = details['filtered_data'][matched_neural_indices]
            self.scatter_neural.setData(neural_t, neural_y)
        else:
            self.scatter_neural.clear()
            
        # Plot Alignment
        self.curve2.setData(t, details['align_data'])
        
        # Alignment Pulses
        align_indices = details['align_indices']
        matches = details['matches'] # align_idx -> neural_idx
        delays_ms = details.get('delays_ms', {})
        
        matched_t = []
        matched_y = []
        missed_t = []
        missed_y = []
        
        for label in self.delay_labels:
            self.p2.removeItem(label)
        self.delay_labels = []
        
        for i, idx in enumerate(align_indices):
            t_val = idx / fs
            y_val = details['align_data'][idx]
            
            if i in matches:
                matched_t.append(t_val)
                matched_y.append(y_val)
                delay_ms = delays_ms.get(i)
                if delay_ms is not None:
                    text_item = pg.TextItem(text=f"{delay_ms:.1f} ms", color='y', anchor=(0.5, 1.2))
                    text_item.setPos(t_val, y_val)
                    self.p2.addItem(text_item)
                    self.delay_labels.append(text_item)
            else:
                missed_t.append(t_val)
                missed_y.append(y_val)
                
        if matched_t:
            self.scatter_matched.setData(matched_t, matched_y)
        if missed_t:
            self.scatter_missed.setData(missed_t, missed_y)

class AnalysisWorker(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, files, threshold_map, replacement_val, method, window_size, pulse_freq_hz):
        super().__init__()
        self.files = files
        self.threshold_map = threshold_map
        self.replacement_val = replacement_val
        self.method = method
        self.window_size = window_size
        self.pulse_freq_hz = float(pulse_freq_hz)

    def run(self):
        results = {}
        total_files = len(self.files)
        
        for i, file_path in enumerate(self.files):
            try:
                file_name = os.path.basename(file_path)
                
                # 1. Data Loss Analysis
                loss_stats = analysis.calculate_data_loss(file_path, self.threshold_map, default_threshold=-1000, 
                                                          replacement_value=self.replacement_val,
                                                          method=self.method, window_size=self.window_size)
                
                # 2. Sync Pulse Detection (if applicable)
                # Check if it's a "raw" file (simple heuristic: name contains 'raw' or structure check inside)
                # We'll let the function decide or check filename
                sync_stats = None
                sync_stats_energy = None
                if 'raw' in file_name.lower():
                     sync_stats = analysis.detect_sync_success_rate(
                         file_path,
                         pulse_freq_hz=self.pulse_freq_hz,
                         method='band'
                     )
                     sync_stats_energy = analysis.detect_sync_success_rate(
                         file_path,
                         pulse_freq_hz=self.pulse_freq_hz,
                         method='energy'
                     )
                
                results[file_path] = {
                    'loss_stats': loss_stats,
                    'sync_stats': sync_stats,
                    'sync_stats_energy': sync_stats_energy
                }
                
                self.progress.emit(int((i + 1) / total_files * 100))
                
                # Optimization: Release memory
                gc.collect()
                
            except Exception as e:
                self.error.emit(f"Error processing {file_path}: {str(e)}")
        
        self.finished.emit(results)

class StatsTrendWindow(QMainWindow):
    def __init__(self, file_results):
        super().__init__()
        self.setWindowTitle("Statistics Trend Analysis")
        self.resize(1000, 600)
        self.file_results = file_results
        
        self.init_ui()
        self.plot_data()
        
    def init_ui(self):
        widget = QWidget()
        self.setCentralWidget(widget)
        layout = QVBoxLayout(widget)
        
        # Graphics Layout
        self.win = pg.GraphicsLayoutWidget(title="Statistics Trend Analysis")
        layout.addWidget(self.win)
        
        # Subplot 1: Data Loss Rate
        self.p_loss = self.win.addPlot(title="Data Loss Rate (%)")
        self.p_loss.setLabel('left', 'Loss %')
        self.p_loss.showGrid(x=True, y=True)
        self.p_loss.addLegend()
        self.p_loss.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        
        self.win.nextRow()
        
        # Subplot 2: Sync Success Rate
        self.p_sync = self.win.addPlot(title="Sync Success Rate (%)")
        self.p_sync.setLabel('left', 'Success %')
        self.p_sync.showGrid(x=True, y=True)
        self.p_sync.addLegend()
        self.p_sync.setXLink(self.p_loss)
        self.p_sync.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        
        self.win.nextRow()
        
        # Subplot 3: Battery Status (PG & Charging)
        self.p_batt_stat = self.win.addPlot(title="Battery Status (Power Good & Charging %)")
        self.p_batt_stat.setLabel('left', 'Percentage %')
        self.p_batt_stat.showGrid(x=True, y=True)
        self.p_batt_stat.addLegend()
        self.p_batt_stat.setXLink(self.p_loss)
        self.p_batt_stat.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        
        self.win.nextRow()
        
        # Subplot 4: RSOC (Start & End)
        self.p_rsoc = self.win.addPlot(title="Battery Level (RSOC %)")
        self.p_rsoc.setLabel('left', 'Level %')
        self.p_rsoc.showGrid(x=True, y=True)
        self.p_rsoc.addLegend()
        self.p_rsoc.setXLink(self.p_loss)
        self.p_rsoc.setAxisItems({'bottom': pg.DateAxisItem(orientation='bottom')})
        
    def plot_data(self):
        if not self.file_results:
            return
            
        # Extract data and sort by time
        data_points = []
        
        # Date pattern: YYYY-MM-DD-HH-MM-SS
        date_pattern = r"(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})"
        
        for fpath, res in self.file_results.items():
            fname = os.path.basename(fpath)
            match = re.search(date_pattern, fname)
            
            ts = 0
            if match:
                dt_str = match.group(1)
                try:
                    dt = datetime.datetime.strptime(dt_str, "%Y-%m-%d-%H-%M-%S")
                    ts = dt.timestamp()
                except:
                    ts = 0
            
            # Determine Mode
            mode = "Unknown"
            if "mode0" in fpath.lower() or "mode 0" in fpath.lower():
                mode = "Mode0"
            elif "mode3" in fpath.lower() or "mode 3" in fpath.lower():
                mode = "Mode3"
            
            # Extract metrics
            # 1. Avg Loss Rate
            loss_stats = res['loss_stats']
            total_loss = 0
            count = 0
            
            pg_ratio = None
            chg_ratio = None
            rsoc_start = None
            rsoc_end = None
            
            for ch_name, ch_res in loss_stats.items():
                total_loss += ch_res['loss_rate']
                count += 1
                
                if 'charging_stats' in ch_res:
                    pg_ratio = ch_res['charging_stats']['pg_ratio'] * 100
                    chg_ratio = ch_res['charging_stats']['charging_ratio'] * 100
                    
                if 'rsoc_stats' in ch_res:
                    rsoc_start = ch_res['rsoc_stats']['start']
                    rsoc_end = ch_res['rsoc_stats']['end']
            
            avg_loss = (total_loss / count * 100) if count > 0 else 0
            
            sync_rate = None
            if res['sync_stats']:
                sync_rate = res['sync_stats'][0] * 100
            
            data_points.append({
                'ts': ts,
                'fname': fname,
                'mode': mode,
                'avg_loss': avg_loss,
                'sync_rate': sync_rate,
                'pg': pg_ratio,
                'charging': chg_ratio,
                'rsoc_start': rsoc_start,
                'rsoc_end': rsoc_end
            })
            
        # Sort by timestamp
        data_points.sort(key=lambda x: x['ts'])
        
        # Prepare arrays for lines (Time sorted)
        t = [x['ts'] for x in data_points]
        
        # Helper to add scatter points based on mode
        def add_mode_scatter(plot_item, t_data, y_data, modes, name_prefix=""):
            # Split by mode
            t_m0, y_m0 = [], []
            t_m3, y_m3 = [], []
            t_unk, y_unk = [], []
            
            for i, mode in enumerate(modes):
                if y_data[i] is None: continue
                
                if mode == "Mode0":
                    t_m0.append(t_data[i])
                    y_m0.append(y_data[i])
                elif mode == "Mode3":
                    t_m3.append(t_data[i])
                    y_m3.append(y_data[i])
                else:
                    t_unk.append(t_data[i])
                    y_unk.append(y_data[i])
            
            # Add Scatter Items
            # Mode 0: Red Circle
            if t_m0:
                scatter = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None), brush=pg.mkBrush(255, 0, 0, 200), symbol='o')
                scatter.addPoints(t_m0, y_m0)
                plot_item.addItem(scatter)
                # Hack for Legend: add a dummy plot if needed, or just rely on scatter being added?
                # ScatterPlotItem doesn't show up in legend easily unless passed to addPlot(name=...).
                # But we are adding Item.
                # Let's add dummy hidden plots for legend if this is the first plot (p_loss)
                if plot_item == self.p_loss:
                    plot_item.plot([t_m0[0]], [y_m0[0]], pen=None, symbol='o', symbolBrush='r', name="Mode 0")

            # Mode 3: Blue Triangle
            if t_m3:
                scatter = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None), brush=pg.mkBrush(0, 0, 255, 200), symbol='t')
                scatter.addPoints(t_m3, y_m3)
                plot_item.addItem(scatter)
                if plot_item == self.p_loss:
                    plot_item.plot([t_m3[0]], [y_m3[0]], pen=None, symbol='t', symbolBrush='b', name="Mode 3")
            
            # Unknown: Gray Square
            if t_unk:
                scatter = pg.ScatterPlotItem(size=8, pen=pg.mkPen(None), brush=pg.mkBrush(100, 100, 100, 200), symbol='s')
                scatter.addPoints(t_unk, y_unk)
                plot_item.addItem(scatter)

        # Plot 1: Loss
        loss = [x['avg_loss'] for x in data_points]
        modes = [x['mode'] for x in data_points]
        # Line (Gray)
        self.p_loss.plot(t, loss, pen=pg.mkPen(200, 200, 200, width=2))
        # Points
        add_mode_scatter(self.p_loss, t, loss, modes)
        
        # Plot 2: Sync
        # Filter None
        sync_data = [(x['ts'], x['sync_rate'], x['mode']) for x in data_points if x['sync_rate'] is not None]
        if sync_data:
            t_s = [x[0] for x in sync_data]
            v_s = [x[1] for x in sync_data]
            m_s = [x[2] for x in sync_data]
            self.p_sync.plot(t_s, v_s, pen=pg.mkPen(200, 200, 200, width=2))
            add_mode_scatter(self.p_sync, t_s, v_s, m_s)
            
        # Plot 3: Battery Status
        # User request: Scatter plot only, no lines. Different shapes for PG vs Charging.
        # Different colors for Mode0 vs Mode3.
        
        # Helper to add status scatter
        def add_status_scatter(plot_item, t_data, y_data, modes, status_type):
            # status_type: "PG" or "Charging"
            # Mode0 Color: Red, Mode3 Color: Blue
            # PG Shape: Square (s)
            # Charging Shape: Triangle (t) or Star? Let's use Triangle for Charging.
            
            symbol = 's' if status_type == "PG" else 't'
            
            # Split by mode
            t_m0, y_m0 = [], []
            t_m3, y_m3 = [], []
            t_unk, y_unk = [], []
            
            for i, mode in enumerate(modes):
                if y_data[i] is None: continue
                
                if mode == "Mode0":
                    t_m0.append(t_data[i])
                    y_m0.append(y_data[i])
                elif mode == "Mode3":
                    t_m3.append(t_data[i])
                    y_m3.append(y_data[i])
                else:
                    t_unk.append(t_data[i])
                    y_unk.append(y_data[i])
            
            # Add Scatter Items
            # Mode 0: Red
            if t_m0:
                scatter = pg.ScatterPlotItem(size=12, pen=pg.mkPen(None), brush=pg.mkBrush(255, 0, 0, 200), symbol=symbol)
                scatter.addPoints(t_m0, y_m0)
                plot_item.addItem(scatter)
                # Dummy for legend
                name = f"{status_type} (Mode0)"
                plot_item.plot([t_m0[0]], [y_m0[0]], pen=None, symbol=symbol, symbolBrush='r', name=name)

            # Mode 3: Blue
            if t_m3:
                scatter = pg.ScatterPlotItem(size=12, pen=pg.mkPen(None), brush=pg.mkBrush(0, 0, 255, 200), symbol=symbol)
                scatter.addPoints(t_m3, y_m3)
                plot_item.addItem(scatter)
                name = f"{status_type} (Mode3)"
                plot_item.plot([t_m3[0]], [y_m3[0]], pen=None, symbol=symbol, symbolBrush='b', name=name)
            
            # Unknown: Gray
            if t_unk:
                scatter = pg.ScatterPlotItem(size=10, pen=pg.mkPen(None), brush=pg.mkBrush(100, 100, 100, 200), symbol=symbol)
                scatter.addPoints(t_unk, y_unk)
                plot_item.addItem(scatter)

        # PG
        pg_data = [(x['ts'], x['pg'], x['mode']) for x in data_points if x['pg'] is not None]
        if pg_data:
            t_p = [x[0] for x in pg_data]
            v_p = [x[1] for x in pg_data]
            m_p = [x[2] for x in pg_data]
            add_status_scatter(self.p_batt_stat, t_p, v_p, m_p, "PG")
            
        # Charging
        chg_data = [(x['ts'], x['charging'], x['mode']) for x in data_points if x['charging'] is not None]
        if chg_data:
            t_c = [x[0] for x in chg_data]
            v_c = [x[1] for x in chg_data]
            m_c = [x[2] for x in chg_data]
            add_status_scatter(self.p_batt_stat, t_c, v_c, m_c, "Charging")
            
        # Plot 4: RSOC
        # Start
        rs_data = [(x['ts'], x['rsoc_start'], x['mode']) for x in data_points if x['rsoc_start'] is not None]
        if rs_data:
            t_rs = [x[0] for x in rs_data]
            v_rs = [x[1] for x in rs_data]
            m_rs = [x[2] for x in rs_data]
            self.p_rsoc.plot(t_rs, v_rs, pen=pg.mkPen(200, 200, 200, style=Qt.PenStyle.DashLine), name="Start Trend")
            add_mode_scatter(self.p_rsoc, t_rs, v_rs, m_rs)
            
        # End
        re_data = [(x['ts'], x['rsoc_end'], x['mode']) for x in data_points if x['rsoc_end'] is not None]
        if re_data:
            t_re = [x[0] for x in re_data]
            v_re = [x[1] for x in re_data]
            m_re = [x[2] for x in re_data]
            self.p_rsoc.plot(t_re, v_re, pen=pg.mkPen(150, 150, 150), name="End Trend")
            add_mode_scatter(self.p_rsoc, t_re, v_re, m_re)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("EDF Data Quality Analysis")
        self.resize(1200, 800)
        
        self.files = [] # List of file paths
        self.file_results = {} # Map file_path -> results
        self.channel_configs = {} # Map channel_name -> {threshold: float, replacement: float}
        
        self.init_ui()
        
    def init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        layout = QVBoxLayout(main_widget)
        
        # --- Top Bar: File Selection ---
        top_layout = QHBoxLayout()
        
        top_layout.addWidget(QLabel("File Type:"))
        self.combo_file_type = QComboBox()
        self.combo_file_type.addItems(["Sensor", "Raw Data", "LFP", "LFP & ESA"])
        top_layout.addWidget(self.combo_file_type)
        
        self.btn_load = QPushButton("Load Folder")
        self.btn_load.clicked.connect(self.load_folder)
        top_layout.addWidget(self.btn_load)
        
        self.btn_clear = QPushButton("Clear Files")
        self.btn_clear.clicked.connect(self.clear_files)
        top_layout.addWidget(self.btn_clear)
        
        self.btn_analyze = QPushButton("Run Analysis")
        self.btn_analyze.clicked.connect(self.run_analysis)
        self.btn_analyze.setEnabled(False)
        top_layout.addWidget(self.btn_analyze)
        
        self.btn_show_pulse = QPushButton("Show Pulse Analysis")
        self.btn_show_pulse.clicked.connect(self.show_pulse_analysis)
        self.btn_show_pulse.setEnabled(False)
        top_layout.addWidget(self.btn_show_pulse)

        top_layout.addWidget(QLabel("Pulse Freq:"))
        self.combo_pulse_freq = QComboBox()
        self.combo_pulse_freq.addItems(["4kHz", "5kHz", "6kHz"])
        self.combo_pulse_freq.setCurrentText("5kHz")
        top_layout.addWidget(self.combo_pulse_freq)
        
        # New Analysis Buttons
        self.btn_lfp_spec = QPushButton("Show LFP Spectrum")
        self.btn_lfp_spec.clicked.connect(self.show_lfp_spectrum)
        self.btn_lfp_spec.setEnabled(False)
        top_layout.addWidget(self.btn_lfp_spec)
        
        self.btn_esa_data = QPushButton("Show ESA Data")
        self.btn_esa_data.clicked.connect(self.show_esa_data)
        self.btn_esa_data.setEnabled(False)
        top_layout.addWidget(self.btn_esa_data)
        
        self.btn_raw_hp = QPushButton("Show Raw HighPass")
        self.btn_raw_hp.clicked.connect(self.show_raw_highpass)
        self.btn_raw_hp.setEnabled(False)
        top_layout.addWidget(self.btn_raw_hp)
        
        self.btn_sensor_timeline = QPushButton("Show Sensor Timeline")
        self.btn_sensor_timeline.clicked.connect(self.show_sensor_timeline)
        self.btn_sensor_timeline.setEnabled(False)
        top_layout.addWidget(self.btn_sensor_timeline)
        
        self.btn_stats_trend = QPushButton("Show Statistics Trend")
        self.btn_stats_trend.clicked.connect(self.show_stats_trend)
        self.btn_stats_trend.setEnabled(False)
        top_layout.addWidget(self.btn_stats_trend)
        
        layout.addLayout(top_layout)
        
        # --- Main Content: Splitter (Files vs Channels) ---
        splitter = QSplitter(Qt.Orientation.Horizontal)
        
        # Left: File List (Tree)
        file_group = QGroupBox("Loaded Files")
        file_layout = QVBoxLayout(file_group)
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels([
            "File Name", "Type", "Sync Success Rate", "Avg Loss Rate",
            "Power Good", "Charging", "RSOC Start", "RSOC End",
            "Avg Delay (ms)", "Sync Energy Rate", "Avg Delay Energy (ms)"
        ])
        self.file_tree.setColumnWidth(0, 200)
        self.file_tree.setColumnWidth(4, 80)
        self.file_tree.setColumnWidth(5, 80)
        self.file_tree.setColumnWidth(6, 80)
        self.file_tree.setColumnWidth(7, 80)
        self.file_tree.setColumnWidth(8, 100)
        self.file_tree.setColumnWidth(9, 110)
        self.file_tree.setColumnWidth(10, 130)
        self.file_tree.itemSelectionChanged.connect(self.on_file_selected)
        file_layout.addWidget(self.file_tree)
        splitter.addWidget(file_group)
        
        # Right: Channel Configuration
        chan_group = QGroupBox("Channel Configuration & Results")
        chan_layout = QVBoxLayout(chan_group)
        
        # New: Info Label for Battery Stats
        self.label_info = QLabel("")
        self.label_info.setStyleSheet("color: blue; font-weight: bold;")
        chan_layout.addWidget(self.label_info)
        
        # Global Config Inputs
        config_layout = QHBoxLayout()
        config_layout.addWidget(QLabel("Default Threshold:"))
        self.input_default_thresh = QLineEdit("-1000")
        config_layout.addWidget(self.input_default_thresh)
        
        config_layout.addWidget(QLabel("Replacement Value:"))
        self.input_replacement = QLineEdit("0")
        config_layout.addWidget(self.input_replacement)
        
        config_layout.addWidget(QLabel("Method:"))
        self.combo_method = QComboBox()
        self.combo_method.addItems(["Fixed Value", "Previous Valid", "Mean Window"])
        self.combo_method.currentIndexChanged.connect(self.on_method_changed)
        config_layout.addWidget(self.combo_method)
        
        self.label_win = QLabel("Win Size:")
        config_layout.addWidget(self.label_win)
        self.input_win = QLineEdit("10")
        self.input_win.setEnabled(False)
        config_layout.addWidget(self.input_win)
        
        self.btn_apply_defaults = QPushButton("Apply to All Channels")
        self.btn_apply_defaults.clicked.connect(self.apply_defaults)
        config_layout.addWidget(self.btn_apply_defaults)
        
        chan_layout.addLayout(config_layout)
        
        self.chan_table = QTableWidget()
        self.chan_table.setColumnCount(8)
        self.chan_table.setHorizontalHeaderLabels(["Channel", "Threshold", "Replacement", "Data Loss (%)", "Loss Count", "Min", "Max", "RMS (LFP)"])
        self.chan_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        chan_layout.addWidget(self.chan_table)
        
        splitter.addWidget(chan_group)
        splitter.setSizes([400, 800])
        
        layout.addWidget(splitter)
        
        # --- Bottom: Status ---
        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar)
        
    def on_method_changed(self):
        method = self.combo_method.currentText()
        is_mean = (method == "Mean Window")
        self.input_win.setEnabled(is_mean)

    def clear_files(self):
        self.files = []
        self.file_results = {}
        self.file_tree.clear()
        self.btn_analyze.setEnabled(False)
        self.btn_show_pulse.setEnabled(False)
        self.btn_lfp_spec.setEnabled(False)
        self.btn_esa_data.setEnabled(False)
        self.btn_raw_hp.setEnabled(False)
        self.btn_sensor_timeline.setEnabled(False)
        self.btn_stats_trend.setEnabled(False)
        
        # Clear config table except headers? Or keep configs?
        # Keep configs is better for UX.

    def load_folder(self):
        # Allow selecting multiple folders using QFileDialog
        dialog = QFileDialog(self, "Select Folders")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        
        # Enable multiple selection in the tree view inside the dialog
        for view in dialog.findChildren((QListView, QTreeView)):
            if isinstance(view.model(), QFileSystemModel):
                view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
                
        if dialog.exec():
            folders = dialog.selectedFiles()
        else:
            return
            
        if not folders:
            return
            
        file_type = self.combo_file_type.currentText()
        # Don't clear self.files, append instead
        
        # Patterns
        pattern = ""
        if file_type == "Sensor": pattern = r".*sensor\.edf$"
        elif file_type == "Raw Data": pattern = r".*raw\.edf$"
        elif file_type == "LFP": pattern = r".*lfp\.edf$" # Assumes only lfp, not ESA
        elif file_type == "LFP & ESA": pattern = r".*LFP&ESA\.edf$"
        
        # Scan
        import glob
        
        new_files = []
        
        # Walk directory recursively for each selected folder
        for folder in folders:
            for root, dirs, files in os.walk(folder):
                for file in files:
                    if re.match(pattern, file, re.IGNORECASE):
                        full_path = os.path.join(root, file)
                        if full_path not in self.files: # Avoid duplicates
                            new_files.append(full_path)
        
        if not new_files:
            QMessageBox.information(self, "Info", "No new matching files found.")
            return
            
        self.files.extend(new_files)
        self.update_file_tree()
        self.btn_analyze.setEnabled(True)
        
        # If this is the first time we load files, load configs from the first one
        if len(self.files) == len(new_files) and self.files:
             self.load_channels_for_config(self.files[0])

    def update_file_tree(self):
        self.file_tree.clear()
        
        # Group by Hour: YYYY-MM-DD-HH
        # Regex to extract date
        # Assuming format: YYYY-MM-DD-HH-MM-SS...
        date_pattern = r"^(\d{4}-\d{2}-\d{2}-\d{2})"
        
        groups = {}
        
        for fpath in self.files:
            fname = os.path.basename(fpath)
            match = re.match(date_pattern, fname)
            if match:
                key = match.group(1)
            else:
                key = "Unknown Time"
                
            if key not in groups:
                groups[key] = []
            groups[key].append(fpath)
            
        # Sort keys
        sorted_keys = sorted(groups.keys())
        
        for key in sorted_keys:
            # Add Top Level Item (Hour Group)
            group_item = QTreeWidgetItem(self.file_tree)
            group_item.setText(0, f"Time Group: {key}")
            group_item.setExpanded(True)
            
            for fpath in groups[key]:
                fname = os.path.basename(fpath)
                
                # Determine Type
                ftype = "Unknown"
                if 'raw' in fname.lower(): ftype = "Raw Neural"
                elif 'lfp' in fname.lower() or 'esa' in fname.lower(): ftype = "LFP/ESA"
                elif 'sensor' in fname.lower(): ftype = "Sensor"
                
                item = QTreeWidgetItem(group_item)
                item.setText(0, fname)
                item.setText(1, ftype)
                item.setText(2, "-")
                item.setText(3, "-")
                item.setText(4, "-")
                item.setText(5, "-")
                item.setText(6, "-")
                item.setText(7, "-")
                item.setText(8, "-")
                item.setText(9, "-")
                item.setText(10, "-")
                # Store full path in user data or tooltip?
                # QTreeWidgetItem doesn't have setData like QTableWidgetItem in same way, but usually setData(0, Qt.UserRole, path)
                item.setData(0, Qt.ItemDataRole.UserRole, fpath)

    def load_files(self):
        # Deprecated logic kept/removed? User replaced "Load EDF Files" with "Load Folder" logic.
        # But maybe they still want file selection? 
        # The prompt says "For file load function, I need improvements: 1. Give folder..."
        # So I will replace the old load_files with load_folder logic above.
        pass

    def load_channels_for_config(self, file_path):
        # We need to read the file header to get channels
        # This is a blocking call but fast for header
        import pyedflib
        try:
            f = pyedflib.EdfReader(file_path)
            labels = f.getSignalLabels()
            
            # Read min values for default thresholds (as per user request)
            # This might be slow for large files.
            # We will read signal data for each channel.
            min_values = []
            for i in range(len(labels)):
                data = f.readSignal(i)
                if len(data) > 0:
                    min_values.append(np.min(data))
                else:
                    min_values.append(0.0)
            
            f.close()
            
            self.chan_table.setRowCount(len(labels))
            # default_thresh = self.input_default_thresh.text() # Use min instead
            default_repl = self.input_replacement.text()
            
            for i, label in enumerate(labels):
                clean_label = label.strip()
                self.chan_table.setItem(i, 0, QTableWidgetItem(clean_label))
                
                # Check if we have existing config
                if clean_label in self.channel_configs:
                    thresh = str(self.channel_configs[clean_label]['threshold'])
                    repl = str(self.channel_configs[clean_label]['replacement'])
                else:
                    # Use min value as default threshold
                    thresh = str(min_values[i])
                    repl = default_repl
                    self.channel_configs[clean_label] = {'threshold': float(thresh), 'replacement': float(repl)}
                
                self.chan_table.setItem(i, 1, QTableWidgetItem(thresh))
                self.chan_table.setItem(i, 2, QTableWidgetItem(repl))
                self.chan_table.setItem(i, 3, QTableWidgetItem("-"))
                self.chan_table.setItem(i, 4, QTableWidgetItem("-"))
                self.chan_table.setItem(i, 5, QTableWidgetItem("-"))
                self.chan_table.setItem(i, 6, QTableWidgetItem("-"))
                self.chan_table.setItem(i, 7, QTableWidgetItem("-"))
                
                # Create a blank item for background reset
                item = QTableWidgetItem("-")
                item.setBackground(QBrush(QColor(255, 255, 255)))
                self.chan_table.setItem(i, 3, item)
                
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Could not read file header: {e}")

    def on_file_selected(self):
        selected_items = self.file_tree.selectedItems()
        self.label_info.setText("") # Clear info
        
        if not selected_items:
            self.btn_show_pulse.setEnabled(False)
            self.btn_lfp_spec.setEnabled(False)
            self.btn_esa_data.setEnabled(False)
            self.btn_raw_hp.setEnabled(False)
            return
            
        item = selected_items[0]
        # Check if it's a file item (has UserRole data)
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        
        if not file_path:
            # It's a group item or invalid
            self.btn_show_pulse.setEnabled(False)
            self.btn_lfp_spec.setEnabled(False)
            self.btn_esa_data.setEnabled(False)
            self.btn_raw_hp.setEnabled(False)
            return
        
        # Enable Buttons based on file type
        ftype = item.text(1)
        
        is_raw = "Raw Neural" in ftype
        is_lfp = "LFP/ESA" in ftype
        
        # Single-file analysis buttons
        self.btn_show_pulse.setEnabled(is_raw)
        self.btn_esa_data.setEnabled(is_lfp)
        
        # Global analysis buttons (Enable if ANY relevant file is loaded)
        has_raw = any("raw" in f.lower() for f in self.files)
        has_lfp = any("lfp" in f.lower() or "esa" in f.lower() for f in self.files)
        
        self.btn_raw_hp.setEnabled(has_raw)
        self.btn_lfp_spec.setEnabled(has_lfp)
        
        # Check if any sensor files are loaded to enable timeline
        # Need to iterate tree? Or just check self.files content?
        # self.files contains all loaded files.
        # But we need to know if any are sensor.
        has_sensor = any("sensor" in f.lower() for f in self.files)
        self.btn_sensor_timeline.setEnabled(has_sensor)
        
        # Load channels for this file
        self.load_channels_for_config(file_path)
        
        # If we have results, show them
        if file_path in self.file_results:
            results = self.file_results[file_path]['loss_stats']
            
            # Show Charging Stats if available
            charging_text = []
            
            for i in range(self.chan_table.rowCount()):
                label = self.chan_table.item(i, 0).text()
                if label in results:
                    # Charging Stats
                    if 'charging_stats' in results[label]:
                        stats = results[label]['charging_stats']
                        pg = stats['pg_ratio'] * 100
                        chg = stats['charging_ratio'] * 100
                        charging_text.append(f"[{label}] Power Good: {pg:.1f}%, Charging: {chg:.1f}%")
                        
                    loss_rate = results[label]['loss_rate'] * 100 # %
                    loss_count = results[label]['loss_count']
                    min_val = results[label].get('min_val', 0)
                    max_val = results[label].get('max_val', 0)
                    
                    self.chan_table.setItem(i, 3, QTableWidgetItem(f"{loss_rate:.2f}%"))
                    self.chan_table.setItem(i, 4, QTableWidgetItem(str(loss_count)))
                    self.chan_table.setItem(i, 5, QTableWidgetItem(f"{min_val:.2f}"))
                    self.chan_table.setItem(i, 6, QTableWidgetItem(f"{max_val:.2f}"))
                    
                    rms = results[label].get('rms', 0.0)
                    if rms > 0:
                        self.chan_table.setItem(i, 7, QTableWidgetItem(f"{rms:.2f}"))
                    else:
                        self.chan_table.setItem(i, 7, QTableWidgetItem("-"))
                    
                    # Highlight if high loss?
                    if loss_rate > 10.0:
                        self.chan_table.item(i, 3).setBackground(QBrush(QColor(255, 200, 200)))
                    else:
                        self.chan_table.item(i, 3).setBackground(QBrush(QColor(255, 255, 255)))
                        
            if charging_text:
                self.label_info.setText(" | ".join(charging_text))
            else:
                self.label_info.setText("")

    def show_pulse_analysis(self):
        selected_items = self.file_tree.selectedItems()
        if not selected_items:
            return
            
        item = selected_items[0]
        file_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not file_path: return
        
        pulse_freq_hz = self.get_selected_pulse_freq_hz()
        self.pulse_window = PulseVisualizationWindow(file_path, pulse_freq_hz=pulse_freq_hz)
        self.pulse_window.show()

    def get_selected_pulse_freq_hz(self):
        text = self.combo_pulse_freq.currentText().strip().lower()
        if text.endswith("khz"):
            text = text[:-3]
        try:
            return float(text) * 1000.0
        except ValueError:
            return 5000.0

    def get_current_configs(self):
        threshold_map = {}
        replacement_val = float(self.input_replacement.text())
        
        # Get Method
        method_map = {"Fixed Value": "fixed", "Previous Valid": "previous", "Mean Window": "mean"}
        method = method_map.get(self.combo_method.currentText(), "fixed")
        
        try:
            window_size = int(self.input_win.text())
        except:
            window_size = 10
        
        for i in range(self.chan_table.rowCount()):
            label = self.chan_table.item(i, 0).text()
            try:
                thresh = float(self.chan_table.item(i, 1).text())
                threshold_map[label] = thresh
            except ValueError:
                pass
        return threshold_map, replacement_val, method, window_size

    def show_lfp_spectrum(self):
        # Gather all LFP files
        lfp_files = []
        for fpath in self.files:
            fname = os.path.basename(fpath).lower()
            if 'lfp' in fname or 'esa' in fname:
                lfp_files.append(fpath)
        
        if not lfp_files:
            QMessageBox.warning(self, "Warning", "No LFP files found in the list.")
            return
            
        threshold_map, replacement_val, method, win_size = self.get_current_configs()
        self.lfp_window = LFPSpectrumWindow(lfp_files, threshold_map, replacement_val, method, win_size)
        self.lfp_window.show()

    def show_esa_data(self):
        # Gather all ESA files
        esa_files = []
        for fpath in self.files:
            fname = os.path.basename(fpath).lower()
            if 'esa' in fname:
                esa_files.append(fpath)
                
        if not esa_files:
            QMessageBox.warning(self, "Warning", "No ESA files found in the list.")
            return
            
        threshold_map, replacement_val, method, win_size = self.get_current_configs()
        
        self.esa_window = ESADataWindow(esa_files, threshold_map, replacement_val, method, win_size)
        self.esa_window.show()

    def show_raw_highpass(self):
        # Gather all Raw files
        raw_files = []
        for fpath in self.files:
            fname = os.path.basename(fpath).lower()
            if 'raw' in fname:
                raw_files.append(fpath)
                
        if not raw_files:
            QMessageBox.warning(self, "Warning", "No Raw Data files found in the list.")
            return
            
        threshold_map, replacement_val, method, win_size = self.get_current_configs()
        
        self.raw_hp_window = RawHighPassWindow(raw_files, threshold_map, replacement_val, method, win_size)
        self.raw_hp_window.show()

    def show_sensor_timeline(self):
        # Gather all sensor files
        sensor_files = []
        # Use self.files (which contains all loaded file paths)
        # Assuming all loaded files are of the selected type "Sensor" if loaded via "Load Folder"
        # BUT user might have mixed if using load_files (legacy)? No, we replaced it.
        # But wait, self.files might contain other types if we loaded multiple times?
        # The logic clears self.files on Load Folder.
        # So self.files contains files of the currently selected type.
        # BUT if type is "Sensor", all are sensor.
        # If type is "LFP", none are sensor.
        # But show_sensor_timeline button is only enabled if "has_sensor" is true.
        # And has_sensor checks if "sensor" is in filename.
        
        for fpath in self.files:
            if "sensor" in os.path.basename(fpath).lower():
                sensor_files.append(fpath)
        
        if sensor_files:
            threshold_map, replacement_val, method, win_size = self.get_current_configs()
            
            self.sensor_timeline_window = SensorTimelineWindow(sensor_files, threshold_map, replacement_val, method, win_size)
            self.sensor_timeline_window.show()
        else:
            QMessageBox.warning(self, "Warning", "No Sensor files found in the list.")

    def show_stats_trend(self):
        if not self.file_results:
            QMessageBox.warning(self, "Warning", "No analysis results to plot.")
            return
            
        self.stats_trend_window = StatsTrendWindow(self.file_results)
        self.stats_trend_window.show()

    def apply_defaults(self):
        # Update current configs
        default_thresh = float(self.input_default_thresh.text())
        default_repl = float(self.input_replacement.text())
        
        for i in range(self.chan_table.rowCount()):
            label = self.chan_table.item(i, 0).text()
            self.channel_configs[label] = {'threshold': default_thresh, 'replacement': default_repl}
            self.chan_table.setItem(i, 1, QTableWidgetItem(str(default_thresh)))
            self.chan_table.setItem(i, 2, QTableWidgetItem(str(default_repl)))

    def run_analysis(self):
        # 1. Gather current configs from table
        # We can use get_current_configs but run_analysis was implemented before it.
        # Let's reuse get_current_configs to be consistent.
        
        threshold_map, replacement_val, method, window_size = self.get_current_configs()
        
        # Update channel configs in memory (get_current_configs reads from table but doesn't update self.channel_configs fully if we want persistence)
        # Actually get_current_configs iterates the table.
        # Let's just update self.channel_configs for consistency if needed, but worker uses the map.
        
        # Update internal state just in case
        for label, thresh in threshold_map.items():
            self.channel_configs[label] = {'threshold': thresh, 'replacement': replacement_val}
        
        pulse_freq_hz = self.get_selected_pulse_freq_hz()
        self.worker = AnalysisWorker(
            self.files,
            threshold_map,
            replacement_val,
            method,
            window_size,
            pulse_freq_hz
        )
        self.worker.progress.connect(self.progress_bar.setValue)
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.error.connect(lambda e: QMessageBox.critical(self, "Error", e))
        
        self.btn_analyze.setEnabled(False)
        self.progress_bar.setValue(0)
        self.worker.start()
        
    def on_analysis_finished(self, results):
        self.file_results = results
        self.btn_analyze.setEnabled(True)
        self.btn_stats_trend.setEnabled(True)
        self.progress_bar.setValue(100)
        
        # Update File Tree
        # Traverse tree
        iterator = QTreeWidgetItemIterator(self.file_tree)
        while iterator.value():
            item = iterator.value()
            file_path = item.data(0, Qt.ItemDataRole.UserRole)
            
            if file_path and file_path in results:
                # Sync Stats
                sync_stats = results[file_path]['sync_stats']
                sync_stats_energy = results[file_path].get('sync_stats_energy')
                if sync_stats:
                    success_rate, n_matched, n_total, avg_delay_ms, msg = sync_stats
                    if n_total > 0:
                        text = f"{success_rate*100:.1f}% ({n_matched}/{n_total})"
                    else:
                        text = f"N/A ({msg})"
                    item.setText(2, text)
                    if avg_delay_ms is None:
                        item.setText(8, "-")
                    else:
                        item.setText(8, f"{avg_delay_ms:.2f}")
                else:
                    item.setText(2, "N/A")
                    item.setText(8, "-")

                if sync_stats_energy:
                    e_success_rate, e_n_matched, e_n_total, e_avg_delay_ms, e_msg = sync_stats_energy
                    if e_n_total > 0:
                        e_text = f"{e_success_rate*100:.1f}% ({e_n_matched}/{e_n_total})"
                    else:
                        e_text = f"N/A ({e_msg})"
                    item.setText(9, e_text)
                    if e_avg_delay_ms is None:
                        item.setText(10, "-")
                    else:
                        item.setText(10, f"{e_avg_delay_ms:.2f}")
                else:
                    item.setText(9, "N/A")
                    item.setText(10, "-")
                    
                # Avg Loss Rate
                loss_stats = results[file_path]['loss_stats']
                total_loss_rate = 0.0
                count = 0
                for ch_stats in loss_stats.values():
                    total_loss_rate += ch_stats['loss_rate']
                    count += 1
                
                avg_loss = (total_loss_rate / count * 100) if count > 0 else 0.0
                item.setText(3, f"{avg_loss:.2f}%")
                
                # Battery Stats
                pg_text = "-"
                chg_text = "-"
                rsoc_start_text = "-"
                rsoc_end_text = "-"
                
                # Check for any channel with charging_stats or rsoc_stats
                # We prioritize showing the first found STAT channel or aggregate?
                # Usually only one battery stat channel per file.
                for ch_name, ch_res in loss_stats.items():
                    if 'charging_stats' in ch_res:
                        stats = ch_res['charging_stats']
                        pg_text = f"{stats['pg_ratio']*100:.1f}%"
                        chg_text = f"{stats['charging_ratio']*100:.1f}%"
                        
                    if 'rsoc_stats' in ch_res:
                        r_stats = ch_res['rsoc_stats']
                        rsoc_start_text = f"{r_stats['start']:.1f}%"
                        rsoc_end_text = f"{r_stats['end']:.1f}%"
                
                item.setText(4, pg_text)
                item.setText(5, chg_text)
                item.setText(6, rsoc_start_text)
                item.setText(7, rsoc_end_text)
                
            iterator += 1
        
        # Update Channel Table (Loss Stats) for currently selected file
        self.on_file_selected()
        QMessageBox.information(self, "Done", "Analysis Complete!")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
