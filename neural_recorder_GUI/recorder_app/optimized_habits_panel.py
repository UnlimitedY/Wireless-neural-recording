"""
Optimized Habits Panel for Neural Recorder GUI
优化的Habits面板，整合了所有核心功能，简化界面，优化布局
"""

import os
import sys
import time
import json
import pickle
import queue
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional, Tuple
import threading
from collections import defaultdict, deque

import serial
import serial.tools.list_ports
import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import (QThread, pyqtSignal, QTimer, Qt, QDate)
try:
    from ..support.path_utils import get_data_directory, get_mouse_data_directory, get_default_save_path
except ImportError:
    from support.path_utils import get_data_directory, get_mouse_data_directory, get_default_save_path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox,
    QLabel, QPushButton, QSpinBox, QDoubleSpinBox, QLineEdit, QTextEdit,
    QComboBox, QListWidget, QDateEdit, QMessageBox, QSizePolicy,
    QFrame, QSplitter, QFileDialog, QScrollArea, QDialog
)
from PyQt6.QtGui import QFont, QColor, QPalette


PSYCHOMETRIC_PROTOCOLS = {5, 6, 7, 8, 9, 10}
PSYCHOMETRIC_VALID_RULES = {0, 1, 2}
PSYCHOMETRIC_STIM_LEVELS: Tuple[float, ...] = (-1.0, -0.5, 0.0, 0.5, 1.0)
PSYCHOMETRIC_FREQ_KHZ: Tuple[float, ...] = (3.0, 4.243, 6.0, 8.485, 12.0)
PSYCHOMETRIC_FREQ_TICKS = [
    (3.0, "3.0"),
    (4.243, "4.243"),
    (6.0, "6.0"),
    (8.485, "8.485"),
    (12.0, "12.0"),
]
PSYCHOMETRIC_RULE_STYLES = {
    0: {"label": "Rule 0 (P5/6)", "color": (255, 193, 7)},
    1: {"label": "Rule 1 (P7/8)", "color": (0, 229, 255)},
    2: {"label": "Rule 2 (P9/10)", "color": (255, 99, 132)},
}


def normalize_psychometric_level(value: Any) -> Optional[float]:
    """Map incoming stimulus values onto the 5 task frequency levels."""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return None

    for level in PSYCHOMETRIC_STIM_LEVELS:
        if abs(numeric_value - level) <= 0.05:
            return level
    return None


def infer_right_choice(trial_type: Any, outcome: Any) -> Optional[int]:
    """Recover the animal's actual side choice from trial type and outcome."""
    try:
        trial_type = int(trial_type)
        outcome = int(outcome)
    except (TypeError, ValueError):
        return None

    if trial_type not in (1, 2) or outcome not in (1, 2):
        return None

    if outcome == 1:
        chosen_side = trial_type
    else:
        chosen_side = 1 if trial_type == 2 else 2

    return 1 if chosen_side == 2 else 0


class SerialWorker(QThread):
    """Serial communication worker thread"""
    connected = pyqtSignal(str)
    data_received = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    disconnected = pyqtSignal(str)
    
    def __init__(self, port: str, baudrate: int = 115200):
        super().__init__()
        self.port = port
        self.baudrate = baudrate
        self.serial_connection = None
        self.running = False
        self._stop_requested = False
        self._write_lock = threading.Lock()
        self._outbound_queue = queue.Queue()
        self._disconnect_emitted = False
        self.last_rx_monotonic = 0.0

    def _emit_disconnect_once(self, message: str):
        if self._stop_requested or self._disconnect_emitted:
            return
        self._disconnect_emitted = True
        self.disconnected.emit(message)
        
    def run(self):
        """Run serial listener"""
        try:
            self.serial_connection = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=0.2,
                write_timeout=1
            )
            self.running = True
            self._stop_requested = False
            self._disconnect_emitted = False
            self.last_rx_monotonic = time.monotonic()
            self.connected.emit(self.port)
            
            while self.running:
                try:
                    self._drain_outbound_commands()
                    if self.serial_connection.in_waiting > 0:
                        try:
                            data = self.serial_connection.readline().decode('utf-8').strip()
                            if data:
                                self.last_rx_monotonic = time.monotonic()
                                self.data_received.emit(data)
                        except UnicodeDecodeError:
                            continue
                    else:
                        time.sleep(0.01) # Sleep 10ms when no data
                except (serial.SerialException, OSError) as e:
                    self._emit_disconnect_once(f"Serial connection lost on {self.port}: {str(e)}")
                    break
                        
        except Exception as e:
            self.error_occurred.emit(f"Serial connection error: {str(e)}")
        finally:
            self.running = False
            if self.serial_connection and self.serial_connection.is_open:
                try:
                    self.serial_connection.close()
                except Exception:
                    pass
            
    def send_data(self, data: str) -> bool:
        """Queue data for the serial worker thread to send."""
        if not self.running or self.serial_connection is None or not self.serial_connection.is_open:
            self.error_occurred.emit("Habits serial is not connected")
            return False
        try:
            self._outbound_queue.put_nowait(str(data))
            return True
        except Exception as e:
            self.error_occurred.emit(f"Failed to queue data: {str(e)}")
        return False

    def _write_data_inline(self, data: str) -> bool:
        try:
            if self.serial_connection and self.serial_connection.is_open:
                payload = (str(data) + '\n').encode('utf-8')
                with self._write_lock:
                    written = self.serial_connection.write(payload)
                    self.serial_connection.flush()
                if written != len(payload):
                    raise serial.SerialTimeoutException(
                        f"Partial write: expected {len(payload)} bytes, wrote {written}"
                    )
                return True
        except Exception as e:
            self.error_occurred.emit(f"Failed to send data: {str(e)}")
            self._emit_disconnect_once(f"Serial connection lost on {self.port}: {str(e)}")
        return False

    def _drain_outbound_commands(self, max_commands: int = 32) -> None:
        for _ in range(max(1, int(max_commands or 1))):
            try:
                data = self._outbound_queue.get_nowait()
            except queue.Empty:
                return
            try:
                if not self._write_data_inline(data):
                    return
            finally:
                try:
                    self._outbound_queue.task_done()
                except Exception:
                    pass
        
    def stop(self, wait_ms: int = 1000):
        """Stop serial communication"""
        self.running = False
        self._stop_requested = True
        if self.serial_connection and self.serial_connection.is_open:
            try:
                self.serial_connection.close()
            except Exception:
                pass
        self.quit()
        try:
            self.wait(max(0, int(wait_ms)))
        except TypeError:
            self.wait()


class TrialDataManager:
    """Efficient trial data manager"""
    
    def __init__(self, max_memory_trials: int = 100000):
        self.max_memory_trials = max_memory_trials
        self.trial_data = deque(maxlen=max_memory_trials)  # 使用deque限制内存使用
        
        # 缓存变量，避免重复计算
        self._daily_trial_cache = {}  # 日期 -> 试验次数
        self._performance_cache = deque(maxlen=max_memory_trials)  
        self._last_cache_update = None
        self._cache_dirty = True
        
        # 按日期索引的数据，用于快速查找
        self._trials_by_date = defaultdict(list)
        
        # Event data storage (last 50 trials)
        self.event_data = deque(maxlen=50)
        self.state_data = deque(maxlen=50) # Store state data

        # Mode switches storage
        self.mode_switches = [] # List of dicts: {'start': datetime, 'end': datetime}

    def add_event_data(self, trial_num, events):
        """Add event data for a trial"""
        # Check if we already have state data for this trial
        state_info = None
        for item in self.state_data:
            if item['trial_num'] == trial_num:
                state_info = item
                break
        
        self.event_data.append({
            'trial_num': trial_num,
            'events': events,
            'state_info': state_info # Link state info if available
        })

    def add_state_data(self, trial_num, outcome, states):
        """Add state data for a trial"""
        state_entry = {
            'trial_num': trial_num,
            'outcome': outcome,
            'states': states
        }
        self.state_data.append(state_entry)
        
        # Try to link with existing event data
        for item in self.event_data:
            if item['trial_num'] == trial_num:
                item['state_info'] = state_entry
                break

    def get_event_data(self):
        """Get recent event data (with linked state info)"""
        # Ensure latest links
        events = list(self.event_data)
        states = list(self.state_data)
        
        # Create a lookup for states
        state_map = {s['trial_num']: s for s in states}
        
        # Merge
        result = []
        for e in events:
            # Prefer fresh lookup over stored link
            s = state_map.get(e['trial_num'])
            if s:
                e['state_info'] = s
            result.append(e)
            
        return result

    def get_state_data(self):
        """Get recent state data"""
        return list(self.state_data)

    def get_event_data_raw(self):
        """Get recent event data (raw)"""
        return list(self.event_data)

    def add_mode_switch(self, switch_data: Dict[str, datetime]):
        """Add a mode switch interval"""
        self.mode_switches.append(switch_data)
        
    def get_recent_mode_switches(self, hours: int = 24) -> List[Dict[str, datetime]]:
        """Get mode switches from the last N hours"""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        recent = []
        for switch in self.mode_switches:
            # Check if the interval overlaps with the recent period
            if switch['end'] >= cutoff_time: 
                recent.append(switch)
        return recent
        
    def add_trial(self, trial_data: Dict[str, Any]):
        """Add trial data"""
        self.trial_data.append(trial_data)
        # 更新日期索引
        date_key = trial_data['timestamp'].date()
        self._trials_by_date[date_key].append(trial_data)
        
        # 更新性能缓存（如果有相关字段）
        if 'trial_num' in trial_data and 'performance' in trial_data:
            self._performance_cache.append({
                'trial_num': trial_data['trial_num'],
                'performance': trial_data['performance'],
                'early_lick': trial_data.get('early_lick_rate', 0),
                'protocol_perf': trial_data.get('protocol_perf', 0)
            })
        
        # 标记缓存为脏
        self._cache_dirty = True
        
        # 清理过期的日期索引（保留最近30天）
        # self._cleanup_old_indices()
        

    def get_daily_trial_count(self, date: datetime.date = None) -> int:
        """Get trial count for a given date (cached)"""
        if date is None:
            date = datetime.now().date()
            
        # 检查缓存
        if not self._cache_dirty and date in self._daily_trial_cache:
            return self._daily_trial_cache[date]
            
        # 使用索引快速计算
        count = len(self._trials_by_date[date])
        self._daily_trial_cache[date] = count
        
        return count
        
    def get_recent_trials(self, hours: int = 24) -> List[Dict[str, Any]]:
        """Get trials from the last N hours"""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        # 使用deque的高效遍历
        recent_trials = []
        for trial in reversed(self.trial_data):  # 从最新的开始
            if trial['timestamp'] >= cutoff_time:
                recent_trials.append(trial)
            else:
                break  # 由于数据是按时间顺序的，可以提前退出
                
        return list(reversed(recent_trials))  # 恢复时间顺序
        
    def get_performance_data(self ) -> tuple:
        """Get performance data for recent trials"""
        if len(self._performance_cache) == 0:
            return [], [], [], []
            
        # 使用缓存的性能数据
        recent_data = list(self._performance_cache)[-self.max_memory_trials:]
        x_data = [d['trial_num'] for d in recent_data]
        y_perf = [d['performance'] for d in recent_data]
        y_early = [d.get('early_lick', 0) for d in recent_data]
        y_proto = [d.get('protocol_perf', 0) for d in recent_data]
        
        return x_data, y_perf, y_early, y_proto
        
    def _cleanup_old_indices(self):
        """Clean up expired date indices"""
        cutoff_date = datetime.now().date() - timedelta(days=30)
        
        # 使用字典推导式一次性重建，比删除更高效
        self._trials_by_date = {date: trials for date, trials in self._trials_by_date.items() 
                               if date >= cutoff_date}
        self._daily_trial_cache = {date: count for date, count in self._daily_trial_cache.items() 
                                  if date >= cutoff_date}
            
    def clear_cache(self):
        """Clear all caches"""
        self._daily_trial_cache.clear()
        self._cache_dirty = True
        
    def get_memory_usage(self) -> Dict[str, int]:
        """Get memory usage"""
        return {
            'trial_data_count': len(self.trial_data),
            'trials_by_date_keys': len(self._trials_by_date),
            'daily_cache_keys': len(self._daily_trial_cache),
            'performance_cache_count': len(self._performance_cache)
        }
        
    def cleanup_old_data(self, days_to_keep: int = 30):
        """Remove data older than N days"""
        cutoff_time = datetime.now() - timedelta(days=days_to_keep)
        
        # 清理试验数据
        original_trial_count = len(self.trial_data)
        # 由于deque不支持直接过滤，我们需要重建
        filtered_trials = deque(maxlen=self.max_memory_trials)
        for trial in self.trial_data:
            if trial['timestamp'] >= cutoff_time:
                filtered_trials.append(trial)
        self.trial_data = filtered_trials
        
        # 重建索引
        self._rebuild_indices()
        
        return {
            'trials_removed': original_trial_count - len(self.trial_data)
        }
        
    def _rebuild_indices(self):
        """Rebuild indices"""
        self._trials_by_date.clear()
        self._performance_cache.clear()
        self.clear_cache()
        
        # 重建试验数据索引
        for trial in self.trial_data:
            date_key = trial['timestamp'].date()
            self._trials_by_date[date_key].append(trial)
            
            # 重建性能缓存
            if 'trial_num' in trial and 'performance' in trial:
                self._performance_cache.append({
                    'trial_num': trial['trial_num'],
                    'performance': trial['performance'],
                    'early_lick': trial.get('early_lick_rate', 0),
                    'protocol_perf': trial.get('protocol_perf', 0)
                })
            
    def force_cleanup(self):
        """Force cleanup to free memory"""
        # 清理过期索引
        self._cleanup_old_indices()
        
        # 清理缓存
        self.clear_cache()
        
        # 如果数据量过大，保留最近的数据
        if len(self.trial_data) > self.max_memory_trials * 0.8:
            self.cleanup_old_data(days_to_keep=7)  # 只保留最近7天
            
    def get_all_trial_data(self) -> List[Dict[str, Any]]:
        """Get all trial data"""
        return list(self.trial_data)

    def get_recent_psychometric_trials(
        self, window_size: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """Get the most recent psychometric-capable trials from GUI memory."""
        if window_size is not None and window_size <= 0:
            return []

        recent_trials: List[Dict[str, Any]] = []
        for trial in reversed(self.trial_data):
            protocol = trial.get('protocol')
            outcome = trial.get('outcome')
            rule = trial.get('rule')
            trial_type = trial.get('trial_type')
            stimu_level = normalize_psychometric_level(trial.get('curr_stimu1'))

            if protocol not in PSYCHOMETRIC_PROTOCOLS:
                continue
            if outcome not in (1, 2) or trial_type not in (1, 2):
                continue
            if rule not in PSYCHOMETRIC_VALID_RULES or stimu_level is None:
                continue

            trial_copy = dict(trial)
            trial_copy['curr_stimu1'] = stimu_level
            recent_trials.append(trial_copy)

            if window_size is not None and len(recent_trials) >= window_size:
                break

        recent_trials.reverse()
        return recent_trials

        
    def clear_data(self):
        """Clear all data"""
        self.trial_data.clear()
        self._trials_by_date.clear()
        self._performance_cache.clear()
        self.event_data.clear()
        self.state_data.clear()
        self.mode_switches.clear()
        self.clear_cache()
        
    def load_from_list(self, trial_list: List[Dict[str, Any]]):
        """Load trial data from a list"""
        self.clear_data()
        
        # 重新构建数据和索引
        for trial in trial_list:
            # 确保timestamp是datetime对象
            if isinstance(trial['timestamp'], str):
                trial['timestamp'] = datetime.fromisoformat(trial['timestamp'])
            self.add_trial(trial)


class PsychometricCurveDialog(QDialog):
    """Psychometric / irrelevant-variable curve viewer."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Psychometric Curve")
        self.resize(920, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.summary_label = QLabel(
            "Waiting for psychometric-capable GUI trials from protocols 5-10."
        )
        self.summary_label.setWordWrap(True)
        self.summary_label.setStyleSheet("color: #5F6B7A;")
        layout.addWidget(self.summary_label)

        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#0B0F14')
        self.plot_widget.setLabel('left', 'P(Right Choice) (%)')
        self.plot_widget.setLabel('bottom', 'Frequency (kHz)')
        self.plot_widget.setTitle('Recent Psychometric / Irrelevant Variable Curves')
        self.plot_widget.showGrid(True, True, alpha=0.3)
        self.plot_widget.setYRange(0, 100)
        self.plot_widget.setXRange(2.5, 12.5)
        self.plot_widget.getAxis('bottom').setTicks([PSYCHOMETRIC_FREQ_TICKS])
        self.plot_widget.addLine(
            y=50, pen=pg.mkPen('#FFB300', width=2, style=Qt.PenStyle.DashLine)
        )
        self.plot_widget.addLegend()
        layout.addWidget(self.plot_widget, 1)

        self.rule_curves = {}
        for rule, style in PSYCHOMETRIC_RULE_STYLES.items():
            self.rule_curves[rule] = self.plot_widget.plot(
                [],
                [],
                name=style['label'],
                pen=pg.mkPen(color=style['color'], width=2),
                symbol='o',
                symbolBrush=pg.mkBrush(*style['color'], 210),
                symbolPen=pg.mkPen(*style['color'], 255),
                symbolSize=8,
            )

        self.counts_label = QLabel("")
        self.counts_label.setWordWrap(True)
        self.counts_label.setStyleSheet(
            "color: #5F6B7A; font-family: 'Consolas', 'Menlo', monospace;"
        )
        layout.addWidget(self.counts_label)

    def refresh_plot(self, trials: List[Dict[str, Any]], requested_n: int):
        """Refresh curves using already-filtered GUI trial data."""
        rule_level_choices = {
            rule: {level: [] for level in PSYCHOMETRIC_STIM_LEVELS}
            for rule in PSYCHOMETRIC_VALID_RULES
        }
        protocol_counts = {protocol: 0 for protocol in sorted(PSYCHOMETRIC_PROTOCOLS)}

        for trial in trials:
            protocol = trial.get('protocol')
            rule = trial.get('rule')
            stimu_level = normalize_psychometric_level(trial.get('curr_stimu1'))
            right_choice = infer_right_choice(trial.get('trial_type'), trial.get('outcome'))

            if protocol in protocol_counts:
                protocol_counts[protocol] += 1
            if (
                rule not in rule_level_choices
                or stimu_level not in rule_level_choices[rule]
                or right_choice is None
            ):
                continue

            rule_level_choices[rule][stimu_level].append(right_choice)

        available_count = len(trials)
        protocol_summary = ", ".join(
            f"P{protocol}:{count}" for protocol, count in protocol_counts.items() if count > 0
        ) or "no eligible trials yet"

        self.summary_label.setText(
            f"Recent {available_count}/{requested_n} valid GUI trials from protocols 5-10. "
            f"Rule 0 shows frequency as an irrelevant variable during protocols 5/6. "
            f"Protocol mix: {protocol_summary}."
        )

        counts_lines = []
        x_values = list(PSYCHOMETRIC_FREQ_KHZ)
        any_curve_has_data = False
        for rule in sorted(PSYCHOMETRIC_RULE_STYLES):
            y_values = []
            count_values = []
            for level in PSYCHOMETRIC_STIM_LEVELS:
                right_choices = rule_level_choices[rule][level]
                count_values.append(len(right_choices))
                if right_choices:
                    y_values.append(float(np.mean(right_choices) * 100.0))
                else:
                    y_values.append(np.nan)

            if any(not np.isnan(value) for value in y_values):
                any_curve_has_data = True
                self.rule_curves[rule].setData(x_values, y_values, connect="finite")
            else:
                self.rule_curves[rule].setData([], [])

            counts_lines.append(
                f"{PSYCHOMETRIC_RULE_STYLES[rule]['label']}: {count_values}"
            )

        if any_curve_has_data:
            self.counts_label.setText("\n".join(counts_lines))
        else:
            self.counts_label.setText(
                "No psychometric-capable GUI trials yet. "
                "This plot needs protocols 5-10 with rule and currStimu[1] in the T packet."
            )


class OptimizedHabitsPanel(QWidget):
    """优化的Habits面板"""
    Neural_recorder_command = pyqtSignal(str)
    TrialStarted = pyqtSignal(int) # Signal emitted when trial starts ('C:number' received)
    DataDirectoryChanged = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Habits panel is embedded in a parent GUI panel, keep a smaller baseline size.
        self.setMinimumSize(760, 520)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        
        # 数据存储 - 使用exe所在目录下的Data文件夹
        self.data_folder = get_data_directory("Data")
        self.selected_data_dir = None
            
        # 串口通信
        self.serial_worker = None
        self.serial_connected = False
        self.connected_port_name = None
        self.serial_data_log = []
        self.expecting_tevent_data = False
        self.expecting_trial_state_data = False
        self.remote_view_mode = False
        self._remote_sd_download_handler = None
        self._last_remote_sd_status_text = ""
        self._resume_habits_guard = None
        
        # 当前试验编号跟踪
        self.current_trial_num = 0
        
        # 使用优化的数据管理器
        self.data_manager = TrialDataManager()
        
        # Track active ESA block
        self.current_esa_start_time = None
        self.cap_warning_threshold = 8
        self.cap_alarm_threshold = 12
        self.cap_plot_normal_threshold = 6
        self.cap_plot_alarm_threshold = 12
        self.cap_drift_state = "unknown"
        self.cap_history = deque(maxlen=2000)
        self.cap_trial_regions = []
        
        # SD upload states
        self.sd_upload_active = False
        self.sd_current_file = None
        self.sd_save_dir = None
        self.psychometric_dialog = None
        self.protocol_progress_data = {}
        
        # 定时器
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update_gui)
        self.update_timer.start(600000)  # 10分钟更新一次
        self.serial_health_timer = QTimer()
        self.serial_health_timer.timeout.connect(self._poll_serial_health)
        self.serial_health_timer.start(1000)
        
        # 数据清理定时器 - 每小时清理一次
        # self.cleanup_timer = QTimer()
        # self.cleanup_timer.timeout.connect(self.periodic_cleanup)
        # self.cleanup_timer.start(3600000)  # 1小时清理一次
        
        # 颜色映射
        self.color_map = [
            QColor(255, 0, 0),      # 红色
            QColor(255, 128, 0),    # 橙色
            QColor(255, 255, 0),    # 黄色
            QColor(128, 255, 0),    # 黄绿色
            QColor(0, 255, 0),      # 绿色
            QColor(0, 255, 128),    # 青绿色
            QColor(0, 255, 255),    # 青色
            QColor(0, 128, 255),    # 蓝色
        ]
        
        self.setup_ui()
        self.setup_connections()
        
        
    def setup_ui(self):
        """设置用户界面"""
        main_layout = QHBoxLayout(self)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(6, 6, 6, 6)
        
        # 创建分割器
        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        
        # 左侧控制面板
        self.left_panel = self.create_left_panel()
        self.left_panel.setMinimumWidth(300)
        self.left_panel.setMaximumWidth(420)
        self.left_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.left_scroll = self._wrap_in_scroll_area(self.left_panel)
        
        # 右侧图表和状态面板
        self.right_panel = self.create_right_panel()
        
        self.main_splitter.addWidget(self.left_scroll)
        self.main_splitter.addWidget(self.right_panel)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([360, 940])
        
        main_layout.addWidget(self.main_splitter)

    def _create_compact_value_stack(self, title: str, widget: QWidget) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        title_label = QLabel(title)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("color: #666; font-size: 10px; font-weight: 600;")

        widget.setMinimumWidth(58)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

        layout.addWidget(title_label)
        layout.addWidget(widget)
        return container

    def _create_status_card(self, title: str, value: str, value_style: str = ""):
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: #F7F9FC;
                border: 1px solid #D7DEE8;
                border-radius: 8px;
            }
        """)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(8, 7, 8, 7)
        layout.setSpacing(3)

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("color: #5F6B7A; font-size: 10px; font-weight: 600;")

        value_label = QLabel(value)
        value_label.setWordWrap(True)
        value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        base_style = "font-weight: bold; font-size: 12px;"
        if value_style:
            base_style = f"{base_style} {value_style}"
        value_label.setStyleSheet(base_style)

        layout.addWidget(title_label)
        layout.addWidget(value_label)
        return card, value_label

    def _wrap_in_scroll_area(self, widget: QWidget) -> QScrollArea:
        """Wrap panels so controls remain accessible when embedded in smaller parent regions."""
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(widget)
        return scroll

    def set_remote_view_mode(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.remote_view_mode = enabled
        if hasattr(self, "connection_group"):
            self.connection_group.setVisible(not enabled)
        if hasattr(self, "control_group"):
            self.control_group.setVisible(not enabled)
        if hasattr(self, "main_splitter"):
            if enabled:
                self.main_splitter.setSizes([280, 900])
            else:
                self.main_splitter.setSizes([360, 940])

    def set_remote_sd_download_handler(self, handler) -> None:
        self._remote_sd_download_handler = handler

    def apply_remote_sd_download_state(
        self,
        *,
        connected: bool,
        active: bool,
        status_text: str = "",
        error_text: str = "",
    ) -> None:
        if not self.remote_view_mode:
            return
        can_trigger = bool(connected) and not bool(active)
        self.download_sd_btn.setText("Downloading SD Files..." if active else "Download SD Files")
        self.download_sd_btn.setEnabled(can_trigger)
        self.download_sd_btn.setToolTip(
            str(error_text or status_text or "Download SD files from the Habits device.")
        )
        if active:
            self.sd_progress_bar.setRange(0, 0)
            self.sd_progress_bar.show()
        else:
            self.sd_progress_bar.setRange(0, 100)
            self.sd_progress_bar.setValue(100 if str(status_text or "").strip().lower() == "download complete" else 0)
            self.sd_progress_bar.hide()
        normalized_status = str(status_text or "").strip()
        if normalized_status and normalized_status != self._last_remote_sd_status_text:
            self.add_message(f"SD download: {normalized_status}")
            self._last_remote_sd_status_text = normalized_status
        normalized_error = str(error_text or "").strip()
        if normalized_error and normalized_error != self._last_remote_sd_status_text:
            self.add_message(f"SD download error: {normalized_error}")
            self._last_remote_sd_status_text = normalized_error
        
    def create_left_panel(self) -> QWidget:
        """创建左侧控制面板"""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)  # 与右侧panel保持一致的边距
        
        # 串口连接区域
        connection_group = self.create_connection_group()
        layout.addWidget(connection_group)
        
        # 控制面板
        control_group = self.create_control_group()
        layout.addWidget(control_group)
        
        # 状态信息
        status_group = self.create_status_group()
        layout.addWidget(status_group)

        protocol_progress_group = self.create_protocol_progress_group()
        layout.addWidget(protocol_progress_group)
        
        # 错误消息
        error_group = self.create_error_group()
        layout.addWidget(error_group)
        
        # 参数管理
        param_group = self.create_param_group()
        layout.addWidget(param_group)
        
        layout.addStretch()
        return panel
        
    def create_connection_group(self) -> QGroupBox:
        """创建连接控制组"""
        group = QGroupBox("Connection")
        self.connection_group = group
        layout = QGridLayout(group)
        layout.setSpacing(8)
        layout.setContentsMargins(10, 22, 10, 10)
        
        # 鼠标ID
        layout.addWidget(QLabel("Mouse ID:"), 0, 0)
        self.mouse_id_edit = QLineEdit("Mouse_001")
        self.mouse_id_edit.setReadOnly(True)  # 由目录选择决定
        self.mouse_id_edit.setStyleSheet("background-color: #f0f0f0; color: #333;")
        layout.addWidget(self.mouse_id_edit, 0, 1)
        
        # 目录选择按钮
        self.select_dir_btn = QPushButton("📂")
        self.select_dir_btn.setMaximumWidth(30)
        self.select_dir_btn.setToolTip("Select Data Directory")
        self.select_dir_btn.clicked.connect(self.select_data_directory)
        layout.addWidget(self.select_dir_btn, 0, 2)
        
        # 串口选择
        layout.addWidget(QLabel("Serial Port:"), 1, 0)
        self.serial_combo = QComboBox()
        self.refresh_serial_ports()
        layout.addWidget(self.serial_combo, 1, 1)
        
        self.refresh_btn = QPushButton("🔄")
        self.refresh_btn.setMaximumWidth(30)
        self.refresh_btn.clicked.connect(self.refresh_serial_ports)
        layout.addWidget(self.refresh_btn, 1, 2)
        
        # 连接按钮
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setStyleSheet("""
            QPushButton {
                background-color: #4CAF50;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #45a049;
            }
            QPushButton:pressed {
                background-color: #3d8b40;
            }
        """)
        layout.addWidget(self.connect_btn, 2, 0, 1, 3)
        
        # 开始日期
        layout.addWidget(QLabel("Start Date:"), 3, 0)
        self.start_date_edit = QDateEdit()
        self.start_date_edit.setDate(QDate.currentDate())
        self.start_date_edit.setCalendarPopup(True)
        layout.addWidget(self.start_date_edit, 3, 1, 1, 2)
        
        return group
        
    def create_control_group(self) -> QGroupBox:
        """创建控制面板组"""
        group = QGroupBox("Control Panel")
        self.control_group = group
        layout = QVBoxLayout(group)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 22, 10, 10)
        
        # 奖励设置
        self.reward_left_edit = QSpinBox()
        self.reward_left_edit.setRange(0, 999)
        self.reward_left_edit.setValue(30)
        self.reward_left_edit.setToolTip("Left")
        
        self.reward_middle_edit = QSpinBox()
        self.reward_middle_edit.setRange(0, 999)
        self.reward_middle_edit.setValue(30)
        self.reward_middle_edit.setToolTip("Middle")
        
        self.reward_right_edit = QSpinBox()
        self.reward_right_edit.setRange(0, 999)
        self.reward_right_edit.setValue(30)
        self.reward_right_edit.setToolTip("Right")
        
        self.reward_btn = QPushButton("Set Reward")

        reward_group = QGroupBox("Reward")
        reward_layout = QVBoxLayout(reward_group)
        reward_layout.setContentsMargins(8, 18, 8, 8)
        reward_layout.setSpacing(6)

        reward_inputs_layout = QHBoxLayout()
        reward_inputs_layout.setContentsMargins(0, 0, 0, 0)
        reward_inputs_layout.setSpacing(6)
        reward_inputs_layout.addWidget(self._create_compact_value_stack("Left", self.reward_left_edit))
        reward_inputs_layout.addWidget(self._create_compact_value_stack("Middle", self.reward_middle_edit))
        reward_inputs_layout.addWidget(self._create_compact_value_stack("Right", self.reward_right_edit))
        reward_layout.addLayout(reward_inputs_layout)
        reward_layout.addWidget(self.reward_btn)
        layout.addWidget(reward_group)
        
        # 光强控制
        self.low_light_edit = QSpinBox()
        self.low_light_edit.setRange(0, 255)
        self.low_light_edit.setValue(1)
        
        self.high_light_edit = QSpinBox()
        self.high_light_edit.setRange(0, 255)
        self.high_light_edit.setValue(255)
        
        self.light_btn = QPushButton("Set Light")

        light_group = QGroupBox("Light")
        light_layout = QVBoxLayout(light_group)
        light_layout.setContentsMargins(8, 18, 8, 8)
        light_layout.setSpacing(6)

        light_inputs_layout = QHBoxLayout()
        light_inputs_layout.setContentsMargins(0, 0, 0, 0)
        light_inputs_layout.setSpacing(6)
        light_inputs_layout.addWidget(self._create_compact_value_stack("Low", self.low_light_edit))
        light_inputs_layout.addWidget(self._create_compact_value_stack("High", self.high_light_edit))
        light_layout.addLayout(light_inputs_layout)
        light_layout.addWidget(self.light_btn)
        layout.addWidget(light_group)
        
        # 协议和超时
        self.protocol_edit = QSpinBox()
        self.protocol_edit.setRange(0, 10)
        self.protocol_edit.setPrefix("P")
        self.protocol_edit.setToolTip(
            "Manual firmware protocol commands are limited to P0-P10. "
            "Low-battery training is routed automatically as Mode0 saved to the Mode3 folder."
        )
        self.protocol_btn = QPushButton("Set P0-P10")
        self.block_switch_mode_btn = QPushButton("BlockSwitch Disabled")
        self.block_switch_mode_btn.setToolTip(
            "BlockSwitchMode is no longer used by the DBRuleSwitch protocol flow."
        )
        self.block_switch_mode_btn.setEnabled(False)
        self.block_switch_mode_btn.setVisible(False)
        self.block_switch_mode_btn.setStyleSheet("""
            QPushButton {
                background-color: #8E24AA;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #7B1FA2;
            }
            QPushButton:pressed {
                background-color: #6A1B9A;
            }
        """)
        self.post_training_protocol0_btn = QPushButton("Enter Post-Training P0")
        self.post_training_protocol0_btn.setToolTip(
            "Send G command to reset the firmware into the post-training protocol0 stage"
        )
        self.post_training_protocol0_btn.setStyleSheet("""
            QPushButton {
                background-color: #FB8C00;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #EF6C00;
            }
            QPushButton:pressed {
                background-color: #E65100;
            }
        """)

        protocol_group = QGroupBox("Protocol")
        protocol_layout = QVBoxLayout(protocol_group)
        protocol_layout.setContentsMargins(8, 18, 8, 8)
        protocol_layout.setSpacing(6)
        protocol_top_row = QHBoxLayout()
        protocol_top_row.setContentsMargins(0, 0, 0, 0)
        protocol_top_row.setSpacing(8)
        protocol_top_row.addWidget(self.protocol_edit, 1)
        protocol_top_row.addWidget(self.protocol_btn, 2)
        protocol_layout.addLayout(protocol_top_row)
        protocol_layout.addWidget(self.block_switch_mode_btn)
        protocol_layout.addWidget(self.post_training_protocol0_btn)
        layout.addWidget(protocol_group)

        self.inter_block_interval_edit = QSpinBox()
        self.inter_block_interval_edit.setRange(0, 720)
        self.inter_block_interval_edit.setValue(60)
        self.inter_block_interval_edit.setSuffix(" min")
        self.inter_block_interval_edit.setToolTip(
            "Minimum interval between trialBlocks in BlockswitchMode"
        )
        self.inter_block_interval_btn = QPushButton("Set Block IBI")

        inter_block_group = QGroupBox("Block Interval")
        inter_block_layout = QHBoxLayout(inter_block_group)
        inter_block_layout.setContentsMargins(8, 18, 8, 8)
        inter_block_layout.setSpacing(8)
        inter_block_layout.addWidget(self.inter_block_interval_edit, 1)
        inter_block_layout.addWidget(self.inter_block_interval_btn, 2)
        layout.addWidget(inter_block_group)
        
        # 读取所有值按钮
        self.read_all_btn = QPushButton("Read All Values")
        self.read_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #2196F3;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #1976D2;
            }
            QPushButton:pressed {
                background-color: #1565C0;
            }
        """)

        self.cap_snapshot_btn = QPushButton("Refresh Cap Baseline")
        self.cap_snapshot_btn.setStyleSheet("""
            QPushButton {
                background-color: #7B1FA2;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #6A1B9A;
            }
            QPushButton:pressed {
                background-color: #4A148C;
            }
        """)
        
        # 控制按钮
        self.pause_btn = QPushButton("Pause Habits")
        self.handshake_btn = QPushButton("Handshake")
        
        self.time_btn = QPushButton("Sync Time")
        self.sync_time_label = QLabel("Not synced")
        self.sync_time_label.setWordWrap(True)
        self.sync_time_label.setStyleSheet("font-size: 10px; color: #666;")
        self.sync_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        actions_group = QGroupBox("Actions")
        actions_layout = QGridLayout(actions_group)
        actions_layout.setContentsMargins(8, 18, 8, 8)
        actions_layout.setHorizontalSpacing(8)
        actions_layout.setVerticalSpacing(6)
        actions_layout.setColumnStretch(0, 1)
        actions_layout.setColumnStretch(1, 1)
        actions_layout.addWidget(self.read_all_btn, 0, 0, 1, 2)
        actions_layout.addWidget(self.cap_snapshot_btn, 1, 0, 1, 2)
        actions_layout.addWidget(self.pause_btn, 2, 0)
        actions_layout.addWidget(self.handshake_btn, 2, 1)
        actions_layout.addWidget(self.time_btn, 3, 0, 1, 2)
        actions_layout.addWidget(self.sync_time_label, 4, 0, 1, 2)
        layout.addWidget(actions_group)
        
        return group
        
    def create_status_group(self) -> QGroupBox:
        """创建状态信息组"""
        group = QGroupBox("Status Information")
        layout = QGridLayout(group)
        layout.setHorizontalSpacing(6)
        layout.setVerticalSpacing(6)
        layout.setContentsMargins(10, 22, 10, 10)

        status_cards = [
            ("Trial", "trial_label", "0 - 0.0%", "color: #2196F3;"),
            ("Days", "days_label", "0.0 d", "color: #FF9800;"),
            ("Trials / Day", "trials_day_label", "0/d", "color: #9C27B0;"),
            ("Protocol Trials", "protocol_trials_label", "0", ""),
            ("Trial Type", "trial_type_label", "-", ""),
            ("Outcome", "outcome_label", "-", ""),
            ("Protocol Perf", "protocol_perf_label", "0%", "color: green;"),
            ("Cap Detect", "cap_detect_label", "Disabled", "color: #888;"),
            ("Cap Baseline", "cap_baseline_label", "- / -", "color: #7B1FA2;"),
            ("Cap Filtered", "cap_filtered_label", "- / -", ""),
            ("Cap Delta", "cap_delta_label", "- / -", ""),
            ("Cap Drift", "cap_drift_label", "Unknown", "color: #888;"),
        ]

        for index, (title, attr_name, default_text, value_style) in enumerate(status_cards):
            card, value_label = self._create_status_card(title, default_text, value_style)
            setattr(self, attr_name, value_label)
            layout.addWidget(card, index // 2, index % 2)

        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        
        return group
        
    def create_error_group(self) -> QGroupBox:
        """创建错误消息组"""
        group = QGroupBox("Messages")
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        
        self.error_listbox = QListWidget()
        self.error_listbox.setMinimumHeight(80)
        self.error_listbox.setMaximumHeight(110)
        layout.addWidget(self.error_listbox)
        
        # 控制按钮
        button_layout = QHBoxLayout()
        self.delete_sel_btn = QPushButton("Delete Selected")
        self.clear_all_btn = QPushButton("Clear All")
        
        button_layout.addWidget(self.delete_sel_btn)
        button_layout.addWidget(self.clear_all_btn)
        layout.addLayout(button_layout)
        
        return group

    def create_protocol_progress_group(self) -> QGroupBox:
        """Create protocol-transition progress telemetry group."""
        group = QGroupBox("Protocol Progress")
        layout = QVBoxLayout(group)
        layout.setSpacing(6)
        layout.setContentsMargins(10, 18, 10, 10)

        self.protocol_progress_summary_label = QLabel(
            "Waiting for protocol progress telemetry from DBRuleSwitch."
        )
        self.protocol_progress_summary_label.setWordWrap(True)
        self.protocol_progress_summary_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.protocol_progress_summary_label.setStyleSheet(
            "font-weight: 600; color: #37474F;"
        )
        layout.addWidget(self.protocol_progress_summary_label)

        self.protocol_progress_detail = QTextEdit()
        self.protocol_progress_detail.setReadOnly(True)
        self.protocol_progress_detail.setMinimumHeight(135)
        self.protocol_progress_detail.setMaximumHeight(180)
        self.protocol_progress_detail.setFont(QFont("Consolas", 9))
        self.protocol_progress_detail.setPlainText(
            "Read All or finish a trial to refresh this section."
        )
        layout.addWidget(self.protocol_progress_detail)

        return group
        
    def create_param_group(self) -> QGroupBox:
        """创建参数管理组"""
        from PyQt6.QtWidgets import QProgressBar
        group = QGroupBox("Parameters / SD Data")
        layout = QGridLayout(group)
        layout.setContentsMargins(10, 18, 10, 10)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)
        
        self.save_params_btn = QPushButton("Save Parameters")
        self.load_params_btn = QPushButton("Load Parameters")
        
        self.download_sd_btn = QPushButton("Download SD Files")
        self.download_sd_btn.setStyleSheet("""
            QPushButton {
                background-color: #008CBA;
                color: white;
                border: none;
                padding: 4px;
                border-radius: 4px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #007B9E;
            }
            QPushButton:disabled {
                background-color: #555555;
            }
        """)
        
        self.sd_progress_bar = QProgressBar()
        self.sd_progress_bar.setRange(0, 100)
        self.sd_progress_bar.setValue(0)
        self.sd_progress_bar.hide()
        
        layout.addWidget(self.save_params_btn, 0, 0)
        layout.addWidget(self.load_params_btn, 0, 1)
        layout.addWidget(self.download_sd_btn, 1, 0, 1, 2)
        layout.addWidget(self.sd_progress_bar, 2, 0, 1, 2)
        
        return group
        
    def create_right_panel(self) -> QWidget:
        """创建右侧面板"""
        panel = QWidget()
        main_layout = QVBoxLayout(panel)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(0, 0, 0, 0)
        
        # 创建垂直分割器用于图表区域和Serial data log
        main_splitter = QSplitter(Qt.Orientation.Vertical)
        main_splitter.setChildrenCollapsible(False)
        
        # 上半部分：图表区域
        self.charts_container = QWidget()
        charts_main_layout = QVBoxLayout(self.charts_container)
        charts_main_layout.setSpacing(10)
        charts_main_layout.setContentsMargins(0, 0, 0, 0)
        
        # Top Charts Container (Horizontal)
        top_charts_container = QWidget()
        top_charts_layout = QHBoxLayout(top_charts_container)
        top_charts_layout.setContentsMargins(0, 0, 0, 0)
        top_charts_layout.setSpacing(10)

        # Performance图表
        perf_group = QGroupBox("Performance Chart")
        perf_layout = QVBoxLayout(perf_group)
        perf_layout.setContentsMargins(5, 5, 5, 5)

        perf_toolbar = QHBoxLayout()
        perf_toolbar.setContentsMargins(0, 0, 0, 0)
        perf_toolbar.setSpacing(8)
        perf_toolbar.addStretch(1)
        perf_toolbar.addWidget(QLabel("Recent N"))

        self.psychometric_n_combo = QComboBox()
        self.psychometric_n_combo.addItems(["100", "250", "500", "1000", "2000"])
        self.psychometric_n_combo.setCurrentText("500")
        self.psychometric_n_combo.setMinimumWidth(90)
        perf_toolbar.addWidget(self.psychometric_n_combo)

        self.psychometric_btn = QPushButton("Psychometric Curve")
        perf_toolbar.addWidget(self.psychometric_btn)
        perf_layout.addLayout(perf_toolbar)
        
        self.plot_widget = pg.PlotWidget()
        self.plot_widget.setBackground('#0B0F14')
        self.plot_widget.setLabel('left', 'Performance (%)')
        self.plot_widget.setLabel('bottom', 'Trial Number')
        self.plot_widget.setTitle('Real-time Trial Performance')
        self.plot_widget.showGrid(True, True, alpha=0.3)
        self.plot_widget.setYRange(0, 100)
        
        # 添加性能基准线
        self.plot_widget.addLine(y=50, pen=pg.mkPen('#FFB300', width=2, style=Qt.PenStyle.DashLine))
        self.plot_widget.addLegend()
        
        # 初始化数据曲线
        # Global Performance (Blue)
        self.performance_curve = self.plot_widget.plot(
            [], [], 
            name='Global Perf',
            pen=pg.mkPen(color=(0, 229, 255), width=2),
            symbol='o', 
            symbolBrush=pg.mkBrush(0, 229, 255, 200),
            symbolPen=pg.mkPen(0, 229, 255, 255),
            symbolSize=7
        )

        # Early Lick Rate (Orange)
        self.early_lick_curve = self.plot_widget.plot(
            [], [], 
            name='Early Lick',
            pen=pg.mkPen(color=(255, 128, 0), width=2),
            symbol='t', 
            symbolBrush=pg.mkBrush(255, 128, 0, 200),
            symbolPen=pg.mkPen(255, 128, 0, 255),
            symbolSize=7
        )

        # Protocol Performance (Green)
        self.protocol_perf_curve = self.plot_widget.plot(
            [], [], 
            name='Protocol Perf',
            pen=pg.mkPen(color=(0, 255, 0), width=2),
            symbol='s', 
            symbolBrush=pg.mkBrush(0, 255, 0, 200),
            symbolPen=pg.mkPen(0, 255, 0, 255),
            symbolSize=7
        )
        
        perf_layout.addWidget(self.plot_widget)

        # Event Raster Chart
        event_group = QGroupBox("Event Raster (Last 50 Trials)")
        event_layout = QVBoxLayout(event_group)
        event_layout.setContentsMargins(5, 5, 5, 5)
        
        self.event_plot_widget = pg.PlotWidget()
        self.event_plot_widget.setBackground('#0B0F14')
        self.event_plot_widget.setLabel('left', 'Trial Number')
        self.event_plot_widget.setLabel('bottom', 'Time (ms)')
        self.event_plot_widget.setTitle('Trial Events')
        self.event_plot_widget.showGrid(True, True, alpha=0.3)
        
        # Initialize scatter items
        self.event_scatters = {}
        # 0: Start (Green), 1: End (Red), 8: Lick Left (Cyan), 10: Lick Right (Magenta)
        event_styles = {
            0: {'color': (0, 255, 0), 'symbol': 'o', 'name': 'Start'},
            1: {'color': (255, 0, 0), 'symbol': 'x', 'name': 'End'},
            8: {'color': (0, 255, 255), 'symbol': 't', 'name': 'Lick L'},
            10: {'color': (255, 0, 255), 'symbol': 't', 'name': 'Lick R'}
        }
        
        for eid, style in event_styles.items():
            scatter = pg.ScatterPlotItem(
                size=8, 
                pen=pg.mkPen(None), 
                brush=pg.mkBrush(*style['color']),
                symbol=style['symbol'],
                name=style['name']
            )
            self.event_plot_widget.addItem(scatter)
            self.event_scatters[eid] = scatter
            
        self.event_plot_widget.addLegend()
        event_layout.addWidget(self.event_plot_widget)

        # Add to horizontal layout
        top_charts_layout.addWidget(perf_group, 1)
        top_charts_layout.addWidget(event_group, 1)
        
        bottom_charts_container = QWidget()
        bottom_charts_layout = QVBoxLayout(bottom_charts_container)
        bottom_charts_layout.setContentsMargins(0, 0, 0, 0)
        bottom_charts_layout.setSpacing(10)

        trials_24h_group = QGroupBox("24h Trials Chart")
        trials_24h_layout = QVBoxLayout(trials_24h_group)
        trials_24h_layout.setContentsMargins(5, 5, 5, 5)

        self.trials_24h_widget = pg.PlotWidget()
        self.trials_24h_widget.setBackground('#0B0F14')
        self.trials_24h_widget.setLabel('left', 'Trial Type (0=Left, 1=Right)')
        self.trials_24h_widget.setLabel('bottom', 'Hours Ago')
        self.trials_24h_widget.setTitle('Trials in Last 24 Hours')
        self.trials_24h_widget.showGrid(True, True, alpha=0.3)
        self.trials_24h_widget.setXRange(0, 24)
        self.trials_24h_widget.setYRange(-0.2, 1.2)
        self.plot_24h_trials()
        trials_24h_layout.addWidget(self.trials_24h_widget)

        cap_trial_group = QGroupBox("Cap Delta By Trial")
        cap_trial_layout = QVBoxLayout(cap_trial_group)
        cap_trial_layout.setContentsMargins(5, 5, 5, 5)
        self.cap_trial_plot_widget = pg.PlotWidget()
        self.cap_trial_plot_widget.setBackground('#0B0F14')
        self.cap_trial_plot_widget.setLabel('left', 'Delta')
        self.cap_trial_plot_widget.setLabel('bottom', 'Time (hours ago)')
        self.cap_trial_plot_widget.setTitle('Cap Delta (Last 24 Hours)')
        self.cap_trial_plot_widget.showGrid(True, True, alpha=0.3)
        self.cap_trial_left_scatter = pg.ScatterPlotItem(name='Delta L', symbol='o', size=6)
        self.cap_trial_right_scatter = pg.ScatterPlotItem(name='Delta R', symbol='t', size=7)
        self.cap_trial_plot_widget.addItem(self.cap_trial_left_scatter)
        self.cap_trial_plot_widget.addItem(self.cap_trial_right_scatter)
        self.cap_trial_current_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('red', width=2))
        self.cap_trial_plot_widget.addItem(self.cap_trial_current_line)
        self.cap_trial_plot_widget.setXRange(-24, 0)
        self.cap_trial_plot_widget.getAxis('bottom').setTicks([[(-24, '24'), (-18, '18'), (-12, '12'), (-6, '6'), (0, '0')]])
        self.cap_trial_plot_widget.addLegend()
        cap_trial_layout.addWidget(self.cap_trial_plot_widget)

        bottom_charts_layout.addWidget(trials_24h_group, 1)
        bottom_charts_layout.addWidget(cap_trial_group, 1)

        charts_main_layout.addWidget(top_charts_container, 1)  # 占用 1 份空间
        charts_main_layout.addWidget(bottom_charts_container, 1)  # 占用 1 份空间
        
        # 下半部分：串口数据显示
        self.serial_data_group = QGroupBox("Serial Data Log")
        data_layout = QVBoxLayout(self.serial_data_group)
        data_layout.setContentsMargins(5, 5, 5, 5)
        
        self.serial_data_display = QTextEdit()
        self.serial_data_display.setReadOnly(True)
        self.serial_data_display.setFont(QFont("Consolas", 9))
        # 设置最小高度而不是最大高度，让它可以伸缩
        self.serial_data_display.setMinimumHeight(100)
        data_layout.addWidget(self.serial_data_display)
        
        # 清除按钮
        clear_btn = QPushButton("Clear Serial Data")
        clear_btn.clicked.connect(self.clear_serial_data)
        data_layout.addWidget(clear_btn)
        
        # 将图表容器和数据日志添加到主分割器
        main_splitter.addWidget(self.charts_container)
        main_splitter.addWidget(self.serial_data_group)
        
        # 设置分割器比例：图表区域占大部分空间，Serial data log占较小空间
        main_splitter.setStretchFactor(0, 4)  # 图表区域占4/5 (增加高度)
        main_splitter.setStretchFactor(1, 1)  # Serial data log占1/5
        main_splitter.setSizes([760, 180])
        
        main_layout.addWidget(main_splitter)
        
        return panel
        
    def setup_connections(self):
        """设置信号连接"""
        # 连接按钮
        self.connect_btn.clicked.connect(self.toggle_serial_connection)
        
        # 控制面板按钮
        self.reward_btn.clicked.connect(self.send_reward_command)
        self.light_btn.clicked.connect(self.send_light_command)
        self.protocol_btn.clicked.connect(self.send_protocol_command)
        self.block_switch_mode_btn.clicked.connect(
            self.send_block_switch_mode_command
        )
        self.psychometric_btn.clicked.connect(self.show_psychometric_dialog)
        self.psychometric_n_combo.currentTextChanged.connect(
            self.on_psychometric_window_size_changed
        )
        self.post_training_protocol0_btn.clicked.connect(
            self.send_post_training_protocol0_command
        )
        self.inter_block_interval_btn.clicked.connect(self.send_inter_block_interval_command)
        self.read_all_btn.clicked.connect(self.send_read_all_command)
        self.cap_snapshot_btn.clicked.connect(self.send_cap_snapshot_command)
        self.pause_btn.clicked.connect(self.send_pause_command)
        self.handshake_btn.clicked.connect(self.send_handshake_command)
        self.time_btn.clicked.connect(self.send_time_command)
        
        # 错误消息按钮
        self.delete_sel_btn.clicked.connect(self.delete_selected_error)
        self.clear_all_btn.clicked.connect(self.clear_all_errors)
        
        # 参数管理按钮
        self.save_params_btn.clicked.connect(self.save_parameters)
        self.load_params_btn.clicked.connect(self.load_parameters)
        self.download_sd_btn.clicked.connect(self.trigger_sd_download)
        
    def select_data_directory(self):
        """选择数据保存目录"""
        directory = QFileDialog.getExistingDirectory(self, "Select Data Directory", self.data_folder)
        if directory:
            self.set_data_directory(directory)

    def set_data_directory(self, directory):
        """Set data directory programmatically"""
        if os.path.exists(directory):
            self.selected_data_dir = directory
            # 从目录名提取Mouse ID
            mouse_id = os.path.basename(directory)
            if not mouse_id: # 处理根目录情况
                mouse_id = os.path.basename(os.path.dirname(directory))
            
            self.mouse_id_edit.setText(mouse_id)
            self.add_message(f"Data directory set to: {directory}")
            self.add_message(f"Mouse ID updated to: {mouse_id}")
            self.DataDirectoryChanged.emit(directory)
            
            # 自动加载新目录下的参数
            self.load_parameters()
        else:
            self.add_message(f"Error: Directory does not exist: {directory}")

    def refresh_serial_ports(self):
        """刷新串口列表"""
        self.serial_combo.clear()
        ports = serial.tools.list_ports.comports()
        for port in ports:
            self.serial_combo.addItem(f"{port.device} - {port.description}")

    def _is_port_still_available(self, port_name: Optional[str]) -> bool:
        if not port_name:
            return False
        try:
            return any(port.device == port_name for port in serial.tools.list_ports.comports())
        except Exception:
            return True

    def _set_serial_ui_connected(self, connected: bool):
        self.serial_connected = bool(connected)
        if connected:
            self.connect_btn.setText("Disconnect")
            self.connect_btn.setStyleSheet("""
                QPushButton {
                    background-color: #f44336;
                    color: white;
                    border: none;
                    padding: 8px;
                    border-radius: 4px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #da190b;
                }
            """)
            self.mouse_id_edit.setEnabled(False)
            self.serial_combo.setEnabled(False)
            self.refresh_btn.setEnabled(False)
        else:
            self.connect_btn.setText("Connect")
            self.connect_btn.setStyleSheet("""
                QPushButton {
                    background-color: #4CAF50;
                    color: white;
                    border: none;
                    padding: 8px;
                    border-radius: 4px;
                    font-weight: bold;
                }
                QPushButton:hover {
                    background-color: #45a049;
                }
            """)
            self.mouse_id_edit.setEnabled(True)
            self.serial_combo.setEnabled(True)
            self.refresh_btn.setEnabled(True)

    def _cleanup_serial_worker(self):
        worker = self.serial_worker
        self.serial_worker = None
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass

    def _handle_serial_disconnected(self, reason: str, show_dialog: bool = False):
        was_connected = self.serial_connected or self.serial_worker is not None
        self._cleanup_serial_worker()
        self.connected_port_name = None
        self._set_serial_ui_connected(False)
        if not was_connected:
            return
        self.add_message(f"Serial disconnected: {reason}")
        if show_dialog:
            QMessageBox.warning(self, "Serial Disconnected", reason)

    def _on_serial_connected(self, port: str):
        self.connected_port_name = port
        self._set_serial_ui_connected(True)
        self.load_parameters()
        time.sleep(0.2)
        self.send_time_command()
        self.add_message(f"Serial connection established successfully: {port}")
        self.plot_24h_trials()

    def _poll_serial_health(self):
        if not self.serial_connected or self.serial_worker is None:
            return
        if not self.serial_worker.isRunning():
            self._handle_serial_disconnected("Habits serial worker stopped unexpectedly")
            return
        if not self._is_port_still_available(self.connected_port_name):
            self._handle_serial_disconnected(f"{self.connected_port_name} is no longer present")
            
    def toggle_serial_connection(self):
        """切换串口连接"""
        if not self.serial_connected:
            # 连接串口
            port_text = self.serial_combo.currentText()
            if not port_text:
                QMessageBox.warning(self, "Warning", "Please select a serial port!")
                return
                
            port = port_text.split(" - ")[0]
            
            try:
                self.serial_worker = SerialWorker(port)
                self.serial_worker.connected.connect(self._on_serial_connected)
                self.serial_worker.data_received.connect(self.handle_serial_data)
                self.serial_worker.error_occurred.connect(self.handle_serial_error)
                self.serial_worker.disconnected.connect(self._handle_serial_disconnected)
                self.serial_worker.start()
                self.connected_port_name = port
                self._set_serial_ui_connected(True)
                
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to open serial port: {str(e)}")
        else:
            # 断开串口
            self._cleanup_serial_worker()
            self.connected_port_name = None
            self._set_serial_ui_connected(False)
            self.save_parameters()
            self.add_message("Serial connection closed")
            
    def handle_serial_data(self, data: str):
        """处理接收到的串口数据"""
        
        # Check for SD file upload packets
        if data.startswith("SD_"):
            if data == "SD_UPLOAD_START":
                self.sd_upload_active = True
                self.add_message("SD file upload started.")
                return
            elif data.startswith("SD_FILE_START:"):
                self.sd_upload_active = True
                parts = data[14:].split(',')
                filename = parts[0].strip()
                if self.sd_save_dir:
                    filepath = os.path.join(self.sd_save_dir, filename)
                    try:
                        self.sd_current_file = open(filepath, 'w', encoding='utf-8')
                        self.add_message(f"Downloading {filename}...")
                    except Exception as e:
                        self.add_message(f"Error creating file {filename}: {str(e)}")
                return
            elif data.startswith("SD_DATA:"):
                self.sd_upload_active = True
                if self.sd_current_file:
                    self.sd_current_file.write(data[8:] + '\n')
                return
            elif data.startswith("SD_FILE_END:"):
                self.sd_upload_active = True
                if self.sd_current_file:
                    self.sd_current_file.close()
                    self.sd_current_file = None
                return
            elif data == "SD_UPLOAD_END":
                self.sd_upload_active = False
                if self.sd_current_file:
                    self.sd_current_file.close()
                    self.sd_current_file = None
                self.sd_progress_bar.setValue(100)
                self.download_sd_btn.setEnabled(True)
                self.add_message("SD file upload completed.")
                QMessageBox.information(self, "Success", "All files downloaded successfully.")
                self.sd_progress_bar.hide()
                return

        # Check for Trial Start signal 'C:number'
        if data.strip().startswith('C:'):
            try:
                trial_num_str = data.strip()[2:]
                trial_num = int(trial_num_str)
                self.TrialStarted.emit(trial_num)
                
                timestamp = datetime.now().strftime("%H:%M:%S")
                self.serial_data_display.append(f"[{timestamp}] Trial Start (C:{trial_num})")
            except ValueError:
                # Log parsing error if number is invalid
                pass
            return
        
        # Check for Tevent header
        if data.strip().startswith("Tevent:"):
            self.parse_tevent_data(data)
            return

        # Check for Trial: header (Space separated)
        if data.strip().startswith("Trial:"): 
            self.parse_trial_state_data(data)
            return

        ## Trial mode switch signal
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {data}"
        # 用于处理时间对齐事件，用来自动化switch 在mode0 和mode3 之间
        if data.startswith("ModeSwitch:"):
            # self.save_time_align_data()
            cmd = data[11:].strip()
            
            # Record ESA blocks (Mode 3 intervals)
            if cmd == 'ESA': # ESA Start
                self.current_esa_start_time = datetime.now()
                # Refresh chart to show the new active block immediately
                self.plot_24h_trials()
            elif cmd == 'LFP': # ESA End (Back to LFP)
                if self.current_esa_start_time:
                    end_time = datetime.now()
                    self.data_manager.add_mode_switch({
                        'start': self.current_esa_start_time,
                        'end': end_time
                    })
                    self.current_esa_start_time = None
                    # Refresh chart to show the new block
                    self.plot_24h_trials()

            self.Neural_recorder_command.emit(cmd) # 2 -> mode3; 1-> mode0
        
        # 添加到日志
        self.serial_data_log.append(log_entry)
        self.serial_data_display.append(log_entry)
        
        # 保持日志大小
        if len(self.serial_data_log) > 1000:
            self.serial_data_log = self.serial_data_log[-500:]
            self.serial_data_display.clear()
            for entry in self.serial_data_log[-100:]:
                self.serial_data_display.append(entry)
                
        # 保存原始数据到文件
        # self.save_data_to_file(data)
        # 解析数据
        self.parse_received_data(data)
        
    def parse_received_data(self, data: str):
        """解析接收到的数据, 由于状态机结束的时候为上一个trial"""
        if not data:
            return
            
        try:
            if data.startswith('T'):  # 试验数据
                parts = data[1:].split(',')
                if len(parts) >= 8:
                    try:
                        def parse_optional_value(index, parser):
                            if index >= len(parts):
                                return None
                            value = parts[index].strip()
                            if not value:
                                return None
                            try:
                                return parser(value)
                            except ValueError:
                                return None

                        trial_num = int(parts[0])
                        trial_type = int(parts[1])
                        protocol_index = int(parts[2])
                        outcome = int(parts[3])
                        early_lick_rate = float(parts[4])
                        perf_100 = float(parts[5])
                        protocol_trials = int(parts[6])
                        protocol_perf = float(parts[7])
                        rule = parse_optional_value(8, int)
                        curr_stimu0 = parse_optional_value(9, float)
                        curr_stimu1 = parse_optional_value(10, float)
                        
                        # Update Protocol UI without clobbering an in-progress user edit.
                        self._set_spinbox_value_if_idle(self.protocol_edit, protocol_index)

                        # Update Status Labels
                        self.current_trial_num = trial_num + 1
                        self.trial_label.setText(f"{trial_num} - {perf_100:.1f}%")
                        self.protocol_trials_label.setText(str(protocol_trials))
                        
                        type_str = "Left" if trial_type == 1 else "Right" if trial_type == 2 else "Middle" if trial_type == 3 else "Unknown"
                        self.trial_type_label.setText(f"{trial_type} ({type_str})")
                        
                        outcome_str = "No Resp" if outcome == 0 else "Correct" if outcome == 1 else "Error" if outcome == 2 else "Early Lick" if outcome == 3 else "Other"
                        self.outcome_label.setText(f"{outcome} ({outcome_str})")
                        
                        self.protocol_perf_label.setText(f"{protocol_perf:.1f}%")
                        
                        # 保存试验数据
                        trial_data = {
                            'timestamp': datetime.now(),
                            'trial_num': trial_num,
                            'trial_type': trial_type,
                            'protocol': protocol_index,
                            'outcome': outcome,
                            'early_lick_rate': early_lick_rate,
                            'performance': perf_100,
                            'protocol_trials': protocol_trials,
                            'protocol_perf': protocol_perf
                        }
                        if rule is not None:
                            trial_data['rule'] = rule
                        if curr_stimu0 is not None:
                            trial_data['curr_stimu0'] = curr_stimu0
                        if curr_stimu1 is not None:
                            trial_data['curr_stimu1'] = curr_stimu1
                        self.data_manager.add_trial(trial_data)
                        
                        # 更新试验次数/天
                        current_trials = self.data_manager.get_daily_trial_count()
                        self.trials_day_label.setText(f"{current_trials}/d")
                        
                        # 更新图表
                        self.update_performance_chart()
                        # 更新24小时试验图表
                        self.plot_24h_trials()
                        self.refresh_psychometric_dialog()
                    except ValueError as e:
                        self.add_message(f"Error parsing T data: {e}")
                    

            elif data.startswith('A'):  # 读取所有值的返回数据
                # 数据格式: A30;30;30;1;255;0;20.75;1761175304;0;3600000;
                # 分别为: reward_left, reward_right, reward_middle, low_light_intensity, 
                #        high_light_intensity, currProtocolIndex, random_value, timestamp,
                #        extra_TimeOut, inter_block_interval_ms
                data_part = data[1:]  # 去掉开头的'A'
                if data_part.endswith(';'):
                    data_part = data_part[:-1]  # 去掉结尾的';'
                
                parts = data_part.split(';')
                if len(parts) >= 9:
                    try:
                        reward_left = int(parts[0])
                        reward_right = int(parts[1])
                        reward_middle = int(parts[2])
                        low_light_intensity = int(parts[3])
                        high_light_intensity = int(parts[4])
                        curr_protocol_index = int(parts[5])
                        random_value = float(parts[6])
                        teensy_timestamp = int(parts[7])
                        extra_timeout = int(parts[8])
                        inter_block_interval_ms = 3600000
                        if len(parts) >= 10 and parts[9] != '':
                            inter_block_interval_ms = int(parts[9])
                        
                        # 更新GUI控件的值
                        self.update_gui_values(reward_left, reward_right, reward_middle, 
                                             low_light_intensity, high_light_intensity, 
                                             curr_protocol_index, teensy_timestamp,
                                             inter_block_interval_ms)
                        
                        # 添加成功消息
                        self.add_message(f"All values updated: R({reward_left},{reward_right},{reward_middle}) "
                                       f"L({low_light_intensity},{high_light_intensity}) P({curr_protocol_index}) "
                                       f"IBI({inter_block_interval_ms} ms)")
                        
                    except (ValueError, IndexError) as e:
                        self.add_message(f"Failed to parse A response: {str(e)}")
                else:
                    self.add_message(f"Invalid A response format: expected at least 9 values, got {len(parts)}")
            elif data.startswith('PG,'):
                self.update_protocol_progress_display(data)
                    
            elif data.startswith('E'):  # 错误消息
                error_msg = data[2:] if len(data) > 2 else "Unknown error"
                self.add_message(f"Error: {error_msg}")
            elif data.startswith('CD,'):
                self.update_cap_detect_status(data)
            elif data.startswith('CB,'):
                self.update_cap_status(data)
            elif data.startswith('SC1'):
                self.add_message("Cap baseline refreshed and locked successfully")
            elif data.startswith('SC0'):
                self.add_message("Cap baseline refresh failed")
            elif data.startswith('SF1'):
                self.add_message("Firmware accepted RF-off confirmation (SoftEvent1 queued)")
                
        except Exception as e:
            self.add_message(f"Data parsing error: {str(e)}")

    def update_cap_status(self, data: str):
        """更新MPR121基线与漂移状态"""
        try:
            parts = data.split(',')
            if len(parts) < 7:
                return

            trial_num = None
            baseline_fallback_flag = 0
            if len(parts) >= 10:
                # Legacy extended format with detect flag plus optional extra fields:
                # CB,trial,detect,baselineL,baselineR,filteredL,filteredR,deltaL,deltaR,...
                trial_num = int(parts[1])
                detect_enabled = int(parts[2])
                self.cap_detect_label.setText("Enabled" if detect_enabled else "Disabled")
                self.cap_detect_label.setStyleSheet(
                    "font-weight: bold; color: #2E7D32;" if detect_enabled else "font-weight: bold; color: #888;"
                )
                baseline_left = int(parts[3])
                baseline_right = int(parts[4])
                filtered_left = int(parts[5])
                filtered_right = int(parts[6])
                delta_left = int(parts[7])
                delta_right = int(parts[8])
                if parts[9] != "":
                    baseline_fallback_flag = int(parts[9])
            elif len(parts) == 9:
                # Ambiguous length:
                # 1) Legacy: CB,trial,detect,baselineL,baselineR,filteredL,filteredR,deltaL,deltaR
                # 2) New FW : CB,trial,baselineL,baselineR,filteredL,filteredR,deltaL,deltaR,fallbackFlag
                detect_like = parts[2] in ("0", "1")
                fallback_like = parts[8] in ("0", "1")
                if detect_like and not fallback_like:
                    trial_num = int(parts[1])
                    detect_enabled = int(parts[2])
                    self.cap_detect_label.setText("Enabled" if detect_enabled else "Disabled")
                    self.cap_detect_label.setStyleSheet(
                        "font-weight: bold; color: #2E7D32;" if detect_enabled else "font-weight: bold; color: #888;"
                    )
                    baseline_left = int(parts[3])
                    baseline_right = int(parts[4])
                    filtered_left = int(parts[5])
                    filtered_right = int(parts[6])
                    delta_left = int(parts[7])
                    delta_right = int(parts[8])
                else:
                    # Default to new firmware format for 9-field packets.
                    trial_num = int(parts[1])
                    baseline_left = int(parts[2])
                    baseline_right = int(parts[3])
                    filtered_left = int(parts[4])
                    filtered_right = int(parts[5])
                    delta_left = int(parts[6])
                    delta_right = int(parts[7])
                    baseline_fallback_flag = int(parts[8]) if parts[8] != "" else 0
            elif len(parts) >= 8:
                trial_num = int(parts[1])
                baseline_left = int(parts[2])
                baseline_right = int(parts[3])
                filtered_left = int(parts[4])
                filtered_right = int(parts[5])
                delta_left = int(parts[6])
                delta_right = int(parts[7])
            else:
                baseline_left = int(parts[1])
                baseline_right = int(parts[2])
                filtered_left = int(parts[3])
                filtered_right = int(parts[4])
                delta_left = int(parts[5])
                delta_right = int(parts[6])
            self.cap_baseline_label.setText(f"{baseline_left} / {baseline_right}")
            self.cap_filtered_label.setText(f"{filtered_left} / {filtered_right}")
            self.cap_delta_label.setText(f"{delta_left:+d} / {delta_right:+d}")
            if baseline_fallback_flag:
                self.add_message("Cap baseline read fallback used")

            max_delta = max(abs(delta_left), abs(delta_right))
            if max_delta >= self.cap_alarm_threshold:
                drift_state = "alarm"
                delta_color = "#C62828"
                drift_text = f"Alarm (Δ={max_delta})"
            elif max_delta >= self.cap_warning_threshold:
                drift_state = "warning"
                delta_color = "#EF6C00"
                drift_text = f"Warning (Δ={max_delta})"
            else:
                drift_state = "normal"
                delta_color = "#2E7D32"
                drift_text = f"Normal (Δ={max_delta})"

            self.cap_delta_label.setStyleSheet(f"font-weight: bold; color: {delta_color};")
            self.cap_drift_label.setText(drift_text)
            self.cap_drift_label.setStyleSheet(f"font-weight: bold; color: {delta_color};")

            if trial_num is not None:
                self.cap_history.append({
                    'trial_num': trial_num,
                    'baseline_left': baseline_left,
                    'baseline_right': baseline_right,
                    'filtered_left': filtered_left,
                    'filtered_right': filtered_right,
                    'delta_left': delta_left,
                    'delta_right': delta_right,
                    'detect_enabled': self.cap_detect_label.text() == "Enabled",
                    'timestamp': datetime.now().isoformat()
                })
                self.update_cap_trial_chart()

            if drift_state != self.cap_drift_state:
                if drift_state == "alarm":
                    self.add_message(
                        f"Cap drift alarm: delta reached {max_delta} (L {delta_left:+d}, R {delta_right:+d})"
                    )
                elif drift_state == "warning":
                    self.add_message(
                        f"Cap drift warning: delta reached {max_delta} (L {delta_left:+d}, R {delta_right:+d})"
                    )
                elif self.cap_drift_state in {"warning", "alarm"}:
                    self.add_message("Cap drift returned to normal range")
                self.cap_drift_state = drift_state
        except (ValueError, IndexError):
            self.add_message(f"Failed to parse cap telemetry: {data}")

    def update_cap_trial_chart(self):
        now = datetime.now()
        self._update_cap_plot_regions(now)
        if len(self.cap_history) == 0:
            self.cap_trial_left_scatter.setData([])
            self.cap_trial_right_scatter.setData([])
            self.cap_trial_plot_widget.setXRange(-24, 0)
            self.cap_trial_plot_widget.getAxis('bottom').setTicks([[(-24, '24'), (-18, '18'), (-12, '12'), (-6, '6'), (0, '0')]])
            return
        left_spots = []
        right_spots = []
        y_min = None
        y_max = None
        for item in self.cap_history:
            ts_raw = item.get('timestamp')
            try:
                ts = datetime.fromisoformat(ts_raw) if isinstance(ts_raw, str) else None
            except ValueError:
                ts = None
            if ts is None:
                continue
            hours_ago = (now - ts).total_seconds() / 3600.0
            if hours_ago < 0 or hours_ago > 24:
                continue
            x = -hours_ago
            yl = item['delta_left']
            yr = item['delta_right']
            y_min = yl if y_min is None else min(y_min, yl, yr)
            y_max = yl if y_max is None else max(y_max, yl, yr)
            if abs(yl) < self.cap_plot_normal_threshold:
                left_color = (67, 160, 71)
            elif abs(yl) <= self.cap_plot_alarm_threshold:
                left_color = (251, 192, 45)
            else:
                left_color = (229, 57, 53)

            if abs(yr) < self.cap_plot_normal_threshold:
                right_color = (67, 160, 71)
            elif abs(yr) <= self.cap_plot_alarm_threshold:
                right_color = (251, 192, 45)
            else:
                right_color = (229, 57, 53)

            left_spots.append({
                'pos': (x, yl),
                'brush': pg.mkBrush(*left_color),
                'pen': pg.mkPen(*left_color),
                'data': 1
            })
            right_spots.append({
                'pos': (x, yr),
                'brush': pg.mkBrush(*right_color),
                'pen': pg.mkPen(*right_color),
                'data': 1
            })

        self.cap_trial_left_scatter.setData(left_spots)
        self.cap_trial_right_scatter.setData(right_spots)
        self.cap_trial_plot_widget.setXRange(-24, 0)
        self.cap_trial_plot_widget.getAxis('bottom').setTicks([[(-24, '24'), (-18, '18'), (-12, '12'), (-6, '6'), (0, '0')]])
        if y_min is None or y_max is None:
            self.cap_trial_plot_widget.setYRange(-15, 15)
        else:
            pad = max(2, int((y_max - y_min) * 0.2))
            self.cap_trial_plot_widget.setYRange(y_min - pad, y_max + pad)

    def _update_cap_plot_regions(self, now: datetime):
        if self.cap_trial_regions:
            for region in self.cap_trial_regions:
                self.cap_trial_plot_widget.removeItem(region)
            self.cap_trial_regions = []
        active_intervals = self.data_manager.get_recent_mode_switches(24)
        if self.current_esa_start_time:
            active_intervals.append({
                'start': self.current_esa_start_time,
                'end': now
            })
        for switch in active_intervals:
            t_end = (now - switch['end']).total_seconds() / 3600
            t_start = (now - switch['start']).total_seconds() / 3600
            x_start = -t_start
            x_end = -t_end
            if x_end < -24:
                continue
            if x_start > 0:
                x_start = 0
            region = pg.LinearRegionItem(
                values=[x_start, x_end],
                brush=pg.mkBrush(0, 150, 136, 100),
                pen=None,
                movable=False
            )
            region.setZValue(-100)
            self.cap_trial_plot_widget.addItem(region)
            self.cap_trial_regions.append(region)

    def update_cap_detect_status(self, data: str):
        try:
            parts = data.split(',')
            if len(parts) < 2:
                return
            detect_enabled = int(parts[1])
            self.cap_detect_label.setText("Enabled" if detect_enabled else "Disabled")
            self.cap_detect_label.setStyleSheet(
                "font-weight: bold; color: #2E7D32;" if detect_enabled else "font-weight: bold; color: #888;"
            )
        except (ValueError, IndexError):
            self.add_message(f"Failed to parse cap detect state: {data}")
            
    def parse_tevent_data(self, data: str):
        """Parse Tevent data line"""
        try:
            parts = data.strip().split()
            if len(parts) < 2:
                return
                
            trial_num = int(parts[0][7:])
            n_events = int(parts[1])
            
            events = []
            idx = 2
            # Each event has 2 values: ID and Timestamp
            
            while idx + 1 < len(parts) and len(events) < n_events:
                eid = int(parts[idx])
                timestamp = int(parts[idx+1])
                events.append({'id': eid, 'time': timestamp})
                idx += 2
            self.data_manager.add_event_data(trial_num, events)
            self.update_event_chart()
            
        except ValueError as e:
            print(f"Error parsing Tevent: {e}")
            pass

    def parse_trial_state_data(self, data: str):
        """Parse Trial State data line
        Format: Trial: <trialNum> <outcome> <nVisited> <state> <time> ...
        """
        try:
            # Remove "Trial:" prefix
            content = data[6:].strip()
            parts = content.split()
            if len(parts) < 3:
                return
                
            trial_num = int(parts[0])
            outcome = int(parts[1])
            n_visited = int(parts[2])
            
            states = []
            idx = 3
            # Each state has 2 values: ID and Timestamp
            
            while idx + 1 < len(parts) and len(states) < n_visited:
                state_id = int(parts[idx])
                timestamp = int(parts[idx+1])
                states.append({'id': state_id, 'time': timestamp})
                idx += 2
                
            self.data_manager.add_state_data(trial_num, outcome, states)
            self.update_event_chart()
            
        except ValueError as e:
            # self.add_message(f"Error parsing Trial State: {e}")
            pass

    def update_event_chart(self):
        """Update event raster chart (Events Only)"""
        # Clear existing items except legend
        self.event_plot_widget.clear()
        self.event_plot_widget.addLegend()
        
        # Re-add scatter items
        # 0: Start (Green), 1: End (Red), 8: Lick Left (Cyan), 10: Lick Right (Magenta)
        
        event_styles = {
            0: {'color': (0, 255, 0), 'symbol': 'o', 'name': 'Start', 'size': 8},
            1: {'color': (255, 0, 0), 'symbol': 'x', 'name': 'End', 'size': 8},
            8: {'color': (0, 255, 255), 'symbol': 't', 'name': 'Lick L', 'size': 8},
            10: {'color': (255, 0, 255), 'symbol': 't', 'name': 'Lick R', 'size': 8}
        }
        
        event_data = self.data_manager.get_event_data()
        
        # Prepare data for scatter plots
        scatter_points = defaultdict(list) # Key: ID (event)
        
        min_trial = float('inf')
        max_trial = float('-inf')
        
        for trial in event_data:
            t_num = trial['trial_num']
            min_trial = min(min_trial, t_num)
            max_trial = max(max_trial, t_num)
            
            # Events
            if 'events' in trial:
                for event in trial['events']:
                    eid = event['id']
                    if eid in event_styles:
                        scatter_points[eid].append({
                            'pos': (event['time'], t_num),
                            'data': 1,
                            'brush': pg.mkBrush(*event_styles[eid]['color']),
                            'pen': pg.mkPen(None),
                            'size': event_styles[eid]['size'],
                            'symbol': event_styles[eid]['symbol']
                        })

        # Draw Scatter Items
        for eid, style in event_styles.items():
            points = scatter_points.get(eid, [])
            if points:
                # Extract data for setData
                spots = [{'pos': p['pos'], 'data': 1, 'brush': p['brush'], 'pen': p['pen'], 'size': p['size'], 'symbol': p['symbol']} for p in points]
                scatter = pg.ScatterPlotItem(name=style['name'])
                scatter.addPoints(spots)
                # scatter.setName(style['name']) # Removed due to AttributeError
                self.event_plot_widget.addItem(scatter)
                
        # Update Y range to follow trials
        if min_trial != float('inf'):
            self.event_plot_widget.setYRange(max(0, max_trial - 50), max_trial + 2)

    def update_performance_chart(self):
        """更新性能图表"""
        # 使用数据管理器的高效方法获取性能数据
        x_data, y_perf, y_early, y_proto = self.data_manager.get_performance_data()
        
        if not x_data:
            return

        # 处理回环连线问题：检测 trial_num 减小的地方，插入 NaN 断开连线
        x_plot = []
        y_perf_plot = []
        y_early_plot = []
        y_proto_plot = []
        
        for i in range(len(x_data)):
            if i > 0 and x_data[i] < x_data[i-1]:
                # 插入断点
                x_plot.append(x_data[i]) 
                y_perf_plot.append(np.nan)
                y_early_plot.append(np.nan)
                y_proto_plot.append(np.nan)
            
            x_plot.append(x_data[i])
            y_perf_plot.append(y_perf[i])
            y_early_plot.append(y_early[i])
            y_proto_plot.append(y_proto[i])
        
        # 更新曲线，使用 connect="finite" 来处理 NaN 断点
        self.performance_curve.setData(x_plot, y_perf_plot, connect="finite")
        self.early_lick_curve.setData(x_plot, y_early_plot, connect="finite")
        self.protocol_perf_curve.setData(x_plot, y_proto_plot, connect="finite")
        
        # 自动调整X轴范围
        if len(x_plot) > 0:
            # 过滤掉NaN用于计算范围
            valid_x = [x for x in x_plot if not np.isnan(x)]
            if valid_x:
                self.plot_widget.setXRange(max(0, min(valid_x) - 5), max(valid_x) + 5)
            
    def handle_serial_error(self, error: str):
        """处理串口错误"""
        self.add_message(f"Serial error: {error}")
        if self.serial_connected:
            self._handle_serial_disconnected(error, show_dialog=False)
        else:
            QMessageBox.critical(self, "Serial Error", error)
        
    def send_serial_command(self, command: str) -> bool:
        """发送串口命令"""
        if self.serial_worker and self.serial_connected:
            success = self.serial_worker.send_data(command)
            if not success:
                self.add_message(f"Failed to send command: {command}")
            return success
        else:
            self.add_message("Serial port not connected")
            return False

    def set_resume_habits_guard(self, callback):
        """Set an optional guard that must pass before sending Resume Habits."""
        self._resume_habits_guard = callback if callable(callback) else None

    def _ensure_resume_habits_ready(self) -> bool:
        guard = getattr(self, "_resume_habits_guard", None)
        if guard is None:
            self.add_message("Resume Habits blocked: RF-off confirmation hook is not configured")
            return False
        try:
            ready = bool(guard())
        except Exception as exc:
            self.add_message(f"Resume Habits blocked: RF-off confirmation failed ({exc})")
            return False
        if not ready:
            self.add_message("Resume Habits blocked: RF power is not confirmed OFF")
        return ready

    @staticmethod
    def _set_spinbox_value_if_idle(widget, value: int) -> None:
        if widget is None:
            return
        try:
            if widget.hasFocus():
                return
        except Exception:
            pass
        try:
            line_edit = widget.lineEdit()
            if line_edit is not None and line_edit.hasFocus():
                return
        except Exception:
            pass
        try:
            normalized = int(value)
        except Exception:
            return
        if int(widget.value()) == normalized:
            return
        widget.blockSignals(True)
        widget.setValue(normalized)
        widget.blockSignals(False)
            
    def send_reward_command(self):
        """发送奖励命令"""
        left = self.reward_left_edit.value()
        middle = self.reward_middle_edit.value()
        right = self.reward_right_edit.value()
        command = f"R{left},{right},{middle},"
        self.send_serial_command(command)
        
    def send_light_command(self):
        """发送光强命令"""
        low = self.low_light_edit.value()
        high = self.high_light_edit.value()
        command = f"L{low},{high},"
        self.send_serial_command(command)
        
    def send_protocol_command(self):
        """发送协议命令"""
        protocol = self.protocol_edit.value()
        if int(protocol) > 10:
            QMessageBox.information(
                self,
                "Protocol",
                "Manual protocol commands are limited to P0-P10. Low-battery training is handled automatically as Mode0 saved to the Mode3 folder.",
            )
            return
        reply = QMessageBox.question(
            self,
            "Confirm",
            f"Change the firmware to protocol P{protocol}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            command = f"Z{protocol},"
            success = self.send_serial_command(command)
            if success:
                self.add_message(f"Manual protocol command sent: P{protocol}")

    def send_block_switch_mode_command(self):
        """BlockSwitchMode is intentionally disabled."""
        self.add_message("BlockSwitchMode is disabled for DBRuleSwitch protocol flow.")
        return False

    def send_post_training_protocol0_command(self):
        """发送训练后protocol0命令"""
        reply = QMessageBox.question(
            self,
            "Confirm Post-Training P0",
            "Send G to enter the post-training protocol0 stage?\n\n"
            "This resets Habits into the special protocol0 free-reward mode.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            success = self.send_serial_command("G")
            if success:
                self.protocol_edit.setValue(0)
                self.add_message("Post-training protocol0 command sent (G)")

    def send_inter_block_interval_command(self):
        """发送BlockswitchMode最小block间隔命令"""
        interval_minutes = self.inter_block_interval_edit.value()
        interval_ms = int(interval_minutes * 60 * 1000)
        command = f"I{interval_ms},"
        success = self.send_serial_command(command)
        if success:
            self.add_message(f"Inter-block interval set to {interval_minutes} min")
            
    def send_pause_command(self):
        """发送暂停/恢复命令"""
        if self.pause_btn.text() == "Pause Habits":
            success = self.send_serial_command("P")
            if success:
                self.pause_btn.setText("Resume Habits")
        else:
            if not self._ensure_resume_habits_ready():
                return
            success = self.send_serial_command("M")
            if success:
                self.pause_btn.setText("Pause Habits")
            
    def send_handshake_command(self):
        """发送握手命令"""
        self.send_serial_command("H")
        
    def send_time_command(self):
        """发送时间校正命令"""
        timestamp = int(time.time())
        command = f"T{timestamp}"
        self.send_serial_command(command)
        
        # 更新同步时间显示
        current_time = datetime.fromtimestamp(timestamp).strftime("%H:%M:%S")
        self.sync_time_label.setText(f"Synced: {current_time}")
        self.add_message(f"Time synchronized at {current_time}")
        
    def send_read_all_command(self):
        """发送读取所有值命令"""
        command = "A"
        success = self.send_serial_command(command)
        if success:
            self.add_message("Read all values command sent")

    def send_cap_snapshot_command(self):
        """发送MPR121快照保存命令"""
        success = self.send_serial_command("C")
        if success:
            self.add_message("Cap baseline refresh command sent")

    def send_rf_off_ready_command(self):
        """发送RF已关闭确认命令，触发固件SoftEvent1"""
        success = self.send_serial_command("F")
        if success:
            self.add_message("RF-off confirmation command sent (F)")
        return success

    def send_charging_warning_command(self, duration_ms: int = 3000, fan_enabled: bool = True):
        """发送训练台滞留警告命令。"""
        duration_ms = max(0, int(duration_ms))
        fan_value = 1 if bool(fan_enabled) else 0
        success = self.send_serial_command(f"W{duration_ms},{fan_value},")
        if success:
            self.add_message(f"Charging warning command sent (W{duration_ms},{fan_value})")
        return success

    def stop_charging_warning_command(self):
        """停止训练台滞留警告。"""
        success = self.send_serial_command("W0,")
        if success:
            self.add_message("Charging warning stop command sent (W0)")
        return success
            
    def update_gui_values(self, reward_left: int, reward_right: int, reward_middle: int,
                         low_light_intensity: int, high_light_intensity: int, 
                         curr_protocol_index: int, teensy_timestamp: int,
                         inter_block_interval_ms: int = 3600000):
        """更新GUI控件的值"""
        try:
            # 更新奖励值
            self._set_spinbox_value_if_idle(self.reward_left_edit, reward_left)
            self._set_spinbox_value_if_idle(self.reward_right_edit, reward_right)
            self._set_spinbox_value_if_idle(self.reward_middle_edit, reward_middle)
            
            # 更新光强值
            self._set_spinbox_value_if_idle(self.low_light_edit, low_light_intensity)
            self._set_spinbox_value_if_idle(self.high_light_edit, high_light_intensity)
            
            # 更新协议索引
            self._set_spinbox_value_if_idle(self.protocol_edit, curr_protocol_index)
            self._set_spinbox_value_if_idle(
                self.inter_block_interval_edit,
                max(0, int(round(inter_block_interval_ms / 60000.0))),
            )
            # update sync time label
            self.sync_time_label.setText(f"Synced: {datetime.fromtimestamp(float(teensy_timestamp)).strftime('%H:%M:%S')}")
            
            self.add_message("GUI values updated successfully")
            
        except Exception as e:
            self.add_message(f"Failed to update GUI values: {str(e)}")

    def update_protocol_progress_display(self, data: str):
        """Update protocol progress telemetry from firmware."""
        if not data.startswith("PG,"):
            return

        payload = data[3:]
        progress_data: Dict[str, str] = {}
        for item in payload.split(';'):
            if not item or '=' not in item:
                continue
            key, value = item.split('=', 1)
            progress_data[key.strip()] = value.strip()

        self.protocol_progress_data = progress_data

        def get_int(key: str, default: int = 0) -> int:
            try:
                return int(progress_data.get(key, default))
            except (TypeError, ValueError):
                return default

        def get_float(key: str, default: float = 0.0) -> float:
            try:
                return float(progress_data.get(key, default))
            except (TypeError, ValueError):
                return default

        curr_protocol = get_int('curr', -1)
        mode = get_int('mode', 0)
        next_protocol = get_int('next', 255)
        pending_protocol = get_int('pending', 255)
        auto_ready = bool(get_int('auto_ready', 0))
        pending_ready = bool(get_int('pending_ready', 0))
        trial_count = get_int('trial_count', 0)
        trial_target = get_int('trial_target', 0)
        trial_op = get_int('trial_op', 0)
        trial_ready = bool(get_int('trial_ready', 0))
        easy_threshold = get_int('easy_threshold', 0)
        easy_required = get_int('easy_required', 0)
        easy_ready = bool(get_int('easy_ready', 0))
        left_easy_perf = get_int('left_easy_perf', 0)
        left_easy_count = get_int('left_easy_count', 0)
        right_easy_perf = get_int('right_easy_perf', 0)
        right_easy_count = get_int('right_easy_count', 0)
        easy100_perf = get_int('easy100_perf', 0)
        easy100_count = get_int('easy100_count', 0)
        easy100_required = get_int('easy100_required', 0)
        protocol_elapsed_sec = get_int('protocol_elapsed_sec', 0)
        protocol_time_target_sec = get_int('protocol_time_target_sec', 0)
        protocol_time_ready = bool(get_int('protocol_time_ready', 0))
        rule_elapsed_sec = get_int('rule_elapsed_sec', 0)
        rule_time_target_sec = get_int('rule_time_target_sec', 0)
        rule_time_ready = bool(get_int('rule_time_ready', 0))
        coverage_enabled = bool(get_int('coverage_enabled', 0))
        coverage_valid = get_int('coverage_valid', 0)
        coverage_window = get_int('coverage_window', 0)
        coverage_min = get_int('coverage_min', 0)
        coverage_max = get_int('coverage_max', 0)
        coverage_ready = bool(get_int('coverage_ready', 0))
        progressive_enabled = bool(get_int('progressive_enabled', 0))
        ramp_progress = get_float('ramp_progress', 0.0)
        ramp_low = get_float('ramp_low', 0.0)
        ramp_high = get_float('ramp_high', 0.0)
        block_step = get_int('block_step', 0)
        block_second_rule = get_int('block_second_rule', 1)
        block_next_a = get_int('block_next_a', 255)
        block_next_b = get_int('block_next_b', 255)
        trial_block_onset = get_int('trial_block_onset', 0)
        rule_hint_active = bool(get_int('rule_hint_active', 0))
        rule_hint_count = get_int('rule_hint_count', 0)
        rule_hint_target = get_int('rule_hint_target', 0)

        coverage_counts_raw = progress_data.get('coverage_counts', '')
        coverage_counts = [
            part.strip() for part in coverage_counts_raw.split(',') if part.strip() != ''
        ]
        if not coverage_counts:
            coverage_counts = ['0', '0', '0', '0', '0']

        class_probs_raw = progress_data.get('class_probs', '')
        class_probs = [
            part.strip() for part in class_probs_raw.split(',') if part.strip() != ''
        ]
        while len(class_probs) < 3:
            class_probs.append('0')

        mode_text = "BlockSwitch" if mode == 1 else "Training"
        if pending_protocol != 255:
            next_text = f"P{pending_protocol}"
            if not pending_ready:
                next_text += " (waiting TrialBlockOnset)"
        elif mode == 1 and block_step == 0 and block_next_a != 255 and block_next_b != 255:
            next_text = f"P{block_next_a} / P{block_next_b} (randomized)"
        elif next_protocol != 255:
            next_text = f"P{next_protocol}"
        else:
            next_text = "-"

        self.protocol_progress_summary_label.setText(
            f"Mode: {mode_text} | Current: P{curr_protocol} | Next: {next_text} | "
            f"Auto Ready: {'Yes' if auto_ready else 'No'}"
        )

        trial_op_text = {0: "-", 1: ">", 2: ">="}.get(trial_op, "-")
        detail_lines = []

        if trial_target > 0:
            detail_lines.append(
                f"Trials: {trial_count} / {trial_op_text}{trial_target} "
                f"(ready: {'yes' if trial_ready else 'no'})"
            )
        else:
            detail_lines.append(f"Trials: {trial_count}")

        if curr_protocol == 0:
            detail_lines.append(
                f"Protocol0 time: {protocol_elapsed_sec / 3600.0:.1f} h / "
                f"{protocol_time_target_sec / 3600.0:.1f} h "
                f"(ready: {'yes' if protocol_time_ready else 'no'})"
            )

        if curr_protocol >= 5 or mode == 1:
            detail_lines.append(
                f"Easy left:  {left_easy_perf}% using {left_easy_count}/{easy_required} easy trials; "
                f"threshold >= {easy_threshold}%"
            )
            detail_lines.append(
                f"Easy right: {right_easy_perf}% using {right_easy_count}/{easy_required} easy trials; "
                f"threshold >= {easy_threshold}% (ready: {'yes' if easy_ready else 'no'})"
            )

        if progressive_enabled:
            detail_lines.append(
                f"Ramp: {ramp_progress * 100.0:.1f}% from easy perf {easy100_perf}% "
                f"using {easy100_count}/{easy100_required} easy trials; "
                f"thresholds {ramp_low:.1f}-{ramp_high:.1f}%"
            )
            detail_lines.append(
                "Class probs (easy/intermediate/ambiguous): "
                f"{class_probs[0]}, {class_probs[1]}, {class_probs[2]}"
            )

        if coverage_enabled:
            detail_lines.append(
                f"Coverage: {coverage_valid}/{coverage_window} valid trials; target per level "
                f"{coverage_min}-{coverage_max} (ready: {'yes' if coverage_ready else 'no'})"
            )
            detail_lines.append(
                f"Coverage counts [-1,-0.5,0,0.5,1]: [{', '.join(coverage_counts)}]"
            )

        if mode == 1:
            detail_lines.append(
                f"Rule time: {rule_elapsed_sec / 3600.0:.1f} h / "
                f"{rule_time_target_sec / 3600.0:.1f} h "
                f"(ready: {'yes' if rule_time_ready else 'no'})"
            )
            detail_lines.append(
                f"Block step: {block_step}, second rule: {block_second_rule}, "
                f"TrialBlockOnset: {trial_block_onset}"
            )
            if pending_protocol != 255:
                detail_lines.append(
                    f"Pending switch gate: TrialBlockOnset must be 1 "
                    f"(current: {trial_block_onset})"
                )

        detail_lines.append(
            f"Rule-switch hint: {'active' if rule_hint_active else 'inactive'} "
            f"({rule_hint_count}/{rule_hint_target})"
        )

        self.protocol_progress_detail.setPlainText("\n".join(detail_lines))
        
    def add_message(self, message: str):
        """添加消息到错误列表"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.error_listbox.addItem(f"[{timestamp}] {message}")
        
        # 保持列表大小
        if self.error_listbox.count() > 100:
            self.error_listbox.takeItem(0)
            
        # 滚动到底部
        self.error_listbox.scrollToBottom()

    def get_psychometric_window_size(self) -> int:
        """Return the requested recent-trial window size for psychometric plots."""
        try:
            return int(self.psychometric_n_combo.currentText())
        except (TypeError, ValueError, AttributeError):
            return 500

    def show_psychometric_dialog(self, *_args):
        """Open or focus the psychometric curve dialog."""
        if self.psychometric_dialog is None:
            self.psychometric_dialog = PsychometricCurveDialog(self)
            self.psychometric_dialog.destroyed.connect(
                lambda *_: setattr(self, 'psychometric_dialog', None)
            )

        self.refresh_psychometric_dialog()
        self.psychometric_dialog.show()
        self.psychometric_dialog.raise_()
        self.psychometric_dialog.activateWindow()

    def on_psychometric_window_size_changed(self, *_args):
        """Refresh the psychometric dialog when the selected window changes."""
        if self.psychometric_dialog is not None and self.psychometric_dialog.isVisible():
            self.refresh_psychometric_dialog()

    def refresh_psychometric_dialog(self):
        """Push the latest GUI-cached psychometric data into the dialog."""
        if self.psychometric_dialog is None:
            return

        window_size = self.get_psychometric_window_size()
        trials = self.data_manager.get_recent_psychometric_trials(window_size)
        self.psychometric_dialog.refresh_plot(trials, window_size)
        
    def delete_selected_error(self):
        """删除选中的错误消息"""
        current_row = self.error_listbox.currentRow()
        if current_row >= 0:
            self.error_listbox.takeItem(current_row)
            
    def clear_all_errors(self):
        """清除所有错误消息"""
        self.error_listbox.clear()
        
    def clear_serial_data(self):
        """清空串口数据日志"""
        self.serial_data_log.clear()
        self.serial_data_display.clear()
        
    def trigger_sd_download(self):
        """触发SD卡文件上传至电脑"""
        if self.remote_view_mode and callable(self._remote_sd_download_handler):
            dir_path = QFileDialog.getExistingDirectory(
                self,
                "Select Save Directory for SD files",
                self.selected_data_dir or "",
            )
            if not dir_path:
                return
            self.sd_save_dir = dir_path
            self.sd_progress_bar.setValue(0)
            self.sd_progress_bar.show()
            self.download_sd_btn.setEnabled(False)
            self.add_message(f"Requesting SD files download to {dir_path}...")
            self.sd_progress_bar.setRange(0, 0)
            success = bool(self._remote_sd_download_handler(dir_path))
            if not success:
                self.sd_progress_bar.setRange(0, 100)
                self.sd_progress_bar.hide()
                self.download_sd_btn.setEnabled(True)
                self.add_message("Failed to request SD files download")
            return
        if not self.serial_worker or not self.serial_worker.running:
            QMessageBox.warning(self, "Error", "Not connected to Habits serial.")
            return
            
        dir_path = QFileDialog.getExistingDirectory(self, "Select Save Directory for SD files", self.selected_data_dir or "")
        if not dir_path:
            return
            
        self.sd_save_dir = dir_path
        self.sd_progress_bar.setValue(0)
        self.sd_progress_bar.show()
        self.download_sd_btn.setEnabled(False)
        self.add_message(f"Requesting SD files download to {dir_path}...")
        
        # Emulating the progress bar moving purely as visual feedback since actual filesize tracker is not implemented fully
        self.sd_progress_bar.setRange(0, 0) # Indeterminate progress during download
        
        self.send_serial_command("U")

    def save_parameters(self):
        """保存参数"""
        try:
            mouse_id = self.mouse_id_edit.text()
            if not mouse_id:
                mouse_id = "Mouse_001"  # 默认值
                
            # 确定保存目录
            if self.selected_data_dir:
                mouse_data_folder = self.selected_data_dir
            else:
                mouse_data_folder = get_mouse_data_directory(mouse_id)
            
            # 确保目录存在
            if not os.path.exists(mouse_data_folder):
                os.makedirs(mouse_data_folder)
            
            params = {
                'mouse_id': mouse_id,
                'reward_left': self.reward_left_edit.value(),
                'reward_middle': self.reward_middle_edit.value(),
                'reward_right': self.reward_right_edit.value(),
                'low_light': self.low_light_edit.value(),
                'high_light': self.high_light_edit.value(),
                'protocol': self.protocol_edit.value(),
                'inter_block_interval_minutes': self.inter_block_interval_edit.value(),
                'start_date': self.start_date_edit.date().toString(),
                'cap_warning_threshold': self.cap_warning_threshold,
                'cap_alarm_threshold': self.cap_alarm_threshold,
                'cap_detect': self.cap_detect_label.text(),
                'cap_baseline': self.cap_baseline_label.text(),
                'cap_filtered': self.cap_filtered_label.text(),
                'cap_delta': self.cap_delta_label.text(),
                'cap_drift': self.cap_drift_label.text(),
                'cap_drift_state': self.cap_drift_state,
                'cap_history': list(self.cap_history)
            }
            
            # 保存参数到mouse ID文件夹下
            params_file = os.path.join(mouse_data_folder, "habits_params.json")
            with open(params_file, 'w') as f:
                json.dump(params, f, indent=2)
            
            # 保存试验数据到mouse ID文件夹下
            trial_data_file = os.path.join(mouse_data_folder, "trial_data.json")
            
            # 直接保存内存中的所有数据（覆盖模式），作为当前状态的快照
            # 这样可以避免追加模式导致的重复数据问题，且能完美还原当前内存状态
            all_trial_data = []
            for trial in self.data_manager.trial_data:
                trial_dict = {
                    'timestamp': trial['timestamp'].isoformat(),
                    'trial_type': trial.get('trial_type', 0),
                    'outcome': trial.get('outcome', 0),
                    'response_time': trial.get('response_time', 0)
                }
                # 保存所有必要字段以还原状态
                if 'trial_num' in trial:
                    trial_dict['trial_num'] = trial['trial_num']
                if 'performance' in trial:
                    trial_dict['performance'] = trial['performance']
                if 'protocol' in trial:
                    trial_dict['protocol'] = trial['protocol']
                if 'early_lick_rate' in trial:
                    trial_dict['early_lick_rate'] = trial['early_lick_rate']
                if 'protocol_trials' in trial:
                    trial_dict['protocol_trials'] = trial['protocol_trials']
                if 'protocol_perf' in trial:
                    trial_dict['protocol_perf'] = trial['protocol_perf']
                if 'rule' in trial:
                    trial_dict['rule'] = trial['rule']
                if 'curr_stimu0' in trial:
                    trial_dict['curr_stimu0'] = trial['curr_stimu0']
                if 'curr_stimu1' in trial:
                    trial_dict['curr_stimu1'] = trial['curr_stimu1']
                    
                all_trial_data.append(trial_dict)
            
            # 保存数据
            with open(trial_data_file, 'w') as f:
                json.dump(all_trial_data, f, indent=2)
                
            # Save mode switches
            mode_switch_file = os.path.join(mouse_data_folder, "mode_switch_data.json")
            switches_to_save = []
            for switch in self.data_manager.mode_switches:
                switches_to_save.append({
                    'start': switch['start'].isoformat(),
                    'end': switch['end'].isoformat()
                })
            with open(mode_switch_file, 'w') as f:
                json.dump(switches_to_save, f, indent=2)
                
            self.add_message("Parameters updated and trial data snapshot saved successfully")
            
        except Exception as e:
            self.add_message(f"Failed to save parameters: {str(e)}")
            
    def load_parameters(self):
        """加载参数"""
        try:
            # 首先尝试从当前mouse ID文件夹加载
            mouse_id = self.mouse_id_edit.text()
            if not mouse_id:
                mouse_id = "Mouse_001"  # 默认值
                
            # 确定加载目录
            if self.selected_data_dir:
                mouse_data_folder = self.selected_data_dir
            else:
                mouse_data_folder = get_mouse_data_directory(mouse_id)
                
            params_file = os.path.join(mouse_data_folder, "habits_params.json")
            
            # 如果mouse ID文件夹不存在，尝试从旧的根目录加载
            if not os.path.exists(params_file) and not self.selected_data_dir:
                params_file = os.path.join(self.data_folder, "habits_params.json")
                
            if os.path.exists(params_file):
                with open(params_file, 'r') as f:
                    params = json.load(f)
                    
                self.mouse_id_edit.setText(params.get('mouse_id', mouse_id))
                self.reward_left_edit.setValue(params.get('reward_left', 30))
                self.reward_middle_edit.setValue(params.get('reward_middle', 30))
                self.reward_right_edit.setValue(params.get('reward_right', 30))
                self.low_light_edit.setValue(params.get('low_light', 1))
                self.high_light_edit.setValue(params.get('high_light', 255))
                self.protocol_edit.setValue(params.get('protocol', 0))
                self.inter_block_interval_edit.setValue(
                    params.get('inter_block_interval_minutes', 60)
                )
                self.cap_warning_threshold = int(params.get('cap_warning_threshold', self.cap_warning_threshold))
                self.cap_alarm_threshold = int(params.get('cap_alarm_threshold', self.cap_alarm_threshold))
                self.cap_detect_label.setText(params.get('cap_detect', self.cap_detect_label.text()))
                self.cap_detect_label.setStyleSheet(
                    "font-weight: bold; color: #2E7D32;" if self.cap_detect_label.text() == "Enabled" else "font-weight: bold; color: #888;"
                )
                self.cap_baseline_label.setText(params.get('cap_baseline', self.cap_baseline_label.text()))
                self.cap_filtered_label.setText(params.get('cap_filtered', self.cap_filtered_label.text()))
                self.cap_delta_label.setText(params.get('cap_delta', self.cap_delta_label.text()))
                self.cap_drift_label.setText(params.get('cap_drift', self.cap_drift_label.text()))
                self.cap_drift_state = params.get('cap_drift_state', self.cap_drift_state)
                self.cap_history = deque(params.get('cap_history', []), maxlen=2000)
                if self.cap_drift_state == "alarm":
                    self.cap_drift_label.setStyleSheet("font-weight: bold; color: #C62828;")
                elif self.cap_drift_state == "warning":
                    self.cap_drift_label.setStyleSheet("font-weight: bold; color: #EF6C00;")
                elif self.cap_drift_state == "normal":
                    self.cap_drift_label.setStyleSheet("font-weight: bold; color: #2E7D32;")
                self.update_cap_trial_chart()
                
                if 'start_date' in params:
                    date = QDate.fromString(params['start_date'])
                    if date.isValid():
                        self.start_date_edit.setDate(date)
                        
                self.add_message("Parameters loaded successfully")
                
                # 如果没有选择目录，更新目录路径
                if not self.selected_data_dir:
                    mouse_id = params.get('mouse_id', mouse_id)
                    mouse_data_folder = get_mouse_data_directory(mouse_id)
            else:
                self.add_message("No parameter file found")
            
            # 加载试验数据
            trial_data_file = os.path.join(mouse_data_folder, "trial_data.json")
            
            # 如果mouse ID文件夹中没有数据，尝试从旧的根目录加载
            if not os.path.exists(trial_data_file) and not self.selected_data_dir:
                trial_data_file = os.path.join(self.data_folder, "trial_data.json")
                
            if os.path.exists(trial_data_file):
                with open(trial_data_file, 'r') as f:
                    trial_data_saved = json.load(f)
                
                # 清空现有数据并重新加载
                self.data_manager.clear_data()
                
                for trial_dict in trial_data_saved:
                    trial_data = {
                        'timestamp': datetime.fromisoformat(trial_dict['timestamp']),
                        'trial_type': trial_dict.get('trial_type', 0),
                        'outcome': trial_dict.get('outcome', 0),
                        'response_time': trial_dict.get('response_time', 0)
                    }
                    # 加载可选字段
                    if 'trial_num' in trial_dict:
                        trial_data['trial_num'] = trial_dict['trial_num']
                    if 'performance' in trial_dict:
                        trial_data['performance'] = trial_dict['performance']
                    if 'protocol' in trial_dict:
                        trial_data['protocol'] = trial_dict['protocol']
                    if 'early_lick_rate' in trial_dict:
                        trial_data['early_lick_rate'] = trial_dict['early_lick_rate']
                    elif 'early_lick' in trial_dict: # 兼容旧格式
                        trial_data['early_lick_rate'] = trial_dict['early_lick']
                    if 'protocol_trials' in trial_dict:
                        trial_data['protocol_trials'] = trial_dict['protocol_trials']
                    if 'protocol_perf' in trial_dict:
                        trial_data['protocol_perf'] = trial_dict['protocol_perf']
                    if 'rule' in trial_dict:
                        trial_data['rule'] = trial_dict['rule']
                    if 'curr_stimu0' in trial_dict:
                        trial_data['curr_stimu0'] = trial_dict['curr_stimu0']
                    if 'curr_stimu1' in trial_dict:
                        trial_data['curr_stimu1'] = trial_dict['curr_stimu1']
                        
                    self.data_manager.add_trial(trial_data)
                
                self.add_message(f"Loaded {len(trial_data_saved)} trial records")

                # 更新图表
                self.update_performance_chart()
                self.plot_24h_trials()
                self.refresh_psychometric_dialog()
            
            # Load mode switches
            mode_switch_file = os.path.join(mouse_data_folder, "mode_switch_data.json")
            # Fallback check
            if not os.path.exists(mode_switch_file):
                 mode_switch_file = os.path.join(self.data_folder, "mode_switch_data.json")
            
            if os.path.exists(mode_switch_file):
                with open(mode_switch_file, 'r') as f:
                    saved_switches = json.load(f)
                
                self.data_manager.mode_switches.clear()
                for s in saved_switches:
                    self.data_manager.add_mode_switch({
                        'start': datetime.fromisoformat(s['start']),
                        'end': datetime.fromisoformat(s['end'])
                    })
                self.add_message(f"Loaded {len(saved_switches)} mode switch records")
                # Refresh chart again to show blocks
                self.plot_24h_trials()
            
            self._refresh_status_information()
                
        except Exception as e:
            self.add_message(f"Failed to load parameters: {str(e)}")

    def _refresh_status_information(self):
        try:
            start_date = self.start_date_edit.date().toPyDate()
            current_date = datetime.now().date()
            days = (current_date - start_date).days
            self.days_label.setText(f"{days:.1f} d")

            if len(self.data_manager.trial_data) == 0:
                self.trial_label.setText("0 - 0.0%")
                self.trials_day_label.setText("0/d")
                self.current_trial_num = 0
                self.protocol_trials_label.setText("0")
                self.trial_type_label.setText("-")
                self.outcome_label.setText("-")
                self.protocol_perf_label.setText("0%")
                return

            last_trial = self.data_manager.trial_data[-1]
            trial_num = last_trial.get('trial_num', len(self.data_manager.trial_data) - 1)
            perf = last_trial.get('performance', 0.0)
            
            self.trial_label.setText(f"{int(trial_num)} - {float(perf):.1f}%")

            self.current_trial_num = int(trial_num) + 1
            current_trials = self.data_manager.get_daily_trial_count()
            self.trials_day_label.setText(f"{current_trials}/d")
            
            # 恢复其他标签
            protocol_trials = last_trial.get('protocol_trials', 0)
            self.protocol_trials_label.setText(str(protocol_trials))
            
            trial_type = last_trial.get('trial_type', 0)
            type_str = "Left" if trial_type == 1 else "Right" if trial_type == 2 else "Middle" if trial_type == 3 else "Unknown"
            self.trial_type_label.setText(f"{trial_type} ({type_str})")
            
            outcome = last_trial.get('outcome', 0)
            outcome_str = "No Resp" if outcome == 0 else "Correct" if outcome == 1 else "Error" if outcome == 2 else "Early Lick" if outcome == 3 else "Other"
            self.outcome_label.setText(f"{outcome} ({outcome_str})")
            
            protocol_perf = last_trial.get('protocol_perf', 0.0)
            self.protocol_perf_label.setText(f"{float(protocol_perf):.1f}%")
            
        except Exception:
            pass
            
    def save_data_to_file(self, data: str):
        """保存原始数据到文件"""
        try:
            mouse_id = self.mouse_id_edit.text()
            date_str = datetime.now().strftime("%Y%m%d")
            filename = f"{mouse_id}_{date_str}.txt"
            
            # 确定保存目录
            if self.selected_data_dir:
                filepath = os.path.join(self.selected_data_dir, filename)
            else:
                filepath = os.path.join(self.data_folder, filename)
            
            with open(filepath, 'a', encoding='utf-8') as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] {data}\n")
                
        except Exception as e:
            self.add_message(f"Failed to save data: {str(e)}")
    
    def save_time_align_data(self):
        """保存TimeAlign时间戳数据到专用文件"""
        try:
            mouse_id = self.mouse_id_edit.text()
            if not mouse_id:
                self.add_message("Warning: Mouse ID is empty, using 'Unknown' for TimeAlign file")
                mouse_id = "Unknown"
            
            # 确定保存目录
            if self.selected_data_dir:
                mouse_folder = self.selected_data_dir
            else:
                mouse_folder = get_mouse_data_directory(mouse_id)
            
            # 生成TimeAlign文件名
            date_str = datetime.now().strftime("%Y%m%d")
            filename = f"TimeAlign_{date_str}.txt"
            filepath = os.path.join(mouse_folder, filename)
            
            # 获取高精度时间戳（毫秒级）
            now = datetime.now()
            timestamp_ms = int(now.timestamp() * 1000)  # 毫秒时间戳
            
            # 保存数据：trial_num 和 时间戳(ms)
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(f"{self.current_trial_num}\t{timestamp_ms}\n")
            
            # 添加日志消息
            self.add_message(f"TimeAlign recorded: Trial {self.current_trial_num}, Time {timestamp_ms}ms")
            
        except Exception as e:
            self.add_message(f"Failed to save TimeAlign data: {str(e)}")
    
    def plot_24h_trials(self):
        """绘制24小时试验散点图"""
        try:
            # 清除现有图表
            self.trials_24h_widget.clear()
            
            # 获取当前时间
            now = datetime.now()
            cutoff_time = now - timedelta(hours=24)
            
            # 使用数据管理器获取24小时内的数据
            recent_trials = self.data_manager.get_recent_trials(24)
            
            # 如果没有trial数据，显示空图表
            if not recent_trials:
                self.trials_24h_widget.setLabel('left', 'Trial Type')
                self.trials_24h_widget.setLabel('bottom', 'Time (hours ago)')
                self.trials_24h_widget.setTitle('24h Trials (No Recent Data)')
                return
            
            # 准备数据
            times = []  # 相对于当前时间的小时数（负值表示过去）
            trial_types = []  # 0=left, 1=right
            outcomes = []  # 0=no response, 1=correct, 2=error
            
            for trial in recent_trials:
                # 计算相对时间（小时前）
                time_diff = (now - trial['timestamp']).total_seconds() / 3600
                times.append(-time_diff)  # 负值表示过去
                
                # 转换trial_type: 1=left->0, 2=right->1, 3=middle->0.5
                if trial['trial_type'] == 1:  # left
                    trial_types.append(0)
                elif trial['trial_type'] == 2:  # right
                    trial_types.append(1)
                else:  # middle or other
                    trial_types.append(0.5)
                
                outcomes.append(trial['outcome'])
            
            # 根据outcome分组绘制散点
            import numpy as np
            times = np.array(times)
            trial_types = np.array(trial_types)
            outcomes = np.array(outcomes)
            
            # 绘制不同outcome的散点
            # Correct trials (outcome=1) - 绿色圆点
            correct_mask = outcomes == 1
            if np.any(correct_mask):
                scatter_correct = pg.ScatterPlotItem(
                    x=times[correct_mask], 
                    y=trial_types[correct_mask],
                    pen=pg.mkPen(None),
                    brush=pg.mkBrush(0, 255, 0, 180),  # 绿色
                    size=15,
                    symbol='o'
                )
                self.trials_24h_widget.addItem(scatter_correct)
            
            # Error trials (outcome=2) - 红色X
            error_mask = outcomes == 2
            if np.any(error_mask):
                scatter_error = pg.ScatterPlotItem(
                    x=times[error_mask], 
                    y=trial_types[error_mask],
                    pen=pg.mkPen(255, 0, 0, 255),  # 红色
                    brush=pg.mkBrush(None),
                    size=15,
                    symbol='x'
                )
                self.trials_24h_widget.addItem(scatter_error)
            
            # Early Lick trials (outcome=3) - 紫色菱形 (显眼)
            early_lick_mask = outcomes == 3
            if np.any(early_lick_mask):
                scatter_early_lick = pg.ScatterPlotItem(
                    x=times[early_lick_mask], 
                    y=trial_types[early_lick_mask],
                    pen=pg.mkPen(255, 0, 255, 255),  # 紫色
                    brush=pg.mkBrush(255, 0, 255, 200),
                    size=6,
                    symbol='d'
                )
                self.trials_24h_widget.addItem(scatter_early_lick)
            
            # No response trials (outcome=0) - 蓝色圆圈
            no_response_mask = outcomes == 0
            if np.any(no_response_mask):
                scatter_no_response = pg.ScatterPlotItem(
                    x=times[no_response_mask], 
                    y=trial_types[no_response_mask],
                    pen=pg.mkPen(255, 255, 255, 255),  # 白色
                    brush=pg.mkBrush(None),
                    size=12,
                    symbol='o'
                )
                self.trials_24h_widget.addItem(scatter_no_response)
            
             # other trials (outcome=4) - 白色方块
            other_mask = outcomes == 4
            if np.any(other_mask):
                scatter_other = pg.ScatterPlotItem(
                    x=times[other_mask], 
                    y=trial_types[other_mask],
                    pen=pg.mkPen(255, 255, 255, 255),  # 白色
                    brush=pg.mkBrush(None),
                    size=12,
                    symbol='s'
                )
                self.trials_24h_widget.addItem(scatter_other)
            
            # 设置坐标轴
            self.trials_24h_widget.setLabel('left', 'Trial Type')
            self.trials_24h_widget.setLabel('bottom', 'Time (hours ago)')
            
            # 设置Y轴刻度和标签
            y_ticks = [(0, 'Left'), (0.5, 'Middle'), (1, 'Right')]
            self.trials_24h_widget.getAxis('left').setTicks([y_ticks])
            
            # 设置X轴范围和刻度
            self.trials_24h_widget.setXRange(-24, 0)
            self.trials_24h_widget.setYRange(-0.2, 1.2)
            x_ticks = [(-24, '24'), (-18, '18'), (-12, '12'), (-6, '6'), (0, '0')]
            self.trials_24h_widget.getAxis('bottom').setTicks([x_ticks])
            
            # 添加当前时间线
            current_line = pg.InfiniteLine(pos=0, angle=90, pen=pg.mkPen('red', width=2))
            self.trials_24h_widget.addItem(current_line)
            
            # 绘制 ESA Mode Blocks (ModeSwitch Intervals)
            # 包括当前正在进行的区间（如果有）
            active_intervals = self.data_manager.get_recent_mode_switches(24)
            if self.current_esa_start_time:
                active_intervals.append({
                    'start': self.current_esa_start_time,
                    'end': now
                })
            for switch in active_intervals:
                # Calculate relative time (hours ago)
                t_end = (now - switch['end']).total_seconds() / 3600
                t_start = (now - switch['start']).total_seconds() / 3600
                
                # Convert to X coordinates (negative values)
                x_start = -t_start
                x_end = -t_end
                
                # Clip to visible range
                if x_end < -24: continue
                if x_start > 0: x_start = 0 
                # Create a semi-transparent block
                # Using Cyan/Teal color with higher opacity for better visibility
                # Add to bottom (z-value) to not obscure dots
                region = pg.LinearRegionItem(
                    values=[x_start, x_end], 
                    brush=pg.mkBrush(0, 150, 136, 100),  # Teal, alpha=100 (increased)
                    pen=None,
                    movable=False
                )
                region.setZValue(-100) # Put behind scatter plots
                self.trials_24h_widget.addItem(region)

            # 计算性能统计
            total_trials = len(recent_trials)
            correct_trials = np.sum(outcomes == 1)
            error_trials = np.sum(outcomes == 2)
            no_response_trials = np.sum(outcomes == 0)
            early_lick_trials = np.sum(outcomes == 3)
            
            # 计算性能百分比（排除no response）
            if total_trials - no_response_trials > 0:
                performance = correct_trials / (total_trials - no_response_trials) * 100
            else:
                performance = 0
            
            # 设置标题
            title = f'24h Trials: {total_trials} total (Perf: {performance:.1f}%, EL: {early_lick_trials})'
            self.trials_24h_widget.setTitle(title)
            
        except Exception as e:
            self.add_message(f"Failed to plot 24h trials: {str(e)}")
            
    def update_gui(self):
        """更新GUI显示"""
        # 更新天数
        start_date = self.start_date_edit.date().toPyDate()
        current_date = datetime.now().date()
        days = (current_date - start_date).days
        self.days_label.setText(f"{days:.1f} d")

        self.save_parameters()
        
    def periodic_cleanup(self):
        """定期数据清理"""
        try:
            # 获取清理前的内存使用情况
            memory_before = self.data_manager.get_memory_usage()
            
            # 执行清理
            self.data_manager.force_cleanup()
            
            # 获取清理后的内存使用情况
            memory_after = self.data_manager.get_memory_usage()
            
            # 记录清理信息
            trials_before = memory_before['trial_data_count']
            trials_after = memory_after['trial_data_count']
            
            if trials_before != trials_after:
                self.add_message(f"Cleanup completed: trials {trials_before}→{trials_after}")
                
        except Exception as e:
            self.add_message(f"Cleanup failed: {str(e)}")
            
    def get_memory_status(self) -> str:
        """获取内存状态信息"""
        memory_info = self.data_manager.get_memory_usage()
        return (f"Memory: trials {memory_info['trial_data_count']}, "
                f"cache keys {memory_info['daily_cache_keys']}")
        
    def closeEvent(self, event):
        """关闭事件处理"""
        if self.serial_worker:
            self.serial_worker.stop()
        # 停止定时器
        if hasattr(self, 'cleanup_timer'):
            self.cleanup_timer.stop()
        event.accept()


if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    
    app = QApplication(sys.argv)
    app.setApplicationName("Optimized Habits Panel")
    
    window = OptimizedHabitsPanel()
    window.show()
    
    sys.exit(app.exec())
