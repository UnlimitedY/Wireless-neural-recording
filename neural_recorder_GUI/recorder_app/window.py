from ast import Pass
from PyQt6.QtCore import (QCoreApplication, QDate, QDateTime, QLocale,
    QMetaObject, QObject, QPoint, QRect,
    QSize, QTime, QUrl, Qt)
from PyQt6.QtWidgets import (QApplication, QComboBox, QFrame, QHBoxLayout,
    QLineEdit, QMainWindow, QMenuBar, QPushButton,
    QSizePolicy, QSpinBox, QStatusBar, QTextBrowser,
    QVBoxLayout, QWidget ,QMessageBox, QDialog, QProgressDialog)
from PyQt6.QtGui import QFont
try:
    from scipy import signal
except Exception:
    signal = None
try:
    from scipy.fft import fft, fftfreq
except Exception:
    fft = None
    fftfreq = None
import numpy as np
from PyQt6.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton
from PyQt6.QtCore import QTimer

try:
    from ..hardware.neural_reader import (
        LFP_filter,
        MODE2_V2_FS,
        MODE2_V2_GUI_MISSING_SAMPLE,
        RELAY_RECOMMENDED_ESB_CHANNELS,
        SerialPort,
        build_runtime_device_esb_channel_command,
        _classify_packet_index_transition,
        _normalize_packet_expected_step,
        my_gaussian_filter1d,
    )
    from ..services.mouse_platform_monitor import ChargingGuardState, MousePlatformDetector
except ImportError:
    from hardware.neural_reader import (
        LFP_filter,
        MODE2_V2_FS,
        MODE2_V2_GUI_MISSING_SAMPLE,
        RELAY_RECOMMENDED_ESB_CHANNELS,
        SerialPort,
        build_runtime_device_esb_channel_command,
        _classify_packet_index_transition,
        _normalize_packet_expected_step,
        my_gaussian_filter1d,
    )
    from services.mouse_platform_monitor import ChargingGuardState, MousePlatformDetector
import warnings

try:
    from . import neural_recorder_main_ui as UI
    from .display_downsampling import (
        adjust_coupled_downsample_factors,
        adjust_downsample_factor,
        display_downsample_strategy_for_stream,
        downsample_xy_for_display,
    )
except ImportError:
    import neural_recorder_main_ui as UI
    from display_downsampling import (
        adjust_coupled_downsample_factors,
        adjust_downsample_factor,
        display_downsample_strategy_for_stream,
        downsample_xy_for_display,
    )
""" pyQtgraph strolling plot"""
"""
Various methods of drawing scrolling plots.
"""
import numpy as np
import pyqtgraph as pg
import cv2
from PyQt6.QtCore import QTimer
import serial
import serial.tools.list_ports
import threading
import sys
import os
import json
import math
from PyQt6 import QtCore, QtWidgets
import time
from datetime import datetime
from multiprocessing import Process ,Queue
if fft is None:
    try:
        from scipy.fftpack import fft
    except Exception:
        fft = None
import re
import logging
from logging.handlers import RotatingFileHandler
import traceback
try:
    from ..support.path_utils import build_daily_file_path, get_application_directory
    from ..hardware.impedance_model import (
        build_impedance_model_metadata,
        impedance_complex_from_mag_phase,
        impedance_mag_phase_from_complex,
        restore_channel_to_ref_impedance,
    )
except ImportError:
    from support.path_utils import build_daily_file_path, get_application_directory
    from hardware.impedance_model import (
        build_impedance_model_metadata,
        impedance_complex_from_mag_phase,
        impedance_mag_phase_from_complex,
        restore_channel_to_ref_impedance,
    )

def setup_logging():
    """Setup logging configuration"""
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Add timestamp to log filename
    current_time = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_file = os.path.join(log_dir, f"neural_recorder_crash_{current_time}.log")
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5),
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Exception hook to log unhandled exceptions
    def exception_hook(exctype, value, tb):
        logging.error("Uncaught exception:", exc_info=(exctype, value, tb))
        # Call the original excepthook
        sys.__excepthook__(exctype, value, tb)
        
    sys.excepthook = exception_hook
    
    logging.info("Application started - Logging initialized")

def safe_guard(func):
    """Decorator to catch and log exceptions in methods"""
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logging.error(f"Error in {func.__name__}: {e}", exc_info=True)
    return wrapper

# Module level constants for safe float conversion
_RE_INCOMPLETE_SCI = re.compile(r'^[+-]?\d+(?:\.\d+)?[eE][+-]?$')
_RE_HEX = re.compile(r'^(?:0x)?[0-9a-fA-F]+$')
_RE_FLOAT_PREFIX = re.compile(r'^[+-]?\d+(?:\.\d+)?')

def _safe_float_fast(v):
    """Optimized safe float conversion"""
    try:
        # Fast path for standard types
        if isinstance(v, (float, int, np.floating, np.integer)):
            return float(v)
            
        # String handling
        s = str(v).strip()
        if not s:
            return np.nan
            
        # Try direct conversion first (fastest for valid strings)
        try:
            return float(s)
        except ValueError:
            pass
            
        # Incomplete scientific notation
        if _RE_INCOMPLETE_SCI.match(s):
            try:
                return float(s + '0')
            except ValueError:
                return np.nan
                
        # Hexadecimal
        if _RE_HEX.match(s):
            try:
                return float(int(s, 16))
            except ValueError:
                return np.nan
                
        # Prefix extraction
        m = _RE_FLOAT_PREFIX.match(s)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return np.nan
                
        return np.nan
    except Exception:
        return np.nan

def _sensor_payload_to_padded_array(sensor_payload, row_count=9):
    """Convert ragged sensor rows to a fixed row_count x max_len float array."""
    rows = []
    for row_index in range(int(row_count)):
        try:
            row = sensor_payload[row_index]
        except Exception:
            row = []

        if isinstance(row, np.ndarray):
            try:
                arr = np.asarray(row, dtype=np.float32).reshape(-1)
            except Exception:
                arr = np.asarray([], dtype=np.float32)
        elif isinstance(row, (str, bytes)):
            arr = np.asarray([_safe_float_fast(row)], dtype=np.float32)
        else:
            try:
                arr = np.asarray([_safe_float_fast(value) for value in row], dtype=np.float32).reshape(-1)
            except TypeError:
                arr = np.asarray([_safe_float_fast(row)], dtype=np.float32)
            except Exception:
                arr = np.asarray([], dtype=np.float32)
        rows.append(arr)

    max_len = max((int(row.size) for row in rows), default=0)
    if max_len <= 0:
        return np.empty((int(row_count), 0), dtype=np.float32)

    sensors_data = np.full((int(row_count), max_len), np.nan, dtype=np.float32)
    for row_index, row in enumerate(rows):
        if row.size > 0:
            sensors_data[row_index, : int(row.size)] = row
    return sensors_data

def _count_missing_from_indices(packet_indices, expected_step=1):
    if packet_indices is None:
        return 0, 0
    try:
        n_packets = len(packet_indices)
    except Exception:
        packet_indices = list(packet_indices)
        n_packets = len(packet_indices)
    if n_packets <= 1:
        return 0, n_packets
    expected_step = _normalize_packet_expected_step(expected_step)
    miss = 0
    last_valid_idx = None
    last_valid_pos = 0
    for start_pos, value in enumerate(packet_indices):
        try:
            last_valid_idx = int(value) & 0xFFFF
            last_valid_pos = start_pos
            break
        except Exception:
            continue
    if last_valid_idx is None:
        return 0, n_packets
    for value in packet_indices[last_valid_pos + 1:]:
        try:
            curr_idx = int(value) & 0xFFFF
        except Exception:
            continue
        transition = _classify_packet_index_transition(last_valid_idx, curr_idx, expected_step=expected_step)
        kind = str(transition.get("kind", "") or "")
        if kind == "out_of_order" or kind == "duplicate":
            continue
        if kind == "reset":
            last_valid_idx = curr_idx
            continue
        miss += max(0, int(transition.get("missing", 0) or 0))
        last_valid_idx = curr_idx
    return int(miss), n_packets


# 添加新的LFP频谱窗口类
class LFPSpectrumWindow(QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent = parent
        self.setWindowTitle("LFP Spectrum Analysis")
        self.setGeometry(100, 100, 1000, 700)
        
        # 采样率设置
        self.fs = 1000  # 1000Hz采样率
        self.channels = 16  # 16个LFP通道
        
        # 频率范围设置
        self.freq_min = 0
        self.freq_max = 500
        
        # 创建中心部件和布局
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        
        # 创建控制面板
        control_panel = self.create_control_panel()
        layout.addWidget(control_panel)
        
        # 创建pyqtgraph图形显示区域
        self.plot_widget = pg.PlotWidget()
        
        # 设置坐标轴标签和样式
        self.plot_widget.setLabel('left', 'Power Spectral Density', units='dB')
        self.plot_widget.setLabel('bottom', 'Frequency', units='Hz')
        self.plot_widget.setTitle('LFP Spectrum Analysis - 16 Channels')
        
        # 显示网格和坐标轴
        self.plot_widget.showGrid(True, True, alpha=0.3)
        
        # 设置背景颜色（使用深色背景以便看清坐标轴）
        self.plot_widget.setBackground('k')  # 黑色背景
        
        # 获取坐标轴并设置样式
        bottom_axis = self.plot_widget.getAxis('bottom')
        left_axis = self.plot_widget.getAxis('left')
        
        # 设置坐标轴文字颜色和大小
        bottom_axis.setTextPen('w')  # 白色文字
        left_axis.setTextPen('w')    # 白色文字
        
        # 设置坐标轴刻度样式
        bottom_axis.setPen('w')      # 白色坐标轴线
        left_axis.setPen('w')        # 白色坐标轴线
        
        # 强制显示坐标轴刻度
        bottom_axis.setStyle(tickTextOffset=10, tickLength=10)
        left_axis.setStyle(tickTextOffset=10, tickLength=10)
        
        # 设置频率范围
        self.plot_widget.setXRange(self.freq_min, self.freq_max)
        
        # 设置合理的Y轴范围
        self.plot_widget.setYRange(-100, 50)  # dB范围
        
        # 添加图例
        self.plot_widget.addLegend()
        
        layout.addWidget(self.plot_widget)
        
        # 初始化频谱曲线
        self.init_spectrum_curves()
        
        # 定时器用于更新频谱
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_spectrum)
        self.update_timer.start(500)  # 每500ms更新一次
        
    def create_control_panel(self):
        """Create control panel"""
        panel = QWidget()
        layout = QHBoxLayout(panel)
        
        # Frequency range selection
        freq_label = QLabel("Frequency Range:")
        self.freq_combo = QComboBox()
        self.freq_combo.addItems(["0-500Hz", "0-100Hz", "0-50Hz", "1-100Hz", "10-100Hz", "0-250Hz"])
        self.freq_combo.currentTextChanged.connect(self.update_frequency_range)
        
        # Display mode selection
        mode_label = QLabel("Display Mode:")
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["All Channels", "Average Power Spectrum", "Select Channel"])
        self.mode_combo.currentTextChanged.connect(self.update_display_mode)
        
        # Window function selection
        window_label = QLabel("Window Function:")
        self.window_combo = QComboBox()
        self.window_combo.addItems(["hann", "hamming", "blackman", "bartlett"])
        
        # Channel selection (used when "Select Channel" mode is chosen)
        channel_label = QLabel("Select Channel:")
        self.channel_combo = QComboBox()
        self.channel_combo.addItems([f"Channel {i}" for i in range(16)])
        self.channel_combo.setEnabled(False)
        
        layout.addWidget(freq_label)
        layout.addWidget(self.freq_combo)
        layout.addWidget(mode_label)
        layout.addWidget(self.mode_combo)
        layout.addWidget(window_label)
        layout.addWidget(self.window_combo)
        layout.addWidget(channel_label)
        layout.addWidget(self.channel_combo)
        layout.addStretch()
        
        return panel
    
    def init_spectrum_curves(self):
        """初始化频谱曲线"""
        self.spectrum_curves = []
        
        # 为16个通道创建不同颜色的曲线
        colors = [
            '#FF0000', '#00FF00', '#0000FF', '#FFFF00', '#FF00FF', '#00FFFF',
            '#800000', '#008000', '#000080', '#808000', '#800080', '#008080',
            '#FFA500', '#A52A2A', '#DDA0DD', '#98FB98', '#F0E68C', '#DEB887',
            '#5F9EA0', '#7FFF00', '#D2691E', '#FF7F50', '#6495ED', '#DC143C',
            '#00CED1', '#9400D3', '#FF1493', '#00BFFF', '#696969', '#1E90FF',
            '#B22222', '#228B22'
        ]
        
        for i in range(16):
            # 创建曲线，初始时不显示数据
            curve = self.plot_widget.plot(
                [], [], 
                pen=pg.mkPen(color=colors[i], width=1.5),
                name=f'Ch{i}'
            )
            self.spectrum_curves.append(curve)
    
    def update_frequency_range(self, freq_range):
        """更新频率范围"""
        if freq_range == "0-500Hz":
            self.freq_min, self.freq_max = 0, 500
        elif freq_range == "0-250Hz":
            self.freq_min, self.freq_max = 0, 250
        elif freq_range == "0-100Hz":
            self.freq_min, self.freq_max = 0, 100
        elif freq_range == "0-50Hz":
            self.freq_min, self.freq_max = 0, 50
        elif freq_range == "1-100Hz":
            self.freq_min, self.freq_max = 1, 100
        elif freq_range == "10-100Hz":
            self.freq_min, self.freq_max = 10, 100
        
        # 更新图的X轴范围
        self.plot_widget.setXRange(self.freq_min, self.freq_max)
    
    def update_display_mode(self, mode):
        """Update display mode"""
        if mode == "Select Channel":
            self.channel_combo.setEnabled(True)
            # Hide all curves
            for curve in self.spectrum_curves:
                curve.setData([], [])
        else:
            self.channel_combo.setEnabled(False)
    
    def update_spectrum(self):
        """更新频谱显示"""
        if self.parent is None or not hasattr(self.parent, 'LFP_raw_data'):
            return
        try:
            if hasattr(self.parent, "plot_update_enabled") and (not self.parent.plot_update_enabled):
                return
        except Exception:
            pass
        
        try:
            current_mode = self.mode_combo.currentText()
            window_func = self.window_combo.currentText()
            
            if current_mode == "Average Power Spectrum":
                self.update_average_spectrum(window_func)
            elif current_mode == "Select Channel":
                self.update_selected_channel_spectrum(window_func)
            else:  # All Channels
                self.update_all_channels_spectrum(window_func)
                
        except Exception as e:
            print(f"频谱更新错误: {e}")
    
    def update_all_channels_spectrum(self, window_func):
        """更新所有通道频谱"""
        for ch in range(16):
            # 获取通道数据（去除偏移）
            channel_data = self.parent.LFP_raw_data[ch] - ch * self.parent.separate_interval
            
            # 去除NaN值
            valid_data = channel_data[~np.isnan(channel_data)]
            
            if len(valid_data) > 256:  # 确保有足够的数据点
                # 计算功率谱密度
                f, Pxx = signal.welch(valid_data, self.fs, 
                                    window=window_func, 
                                    nperseg=min(1024, len(valid_data)//4),
                                    noverlap=None)
                
                # 转换为dB
                Pxx_db = 10 * np.log10(Pxx + 1e-12)  # 避免log(0)
                
                # 频率范围过滤
                freq_mask = (f >= self.freq_min) & (f <= self.freq_max)
                
                # 更新曲线
                self.spectrum_curves[ch].setData(f[freq_mask], Pxx_db[freq_mask])
            else:
                # 如果数据不足，清空曲线
                self.spectrum_curves[ch].setData([], [])
    
    def update_average_spectrum(self, window_func):
        """更新平均功率谱"""
        # 先隐藏所有单独的通道曲线
        for i, curve in enumerate(self.spectrum_curves):
            if i > 0:  # 保留第一条曲线用于显示平均值
                curve.setData([], [])
        
        all_pxx = []
        
        for ch in range(16):
            # 获取通道数据（去除偏移）
            channel_data = self.parent.LFP_raw_data[ch] - ch * self.parent.separate_interval
            valid_data = channel_data[~np.isnan(channel_data)]
            
            if len(valid_data) > 256:
                f, Pxx = signal.welch(valid_data, self.fs, 
                                    window=window_func, 
                                    nperseg=min(1024, len(valid_data)//4))
                all_pxx.append(Pxx)
        
        if all_pxx:
            # 计算平均功率谱
            avg_pxx = np.mean(all_pxx, axis=0)
            avg_pxx_db = 10 * np.log10(avg_pxx + 1e-12)
            
            # 频率范围过滤
            freq_mask = (f >= self.freq_min) & (f <= self.freq_max)
            
            # 使用第一条曲线显示平均频谱，并修改其样式
            self.spectrum_curves[0].setData(f[freq_mask], avg_pxx_db[freq_mask])
            self.spectrum_curves[0].setPen(pg.mkPen(color='red', width=3))
    
    def update_selected_channel_spectrum(self, window_func):
        """更新选择的通道频谱"""
        selected_ch = self.channel_combo.currentIndex()
        
        # 隐藏所有曲线
        for curve in self.spectrum_curves:
            curve.setData([], [])
        
        # 获取选择通道的数据
        channel_data = self.parent.LFP_raw_data[selected_ch] - selected_ch * self.parent.separate_interval
        valid_data = channel_data[~np.isnan(channel_data)]
        
        if len(valid_data) > 256:
            # 计算功率谱密度
            f, Pxx = signal.welch(valid_data, self.fs, 
                                window=window_func, 
                                nperseg=min(1024, len(valid_data)//4))
            
            # 转换为dB
            Pxx_db = 10 * np.log10(Pxx + 1e-12)
            
            # 频率范围过滤
            freq_mask = (f >= self.freq_min) & (f <= self.freq_max)
            
            # 显示选择的通道
            self.spectrum_curves[selected_ch].setData(f[freq_mask], Pxx_db[freq_mask])
            self.spectrum_curves[selected_ch].setPen(pg.mkPen(color='blue', width=2))


"""main class"""
import threading
import sys
import os
import traceback
import time

class MainThreadWatchdog(threading.Thread):
    """
    Watchdog thread to monitor the main thread for freezes.
    It periodically checks if the main thread has updated a timestamp.
    If the timestamp hasn't been updated for `timeout` seconds, it logs the stack trace.
    """
    def __init__(self, main_window, timeout=5.0, check_interval=1.0):
        super().__init__(daemon=True)
        self.main_window = main_window
        self.timeout = timeout
        self.check_interval = check_interval
        self.running = True
        self.name = "MainThreadWatchdog"

    def run(self):
        logging.info("Watchdog started")
        while self.running:
            time.sleep(self.check_interval)
            try:
                # Check if main thread is alive
                last_alive = getattr(self.main_window, 'last_alive_timestamp', 0)
                if time.time() - last_alive > self.timeout:
                    # Main thread is stuck!
                    logging.warning(f"Watchdog: Main thread unresponsive for {time.time() - last_alive:.1f}s")
                    
                    # Get main thread stack trace
                    # Main thread id is usually the starting thread.
                    # We can iterate over all threads to find the one that matches main thread ID if needed,
                    # or just dump all threads.
                    self._log_all_thread_stacks()
                    
                    # Don't spam logs too much, maybe wait longer before next log
                    time.sleep(self.timeout) 
            except Exception as e:
                logging.error(f"Watchdog error: {e}")

    def _log_all_thread_stacks(self):
        """Log stack traces of all running threads"""
        id2name = dict([(th.ident, th.name) for th in threading.enumerate()])
        code = []
        for threadId, stack in sys._current_frames().items():
            threadName = id2name.get(threadId, "")
            code.append(f"\n# Thread: {threadName}({threadId})")
            for filename, lineno, name, line in traceback.extract_stack(stack):
                code.append(f'File: "{filename}", line {lineno}, in {name}')
                if line:
                    code.append(f"  {line.strip()}")
        
        logging.warning("\n".join(code))

class ESBMainWindow(UI.MainWindow):
    def __init__(self):
        super(ESBMainWindow ,self).__init__()
        
        # Initialize watchdog timestamp
        self.last_alive_timestamp = time.time()
        
        # Start watchdog timer (updates timestamp in main thread)
        self.watchdog_timer = QTimer()
        self.watchdog_timer.timeout.connect(self._kick_watchdog)
        self.watchdog_timer.start(500) # Kick every 500ms
        
        # Start watchdog thread
        self.watchdog = MainThreadWatchdog(self)
        self.watchdog.start()

        """ hyper """
        self.plot_update_enabled = False
        self.Auto_ST_Update = False
        self.Auto_ST_Update_flag = False
        self.channel_panding = None
        self.single_update_data_size = 0
        self.spike1ch_rms_pending = 0
        self.colorList = ['#e6194B', '#3cb44b', '#ffe119', '#4363d8', 
                          '#f58231', '#42d4f4', '#f032e6', '#fabed4', 
                          '#469990', '#dcbeff', '#9A6324', '#fffac8', 
                          '#800000', '#aaffc3', '#000075', '#a9a9a9', 
                          '#ffffff', '#42d4f4', '#e6194B', '#3cb44b', '#ffe119', '#4363d8', 
                          '#f58231', '#42d4f4', '#f032e6', '#fabed4', 
                          '#469990', '#dcbeff', '#9A6324', '#fffac8', 
                          '#800000', '#aaffc3', '#000075', '#a9a9a9', 
                          '#ffffff', '#42d4f4'] # pink cyan red
        self.IMUaccle_name = ['Accl_X' ,'Accl_Y' ,'Accl_Z' ,'Gryo_X' ,'Gryo_Y' ,'Gryo_Z']

        # Cache pens to avoid recreation in update loops
        self.pens_solid = [pg.mkPen({'color': c, 'width': 1}) for c in self.colorList]
        self.pens_dashed = [pg.mkPen({'color': c, 'width': 1, "style": QtCore.Qt.PenStyle.DashLine}) for c in self.colorList]
        self.pen_cyan = pg.mkPen({'color': 'c', 'width': 1})
        self.pen_white = pg.mkPen({'color': 'w', 'width': 1})
        self.pen_white_2 = pg.mkPen({'color': 'w', 'width': 2})

        self.current_sample_mode = 0 
        self._sample_mode_hint_initialized = False
        self.mode3_esa_reref_mode = 0 # default re-reference off
        self.impedance_test_running = False
        self.impedance_history = []
        self.impedance_history_path = os.path.join(get_application_directory(), "Data", "impedance_history.json")
        self.impedance_series_resistor_kohm = 220.0
        self.impedance_shunt_cap_pf = 47.0
        self.impedance_test_frequency_hz = 1000.0

        """ init params """
        # system
        self.mSerial = None
        self.t1 = None
        self.curr_active_ports = None
        self.pen1 = pg.mkPen(color=(255, 0, 0))
        # RF power control - 使用主UI中的串口连接
        self.rf_power_status = 1  # 1: 关闭, 2: 开启
        ## sample rate
        self.lfp_sample_rate = 1000 # 1khz default
        self.spike_sample_rate = 20833 # 20khz default
        self.spike_raster_bin = 18
        ## display lengths
        self.chart_x_length_s = 5 # 5s
        self.update_packets_num = 0  # genarated data points when GUI update is enabled; determined by @param GUIUpdateInterval ; packets number
        self.lfp_display_data_num = self.lfp_sample_rate * self.chart_x_length_s # 在plot中一次展示的windows的个数,通过采样频率来确定
        self.spike_display_data_num = self.spike_sample_rate * self.chart_x_length_s // 2
        self.mode2_sample_rate = int(MODE2_V2_FS)
        self.mode2_display_data_num = self.mode2_sample_rate * self.chart_x_length_s
        self.spike_raster_display_data_num = self.spike_display_data_num // self.spike_raster_bin 
        self.spike_channel_num = 16
        self.lfp_channel_num = 16
        self.mode0_mand_display_smooth_ms = 4
        self._mode0_mand_display_history = [np.array([], dtype=np.float32) for _ in range(16)]
        self.raster_render_stride = 2
        self.raster_max_points_per_channel = 1200
        self.raster_sr_render_step = 2
        self.raster_render_last_stride = self.raster_render_stride
        self.raster_render_last_factor = 1
        self._raster_render_counter = 0
        self.display_refresh_hz_steps = (12.0, 8.0, 6.0, 4.0)
        self.display_refresh_base_hz = 12.0
        self.display_refresh_min_hz = 4.0
        self.plot_min_render_interval_s = 1.0 / self.display_refresh_base_hz
        self._last_plot_render_monotonic = 0.0
        self._last_plot_render_monotonic_by_stream = {}
        self._last_plot_setdata_monotonic_by_stream = {}
        self.display_render_actual_hz_by_stream = {
            "lfp": 0.0,
            "spike": 0.0,
            "mode2": 0.0,
            "mode3_lfp_esa": 0.0,
            "mode3_raw": 0.0,
        }
        self.display_refresh_hz_by_stream = {
            "lfp": self.display_refresh_base_hz,
            "spike": self.display_refresh_base_hz,
            "mode2": self.display_refresh_base_hz,
            "mode3_lfp_esa": self.display_refresh_base_hz,
            "mode3_raw": self.display_refresh_base_hz,
        }
        self.display_refresh_stable_windows = {"lfp": 0, "spike": 0, "mode2": 0, "mode3_lfp_esa": 0, "mode3_raw": 0}
        self.display_refresh_last_action = {
            "lfp": "init",
            "spike": "init",
            "mode2": "init",
            "mode3_lfp_esa": "init",
            "mode3_raw": "init",
        }
        self.display_downsample_factors = {"lfp": 1, "spike": 1, "mode2": 1, "mode3_lfp_esa": 1, "mode3_raw": 1}
        self.display_downsample_stable_windows = {"lfp": 0, "spike": 0, "mode2": 0, "mode3_lfp_esa": 0, "mode3_raw": 0}
        self.display_downsample_last_action = {
            "lfp": "init",
            "spike": "init",
            "mode2": "init",
            "mode3_lfp_esa": "init",
            "mode3_raw": "init",
        }
        self.display_downsample_mode3_metrics = {}
        self.display_downsample_mode3_stable_windows = 0
        self.display_downsample_mode3_metric_max_age_s = 5.0
        self.display_downsample_mode3_update_counter = 0
        self.display_downsample_mode3_last_adjust_versions = {}
        self.display_downsample_mode3_max_factor_ratio = 4
        self._remote_active_mode_text = ""
        self._mode3_display_hint_until = 0.0
        self._mode3_display_legacy_factor_sync_done = False
        self.display_downsample_loss_threshold_percent = 0.1
        self.display_downsample_render_threshold_ms = 83.0
        self.display_downsample_mode2_render_threshold_ms = 24.0
        self.display_downsample_max_factor = 64
        self.display_downsample_stable_windows_required = 30
        self.display_downsample_strategy = "balanced"
        self._last_display_tuning_profile_payload = ""
        self._last_display_tuning_profile_push_monotonic = 0.0
        self._load_display_tuning_profile(getattr(self, "profile", {}))
        self.last_detail_render_metrics = {}
        
        ################# spike raw data mode 1
        self.ring_spike_pointer = 0
        self.spike_x = np.arange(0, self.spike_display_data_num, 1)
        self.spike_raw_data = np.full((1 ,self.spike_display_data_num) ,np.nan)
        # Spike滤波相关参数与缓冲
        self.spike_filter_enabled = False
        self.spike_filter_low_cut = 300.0
        self.spike_filter_high_cut = 3000.0
        self.spike_filter_fs = 20000.0
        self.spike_filtered_data = np.full((1 ,self.spike_display_data_num) ,np.nan)
        self._spike_filter_b = None
        self._spike_filter_a = None
        self._last_mode0_raw_channel = None
        self._mode0_raw_has_wrapped = False
        self._spike_raw_has_wrapped = False
        self.raw_spike_threshod = np.full((self.spike_channel_num ,self.spike_display_data_num) ,np.nan) # Spike threshold

        ####### spike events raster recording in 17KHz sample rate; Figure 1
        self.ring_spike_raster_pointer = 0
        self.spike_raster_x = np.arange(0, self.spike_raster_display_data_num, 1)
        self.spike_raster_data = np.full((self.spike_channel_num ,self.spike_raster_display_data_num) ,np.nan)

        ####### Spike rate 
        self.SR_value = np.full((1 ,self.spike_raster_display_data_num) ,np.nan) # using the slided average to cale the spiking rate 

        ###### counters
        self.spikemisspackets_mode_1 = 0
        self.spikeaccumulpackets_mode_1 = 0
        self.spike_timestamp_note = 0

        ###### params
        self.spike_raw_channel = 0 # raw data display channel
        self.mode3_channel_thresholds_uv = [60.0 for _ in range(self.spike_channel_num)]

        ################# LFP raw data mode 0 
        self.separate_interval = 500# 500
        self.esa_scale_factor = 10 # Default ESA/MAND scale factor
        self.lfp_feature_tick_prefix = "ESA"
        self.ring_lfp_pointer = 0
        self.LFP_x = np.arange(0, self.lfp_display_data_num, 1)
        self.LFP_raw_data =np.full((self.lfp_channel_num ,self.lfp_display_data_num) ,np.nan) 
        self._lfp_has_wrapped = False
        self.lfpmisspackets = 0 # recording the number of missed packets every GUI update events
        self.lfpaccumulpackets = 0 # recording received packets number

        self.lfp_filter_length = self.lfp_sample_rate * 2 # 2s data length
        self.lfp_filter_buffer = np.full((self.lfp_channel_num ,self.lfp_filter_length) ,np.nan)
        self.lfp_filter_enabled = False

        ################# ESA raw data (same format as LFP)
        self.esa_channel_num = 16
        self.ring_esa_pointer = 0
        self.ESA_x = np.arange(0, self.lfp_display_data_num, 1)  # 使用与LFP相同的时间轴
        self.ESA_raw_data = np.full((self.esa_channel_num, self.lfp_display_data_num), np.nan)

        ################# spike mode 2 
        self.separate_interval_spike = 500
        self.spike_mode2_curr_channel = [0 for _ in range(16)]
        self.ring_spike_mode2_pointer = 0
        self.spike_mode2_x = np.arange(0, self.mode2_display_data_num, 1)
        self.spike_mode2_raw_data =np.full((self.spike_channel_num ,self.mode2_display_data_num) ,np.nan)
        self.mode2_alignment_data = np.full(self.mode2_display_data_num, np.nan)
        self._mode2_has_wrapped = False
        self.spike_moide2_misspackets = 0 # recording the number of missed packets every GUI update events
        self.spike_mode2_accumulpackets = 0 # recording received packets number
        self._mode2_packet_loss_snapshot = {}
        self._closing_in_progress = False

        self.reinit_rawdata_mode2 = False
        self.reinit_rawdata_mode2_temp = 0

        ############### Other sensors
        self.ring_LSR_pointer = 0
        self.LSR_display_data_num = 1000 
        self.LSR_timestamp = np.arange(0, self.LSR_display_data_num, 1) 
        self.RSOC_stat = np.full((1 ,self.LSR_display_data_num) ,np.nan) # battery RSOC
        self.STAT_stat = np.full((1 ,self.LSR_display_data_num) ,np.nan) # battery_PPM
        self.Voltage_stat = np.full((1 ,self.LSR_display_data_num) ,np.nan) # Power status
        self.IMUdata = np.full((6 ,self.LSR_display_data_num) ,np.nan) # AcclX,Y,Z ,geclo X ,Y,Z

        # current battery status
        self.RSOC = 0
        self.Reported_RSOC = 0
        self.Battery_STAT = 0
        self.Battery_voltage = 0
        self.last_battery_sample_time = 0.0
        self.RF_turnoff = False
        self.last_battery_update_time = 0 # Throttle battery updates
        self.mode3_battery_pause_threshold = 10
        self.mode3_battery_resume_threshold = 20
        self.mode3_trial_trigger_paused = False
        self.mode3_trial_trigger_target_paused = False
        self.low_battery_mode0_training_target = False
        self.low_battery_mode0_training_active = False
        self.low_battery_mode0_training_reason = ""
        self.mode3_trial_trigger_guard_interval_ms = 10000

        self.RF_timer = QTimer()
        self.RF_timer.timeout.connect(self.rf_power_control)
        self.RF_timer.start(1000 * 60)  # 每1分钟更新一次RF状态
        # Independent battery-policy guard for Mode3 trial trigger.
        # The policy is evaluated periodically, but D0/D1 are only sent while RF is ON.
        self.mode3_trial_trigger_guard_timer = QTimer()
        self.mode3_trial_trigger_guard_timer.timeout.connect(
            self._poll_mode3_trial_trigger_gate
        )
        self.mode3_trial_trigger_guard_timer.start(
            self.mode3_trial_trigger_guard_interval_ms
        )
        self.video_segment_max_seconds = 60 * 60
        self.video_segment_start_time = None
        self.video_recording_source_path = None
        self.video_segment_timer = QTimer()
        self.video_segment_timer.setInterval(1000)
        self.video_segment_timer.timeout.connect(self._check_video_segment_rotation)

        self.charging_guard_config_json_path = os.path.join(
            get_application_directory(), "Data", "charging_guard_cages.json"
        )
        self.charging_guard_active_cage = "cage1"
        self.charging_guard_presets = self._load_charging_guard_presets()
        self.charging_guard_config = self._default_charging_guard_config()
        self.charging_guard_config.update(
            self.charging_guard_presets.get(self.charging_guard_active_cage, {})
        )
        self.charging_guard_detector = MousePlatformDetector(
            roi_norm=self.charging_guard_config["roi_norm"],
            dark_threshold=self.charging_guard_config["dark_threshold"],
            min_area_ratio=self.charging_guard_config["min_area_ratio"],
        )
        self.charging_guard_state = ChargingGuardState(
            required_hits=self.charging_guard_config["required_hits"]
        )
        self.charging_guard_last_detection = {
            "mouse_present": False,
            "area_ratio": 0.0,
            "roi": (0, 0, 0, 0),
            "reason": "not checked",
        }
        self.charging_guard_timer = QTimer()
        self.charging_guard_timer.timeout.connect(self._poll_charging_guard)
        self.charging_guard_timer.start(self.charging_guard_config["check_interval_ms"])
        if hasattr(self, 'charging_guard_enable_checkbox'):
            self.charging_guard_enable_checkbox.toggled.connect(self._on_charging_guard_toggled)
            self.charging_guard_enable_checkbox.setChecked(
                bool(self.charging_guard_config.get("enabled", False))
            )
        if hasattr(self, 'charging_guard_config_button'):
            self.charging_guard_config_button.clicked.connect(
                self.open_charging_guard_config_dialog
            )
        guard_enabled = bool(self.charging_guard_config.get("enabled", False))
        self._refresh_charging_guard_ui(
            "Watching" if guard_enabled else "Off",
            enabled=guard_enabled,
        )
        
        # 连接RF按钮信号
        self.rf_power_on_button.clicked.connect(self.rf_power_on)
        self.rf_power_off_button.clicked.connect(self.rf_power_off)
        if hasattr(self, 'rf_status_button'):
            self.rf_status_button.clicked.connect(self.rf_query_status)
        if hasattr(self, 'rf_channel_combo'):
            self.rf_channel_combo.currentIndexChanged.connect(self.rf_query_status)
        if hasattr(self, 'main_serial_refresh_button'):
            self.main_serial_refresh_button.clicked.connect(self.refresh_main_serial_ports)
        if hasattr(self, 'main_serial_connect_button'):
            self.main_serial_connect_button.clicked.connect(self.toggle_main_serial_connection)
        if hasattr(self, 'restart_relay_button'):
            self.restart_relay_button.clicked.connect(self.restart_relay_device)
        if hasattr(self, 'apply_relay_esb_channel_button'):
            self.apply_relay_esb_channel_button.clicked.connect(self.apply_relay_esb_channel)
        self._serial_disconnect_handling = False
        self._serial_auto_reconnect_target = None
        self._serial_auto_reconnect_attempts = 0
        self._serial_auto_reconnect_max_attempts = 15
        self._serial_auto_reconnect_exhausted = False
        self._rf_serial_lock = threading.Lock()
        self.neural_serial_health_timer = QTimer()
        self.neural_serial_health_timer.timeout.connect(self._poll_neural_serial_health)
        self.neural_serial_health_timer.start(1000)
        self.rf_serial_health_timer = QTimer()
        self.rf_serial_health_timer.timeout.connect(self._poll_rf_serial_health)
        self.rf_serial_health_timer.start(1500)
        
        # Spike threshold auto update buffer
        self.calcST_counter = [0 for _ in range(self.spike_channel_num)]
        self.computedST = [0 for _ in range(self.spike_channel_num)]
        self.updateSTflag = [0 for _ in range(self.spike_channel_num)]

        self.plot_update_toggle_button = QPushButton("Plot Update: Off")
        self.plot_update_toggle_button.setCheckable(True)
        self.plot_update_toggle_button.setChecked(False)
        self.plot_update_toggle_button.setMinimumHeight(28)
        self.plot_update_toggle_button.setMinimumWidth(110)
        self.plot_update_toggle_button.clicked.connect(self.on_plot_update_toggle_clicked)
        self._refresh_plot_update_toggle_style()
        self.display_tuning_toggle_button = QPushButton("Display: Balanced")
        self.display_tuning_toggle_button.setCheckable(True)
        self.display_tuning_toggle_button.setChecked(self._display_downsample_enabled())
        self.display_tuning_toggle_button.setMinimumHeight(28)
        self.display_tuning_toggle_button.setMinimumWidth(135)
        self.display_tuning_toggle_button.clicked.connect(self.on_display_tuning_toggle_clicked)
        self._refresh_display_tuning_button_style()
        self.mode3_reref_mode_combo.currentIndexChanged.connect(self.on_mode3_reref_mode_changed)
        self._refresh_mode3_reref_control_style()
        try:
            control_layout = self.stop_sampling_button.parentWidget().layout()
            if control_layout is not None:
                idx = control_layout.indexOf(self.stop_sampling_button)
                control_layout.insertWidget(idx + 1, self.plot_update_toggle_button)
                control_layout.insertWidget(idx + 2, self.display_tuning_toggle_button)
        except Exception:
            pass

        """ build  widgets containing these charts """
        pg.setConfigOption('background', 'k')
        pg.setConfigOption('foreground', 'w')
        
        """ raw data graph spike mode 1 """
        # raw data graph: figure 3; two axis ; raw data and threshold data ; spike dat
        self.SPIKE_view_channel = pg.ViewBox() # 定义一个视图框
        self.spike_channel = pg.GraphicsView() # 设置 绘图
        self.SPIKE_layout_channel = pg.GraphicsLayout() # 整个绘图layout 初始化
        self.spike_channel.setCentralWidget(self.SPIKE_layout_channel) # 将绘图区域设置为视图的中心组件

        self.spike_pI_channel = pg.PlotItem() # 定义一条曲线
        self.spike_pI_channel.setTitle('channel 0-15')
        self.spike_v1_channel = self.spike_pI_channel.vb # 得到曲线的视图层
        self.SPIKE_layout_channel.addItem(self.spike_pI_channel, row = 1, col = 1)# 将这个曲线层放到中间
        self.SPIKE_layout_channel.scene().addItem(self.SPIKE_view_channel)

        self.SPIKE_view_channel.setXLink(self.spike_v1_channel)
        self.spike_pI_channel.getAxis("left").setLabel('spike raw data/uv', color='#FFC0CB')
        self.spike_pI_channel.addLegend()

        # updating indicate lines: infiniteLine  
        self.spike_updating_indicater_mode_1 = pg.InfiniteLine(movable=False, angle=90, pen=pg.mkPen(color='w', width=3))
        self.spike_v1_channel.addItem(self.spike_updating_indicater_mode_1)

        self.raw_threshold_channel = pg.PlotCurveItem(None ,None ,pen='#DC143C') # threshold line
        self.spike_v1_channel.addItem(self.raw_threshold_channel)

        self.raw_dataline_channel = pg.PlotCurveItem(None, None ,name='channel 0-15') # raw data of one channel line
        self.spike_v1_channel.addItem(self.raw_dataline_channel)
        
        # Alignment signal curve (Mode 3)
        self.alignment_curve = pg.PlotCurveItem(None, None, pen=pg.mkPen({'color': 'r', 'width': 2}), name='Alignment')
        self.spike_v1_channel.addItem(self.alignment_curve)
        self.alignment_buffer = np.zeros(self.spike_display_data_num)
        
        self.spike_v1_channel.enableAutoRange(axis= pg.ViewBox.XYAxes ,enable = False)

        self.spike_v1_channel.setLimits(xMin=0, xMax=self.spike_display_data_num, yMin=-2000, yMax=2000) # 1mv range
        self.spike_v1_channel.setXRange(0 ,self.spike_display_data_num)
        
        """ spiking rate and raster graph mode 1 """
        self.SR_view_channel = pg.ViewBox() # 定义一个视图框
        self.SR_channel = pg.GraphicsView() # 设置 绘图
        self.SR_channel.setWindowTitle('SR data 0-15 channels')
        self.SR_layout_channel = pg.GraphicsLayout() # 整个绘图layout 初始化
        self.SR_channel.setCentralWidget(self.SR_layout_channel) # 将绘图区域设置为视图的中心组件
        
        self.SR_pI_channel = pg.PlotItem() # 定义一条曲线
        self.SR_v1_channel = self.SR_pI_channel.vb # 得到曲线的视图层
        self.SR_layout_channel.addItem(self.SR_pI_channel, row = 1, col = 1)# 将这个曲线层放到中间
        self.SR_layout_channel.scene().addItem(self.SR_view_channel)
        self.SR_view_channel.setXLink(self.SR_v1_channel)
        self.SR_pI_channel.getAxis("left").setLabel('SR', color='#FFC0CB')
        self.SR_pI_channel.addLegend()

        # updating indicate lines: infiniteLine  
        self.spike_raster_updating_indicater_mode_1 = pg.InfiniteLine(movable=False, label='', angle=90, pen=pg.mkPen(color='w', width=3), 
                                  labelOpts={'position':0.9, 'color':'y', 'fill': (200,200,200,50)})
        self.SR_v1_channel.addItem(self.spike_raster_updating_indicater_mode_1)

        # spike rate lines
        self.spike_SR_channel = pg.PlotCurveItem(None, None,pen='#FFFFFF')
        self.SR_v1_channel.addItem(self.spike_SR_channel)

        # raster ticks setting
        raster_ticks = {
            int(value):'Ch{}'.format(channel) for value, channel in 
            zip(np.arange(0, self.spike_channel_num, 1), range(16))
        }
        self.SR_pI_channel.getAxis("left").setTicks([raster_ticks.items()])
        font = QFont()
        font.setBold(True)
        font.setPointSize(6)
        self.SR_pI_channel.getAxis("left").setTickFont(font)

        # raster lines
        self.spike_raster_channel = []
        for i in range(self.spike_channel_num):
            self.spike_raster_channel.append(pg.ScatterPlotItem(None, None,pen='#FFFFFF'))
            self.spike_raster_channel[i].setSymbol('arrow_up')
            self.SR_v1_channel.addItem(self.spike_raster_channel[i])
        self.SR_v1_channel.enableAutoRange(axis=pg.ViewBox.XYAxes ,enable = False)
        self.SR_v1_channel.setLimits(xMin=0, xMax=self.spike_raster_display_data_num, yMin=-1, yMax=16) # 1mv range
        self.SR_v1_channel.setYRange(-1 ,16) 
        self.SR_v1_channel.setXRange(0 ,self.spike_raster_display_data_num) 

        """ LFP graph mode 0 - LEFT chart """
        self.LFP_view_channel = pg.ViewBox()
        self.LFP_channel = pg.GraphicsView()
        self.LFP_layout_channel = pg.GraphicsLayout()
        self.LFP_channel.setCentralWidget(self.LFP_layout_channel)
        
        self.LFP_pI_channel = pg.PlotItem() 
        self.LFP_pI_channel.setTitle('LFP 16 channels')
        self.LFP_v1_channel = self.LFP_pI_channel.vb
        self.LFP_layout_channel.addItem(self.LFP_pI_channel, row=1, col=1)
        self.LFP_layout_channel.scene().addItem(self.LFP_view_channel)
        self.LFP_view_channel.setXLink(self.LFP_v1_channel)
        self.LFP_pI_channel.getAxis("left").setLabel('LFP 1kHz', color='#FFC0CB')
        self.LFP_pI_channel.addLegend()
         
        # LFP lines (16 channels)
        self.LFP_raw_channel = []
        for i in range(self.lfp_channel_num):
            lfp_channel = pg.PlotCurveItem(None, None, pen='#FFFFFF')
            self.LFP_raw_channel.append(lfp_channel)
            self.LFP_v1_channel.addItem(lfp_channel)
            
        # LFP updating indicator line
        self.updating_indicater = pg.InfiniteLine(movable=False, angle=90, pen=pg.mkPen(color='w', width=3))
        self.LFP_v1_channel.addItem(self.updating_indicater)
        
        # LFP Y-axis ticks (16 channels)
        lfp_ticks = {}
        for channel in range(16):
            lfp_y_pos = int(channel * self.separate_interval)
            lfp_ticks[lfp_y_pos] = f'LFP Ch{channel}'
        self.LFP_pI_channel.getAxis("left").setTicks([lfp_ticks.items()])

        self.LFP_v1_channel.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        self.LFP_v1_channel.setLimits(xMin=0, xMax=self.lfp_display_data_num, yMin=-self.separate_interval, yMax=(self.lfp_channel_num + 1) * self.separate_interval)
        self.LFP_v1_channel.setYRange(-self.separate_interval, (self.lfp_channel_num + 1) * self.separate_interval)
        self.LFP_v1_channel.setXRange(0, self.lfp_display_data_num)

        """ ESA/MAND graph - RIGHT chart """
        self.ESA_view_channel = pg.ViewBox()
        self.ESA_channel = pg.GraphicsView()
        self.ESA_layout_channel = pg.GraphicsLayout()
        self.ESA_channel.setCentralWidget(self.ESA_layout_channel)
        
        self.ESA_pI_channel = pg.PlotItem()
        self.ESA_pI_channel.setTitle('ESA/MAND 16 channels')
        self.ESA_v1_channel = self.ESA_pI_channel.vb
        self.ESA_layout_channel.addItem(self.ESA_pI_channel, row=1, col=1)
        self.ESA_layout_channel.scene().addItem(self.ESA_view_channel)
        self.ESA_view_channel.setXLink(self.ESA_v1_channel)
        self.ESA_pI_channel.getAxis("left").setLabel('ESA/MAND', color='#FFFF00')
        self.ESA_pI_channel.addLegend()

        # ESA lines (16 channels)
        self.ESA_raw_channel = []
        for i in range(self.lfp_channel_num):
            esa_channel = pg.PlotCurveItem(None, None, pen='#FFFF00')
            self.ESA_raw_channel.append(esa_channel)
            self.ESA_v1_channel.addItem(esa_channel)

        # ESA updating indicator line
        self.ESA_updating_indicater = pg.InfiniteLine(movable=False, angle=90, pen=pg.mkPen(color='w', width=3))
        self.ESA_v1_channel.addItem(self.ESA_updating_indicater)

        # ESA Y-axis ticks (16 channels)
        esa_ticks = {}
        for channel in range(16):
            esa_y_pos = int(channel * self.separate_interval)
            esa_ticks[esa_y_pos] = f'ESA Ch{channel}'
        self.ESA_pI_channel.getAxis("left").setTicks([esa_ticks.items()])

        self.ESA_v1_channel.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        self.ESA_v1_channel.setLimits(xMin=0, xMax=self.lfp_display_data_num, yMin=-self.separate_interval, yMax=(self.lfp_channel_num + 1) * self.separate_interval)
        self.ESA_v1_channel.setYRange(-self.separate_interval, (self.lfp_channel_num + 1) * self.separate_interval)
        self.ESA_v1_channel.setXRange(0, self.lfp_display_data_num)

        """ 16 channel AP data spike mode 2"""
        self.AP_view_channel = pg.ViewBox() # 定义一个视图框
        self.AP_channel = pg.GraphicsView() # 设置 绘图
        self.AP_layout_channel = pg.GraphicsLayout() # 整个绘图layout 初始化
        self.AP_channel.setCentralWidget(self.AP_layout_channel) # 将绘图区域设置为视图的中心组件
        
        self.AP_pI_channel = pg.PlotItem() 
        self.AP_pI_channel.setTitle('Mode2 v2 AP data RHD Ch0-Ch15')
        self.AP_v1_channel = self.AP_pI_channel.vb # 得到曲线的视图层
        self.AP_layout_channel.addItem(self.AP_pI_channel, row = 1, col = 1)# 将这个曲线层放到中间
        self.AP_layout_channel.scene().addItem(self.AP_view_channel)
        self.AP_view_channel.setXLink(self.AP_v1_channel)
        self.AP_pI_channel.getAxis("left").setLabel('AP 10.417kHz', color='#FFC0CB')
        self.AP_pI_channel.addLegend()
         
        # AP lines
        self.AP_raw_channel = []
        for i in range(self.spike_channel_num):
            self.AP_raw_channel.append(pg.PlotCurveItem(None, None,pen='#FFFFFF'))
            self.AP_v1_channel.addItem(self.AP_raw_channel[i])
        self.AP_alignment_curve = pg.PlotCurveItem(None, None, pen=pg.mkPen({'color': 'r', 'width': 2}), name='Alignment')
        self.AP_v1_channel.addItem(self.AP_alignment_curve)
        # updating indicate lines: infiniteLine  
        self.AP_updating_indicater = pg.InfiniteLine(movable=False, angle=90, pen=pg.mkPen(color='w', width=3))
        self.AP_v1_channel.addItem(self.AP_updating_indicater)
        
        # spike ticks setting
        spike_mode2_ticks = {
            int(value):'Ch{}'.format(channel) for value, channel in 
            zip(np.arange(0, self.spike_channel_num * self.separate_interval_spike, self.separate_interval_spike), range(16))
        }
        self.AP_pI_channel.getAxis("left").setTicks([spike_mode2_ticks.items()])

        self.AP_v1_channel.setLimits(xMin=0, xMax=self.mode2_display_data_num, yMin=-self.separate_interval_spike, yMax=(self.spike_channel_num + 1) * self.separate_interval_spike)
        self.AP_v1_channel.setYRange(-self.separate_interval_spike ,(self.spike_channel_num + 1) * self.separate_interval_spike) 
        self.AP_v1_channel.setXRange(0 ,self.mode2_display_data_num)
        
        """ other sensors graph """
        self.IMU_yrange_accle = 2
        self.IMU_yrange_gyro = 500

        # 1. Accelerometer View
        self.accl_view_channel = pg.ViewBox() # 定义一个视图框
        self.accl_channel = pg.GraphicsView() # 设置 绘图
        self.accl_channel.setWindowTitle('accl data')
        self.accl_layout_channel = pg.GraphicsLayout() # 整个绘图layout 初始化
        self.accl_channel.setCentralWidget(self.accl_layout_channel) # 将绘图区域设置为视图的中心组件
        self.accl_pI_channel = pg.PlotItem() # 定义一条曲线
        self.accl_v1_channel = self.accl_pI_channel.vb # 得到曲线的视图层
        self.accl_layout_channel.addItem(self.accl_pI_channel, row = 1, col = 1)# 将这个曲线层放到中间
        self.accl_layout_channel.scene().addItem(self.accl_view_channel)
        self.accl_view_channel.setXLink(self.accl_v1_channel)
        self.accl_pI_channel.getAxis("left").setLabel('accl/g', color='#FFC0CB')
        self.accl_pI_channel.addLegend()

        # 2. Gyroscope View
        self.gyro_view_channel = pg.ViewBox()
        self.gyro_channel = pg.GraphicsView()
        self.gyro_channel.setWindowTitle('gyro data')
        self.gyro_layout_channel = pg.GraphicsLayout()
        self.gyro_channel.setCentralWidget(self.gyro_layout_channel)
        self.gyro_pI_channel = pg.PlotItem()
        self.gyro_v1_channel = self.gyro_pI_channel.vb
        self.gyro_layout_channel.addItem(self.gyro_pI_channel, row=1, col=1)
        self.gyro_layout_channel.scene().addItem(self.gyro_view_channel)
        self.gyro_view_channel.setXLink(self.gyro_v1_channel)
        self.gyro_pI_channel.getAxis("left").setLabel('gyro/dps', color='#FFC0CB')
        self.gyro_pI_channel.addLegend()
        
        self.IMU_accl_channel = [] ## accle & gyro lines
        for i in range(6):
            curve = pg.PlotCurveItem(None, None,pen=self.colorList[i] ,name=self.IMUaccle_name[i])
            self.IMU_accl_channel.append(curve)
            if i < 3:
                self.accl_v1_channel.addItem(curve)
            else:
                self.gyro_v1_channel.addItem(curve)
        
        self.accle_updating_indicater = pg.InfiniteLine(movable=False, label='{value:0.2f}', angle=90, pen=pg.mkPen(color='w', width=3), 
                                  labelOpts={'position':0.9, 'color':(150,0,0), 'fill': (200,200,200,50)})
        self.accl_v1_channel.addItem(self.accle_updating_indicater)

        self.gyro_updating_indicater = pg.InfiniteLine(movable=False, label='{value:0.2f}', angle=90, pen=pg.mkPen(color='w', width=3),
                                  labelOpts={'position':0.9, 'color':(150,0,0), 'fill': (200,200,200,50)})
        self.gyro_v1_channel.addItem(self.gyro_updating_indicater)
        
        self.accl_v1_channel.enableAutoRange(axis=pg.ViewBox.XYAxes ,enable = False)
        self.accl_v1_channel.setLimits(xMin=0, xMax=self.LSR_display_data_num, yMin=-self.IMU_yrange_accle, yMax=self.IMU_yrange_accle) # 1mv range
        self.accl_v1_channel.setXRange(0 ,self.LSR_display_data_num) 
        self.accl_v1_channel.setYRange(-self.IMU_yrange_accle ,self.IMU_yrange_accle)

        self.gyro_v1_channel.enableAutoRange(axis=pg.ViewBox.XYAxes, enable=False)
        self.gyro_v1_channel.setLimits(xMin=0, xMax=self.LSR_display_data_num, yMin=-self.IMU_yrange_gyro, yMax=self.IMU_yrange_gyro)
        self.gyro_v1_channel.setXRange(0, self.LSR_display_data_num)
        self.gyro_v1_channel.setYRange(-self.IMU_yrange_gyro, self.IMU_yrange_gyro)
        
        """ addWidget """
        self.lfp_tab.chart_container_layout.addWidget(self.LFP_channel)
        if hasattr(self, 'ESA_channel'):
            self.lfp_tab.chart_container_layout.addWidget(self.ESA_channel)
        self.spike1ch_tab.chart_container_layout.addWidget(self.spike_channel)
        self.raster_tab.chart_container_layout.addWidget(self.SR_channel)
        self.imu_tab.chart_container_layout.addWidget(self.accl_channel)
        self.imu_tab.chart_container_layout.addWidget(self.gyro_channel)
        self.spike4ch_tab.chart_container_layout.addWidget(self.AP_channel)

        """ callback function """
        # LFP保存按钮（互斥控制）
        self.lfp_tab.start_save_button.clicked.connect(self.start_save_lfp)
        self.lfp_tab.stop_save_button.clicked.connect(self.stop_save_lfp)
        self.lfp_tab.start_save_mode3_button.clicked.connect(self.start_save_mode3)
        self.lfp_tab.stop_save_mode3_button.clicked.connect(self.stop_save_mode3)
        self.spike4ch_tab.start_save_button.clicked.connect(self.start_save_mode2)
        self.spike4ch_tab.stop_save_button.clicked.connect(self.stop_save_mode2)
        self.spike1ch_tab.start_save_button.clicked.connect(self.start_save_mode1)
        self.spike1ch_tab.stop_save_button.clicked.connect(self.stop_save_mode1)

        self.start_sampling_button.clicked.connect(self.sample_start)
        self.stop_sampling_button.clicked.connect(self.sample_stop)
        self.sampling_mode_combo.currentIndexChanged.connect(self.sample_mode_switch)
        if getattr(self.spike4ch_tab, "channel_combos", []):
            self.spike4ch_tab.send_channels_button.clicked.connect(self.spike_channel_switch_mode2)
        self.spike1ch_tab.send_command_button.clicked.connect(self.spike_channel_switch_mode1)
        self.spike1ch_tab.send_mode3_command_button.clicked.connect(self.spike_channel_switch_mode3)
        # Spike1Ch 滤波与频谱控件信号连接
        self.spike1ch_tab.filter_enable_checkbox.toggled.connect(self.on_spike_filter_toggle)
        self.spike1ch_tab.low_cut_spin.valueChanged.connect(self.on_spike_filter_params_changed)
        self.spike1ch_tab.high_cut_spin.valueChanged.connect(self.on_spike_filter_params_changed)
        self.spike1ch_tab.sample_rate_combo.currentIndexChanged.connect(self.on_spike_sample_rate_changed)
        self.spike1ch_tab.open_spectrum_button.clicked.connect(self.open_spike_spectrum)
        self.raster_tab.auto_threshold_button.clicked.connect(self.Auto_threshold_update)
        self.raster_tab.send_threshold_button.clicked.connect(self.spike_threshold_set)
        self.update_battery_button.clicked.connect(self.IMU_mode_setting)
        self.lfp_tab.update_scale_button.clicked.connect(self.update_lfp_scale)
        if hasattr(self.lfp_tab, "mand_smooth_combo"):
            self.lfp_tab.mand_smooth_combo.currentTextChanged.connect(self.on_mode0_mand_display_smooth_changed)
        self.lfp_tab.enable_filter_button.clicked.connect(self.lfp_filter_on)
        self.lfp_tab.disable_filter_button.clicked.connect(self.lfp_filter_off)

        self.rf_power_on_button.clicked.connect(self.rf_power_on)
        self.rf_power_off_button.clicked.connect(self.rf_power_off)

        # Add spectrum window button
        self.spectrum_window_button = QPushButton("LFP Spectrum Analysis")
        self.spectrum_window_button.clicked.connect(self.open_spectrum_window)
        # Add button to LFP tab combined control row
        if hasattr(self.lfp_tab, 'lfp_combined_row'):
            self.lfp_tab.lfp_combined_row.addWidget(self.spectrum_window_button)
        else:
            self.lfp_tab.control_panel_layout.addWidget(self.spectrum_window_button)
        # Spectrum window instance
        self.spectrum_window = None

        # Connect Habits Panel Trial Start signal
        if hasattr(self, 'habits_tab') and hasattr(self.habits_tab, 'habits_panel'):
            self.habits_tab.habits_panel.TrialStarted.connect(self.on_trial_start)
            if hasattr(self.habits_tab.habits_panel, 'set_resume_habits_guard'):
                self.habits_tab.habits_panel.set_resume_habits_guard(
                    self._ensure_rf_off_for_habits_resume
                )
        if hasattr(self, 'impedance_tab'):
            self.impedance_tab.start_test_button.clicked.connect(self.start_impedance_test)
            self.impedance_tab.series_resistor_spin.setValue(self.impedance_series_resistor_kohm)
            self.impedance_tab.shunt_cap_spin.setValue(self.impedance_shunt_cap_pf)
            self.impedance_tab.series_resistor_spin.valueChanged.connect(self.on_impedance_compensation_params_changed)
            self.impedance_tab.shunt_cap_spin.valueChanged.connect(self.on_impedance_compensation_params_changed)
            self._refresh_impedance_compensation_summary()
            self._load_impedance_history()
        self.main_serial_connected = False
        self._set_serial_dependent_controls_enabled(False)
        if hasattr(self, 'main_serial_status_indicator'):
            self.set_main_serial_ui_connected(False)

    def _default_charging_guard_config(self):
        return {
            "enabled": False,
            "roi_norm": [0.1, 0.5, 0.3, 0.2],
            "dark_threshold": 90,
            "min_area_ratio": 0.30,
            "check_interval_ms": 10000,
            "required_hits": 6,
            "warning_duration_ms": 3000,
            "fan_enabled": True,
        }

    def _default_charging_guard_presets(self):
        base = self._default_charging_guard_config()
        return {
            "cage1": dict(base),
            "cage2": dict(base),
            "cage3": dict(base),
        }

    def _load_charging_guard_presets(self):
        defaults = self._default_charging_guard_presets()
        path = self.charging_guard_config_json_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            data = {"active_cage": "cage1", "presets": defaults}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return defaults

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            file_presets = data.get("presets", {})
            for cage in ("cage1", "cage2", "cage3"):
                if isinstance(file_presets.get(cage), dict):
                    defaults[cage].update(file_presets[cage])
            active = str(data.get("active_cage", "cage1"))
            self.charging_guard_active_cage = active if active in defaults else "cage1"
            return defaults
        except Exception as e:
            self.log_message(f"Warning: failed to load charging guard presets: {e}", level="warning")
            return defaults

    def _save_charging_guard_presets(self):
        path = self.charging_guard_config_json_path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            "active_cage": self.charging_guard_active_cage,
            "presets": self.charging_guard_presets,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def _save_current_charging_guard_to_active_cage(self):
        self.charging_guard_presets[self.charging_guard_active_cage] = {
            "enabled": bool(self.charging_guard_config.get("enabled", False)),
            "roi_norm": list(self.charging_guard_config.get("roi_norm", [0.1, 0.5, 0.3, 0.2])),
            "dark_threshold": int(self.charging_guard_config.get("dark_threshold", 90)),
            "min_area_ratio": float(self.charging_guard_config.get("min_area_ratio", 0.3)),
            "check_interval_ms": int(self.charging_guard_config.get("check_interval_ms", 10000)),
            "required_hits": int(self.charging_guard_config.get("required_hits", 6)),
            "warning_duration_ms": int(self.charging_guard_config.get("warning_duration_ms", 3000)),
            "fan_enabled": bool(self.charging_guard_config.get("fan_enabled", True)),
        }
        self._save_charging_guard_presets()

    def open_charging_guard_config_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Charging Guard Config")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        top_row = QHBoxLayout()
        top_row.addWidget(QtWidgets.QLabel("Cage:"))
        cage_combo = QComboBox()
        cage_combo.addItems(["cage1", "cage2", "cage3"])
        cage_combo.setCurrentText(self.charging_guard_active_cage)
        top_row.addWidget(cage_combo)
        top_row.addStretch(1)
        layout.addLayout(top_row)

        form = QtWidgets.QFormLayout()
        enabled_box = QtWidgets.QCheckBox("Enabled")
        roi_edit = QLineEdit()
        roi_edit.setPlaceholderText("x,y,w,h (0~1), e.g. 0.1,0.5,0.3,0.2")
        dark_spin = QSpinBox()
        dark_spin.setRange(0, 255)
        min_area_edit = QLineEdit()
        min_area_edit.setPlaceholderText("e.g. 0.30")
        interval_spin = QSpinBox()
        interval_spin.setRange(1000, 600000)
        required_hits_spin = QSpinBox()
        required_hits_spin.setRange(1, 50)
        warning_spin = QSpinBox()
        warning_spin.setRange(0, 120000)
        fan_box = QtWidgets.QCheckBox("Fan airflow")

        form.addRow("Enable", enabled_box)
        form.addRow("ROI norm", roi_edit)
        form.addRow("Dark threshold", dark_spin)
        form.addRow("Min area ratio", min_area_edit)
        form.addRow("Check interval (ms)", interval_spin)
        form.addRow("Required hits", required_hits_spin)
        form.addRow("Warning duration (ms)", warning_spin)
        form.addRow("Fan airflow", fan_box)
        layout.addLayout(form)

        button_row = QHBoxLayout()
        save_button = QPushButton("Save && Apply")
        cancel_button = QPushButton("Cancel")
        button_row.addStretch(1)
        button_row.addWidget(save_button)
        button_row.addWidget(cancel_button)
        layout.addLayout(button_row)

        def load_cage_to_form(cage_key):
            cfg = self.charging_guard_presets.get(cage_key, self._default_charging_guard_config())
            roi = cfg.get("roi_norm", [0.1, 0.5, 0.3, 0.2])
            enabled_box.setChecked(bool(cfg.get("enabled", False)))
            roi_edit.setText(",".join(str(float(v)) for v in roi))
            dark_spin.setValue(int(cfg.get("dark_threshold", 90)))
            min_area_edit.setText(str(float(cfg.get("min_area_ratio", 0.30))))
            interval_spin.setValue(max(1000, int(cfg.get("check_interval_ms", 10000))))
            required_hits_spin.setValue(max(1, int(cfg.get("required_hits", 6))))
            warning_spin.setValue(max(0, int(cfg.get("warning_duration_ms", 3000))))
            fan_box.setChecked(bool(cfg.get("fan_enabled", True)))

        def save_and_apply():
            cage_key = cage_combo.currentText()
            try:
                roi_values = [float(x.strip()) for x in roi_edit.text().split(",") if x.strip() != ""]
                if len(roi_values) != 4:
                    raise ValueError("ROI must have 4 numbers: x,y,w,h")
                min_area = float(min_area_edit.text().strip())
                if min_area < 0:
                    raise ValueError("min_area_ratio must be >= 0")
                cfg = {
                    "enabled": bool(enabled_box.isChecked()),
                    "roi_norm": roi_values,
                    "dark_threshold": int(dark_spin.value()),
                    "min_area_ratio": min_area,
                    "check_interval_ms": int(interval_spin.value()),
                    "required_hits": int(required_hits_spin.value()),
                    "warning_duration_ms": int(warning_spin.value()),
                    "fan_enabled": bool(fan_box.isChecked()),
                }
                self.charging_guard_active_cage = cage_key
                self.charging_guard_presets[cage_key] = cfg
                self._save_charging_guard_presets()
                self.configure_charging_guard(cfg)
                self.log_message(
                    f"Charging guard config saved to {cage_key} ({self.charging_guard_config_json_path})",
                    level="success",
                )
                dialog.accept()
            except Exception as e:
                QMessageBox.warning(self, "Invalid config", str(e))

        cage_combo.currentTextChanged.connect(load_cage_to_form)
        save_button.clicked.connect(save_and_apply)
        cancel_button.clicked.connect(dialog.reject)
        load_cage_to_form(cage_combo.currentText())
        dialog.exec()

    def configure_charging_guard(self, config):
        """Apply optional charging guard settings from the active profile config."""
        if not isinstance(config, dict):
            self.log_message("Warning: charging_guard config must be a JSON object", level="warning")
            return

        cfg = dict(self.charging_guard_config)
        for key in ("enabled", "roi_norm", "dark_threshold", "min_area_ratio",
                    "check_interval_ms", "required_hits", "warning_duration_ms",
                    "fan_enabled"):
            if key in config:
                cfg[key] = config[key]

        try:
            interval_ms = max(1000, int(cfg.get("check_interval_ms", 10000)))
            required_hits = max(1, int(cfg.get("required_hits", 6)))
            warning_duration_ms = max(0, int(cfg.get("warning_duration_ms", 3000)))
            fan_enabled = bool(cfg.get("fan_enabled", True))
            self.charging_guard_config.update({
                "enabled": bool(cfg.get("enabled", False)),
                "roi_norm": cfg.get("roi_norm", [0.1, 0.5, 0.3, 0.2]),
                "dark_threshold": int(cfg.get("dark_threshold", 90)),
                "min_area_ratio": float(cfg.get("min_area_ratio", 0.30)),
                "check_interval_ms": interval_ms,
                "required_hits": required_hits,
                "warning_duration_ms": warning_duration_ms,
                "fan_enabled": fan_enabled,
            })
            self.charging_guard_detector.configure(
                roi_norm=self.charging_guard_config["roi_norm"],
                dark_threshold=self.charging_guard_config["dark_threshold"],
                min_area_ratio=self.charging_guard_config["min_area_ratio"],
            )
            self.charging_guard_state.configure(required_hits)
            self.charging_guard_timer.setInterval(interval_ms)

            enabled = bool(self.charging_guard_config["enabled"])
            if hasattr(self, 'charging_guard_enable_checkbox'):
                old_state = self.charging_guard_enable_checkbox.blockSignals(True)
                self.charging_guard_enable_checkbox.setChecked(enabled)
                self.charging_guard_enable_checkbox.blockSignals(old_state)

            if enabled:
                self._reset_charging_guard("config updated")
                self._refresh_charging_guard_ui("Watching", enabled=True)
            else:
                self._reset_charging_guard("disabled")
                self._refresh_charging_guard_ui("Off", enabled=False)
            self._save_current_charging_guard_to_active_cage()
            self.log_message("Charging guard config loaded", level="success")
        except Exception as e:
            self.log_message(f"Warning: failed to load charging_guard config: {e}", level="warning")

    def _on_charging_guard_toggled(self, checked):
        self.charging_guard_config["enabled"] = bool(checked)
        self._save_current_charging_guard_to_active_cage()
        if checked:
            self.charging_guard_state.reset("enabled")
            self._refresh_charging_guard_ui("Watching", enabled=True)
            self.log_message("Charging guard enabled", level="info")
        else:
            self._reset_charging_guard("disabled")
            self._refresh_charging_guard_ui("Off", enabled=False)
            self.log_message("Charging guard disabled", level="info")

    def _get_habits_panel(self):
        if hasattr(self, 'habits_tab') and hasattr(self.habits_tab, 'habits_panel'):
            return self.habits_tab.habits_panel
        return None

    def _ensure_rf_off_for_habits_resume(self):
        if not getattr(self, 'rf_connected', False):
            self.log_message(
                "Warning: RF control serial not connected; cannot resume Habits safely",
                level="warning",
            )
            return False
        confirmed = bool(self._rf_set_power(False))
        if not confirmed:
            try:
                self._rf_query_status()
            except Exception:
                pass
            confirmed = getattr(self, 'rf_power_status', None) == 1
        if confirmed:
            self.log_message("Info: RF power OFF confirmed before Resume Habits", level="info")
            return True
        self.log_message(
            "Warning: Resume Habits blocked because RF power OFF was not confirmed",
            level="warning",
        )
        return False

    def _send_charging_guard_warning(self):
        panel = self._get_habits_panel()
        if panel is None or not hasattr(panel, 'send_charging_warning_command'):
            self.log_message("Warning: Habits panel is not available for charging guard", level="warning")
            return False
        duration_ms = int(self.charging_guard_config.get("warning_duration_ms", 3000))
        fan_enabled = bool(self.charging_guard_config.get("fan_enabled", True))
        return bool(panel.send_charging_warning_command(duration_ms, fan_enabled))

    def _send_charging_guard_stop(self):
        panel = self._get_habits_panel()
        if panel is None or not hasattr(panel, 'stop_charging_warning_command'):
            self.log_message("Warning: Habits panel is not available for charging guard stop", level="warning")
            return False
        return bool(panel.stop_charging_warning_command())

    def _reset_charging_guard(self, reason="reset", send_stop=True):
        if not hasattr(self, 'charging_guard_state'):
            return
        decision = self.charging_guard_state.reset(reason)
        if send_stop and decision.get("action") == "stop":
            self._send_charging_guard_stop()
        enabled = self.charging_guard_config.get("enabled", False)
        self._refresh_charging_guard_ui(
            decision.get("status", "Reset") if enabled else "Off",
            enabled=enabled,
        )

    def _charging_guard_rf_is_on(self):
        if not getattr(self, 'rf_connected', False):
            return False
        try:
            self._rf_query_status()
        except Exception:
            return False
        return getattr(self, 'rf_power_status', None) == 2

    def _poll_charging_guard(self):
        if not self.charging_guard_config.get("enabled", False):
            return

        reason = ""
        frame = None
        if not getattr(self, 'is_camera_on', False):
            reason = "camera off"
        elif not getattr(self.camera_module, 'is_camera_open', False):
            reason = "camera closed"
        else:
            frame = self.camera_module.get_frame()

        detection = self.charging_guard_detector.detect(frame)
        self.charging_guard_last_detection = detection
        mouse_present = bool(detection.get("mouse_present", False))

        if not reason and not mouse_present:
            reason = "mouse absent"

        rf_on = False
        if not reason:
            rf_on = self._charging_guard_rf_is_on()
            if not rf_on:
                reason = "RF off"

        decision = self.charging_guard_state.update(mouse_present and rf_on, reason)
        action = decision.get("action")
        if action == "warning":
            self._send_charging_guard_warning()
        elif action == "stop":
            self._send_charging_guard_stop()

        self._refresh_charging_guard_ui(decision.get("status", "Watching"), enabled=True)

    def _set_mouse_detection_label(self, detection):
        if not hasattr(self, 'mouse_detection_status_label'):
            return
        present = bool(detection.get("mouse_present", False))
        ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
        if present:
            self.mouse_detection_status_label.setText(f"Mouse: YES {ratio:.1f}%")
            self.mouse_detection_status_label.setStyleSheet(
                "background-color: #DCFCE7; color: #166534; border-radius: 4px; padding: 5px;"
            )
        else:
            self.mouse_detection_status_label.setText(f"Mouse: NO {ratio:.1f}%")
            self.mouse_detection_status_label.setStyleSheet(
                "background-color: #F3F4F6; color: #111827; border-radius: 4px; padding: 5px;"
            )

    def _refresh_charging_guard_ui(self, status_text, enabled=True):
        if hasattr(self, 'charging_guard_status_label'):
            self.charging_guard_status_label.setText(f"Guard: {status_text}")
            if not enabled:
                style = "background-color: #E5E7EB; color: #111827; border-radius: 4px; padding: 5px;"
            elif "Warning" in str(status_text):
                style = "background-color: #FEE2E2; color: #991B1B; border-radius: 4px; padding: 5px; font-weight: bold;"
            elif str(status_text).startswith("Reset"):
                style = "background-color: #FEF3C7; color: #92400E; border-radius: 4px; padding: 5px;"
            else:
                style = "background-color: #DBEAFE; color: #1E40AF; border-radius: 4px; padding: 5px;"
            self.charging_guard_status_label.setStyleSheet(style)
        self._set_mouse_detection_label(getattr(self, 'charging_guard_last_detection', {}))

    def get_charging_guard_detection_text(self):
        detection = getattr(self, 'charging_guard_last_detection', {})
        present = bool(detection.get("mouse_present", False))
        ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
        reason = str(detection.get("reason", "not checked"))
        guard_state = "ON" if self.charging_guard_config.get("enabled", False) else "OFF"
        return f"Charging Guard {guard_state} | Mouse: {'YES' if present else 'NO'} | area {ratio:.1f}% | {reason}"

    def annotate_charging_guard_frame(self, frame):
        if frame is None:
            return frame
        try:
            now = time.monotonic()
            last_preview = getattr(self, '_charging_guard_last_preview_detection_time', 0.0)
            if now - last_preview >= 0.25:
                self.charging_guard_last_detection = self.charging_guard_detector.detect(frame)
                self._charging_guard_last_preview_detection_time = now
                self._set_mouse_detection_label(self.charging_guard_last_detection)

            detection = getattr(self, 'charging_guard_last_detection', {})
            display_frame = frame.copy()
            roi = detection.get("roi", (0, 0, 0, 0))
            x, y, w, h = [int(v) for v in roi]
            present = bool(detection.get("mouse_present", False))
            color = (0, 220, 0) if present else (0, 180, 255)
            if w > 0 and h > 0:
                cv2.rectangle(display_frame, (x, y), (x + w, y + h), color, 2)
            ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
            text = f"Mouse {'YES' if present else 'NO'} {ratio:.1f}%"
            cv2.putText(
                display_frame,
                text,
                (max(10, x), max(30, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                color,
                2,
                cv2.LINE_AA,
            )
            return display_frame
        except Exception:
            return frame

    def on_trial_start(self, trial_num=1):
        """Handle trial start signal from Habits Panel"""
        self._reset_charging_guard(f"trial {trial_num} start")
        if self.mSerial:
            self.mSerial.trigger_alignment(trial_num)
            # Optionally log
            # self.log_message(f"Trial Start {trial_num} detected, alignment signal triggered", level="info")

    def _set_serial_dependent_controls_enabled(self, enabled):
        widgets = [
            'start_sampling_button', 'stop_sampling_button', 'sampling_mode_combo', 'mode3_reref_mode_combo',
            'lfp_tab.start_save_button', 'lfp_tab.stop_save_button', 'lfp_tab.start_save_mode3_button',
            'lfp_tab.stop_save_mode3_button', 'spike1ch_tab.start_save_button', 'spike1ch_tab.stop_save_button',
            'spike4ch_tab.start_save_button', 'spike4ch_tab.stop_save_button', 'spike1ch_tab.send_command_button',
            'spike1ch_tab.send_mode3_command_button', 'spike1ch_tab.mode3_channel_combo',
            'raster_tab.auto_threshold_button', 'raster_tab.channel_combo',
            'raster_tab.threshold_combo', 'update_battery_button', 'lfp_tab.update_scale_button',
            'plot_update_toggle_button', 'impedance_tab.start_test_button'
        ]
        for path in widgets:
            obj = self
            try:
                for name in path.split('.'):
                    obj = getattr(obj, name)
                obj.setEnabled(enabled)
            except Exception:
                pass

    def _is_port_available(self, port_name):
        if not port_name:
            return False
        try:
            return any(p.device == port_name for p in serial.tools.list_ports.comports())
        except Exception:
            return True

    def _get_command_payload_length(self, command):
        if isinstance(command, str):
            return len(command.encode("utf-8"))
        if isinstance(command, (bytes, bytearray, memoryview)):
            return len(command)
        if isinstance(command, np.ndarray):
            return int(command.size)
        if isinstance(command, (list, tuple)):
            return len(command)
        return None

    def _is_neural_serial_alive(self, require_recent_packets=False, max_silence_s=8.0):
        if self.mSerial is None:
            return False, "Neural serial object is missing"
        if not getattr(self, 'main_serial_connected', False):
            return False, "Neural serial is not marked connected"
        try:
            if not self.mSerial.isRunning():
                return False, "Neural serial reader thread is not running"
        except Exception:
            pass
        try:
            if hasattr(self.mSerial, '_port_is_open') and not self.mSerial._port_is_open():
                return False, "Neural serial port is closed"
        except Exception:
            pass
        port_name = getattr(self, 'curr_active_ports', None)
        if port_name and not self._is_port_available(port_name):
            return False, f"Neural serial port {port_name} is no longer present"
        # NOTE:
        # Do NOT use packet silence as disconnection criterion.
        # In some experiments, neural packets can legitimately pause for long periods.
        # Keep only hard-failure checks (thread/port/presence).
        _ = require_recent_packets
        _ = max_silence_s
        return True, ""

    def _send_neural_command(self, command, action_name, transport_retries=1):
        if not self._ensure_serial_connected(action_name):
            return False
        expected_len = self._get_command_payload_length(command)
        send_ok = False
        try:
            if hasattr(self.mSerial, "send_command_frame"):
                send_ok = bool(self.mSerial.send_command_frame(command, repeats=transport_retries))
            else:
                bytes_written = self.mSerial.send_data(command)
                if expected_len is None:
                    send_ok = bytes_written > 0
                else:
                    send_ok = (bytes_written == expected_len)
        except Exception as e:
            self.log_message(
                f"Error: Failed to send neural command for {action_name} ({str(e)})",
                level="error"
            )
            return False
        if not send_ok:
            self.log_message(
                f"Error: Failed to send neural command for {action_name}",
                level="error"
            )
            return False
        return True

    def _poll_neural_serial_health(self):
        if getattr(self, 'main_serial_connected', False) and self.mSerial is not None:
            alive, reason = self._is_neural_serial_alive(require_recent_packets=True)
            if not alive:
                self._handle_serial_disconnected(reason)
                return
        target = getattr(self, '_serial_auto_reconnect_target', None)
        if not target or getattr(self, 'main_serial_connected', False):
            return
        if self._serial_auto_reconnect_attempts >= self._serial_auto_reconnect_max_attempts:
            if not self._serial_auto_reconnect_exhausted:
                self._serial_auto_reconnect_exhausted = True
                self.log_message(
                    f"Error: Automatic reconnect to {target} failed after "
                    f"{self._serial_auto_reconnect_max_attempts} attempts",
                    level="error"
                )
            return
        if not self._is_port_available(target):
            return
        self._serial_auto_reconnect_attempts += 1
        self.log_message(
            f"Info: Auto reconnect attempt {self._serial_auto_reconnect_attempts}/"
            f"{self._serial_auto_reconnect_max_attempts} to {target}",
            level="info"
        )
        ok = self.set_connected_port(target)
        if ok:
            self.main_serial_connected = True
            self.set_main_serial_ui_connected(True, target)
            self._set_serial_dependent_controls_enabled(True)
            self._serial_auto_reconnect_target = None
            self._serial_auto_reconnect_attempts = 0
            self._serial_auto_reconnect_exhausted = False
            self.log_message(f"Success: Neural serial reconnected automatically: {target}", level="success")
            self.statusBar().showMessage(f"Reconnected to {target}")

    def _mark_rf_connection_lost(self, reason):
        if not getattr(self, 'rf_connected', False) and getattr(self, 'rf_serial_connection', None) is None:
            return
        self.rf_power_status = 0
        self.disconnect_rf_serial()
        self.log_message(f"Warning: RF serial connection lost: {reason}", level="warning")
        try:
            self.statusBar().showMessage(f"RF serial lost: {reason}")
        except Exception:
            pass

    def _is_rf_serial_alive(self):
        conn = getattr(self, 'rf_serial_connection', None)
        if not getattr(self, 'rf_connected', False) or conn is None:
            return False, "RF serial is not connected"
        try:
            if not bool(getattr(conn, 'is_open', False)):
                return False, "RF serial port is closed"
        except Exception:
            return False, "RF serial handle is invalid"
        port_name = getattr(conn, 'port', None)
        if port_name and not self._is_port_available(port_name):
            return False, f"RF serial port {port_name} is no longer present"
        return True, ""

    def _poll_rf_serial_health(self):
        if not getattr(self, 'rf_connected', False):
            return
        alive, reason = self._is_rf_serial_alive()
        if not alive:
            self._mark_rf_connection_lost(reason)

    def _ensure_serial_connected(self, action_name):
        if self.mSerial is None or not getattr(self, 'main_serial_connected', False):
            self.log_message(f"Warning: Please connect Neural serial first before {action_name}", level="warning")
            return False
        alive, reason = self._is_neural_serial_alive(require_recent_packets=False)
        if not alive:
            self._handle_serial_disconnected(reason)
            self.log_message(f"Warning: Neural serial is unavailable before {action_name}", level="warning")
            return False
        return True

    def toggle_main_serial_connection(self):
        if getattr(self, 'main_serial_connected', False):
            self.disconnect_main_serial()
        else:
            self.connect_main_serial()

    def restart_relay_device(self):
        if not self._ensure_serial_connected("restarting relay"):
            return
        relay_port = getattr(self, 'curr_active_ports', None)
        if not relay_port:
            self.log_message("Warning: Relay port is unknown, cannot restart relay", level="warning")
            return
        confirm = QMessageBox.question(
            self,
            "Restart Relay",
            (
                f"Restart relay on {relay_port}?\n\n"
                "This will briefly disconnect the neural serial and then reconnect automatically."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            success = False
            if hasattr(self.mSerial, "send_relay_control_command"):
                success = bool(self.mSerial.send_relay_control_command("reboot", repeats=1))
            if not success:
                self.log_message("Error: Failed to send relay reboot command", level="error")
                return
            self.log_message(f"Info: Relay reboot command sent to {relay_port}", level="info")
            self.statusBar().showMessage(f"Restarting relay on {relay_port}...")
            self.disconnect_main_serial()
            self._serial_auto_reconnect_target = relay_port
            self._serial_auto_reconnect_attempts = 0
            self._serial_auto_reconnect_exhausted = False
            self.log_message(f"Info: Waiting for relay {relay_port} to come back online", level="info")
        except Exception as e:
            self.log_message(f"Error: Failed to restart relay: {str(e)}", level="error")

    def _get_selected_relay_esb_channel(self):
        if not hasattr(self, "relay_esb_channel_combo"):
            return None
        channel = self.relay_esb_channel_combo.currentData()
        if channel is None:
            match = re.search(r"(\d+)", self.relay_esb_channel_combo.currentText())
            if match is None:
                return None
            channel = int(match.group(1))
        return int(channel)

    def apply_relay_esb_channel(self):
        if not self._ensure_serial_connected("setting ESB channel"):
            return
        relay_port = getattr(self, 'curr_active_ports', None)
        if not relay_port:
            self.log_message("Warning: Relay port is unknown, cannot set ESB channel", level="warning")
            return
        channel = self._get_selected_relay_esb_channel()
        if channel not in RELAY_RECOMMENDED_ESB_CHANNELS:
            self.log_message("Error: Selected relay ESB channel is invalid", level="error")
            return
        confirm = QMessageBox.question(
            self,
            "Apply ESB Channel",
            (
                f"Switch both the relay and the peripheral to ESB CH{channel} on {relay_port}?\n\n"
                "This will first send a runtime channel-switch command to the peripheral firmware, "
                "then switch the relay listener to the same channel.\n\n"
                "Use this only after both the relay and the peripheral on this link are flashed "
                "with the runtime channel-switch firmware."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            peripheral_success = False
            relay_success = False
            device_command = build_runtime_device_esb_channel_command(channel)
            if self.mSerial is not None:
                self.mSerial.begin_mode_transition(600)
            peripheral_success = self._send_neural_command(
                device_command,
                f"setting peripheral ESB channel CH{channel}",
                transport_retries=2,
            )
            if not peripheral_success:
                self.log_message(
                    f"Error: Failed to send peripheral ESB channel command for CH{channel}",
                    level="error",
                )
                return
            time.sleep(0.25)
            command_name = f"set_esb_channel_{channel}"
            if hasattr(self.mSerial, "send_relay_control_command"):
                relay_success = bool(self.mSerial.send_relay_control_command(command_name, repeats=1))
            if not relay_success:
                self.log_message(f"Error: Failed to send relay ESB channel command for CH{channel}", level="error")
                return
            self.log_message(
                f"Info: Peripheral and relay ESB channel switch commands sent to {relay_port}: CH{channel}",
                level="info",
            )
            self.log_message(
                "Info: Complete one-click ESB channel switching requires both the relay and the peripheral to run the updated firmware",
                level="info",
            )
            self.log_message(
                "Info: Older firmware that does not support runtime ESB channel switching will ignore the new command",
                level="info",
            )
            self.statusBar().showMessage(f"Peripheral + relay ESB channel switch sent: CH{channel}")
        except Exception as e:
            self.log_message(f"Error: Failed to set relay ESB channel: {str(e)}", level="error")

    def connect_main_serial(self):
        selected_port = self.main_serial_combo.currentData()
        if selected_port is None or selected_port == "":
            selected_port = self.main_serial_combo.currentText()
            if " - " in selected_port:
                selected_port = selected_port.split(" - ", 1)[0]
        if selected_port in ("Select Neural Port", "No ports available", "", None):
            self.log_message("Warning: Please select a valid Neural serial port first", level="warning")
            return
        try:
            rf_port = None
            if getattr(self, "rf_serial_connection", None) is not None:
                rf_port = getattr(self.rf_serial_connection, "port", None)
            if getattr(self, "rf_connected", False) and rf_port and str(rf_port) == str(selected_port):
                self.log_message(
                    f"Error: Neural port {selected_port} is already occupied by RF control serial. "
                    "Please disconnect RF or choose another port.",
                    level="error"
                )
                return
        except Exception:
            pass
        if getattr(self, 'main_serial_connected', False):
            if self.curr_active_ports == selected_port and self.mSerial is not None:
                return
            self.disconnect_main_serial()
        ok = self.set_connected_port(selected_port)
        if ok:
            self.main_serial_connected = True
            self.set_main_serial_ui_connected(True, selected_port)
            self._set_serial_dependent_controls_enabled(True)
            self._serial_auto_reconnect_target = None
            self._serial_auto_reconnect_attempts = 0
            self._serial_auto_reconnect_exhausted = False
            self.log_message(f"Success: Neural serial connected: {selected_port}", level="success")
        else:
            self.main_serial_connected = False
            self.set_main_serial_ui_connected(False)
            self._set_serial_dependent_controls_enabled(False)

    def _active_serial_save_modes(self, serial_thread):
        modes = []
        if serial_thread is None:
            return modes
        if bool(getattr(serial_thread, "save_file_lfp_flag", False)):
            modes.append("lfp")
        if bool(getattr(serial_thread, "save_file_mode1_flag", False)):
            modes.append("mode1")
        if bool(getattr(serial_thread, "save_file_mode2_flag", False)):
            modes.append("mode2")
        if bool(getattr(serial_thread, "save_file_mode3_flag", False)):
            modes.append("mode3")
        return modes

    def disconnect_main_serial(self, finalize=True, wait_ms=1000):
        if self.mSerial is None and not getattr(self, 'main_serial_connected', False):
            return
        serial_thread = self.mSerial
        try:
            if serial_thread is not None:
                try:
                    serial_thread.stop(finalize=False)
                except Exception:
                    pass
                try:
                    serial_thread.wait(int(wait_ms))
                except Exception:
                    pass
                if finalize:
                    try:
                        active_modes = self._active_serial_save_modes(serial_thread)
                        if hasattr(serial_thread, "finalize_save_buffers"):
                            serial_thread.finalize_save_buffers(modes=active_modes or None)
                    except Exception:
                        logging.error("Failed to finalize neural save buffers during disconnect", exc_info=True)
                try:
                    serial_thread.port_close(finalize=False)
                except Exception:
                    pass
        finally:
            self.mSerial = None
            self.curr_active_ports = None
            self.main_serial_connected = False
            self._serial_auto_reconnect_target = None
            self._serial_auto_reconnect_attempts = 0
            self._serial_auto_reconnect_exhausted = False
            self.set_main_serial_ui_connected(False)
            self._set_serial_dependent_controls_enabled(False)
            self.statusBar().showMessage("Neural serial disconnected")
            self.log_message("Info: Neural serial disconnected", level="info")

    def _handle_serial_disconnected(self, error_msg):
        """Handle unexpected serial disconnection detected by the SerialPort thread.
        This slot is invoked from the SerialPort thread via Qt signal, so it runs on the main thread.
        """
        if self._serial_disconnect_handling:
            return
        self._serial_disconnect_handling = True
        try:
            disconnected_port = getattr(self, 'curr_active_ports', None)
            self.log_message(f"Error: Serial connection lost: {error_msg}", level="error")

            try:
                if self.mSerial is not None:
                    try:
                        self.mSerial.wait(500)
                    except Exception:
                        pass
            except Exception:
                pass
            self.mSerial = None
            self.main_serial_connected = False
            self.set_main_serial_ui_connected(False)
            self._set_serial_dependent_controls_enabled(False)
            self.statusBar().showMessage("Serial connection lost, waiting to reconnect")
            self.refresh_main_serial_ports()

            if disconnected_port:
                self._serial_auto_reconnect_target = disconnected_port
                self._serial_auto_reconnect_attempts = 0
                self._serial_auto_reconnect_exhausted = False
                self.log_message(f"Info: Automatic reconnect scheduled for {disconnected_port}", level="info")
        finally:
            self._serial_disconnect_handling = False

    def _attempt_reconnect(self, port, max_retries=3, delay_ms=2000):
        """Try to reconnect to the given serial port with retries."""
        import time as _time
        for attempt in range(1, max_retries + 1):
            self.log_message(f"Info: Reconnect attempt {attempt}/{max_retries} to {port}...", level="info")
            self.statusBar().showMessage(f"Reconnecting ({attempt}/{max_retries})...")

            # Brief delay to allow USB / Bluetooth to re-enumerate
            from PyQt6.QtCore import QThread
            QThread.msleep(delay_ms)
            from PyQt6.QtWidgets import QApplication
            QApplication.processEvents()

            # Check if port exists
            try:
                if not self._is_port_available(port):
                    self.log_message(f"Warning: Port {port} not found, waiting...", level="warning")
                    continue
            except Exception:
                pass

            # Try to connect
            ok = self.set_connected_port(port)
            if ok:
                self.main_serial_connected = True
                self.set_main_serial_ui_connected(True, port)
                self._set_serial_dependent_controls_enabled(True)
                self.log_message(f"Success: Reconnected to {port} on attempt {attempt}", level="success")
                self.statusBar().showMessage(f"Reconnected to {port}")
                return True

        self.log_message(f"Error: Failed to reconnect to {port} after {max_retries} attempts", level="error")
        self.statusBar().showMessage("Reconnection failed")
        return False

    def _close_progress_step(self, dialog, value, text):
        if dialog is None:
            return
        try:
            dialog.setLabelText(str(text))
            dialog.setValue(int(value))
            QApplication.processEvents()
        except Exception:
            pass

    def _stop_timer_attr(self, name):
        timer = getattr(self, name, None)
        if timer is None:
            return
        try:
            timer.stop()
        except Exception:
            pass

    def _shutdown_resources_with_progress(self, dialog=None):
        timer_names = (
            "watchdog_timer",
            "RF_timer",
            "mode3_trial_trigger_guard_timer",
            "video_segment_timer",
            "charging_guard_timer",
            "neural_serial_health_timer",
            "rf_serial_health_timer",
            "camera_timer",
            "camera_health_timer",
            "_battery_timeline_refresh_timer",
        )
        self._close_progress_step(dialog, 1, "Stopping GUI timers...")
        for timer_name in timer_names:
            self._stop_timer_attr(timer_name)

        self._close_progress_step(dialog, 2, "Stopping video and camera...")
        try:
            if getattr(self, "is_recording", False):
                self.camera_module.stop_recording()
        except Exception:
            pass
        try:
            if getattr(self, "is_camera_on", False):
                self.camera_module.close_camera()
        except Exception:
            pass
        try:
            if hasattr(self, "camera_window") and self.camera_window:
                self.camera_window.close()
        except Exception:
            pass

        self._close_progress_step(dialog, 3, "Closing Habits serial worker...")
        serial_worker = None
        try:
            habits_panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
            serial_worker = getattr(habits_panel, "serial_worker", None)
            if serial_worker is not None and hasattr(serial_worker, "stop"):
                serial_worker.stop(wait_ms=1000)
        except TypeError:
            try:
                serial_worker.stop()
            except Exception:
                pass
        except Exception:
            pass

        self._close_progress_step(dialog, 4, "Closing RF serial...")
        try:
            if getattr(self, "rf_connected", False) or getattr(self, "rf_serial_connection", None) is not None:
                self.disconnect_rf_serial()
        except Exception:
            pass

        self._close_progress_step(dialog, 5, "Stopping neural reader and EDF writer...")
        try:
            self.disconnect_main_serial(finalize=True, wait_ms=1200)
        except Exception:
            pass

        self._close_progress_step(dialog, 6, "Flushing local history...")
        try:
            if hasattr(self, "battery_history_manager"):
                self.battery_history_manager.flush()
        except Exception:
            pass

    def closeEvent(self, event):
        if getattr(self, "_closing_in_progress", False):
            event.accept()
            return
        reply = QMessageBox.question(
            self,
            'check quit',
            'Are you sure to quit?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self._closing_in_progress = True
        dialog = QProgressDialog("Closing GUI...", None, 0, 6, self)
        dialog.setWindowTitle("Closing")
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setCancelButton(None)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        dialog.show()
        QApplication.processEvents()
        try:
            self._shutdown_resources_with_progress(dialog)
        finally:
            try:
                dialog.setValue(6)
                dialog.close()
            except Exception:
                pass
            print("Main window closed.")
            event.accept()

    def _kick_watchdog(self):
        """Update the timestamp to prove the main thread is alive"""
        self.last_alive_timestamp = time.time()

    def open_spectrum_window(self):
        """Open LFP spectrum analysis window"""
        if self.spectrum_window is None:
            self.spectrum_window = LFPSpectrumWindow(self)
        
        self.spectrum_window.show()
        self.spectrum_window.raise_()
        self.spectrum_window.activateWindow()

    def update_lfp_scale(self):
        """Update LFP and ESA/MAND Y-axis scale"""
        try:
            old_interval = self.separate_interval
            self.separate_interval = int(self.lfp_tab.scale_combo.currentText())
            self.esa_scale_factor = int(self.lfp_tab.esa_scale_combo.currentText())
            self.mode0_mand_display_smooth_ms = self._selected_mode0_mand_display_smooth_ms()

            if old_interval != self.separate_interval:
                for channel in range(self.lfp_channel_num):
                    offset_delta = channel * (self.separate_interval - old_interval)
                    self.LFP_raw_data[channel] = self.LFP_raw_data[channel] + offset_delta
                    self.ESA_raw_data[channel] = self.ESA_raw_data[channel] + offset_delta

            lfp_ticks = {}
            esa_ticks = {}
            for channel in range(self.lfp_channel_num):
                y_pos = int(channel * self.separate_interval)
                lfp_ticks[y_pos] = f'LFP{channel}'
                esa_ticks[y_pos] = f'{getattr(self, "lfp_feature_tick_prefix", "ESA")}{channel}'

            self.LFP_pI_channel.getAxis("left").setTicks([lfp_ticks.items()])
            self.ESA_pI_channel.getAxis("left").setTicks([esa_ticks.items()])

            y_min = -self.separate_interval
            y_max = (self.lfp_channel_num + 1) * self.separate_interval
            self.LFP_v1_channel.setLimits(xMin=0, xMax=self.lfp_display_data_num, yMin=y_min, yMax=y_max)
            self.LFP_v1_channel.setYRange(y_min, y_max)
            self.ESA_v1_channel.setLimits(xMin=0, xMax=self.lfp_display_data_num, yMin=y_min, yMax=y_max)
            self.ESA_v1_channel.setYRange(y_min, y_max)

            for i in range(self.lfp_channel_num):
                self.LFP_raw_channel[i].setData(self.LFP_x, self.LFP_raw_data[i], pen=self.pens_solid[i])
                self.ESA_raw_channel[i].setData(self.ESA_x, self.ESA_raw_data[i], pen=self.pens_dashed[i])

            self.statusBar().showMessage(f"LFP Scale Updated: Interval={self.separate_interval}, ESA&MAND Scale={self.esa_scale_factor}")
        except ValueError:
            self.statusBar().showMessage("Error: Invalid scale values")

    def _selected_mode0_mand_display_smooth_ms(self):
        combo = getattr(getattr(self, "lfp_tab", None), "mand_smooth_combo", None)
        try:
            value = int(float(combo.currentText())) if combo is not None else int(self.mode0_mand_display_smooth_ms)
        except Exception:
            value = int(getattr(self, "mode0_mand_display_smooth_ms", 4) or 4)
        return max(4, value)

    def _reset_mode0_mand_display_smoothing(self):
        channel_count = int(getattr(self, "esa_channel_num", 16) or 16)
        self._mode0_mand_display_history = [np.array([], dtype=np.float32) for _ in range(channel_count)]

    def on_mode0_mand_display_smooth_changed(self, _text=None):
        self.mode0_mand_display_smooth_ms = self._selected_mode0_mand_display_smooth_ms()
        self._reset_mode0_mand_display_smoothing()
        try:
            self.statusBar().showMessage(f"Mode0 MAND display smoothing: {self.mode0_mand_display_smooth_ms} ms")
        except Exception:
            pass

    # Spike 单通道滤波相关方法
    def _update_spike_filter_coeffs(self):
        """根据当前参数计算带通滤波器系数（Butterworth）"""
        fs = float(self.spike_filter_fs)
        low = max(1.0, float(self.spike_filter_low_cut))
        high = min(fs/2 - 1.0, float(self.spike_filter_high_cut))
        if high <= low:
            high = low + 1.0
        nyq = fs / 2.0
        wn = [low/nyq, high/nyq]
        try:
            self._spike_filter_b, self._spike_filter_a = signal.butter(4, wn, btype='bandpass')
        except Exception:
            self._spike_filter_b, self._spike_filter_a = None, None

    def _apply_spike_filter_buffer(self):
        """对显示缓冲进行滤波，结果写入 spike_filtered_data"""
        if not self.spike_filter_enabled:
            return
        if self._spike_filter_b is None or self._spike_filter_a is None:
            self._update_spike_filter_coeffs()
            if self._spike_filter_b is None:
                return
        x = np.array(self.spike_raw_data[0], dtype=np.float64)
        nan_mask = np.isnan(x)
        if np.all(nan_mask):
            return
        x[nan_mask] = 0.0
        try:
            y = signal.lfilter(self._spike_filter_b, self._spike_filter_a, x)
        except Exception:
            return
        y[nan_mask] = np.nan
        self.spike_filtered_data[0] = y

    def on_spike_filter_toggle(self, checked):
        self.spike_filter_enabled = bool(checked)
        if self.spike_filter_enabled:
            self._update_spike_filter_coeffs()
            self._apply_spike_filter_buffer()
        # 立即刷新显示
        try:
            if self.plot_update_enabled:
                if int(getattr(self, "current_sample_mode", -1)) == 0:
                    raw_has_wrapped = bool(getattr(self, "_mode0_raw_has_wrapped", False))
                    y_values = self.spike_filtered_data[0] if self.spike_filter_enabled else self.spike_raw_data[0]
                    x_view, y_view = self._single_raw_xy_for_display(y_values, "lfp", raw_has_wrapped)
                    self.raw_dataline_channel.setData(
                        x_view,
                        y_view,
                        pen=pg.mkPen({'color': 'c' if self.spike_filter_enabled else 'w' ,'width':1})
                    )
                    return
                if self.spike_filter_enabled:
                    self.raw_dataline_channel.setData(self.spike_x, self.spike_filtered_data[0], pen=pg.mkPen({'color': 'c' ,'width':1}))
                else:
                    self.raw_dataline_channel.setData(self.spike_x, self.spike_raw_data[0], pen=pg.mkPen({'color': 'w' ,'width':1}))
        except Exception:
            pass

    def _update_spike1ch_rms_label(self):
        try:
            if self.spike_filter_enabled:
                y = self.spike_filtered_data[0]
            else:
                y = self.spike_raw_data[0]

            y = np.asarray(y, dtype=np.float64)
            mask = np.isfinite(y)
            if not np.any(mask):
                rms = np.nan
            else:
                rms = float(np.sqrt(np.mean(y[mask] * y[mask])))

            if hasattr(self, 'spike1ch_tab') and hasattr(self.spike1ch_tab, 'rms_label'):
                if np.isfinite(rms):
                    self.spike1ch_tab.rms_label.setText(f"{rms:.1f} uVrms")
                else:
                    self.spike1ch_tab.rms_label.setText("-- uVrms")
        except Exception:
            pass

    def _mark_spike1ch_rms_samples(self, sample_count):
        try:
            sample_count = max(0, int(sample_count or 0))
        except Exception:
            sample_count = 0
        if sample_count <= 0:
            return

        try:
            self.spike1ch_rms_pending += sample_count
        except Exception:
            self.spike1ch_rms_pending = sample_count

        try:
            threshold = max(1, int(self.spike_display_data_num))
        except Exception:
            threshold = sample_count
        if self.spike1ch_rms_pending >= threshold:
            self.spike1ch_rms_pending = 0
            self._update_spike1ch_rms_label()

    def on_spike_filter_params_changed(self):
        self.spike_filter_low_cut = float(self.spike1ch_tab.low_cut_spin.value())
        self.spike_filter_high_cut = float(self.spike1ch_tab.high_cut_spin.value())
        if self.spike_filter_enabled:
            self._update_spike_filter_coeffs()
            self._apply_spike_filter_buffer()
            try:
                if self.plot_update_enabled:
                    if int(getattr(self, "current_sample_mode", -1)) == 0:
                        x_view, y_view = self._single_raw_xy_for_display(
                            self.spike_filtered_data[0],
                            "lfp",
                            bool(getattr(self, "_mode0_raw_has_wrapped", False)),
                        )
                        self.raw_dataline_channel.setData(x_view, y_view, pen=pg.mkPen({'color': 'c' ,'width':1}))
                    else:
                        self.raw_dataline_channel.setData(self.spike_x, self.spike_filtered_data[0], pen=pg.mkPen({'color': 'c' ,'width':1}))
            except Exception:
                pass

    def on_spike_sample_rate_changed(self, idx):
        # 0 -> 12500 Hz ; 1 -> 20000 Hz
        self.spike_filter_fs = 12500.0 if idx == 0 else 20000.0
        if self.spike_filter_enabled:
            self._update_spike_filter_coeffs()
            self._apply_spike_filter_buffer()
            try:
                if self.plot_update_enabled:
                    if int(getattr(self, "current_sample_mode", -1)) == 0:
                        x_view, y_view = self._single_raw_xy_for_display(
                            self.spike_filtered_data[0],
                            "lfp",
                            bool(getattr(self, "_mode0_raw_has_wrapped", False)),
                        )
                        self.raw_dataline_channel.setData(x_view, y_view, pen=pg.mkPen({'color': 'c' ,'width':1}))
                    else:
                        self.raw_dataline_channel.setData(self.spike_x, self.spike_filtered_data[0], pen=pg.mkPen({'color': 'c' ,'width':1}))
            except Exception:
                pass

    def _set_spike_sample_rate_for_stream(self, sample_rate_hz):
        try:
            sample_rate_hz = float(sample_rate_hz)
        except Exception:
            return
        target_index = 0 if sample_rate_hz <= 12500.0 else 1
        if abs(float(getattr(self, "spike_filter_fs", 0.0) or 0.0) - sample_rate_hz) < 0.5:
            return
        self.spike_filter_fs = 12500.0 if target_index == 0 else 20000.0
        try:
            combo = self.spike1ch_tab.sample_rate_combo
            combo.blockSignals(True)
            combo.setCurrentIndex(target_index)
            combo.blockSignals(False)
        except Exception:
            pass
        if self.spike_filter_enabled:
            self._update_spike_filter_coeffs()
            self._apply_spike_filter_buffer()

    def _reset_single_raw_chart_buffers(self, *, clear_raster=False):
        self.ring_spike_pointer = 0
        self.spike_raw_data.fill(np.nan)
        self.spike_filtered_data.fill(np.nan)
        self.alignment_buffer.fill(0)
        self._last_mode0_raw_channel = None
        self._mode0_raw_has_wrapped = False
        self._spike_raw_has_wrapped = False
        self._reset_chart_render_timing(("spike", "mode3_raw", "lfp"))
        if clear_raster:
            self.ring_spike_raster_pointer = 0
            self.spike_raster_data.fill(np.nan)

    def _reset_lfp_chart_buffers(self):
        self.ring_lfp_pointer = 0
        self.LFP_raw_data.fill(np.nan)
        self.ESA_raw_data.fill(np.nan)
        self._lfp_has_wrapped = False
        self._reset_mode0_mand_display_smoothing()
        self._reset_chart_render_timing(("lfp", "mode3_lfp_esa"))

    def _reset_mode2_chart_buffers(self):
        self.ring_spike_mode2_pointer = 0
        self.spike_mode2_raw_data.fill(np.nan)
        self.mode2_alignment_data.fill(np.nan)
        self.spike_mode2_curr_channel = [0 for _ in range(16)]
        self._mode2_has_wrapped = False
        self._reset_chart_render_timing(("mode2",))

    def _reset_chart_render_timing(self, stream_keys=None):
        if stream_keys is None:
            stream_keys = self._display_tuning_stream_keys()
        last_plot_by_stream = getattr(self, "_last_plot_render_monotonic_by_stream", None)
        if isinstance(last_plot_by_stream, dict):
            for stream_key in stream_keys:
                normalized = self._display_refresh_stream_key(stream_key)
                last_plot_by_stream.pop(normalized, None)
                try:
                    legacy_key = {0: "lfp", 1: "spike", 2: "mode2"}.get(int(stream_key), stream_key)
                    last_plot_by_stream.pop(legacy_key, None)
                except Exception:
                    pass
        last_setdata_by_stream = getattr(self, "_last_plot_setdata_monotonic_by_stream", None)
        if isinstance(last_setdata_by_stream, dict):
            for stream_key in stream_keys:
                last_setdata_by_stream.pop(self._display_refresh_stream_key(stream_key), None)
        actual_hz = getattr(self, "display_render_actual_hz_by_stream", None)
        if isinstance(actual_hz, dict):
            for stream_key in stream_keys:
                actual_hz[self._display_refresh_stream_key(stream_key)] = 0.0
        hz_by_stream = getattr(self, "display_refresh_hz_by_stream", None)
        if isinstance(hz_by_stream, dict):
            for stream_key in stream_keys:
                hz_by_stream[self._display_refresh_stream_key(stream_key)] = float(getattr(self, "display_refresh_base_hz", 12.0) or 12.0)
        stable = getattr(self, "display_refresh_stable_windows", None)
        if isinstance(stable, dict):
            for stream_key in stream_keys:
                stable[self._display_refresh_stream_key(stream_key)] = 0
        actions = getattr(self, "display_refresh_last_action", None)
        if isinstance(actions, dict):
            for stream_key in stream_keys:
                actions[self._display_refresh_stream_key(stream_key)] = "reset"

    def _refresh_plot_update_toggle_style(self):
        if self.plot_update_enabled:
            self.plot_update_toggle_button.setText("Plot Update: On")
            self.plot_update_toggle_button.setStyleSheet(
                "QPushButton { background-color: #4CAF50; color: white; border: none; border-radius: 6px; padding: 6px 10px; font-weight: bold; }"
                "QPushButton:pressed { background-color: #43A047; }"
            )
        else:
            self.plot_update_toggle_button.setText("Plot Update: Off")
            self.plot_update_toggle_button.setStyleSheet(
                "QPushButton { background-color: #F44336; color: white; border: none; border-radius: 6px; padding: 6px 10px; font-weight: bold; }"
                "QPushButton:pressed { background-color: #E53935; }"
            )

    def on_plot_update_toggle_clicked(self, checked: bool):
        self.plot_update_enabled = bool(checked)
        if self.plot_update_enabled:
            self._last_plot_render_monotonic = 0.0
            self._reset_chart_render_timing()
        self._refresh_plot_update_toggle_style()
        if self.plot_update_enabled:
            self.statusBar().showMessage("Plot updates enabled")
        else:
            self.statusBar().showMessage("Plot updates disabled (data refresh paused)")

    def _refresh_mode3_reref_control_style(self):
        if self.mode3_esa_reref_mode == 2:
            self.mode3_reref_mode_combo.setStyleSheet("background-color: #4CAF50; color: white; border-radius: 4px;")
        elif self.mode3_esa_reref_mode == 1:
            self.mode3_reref_mode_combo.setStyleSheet("background-color: #2196F3; color: white; border-radius: 4px;")
        else:
            self.mode3_reref_mode_combo.setStyleSheet("background-color: #F44336; color: white; border-radius: 4px;")

    def on_mode3_reref_mode_changed(self, mode: int):
        if mode not in (0, 1, 2):
            mode = 0
        self.mode3_esa_reref_mode = int(mode)
        self._refresh_mode3_reref_control_style()
        if self.mSerial is None:
            self.statusBar().showMessage("Mode0/3 ReRef updated locally, connect neural serial to apply")
            return
        cmd = [0x00, 0x06, self.mode3_esa_reref_mode, 0x00]
        try:
            if not self._send_neural_command(cmd, "setting Mode0/3 median re-reference"):
                return
            state_text = ["OFF", "FAST", "STABLE"][self.mode3_esa_reref_mode]
            self.log_message(f"Mode0/3 median re-reference: {state_text}", level="success")
            self.statusBar().showMessage(f"Mode0/3 median re-reference set to {state_text}")
        except Exception as e:
            self.log_message(f"Error: failed to set Mode0/3 median re-reference: {str(e)}", level="error")
        
    class _SpikeSpectrumDialog(QDialog):
        def __init__(self, parent=None):
            super().__init__(parent)
            self.setWindowTitle("Spike Spectrum")
            self.resize(800, 400)
            layout = QVBoxLayout(self)
            self.plot_widget = pg.PlotWidget()
            layout.addWidget(self.plot_widget)

    def open_spike_spectrum(self):
        """打开频谱窗口，计算当前显示缓冲的幅度谱"""
        dlg = self._SpikeSpectrumDialog(self)
        x = self.spike_filtered_data[0] if self.spike_filter_enabled else self.spike_raw_data[0]
        try:
            sig = np.array(x, dtype=np.float64)
            sig[np.isnan(sig)] = 0.0
            fs = float(self.spike_filter_fs)
            n = len(sig)
            if n > 1:
                freqs = np.fft.rfftfreq(n, d=1.0/fs)
                spectrum = np.abs(np.fft.rfft(sig))
                dlg.plot_widget.plot(freqs, spectrum, pen=pg.mkPen({'color': 'y', 'width': 2}))
                dlg.plot_widget.setLabel('bottom', 'Frequency', units='Hz')
                dlg.plot_widget.setLabel('left', 'Amplitude')
                dlg.plot_widget.setTitle("Spike Spectrum ({} Hz)".format(int(fs)))
        except Exception:
            pass
        dlg.show()
        dlg.exec()

    @staticmethod
    def _mode_label_to_sample_index(mode_label):
        try:
            numeric_mode = int(mode_label)
        except Exception:
            numeric_mode = None
        if numeric_mode in (0, 1, 2, 3):
            return int(numeric_mode)
        normalized = str(mode_label or "").strip().lower().replace(" ", "")
        normalized = normalized.replace("_", "").replace("-", "")
        if not normalized or normalized == "idle":
            return None
        if normalized in {"16channelslfp", "lfp", "mode0", "mode0lfp+mand+raw", "mode0lfp+mand"}:
            return 0
        if normalized in {"singlechannelspike", "singlechannel", "mode1"}:
            return 1
        if normalized in {"16channelsspike", "16channel", "16chspike", "4channelsspike", "4channel", "mode2"}:
            return 2
        if normalized in {"esa&mua", "esamua", "rsa&mua", "rsamua", "mode3"}:
            return 3
        if "mode3" in normalized:
            return 3
        if "mua" in normalized and ("esa" in normalized or "rsa" in normalized):
            return 3
        return None

    @classmethod
    def _mode_label_is_mode3(cls, mode_label):
        return cls._mode_label_to_sample_index(mode_label) == 3

    def _mark_mode3_display_hint(self, active=True, ttl_s=10.0):
        if not active:
            self._mode3_display_hint_until = 0.0
            self._mode3_display_legacy_factor_sync_done = False
            return
        try:
            ttl_s = max(1.0, float(ttl_s or 10.0))
        except Exception:
            ttl_s = 10.0
        self._mode3_display_hint_until = time.monotonic() + ttl_s
        self._sync_legacy_mode3_display_factors()

    def _is_mode3_display_context(self):
        try:
            raw_mode = getattr(self, "current_sample_mode", -1)
            current_mode = -1 if raw_mode is None else int(raw_mode)
        except Exception:
            current_mode = -1
        state = self._display_tuning_state()
        if current_mode == 3:
            self._sync_legacy_mode3_display_factors()
            return True
        if self._mode_label_is_mode3(getattr(self, "current_recording_mode", "")):
            self._sync_legacy_mode3_display_factors()
            return True
        if self._mode_label_is_mode3(getattr(self, "_remote_active_mode_text", "")):
            self._sync_legacy_mode3_display_factors()
            return True
        if current_mode in (0, 1, 2) and bool(state.get("_sample_mode_hint_initialized", False)):
            return False
        try:
            if time.monotonic() <= float(getattr(self, "_mode3_display_hint_until", 0.0) or 0.0):
                self._sync_legacy_mode3_display_factors()
                return True
        except Exception:
            pass
        return False

    def _set_lfp_feature_update_indicators(self, pointer):
        """Keep the LFP and ESA/MAND refresh cursors aligned."""
        try:
            position = float(pointer)
        except Exception:
            return
        for attr_name in ("updating_indicater", "ESA_updating_indicater"):
            indicator = getattr(self, attr_name, None)
            if indicator is not None and hasattr(indicator, "setPos"):
                try:
                    indicator.setPos(position)
                except Exception:
                    pass

    def _smooth_mode0_mand_for_display(self, mand_data):
        mand_arr = np.asarray(mand_data, dtype=np.float32)
        if mand_arr.ndim != 2 or mand_arr.shape[0] == 0 or mand_arr.shape[1] == 0:
            return mand_arr

        smooth_ms = self._selected_mode0_mand_display_smooth_ms()
        self.mode0_mand_display_smooth_ms = smooth_ms
        mand_bin_samples = max(1, int(round(float(self.lfp_sample_rate) * 0.004)))
        window_bins = max(1, int(round(float(smooth_ms) / 4.0)))
        if window_bins <= 1:
            return mand_arr

        histories = getattr(self, "_mode0_mand_display_history", None)
        if not isinstance(histories, list) or len(histories) < mand_arr.shape[0]:
            self._reset_mode0_mand_display_smoothing()
            histories = self._mode0_mand_display_history

        smoothed_arr = mand_arr.copy()
        usable_samples = (mand_arr.shape[1] // mand_bin_samples) * mand_bin_samples
        if usable_samples <= 0:
            return smoothed_arr

        for channel_num in range(min(mand_arr.shape[0], len(histories))):
            channel_values = mand_arr[channel_num, :usable_samples]
            bin_values = channel_values.reshape(-1, mand_bin_samples)
            bin_means = np.empty(bin_values.shape[0], dtype=np.float32)
            for bin_index in range(bin_values.shape[0]):
                values = bin_values[bin_index]
                finite = np.isfinite(values)
                bin_means[bin_index] = float(np.mean(values[finite])) if np.any(finite) else np.nan

            history = np.asarray(histories[channel_num], dtype=np.float32)
            combined = np.concatenate((history, bin_means)) if history.size else bin_means
            history_len = int(history.size)
            smoothed_bins = np.empty(bin_means.shape[0], dtype=np.float32)
            for bin_index in range(bin_means.shape[0]):
                end = history_len + bin_index + 1
                start = max(0, end - window_bins)
                values = combined[start:end]
                finite = np.isfinite(values)
                smoothed_bins[bin_index] = float(np.mean(values[finite])) if np.any(finite) else np.nan

            keep = max(0, window_bins - 1)
            histories[channel_num] = combined[-keep:].astype(np.float32, copy=True) if keep > 0 else np.array([], dtype=np.float32)
            smoothed_arr[channel_num, :usable_samples] = np.repeat(smoothed_bins, mand_bin_samples)

        return smoothed_arr

    def _display_tuning_stream_keys(self):
        return ("lfp", "spike", "mode2", "mode3_lfp_esa", "mode3_raw")

    def _display_tuning_state(self):
        try:
            state = object.__getattribute__(self, "__dict__")
        except Exception:
            state = {}
        return state if isinstance(state, dict) else {}

    def _load_display_tuning_profile(self, profile=None):
        state = self._display_tuning_state()
        profile = profile if isinstance(profile, dict) else {}
        tuning = profile.get("detail_display_tuning", {})
        if not isinstance(tuning, dict):
            tuning = profile.get("display_downsample_profile", {})
        if not isinstance(tuning, dict):
            tuning = {}
        strategy = str(tuning.get("strategy", tuning.get("mode", "balanced")) or "balanced").strip().lower()
        if strategy not in {"balanced", "interval_only"}:
            strategy = "balanced"
        self.display_downsample_strategy = strategy
        factors = tuning.get("factors", tuning.get("display_downsample_factors", {}))
        if isinstance(factors, dict):
            factor_map = state.get("display_downsample_factors")
            if not isinstance(factor_map, dict):
                factor_map = {}
                self.display_downsample_factors = factor_map
                state["display_downsample_factors"] = factor_map
            max_factor = int(state.get("display_downsample_max_factor", 64) or 64)
            aliases = {
                "mode0_lfp": "lfp",
                "mode1_raw": "spike",
                "mode2_raw": "mode2",
                "mode3_lfp": "mode3_lfp_esa",
                "mode3_esa": "mode3_lfp_esa",
                "mode3_lfp_esa": "mode3_lfp_esa",
                "mode3_raw": "mode3_raw",
            }
            for key, value in factors.items():
                stream_key = aliases.get(str(key).strip().lower(), str(key).strip().lower())
                if stream_key not in self._display_tuning_stream_keys():
                    continue
                try:
                    factor_map[stream_key] = max(1, min(max_factor, int(value)))
                except Exception:
                    continue
        actions = tuning.get("last_action", {})
        if isinstance(actions, dict):
            action_map = state.get("display_downsample_last_action")
            if not isinstance(action_map, dict):
                action_map = {}
                self.display_downsample_last_action = action_map
                state["display_downsample_last_action"] = action_map
            for key in self._display_tuning_stream_keys():
                if key in actions:
                    action_map[key] = str(actions.get(key, "") or "")
        self._refresh_display_tuning_button_style()

    def _display_downsample_enabled(self):
        strategy = self._display_tuning_state().get("display_downsample_strategy", "balanced")
        return str(strategy or "balanced") != "interval_only"

    def _display_downsample_render_threshold_ms(self, stream_key):
        try:
            normalized_stream = self._display_downsample_stream_key(stream_key)
        except Exception:
            normalized_stream = str(stream_key or "")
        attr_name = (
            "display_downsample_mode2_render_threshold_ms"
            if normalized_stream == "mode2"
            else "display_downsample_render_threshold_ms"
        )
        fallback = 24.0 if normalized_stream == "mode2" else 83.0
        try:
            threshold = float(getattr(self, attr_name, fallback) or fallback)
        except Exception:
            threshold = fallback
        return max(1.0, threshold)

    def _display_tuning_profile_payload(self):
        state = self._display_tuning_state()
        factors = state.get("display_downsample_factors", {})
        if not isinstance(factors, dict):
            factors = {}
        actions = state.get("display_downsample_last_action", {})
        if not isinstance(actions, dict):
            actions = {}
        return {
            "strategy": str(state.get("display_downsample_strategy", "balanced") or "balanced"),
            "factors": {
                key: int(max(1, int(factors.get(key, 1) or 1)))
                for key in self._display_tuning_stream_keys()
            },
            "last_action": {
                key: str(actions.get(key, "") or "")
                for key in self._display_tuning_stream_keys()
            },
            "mode3_stable_windows": int(state.get("display_downsample_mode3_stable_windows", 0) or 0),
            "updated_epoch": time.time(),
        }

    @staticmethod
    def _display_tuning_profile_signature(payload):
        if not isinstance(payload, dict):
            payload = {}
        signature_payload = dict(payload)
        signature_payload.pop("updated_epoch", None)
        return json.dumps(signature_payload, sort_keys=True, separators=(",", ":"))

    def _push_display_tuning_profile(self, force=False):
        payload = self._display_tuning_profile_payload()
        signature = self._display_tuning_profile_signature(payload)
        now_mono = time.monotonic()
        if (
            not bool(force)
            and signature == str(getattr(self, "_last_display_tuning_profile_payload", "") or "")
        ):
            return
        if (
            not bool(force)
            and now_mono - float(getattr(self, "_last_display_tuning_profile_push_monotonic", 0.0) or 0.0) < 20.0
        ):
            return
        sender = getattr(self, "_send_remote", None)
        if callable(sender):
            try:
                sender({
                    "type": "detail_display_tuning_update",
                    "profile": payload,
                    "force": bool(force),
                })
                self._last_display_tuning_profile_payload = signature
                self._last_display_tuning_profile_push_monotonic = now_mono
            except Exception:
                pass

    def _push_display_interval_pressure(self, stream_key, reason, loss_percent=0.0, render_ms=0.0):
        sender = getattr(self, "_send_remote", None)
        if not callable(sender):
            return
        try:
            normalized_stream = self._display_downsample_stream_key(stream_key)
        except Exception:
            normalized_stream = str(stream_key or "")
        now_mono = time.monotonic()
        pressure_times = self._display_tuning_state().get("_display_interval_pressure_last_mono")
        if not isinstance(pressure_times, dict):
            pressure_times = {}
            self._display_interval_pressure_last_mono = pressure_times
        last_mono = float(pressure_times.get(normalized_stream, 0.0) or 0.0)
        if last_mono > 0.0 and (now_mono - last_mono) < 3.0:
            return
        pressure_times[normalized_stream] = now_mono
        try:
            sender({
                "type": "detail_display_interval_pressure",
                "stream_key": normalized_stream,
                "reason": str(reason or "detail_render_slow"),
                "loss_percent": float(loss_percent or 0.0),
                "render_ms": float(render_ms or 0.0),
                "display_downsample_factor": int(self._display_downsample_factor(stream_key)),
                "display_downsample_strategy": str(getattr(self, "display_downsample_strategy", "balanced") or "balanced"),
            })
        except Exception:
            pass

    def _set_display_downsample_strategy(self, strategy, notify=True):
        strategy = str(strategy or "balanced").strip().lower()
        if strategy not in {"balanced", "interval_only"}:
            strategy = "balanced"
        self.display_downsample_strategy = strategy
        self._refresh_display_tuning_button_style()
        if bool(notify):
            self._push_display_tuning_profile(force=True)

    def _refresh_display_tuning_button_style(self):
        button = self._display_tuning_state().get("display_tuning_toggle_button")
        if button is None:
            return
        balanced = self._display_downsample_enabled()
        try:
            button.blockSignals(True)
            button.setChecked(bool(balanced))
            button.setText("Display: Balanced" if balanced else "Display: FPS Only")
            button.setToolTip(
                "Balanced uses display downsampling before slowing chart refresh. "
                "FPS Only disables downsampling and relies on refresh interval control."
            )
            if balanced:
                button.setStyleSheet(
                    "QPushButton { background-color: #2563EB; color: white; border-radius: 4px; padding: 4px 8px; font-weight: bold; }"
                    "QPushButton:hover { background-color: #1D4ED8; }"
                )
            else:
                button.setStyleSheet(
                    "QPushButton { background-color: #64748B; color: white; border-radius: 4px; padding: 4px 8px; font-weight: bold; }"
                    "QPushButton:hover { background-color: #475569; }"
                )
        except Exception:
            pass
        finally:
            try:
                button.blockSignals(False)
            except Exception:
                pass

    def on_display_tuning_toggle_clicked(self, checked: bool):
        self._set_display_downsample_strategy("balanced" if checked else "interval_only", notify=True)
        try:
            self.statusBar().showMessage(
                "Display tuning: balanced downsampling + interval"
                if checked else
                "Display tuning: FPS interval only"
            )
        except Exception:
            pass

    def _sync_legacy_mode3_display_factors(self):
        if bool(getattr(self, "_mode3_display_legacy_factor_sync_done", False)):
            return
        factors = getattr(self, "display_downsample_factors", None)
        if not isinstance(factors, dict):
            return
        for legacy_key, mode3_key in (("lfp", "mode3_lfp_esa"), ("spike", "mode3_raw")):
            try:
                legacy_factor = int(factors.get(legacy_key, 1) or 1)
                mode3_factor = int(factors.get(mode3_key, 1) or 1)
            except Exception:
                continue
            if legacy_factor > mode3_factor:
                factors[mode3_key] = legacy_factor
        self._rebalance_mode3_display_downsample_factors()
        self._mode3_display_legacy_factor_sync_done = True

    def _display_downsample_stream_key(self, stream_key):
        stream_key = str(stream_key or "stream")
        if self._is_mode3_display_context():
            if stream_key == "lfp":
                return "mode3_lfp_esa"
            if stream_key == "spike":
                return "mode3_raw"
        return stream_key

    def _display_refresh_stream_key(self, stream_key):
        try:
            mode_stream_key = {0: "lfp", 1: "spike", 2: "mode2"}.get(int(stream_key), stream_key)
        except Exception:
            mode_stream_key = stream_key
        return self._display_downsample_stream_key(mode_stream_key)

    def _display_refresh_hz(self, stream_key):
        stream_key = self._display_refresh_stream_key(stream_key)
        hz_by_stream = getattr(self, "display_refresh_hz_by_stream", None)
        if not isinstance(hz_by_stream, dict):
            hz_by_stream = {}
            self.display_refresh_hz_by_stream = hz_by_stream
        try:
            base_hz = float(getattr(self, "display_refresh_base_hz", 12.0) or 12.0)
        except Exception:
            base_hz = 12.0
        try:
            min_hz = float(getattr(self, "display_refresh_min_hz", 4.0) or 4.0)
        except Exception:
            min_hz = 4.0
        try:
            hz = float(hz_by_stream.get(stream_key, base_hz) or base_hz)
        except Exception:
            hz = base_hz
        hz = max(min_hz, min(base_hz, hz))
        hz_by_stream[stream_key] = hz
        return hz

    def _plot_render_interval_s(self, stream_key):
        hz = self._display_refresh_hz(stream_key)
        if hz <= 0.0:
            return float(getattr(self, "plot_min_render_interval_s", 1.0 / 12.0) or 1.0 / 12.0)
        return 1.0 / hz

    def _display_actual_render_hz(self, stream_key):
        stream_key = self._display_refresh_stream_key(stream_key)
        actual_map = getattr(self, "display_render_actual_hz_by_stream", None)
        if not isinstance(actual_map, dict):
            actual_map = {}
            self.display_render_actual_hz_by_stream = actual_map
        try:
            hz = float(actual_map.get(stream_key, 0.0) or 0.0)
        except Exception:
            hz = 0.0
        return max(0.0, hz)

    def _record_display_render_actual_hz(self, stream_key, now_mono=None):
        stream_key = self._display_refresh_stream_key(stream_key)
        if now_mono is None:
            now_mono = time.monotonic()
        try:
            now_mono = float(now_mono)
        except Exception:
            now_mono = time.monotonic()
        last_setdata_by_stream = getattr(self, "_last_plot_setdata_monotonic_by_stream", None)
        if not isinstance(last_setdata_by_stream, dict):
            last_setdata_by_stream = {}
            self._last_plot_setdata_monotonic_by_stream = last_setdata_by_stream
        actual_map = getattr(self, "display_render_actual_hz_by_stream", None)
        if not isinstance(actual_map, dict):
            actual_map = {}
            self.display_render_actual_hz_by_stream = actual_map
        previous = float(last_setdata_by_stream.get(stream_key, 0.0) or 0.0)
        actual_hz = 0.0
        if previous > 0.0 and now_mono > previous:
            actual_hz = min(240.0, 1.0 / max(1e-6, now_mono - previous))
        last_setdata_by_stream[stream_key] = now_mono
        actual_map[stream_key] = actual_hz
        return actual_hz

    def _display_refresh_label(self, stream_key):
        target_hz = self._display_refresh_hz(stream_key)
        actual_hz = self._display_actual_render_hz(stream_key)
        if actual_hz > 0.0:
            return f"{actual_hz:.1f}/{target_hz:g}Hz"
        return f"--/{target_hz:g}Hz"

    def _display_downsample_factor(self, stream_key):
        stream_key = self._display_downsample_stream_key(stream_key)
        if not self._display_downsample_enabled():
            return 1
        factors = getattr(self, "display_downsample_factors", None)
        if not isinstance(factors, dict):
            factors = {}
            self.display_downsample_factors = factors
        try:
            max_factor = int(getattr(self, "display_downsample_max_factor", 64) or 64)
        except Exception:
            max_factor = 64
        try:
            factor = int(factors.get(stream_key, 1) or 1)
        except Exception:
            factor = 1
        factor = max(1, min(max_factor, factor))
        factors[stream_key] = factor
        return factor

    def _display_downsample_label(self, stream_key):
        if not self._display_downsample_enabled():
            return "DS off"
        return f"DS x{self._display_downsample_factor(stream_key)} {self._display_refresh_label(stream_key)}"

    def _rebalance_mode3_display_downsample_factors(self):
        if not self._display_downsample_enabled():
            return 1, 1
        factors = getattr(self, "display_downsample_factors", None)
        if not isinstance(factors, dict):
            factors = {}
            self.display_downsample_factors = factors
        try:
            max_factor = int(getattr(self, "display_downsample_max_factor", 64) or 64)
        except Exception:
            max_factor = 64
        try:
            max_ratio = max(1, int(getattr(self, "display_downsample_mode3_max_factor_ratio", 4) or 4))
        except Exception:
            max_ratio = 4
        keys = ("mode3_lfp_esa", "mode3_raw")
        for key in keys:
            try:
                factors[key] = max(1, min(max_factor, int(factors.get(key, 1) or 1)))
            except Exception:
                factors[key] = 1
        while True:
            high_key = max(keys, key=lambda key: factors[key])
            low_key = min(keys, key=lambda key: factors[key])
            if factors[high_key] <= factors[low_key] * max_ratio or factors[low_key] >= max_factor:
                break
            factors[low_key] = max(1, min(max_factor, factors[low_key] * 2))
        return factors["mode3_lfp_esa"], factors["mode3_raw"]

    def _mode3_display_downsample_label(self):
        if not self._display_downsample_enabled():
            return "DS off"
        lfp_factor, raw_factor = self._rebalance_mode3_display_downsample_factors()
        lfp_hz_label = self._display_refresh_label("mode3_lfp_esa")
        raw_hz_label = self._display_refresh_label("mode3_raw")
        return f"DS LFP/ESA x{lfp_factor} {lfp_hz_label} Raw x{raw_factor} {raw_hz_label}"

    def _downsample_xy_for_display(self, x_values, y_values, stream_key):
        normalized_stream = self._display_downsample_stream_key(stream_key)
        return downsample_xy_for_display(
            x_values,
            y_values,
            self._display_downsample_factor(normalized_stream),
            strategy=display_downsample_strategy_for_stream(normalized_stream),
        )

    @staticmethod
    def _write_ring_1d(target, pointer, values):
        try:
            target_arr = np.asarray(target)
            value_arr = np.asarray(values, dtype=target_arr.dtype).reshape(-1)
        except Exception:
            return int(pointer or 0), False

        capacity = int(target_arr.size)
        if capacity <= 0:
            return 0, False
        try:
            start = int(pointer or 0) % capacity
        except Exception:
            start = 0
        count = int(value_arr.size)
        if count <= 0:
            return start, False

        end = start + count
        wrapped = count >= capacity or end >= capacity
        if count >= capacity:
            tail = value_arr[-capacity:]
            next_pointer = end % capacity
            split = capacity - next_pointer
            if split > 0:
                target_arr[next_pointer:] = tail[:split]
            if next_pointer > 0:
                target_arr[:next_pointer] = tail[split:]
            return next_pointer, True

        if end > capacity:
            split = capacity - start
            target_arr[start:] = value_arr[:split]
            target_arr[:end - capacity] = value_arr[split:]
            return end - capacity, True

        target_arr[start:end] = value_arr
        return end % capacity, wrapped

    @staticmethod
    def _mode2_display_channel_values(values, target_len, offset=0.0):
        try:
            target_len = max(0, int(target_len or 0))
        except Exception:
            target_len = 0
        if target_len <= 0:
            return np.asarray([], dtype=np.float32)
        try:
            source = np.asarray(values, dtype=np.float32).reshape(-1)
        except Exception:
            source = np.asarray([], dtype=np.float32)
        if source.size > 0:
            try:
                missing_value = float(MODE2_V2_GUI_MISSING_SAMPLE)
            except Exception:
                missing_value = -1000.0
            if np.isfinite(missing_value):
                source = source.astype(np.float32, copy=True)
                source[np.isclose(source, np.float32(missing_value), rtol=0.0, atol=1e-6)] = np.nan

        if source.size >= target_len:
            output = source[:target_len].astype(np.float32, copy=True)
        else:
            output = np.full(target_len, np.nan, dtype=np.float32)
            if source.size > 0:
                output[:source.size] = source

        try:
            offset = float(offset or 0.0)
        except Exception:
            offset = 0.0
        if offset != 0.0:
            output += np.float32(offset)
        return output

    @staticmethod
    def _advance_ring_pointer(pointer, count, capacity):
        try:
            capacity = int(capacity or 0)
        except Exception:
            capacity = 0
        if capacity <= 0:
            return 0, False
        try:
            start = int(pointer or 0) % capacity
        except Exception:
            start = 0
        try:
            count = max(0, int(count or 0))
        except Exception:
            count = 0
        if count <= 0:
            return start, False
        end = start + count
        return end % capacity, (count >= capacity or end >= capacity)

    def _ring_xy_for_display(self, x_values, y_values, stream_key, pointer=0, has_wrapped=False):
        try:
            y_arr = np.asarray(y_values, dtype=np.float64).reshape(-1)
            x_arr = np.asarray(x_values).reshape(-1)
        except Exception:
            return x_values, y_values

        length = min(int(x_arr.size), int(y_arr.size))
        if length <= 0:
            return x_arr[:0], y_arr[:0]

        y_arr = y_arr[:length]
        x_arr = x_arr[:length]
        try:
            pointer = int(pointer)
        except Exception:
            pointer = 0 if has_wrapped else length
        pointer = pointer % length if has_wrapped else max(0, min(length, pointer))
        if not has_wrapped and pointer == 0 and np.isfinite(y_arr).any():
            has_wrapped = True

        if has_wrapped:
            try:
                pointer = int(pointer) % length
            except Exception:
                pointer = 0
            y_arr = y_arr.copy()
            if 0 <= pointer < length:
                y_arr[pointer] = np.nan
        else:
            y_arr = y_arr[:pointer]
            x_arr = x_arr[:pointer]

        return self._downsample_xy_for_display(x_arr, y_arr, stream_key)

    def _single_raw_xy_for_display(self, y_values, stream_key, has_wrapped=False):
        try:
            y_arr = np.asarray(y_values, dtype=np.float64).reshape(-1)
            x_arr = np.asarray(self.spike_x).reshape(-1)
        except Exception:
            return self.spike_x, y_values

        length = min(int(x_arr.size), int(y_arr.size))
        if length <= 0:
            return x_arr[:0], y_arr[:0]

        y_arr = y_arr[:length]
        x_arr = x_arr[:length]
        return self._ring_xy_for_display(
            x_arr,
            y_arr,
            stream_key,
            pointer=getattr(self, "ring_spike_pointer", 0),
            has_wrapped=has_wrapped,
        )

    @staticmethod
    def _packet_loss_percent(missing, received):
        try:
            missing = float(missing or 0.0)
            received = float(received or 0.0)
        except Exception:
            return 0.0
        denominator = missing + received
        if denominator <= 0.0:
            return 0.0
        return max(0.0, (missing / denominator) * 100.0)

    def _apply_mode2_packet_loss_snapshot(self, snapshot):
        if not isinstance(snapshot, dict):
            return False
        try:
            missing = max(0, int(snapshot.get("missing", snapshot.get("packet_loss", 0)) or 0))
            received = max(0, int(snapshot.get("received", snapshot.get("packet_count", 0)) or 0))
        except Exception:
            return False
        self._mode2_packet_loss_snapshot = dict(snapshot)
        self.spike_moide2_misspackets = missing
        self.spike_mode2_accumulpackets = received
        return True

    def _handle_serial_status_update(self, payload):
        if not isinstance(payload, dict):
            return
        try:
            stream_mode = int(payload.get("stream_mode", -1))
        except Exception:
            stream_mode = -1
        if stream_mode != 2:
            return
        self._apply_mode2_packet_loss_snapshot({
            "missing": payload.get("packet_loss", 0),
            "received": payload.get("packet_count", 0),
            "percent": payload.get("packet_loss_percent_current", 0.0),
            "expected": payload.get("packet_loss_expected_current", 0),
            "batch_missing": payload.get("packet_loss_batch", 0),
            "batch_received": payload.get("packet_count_batch", 0),
            "sequence": payload.get("packet_metrics_seq", 0),
        })

    def _adapt_display_refresh_interval(self, stream_key, render_ms, render_threshold_ms, downsample_factor):
        stream_key = self._display_refresh_stream_key(stream_key)
        hz_by_stream = getattr(self, "display_refresh_hz_by_stream", None)
        if not isinstance(hz_by_stream, dict):
            hz_by_stream = {}
            self.display_refresh_hz_by_stream = hz_by_stream
        stable_map = getattr(self, "display_refresh_stable_windows", None)
        if not isinstance(stable_map, dict):
            stable_map = {}
            self.display_refresh_stable_windows = stable_map
        action_map = getattr(self, "display_refresh_last_action", None)
        if not isinstance(action_map, dict):
            action_map = {}
            self.display_refresh_last_action = action_map

        steps = tuple(float(v) for v in getattr(self, "display_refresh_hz_steps", (12.0, 8.0, 6.0, 4.0)) or (12.0, 8.0, 6.0, 4.0))
        steps = tuple(sorted({v for v in steps if v > 0.0}, reverse=True)) or (12.0, 8.0, 6.0, 4.0)
        hz = self._display_refresh_hz(stream_key)
        current_index = min(range(len(steps)), key=lambda idx: abs(steps[idx] - hz))
        hz = steps[current_index]
        hz_by_stream[stream_key] = hz
        try:
            render_ms = float(render_ms or 0.0)
        except Exception:
            render_ms = 0.0
        try:
            render_threshold_ms = float(render_threshold_ms or 0.0)
        except Exception:
            render_threshold_ms = 0.0
        try:
            downsample_factor = int(downsample_factor or 1)
        except Exception:
            downsample_factor = 1
        try:
            max_factor = int(getattr(self, "display_downsample_max_factor", 64) or 64)
        except Exception:
            max_factor = 64

        action = "hold_refresh"
        should_slow = render_threshold_ms > 0.0 and render_ms > render_threshold_ms and (
            downsample_factor >= max_factor or not self._display_downsample_enabled()
        )
        if should_slow and current_index < len(steps) - 1:
            current_index += 1
            hz_by_stream[stream_key] = steps[current_index]
            stable_map[stream_key] = 0
            action = "decrease_refresh"
        elif (
            render_threshold_ms > 0.0
            and render_ms <= render_threshold_ms * 0.35
            and current_index > 0
        ):
            stable_windows = int(stable_map.get(stream_key, 0) or 0) + 1
            required = int(getattr(self, "display_downsample_stable_windows_required", 30) or 30)
            if stable_windows >= required:
                current_index -= 1
                hz_by_stream[stream_key] = steps[current_index]
                stable_map[stream_key] = 0
                action = "increase_refresh_stable"
            else:
                stable_map[stream_key] = stable_windows
                action = "hold_refresh_stable"
        else:
            stable_map[stream_key] = 0

        action_map[stream_key] = action
        return action

    def _adapt_display_downsample(self, stream_key, loss_percent, setdata_ms):
        state = self._display_tuning_state()
        render_threshold = self._display_downsample_render_threshold_ms(stream_key)
        if not self._display_downsample_enabled():
            action_map = getattr(self, "display_downsample_last_action", None)
            if isinstance(action_map, dict):
                action_map[self._display_downsample_stream_key(stream_key)] = "disabled_interval_only"
            try:
                if float(setdata_ms or 0.0) > render_threshold:
                    self._push_display_interval_pressure(
                        stream_key,
                        "display_interval_only_pressure",
                        loss_percent=loss_percent,
                        render_ms=setdata_ms,
                    )
            except Exception:
                pass
            refresh_action = self._adapt_display_refresh_interval(stream_key, setdata_ms, render_threshold, 1)
            return 1, f"disabled_interval_only/{refresh_action}"
        if self._is_mode3_display_context() and str(stream_key or "") in {"lfp", "spike"}:
            return self._adapt_mode3_display_downsample(stream_key, loss_percent, setdata_ms)

        stream_key = self._display_downsample_stream_key(stream_key)
        current_factor = self._display_downsample_factor(stream_key)
        stable_map = getattr(self, "display_downsample_stable_windows", None)
        if not isinstance(stable_map, dict):
            stable_map = {}
            self.display_downsample_stable_windows = stable_map
        try:
            stable_windows = int(stable_map.get(stream_key, 0) or 0)
        except Exception:
            stable_windows = 0
        next_factor, next_stable, action = adjust_downsample_factor(
            current_factor,
            stable_windows,
            loss_percent,
            setdata_ms,
            render_threshold_ms=render_threshold,
            max_factor=int(state.get("display_downsample_max_factor", 64) or 64),
            stable_windows_required=int(state.get("display_downsample_stable_windows_required", 30) or 30),
        )
        self.display_downsample_factors[stream_key] = int(next_factor)
        stable_map[stream_key] = int(next_stable)
        action_map = getattr(self, "display_downsample_last_action", None)
        if not isinstance(action_map, dict):
            action_map = {}
            self.display_downsample_last_action = action_map
        action_map[stream_key] = str(action)
        refresh_action = self._adapt_display_refresh_interval(
            stream_key,
            setdata_ms,
            render_threshold,
            next_factor,
        )
        if int(next_factor) != int(current_factor):
            self._push_display_tuning_profile(force=False)
        else:
            try:
                max_factor = int(getattr(self, "display_downsample_max_factor", 64) or 64)
                if (
                    int(next_factor) >= max_factor
                    and float(setdata_ms or 0.0) > render_threshold
                ):
                    self._push_display_interval_pressure(
                        stream_key,
                        "display_downsample_max_pressure",
                        loss_percent=loss_percent,
                        render_ms=setdata_ms,
                    )
            except Exception:
                pass
        if refresh_action != "hold_refresh":
            action = f"{action}/{refresh_action}"
            action_map[stream_key] = str(action)
        return int(next_factor), str(action)

    def _adapt_mode3_display_downsample(self, stream_key, loss_percent, setdata_ms):
        stream_key = self._display_downsample_stream_key(stream_key)
        now_mono = time.monotonic()
        metrics = getattr(self, "display_downsample_mode3_metrics", None)
        if not isinstance(metrics, dict):
            metrics = {}
            self.display_downsample_mode3_metrics = metrics
        try:
            update_counter = int(getattr(self, "display_downsample_mode3_update_counter", 0) or 0) + 1
        except Exception:
            update_counter = 1
        self.display_downsample_mode3_update_counter = update_counter
        try:
            metrics[stream_key] = {
                "loss_percent": max(0.0, float(loss_percent or 0.0)),
                "render_ms": max(0.0, float(setdata_ms or 0.0)),
                "updated_mono": now_mono,
                "version": update_counter,
            }
        except Exception:
            metrics[stream_key] = {
                "loss_percent": 0.0,
                "render_ms": 0.0,
                "updated_mono": now_mono,
                "version": update_counter,
            }

        required_keys = ("mode3_lfp_esa", "mode3_raw")
        max_age_s = float(getattr(self, "display_downsample_mode3_metric_max_age_s", 5.0) or 5.0)
        if not all(key in metrics for key in required_keys):
            action_map = getattr(self, "display_downsample_last_action", None)
            if isinstance(action_map, dict):
                action_map[stream_key] = "hold_wait_coupled_metric"
            return self._display_downsample_factor(stream_key), "hold_wait_coupled_metric"
        if any((now_mono - float(metrics[key].get("updated_mono", 0.0) or 0.0)) > max_age_s for key in required_keys):
            action_map = getattr(self, "display_downsample_last_action", None)
            if isinstance(action_map, dict):
                action_map[stream_key] = "hold_stale_coupled_metric"
            return self._display_downsample_factor(stream_key), "hold_stale_coupled_metric"

        self._rebalance_mode3_display_downsample_factors()
        last_versions = getattr(self, "display_downsample_mode3_last_adjust_versions", None)
        if not isinstance(last_versions, dict):
            last_versions = {}
            self.display_downsample_mode3_last_adjust_versions = last_versions
        if not all(int(metrics[key].get("version", 0) or 0) > int(last_versions.get(key, 0) or 0) for key in required_keys):
            action_map = getattr(self, "display_downsample_last_action", None)
            if isinstance(action_map, dict):
                action_map[stream_key] = "hold_wait_coupled_pair"
            return self._display_downsample_factor(stream_key), "hold_wait_coupled_pair"

        current_factors = {
            key: self._display_downsample_factor(key)
            for key in required_keys
        }
        stream_metrics = {
            key: {
                "loss_percent": metrics[key].get("loss_percent", 0.0),
                "render_ms": metrics[key].get("render_ms", 0.0),
            }
            for key in required_keys
        }
        next_factors, next_stable, actions = adjust_coupled_downsample_factors(
            current_factors,
            int(getattr(self, "display_downsample_mode3_stable_windows", 0) or 0),
            stream_metrics,
            render_threshold_ms=float(getattr(self, "display_downsample_render_threshold_ms", 83.0) or 83.0),
            max_factor=int(getattr(self, "display_downsample_max_factor", 64) or 64),
            max_factor_ratio=int(getattr(self, "display_downsample_mode3_max_factor_ratio", 4) or 4),
            stable_windows_required=int(getattr(self, "display_downsample_stable_windows_required", 30) or 30),
        )
        self.display_downsample_mode3_stable_windows = int(next_stable)
        factors = getattr(self, "display_downsample_factors", None)
        if not isinstance(factors, dict):
            factors = {}
            self.display_downsample_factors = factors
        action_map = getattr(self, "display_downsample_last_action", None)
        if not isinstance(action_map, dict):
            action_map = {}
            self.display_downsample_last_action = action_map
        for key in required_keys:
            factors[key] = int(next_factors.get(key, current_factors[key]))
            refresh_action = self._adapt_display_refresh_interval(
                key,
                stream_metrics.get(key, {}).get("render_ms", 0.0),
                float(getattr(self, "display_downsample_render_threshold_ms", 83.0) or 83.0),
                factors[key],
            )
            action = str(actions.get(key, "hold"))
            if refresh_action != "hold_refresh":
                action = f"{action}/{refresh_action}"
            action_map[key] = action
            actions[key] = action
            last_versions[key] = int(metrics[key].get("version", 0) or 0)
        if any(int(next_factors.get(key, current_factors[key])) != int(current_factors[key]) for key in required_keys):
            self._push_display_tuning_profile(force=False)
        else:
            try:
                max_factor = int(getattr(self, "display_downsample_max_factor", 64) or 64)
                render_threshold = float(getattr(self, "display_downsample_render_threshold_ms", 83.0) or 83.0)
                for key in required_keys:
                    metric = stream_metrics.get(key, {})
                    if (
                        int(factors.get(key, 1) or 1) >= max_factor
                        and float(metric.get("render_ms", 0.0) or 0.0) > render_threshold
                    ):
                        self._push_display_interval_pressure(
                            key,
                            "display_downsample_max_pressure",
                            loss_percent=metric.get("loss_percent", 0.0),
                            render_ms=metric.get("render_ms", 0.0),
                        )
            except Exception:
                pass
        return self._display_downsample_factor(stream_key), str(actions.get(stream_key, "hold"))
        
    # data update function
    @safe_guard
    def update_plot_data(self, data):  # 注意：实际的一次更新得到的包的数量是在浮动的根据线程处理的速度
        try:
            mode_key = int(data[0][0])
        except Exception:
            mode_key = -1
        try:
            header = data[0] if isinstance(data, (list, tuple)) and data else []
            if (
                mode_key == 0
                and isinstance(header, (list, tuple))
                and len(header) > 1
                and (
                    int(getattr(self, "current_sample_mode", -1)) == 3
                    or self._mode_label_is_mode3(getattr(self, "current_recording_mode", ""))
                    or self._mode_label_is_mode3(getattr(self, "_remote_active_mode_text", ""))
                )
            ):
                self._mark_mode3_display_hint(True)
        except Exception:
            pass
        mode3_display_context = self._is_mode3_display_context()
        render_metrics = {
            "stream": {0: "lfp", 1: "spike", 2: "mode2"}.get(mode_key, "unknown"),
            "mode": int(mode_key),
            "rendered": False,
            "buffer_update_ms": 0.0,
            "lfp_buffer_update_ms": 0.0,
            "raw_buffer_update_ms": 0.0,
            "mode2_buffer_update_ms": 0.0,
            "lfp_setdata_ms": 0.0,
            "esa_setdata_ms": 0.0,
            "raw_setdata_ms": 0.0,
            "raster_setdata_ms": 0.0,
            "raster_display_downsample_factor": 1,
            "raster_render_stride": 1,
            "mode2_setdata_ms": 0.0,
            "sensor_setdata_ms": 0.0,
            "display_downsample_factor": 1,
            "display_downsample_action": "",
            "render_interval_hz": 12.0,
            "render_target_hz": 12.0,
            "render_actual_hz": 0.0,
            "render_interval_action": "",
        }
        self.last_detail_render_metrics = render_metrics
        if self.curr_active_ports is not None:
            buffer_started = time.monotonic()
            self.raw_data_generator(self.curr_active_ports, data, update_buffers=self.plot_update_enabled)
            buffer_update_ms = max(0.0, (time.monotonic() - buffer_started) * 1000.0)
            render_metrics["buffer_update_ms"] = buffer_update_ms
            if mode_key == 0:
                render_metrics["lfp_buffer_update_ms"] = buffer_update_ms
            elif mode_key == 1:
                render_metrics["raw_buffer_update_ms"] = buffer_update_ms
            elif mode_key == 2:
                render_metrics["mode2_buffer_update_ms"] = buffer_update_ms

            render_now = bool(self.plot_update_enabled)
            if render_now:
                now_mono = time.monotonic()
                stream_key = self._display_refresh_stream_key(mode_key)
                render_interval_s = self._plot_render_interval_s(stream_key)
                target_hz = self._display_refresh_hz(stream_key)
                render_metrics["render_interval_hz"] = target_hz
                render_metrics["render_target_hz"] = target_hz
                render_metrics["render_actual_hz"] = self._display_actual_render_hz(stream_key)
                refresh_actions = getattr(self, "display_refresh_last_action", None)
                if isinstance(refresh_actions, dict):
                    render_metrics["render_interval_action"] = str(refresh_actions.get(stream_key, "") or "")
                last_plot_by_stream = getattr(self, "_last_plot_render_monotonic_by_stream", None)
                if not isinstance(last_plot_by_stream, dict):
                    last_plot_by_stream = {}
                    self._last_plot_render_monotonic_by_stream = last_plot_by_stream
                last_stream_render = float(last_plot_by_stream.get(stream_key, 0.0) or 0.0)
                if (now_mono - last_stream_render) < render_interval_s:
                    render_now = False
                else:
                    last_plot_by_stream[stream_key] = now_mono
                    self._last_plot_render_monotonic = now_mono
            
            # Optimization: If plot updates are disabled, skip ALL plot updates
            if not self.plot_update_enabled:
                # Still update battery/status indicators as they are low frequency
                try:
                    # Update battery if needed (it's updated in raw_data_generator but also timeline here?)
                    # No, timeline update is separate.
                     # battery data
                    if time.time() - self.last_battery_update_time > 10:
                        self.update_battery_indicator(self.RSOC, self.Battery_STAT, self.Battery_voltage)
                        self.last_battery_update_time = time.time()
                    pass
                except:
                    pass
                self.last_detail_render_metrics = render_metrics
                return

            if(int(data[0][0]) == 0):
                """ LFP raw data update """
                mode0_raw_payload_available = False
                try:
                    mode0_raw_payload_available = len(data) > 7 and len(data[7]) > 0 and not mode3_display_context
                    mode0_raw_channel = int(data[8]) if len(data) > 8 else int(getattr(self, "spike_raw_channel", 0) or 0)
                except Exception:
                    mode0_raw_channel = int(getattr(self, "spike_raw_channel", 0) or 0)
                # # update infinited line
                self._set_lfp_feature_update_indicators(self.ring_lfp_pointer) # span (0, 1)
                lfp_loss_percent = self._packet_loss_percent(self.lfpmisspackets, self.lfpaccumulpackets)
                lfp_ds_label = self._mode3_display_downsample_label() if mode3_display_context else self._display_downsample_label("lfp")
                render_metrics["display_downsample_factor"] = self._display_downsample_factor("lfp")
                if mode3_display_context:
                    lfp_title_prefix = "Mode3 LFP"
                    self.ESA_pI_channel.setTitle('ESA 16 channels')
                    self.ESA_pI_channel.getAxis("left").setLabel('ESA', color='#FFFF00')
                    feature_tick_prefix = "ESA"
                else:
                    lfp_title_prefix = "Mode0 LFP"
                    self.ESA_pI_channel.setTitle('MAND 16 channels')
                    self.ESA_pI_channel.getAxis("left").setLabel('MAND uV', color='#FFFF00')
                    feature_tick_prefix = "MAND"
                self.lfp_feature_tick_prefix = feature_tick_prefix
                self.LFP_pI_channel.setTitle('{} 16 channels <span style="color: yellow; font-size: 10pt">loss:{}/{} {}</span>'.format(lfp_title_prefix, self.lfpmisspackets, self.lfpaccumulpackets, lfp_ds_label))
                feature_ticks = {
                    int(channel * self.separate_interval): f'{feature_tick_prefix}{channel}'
                    for channel in range(16)
                }
                self.ESA_pI_channel.getAxis("left").setTicks([feature_ticks.items()])
                ## update rssi
                if self.mSerial is not None:
                    self.update_rssi(self.mSerial.rssi)
                ## update loss rate
                self.update_packet_loss(round(self.lfpmisspackets /(self.lfpaccumulpackets + self.lfpmisspackets + 1), 2) * 100) # +1 防止divide zero
                if render_now:
                    render_metrics["rendered"] = True
                    lfp_setdata_started = time.monotonic()
                    for i in range(16): # diff color diff channels separate_interval
                        x_view, y_view = self._ring_xy_for_display(
                            self.LFP_x,
                            self.LFP_raw_data[i],
                            "lfp",
                            pointer=self.ring_lfp_pointer,
                            has_wrapped=bool(getattr(self, "_lfp_has_wrapped", False)),
                        )
                        self.LFP_raw_channel[i].setData(x_view, y_view ,pen=self.pens_solid[i])   
                    render_metrics["lfp_setdata_ms"] = max(0.0, (time.monotonic() - lfp_setdata_started) * 1000.0)

                    esa_setdata_started = time.monotonic()
                    for i in range(16): # ESA channels with different colors
                        x_view, y_view = self._ring_xy_for_display(
                            self.ESA_x,
                            self.ESA_raw_data[i],
                            "lfp",
                            pointer=self.ring_lfp_pointer,
                            has_wrapped=bool(getattr(self, "_lfp_has_wrapped", False)),
                        )
                        self.ESA_raw_channel[i].setData(x_view, y_view ,pen=self.pens_dashed[i])  # 使用PyQt6枚举修正虚线样式
                    render_metrics["esa_setdata_ms"] = max(0.0, (time.monotonic() - esa_setdata_started) * 1000.0)

                    if mode0_raw_payload_available:
                        raw_setdata_started = time.monotonic()
                        raw_has_wrapped = bool(getattr(self, "_mode0_raw_has_wrapped", False))
                        self.spike_updating_indicater_mode_1.setPos(self.ring_spike_pointer)
                        self.spike_pI_channel.setTitle(
                            'Mode0 raw RHD Ch{} <span style="color: yellow; font-size: 10pt">12.5kHz {}</span>'.format(
                                mode0_raw_channel,
                                lfp_ds_label,
                            )
                        )
                        if self.spike_filter_enabled:
                            x_view, y_view = self._single_raw_xy_for_display(self.spike_filtered_data[0], "lfp", raw_has_wrapped)
                            self.raw_dataline_channel.setData(x_view, y_view, pen=self.pen_cyan)
                        else:
                            x_view, y_view = self._single_raw_xy_for_display(self.spike_raw_data[0], "lfp", raw_has_wrapped)
                            self.raw_dataline_channel.setData(x_view, y_view, pen=self.pen_white)
                        x_view, y_view = self._single_raw_xy_for_display(self.alignment_buffer, "lfp", raw_has_wrapped)
                        self.alignment_curve.setData(x_view, y_view)
                        render_metrics["raw_setdata_ms"] = max(0.0, (time.monotonic() - raw_setdata_started) * 1000.0)
                    next_factor, action = self._adapt_display_downsample(
                        "lfp",
                        lfp_loss_percent,
                        render_metrics["lfp_setdata_ms"] + render_metrics["esa_setdata_ms"] + render_metrics["raw_setdata_ms"],
                    )
                    render_metrics["display_downsample_factor"] = next_factor
                    render_metrics["display_downsample_action"] = action

                    self.spike_raster_updating_indicater_mode_1.setPos(self.ring_spike_raster_pointer)
                    if mode3_display_context:
                        raster_ms = self._maybe_refresh_raster_plot(render_metrics)
                        if raster_ms > 0.0:
                            spike_loss_percent = self._packet_loss_percent(
                                self.spikemisspackets_mode_1,
                                self.spikeaccumulpackets_mode_1,
                            )
                            raw_next_factor, raw_action = self._adapt_display_downsample(
                                "spike",
                                spike_loss_percent,
                                raster_ms,
                            )
                            render_metrics["display_downsample_factor"] = raw_next_factor
                            render_metrics["display_downsample_action"] = f"{action}/{raw_action}"
                    actual_hz = self._record_display_render_actual_hz("lfp")
                    render_metrics["render_actual_hz"] = actual_hz
                    lfp_ds_label = self._mode3_display_downsample_label() if mode3_display_context else self._display_downsample_label("lfp")
                    self.LFP_pI_channel.setTitle('{} 16 channels <span style="color: yellow; font-size: 10pt">loss:{}/{} {}</span>'.format(lfp_title_prefix, self.lfpmisspackets, self.lfpaccumulpackets, lfp_ds_label))
                    if mode0_raw_payload_available:
                        self.spike_pI_channel.setTitle(
                            'Mode0 raw RHD Ch{} <span style="color: yellow; font-size: 10pt">12.5kHz {}</span>'.format(
                                mode0_raw_channel,
                                lfp_ds_label,
                            )
                        )
                    
            
            elif(int(data[0][0]) == 1):
                """ spike data mode 1 """ 
                #### raw data update
                # update indicte line
                self.spike_updating_indicater_mode_1.setPos(self.ring_spike_pointer)
                spike_loss_percent = self._packet_loss_percent(self.spikemisspackets_mode_1, self.spikeaccumulpackets_mode_1)
                spike_ds_label = self._mode3_display_downsample_label() if mode3_display_context else self._display_downsample_label("spike")
                render_metrics["display_downsample_factor"] = self._display_downsample_factor("spike")
                self.spike_pI_channel.setTitle('RHD Ch{} <span style="color: yellow; font-size: 10pt">loss: {}/{} time: {} s {}</span>'.format(self.spike_raw_channel, self.spikemisspackets_mode_1, self.spikeaccumulpackets_mode_1, self.spike_timestamp_note // 1000, spike_ds_label))
                ## update rssi
                if self.mSerial is not None:
                    self.update_rssi(self.mSerial.rssi)
                ## update loss rate
                self.update_packet_loss(round(self.spikemisspackets_mode_1 /(self.spikemisspackets_mode_1 + self.spikeaccumulpackets_mode_1 + 1), 2) * 100)
                # update channels index
                # self.spike_pI_channel.setTitle('channel {}'.format(self.spike_raw_channel))
                if render_now:
                    render_metrics["rendered"] = True
                    raw_setdata_started = time.monotonic()
                    raw_has_wrapped = bool(getattr(self, "_spike_raw_has_wrapped", False))
                    if self.spike_filter_enabled:
                        try:
                            x_view, y_view = self._single_raw_xy_for_display(self.spike_filtered_data[0], "spike", raw_has_wrapped)
                            self.raw_dataline_channel.setData(x_view, y_view ,pen=self.pen_cyan)
                        except Exception:
                            pass
                    else:
                        x_view, y_view = self._single_raw_xy_for_display(self.spike_raw_data[0], "spike", raw_has_wrapped)
                        self.raw_dataline_channel.setData(x_view, y_view ,pen=self.pen_white)
                    
                    # Update alignment curve
                    x_view, y_view = self._single_raw_xy_for_display(self.alignment_buffer, "spike", raw_has_wrapped)
                    self.alignment_curve.setData(x_view, y_view)
                    render_metrics["raw_setdata_ms"] = max(0.0, (time.monotonic() - raw_setdata_started) * 1000.0)
 
                #### raster data update
                # update infinited line
                self.spike_raster_updating_indicater_mode_1.setPos(self.ring_spike_raster_pointer)
                if render_now:
                    raster_ms = self._maybe_refresh_raster_plot(render_metrics)
                    next_factor, action = self._adapt_display_downsample(
                        "spike",
                        spike_loss_percent,
                        render_metrics["raw_setdata_ms"] + raster_ms,
                    )
                    render_metrics["display_downsample_factor"] = next_factor
                    render_metrics["display_downsample_action"] = action
                    actual_hz = self._record_display_render_actual_hz("spike")
                    render_metrics["render_actual_hz"] = actual_hz
                    spike_ds_label = self._mode3_display_downsample_label() if mode3_display_context else self._display_downsample_label("spike")
                    self.spike_pI_channel.setTitle('RHD Ch{} <span style="color: yellow; font-size: 10pt">loss: {}/{} time: {} s {}</span>'.format(self.spike_raw_channel, self.spikemisspackets_mode_1, self.spikeaccumulpackets_mode_1, self.spike_timestamp_note // 1000, spike_ds_label))

                """ auto update spike threshold """
                # 循环的自动采样不同channel的raw data 来 时刻更新 spike threhsold
                if(self.Auto_ST_Update):
                    self.Auto_ST_Update_flag += self.single_update_data_size
                    if(self.Auto_ST_Update_flag >= self.spike_display_data_num): # pending enough raw data
                        # reset pending data flag
                        self.Auto_ST_Update_flag = 0
                        # set the next channel
                        self.spike1ch_tab.channel_combo.setCurrentIndex(self.channel_panding) 
                        # update the threshold
                        self.spike_channel_switch_mode1(update_threshold=True)
                        self.channel_panding += 1
                        if(self.channel_panding >= 16):
                            self.Auto_ST_Update = False
                            self.channel_panding = 0
                            self.Auto_ST_Update_flag = 0
                            self.statusBar().showMessage("Note:Auto Spike Threshold completed!")
            
            elif(int(data[0][0]) == 2):
                """ spike data mode 2 """ 
                # # update infinited line
                self.AP_updating_indicater.setPos(self.ring_spike_mode2_pointer) # span (0, 1)
                mode2_loss_percent = self._packet_loss_percent(self.spike_moide2_misspackets, self.spike_mode2_accumulpackets)
                mode2_ds_label = self._display_downsample_label("mode2")
                render_metrics["display_downsample_factor"] = self._display_downsample_factor("mode2")
                self.AP_pI_channel.setTitle('Mode2 v2 AP data RHD Ch0-Ch15 <span style="color: yellow; font-size: 10pt">loss_packets:{}/{} time: {} s {}</span>'.format(self.spike_moide2_misspackets, self.spike_mode2_accumulpackets, self.spike_timestamp_note // 1000, mode2_ds_label))
                ## update rssi
                if self.mSerial is not None:
                    self.update_rssi(self.mSerial.rssi)
                ## update loss rate
                self.update_packet_loss(round(self.spike_moide2_misspackets /(self.spike_moide2_misspackets + self.spike_mode2_accumulpackets + 1), 2) * 100)

                if render_now:
                    render_metrics["rendered"] = True
                    mode2_setdata_started = time.monotonic()
                    for i in range(16): # diff color diff channels separate_interval
                        if(self.spike_mode2_curr_channel[i] != 0):
                            self.reinit_rawdata_mode2_temp += 1
                        x_view, y_view = self._ring_xy_for_display(
                            self.spike_mode2_x,
                            self.spike_mode2_raw_data[i],
                            "mode2",
                            pointer=self.ring_spike_mode2_pointer,
                            has_wrapped=bool(getattr(self, "_mode2_has_wrapped", False)),
                        )
                        self.AP_raw_channel[i].setData(x_view, y_view ,pen=self.pens_solid[i])
                    x_view, y_view = self._ring_xy_for_display(
                        self.spike_mode2_x,
                        self.mode2_alignment_data,
                        "mode2",
                        pointer=self.ring_spike_mode2_pointer,
                        has_wrapped=bool(getattr(self, "_mode2_has_wrapped", False)),
                    )
                    self.AP_alignment_curve.setData(x_view, y_view)
                    render_metrics["mode2_setdata_ms"] = max(0.0, (time.monotonic() - mode2_setdata_started) * 1000.0)
                    next_factor, action = self._adapt_display_downsample(
                        "mode2",
                        mode2_loss_percent,
                        render_metrics["mode2_buffer_update_ms"] + render_metrics["mode2_setdata_ms"],
                    )
                    render_metrics["display_downsample_factor"] = next_factor
                    render_metrics["display_downsample_action"] = action
                    actual_hz = self._record_display_render_actual_hz("mode2")
                    render_metrics["render_actual_hz"] = actual_hz
                    mode2_ds_label = self._display_downsample_label("mode2")
                    self.AP_pI_channel.setTitle('Mode2 v2 AP data RHD Ch0-Ch15 <span style="color: yellow; font-size: 10pt">loss_packets:{}/{} time: {} s {}</span>'.format(self.spike_moide2_misspackets, self.spike_mode2_accumulpackets, self.spike_timestamp_note // 1000, mode2_ds_label))
  

            """ LSR data update """ 
            self.accle_updating_indicater.setPos(self.ring_LSR_pointer) # span (0, 1)
            self.gyro_updating_indicater.setPos(self.ring_LSR_pointer)
            if(self.ring_LSR_pointer >= self.LSR_display_data_num):
                self.ring_LSR_pointer = 0
            # IMU data
            if render_now:
                sensor_setdata_started = time.monotonic()
                for imu_channel in range(6):
                    self.IMU_accl_channel[imu_channel].setData(self.LSR_timestamp ,self.IMUdata[imu_channel] ,name=self.IMUaccle_name[imu_channel] ,
                                                     pen=self.pens_solid[imu_channel], symbol='o')
                render_metrics["sensor_setdata_ms"] = max(0.0, (time.monotonic() - sensor_setdata_started) * 1000.0)
            # battery update
            self.update_battery_indicator(self.RSOC, self.Battery_STAT, self.Battery_voltage)
            try:
                active_stream_key = self._display_refresh_stream_key(mode_key)
                target_hz = self._display_refresh_hz(active_stream_key)
                render_metrics["render_interval_hz"] = target_hz
                render_metrics["render_target_hz"] = target_hz
                render_metrics["render_actual_hz"] = self._display_actual_render_hz(active_stream_key)
                refresh_actions = getattr(self, "display_refresh_last_action", None)
                if isinstance(refresh_actions, dict):
                    render_metrics["render_interval_action"] = str(refresh_actions.get(active_stream_key, "") or "")
            except Exception:
                pass
            self.last_detail_render_metrics = render_metrics
           
             
    def _safe_battery_int(self, value, fallback):
        try:
            v = float(value)
        except Exception:
            return int(fallback)
        if not np.isfinite(v):
            return int(fallback)
        return int(v)

    def _sanitize_rsoc_series(self, rsoc_values):
        rsoc_arr = np.asarray(rsoc_values, dtype=np.float32)
        sanitized = np.full(rsoc_arr.shape, np.nan, dtype=np.float32)
        valid_mask = np.isfinite(rsoc_arr) & (rsoc_arr >= 0.0) & (rsoc_arr <= 100.0)
        if np.any(valid_mask):
            sanitized[valid_mask] = rsoc_arr[valid_mask]
        return sanitized

    def _apply_battery_state(self, rsoc_value, stat_value, voltage_value, source):
        if source == "idle" and getattr(self, "current_recording_mode", "Idle") != "Idle":
            return False

        prev_rsoc = int(getattr(self, "RSOC", 0))
        prev_stat = int(getattr(self, "Battery_STAT", 0))
        prev_voltage = int(getattr(self, "Battery_voltage", 0))

        reported_rsoc = self._safe_battery_int(rsoc_value, prev_rsoc)
        stat = self._safe_battery_int(stat_value, prev_stat)
        voltage = self._safe_battery_int(voltage_value, prev_voltage)

        if reported_rsoc < 0 or reported_rsoc > 100:
            reported_rsoc = prev_rsoc
        if stat not in (0, 1, 256, 257):
            stat = prev_stat
        if voltage < 2000 or voltage > 5000:
            voltage = prev_voltage

        now_ts = time.time()
        if self.last_battery_sample_time > 0:
            dt = now_ts - self.last_battery_sample_time
            if dt < 2.0 and abs(voltage - prev_voltage) > 350:
                voltage = prev_voltage

        self.RSOC = reported_rsoc
        self.Reported_RSOC = reported_rsoc
        self.Battery_STAT = stat
        self.Battery_voltage = voltage
        self.last_battery_sample_time = now_ts
        self._update_mode3_battery_trial_gate(trigger_source=f"{source} battery update")
        return True

    def _send_habits_serial_command(self, command: str, action_name: str) -> bool:
        panel = getattr(getattr(self, "habits_tab", None), "habits_panel", None)
        if panel is None or not hasattr(panel, "send_serial_command"):
            self.log_message(
                f"Warning: Habits panel unavailable, skip {action_name}",
                level="warning",
            )
            return False
        try:
            ok = bool(panel.send_serial_command(command))
        except Exception as e:
            self.log_message(
                f"Warning: Failed {action_name} ({str(e)})",
                level="warning",
            )
            return False
        if not ok:
            self.log_message(
                f"Warning: Failed {action_name} (command: {command})",
                level="warning",
            )
        return ok

    def _set_mode3_trial_trigger_paused(self, paused: bool, reason: str = "") -> bool:
        desired = bool(paused)
        if desired == bool(getattr(self, "mode3_trial_trigger_paused", False)):
            return True

        command = "D0," if desired else "D1,"
        action_name = "pausing Mode3 trial trigger" if desired else "resuming Mode3 trial trigger"
        if not self._send_habits_serial_command(command, action_name):
            return False

        self.mode3_trial_trigger_paused = desired
        rsoc = int(getattr(self, "RSOC", 0))
        reason_text = f" ({reason})" if reason else ""
        if desired:
            self.log_message(
                f"Warning: Firmware low-battery pause entered at battery {rsoc}%{reason_text}; GUI low-battery route stays Mode0",
                level="warning",
            )
        else:
            self.log_message(
                f"Info: Firmware low-battery pause exited at battery {rsoc}%{reason_text}",
                level="info",
            )
        return True

    def _refresh_mode3_trial_trigger_target(self):
        rsoc = int(getattr(self, "RSOC", 0))
        voltage = float(getattr(self, "Battery_voltage", 0.0) or 0.0)
        has_battery_signal = bool(rsoc > 0 or voltage > 0.0)
        pause_threshold = int(getattr(self, "mode3_battery_pause_threshold", 10))
        resume_threshold = int(getattr(self, "mode3_battery_resume_threshold", 20))
        low_mode0_target = bool(
            getattr(
                self,
                "low_battery_mode0_training_target",
                False,
            )
        )
        reason = ""

        if not has_battery_signal:
            reason = ""
        elif rsoc < pause_threshold:
            low_mode0_target = True
            reason = f"RSOC<{pause_threshold}%"
        elif rsoc > resume_threshold:
            low_mode0_target = False
            reason = f"RSOC>{resume_threshold}%"

        self.low_battery_mode0_training_target = low_mode0_target
        self.low_battery_mode0_training_reason = reason
        self.mode3_trial_trigger_target_paused = False
        return low_mode0_target, reason

    def _update_mode3_battery_trial_gate(self, trigger_source: str = ""):
        self._refresh_mode3_trial_trigger_target()
        return True

    def _poll_mode3_trial_trigger_gate(self):
        self._update_mode3_battery_trial_gate(trigger_source="10s policy check")

    def _append_spike_raster_buffer(self, spike_raster_data_mode_1):
        try:
            raster_data = np.asarray(spike_raster_data_mode_1, dtype=np.float32)
            if raster_data.ndim != 2 or raster_data.shape[0] < self.spike_channel_num or raster_data.shape[1] <= 0:
                return
            raster_data = raster_data[: self.spike_channel_num]
            next_pointer = self.ring_spike_raster_pointer
            for channel_num in range(self.spike_channel_num):
                next_pointer, _ = self._write_ring_1d(
                    self.spike_raster_data[channel_num],
                    self.ring_spike_raster_pointer,
                    raster_data[channel_num] * (channel_num + 1),
                )
            self.ring_spike_raster_pointer = next_pointer
        except Exception:
            return

    def _raster_display_downsample_factor(self):
        try:
            factor = int(self._display_downsample_factor("spike"))
        except Exception:
            factor = 1
        return max(1, factor)

    def _raster_dynamic_render_stride(self):
        try:
            base_stride = max(1, int(getattr(self, "raster_render_stride", 2) or 2))
        except Exception:
            base_stride = 2
        factor = self._raster_display_downsample_factor()
        multiplier = max(1, factor // 2)
        stride = max(1, base_stride * multiplier)
        self.raster_render_last_factor = int(factor)
        self.raster_render_last_stride = int(stride)
        return int(stride)

    def _maybe_refresh_raster_plot(self, render_metrics=None):
        stride = self._raster_dynamic_render_stride()
        if isinstance(render_metrics, dict):
            render_metrics["raster_display_downsample_factor"] = int(getattr(self, "raster_render_last_factor", 1) or 1)
            render_metrics["raster_render_stride"] = int(stride)
        try:
            self._raster_render_counter += 1
        except Exception:
            self._raster_render_counter = 1
        if self._raster_render_counter < stride:
            return 0.0
        self._raster_render_counter = 0
        raster_setdata_started = time.monotonic()
        self._refresh_raster_plot(render_stride=stride)
        raster_ms = max(0.0, (time.monotonic() - raster_setdata_started) * 1000.0)
        if isinstance(render_metrics, dict):
            render_metrics["raster_setdata_ms"] = raster_ms
        return raster_ms

    def _refresh_raster_plot(self, render_stride=None):
        raster_factor = self._raster_display_downsample_factor()
        try:
            if render_stride is None:
                render_stride = self._raster_dynamic_render_stride()
        except Exception:
            render_stride = getattr(self, "raster_render_last_stride", 1)
        try:
            self.SR_pI_channel.setTitle(
                'Raster / SR <span style="color: yellow; font-size: 10pt">DS x{} stride x{}</span>'.format(
                    int(raster_factor),
                    int(render_stride),
                )
            )
        except Exception:
            pass
        for channel_num in range(self.spike_channel_num):
            spike_idx = np.flatnonzero(self.spike_raster_data[channel_num] > 0)
            max_points = max(1, int(self.raster_max_points_per_channel) // raster_factor)
            if spike_idx.size > max_points:
                step = max(1, int(np.ceil(spike_idx.size / max_points)))
                spike_idx = spike_idx[::step]
            if spike_idx.size == 0:
                self.spike_raster_channel[channel_num].setData([], [])
                continue
            y_vals = np.full(spike_idx.shape, channel_num, dtype=np.float32)
            self.spike_raster_channel[channel_num].setData(spike_idx, y_vals, pen=self.pens_solid[channel_num])

        sr = np.count_nonzero(self.spike_raster_data > 0, axis=0).astype(np.float32)
        sr = my_gaussian_filter1d(sr, sigma=10)
        self.SR_value[0] = sr
        sr_step = max(1, int(self.raster_sr_render_step) * raster_factor)
        if sr_step > 1:
            self.spike_SR_channel.setData(self.spike_raster_x[::sr_step], sr[::sr_step], pen=self.pen_white_2)
        else:
            self.spike_SR_channel.setData(self.spike_raster_x, sr, pen=self.pen_white_2)

    def raw_data_generator(self ,port, data, update_buffers: bool = True):
        """
        update all data array via ring buffer
        @data:
        lfp_mode_0: [[0], self.lfptimestamp_GUI, self.lfpdata_GUI, self.sensordata_GUI]
        spike_mode_1: [[1, self.spike_channel_index_mode1], self.spiketimestamp_GUI, self.spikedata_GUI, self.sensordata_GUI, self.spikerasterdata_GUI]
        """
        if port is not None:
            serial_thread = getattr(self, "mSerial", None)
            file_size_lfp = int(getattr(serial_thread, "file_size_lfp", 0) or 0)
            file_size_mode1 = int(getattr(serial_thread, "file_size_mode1", 0) or 0)
            file_size_mode2 = int(getattr(serial_thread, "file_size_mode2", 0) or 0)
            """ Mode 1&3 """
            if(int(data[0][0]) == 0): #TODO LFP re-reference
                mode3_context = self._is_mode3_display_context()
                if mode3_context and len(data[0]) > 1:
                    mode3_reref_state = int(data[0][1]) if int(data[0][1]) in (0, 1, 2) else 0
                    if mode3_reref_state != self.mode3_esa_reref_mode:
                        self.mode3_esa_reref_mode = mode3_reref_state
                        self.mode3_reref_mode_combo.blockSignals(True)
                        self.mode3_reref_mode_combo.setCurrentIndex(self.mode3_esa_reref_mode)
                        self.mode3_reref_mode_combo.blockSignals(False)
                        self._refresh_mode3_reref_control_style()
                """ LFP raw data figure 1"""
                lfp_timestamp = np.array(data[1])
                raw_data = np.array(data[2])
                feature_scale = self.esa_scale_factor
                feature_data = np.array(data[5], dtype=np.float32)
                if not mode3_context:
                    feature_data = self._smooth_mode0_mand_for_display(feature_data)
                ESA_raw_data = feature_data * feature_scale # Mode0 reuses this container for MAND.
                spike_raster_data_mode_1 = np.array(data[4], dtype=np.float32)
                mode0_raw_data = np.array(data[7], dtype=np.float32).reshape(-1) if len(data) > 7 else np.array([], dtype=np.float32)
                try:
                    mode0_raw_channel = int(data[8])
                except Exception:
                    mode0_raw_channel = int(getattr(self, "spike_raw_channel", 0) or 0)

                packet_indices = np.array(data[6], dtype=np.uint32) if len(data) > 6 else np.array([], dtype=np.uint32)
                loss_packets_value, packet_count = _count_missing_from_indices(packet_indices, expected_step=1)
                self.lfpmisspackets +=  loss_packets_value
                self.lfpaccumulpackets += packet_count
                # 每save file 1次就重新统计丢失的包的数量
                if(file_size_lfp > 0 and self.lfpaccumulpackets >= file_size_lfp):
                    self.lfpaccumulpackets = 0
                    self.lfpmisspackets = 0

                try:
                    lfp_sample_count = int(len(raw_data[0])) if raw_data.ndim >= 2 and raw_data.shape[0] > 0 else 0
                except Exception:
                    lfp_sample_count = 0
                ESA_pointer_temp = self.ring_lfp_pointer
                if update_buffers and lfp_sample_count > 0:
                    for channel_num in range(min(16, int(raw_data.shape[0]))):
                        temp_vdd = raw_data[channel_num] + channel_num * self.separate_interval
                        self._write_ring_1d(
                            self.LFP_raw_data[channel_num],
                            self.ring_lfp_pointer,
                            temp_vdd,
                        )
                    self.ring_lfp_pointer, lfp_wrapped = self._advance_ring_pointer(
                        self.ring_lfp_pointer,
                        lfp_sample_count,
                        self.lfp_display_data_num,
                    )
                    self._lfp_has_wrapped = bool(getattr(self, "_lfp_has_wrapped", False)) or lfp_wrapped
                if update_buffers:
                    if(self.lfp_filter_enabled and self.ring_lfp_pointer > 0):
                        for channel_num in range(16):
                            self.LFP_raw_data[channel_num][0:self.ring_lfp_pointer] = LFP_filter(self.LFP_raw_data[channel_num][0:self.ring_lfp_pointer] - channel_num * self.separate_interval, 
                                                                                                 self.lfp_tab.low_cutoff.currentText(), self.lfp_tab.high_cutoff.currentText()) + channel_num * self.separate_interval

                    if(len(ESA_raw_data[0]) != 0):
                        for channel_num in range(min(16, int(ESA_raw_data.shape[0]))):
                            temp_vdd = ESA_raw_data[channel_num] + channel_num * self.separate_interval
                            self._write_ring_1d(self.ESA_raw_data[channel_num], ESA_pointer_temp, temp_vdd)
                    self._append_spike_raster_buffer(spike_raster_data_mode_1)
                    if mode0_raw_data.size > 0 and not mode3_context:
                        self._set_spike_sample_rate_for_stream(12500.0)
                        previous_mode0_raw_channel = getattr(self, "_last_mode0_raw_channel", None)
                        if previous_mode0_raw_channel is not None and int(previous_mode0_raw_channel) != int(mode0_raw_channel):
                            self._reset_single_raw_chart_buffers(clear_raster=False)
                        self._last_mode0_raw_channel = int(mode0_raw_channel)
                        self.spike_raw_channel = mode0_raw_channel
                        if len(lfp_timestamp) > 0:
                            self.spike_timestamp_note = lfp_timestamp[0]
                        raw_start_pointer = self.ring_spike_pointer
                        self.ring_spike_pointer, raw_wrapped = self._write_ring_1d(
                            self.spike_raw_data[0],
                            raw_start_pointer,
                            mode0_raw_data,
                        )
                        self._write_ring_1d(
                            self.alignment_buffer,
                            raw_start_pointer,
                            np.zeros(int(mode0_raw_data.size), dtype=self.alignment_buffer.dtype),
                        )
                        self._mode0_raw_has_wrapped = bool(getattr(self, "_mode0_raw_has_wrapped", False)) or raw_wrapped
                        if self.spike_filter_enabled:
                            self._apply_spike_filter_buffer()
                        self._mark_spike1ch_rms_samples(mode0_raw_data.size)
        
                # Clear buffers after processing
                # This ensures that even if plot updates are disabled, the data is processed (e.g. for saving)
                # but doesn't accumulate indefinitely in the source if it was meant to be consumed here.
                # However, raw_data_generator consumes `data` which is passed as argument.
                # The caller `update_plot_data` is responsible for clearing the buffers in `SerialPort`?
                # No, `update_plot_data` receives `data` from the signal. The signal emission in `neural_reader.py` clears the buffer in `SerialPort`.
                # So memory leak in `SerialPort` buffers is unlikely if signals are emitted.
                # But what if signals are emitted faster than processed? The queue grows.
                
                pass

            """  mode 1  & 3"""
            if(int(data[0][0]) == 1):
                """ spike raw data + raster data mode 1 """
                if self.current_sample_mode == 1 and not self._is_mode3_display_context():
                    self._set_spike_sample_rate_for_stream(20000.0)
                    self.spike_raw_channel = int(data[0][1])
                else:
                    self._set_spike_sample_rate_for_stream(12500.0)
                    self.spike_raw_channel = int(data[0][1])
                spike_timestamp_mode_1 = np.array(data[1])
                spike_raw_data_mode_1 = np.array(data[2])
                spike_raster_data_mode_1 = np.array(data[4], dtype=np.float32)
                alignment_data = np.array(np.array(data[5], dtype=np.float32) > 0) # 这里只考虑 trial 触发的时候为 1
                if len(spike_timestamp_mode_1) == 0 or len(spike_raw_data_mode_1) == 0:
                    return
                if len(alignment_data) != len(spike_raw_data_mode_1):
                    normalized_alignment = np.zeros(len(spike_raw_data_mode_1), dtype=np.float32)
                    copy_len = min(len(alignment_data), len(spike_raw_data_mode_1))
                    if copy_len > 0:
                        normalized_alignment[:copy_len] = alignment_data[:copy_len]
                    alignment_data = normalized_alignment

                # curr timestamp
                self.spike_timestamp_note = spike_timestamp_mode_1[0]
                
                packet_indices = np.array(data[6], dtype=np.uint32) if len(data) > 6 else np.array([], dtype=np.uint32)
                expected_step = 1 if (self.current_sample_mode == 1 and not self._is_mode3_display_context()) else 4
                loss_packets_value, packet_count = _count_missing_from_indices(packet_indices, expected_step=expected_step)
                self.spikemisspackets_mode_1 +=  loss_packets_value
                self.spikeaccumulpackets_mode_1 += packet_count
                # 每save file一次就重新统计丢失的包的数量
                if(file_size_mode1 > 0 and self.spikeaccumulpackets_mode_1 >= file_size_mode1):
                    self.spikeaccumulpackets_mode_1 = 0
                    self.spikemisspackets_mode_1 = 0
                
                self.single_update_data_size = len(spike_raw_data_mode_1)
                spike_start_pointer = self.ring_spike_pointer
                if update_buffers:
                    self.ring_spike_pointer, spike_wrapped = self._write_ring_1d(
                        self.spike_raw_data[0],
                        spike_start_pointer,
                        spike_raw_data_mode_1,
                    )
                    if len(alignment_data) > 0:
                        self._write_ring_1d(
                            self.alignment_buffer,
                            spike_start_pointer,
                            alignment_data * 1000,
                        )
                    self._spike_raw_has_wrapped = bool(getattr(self, "_spike_raw_has_wrapped", False)) or spike_wrapped

                if update_buffers and self.spike_filter_enabled:
                    self._apply_spike_filter_buffer()

                if update_buffers:
                    self._mark_spike1ch_rms_samples(self.single_update_data_size)

                if update_buffers:
                    self._append_spike_raster_buffer(spike_raster_data_mode_1)
            
                """  mode 2  """
            elif(int(data[0][0]) == 2):
                """ spike raw data AP mode 2 """
                spike_timestamp = np.array(data[1])
                spikeraw_data =data[2]
                packet_indices = np.array(data[4], dtype=np.uint32) if len(data) > 4 else np.array([], dtype=np.uint32)
                alignment_payload = np.asarray(data[6], dtype=np.float32).reshape(-1) if len(data) > 6 else np.asarray([], dtype=np.float32)
                 # curr timestamp
                self.spike_timestamp_note = spike_timestamp[0]

                loss_snapshot_applied = self._apply_mode2_packet_loss_snapshot(data[5] if len(data) > 5 else None)
                if not loss_snapshot_applied:
                    loss_packets_value, packet_count = _count_missing_from_indices(packet_indices, expected_step=1)
                    self.spike_moide2_misspackets +=  loss_packets_value
                    self.spike_mode2_accumulpackets += packet_count

                # 每save file一次就重新统计丢失的包的数量
                if(file_size_mode2 > 0 and self.spike_mode2_accumulpackets >= file_size_mode2):
                    self.spike_mode2_accumulpackets = 0
                    self.spike_moide2_misspackets = 0

                try:
                    spikeraw_iterable = list(spikeraw_data)
                except Exception:
                    spikeraw_iterable = []
                spikeraw_arrays = [
                    np.asarray(ch, dtype=np.float32).reshape(-1)
                    for ch in spikeraw_iterable[:self.spike_channel_num]
                ]
                if len(spikeraw_arrays) < self.spike_channel_num:
                    spikeraw_arrays.extend(
                        np.asarray([], dtype=np.float32)
                        for _ in range(self.spike_channel_num - len(spikeraw_arrays))
                    )
                self.spike_mode2_curr_channel = [len(ch) for ch in spikeraw_arrays]
                
                max_len = max(self.spike_mode2_curr_channel) if self.spike_mode2_curr_channel else 0
                mode2_start_pointer = self.ring_spike_mode2_pointer
                if update_buffers and max_len > 0:
                    for channel_num in range(self.spike_channel_num):
                        channel_values = self._mode2_display_channel_values(
                            spikeraw_arrays[channel_num],
                            max_len,
                            channel_num * self.separate_interval_spike,
                        )
                        self._write_ring_1d(
                            self.spike_mode2_raw_data[channel_num],
                            mode2_start_pointer,
                            channel_values,
                        )
                    if alignment_payload.size > 0:
                        alignment_values = np.zeros(max_len, dtype=np.float32)
                        copy_len = min(max_len, int(alignment_payload.size))
                        if copy_len > 0:
                            alignment_values[:copy_len] = (alignment_payload[:copy_len] > 0).astype(np.float32)
                        alignment_display = alignment_values * (self.separate_interval_spike * 0.8) - (self.separate_interval_spike * 0.9)
                        self._write_ring_1d(
                            self.mode2_alignment_data,
                            mode2_start_pointer,
                            alignment_display,
                        )
                    self.ring_spike_mode2_pointer, mode2_wrapped = self._advance_ring_pointer(
                        mode2_start_pointer,
                        max_len,
                        self.mode2_display_data_num,
                    )
                    self._mode2_has_wrapped = bool(getattr(self, "_mode2_has_wrapped", False)) or mode2_wrapped
                pass


            """ sensing data 9 data """
            # 传感器数据健壮转换。mode2 现在通过独立sensor包携带100Hz accel。
            sensor_payload = data[3] if len(data) > 3 else []
            sensors_data = _sensor_payload_to_padded_array(sensor_payload, row_count=9)

            # Mode3 raw payloads intentionally do not carry sensor rows.
            # They share the "mode 1" chart type for display, so protect the
            # chart path from indexing empty sensor arrays and avoid repeated
            # traceback logging in the hot GUI path.
            if (
                getattr(sensors_data, "ndim", 0) >= 2
                and sensors_data.shape[0] >= 9
                and sensors_data.shape[1] > 0
            ):

                sensor_sample_count = len(sensors_data[0])
                reported_rsoc_series = self._sanitize_rsoc_series(sensors_data[6])

                finite_battery_rows = [
                    row[np.isfinite(row)] for row in (sensors_data[6], sensors_data[7], sensors_data[8])
                ]
                if all(row.size > 0 for row in finite_battery_rows):
                    self._apply_battery_state(
                        finite_battery_rows[0][-1],
                        finite_battery_rows[1][-1],
                        finite_battery_rows[2][-1],
                        source="stream",
                    )
                sensor_start_pointer = self.ring_LSR_pointer
                if update_buffers:
                    next_sensor_pointer = sensor_start_pointer
                    for i in range(6):
                        next_sensor_pointer, _ = self._write_ring_1d(
                            self.IMUdata[i],
                            sensor_start_pointer,
                            sensors_data[i],
                        )

                    self._write_ring_1d(self.RSOC_stat[0], sensor_start_pointer, reported_rsoc_series)
                    self._write_ring_1d(self.STAT_stat[0], sensor_start_pointer, sensors_data[7])
                    self._write_ring_1d(self.Voltage_stat[0], sensor_start_pointer, sensors_data[8])
                    self.ring_LSR_pointer = next_sensor_pointer

    """callback function"""
    def set_connected_port(self, port):
        """Set connected serial port"""
        if not port:
            return False
        try:
            self.curr_active_ports = port
            self.mSerial = SerialPort(self.curr_active_ports,2000000)
            self.mSerial.port_open()
            self.mSerial.mode3_thresholds_uv = list(self.mode3_channel_thresholds_uv)
            self.mSerial.GUIUpdate.connect(self.update_plot_data)
            self.mSerial.EmptyGUIUpdate.connect(self.update_idle_status)
            self.mSerial.CameraGUIUpdate.connect(self.update_carmera_status)
            self.mSerial.ProgressUpdate.connect(self.update_save_progress)
            self.mSerial.ImpedanceProgress.connect(self.handle_impedance_progress)
            self.mSerial.ImpedanceResult.connect(self.handle_impedance_result)
            self.mSerial.SerialDisconnected.connect(self._handle_serial_disconnected)
            if hasattr(self.mSerial, "StatusUpdate"):
                self.mSerial.StatusUpdate.connect(self._handle_serial_status_update)
            if hasattr(self.mSerial, "configure_detail_stream"):
                self.mSerial.configure_detail_stream(min_interval_ms=0)
            if hasattr(self.mSerial, "set_detail_enabled"):
                self.mSerial.set_detail_enabled(True)
            self.mSerial.start()
            # RF power control status update
            self.rf_power_status_update()
            self.on_mode3_reref_mode_changed(self.mode3_esa_reref_mode)
            # HABITS command, avoid duplicate connections after reconnects
            try:
                self.habits_tab.habits_panel.Neural_recorder_command.disconnect(self.HABITS_command_process)
            except Exception:
                pass
            self.habits_tab.habits_panel.Neural_recorder_command.connect(self.HABITS_command_process)
            self.statusBar().showMessage(f"Connected to {port}")
            return True
        except Exception as e:
            print(sys.exc_info())
            self.curr_active_ports = None
            self.mSerial = None
            err_text = str(e)
            if "PermissionError" in err_text or "拒绝访问" in err_text:
                self.log_message(
                    f"Error: Failed to open {port}: access denied. "
                    "The port may be occupied by another app (or RF serial).",
                    level="error"
                )
            else:
                self.log_message(f"Warning: Serial init failed on {port}: {err_text}", level="warning")
            return False
    
    def update_save_progress(self, mode, percent, run_time):
        """Update save progress bars and run time"""
        # Convert run_time (minutes) to Day Hour Min format
        total_min = int(run_time)
        days = total_min // (24 * 60)
        remaining_min = total_min % (24 * 60)
        hours = remaining_min // 60
        minutes = remaining_min % 60
        
        if days > 0:
            time_str = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            time_str = f"{hours}h {minutes}m"
        else:
            time_str = f"{minutes}m"
            
        self.run_time_label.setText(f"Run: {time_str}")
        
        if mode == 0: # LFP
            if hasattr(self.lfp_tab, 'lfp_progress_bar'):
                self.lfp_tab.lfp_progress_bar.setValue(int(percent))
        elif mode == 3: # Mode3
            if hasattr(self.lfp_tab, 'mode3_progress_bar'):
                self.lfp_tab.mode3_progress_bar.setValue(int(percent))
        elif mode == 1: # Mode1
            if hasattr(self.spike1ch_tab, 'mode1_progress_bar'):
                self.spike1ch_tab.mode1_progress_bar.setValue(int(percent))
        elif mode == 2: # Mode2
            if hasattr(self.spike4ch_tab, 'mode2_progress_bar'):
                self.spike4ch_tab.mode2_progress_bar.setValue(int(percent))

    def _refresh_impedance_compensation_summary(self):
        if hasattr(self, 'impedance_tab'):
            metadata = build_impedance_model_metadata(
                self.impedance_series_resistor_kohm,
                self.impedance_shunt_cap_pf,
                self.impedance_test_frequency_hz,
            )
            params = metadata["impedance_model_params"]
            self.impedance_tab.set_compensation_summary(
                "Displayed values use channel-to-REF impedance after removing board RC | "
                f"Board RC: R={self.impedance_series_resistor_kohm:.2f} kOhm, "
                f"C={self.impedance_shunt_cap_pf:.2f} pF, f={self.impedance_test_frequency_hz:.0f} Hz | "
                f"RHD parasitics: Cin_sig={params['signal_input_cap_pf']:.0f} pF, "
                f"Rin_sig={params['signal_input_resistance_ohm'] / 1.0e6:.1f} MOhm, "
                f"Cin_ref={params['reference_input_cap_pf']:.0f} pF, "
                f"Rin_ref={params['reference_input_resistance_ohm'] / 1.0e6:.1f} MOhm"
            )

    @staticmethod
    def _impedance_complex_from_mag_phase(magnitude_ohm, phase_deg):
        return impedance_complex_from_mag_phase(magnitude_ohm, phase_deg)

    @staticmethod
    def _impedance_mag_phase_from_complex(z_value):
        return impedance_mag_phase_from_complex(z_value)

    def _restore_channel_to_ref_impedance(self, measured_magnitude_ohm, measured_phase_deg):
        try:
            return restore_channel_to_ref_impedance(
                measured_magnitude_ohm,
                measured_phase_deg,
                self.impedance_series_resistor_kohm,
                self.impedance_shunt_cap_pf,
                self.impedance_test_frequency_hz,
            )
        except Exception:
            logging.warning("Failed to restore channel-to-REF impedance; using measured value", exc_info=True)
            return self._impedance_complex_from_mag_phase(measured_magnitude_ohm, measured_phase_deg)

    def _current_impedance_model_metadata(self):
        return build_impedance_model_metadata(
            self.impedance_series_resistor_kohm,
            self.impedance_shunt_cap_pf,
            self.impedance_test_frequency_hz,
        )

    @staticmethod
    def _record_uses_current_impedance_model(record, metadata):
        if not isinstance(record, dict):
            return False
        if record.get("impedance_model_version") != metadata.get("impedance_model_version"):
            return False
        params = record.get("impedance_model_params")
        target_params = metadata.get("impedance_model_params", {})
        if not isinstance(params, dict):
            return False
        for key, target_value in target_params.items():
            try:
                if not math.isclose(float(params.get(key)), float(target_value), rel_tol=0.0, abs_tol=1.0e-9):
                    return False
            except Exception:
                return False
        return True

    def _recompute_impedance_record(self, record):
        if not isinstance(record, dict):
            return None
        model_metadata = self._current_impedance_model_metadata()

        raw_magnitude = record.get("raw_impedance_magnitude_ohm", record.get("impedance_ohm", []))
        raw_phase = record.get("raw_impedance_phase_deg")
        if raw_phase is None:
            raw_phase = record.get("impedance_phase_deg")
        if raw_phase is None:
            phase_cdeg = record.get("impedance_phase_cdeg", [])
            raw_phase = [float(v) / 100.0 for v in phase_cdeg]

        try:
            raw_magnitude = [float(v) for v in list(raw_magnitude)[:16]]
            raw_phase = [float(v) for v in list(raw_phase)[:len(raw_magnitude)]]
        except Exception:
            return None

        if not raw_magnitude:
            return None
        if len(raw_phase) < len(raw_magnitude):
            raw_phase.extend([0.0] * (len(raw_magnitude) - len(raw_phase)))

        corrected_magnitude = []
        corrected_phase = []
        corrected_real = []
        corrected_imag = []
        open_marker = float(0xFFFFFFFF)
        for magnitude_ohm, phase_deg in zip(raw_magnitude, raw_phase):
            if magnitude_ohm >= open_marker:
                # Open-circuit channel: preserve marker, skip compensation
                corrected_magnitude.append(magnitude_ohm)
                corrected_phase.append(0.0)
                corrected_real.append(magnitude_ohm)
                corrected_imag.append(0.0)
                continue
            z_corrected = self._restore_channel_to_ref_impedance(magnitude_ohm, phase_deg)
            mag_corrected, phase_corrected = self._impedance_mag_phase_from_complex(z_corrected)
            corrected_magnitude.append(float(mag_corrected))
            corrected_phase.append(float(phase_corrected))
            corrected_real.append(float(z_corrected.real))
            corrected_imag.append(float(z_corrected.imag))

        updated = dict(record)
        if (
            "corrected_impedance_magnitude_ohm" in record
            and not self._record_uses_current_impedance_model(record, model_metadata)
            and "legacy_corrected_impedance_magnitude_ohm" not in updated
        ):
            updated["legacy_corrected_impedance_magnitude_ohm"] = list(record.get("corrected_impedance_magnitude_ohm", []))
            updated["legacy_corrected_impedance_phase_deg"] = list(record.get("corrected_impedance_phase_deg", record.get("impedance_phase_deg", [])))
            updated["legacy_corrected_impedance_real_ohm"] = list(record.get("corrected_impedance_real_ohm", []))
            updated["legacy_corrected_impedance_imag_ohm"] = list(record.get("corrected_impedance_imag_ohm", []))
            updated["legacy_impedance_model_version"] = str(record.get("impedance_model_version") or "legacy_unknown")
            if "impedance_model_params" in record:
                updated["legacy_impedance_model_params"] = dict(record.get("impedance_model_params") or {})
        updated["raw_impedance_magnitude_ohm"] = raw_magnitude
        updated["raw_impedance_phase_deg"] = raw_phase
        updated["corrected_impedance_magnitude_ohm"] = corrected_magnitude
        updated["corrected_impedance_phase_deg"] = corrected_phase
        updated["corrected_impedance_real_ohm"] = corrected_real
        updated["corrected_impedance_imag_ohm"] = corrected_imag
        updated["impedance_ohm"] = corrected_magnitude
        updated["impedance_phase_deg"] = corrected_phase
        updated["impedance_phase_cdeg"] = [int(round(v * 100.0)) for v in corrected_phase]
        updated.update(model_metadata)
        updated["corrected_impedance_quantity"] = model_metadata["impedance_target_quantity"]
        updated["rc_series_resistor_kohm"] = float(self.impedance_series_resistor_kohm)
        updated["rc_shunt_cap_pf"] = float(self.impedance_shunt_cap_pf)
        updated["compensation_frequency_hz"] = float(self.impedance_test_frequency_hz)
        return updated

    def on_impedance_compensation_params_changed(self, *args):
        if hasattr(self, 'impedance_tab'):
            self.impedance_series_resistor_kohm = float(self.impedance_tab.series_resistor_spin.value())
            self.impedance_shunt_cap_pf = float(self.impedance_tab.shunt_cap_spin.value())
        self._refresh_impedance_compensation_summary()

        recomputed_history = []
        for record in list(self.impedance_history):
            updated = self._recompute_impedance_record(record)
            if updated is not None:
                recomputed_history.append(updated)
        self.impedance_history = recomputed_history

        if hasattr(self, 'impedance_tab'):
            self.impedance_tab.refresh_history(self.impedance_history)
            if self.impedance_history:
                self.impedance_tab.update_result(self.impedance_history[-1])

    def _normalize_impedance_record(self, record):
        if not isinstance(record, dict):
            return None
        try:
            source_values = record.get("raw_impedance_magnitude_ohm", record.get("impedance_ohm", []))
            values = [float(v) for v in list(source_values)[:16]]
        except Exception:
            return None
        if not values:
            return None

        timestamp = record.get("timestamp")
        timestamp_epoch = record.get("timestamp_epoch")
        if timestamp_epoch is None:
            try:
                if timestamp:
                    timestamp_epoch = datetime.fromisoformat(str(timestamp)).timestamp()
                else:
                    timestamp_epoch = time.time()
            except Exception:
                timestamp_epoch = time.time()

        if not timestamp:
            timestamp = datetime.fromtimestamp(float(timestamp_epoch)).astimezone().isoformat(timespec='seconds')

        local_time_display = record.get("local_time_display")
        if not local_time_display:
            try:
                local_time_display = datetime.fromtimestamp(float(timestamp_epoch)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                local_time_display = "--"

        normalized = {
            "timestamp": timestamp,
            "timestamp_epoch": float(timestamp_epoch),
            "local_time_display": local_time_display,
            "device_timestamp_ms": int(record.get("device_timestamp_ms", 0) or 0),
            "sample_mode": int(record.get("sample_mode", 0) or 0),
            "channel_count": int(record.get("channel_count", len(values)) or len(values)),
            "impedance_ohm": values,
        }
        if "impedance_phase_cdeg" in record:
            normalized["impedance_phase_cdeg"] = list(record.get("impedance_phase_cdeg", []))
        if "impedance_phase_deg" in record:
            normalized["impedance_phase_deg"] = list(record.get("impedance_phase_deg", []))
        if "raw_impedance_magnitude_ohm" in record:
            normalized["raw_impedance_magnitude_ohm"] = list(record.get("raw_impedance_magnitude_ohm", []))
        if "raw_impedance_phase_deg" in record:
            normalized["raw_impedance_phase_deg"] = list(record.get("raw_impedance_phase_deg", []))
        for key in (
            "corrected_impedance_magnitude_ohm",
            "corrected_impedance_phase_deg",
            "corrected_impedance_real_ohm",
            "corrected_impedance_imag_ohm",
            "impedance_model_version",
            "impedance_model_name",
            "impedance_model_params",
            "impedance_target_quantity",
            "corrected_impedance_quantity",
            "legacy_corrected_impedance_magnitude_ohm",
            "legacy_corrected_impedance_phase_deg",
            "legacy_corrected_impedance_real_ohm",
            "legacy_corrected_impedance_imag_ohm",
            "legacy_impedance_model_version",
            "legacy_impedance_model_params",
        ):
            if key in record:
                value = record.get(key)
                if isinstance(value, list):
                    normalized[key] = list(value)
                elif isinstance(value, dict):
                    normalized[key] = dict(value)
                else:
                    normalized[key] = value
        recomputed = self._recompute_impedance_record(normalized)
        return recomputed if recomputed is not None else normalized

    def _load_impedance_history(self):
        loaded_records = []
        if os.path.exists(self.impedance_history_path):
            try:
                with open(self.impedance_history_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                if isinstance(payload, dict):
                    payload = payload.get("tests", [])
                if isinstance(payload, list):
                    for item in payload:
                        record = self._normalize_impedance_record(item)
                        if record is not None:
                            loaded_records.append(record)
            except Exception as e:
                logging.warning(f"Failed to load impedance history: {e}", exc_info=True)

        loaded_records.sort(key=lambda item: float(item.get("timestamp_epoch", 0.0)))
        self.impedance_history = loaded_records

        if hasattr(self, 'impedance_tab'):
            self.impedance_tab.refresh_history(self.impedance_history)
            if self.impedance_history:
                self.impedance_tab.update_result(self.impedance_history[-1])
            else:
                self.impedance_tab.update_progress(0, "Ready", False)

    def _save_impedance_history(self):
        try:
            os.makedirs(os.path.dirname(self.impedance_history_path), exist_ok=True)
            with open(self.impedance_history_path, "w", encoding="utf-8") as f:
                json.dump(self.impedance_history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Failed to save impedance history: {e}", exc_info=True)
            self.log_message(f"Error: failed to save impedance history: {str(e)}", level="error")

    def start_impedance_test(self):
        if not self._ensure_serial_connected("starting impedance test"):
            return
        if self.impedance_test_running:
            self.log_message("Warning: Impedance test is already running", level="warning")
            return

        try:
            self.impedance_test_running = True
            if hasattr(self, 'impedance_tab'):
                self.impedance_tab.update_progress(0, "Command sent, waiting for device", True)
            if not self._send_neural_command([0x00, 0x09], "starting impedance test"):
                raise RuntimeError("serial command write failed")
            self.statusBar().showMessage("Impedance test command sent")
            self.log_message("Success: impedance test command sent", level="success")
        except Exception as e:
            self.impedance_test_running = False
            if hasattr(self, 'impedance_tab'):
                self.impedance_tab.update_progress(0, "Failed to send command", False)
            self.log_message(f"Error: failed to start impedance test: {str(e)}", level="error")

    def handle_impedance_progress(self, payload):
        if not isinstance(payload, dict):
            return
        stage = int(payload.get("stage", 0))
        current_channel = int(payload.get("current_channel", 0))
        total_channels = max(1, int(payload.get("total_channels", 16)))
        percent = int(payload.get("percent", 0))
        self.impedance_test_running = True

        if stage == 0:
            status_text = "Preparing impedance test"
        elif stage == 1:
            last_channel = max(0, total_channels - 1)
            status_text = f"Testing CH {current_channel}/{last_channel}"
        else:
            status_text = f"Running stage {stage}"

        if hasattr(self, 'impedance_tab'):
            self.impedance_tab.update_progress(percent, status_text, True)
        self.statusBar().showMessage(f"Impedance test: {status_text} ({percent}%)")

    def handle_impedance_result(self, payload):
        self.impedance_test_running = False
        if not isinstance(payload, dict):
            return

        values = [float(v) for v in list(payload.get("impedance_ohm", []))[:16]]
        phase_values = list(payload.get("impedance_phase_deg", []))
        if not phase_values:
            phase_values = [float(v) / 100.0 for v in list(payload.get("impedance_phase_cdeg", []))[:16]]
        phase_values = [float(v) for v in phase_values[:len(values)]]
        if len(phase_values) < len(values):
            phase_values.extend([0.0] * (len(values) - len(phase_values)))
        if not values:
            if hasattr(self, 'impedance_tab'):
                self.impedance_tab.update_progress(0, "No impedance data received", False)
            self.log_message("Warning: impedance test finished without valid data", level="warning")
            return

        local_dt = datetime.now().astimezone()
        record = self._normalize_impedance_record({
            "timestamp": local_dt.isoformat(timespec='seconds'),
            "timestamp_epoch": local_dt.timestamp(),
            "local_time_display": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "device_timestamp_ms": int(payload.get("timestamp_ms", 0) or 0),
            "sample_mode": int(payload.get("sample_mode", 0) or 0),
            "channel_count": int(payload.get("channel_count", len(values)) or len(values)),
            "raw_impedance_magnitude_ohm": values,
            "raw_impedance_phase_deg": phase_values[:len(values)],
            "impedance_ohm": values,
        })
        if record is None:
            return

        if self.impedance_history:
            last_record = self.impedance_history[-1]
            duplicate_result = (
                int(last_record.get("device_timestamp_ms", 0) or 0) == int(record.get("device_timestamp_ms", 0) or 0)
                and list(last_record.get("raw_impedance_magnitude_ohm", [])) == list(record.get("raw_impedance_magnitude_ohm", []))
                and list(last_record.get("raw_impedance_phase_deg", [])) == list(record.get("raw_impedance_phase_deg", []))
            )
        else:
            duplicate_result = False

        if not duplicate_result:
            self.impedance_history.append(record)
            self.impedance_history.sort(key=lambda item: float(item.get("timestamp_epoch", 0.0)))
            self._save_impedance_history()
        else:
            record = self.impedance_history[-1]

        if hasattr(self, 'impedance_tab'):
            self.impedance_tab.update_result(record)
            self.impedance_tab.refresh_history(self.impedance_history)

        avg_value = float(np.mean(np.asarray(record.get("impedance_ohm", values), dtype=np.float32)))
        avg_phase_deg = float(np.mean(np.asarray(record.get("impedance_phase_deg", phase_values[:len(values)]), dtype=np.float32)))
        self.statusBar().showMessage(
            f"Impedance test completed at {record['local_time_display']}"
        )
        self.log_message(
            f"Success: impedance test completed, restored channel-to-REF average {UI.ImpedanceTab.format_impedance_value(avg_value)}, "
            f"phase {avg_phase_deg:.1f} deg",
            level="success"
        )

    def _get_daily_dir(self, base_path, start_time):
        """
        Generate a daily subdirectory path based on the start_time.
        Creates the directory if it doesn't exist.
        """
        return build_daily_file_path(base_path, start_time)

    def _build_video_recording_path(self, source_path):
        if not source_path:
            return None
        try:
            import datetime
            return self._get_daily_dir(source_path, datetime.datetime.now())
        except Exception as e:
            self.log_message(f"Error creating daily directory: {e}", level="error")
            return source_path

    def _start_video_segment(self, save_path):
        if self.camera_module.start_recording(save_path):
            self.video_segment_start_time = time.time()
            return True
        return False

    def _check_video_segment_rotation(self):
        if not self.is_recording:
            return
        if self.video_segment_start_time is None:
            self.video_segment_start_time = time.time()
            return
        if (time.time() - self.video_segment_start_time) < self.video_segment_max_seconds:
            return
        source_path = self.video_recording_source_path if self.video_recording_source_path else None
        next_save_path = self._build_video_recording_path(source_path)
        if not self.camera_module.stop_recording():
            self.log_message("Video segment rotate failed: stop recording failed", level="warning")
            self.video_segment_start_time = time.time()
            return
        if self._start_video_segment(next_save_path):
            self.log_message("Video recording auto-rotated: new 1-hour segment started", level="info")
            return
        self.is_recording = False
        self.video_segment_timer.stop()
        self.video_segment_start_time = None
        self.video_recording_source_path = None
        self.toggle_recording_button.setText("Start recording")
        self.toggle_recording_button.setStyleSheet("background-color: #2196F3; color: white; border-radius: 4px; padding: 5px; font-weight: bold;")
        self.select_save_path_button.setEnabled(True)
        self.log_message("Video recording stopped: failed to start new segment", level="error")

    def toggle_recording(self):
        """Toggle recording with daily directory support"""
        if not self.is_recording:
            # Global Save Check
            if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
                self.log_message("Save Disabled: Global saving is disabled.", level="warning")
                return

            self.video_recording_source_path = self.video_save_path
            final_save_path = self._build_video_recording_path(self.video_recording_source_path)

            # 开始录制
            if self._start_video_segment(final_save_path):
                self.is_recording = True
                self.video_segment_timer.start()
                self.toggle_recording_button.setText("Stop recording")
                self.toggle_recording_button.setStyleSheet("background-color: #FF5252; color: white; border-radius: 4px; padding: 5px; font-weight: bold;")
                self.select_save_path_button.setEnabled(False)
                self.log_message(f"Video recording started: {final_save_path}", level="info")
        else:
            # 停止录制
            if self.camera_module.stop_recording():
                self.is_recording = False
                self.video_segment_timer.stop()
                self.video_segment_start_time = None
                self.video_recording_source_path = None
                self.toggle_recording_button.setText("Start recording")
                self.toggle_recording_button.setStyleSheet("background-color: #2196F3; color: white; border-radius: 4px; padding: 5px; font-weight: bold;")
                self.select_save_path_button.setEnabled(True)
                self.log_message("Video recording stopped", level="info")

    def update_carmera_status(self):
        # 如果摄像头处于记录状态就保存一次文文件
        if(self.toggle_recording_button.text() == "Stop recording"):
            self.toggle_recording()
        # 检查是否摄像头已经打开，否则打开摄像头
        if(self.toggle_camera_button.text() == "Open camera"):
            self.toggle_camera()
        # 检查是否存在一个saving flag 为 true
        if(self.mSerial.save_file_lfp_flag or self.mSerial.save_file_mode3_flag):
            # 开始记录video
            self.toggle_recording()

    def rf_power_on(self):
        self._rf_set_power(True)
        
    def rf_power_off(self):
        self._rf_set_power(False)

    def rf_query_status(self):
        self._rf_query_status()

    def _rf_get_addr(self):
        try:
            if hasattr(self, 'rf_channel_combo'):
                return int(self.rf_channel_combo.currentIndex()) + 1
        except Exception:
            pass
        return 1

    def _rf_build_cmd(self, addr, op):
        start = 0xA0
        checksum = (start + int(addr) + int(op)) & 0xFF
        return bytes([start, int(addr) & 0xFF, int(op) & 0xFF, checksum])

    def _rf_send_cmd(self, op):
        alive, reason = self._is_rf_serial_alive()
        if not alive:
            if getattr(self, 'rf_connected', False):
                self._mark_rf_connection_lost(reason)
            else:
                try:
                    self.statusBar().showMessage("RF serial not connected")
                except Exception:
                    pass
            return b""
        if getattr(self, 'rf_serial_connection', None) is None:
            return b""

        addr = self._rf_get_addr()
        cmd = self._rf_build_cmd(addr, op)
        try:
            response = bytearray()
            with self._rf_serial_lock:
                try:
                    self.rf_serial_connection.reset_input_buffer()
                except Exception:
                    pass
                try:
                    self.rf_serial_connection.reset_output_buffer()
                except Exception:
                    pass
                written = self.rf_serial_connection.write(cmd)
                self.rf_serial_connection.flush()
                if written != len(cmd):
                    raise serial.SerialTimeoutException(
                        f"Partial RF write: expected {len(cmd)} bytes, wrote {written}"
                    )
                deadline = time.monotonic() + 0.3
                while time.monotonic() < deadline:
                    n = getattr(self.rf_serial_connection, 'in_waiting', 0)
                    if n and n > 0:
                        response.extend(self.rf_serial_connection.read(n))
                        time.sleep(0.02)
                    else:
                        time.sleep(0.02)
            return bytes(response)
        except Exception as e:
            self._mark_rf_connection_lost(str(e))
            return b""

    def _rf_parse_status(self, resp):
        if not resp or len(resp) < 3:
            return None
        addr = self._rf_get_addr()
        for i in range(len(resp) - 3, -1, -1):
            if resp[i] == 0xA0 and resp[i + 1] == addr:
                state = resp[i + 2]
                if state == 0x01:
                    return 2
                if state == 0x00:
                    return 1
        return None

    def _rf_set_power(self, on):
        op = 0x03 if on else 0x02
        resp = self._rf_send_cmd(op)
        parsed = self._rf_parse_status(resp)
        if parsed is None:
            time.sleep(0.08)
            query_resp = self._rf_send_cmd(0x05)
            parsed = self._rf_parse_status(query_resp)
        if parsed is None:
            self.rf_power_status = 0
            self.rf_power_status_update()
            self.log_message(
                f"Warning: RF power {'on' if on else 'off'} command sent but device status was not confirmed",
                level="warning"
            )
            return False
        self.rf_power_status = parsed
        self.rf_power_status_update()
        desired = 2 if on else 1
        if parsed != desired:
            self.log_message(
                f"Warning: RF device reported {'ON' if parsed == 2 else 'OFF'} after requesting "
                f"{'ON' if on else 'OFF'}",
                level="warning"
            )
            return False
        return True

    def _rf_query_status(self):
        resp = self._rf_send_cmd(0x05)
        parsed = self._rf_parse_status(resp)
        if parsed is None:
            self.rf_power_status = 0
            self.rf_power_status_update()
            self.log_message("Warning: RF status query returned no valid response", level="warning")
            return False
        self.rf_power_status = parsed
        self.rf_power_status_update()
        return True

    def rf_power_status_update(self):
        """更新RF功率状态显示"""
        if hasattr(self, 'rf_power_status'):
            if self.rf_power_status == 2:  # 开启状态
                self.rf_power_on_button.setStyleSheet("background-color: #66BB6A; color: black; border: none; border-radius: 4px; padding: 5px; font-weight: bold;")
                self.rf_power_off_button.setStyleSheet("background-color: #F44336; color: white; border: none; border-radius: 4px; padding: 5px; font-weight: bold;")
                if hasattr(self, 'rf_status_indicator'):
                    self.rf_status_indicator.setText("RF: ON")
                    self.rf_status_indicator.setStyleSheet("background-color: #66BB6A; color: black; border-radius: 4px; padding: 5px;")
            elif self.rf_power_status == 1:  # 关闭状态
                self.rf_power_off_button.setStyleSheet("background-color: #FF5252; color: black; border: none; border-radius: 4px; padding: 5px; font-weight: bold;")
                self.rf_power_on_button.setStyleSheet("background-color: #2196F3; color: white; border: none; border-radius: 4px; padding: 5px; font-weight: bold;")
                if hasattr(self, 'rf_status_indicator'):
                    self.rf_status_indicator.setText("RF: OFF")
                    self.rf_status_indicator.setStyleSheet("background-color: #FF5252; color: black; border-radius: 4px; padding: 5px;")
            else:
                if hasattr(self, 'rf_status_indicator'):
                    self.rf_status_indicator.setText("RF: --")
                    self.rf_status_indicator.setStyleSheet("background-color: #E0E0E0; color: black; border-radius: 4px; padding: 5px;")
        self._update_mode3_battery_trial_gate(trigger_source="RF status update")

    def rf_power_control(self):
        """
        控制逻辑：判定时间 1分钟一次
        以电池电量为控制变量
        电池80% 以下，打开电源
        电池100% , 关闭电源
        """    
        # print("Current RSOC", self.RSOC)
        Pass
        # 只有在RF串口连接时才进行自动控制
        # if not self.rf_connected:
        #     return
            
        # current_rf_status = getattr(self, 'rf_power_status', 1)  # 默认为关闭状态
        
        # if(self.RSOC < 80 and current_rf_status == 1): # 电量低且RF关闭时，开启RF
        #     if(self.mSerial.save_file_lfp_flag and self.current_sample_mode == 0):
        #         self.rf_power_on()
        # elif(self.RSOC == 100 and current_rf_status == 2): # 电量高且RF开启时，关闭RF
        #     self.rf_power_off()
        
       

    def update_idle_status(self, status):
        accepted = self._apply_battery_state(status[0], status[1], status[2], source="idle")
        if accepted:
            # print(self.RSOC, self.Battery_voltage)
            self.update_battery_indicator(self.RSOC, self.Battery_STAT, self.Battery_voltage)
    
    def start_save_lfp(self):
        if not self._ensure_serial_connected("starting Mode0 saving"):
            return
        # Global Save Check
        if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
            self.log_message("Save Disabled: Global saving is disabled.", level="warning")
            # QMessageBox.warning(self, "Save Disabled", "Global saving is disabled. Please enable it in the top control bar.")
            return

        # 文件路径检查
        if not hasattr(self.lfp_tab, 'lfp_file_path_label') or self.lfp_tab.lfp_file_path_label.text() == "File path don't selected":
            self.log_message("Warning: Please select a Mode0 save path first!", level="warning")
            return
        # 开始 video 记录并只有当之前没有处于记录状态下才开始， 这里只作为初始的开启camera的记录
        if(self.toggle_camera_button.text() == "Open camera"):
            self.toggle_camera()
        if(self.toggle_recording_button.text() == "Start recording"):
            self.toggle_recording()
        try:
            if bool(getattr(self, "low_battery_mode0_training_active", False)) and hasattr(self.mSerial, "finalize_save_buffers"):
                self.mSerial.finalize_save_buffers(modes=["lfp"])
        except Exception:
            self.log_message("Warning: Failed to finalize low-battery Mode0 training buffer before Mode0 save", level="warning")
        # 互斥：仅开启Mode0保存
        self.mSerial.save_file_lfp_flag = True
        self.mSerial.save_file_mode1_flag = False
        self.mSerial.save_file_mode2_flag = False
        self.mSerial.save_file_mode3_flag = False
        # 路径设置
        self.mSerial.lfp_file_addr = self.lfp_tab.lfp_file_path_label.text()
        # 更新按钮状态
        self.lfp_tab.start_save_button.setEnabled(False)
        self.lfp_tab.stop_save_button.setEnabled(True)
        # 关闭其他面板的保存按钮状态
        self.spike1ch_tab.start_save_button.setEnabled(True)
        self.spike1ch_tab.stop_save_button.setEnabled(False)
        self.spike4ch_tab.start_save_button.setEnabled(True)
        self.spike4ch_tab.stop_save_button.setEnabled(False)
        self.lfp_tab.start_save_mode3_button.setEnabled(True)
        self.lfp_tab.stop_save_mode3_button.setEnabled(False)
        self.low_battery_mode0_training_active = False
        

    def stop_save_lfp(self):
        if not self._ensure_serial_connected("stopping Mode0 saving"):
            return
        self.mSerial.save_file_lfp_flag = False
        # self.mSerial.lfp_file_addr = ""
        # 更新按钮状态
        self.lfp_tab.start_save_button.setEnabled(True)
        self.lfp_tab.stop_save_button.setEnabled(False)

    def start_save_mode0_training_to_mode3_path(self):
        if not self._ensure_serial_connected("starting low-battery Mode0 training saving"):
            return False
        if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
            self.log_message("Save Disabled: Global saving is disabled.", level="warning")
            return False
        if not hasattr(self.lfp_tab, 'mode3_file_path_label') or self.lfp_tab.mode3_file_path_label.text() == "File path don't selected":
            self.log_message("Warning: Please select a Mode3 save path first!", level="warning")
            return False
        if(self.toggle_camera_button.text() == "Open camera"):
            self.toggle_camera()
        if(self.toggle_recording_button.text() == "Start recording"):
            self.toggle_recording()
        try:
            if getattr(self.mSerial, "save_file_lfp_flag", False) and hasattr(self.mSerial, "finalize_save_buffers"):
                self.mSerial.finalize_save_buffers(modes=["lfp"])
        except Exception:
            self.log_message("Warning: Failed to finalize previous LFP buffer before low-battery Mode0 training save", level="warning")
        self.mSerial.save_file_lfp_flag = True
        self.mSerial.save_file_mode1_flag = False
        self.mSerial.save_file_mode2_flag = False
        self.mSerial.save_file_mode3_flag = False
        self.mSerial.lfp_file_addr = self.lfp_tab.mode3_file_path_label.text()
        self.lfp_tab.start_save_button.setEnabled(False)
        self.lfp_tab.stop_save_button.setEnabled(True)
        self.spike1ch_tab.start_save_button.setEnabled(True)
        self.spike1ch_tab.stop_save_button.setEnabled(False)
        self.spike4ch_tab.start_save_button.setEnabled(True)
        self.spike4ch_tab.stop_save_button.setEnabled(False)
        self.lfp_tab.start_save_mode3_button.setEnabled(True)
        self.lfp_tab.stop_save_mode3_button.setEnabled(False)
        return True

    def start_save_mode1(self):
        if not self._ensure_serial_connected("starting Mode1 saving"):
            return
        # Global Save Check
        if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
            self.log_message("Save Disabled: Global saving is disabled.", level="warning")
            # QMessageBox.warning(self, "Save Disabled", "Global saving is disabled. Please enable it in the top control bar.")
            return

        # 文件路径检查
        if not hasattr(self.spike1ch_tab, 'mode1_file_path_label') or self.spike1ch_tab.mode1_file_path_label.text() == "File path don't selected":
            self.log_message("Warning: Please select a Mode1 save path first!", level="warning")
            return
        # 互斥：仅开启Mode1保存
        self.mSerial.save_file_lfp_flag = False
        self.mSerial.save_file_mode1_flag = True
        self.mSerial.save_file_mode2_flag = False
        self.mSerial.save_file_mode3_flag = False
        # 路径设置
        self.mSerial.mode1_file_addr = self.spike1ch_tab.mode1_file_path_label.text()
        # 更新按钮状态
        self.spike1ch_tab.start_save_button.setEnabled(False)
        self.spike1ch_tab.stop_save_button.setEnabled(True)
        self.lfp_tab.start_save_button.setEnabled(True)
        self.lfp_tab.stop_save_button.setEnabled(False)
        self.lfp_tab.start_save_mode3_button.setEnabled(True)
        self.lfp_tab.stop_save_mode3_button.setEnabled(False)
        self.spike4ch_tab.start_save_button.setEnabled(True)
        self.spike4ch_tab.stop_save_button.setEnabled(False)

    def stop_save_mode1(self):
        if not self._ensure_serial_connected("stopping Mode1 saving"):
            return
        self.mSerial.save_file_mode1_flag = False
        # 更新按钮状态
        self.spike1ch_tab.start_save_button.setEnabled(True)
        self.spike1ch_tab.stop_save_button.setEnabled(False)

    
    def start_save_mode2(self, sync_camera=False):
        if not self._ensure_serial_connected("starting Mode2 saving"):
            return False
        # Global Save Check
        if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
            self.log_message("Save Disabled: Global saving is disabled.", level="warning")
            # QMessageBox.warning(self, "Save Disabled", "Global saving is disabled. Please enable it in the top control bar.")
            return False

        # 文件路径检查
        if not hasattr(self.spike4ch_tab, 'file_path_label') or self.spike4ch_tab.file_path_label.text() == "file path don't selected":
            self.log_message("Warning: Please select a Mode2 save path first!", level="warning")
            return False
        if sync_camera:
            if(self.toggle_camera_button.text() == "Open camera"):
                self.toggle_camera()
            if(self.toggle_recording_button.text() == "Start recording"):
                self.toggle_recording()
        # 互斥：仅开启Mode2保存
        self.mSerial.save_file_lfp_flag = False
        self.mSerial.save_file_mode1_flag = False
        self.mSerial.save_file_mode2_flag = True
        self.mSerial.save_file_mode3_flag = False
        # 路径设置
        self.mSerial.mode2_file_addr = self.spike4ch_tab.file_path_label.text()
        # 更新按钮状态
        self.spike4ch_tab.start_save_button.setEnabled(False)
        self.spike4ch_tab.stop_save_button.setEnabled(True)
        self.lfp_tab.start_save_button.setEnabled(True)
        self.lfp_tab.stop_save_button.setEnabled(False)
        self.lfp_tab.start_save_mode3_button.setEnabled(True)
        self.lfp_tab.stop_save_mode3_button.setEnabled(False)
        self.spike1ch_tab.start_save_button.setEnabled(True)
        self.spike1ch_tab.stop_save_button.setEnabled(False)
        return True

    def stop_save_mode2(self):
        if not self._ensure_serial_connected("stopping Mode2 saving"):
            return
        self.mSerial.save_file_mode2_flag = False
        self.mSerial.mode2_file_addr = ""
        # 更新按钮状态
        self.spike4ch_tab.start_save_button.setEnabled(True)
        self.spike4ch_tab.stop_save_button.setEnabled(False)

    def start_save_mode3(self):
        if not self._ensure_serial_connected("starting Mode3 saving"):
            return
        # Global Save Check
        if hasattr(self, 'global_save_enable_button') and not self.global_save_enable_button.isChecked():
            self.log_message("Save Disabled: Global saving is disabled.", level="warning")
            # QMessageBox.warning(self, "Save Disabled", "Global saving is disabled. Please enable it in the top control bar.")
            return

        # 文件路径检查
        if not hasattr(self.lfp_tab, 'mode3_file_path_label') or self.lfp_tab.mode3_file_path_label.text() == "File path don't selected":
            self.log_message("Warning: Please select a Mode3 save path first!", level="warning")
            return
        if(self.toggle_camera_button.text() == "Open camera"):
            self.toggle_camera()
        if(self.toggle_recording_button.text() == "Start recording"):
            self.toggle_recording()
        # 互斥：仅开启Mode3保存
        self.mSerial.save_file_lfp_flag = False
        self.mSerial.save_file_mode1_flag = False
        self.mSerial.save_file_mode2_flag = False
        self.mSerial.save_file_mode3_flag = True
        # 路径设置
        self.mSerial.mode3_file_addr = self.lfp_tab.mode3_file_path_label.text()
        # 更新按钮状态
        self.lfp_tab.start_save_mode3_button.setEnabled(False)
        self.lfp_tab.stop_save_mode3_button.setEnabled(True)
        self.lfp_tab.start_save_button.setEnabled(True)
        self.lfp_tab.stop_save_button.setEnabled(False)
        self.spike4ch_tab.start_save_button.setEnabled(True)
        self.spike4ch_tab.stop_save_button.setEnabled(False)
        self.spike1ch_tab.start_save_button.setEnabled(True)
        self.spike1ch_tab.stop_save_button.setEnabled(False)

    def stop_save_mode3(self):
        if not self._ensure_serial_connected("stopping Mode3 saving"):
            return
        self.mSerial.save_file_mode3_flag = False
        # 更新按钮状态
        self.lfp_tab.start_save_mode3_button.setEnabled(True)
        self.lfp_tab.stop_save_mode3_button.setEnabled(False)

    def sample_start(self):
        if not self._ensure_serial_connected("starting sampling"):
            return
        self.mSerial.flush()
        self.open_command = [0x01 ,0x00] 
        if not self._send_neural_command(self.open_command, "starting sampling", transport_retries=2):
            return
        time.sleep(0.1)
        if self.mSerial is not None:
            self.mSerial.begin_mode_transition(100)
        self.current_recording_mode = self.sampling_mode_combo.currentText()

    def sample_stop(self):
        if not self._ensure_serial_connected("stopping sampling"):
            return
        self.close_command = [0x02 ,0x00] # invalid sample mode
        if not self._send_neural_command(self.close_command, "stopping sampling", transport_retries=2):
            return
        if self.mSerial is not None:
            self.mSerial.begin_mode_transition(100)
        self.current_recording_mode = "Idle"

    def sample_mode_switch(self, mode=None): # 切换选项卡的时候就会触发 模式的改变
        if not self._ensure_serial_connected("switching sampling mode"):
            return
        if(mode == None):
            mode = self.sampling_mode_combo.currentIndex()
        prev_mode = self.current_sample_mode
        if not self._send_neural_command([0x00 ,0x03, int(hex(mode) ,16), 0x00], "switching sampling mode"):
            return
        if self.mSerial is not None:
            self.mSerial.begin_mode_transition(100)
        self.current_sample_mode = mode
        self._sample_mode_hint_initialized = True
        if mode == 3:
            self._mark_mode3_display_hint(True)
        else:
            self._mark_mode3_display_hint(False)
        if (prev_mode in (0, 1, 3) or self.current_sample_mode in (0, 1, 3)) and (prev_mode != self.current_sample_mode):
            self._reset_single_raw_chart_buffers(clear_raster=(prev_mode in (1, 3) or self.current_sample_mode in (1, 3)))
        if (prev_mode in (0, 3) or self.current_sample_mode in (0, 3)) and (prev_mode != self.current_sample_mode):
            self._reset_lfp_chart_buffers()
        if (prev_mode == 2 or self.current_sample_mode == 2) and (prev_mode != self.current_sample_mode):
            self._reset_mode2_chart_buffers()
        
        # Update current recording mode string if we are recording
        if hasattr(self, 'current_recording_mode'):
            self.current_recording_mode = self.sampling_mode_combo.itemText(mode)
            
        # 根据选择的采样模式跳转到对应选项卡
        if self.current_sample_mode == 0:
            self.tab_widget.setCurrentIndex(0)  # 16通道LFP
        elif self.current_sample_mode == 1:
            self.tab_widget.setCurrentIndex(4)  #  单通道Spike
        elif self.current_sample_mode == 2:
            self.tab_widget.setCurrentIndex(1)  # 4通道Spike
        if self.current_sample_mode in (1, 3):
            time.sleep(0.01)
            self._apply_mode_raw_channel_profile(self.current_sample_mode)
    
    def _cache_mode3_threshold(self, channel_idx, threshold_uv):
        channel_idx = int(channel_idx)
        if channel_idx < 0 or channel_idx >= len(self.mode3_channel_thresholds_uv):
            return
        self.mode3_channel_thresholds_uv[channel_idx] = float(threshold_uv)
        if self.mSerial is not None:
            self.mSerial.mode3_thresholds_uv = list(self.mode3_channel_thresholds_uv)

    def _clamp_neural_channel(self, channel_idx):
        channel_idx = max(0, min(self.spike_channel_num - 1, int(channel_idx)))
        return channel_idx

    def _send_raw_channel_with_threshold(
        self,
        gui_channel,
        threshold_uv,
        use_mode1_mapping,
        action_desc,
    ):
        gui_channel = int(gui_channel)
        _ = use_mode1_mapping
        command_channel = self._clamp_neural_channel(gui_channel)
        temp_threshold, threshold_uv_abs, clipped = self._encode_absolute_threshold(threshold_uv)
        temp_threshold_hex = format(temp_threshold & int('0xFFFF', 16), '04x')
        channel_command = [0x00, 0x04, int(command_channel), 0x00, int(temp_threshold_hex[2:4], 16), int(temp_threshold_hex[0:2], 16)]
        ok = self._send_neural_command(channel_command, action_desc)
        return ok, threshold_uv_abs, clipped, command_channel

    def _apply_mode_raw_channel_profile(self, mode):
        if not self._ensure_serial_connected("applying mode raw-channel profile"):
            return False
        if mode == 1:
            gui_channel = int(self.spike1ch_tab.channel_combo.currentIndex())
            threshold_uv = float(self.mode3_channel_thresholds_uv[self._clamp_neural_channel(gui_channel)])
            ok, _, _, _ = self._send_raw_channel_with_threshold(
                gui_channel, threshold_uv, use_mode1_mapping=True, action_desc="applying mode1 raw-channel profile"
            )
            return ok
        if mode == 3:
            gui_channel = int(self.spike1ch_tab.mode3_channel_combo.currentIndex())
            threshold_uv = float(self.mode3_channel_thresholds_uv[gui_channel])
            ok, _, _, _ = self._send_raw_channel_with_threshold(
                gui_channel, threshold_uv, use_mode1_mapping=False, action_desc="applying mode3 raw-channel profile"
            )
            return ok
        return True

    def _encode_absolute_threshold(self, threshold_uv):
        threshold_uv_abs = abs(float(threshold_uv))
        temp_threshold = int((threshold_uv_abs / 1000 / 1000 * 192 + 1.225) / self.mSerial.DAC_resolution) - int('0x8000', 16)
        clipped = False
        if temp_threshold < 0:
            temp_threshold = 0
            clipped = True
        elif temp_threshold > int('0x7FFF', 16):
            temp_threshold = int('0x7FFF', 16)
            clipped = True
        return temp_threshold, threshold_uv_abs, clipped
    
    def spike_channel_switch_mode1(self, update_threshold=False): 
        if not self._ensure_serial_connected("setting spike channel"):
            return
        if (self.current_sample_mode >= 0):
            current_channel = self.spike1ch_tab.channel_combo.currentIndex()
            if update_threshold:
                threshold_uv_raw = float(self.mSerial.calc_SpikeThreshold(self.spike_raw_data[0]))
            else:
                threshold_uv_raw = float(self.mode3_channel_thresholds_uv[self._clamp_neural_channel(current_channel)])
            ok, threshold_uv, clipped, _ = self._send_raw_channel_with_threshold(
                current_channel, threshold_uv_raw, use_mode1_mapping=True, action_desc="setting mode1 spike channel"
            )
            if not ok:
                return
            self._reset_single_raw_chart_buffers(clear_raster=False)
            if clipped:
                self.statusBar().showMessage("Warning: Auto threshold clipped to valid absolute range")
            else:
                if update_threshold:
                    self.statusBar().showMessage("Threshold update: channel {} threshold {} uV".format(current_channel, threshold_uv))
                else:
                    self.statusBar().showMessage("Mode1 raw channel {} applied (cached threshold {} uV)".format(current_channel, threshold_uv))
            if update_threshold:
                self._cache_mode3_threshold(self._clamp_neural_channel(current_channel), threshold_uv)
        else:
            self.log_message("Warning: Please set the sample mode to Spike Mode 1!", level="warning")
            pass

    def spike_channel_switch_mode3(self):
        if not self._ensure_serial_connected("setting mode0/mode3 raw channel"):
            return
        current_channel = self.spike1ch_tab.mode3_channel_combo.currentIndex()
        threshold_uv = float(self.mode3_channel_thresholds_uv[current_channel])
        ok, threshold_uv_abs, clipped, _ = self._send_raw_channel_with_threshold(
            current_channel, threshold_uv, use_mode1_mapping=False, action_desc="setting mode0/mode3 raw channel"
        )
        if not ok:
            return
        self._reset_single_raw_chart_buffers(clear_raster=False)
        if clipped:
            self.statusBar().showMessage("Warning: Mode0/3 threshold clipped to valid absolute range")
        else:
            self.statusBar().showMessage("Mode0/3 raw channel {} applied (threshold {} uV)".format(current_channel, threshold_uv_abs))
    
    def spike_channel_switch_mode2(self):
        message = "Mode2 v2 streams fixed 16 channels (Ch0-Ch15); channel selection is disabled."
        try:
            self.statusBar().showMessage(message)
        except Exception:
            pass
        try:
            self.log_message(message, level="info")
        except Exception:
            pass

    def Auto_threshold_update(self):
        if not self._ensure_serial_connected("auto threshold update"):
            return
        if (self.current_sample_mode >= 0):
            self.Auto_ST_Update = True
            self.channel_panding = 0
            self.Auto_ST_Update_flag = 0
            self.statusBar().showMessage("Note:Auto Spike Threshold begining!")
        else:
            self.log_message("Warning: Please set the sample mode to Spike Mode 1!", level="warning")
            pass

    def spike_threshold_set(self): #TODO channel 0, 14 spike raster 有问题可能
        if not self._ensure_serial_connected("setting spike threshold"):
            return
        if (self.current_sample_mode >= 0):
            current_channel = self.raster_tab.channel_combo.currentIndex()
            # threshold manual setting 
            threshold_text = self.raster_tab.threshold_combo.currentText().strip()
            if threshold_text == "":
                self.log_message("Warning: Threshold is empty", level="warning")
                return
            try:
                threshold_uv = float(threshold_text)
            except ValueError:
                self.log_message("Warning: Threshold must be numeric", level="warning")
                return
            use_mode1_mapping = (self.current_sample_mode == 1)
            ok, threshold_uv, clipped, _ = self._send_raw_channel_with_threshold(
                current_channel, threshold_uv, use_mode1_mapping=use_mode1_mapping, action_desc="setting spike threshold"
            )
            if not ok:
                return
            self.raster_tab.threshold_combo.setCurrentText(str(threshold_uv))
            if clipped:
                self.statusBar().showMessage("Warning: Manual threshold clipped to valid absolute range")
            else:
                self.statusBar().showMessage("Threshold update: channel {} threshold {} uV".format(current_channel, threshold_uv))
            cache_channel = self._clamp_neural_channel(current_channel)
            self._cache_mode3_threshold(cache_channel, threshold_uv)

        else:
            self.log_message("Warning: Please set the sample mode to Spike Mode 1!", level="warning")
            pass

    def IMU_mode_setting(self):
        if not self._ensure_serial_connected("setting IMU mode"):
            return
        self.IMU_command = [0x03, 0x00 ,  0x00 ,0x00] 
        # battery_mode
        self.IMU_command[-2] = int(hex(self.battery_status_combo.currentIndex() + 1) ,16)
        print(self.IMU_command)
        self._send_neural_command(self.IMU_command, "setting IMU mode")

    def HABITS_command_process(self, command):
        # 根据模式来开启和关闭当前trialblock的数据保存
        # print(command)
        if(command[0] == 'L'): # trial block end 
            # mode3 file saved
            self.start_save_lfp()
            self.sample_mode_switch(0) 
            time.sleep(0.1) # 等待 把 esa 模式关闭 switch to lfp mode
            if not getattr(self, 'rf_connected', False):
                self.log_message("Warning: RF control serial not connected; cannot turn RF power on", level="warning")
            else:
                self.rf_power_on()
             
        elif(command[0] == 'E'): # trial block onset -> file saving -> LFP
            if bool(getattr(self, "low_battery_mode0_training_target", False)):
                self.low_battery_mode0_training_active = True
                try:
                    self.habits_tab.habits_panel.send_rf_off_ready_command()
                    self.log_message("Info: F sent for low-battery Mode0 training route; RF kept ON", level="info")
                except Exception:
                    self.log_message("Warning: Failed to send F for low-battery Mode0 training route", level="warning")
                if self.start_save_mode0_training_to_mode3_path():
                    self.sample_mode_switch(0)
                return
            rf_off_confirmed = False
            if not getattr(self, 'rf_connected', False):
                self.log_message("Warning: RF control serial not connected; cannot turn RF power off", level="warning")
            else:
                self.rf_power_off()
                rf_off_confirmed = (getattr(self, 'rf_power_status', None) == 1)
                if not rf_off_confirmed:
                    self._rf_query_status()
                    rf_off_confirmed = (getattr(self, 'rf_power_status', None) == 1)
            if rf_off_confirmed:
                try:
                    self.habits_tab.habits_panel.send_rf_off_ready_command()
                except Exception:
                    self.log_message("Warning: Failed to send RF-off confirmation command F to HABITS", level="warning")
            else:
                self.log_message("Warning: RF OFF confirmation not available, skip sending F", level="warning")

            self._reset_charging_guard("RF off")
            if self.mode3_trial_trigger_paused:
                self.log_message(
                    f"Info: Battery RSOC is {self.RSOC}%; firmware reports low-battery pause, but normal ESA route remains enabled.",
                    level="info",
                )
            self.start_save_mode2(sync_camera=True)
            # mode2 file save begin
            self.sample_mode_switch(2)  #  ESA -> mode2;  LFP -> mode0
        pass
    def _refresh_lfp_filter_controls(self):
        try:
            self.lfp_tab.enable_filter_button.setEnabled(not bool(self.lfp_filter_enabled))
            self.lfp_tab.disable_filter_button.setEnabled(bool(self.lfp_filter_enabled))
            if self.lfp_filter_enabled:
                self.lfp_tab.enable_filter_button.setStyleSheet(
                    "QPushButton { background-color: #2E7D32; color: white; font-weight: bold; }"
                )
                self.lfp_tab.disable_filter_button.setStyleSheet("")
            else:
                self.lfp_tab.enable_filter_button.setStyleSheet("")
                self.lfp_tab.disable_filter_button.setStyleSheet(
                    "QPushButton { background-color: #C62828; color: white; font-weight: bold; }"
                )
        except Exception:
            pass

    def lfp_filter_on(self):
        self.lfp_filter_enabled = True
        self._refresh_lfp_filter_controls()
        try:
            self.statusBar().showMessage(
                f"LFP filter on: {self.lfp_tab.low_cutoff.currentText()}-{self.lfp_tab.high_cutoff.currentText()} Hz"
            )
        except Exception:
            pass
    
    def lfp_filter_off(self):
        self.lfp_filter_enabled = False
        self._refresh_lfp_filter_controls()
        try:
            self.statusBar().showMessage("LFP filter off")
        except Exception:
            pass



""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""""
def main():
    """主函数"""
    setup_logging()
    
    app = QApplication(sys.argv)
    main_window = ESBMainWindow()
    main_window.show()
    sys.exit(app.exec())

import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()


    
