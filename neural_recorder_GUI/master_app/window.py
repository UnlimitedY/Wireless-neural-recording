import multiprocessing as mp
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import QDate, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QProgressDialog,
    QPushButton,
    QMenu,
    QScrollArea,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

try:
    from ..monitoring.alarming import (
        ALARM_CONFIG_FILENAME,
        build_card_position,
        evaluate_slot_alarm_conditions,
        load_alarm_config,
        send_alarm_email,
    )
    from ..services.health_metrics import classify_packet_loss_state
    from ..hardware.camera_module import get_available_cameras
    from .contracts import (
        MASTER_CONFIG_FILENAME,
        SlotConfig,
        extract_mouse_id_from_directory,
        load_master_console_config,
        make_slot_command,
    )
    from .battery_timeline import build_battery_timeline_payload
    from .runtime_config import load_runtime_config
    from .protocol_flow_dialog import ProtocolFlowDialog
    from .card_layout import (
        CollapsibleSectionCard,
        CompactActionBar,
        StatusChipGrid,
        set_checked_if_changed,
        set_enabled_if_changed,
        set_text_if_changed,
        set_tone_if_changed,
    )
    from .ui import (
        BatteryStatusSmoother,
        MASTER_CONSOLE_STYLESHEET,
        MetricTile,
        SectionCard,
        format_voltage_text,
        format_run_duration,
        format_on_off_text,
        format_status_text,
        join_status_fragments,
        set_tone,
    )
    from ..recorder_app.neural_recorder_main_ui import TimelineWidget
    from ..support.path_utils import get_application_directory
    from ..storage.runtime_cache import SlotRuntimeCache, read_bytes, read_json
    from ..widgets.shared_ui_sections import CompactSystemLogView
    from ..services.slot_service import run_slot_service
    from ..support.windows_runtime import apply_windows_process_role, configure_qt_runtime
    from ..services.controllers import normalize_mode3_reref_mode
except ImportError:
    from monitoring.alarming import (
        ALARM_CONFIG_FILENAME,
        build_card_position,
        evaluate_slot_alarm_conditions,
        load_alarm_config,
        send_alarm_email,
    )
    from services.health_metrics import classify_packet_loss_state
    from hardware.camera_module import get_available_cameras
    from master_app.contracts import (
        MASTER_CONFIG_FILENAME,
        SlotConfig,
        extract_mouse_id_from_directory,
        load_master_console_config,
        make_slot_command,
    )
    from master_app.battery_timeline import build_battery_timeline_payload
    from master_app.runtime_config import load_runtime_config
    from master_app.protocol_flow_dialog import ProtocolFlowDialog
    from master_app.card_layout import (
        CollapsibleSectionCard,
        CompactActionBar,
        StatusChipGrid,
        set_checked_if_changed,
        set_enabled_if_changed,
        set_text_if_changed,
        set_tone_if_changed,
    )
    from master_app.ui import (
        BatteryStatusSmoother,
        MASTER_CONSOLE_STYLESHEET,
        MetricTile,
        SectionCard,
        format_voltage_text,
        format_run_duration,
        format_on_off_text,
        format_status_text,
        join_status_fragments,
        set_tone,
    )
    from recorder_app.neural_recorder_main_ui import TimelineWidget
    from support.path_utils import get_application_directory
    from storage.runtime_cache import SlotRuntimeCache, read_bytes, read_json
    from widgets.shared_ui_sections import CompactSystemLogView
    from services.slot_service import run_slot_service
    from support.windows_runtime import apply_windows_process_role, configure_qt_runtime
    from services.controllers import normalize_mode3_reref_mode


MODE_OPTIONS = [
    ("Mode0 LFP+MAND+Raw", 0),
    ("single channel Spike", 1),
    ("16 channels Spike", 2),
    ("ESA&MUA", 3),
]
MODE_LABEL_TO_VALUE = {
    str(label).strip().lower(): int(value) for label, value in MODE_OPTIONS
}
MODE_LABEL_TO_VALUE.update({
    "16 channels lfp": 0,
    "lfp": 0,
    "mode0": 0,
    "mode0 lfp": 0,
    "mode0 lfp+mand": 0,
    "4 channels spike": 2,
    "4 channel spike": 2,
    "4channelsspike": 2,
    "16channelsspike": 2,
})

SAVE_OPTIONS = [
    ("Mode0 LFP+MAND+Raw", "lfp"),
    ("single channel Spike", "mode1"),
    ("16 channels Spike", "mode2"),
    ("ESA&MUA", "mode3"),
]
SAVE_MODE_LABELS = {str(value): str(label) for label, value in SAVE_OPTIONS}


def _save_mode_display_label(mode_key: object, save_route: Optional[Dict[str, object]] = None) -> str:
    normalized = str(mode_key or "").strip()
    if not normalized:
        return "OFF"
    label = SAVE_MODE_LABELS.get(normalized, normalized)
    route = save_route if isinstance(save_route, dict) else {}
    path_role = str(route.get("path_role", "") or "").strip().lower()
    data_mode = str(route.get("data_mode", "") or "").strip().lower()
    if normalized == "lfp" and data_mode == "mode0" and path_role == "mode3_training":
        return f"{label} -> Mode3 folder"
    return label

IMU_MODE_OPTIONS = [
    ("low-power", 1),
    ("Accel", 2),
    ("Accel+gyro", 3),
]

SPIKE_FILTER_SAMPLE_RATE_OPTIONS = [
    ("12500 Hz", 12500),
    ("20000 Hz", 20000),
]

MODE3_REREF_OPTIONS = [
    ("ReRef: OFF", 0),
    ("ReRef: FAST", 1),
    ("ReRef: STABLE", 2),
]

RELAY_CHANNEL_OPTIONS = [84, 78, 67, 50, 33, 17, 2]

ESB_TX_POWER_OPTIONS = [
    ("0 dBm", 0),
    ("4 dBm", 1),
    ("-4 dBm", 2),
    ("-8 dBm", 3),
    ("-12 dBm", 4),
    ("-16 dBm", 5),
    ("-20 dBm", 6),
    ("-40 dBm", 7),
]

ESB_TX_POWER_DEFAULT_BY_MODE = {
    0: 0,
    1: 0,
    2: 0,
    3: 0,
}

ESB_RETRANSMIT_DEFAULT_BY_MODE = {
    0: 1,
    1: 3,
    2: 1,
    3: 3,
}

ESB_ACK_WINDOW_MIN_US = 450
ESB_ACK_WINDOW_MAX_US = 4000
ESB_ACK_WINDOW_DEFAULT_US = 1200
ESB_ACK_WINDOW_LOW_POWER_US = 450
ESB_ACK_WINDOW_DEFAULT_BY_MODE = {
    0: ESB_ACK_WINDOW_DEFAULT_US,
    1: ESB_ACK_WINDOW_DEFAULT_US,
    2: ESB_ACK_WINDOW_DEFAULT_US,
    3: ESB_ACK_WINDOW_DEFAULT_US,
}
ESB_NOACK_DEFAULT_BY_MODE = {
    0: 0,
    1: 0,
    2: 0,
    3: 0,
}

MODE3_REREF_LABELS = {
    0: "Mode0/3 ReRef: OFF",
    1: "Mode0/3 ReRef: FAST",
    2: "Mode0/3 ReRef: STABLE",
}

IMU_MODE_LABELS = {
    1: "low-power",
    2: "Accel",
    3: "Accel+gyro",
}


def _state_to_tone(state: str) -> str:
    lowered = str(state or "").strip().lower()
    if lowered in {"ready", "ok"}:
        return "ok"
    if lowered in {"degraded", "initializing", "starting"}:
        return "warning"
    if lowered in {"failed", "error"}:
        return "danger"
    return "neutral"


def _state_explanation(status: Dict[str, object]) -> str:
    state = str(status.get("state", "unknown") or "unknown").strip().lower()
    rf_state = str(status.get("rf", {}).get("connection_state", "idle") or "idle").strip().lower()
    rf_reason = str(status.get("rf", {}).get("last_failure_reason", "") or "").strip()
    camera_state = str(status.get("camera", {}).get("health_state", "ok") or "ok").strip().lower()
    last_error = str(status.get("last_error", "") or "").strip()
    if state == "ready":
        return "Ready: 当前 cage 的主进程正常运行，关键设备没有处于自动恢复或失败状态。"
    if state == "degraded":
        detail = []
        if rf_state == "port_missing":
            detail.append(f"RF 端口已从系统消失{f'：{rf_reason}' if rf_reason else ''}")
        elif rf_state == "device_unresponsive":
            detail.append(f"RF 设备无响应{f'：{rf_reason}' if rf_reason else ''}")
        elif rf_state in {"recovering", "reconnecting"}:
            detail.append(f"RF 正在自动重连{f'：{rf_reason}' if rf_reason else ''}")
        if camera_state == "recovering":
            detail.append("Camera 正在自动恢复")
        suffix = "；".join(detail) if detail else "至少一个子系统正在恢复中"
        return f"Degraded: 后台仍在运行，但 {suffix}。采集和保存优先级仍然保留。"
    if state == "failed":
        return "Failed: RF 或 Camera 进入失败状态，需要人工检查硬件连接或端口。"
    if state == "error":
        return f"Error: 后端记录到错误。{last_error or '请查看 System Log。'}"
    return "Unknown: 还没有收到完整状态，或者进程正在启动。"


def _recovery_tone(rf_state: str, camera_state: str) -> str:
    rf_lower = str(rf_state or "").strip().lower()
    camera_lower = str(camera_state or "").strip().lower()
    if "failed" in {rf_lower, camera_lower}:
        return "danger"
    if rf_lower in {"recovering", "reconnecting", "port_missing", "device_unresponsive"} or camera_lower == "recovering":
        return "warning"
    return "ok"


def _set_button_tone(button: QPushButton, tone: str) -> None:
    set_tone_if_changed(button, tone)


def _mode3_reref_label(mode: int) -> str:
    normalized = normalize_mode3_reref_mode(mode, fallback=0)
    return MODE3_REREF_LABELS.get(normalized, MODE3_REREF_LABELS[0])


def _mode3_reref_tone(mode: int) -> str:
    normalized = normalize_mode3_reref_mode(mode, fallback=0)
    if normalized == 2:
        return "success"
    if normalized == 1:
        return "accent"
    return "danger"


def _normalize_imu_mode(mode: object) -> int:
    try:
        normalized = int(mode)
    except Exception:
        normalized = 1
    if normalized not in IMU_MODE_LABELS:
        normalized = 1
    return normalized


def _imu_mode_label(mode: object) -> str:
    return IMU_MODE_LABELS.get(_normalize_imu_mode(mode), IMU_MODE_LABELS[1])


def _coerce_nonnegative_float(value: object) -> float:
    try:
        return max(0.0, float(value or 0.0))
    except Exception:
        return 0.0


def _format_read_batch_packets(value: object) -> str:
    numeric = _coerce_nonnegative_float(value)
    return f"{numeric:.0f} pkts"


def _read_batch_pressure_tone(packet_count: object, warn_count: object, redline_count: object, force_danger: bool = False) -> str:
    if force_danger:
        return "danger"
    packets = _coerce_nonnegative_float(packet_count)
    warn = _coerce_nonnegative_float(warn_count)
    redline = _coerce_nonnegative_float(redline_count)
    if redline > 0.0 and packets >= redline:
        return "danger"
    if warn > 0.0 and packets >= warn:
        return "warning"
    if packets > 0.0:
        return "ok"
    return "neutral"


def _read_batch_pressure_tooltip(mode_key: str, packets: object, warn_count: object, redline_count: object, bytes_value: object) -> str:
    packet_value = _coerce_nonnegative_float(packets)
    warn_value = int(_coerce_nonnegative_float(warn_count))
    redline_value = int(_coerce_nonnegative_float(redline_count))
    byte_value = int(_coerce_nonnegative_float(bytes_value))
    return (
        f"{mode_key.upper()} read-all pressure: {packet_value:.0f} parsed packet(s)\n"
        f"Warning: >= {warn_value} pkts | Redline: >= {redline_value} pkts\n"
        "Relay CDC FIFO redline source: CONFIG_USB_CDC_ACM_RINGBUF_SIZE=30000 bytes; "
        f"byte EWMA kept for diagnostics: {byte_value} B"
    )


def _format_epoch_clock(epoch: object) -> str:
    try:
        value = float(epoch or 0.0)
    except Exception:
        value = 0.0
    if value <= 0:
        return "--"
    try:
        return datetime.fromtimestamp(value).strftime("%H:%M:%S")
    except Exception:
        return "--"


@dataclass
class SlotHandle:
    slot_config: SlotConfig
    runtime_cache: SlotRuntimeCache
    command_queue: mp.Queue
    service_process: mp.Process
    priority_command_queue: Optional[mp.Queue] = None


def _is_priority_slot_command(command: Dict[str, object]) -> bool:
    if not isinstance(command, dict):
        return False
    command_type = str(command.get("type", "") or "").strip()
    if command_type in {"shutdown", "detail_attach"}:
        return True
    if command_type in {"connect_subsystem", "disconnect_subsystem"}:
        subsystem = str(command.get("subsystem", "") or "").strip().lower()
        return subsystem in {"neural", "habits", "rf"}
    return command_type in {
        "rf_query",
        "rf_power",
        "habits_serial",
        "habits_sd_download",
        "habits_pause_toggle",
        "habits_read_all",
        "habits_time_sync",
        "set_habits_data_directory",
        "set_habits_start_date",
    }


class CameraPreviewWindow(QWidget):
    closed = pyqtSignal()

    def __init__(self, slot_label: str, runtime_cache: SlotRuntimeCache, poll_interval_ms: int = 120):
        super().__init__()
        self.runtime_cache = runtime_cache
        self._last_sequence = -1
        self.setWindowTitle(f"{slot_label} Camera Preview")
        self.resize(860, 620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.status_label = QLabel("Waiting for camera preview...")
        self.status_label.setObjectName("InlineLabel")
        layout.addWidget(self.status_label)

        self.detection_label = QLabel("Charging Guard: not running")
        self.detection_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detection_label.setMinimumHeight(28)
        self.detection_label.setStyleSheet(
            "background-color: #111827; color: #E5E7EB; border-radius: 10px; padding: 6px;"
        )
        layout.addWidget(self.detection_label)

        self.preview_label = QLabel("Preview paused")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumHeight(500)
        self.preview_label.setStyleSheet(
            "background: #0F172A; color: #E2E8F0; border-radius: 16px; border: 1px solid #1E293B;"
        )
        layout.addWidget(self.preview_label, 1)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_preview)
        self.poll_timer.start(max(30, int(poll_interval_ms)))

    def _poll_preview(self) -> None:
        meta = self.runtime_cache.read_camera_preview_meta({})
        if not meta or not bool(meta.get("enabled", False)):
            self.status_label.setText("Camera preview is off. Open camera and press Show Camera.")
            self.detection_label.setText("Charging Guard: not running")
            self.preview_label.setText("Preview paused")
            self.preview_label.setPixmap(QPixmap())
            return

        sequence = int(meta.get("sequence", -1) or -1)
        camera_id = meta.get("camera_id")
        charging_guard = meta.get("charging_guard", {})
        detection_text = str(
            (charging_guard or {}).get("detection_text", "Charging Guard: not running")
            or "Charging Guard: not running"
        )
        self.status_label.setText(f"Camera {camera_id if camera_id is not None else '--'} preview")
        self.detection_label.setText(detection_text)
        guard_status = str((charging_guard or {}).get("status_text", "Off") or "Off").lower()
        if "warning" in guard_status:
            self.detection_label.setStyleSheet(
                "background-color: #7F1D1D; color: #FEE2E2; border-radius: 10px; padding: 6px; font-weight: 700;"
            )
        elif bool((charging_guard or {}).get("mouse_present", False)):
            self.detection_label.setStyleSheet(
                "background-color: #14532D; color: #DCFCE7; border-radius: 10px; padding: 6px; font-weight: 600;"
            )
        else:
            self.detection_label.setStyleSheet(
                "background-color: #111827; color: #E5E7EB; border-radius: 10px; padding: 6px;"
            )
        if sequence <= self._last_sequence:
            return
        self._last_sequence = sequence

        payload = read_bytes(self.runtime_cache.camera_preview_frame_path, b"")
        if not payload:
            self.preview_label.setText("Waiting for frames...")
            self.preview_label.setPixmap(QPixmap())
            return

        pixmap = QPixmap()
        if not pixmap.loadFromData(payload, "JPG"):
            self.preview_label.setText("Preview frame decode failed")
            self.preview_label.setPixmap(QPixmap())
            return
        scaled = pixmap.scaled(
            self.preview_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview_label.setPixmap(scaled)
        self.preview_label.setText("")

    def resizeEvent(self, event) -> None:
        self._last_sequence = -1
        super().resizeEvent(event)

    def closeEvent(self, event) -> None:
        try:
            self.poll_timer.stop()
        except Exception:
            pass
        self.closed.emit()
        super().closeEvent(event)


class SlotCard(QFrame):
    def __init__(self, slot_config: SlotConfig, command_sender, detail_toggle, camera_catalog_refresh, camera_preview_toggle):
        super().__init__()
        self.slot_config = slot_config
        self.command_sender = command_sender
        self.detail_toggle = detail_toggle
        self.camera_catalog_refresh = camera_catalog_refresh
        self.camera_preview_toggle = camera_preview_toggle
        self.current_status: Dict[str, object] = {}
        self._last_log_mtime = 0.0
        self._last_timeline_mtime = 0.0
        self._last_timeline_live_epoch = 0.0
        self._last_log_refresh_epoch = 0.0
        self._last_timeline_refresh_epoch = 0.0
        self.system_log_refresh_s = 2.0
        self.timeline_refresh_s = 5.0
        self._timeline_has_records = False
        self._camera_catalog: List[Dict[str, object]] = []
        self._mode_combo_syncing = False
        self._pending_sample_mode: Optional[int] = None
        self._last_status_signature = ""
        self._control_dirty: Dict[str, bool] = {
            "imu_mode": False,
            "reward": False,
            "light": False,
            "protocol": False,
            "inter_block": False,
            "habits_start_date": False,
            "spike_filter_enabled": False,
            "spike_filter_low_cut_hz": False,
            "spike_filter_high_cut_hz": False,
            "spike_filter_sample_rate_hz": False,
            "rc_series_resistor_kohm": False,
            "rc_shunt_cap_pf": False,
            "mode0_quant_bits": False,
            "mode0_quant_full_scale_uv": False,
        }
        self._pending_control_values: Dict[str, object] = {}
        self._programmatic_control_keys = set()
        self._last_habits_read_all_seq = 0
        self._protocol_flow_dialog = None
        self._battery_status_smoother = BatteryStatusSmoother(confirm_samples=3)
        self.setObjectName("SlotCard")
        self.setMinimumWidth(520)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(14)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(10)

        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        self.title_label = QLabel(self.slot_config.label)
        self.title_label.setObjectName("CardTitle")
        title_layout.addWidget(self.title_label)

        mouse_layout = QHBoxLayout()
        mouse_layout.setSpacing(8)
        self.mouse_caption_label = QLabel("Training Mouse")
        self.mouse_caption_label.setObjectName("MouseBadgeCaption")
        mouse_layout.addWidget(self.mouse_caption_label, alignment=Qt.AlignmentFlag.AlignVCenter)
        self.mouse_id_badge = QLabel("Mouse --")
        self.mouse_id_badge.setObjectName("MouseBadgeValue")
        mouse_layout.addWidget(self.mouse_id_badge, alignment=Qt.AlignmentFlag.AlignVCenter)
        mouse_layout.addStretch(1)
        title_layout.addLayout(mouse_layout)

        self.profile_label = QLabel(self.slot_config.profile_path)
        self.profile_label.setObjectName("CardSubtitle")
        self.profile_label.setWordWrap(True)
        title_layout.addWidget(self.profile_label)
        header_layout.addLayout(title_layout, stretch=1)

        pills_layout = QHBoxLayout()
        pills_layout.setSpacing(8)
        self.state_pill = QLabel("Starting")
        self.state_pill.setObjectName("StatusPill")
        set_tone(self.state_pill, "warning")
        pills_layout.addWidget(self.state_pill)

        self.chart_pill = QLabel("Charts Off")
        self.chart_pill.setObjectName("StatusPill")
        set_tone(self.chart_pill, "neutral")
        pills_layout.addWidget(self.chart_pill)
        header_layout.addLayout(pills_layout)

        self.detail_toggle_btn = QPushButton("Open Charts")
        self.detail_toggle_btn.setCheckable(True)
        _set_button_tone(self.detail_toggle_btn, "accent")
        header_layout.addWidget(self.detail_toggle_btn)

        self.reload_btn = QPushButton("Reload Profile")
        _set_button_tone(self.reload_btn, "ghost")
        header_layout.addWidget(self.reload_btn)

        root.addLayout(header_layout)

        metrics_layout = QGridLayout()
        metrics_layout.setHorizontalSpacing(10)
        metrics_layout.setVerticalSpacing(10)

        self.devices_tile = MetricTile("Devices", "--")
        self.recording_tile = MetricTile("Acquisition", "--")
        self.battery_tile = MetricTile("Battery", "--")
        self.wireless_tile = MetricTile("Wireless", "--")
        self.mode0_power_tile = MetricTile("Mode0 Power", "-- mW")
        self.habits_tile = MetricTile("Habits", "--")
        self.recovery_tile = MetricTile("Recovery", "--")
        self.read_mode0_tile = MetricTile("Read M0", "-- pkts")
        self.read_mode1_tile = MetricTile("Read M1", "-- pkts")
        self.read_mode2_tile = MetricTile("Read M2", "-- pkts")
        self.read_mode3_tile = MetricTile("Read M3", "-- pkts")

        metrics_layout.addWidget(self.devices_tile, 0, 0)
        metrics_layout.addWidget(self.recording_tile, 0, 1)
        metrics_layout.addWidget(self.battery_tile, 1, 0)
        metrics_layout.addWidget(self.wireless_tile, 1, 1)
        metrics_layout.addWidget(self.mode0_power_tile, 2, 0)
        metrics_layout.addWidget(self.habits_tile, 2, 1)
        metrics_layout.addWidget(self.recovery_tile, 3, 0)
        metrics_layout.addWidget(self.read_mode0_tile, 3, 1)
        metrics_layout.addWidget(self.read_mode1_tile, 4, 0)
        metrics_layout.addWidget(self.read_mode2_tile, 4, 1)
        metrics_layout.addWidget(self.read_mode3_tile, 5, 0)
        root.addLayout(metrics_layout)

        self.status_chip_grid = StatusChipGrid(columns=3)
        root.addWidget(self.status_chip_grid)

        self.error_banner = QLabel("Last error: -")
        self.error_banner.setObjectName("ErrorBanner")
        self.error_banner.setWordWrap(True)
        root.addWidget(self.error_banner)

        connections_section = CollapsibleSectionCard(
            "Primary Actions",
            "Daily controls stay visible here; low-frequency settings are folded below.",
            expanded=True,
        )
        connections_grid = QGridLayout()
        connections_grid.setHorizontalSpacing(8)
        connections_grid.setVerticalSpacing(8)

        self.neural_connect_btn = QPushButton("Connect Neural")
        _set_button_tone(self.neural_connect_btn, "success")
        self.neural_disconnect_btn = QPushButton("Disconnect Neural")
        self.neural_disconnect_btn.hide()
        _set_button_tone(self.neural_disconnect_btn, "ghost")
        self.rf_connect_btn = QPushButton("Connect RF")
        _set_button_tone(self.rf_connect_btn, "success")
        self.rf_disconnect_btn = QPushButton("Disconnect RF")
        self.rf_disconnect_btn.hide()
        _set_button_tone(self.rf_disconnect_btn, "ghost")
        self.habits_connect_btn = QPushButton("Connect Habits")
        _set_button_tone(self.habits_connect_btn, "success")
        self.camera_id_label = QLabel("Camera ID")
        self.camera_id_label.setObjectName("InlineLabel")
        self.camera_combo = QComboBox()
        self.camera_combo.setMinimumWidth(180)
        self.camera_refresh_btn = QPushButton("Refresh Camera IDs")
        _set_button_tone(self.camera_refresh_btn, "ghost")
        self.camera_open_btn = QPushButton("Open Camera")
        _set_button_tone(self.camera_open_btn, "ghost")
        self.camera_show_btn = QPushButton("Show Camera")
        self.camera_show_btn.setCheckable(True)
        _set_button_tone(self.camera_show_btn, "accent")
        self.camera_record_btn = QPushButton("Start Recording")
        _set_button_tone(self.camera_record_btn, "accent")
        self.charging_guard_checkbox = QCheckBox("Charging Guard")
        self.charging_guard_config_btn = QPushButton("Config")
        _set_button_tone(self.charging_guard_config_btn, "ghost")
        self.camera_status_label = QLabel("Camera: not selected")
        self.camera_status_label.setObjectName("SectionDescription")
        self.camera_status_label.setWordWrap(True)
        self.camera_status_label.setTextFormat(Qt.TextFormat.RichText)
        self.charging_guard_status_label = QLabel("Guard: Off")
        self.charging_guard_status_label.setObjectName("SectionDescription")
        self.charging_guard_status_label.setWordWrap(True)
        self.charging_guard_progress_label = QLabel("Hits: 0/0")
        self.charging_guard_progress_label.setObjectName("SectionDescription")
        self.charging_guard_progress_label.setWordWrap(True)
        self.mouse_detection_status_label = QLabel("Mouse: --")
        self.mouse_detection_status_label.setObjectName("SectionDescription")
        self.mouse_detection_status_label.setWordWrap(True)

        connections_grid.addWidget(self.neural_connect_btn, 0, 0)
        connections_grid.addWidget(self.rf_connect_btn, 0, 1)
        connections_grid.addWidget(self.habits_connect_btn, 0, 2)
        connections_grid.addWidget(self.camera_open_btn, 0, 3)
        connections_grid.addWidget(self.camera_show_btn, 1, 0)
        connections_grid.addWidget(self.camera_record_btn, 1, 1)
        connections_grid.addWidget(self.camera_id_label, 1, 2)
        connections_grid.addWidget(self.camera_combo, 1, 3)
        connections_grid.addWidget(self.camera_refresh_btn, 2, 3)
        connections_grid.addWidget(self.charging_guard_checkbox, 2, 0)
        connections_grid.addWidget(self.charging_guard_config_btn, 2, 1)
        connections_grid.addWidget(self.charging_guard_status_label, 2, 2)
        connections_grid.addWidget(self.charging_guard_progress_label, 3, 0, 1, 2)
        connections_grid.addWidget(self.mouse_detection_status_label, 3, 2, 1, 2)
        connections_grid.addWidget(self.camera_status_label, 4, 0, 1, 4)
        connections_section.body_layout.addLayout(connections_grid)
        root.addWidget(connections_section)

        acquisition_section = CollapsibleSectionCard("Acquisition", expanded=True)
        acquisition_grid = QGridLayout()
        acquisition_grid.setHorizontalSpacing(8)
        acquisition_grid.setVerticalSpacing(8)

        sample_mode_label = QLabel("Sample Mode")
        sample_mode_label.setObjectName("InlineLabel")
        self.mode_combo = QComboBox()
        for label, value in MODE_OPTIONS:
            self.mode_combo.addItem(label, value)
        self.mode_combo.setToolTip("Choose the target sample mode to send. The actual device mode is shown separately below.")
        self.sample_start_btn = QPushButton("Start Sampling")
        _set_button_tone(self.sample_start_btn, "accent")
        self.sample_stop_btn = QPushButton("Stop Sampling")
        self.sample_stop_btn.hide()
        _set_button_tone(self.sample_stop_btn, "danger")
        current_mode_label = QLabel("Current Mode")
        current_mode_label.setObjectName("InlineLabel")
        self.sample_mode_status_label = QLabel("Current: Idle")
        self.sample_mode_status_label.setObjectName("SectionDescription")
        self.sample_mode_status_label.setWordWrap(True)
        self.sample_mode_status_label.setTextFormat(Qt.TextFormat.RichText)

        save_mode_label = QLabel("Save Track")
        save_mode_label.setObjectName("InlineLabel")
        self.save_combo = QComboBox()
        for label, value in SAVE_OPTIONS:
            self.save_combo.addItem(label, value)
        self.save_on_btn = QPushButton("Save On")
        _set_button_tone(self.save_on_btn, "success")
        self.save_off_btn = QPushButton("Save Off")
        self.save_off_btn.hide()
        _set_button_tone(self.save_off_btn, "ghost")

        reref_label = QLabel("Mode0/3 ReRef")
        reref_label.setObjectName("InlineLabel")
        self.mode3_reref_button = QToolButton()
        self.mode3_reref_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.mode3_reref_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.mode3_reref_menu = QMenu(self.mode3_reref_button)
        self._mode3_reref_actions: Dict[int, QAction] = {}
        for label, value in MODE3_REREF_OPTIONS:
            action = self.mode3_reref_menu.addAction(label)
            action.triggered.connect(
                lambda _checked=False, mode=value: self._send_mode3_reref_command(int(mode))
            )
            self._mode3_reref_actions[int(value)] = action
        self.mode3_reref_button.setMenu(self.mode3_reref_menu)
        self._refresh_mode3_reref_button(mode=0, connected=False, pending=False, target_mode=0)
        self.global_save_toggle = QPushButton("Global Save On")
        self.global_save_toggle.setCheckable(True)
        self.global_save_toggle.setChecked(True)
        _set_button_tone(self.global_save_toggle, "success")

        imu_mode_label = QLabel("IMU Mode")
        imu_mode_label.setObjectName("InlineLabel")
        self.imu_mode_combo = QComboBox()
        for label, value in IMU_MODE_OPTIONS:
            self.imu_mode_combo.addItem(label, value)
        self.imu_mode_combo.setCurrentIndex(1)
        self.imu_apply_btn = QPushButton("Apply IMU Mode")
        _set_button_tone(self.imu_apply_btn, "accent")
        self.imu_status_label = QLabel("Current: Accel")
        self.imu_status_label.setObjectName("SectionDescription")
        self.imu_status_label.setWordWrap(True)
        self.imu_status_label.setTextFormat(Qt.TextFormat.RichText)

        acquisition_grid.addWidget(sample_mode_label, 0, 0)
        acquisition_grid.addWidget(self.mode_combo, 0, 1)
        acquisition_grid.addWidget(self.sample_start_btn, 0, 2, 1, 2)
        acquisition_grid.addWidget(current_mode_label, 1, 0)
        acquisition_grid.addWidget(self.sample_mode_status_label, 1, 1, 1, 3)
        acquisition_grid.addWidget(save_mode_label, 2, 0)
        acquisition_grid.addWidget(self.save_combo, 2, 1)
        acquisition_grid.addWidget(self.save_on_btn, 2, 2, 1, 2)
        acquisition_grid.addWidget(reref_label, 3, 0)
        acquisition_grid.addWidget(self.mode3_reref_button, 3, 1)
        acquisition_grid.addWidget(self.global_save_toggle, 3, 2, 1, 2)
        acquisition_grid.addWidget(imu_mode_label, 4, 0)
        acquisition_grid.addWidget(self.imu_mode_combo, 4, 1)
        acquisition_grid.addWidget(self.imu_apply_btn, 4, 2)
        acquisition_grid.addWidget(self.imu_status_label, 4, 3)
        acquisition_section.body_layout.addLayout(acquisition_grid)
        root.addWidget(acquisition_section)

        signal_section = CollapsibleSectionCard("Advanced Signal", expanded=False)
        signal_grid = QGridLayout()
        signal_grid.setHorizontalSpacing(8)
        signal_grid.setVerticalSpacing(8)

        spike_filter_label = QLabel("Spike Filter")
        spike_filter_label.setObjectName("InlineLabel")
        self.spike_filter_checkbox = QCheckBox("Enable")
        self.spike_filter_low_spin = QDoubleSpinBox()
        self.spike_filter_low_spin.setRange(1.0, 9999.0)
        self.spike_filter_low_spin.setDecimals(1)
        self.spike_filter_low_spin.setSingleStep(10.0)
        self.spike_filter_low_spin.setValue(300.0)
        self.spike_filter_low_spin.setSuffix(" Hz")
        self.spike_filter_high_spin = QDoubleSpinBox()
        self.spike_filter_high_spin.setRange(10.0, 10000.0)
        self.spike_filter_high_spin.setDecimals(1)
        self.spike_filter_high_spin.setSingleStep(10.0)
        self.spike_filter_high_spin.setValue(3000.0)
        self.spike_filter_high_spin.setSuffix(" Hz")
        self.spike_filter_sample_rate_combo = QComboBox()
        for label, value in SPIKE_FILTER_SAMPLE_RATE_OPTIONS:
            self.spike_filter_sample_rate_combo.addItem(label, value)
        self.spike_filter_apply_btn = QPushButton("Apply Filter")
        _set_button_tone(self.spike_filter_apply_btn, "accent")
        self.spike_filter_hint_label = QLabel("Spike / LFP spectrum windows stay in the detail chart.")
        self.spike_filter_hint_label.setObjectName("SectionDescription")
        self.spike_filter_hint_label.setWordWrap(True)

        impedance_label = QLabel("Impedance RC")
        impedance_label.setObjectName("InlineLabel")
        self.impedance_series_spin = QDoubleSpinBox()
        self.impedance_series_spin.setRange(0.0, 10000.0)
        self.impedance_series_spin.setDecimals(2)
        self.impedance_series_spin.setSingleStep(1.0)
        self.impedance_series_spin.setValue(220.0)
        self.impedance_series_spin.setSuffix(" kOhm")
        self.impedance_shunt_spin = QDoubleSpinBox()
        self.impedance_shunt_spin.setRange(0.0, 100000.0)
        self.impedance_shunt_spin.setDecimals(2)
        self.impedance_shunt_spin.setSingleStep(10.0)
        self.impedance_shunt_spin.setValue(47.0)
        self.impedance_shunt_spin.setSuffix(" pF")
        self.impedance_apply_btn = QPushButton("Apply RC")
        _set_button_tone(self.impedance_apply_btn, "accent")
        self.impedance_hint_label = QLabel("Shared with the detail-view impedance panel and cached history metadata.")
        self.impedance_hint_label.setObjectName("SectionDescription")
        self.impedance_hint_label.setWordWrap(True)

        signal_grid.addWidget(spike_filter_label, 0, 0)
        signal_grid.addWidget(self.spike_filter_checkbox, 0, 1)
        signal_grid.addWidget(self.spike_filter_low_spin, 0, 2)
        signal_grid.addWidget(self.spike_filter_high_spin, 0, 3)
        signal_grid.addWidget(self.spike_filter_sample_rate_combo, 0, 4)
        signal_grid.addWidget(self.spike_filter_apply_btn, 0, 5)
        signal_grid.addWidget(self.spike_filter_hint_label, 1, 0, 1, 6)
        signal_grid.addWidget(impedance_label, 2, 0)
        signal_grid.addWidget(self.impedance_series_spin, 2, 1, 1, 2)
        signal_grid.addWidget(self.impedance_shunt_spin, 2, 3, 1, 2)
        signal_grid.addWidget(self.impedance_apply_btn, 2, 5)
        signal_grid.addWidget(self.impedance_hint_label, 3, 0, 1, 6)
        signal_section.body_layout.addLayout(signal_grid)
        root.addWidget(signal_section)

        progress_section = CollapsibleSectionCard("Save Progress", expanded=True)
        progress_grid = QGridLayout()
        progress_grid.setHorizontalSpacing(8)
        progress_grid.setVerticalSpacing(8)

        self.lfp_progress_label = QLabel(_save_mode_display_label("lfp"))
        self.lfp_progress_label.setObjectName("InlineLabel")
        self.lfp_progress_bar = QProgressBar()
        self.lfp_progress_bar.setRange(0, 100)
        self.lfp_progress_bar.setFormat("%p%")
        self.mode3_progress_label = QLabel(_save_mode_display_label("mode3"))
        self.mode3_progress_label.setObjectName("InlineLabel")
        self.mode3_progress_bar = QProgressBar()
        self.mode3_progress_bar.setRange(0, 100)
        self.mode3_progress_bar.setFormat("%p%")
        self.mode1_progress_label = QLabel(_save_mode_display_label("mode1"))
        self.mode1_progress_label.setObjectName("InlineLabel")
        self.mode1_progress_bar = QProgressBar()
        self.mode1_progress_bar.setRange(0, 100)
        self.mode1_progress_bar.setFormat("%p%")
        self.mode2_progress_label = QLabel(_save_mode_display_label("mode2"))
        self.mode2_progress_label.setObjectName("InlineLabel")
        self.mode2_progress_bar = QProgressBar()
        self.mode2_progress_bar.setRange(0, 100)
        self.mode2_progress_bar.setFormat("%p%")
        self.save_progress_summary = QLabel("Writer lag: 0 | Active save: -")
        self.save_progress_summary.setObjectName("SectionDescription")
        self.save_progress_summary.setWordWrap(True)

        progress_grid.addWidget(self.lfp_progress_label, 0, 0)
        progress_grid.addWidget(self.lfp_progress_bar, 0, 1)
        progress_grid.addWidget(self.mode3_progress_label, 1, 0)
        progress_grid.addWidget(self.mode3_progress_bar, 1, 1)
        progress_grid.addWidget(self.mode1_progress_label, 2, 0)
        progress_grid.addWidget(self.mode1_progress_bar, 2, 1)
        progress_grid.addWidget(self.mode2_progress_label, 3, 0)
        progress_grid.addWidget(self.mode2_progress_bar, 3, 1)
        progress_grid.addWidget(self.save_progress_summary, 4, 0, 1, 2)
        progress_section.body_layout.addLayout(progress_grid)
        root.addWidget(progress_section)

        relay_section = CollapsibleSectionCard("Relay / RF Maintenance", expanded=False)
        relay_grid = QGridLayout()
        relay_grid.setHorizontalSpacing(8)
        relay_grid.setVerticalSpacing(8)

        relay_channel_label = QLabel("Relay Channel")
        relay_channel_label.setObjectName("InlineLabel")
        self.relay_channel_combo = QComboBox()
        for channel in RELAY_CHANNEL_OPTIONS:
            self.relay_channel_combo.addItem(f"CH{channel}", channel)
        self.apply_relay_channel_btn = QPushButton("Apply ESB")
        _set_button_tone(self.apply_relay_channel_btn, "accent")
        self.restart_relay_btn = QPushButton("Restart Relay")
        _set_button_tone(self.restart_relay_btn, "danger")
        self.restart_device_btn = QPushButton("Restart Device")
        self.restart_device_btn.setToolTip("Reboot the animal-side recording firmware through the relay.")
        _set_button_tone(self.restart_device_btn, "danger")
        self.rf_on_btn = QPushButton("RF On")
        _set_button_tone(self.rf_on_btn, "success")
        self.rf_off_btn = QPushButton("RF Off")
        self.rf_off_btn.hide()
        _set_button_tone(self.rf_off_btn, "ghost")
        self.rf_query_btn = QPushButton("RF Query")
        _set_button_tone(self.rf_query_btn, "ghost")
        relay_grid.addWidget(relay_channel_label, 0, 0)
        relay_grid.addWidget(self.relay_channel_combo, 0, 1)
        relay_grid.addWidget(self.apply_relay_channel_btn, 0, 2)
        relay_grid.addWidget(self.restart_relay_btn, 0, 3)
        relay_grid.addWidget(self.rf_on_btn, 1, 0, 1, 2)
        relay_grid.addWidget(self.rf_query_btn, 1, 2)
        relay_grid.addWidget(self.restart_device_btn, 1, 3)

        esb_mode_header = QLabel("Mode ESB Config")
        esb_mode_header.setObjectName("InlineLabel")
        esb_tx_header = QLabel("TX Power")
        esb_tx_header.setObjectName("InlineLabel")
        esb_retx_header = QLabel("Retransmit")
        esb_retx_header.setObjectName("InlineLabel")
        esb_ack_header = QLabel("ACK/Retry Delay")
        esb_ack_header.setObjectName("InlineLabel")
        esb_noack_header = QLabel("NoACK")
        esb_noack_header.setObjectName("InlineLabel")
        relay_grid.addWidget(esb_mode_header, 2, 0)
        relay_grid.addWidget(esb_tx_header, 2, 1)
        relay_grid.addWidget(esb_retx_header, 2, 2)
        relay_grid.addWidget(esb_ack_header, 2, 3)
        relay_grid.addWidget(esb_noack_header, 2, 4)

        self.esb_tx_power_combos: Dict[int, QComboBox] = {}
        self.esb_retransmit_spins: Dict[int, QSpinBox] = {}
        self.esb_ack_window_spins: Dict[int, QSpinBox] = {}
        self.esb_noack_spins: Dict[int, QSpinBox] = {}
        self.esb_config_apply_buttons: Dict[int, QPushButton] = {}
        for mode in range(4):
            row = 3 + mode
            mode_label = QLabel(f"Mode{mode}")
            mode_label.setObjectName("InlineLabel")

            tx_power_combo = QComboBox()
            for tx_label, tx_code in ESB_TX_POWER_OPTIONS:
                tx_power_combo.addItem(tx_label, tx_code)
            default_tx_code = ESB_TX_POWER_DEFAULT_BY_MODE.get(mode, 0)
            default_tx_index = tx_power_combo.findData(default_tx_code)
            if default_tx_index >= 0:
                tx_power_combo.setCurrentIndex(default_tx_index)
            tx_power_combo.setToolTip(f"Animal firmware ESB TX power for Mode{mode}.")

            retransmit_spin = QSpinBox()
            retransmit_spin.setRange(0, 15)
            retransmit_spin.setValue(ESB_RETRANSMIT_DEFAULT_BY_MODE.get(mode, 3))
            retransmit_spin.setSuffix(" retx")
            retransmit_spin.setToolTip(f"Animal firmware ESB retransmit count for Mode{mode}.")

            ack_window_spin = QSpinBox()
            ack_window_spin.setRange(ESB_ACK_WINDOW_MIN_US, ESB_ACK_WINDOW_MAX_US)
            ack_window_spin.setSingleStep(50)
            ack_window_spin.setValue(ESB_ACK_WINDOW_DEFAULT_BY_MODE.get(mode, ESB_ACK_WINDOW_DEFAULT_US))
            ack_window_spin.setSuffix(" us")
            ack_window_spin.setToolTip(
                f"Animal firmware ESB ACK/retransmit delay for Mode{mode}; keep 1200 us unless retry telemetry stays low at a shorter value."
            )

            noack_spin = QSpinBox()
            noack_spin.setRange(0, 100)
            noack_spin.setSingleStep(5)
            noack_spin.setValue(ESB_NOACK_DEFAULT_BY_MODE.get(mode, 0))
            noack_spin.setSuffix("%")
            noack_spin.setToolTip(
                f"Percent of Mode{mode} data packets sent without ACK. 0% means all data packets request ACK; 100% means no data ACK."
            )

            apply_button = QPushButton(f"Apply M{mode}")
            apply_button.setToolTip(f"Apply TX power, retransmit count, ACK window, and noack ratio for Mode{mode}.")
            _set_button_tone(apply_button, "accent")

            self.esb_tx_power_combos[mode] = tx_power_combo
            self.esb_retransmit_spins[mode] = retransmit_spin
            self.esb_ack_window_spins[mode] = ack_window_spin
            self.esb_noack_spins[mode] = noack_spin
            self.esb_config_apply_buttons[mode] = apply_button

            relay_grid.addWidget(mode_label, row, 0)
            relay_grid.addWidget(tx_power_combo, row, 1)
            relay_grid.addWidget(retransmit_spin, row, 2)
            relay_grid.addWidget(ack_window_spin, row, 3)
            relay_grid.addWidget(noack_spin, row, 4)
            relay_grid.addWidget(apply_button, row, 5)

        mode0_quant_label = QLabel("Mode0 Quant")
        mode0_quant_label.setObjectName("InlineLabel")
        self.mode0_quant_bits_spin = QSpinBox()
        self.mode0_quant_bits_spin.setRange(8, 12)
        self.mode0_quant_bits_spin.setValue(12)
        self.mode0_quant_bits_spin.setSuffix(" bit")
        self.mode0_quant_bits_spin.setToolTip("Mode0 neural packed payload bit depth. 12-bit is the largest payload-safe setting.")
        self.mode0_quant_range_spin = QSpinBox()
        self.mode0_quant_range_spin.setRange(500, 10000)
        self.mode0_quant_range_spin.setSingleStep(500)
        self.mode0_quant_range_spin.setValue(1000)
        self.mode0_quant_range_spin.setSuffix(" uV")
        self.mode0_quant_range_spin.setToolTip("Mode0 signed LFP quantization full-scale range. Selected raw is fixed at +/-500 uV.")
        self.mode0_quant_apply_btn = QPushButton("Apply Quant")
        _set_button_tone(self.mode0_quant_apply_btn, "accent")
        relay_grid.addWidget(mode0_quant_label, 7, 0)
        relay_grid.addWidget(self.mode0_quant_bits_spin, 7, 1)
        relay_grid.addWidget(self.mode0_quant_range_spin, 7, 2)
        relay_grid.addWidget(self.mode0_quant_apply_btn, 7, 3)

        relay_section.body_layout.addLayout(relay_grid)
        root.addWidget(relay_section)

        habits_section = CollapsibleSectionCard("Habits Parameters", expanded=False)
        habits_connection_grid = QGridLayout()
        habits_connection_grid.setHorizontalSpacing(8)
        habits_connection_grid.setVerticalSpacing(8)

        self.habits_data_dir_title = QLabel("Data Directory")
        self.habits_data_dir_title.setObjectName("InlineLabel")
        self.habits_data_dir_label = QLabel("Not selected")
        self.habits_data_dir_label.setObjectName("SectionDescription")
        self.habits_data_dir_label.setWordWrap(True)
        self.habits_data_dir_btn = QPushButton("Select Data Directory")
        _set_button_tone(self.habits_data_dir_btn, "ghost")
        self.habits_start_date_label = QLabel("Start Date")
        self.habits_start_date_label.setObjectName("InlineLabel")
        self.habits_start_date_edit = QDateEdit()
        self.habits_start_date_edit.setCalendarPopup(True)
        self.habits_start_date_edit.setDate(QDate.currentDate())
        self.habits_sync_label = QLabel("Not synced")
        self.habits_sync_label.setObjectName("SectionDescription")
        self.habits_sync_label.setWordWrap(True)

        habits_connection_grid.addWidget(self.habits_data_dir_title, 0, 0)
        habits_connection_grid.addWidget(self.habits_data_dir_label, 0, 1, 1, 2)
        habits_connection_grid.addWidget(self.habits_data_dir_btn, 0, 3)
        habits_connection_grid.addWidget(self.habits_start_date_label, 1, 0)
        habits_connection_grid.addWidget(self.habits_start_date_edit, 1, 1)
        habits_connection_grid.addWidget(self.habits_sync_label, 1, 2, 1, 2)
        habits_section.body_layout.addLayout(habits_connection_grid)

        habits_quick_grid = QGridLayout()
        habits_quick_grid.setHorizontalSpacing(8)
        habits_quick_grid.setVerticalSpacing(8)

        self.habits_pause_btn = QPushButton("Pause Habits")
        _set_button_tone(self.habits_pause_btn, "ghost")
        self.habits_read_all_btn = QPushButton("Read All Values")
        _set_button_tone(self.habits_read_all_btn, "accent")
        self.habits_read_all_btn.setMinimumHeight(40)
        self.habits_read_all_btn.setToolTip("Read back the full Habits device state and refresh all related UI values.")
        self.habits_time_btn = QPushButton("Sync Time")
        _set_button_tone(self.habits_time_btn, "ghost")
        self.habits_handshake_btn = QPushButton("Handshake")
        _set_button_tone(self.habits_handshake_btn, "ghost")
        self.habits_cap_btn = QPushButton("Refresh Cap Baseline")
        _set_button_tone(self.habits_cap_btn, "ghost")
        self.protocol_flow_btn = QPushButton("Protocol Flow")
        _set_button_tone(self.protocol_flow_btn, "accent")
        self.block_switch_btn = QPushButton("BlockSwitch Disabled")
        _set_button_tone(self.block_switch_btn, "ghost")
        self.block_switch_btn.setEnabled(False)
        self.block_switch_btn.setVisible(False)
        self.post_training_btn = QPushButton("Enter Post-Training P0")
        _set_button_tone(self.post_training_btn, "ghost")

        habits_quick_grid.addWidget(self.habits_pause_btn, 0, 0)
        habits_quick_grid.addWidget(self.habits_read_all_btn, 0, 1, 1, 2)
        habits_quick_grid.addWidget(self.habits_time_btn, 0, 3)
        habits_quick_grid.addWidget(self.habits_handshake_btn, 1, 0)
        habits_quick_grid.addWidget(self.habits_cap_btn, 1, 1)
        habits_quick_grid.addWidget(self.protocol_flow_btn, 1, 2)
        habits_quick_grid.addWidget(self.post_training_btn, 1, 3)
        habits_section.body_layout.addLayout(habits_quick_grid)

        habits_config_grid = QGridLayout()
        habits_config_grid.setHorizontalSpacing(8)
        habits_config_grid.setVerticalSpacing(8)

        reward_label = QLabel("Reward L / M / R")
        reward_label.setObjectName("InlineLabel")
        self.reward_left_spin = QSpinBox()
        self.reward_left_spin.setRange(0, 999)
        self.reward_left_spin.setValue(30)
        self.reward_middle_spin = QSpinBox()
        self.reward_middle_spin.setRange(0, 999)
        self.reward_middle_spin.setValue(30)
        self.reward_right_spin = QSpinBox()
        self.reward_right_spin.setRange(0, 999)
        self.reward_right_spin.setValue(30)
        self.reward_apply_btn = QPushButton("Set Reward")
        _set_button_tone(self.reward_apply_btn, "accent")

        light_label = QLabel("Light Low / High")
        light_label.setObjectName("InlineLabel")
        self.light_low_spin = QSpinBox()
        self.light_low_spin.setRange(0, 255)
        self.light_low_spin.setValue(1)
        self.light_high_spin = QSpinBox()
        self.light_high_spin.setRange(0, 255)
        self.light_high_spin.setValue(255)
        self.light_apply_btn = QPushButton("Set Light")
        _set_button_tone(self.light_apply_btn, "accent")

        protocol_label = QLabel("Protocol")
        protocol_label.setObjectName("InlineLabel")
        self.protocol_spin = QSpinBox()
        self.protocol_spin.setRange(0, 10)
        self.protocol_apply_btn = QPushButton("Set P0-P10")
        _set_button_tone(self.protocol_apply_btn, "accent")
        ibi_label = QLabel("Inter-Block Minutes")
        ibi_label.setObjectName("InlineLabel")
        self.inter_block_spin = QSpinBox()
        self.inter_block_spin.setRange(0, 720)
        self.inter_block_spin.setSuffix(" min")
        self.inter_block_spin.setValue(60)
        self.inter_block_apply_btn = QPushButton("Set Block IBI")
        _set_button_tone(self.inter_block_apply_btn, "accent")

        habits_config_grid.addWidget(reward_label, 0, 0)
        habits_config_grid.addWidget(self.reward_left_spin, 0, 1)
        habits_config_grid.addWidget(self.reward_middle_spin, 0, 2)
        habits_config_grid.addWidget(self.reward_right_spin, 0, 3)
        habits_config_grid.addWidget(self.reward_apply_btn, 0, 4)
        habits_config_grid.addWidget(light_label, 1, 0)
        habits_config_grid.addWidget(self.light_low_spin, 1, 1)
        habits_config_grid.addWidget(self.light_high_spin, 1, 2)
        habits_config_grid.addWidget(self.light_apply_btn, 1, 3, 1, 2)
        habits_config_grid.addWidget(protocol_label, 2, 0)
        habits_config_grid.addWidget(self.protocol_spin, 2, 1)
        habits_config_grid.addWidget(self.protocol_apply_btn, 2, 2)
        habits_config_grid.addWidget(ibi_label, 2, 3)
        habits_config_grid.addWidget(self.inter_block_spin, 2, 4)
        habits_config_grid.addWidget(self.inter_block_apply_btn, 3, 3, 1, 2)
        habits_section.body_layout.addLayout(habits_config_grid)
        root.addWidget(habits_section)

        monitoring_section = CollapsibleSectionCard("Monitoring", expanded=True)

        trigger_gate_label = QLabel("Mode3 Trial Trigger Gate")
        trigger_gate_label.setObjectName("InlineLabel")
        monitoring_section.body_layout.addWidget(trigger_gate_label)
        self.trial_trigger_gate_label = QLabel("Trigger ready")
        self.trial_trigger_gate_label.setObjectName("SectionDescription")
        self.trial_trigger_gate_label.setTextFormat(Qt.TextFormat.RichText)
        self.trial_trigger_gate_label.setWordWrap(True)
        self.trial_trigger_gate_label.setStyleSheet(
            "background-color: #F0FDF4; color: #166534; border-radius: 8px; padding: 6px; font-weight: 700;"
        )
        monitoring_section.body_layout.addWidget(self.trial_trigger_gate_label)

        timeline_label = QLabel("24-Hour Timeline")
        timeline_label.setObjectName("InlineLabel")
        monitoring_section.body_layout.addWidget(timeline_label)
        self.timeline_widget = TimelineWidget()
        self.timeline_widget.setMinimumHeight(150)
        monitoring_section.body_layout.addWidget(self.timeline_widget)

        log_label = QLabel("System Log")
        log_label.setObjectName("InlineLabel")
        log_header_layout = QHBoxLayout()
        log_header_layout.addWidget(log_label)
        log_header_layout.addStretch(1)
        self.clear_system_log_btn = QPushButton("Clear System Log")
        _set_button_tone(self.clear_system_log_btn, "ghost")
        log_header_layout.addWidget(self.clear_system_log_btn)
        monitoring_section.body_layout.addLayout(log_header_layout)
        self.system_log_view = CompactSystemLogView()
        self.system_log_view.setMinimumHeight(170)
        monitoring_section.body_layout.addWidget(self.system_log_view)
        root.addWidget(monitoring_section)

        self.neural_connect_btn.clicked.connect(self._toggle_neural_connection)
        self.rf_connect_btn.clicked.connect(self._toggle_rf_connection)
        self.habits_connect_btn.clicked.connect(self._toggle_habits_connection)
        self.camera_combo.currentIndexChanged.connect(self._on_camera_selection_changed)
        self.camera_refresh_btn.clicked.connect(
            lambda: self.camera_catalog_refresh(self.slot_config.slot_id)
        )
        self.camera_open_btn.clicked.connect(self._toggle_camera_open)
        self.camera_show_btn.clicked.connect(self._toggle_camera_preview)
        self.camera_record_btn.clicked.connect(self._toggle_camera_record)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_combo_changed)
        self.sample_start_btn.clicked.connect(self._toggle_sampling)
        self.global_save_toggle.clicked.connect(self._on_global_save_toggled)
        self.imu_mode_combo.currentIndexChanged.connect(self._on_imu_mode_selection_changed)
        self.imu_apply_btn.clicked.connect(self._apply_imu_command)
        self.spike_filter_checkbox.toggled.connect(lambda _checked: self._mark_control_dirty("spike_filter_enabled"))
        self.spike_filter_low_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("spike_filter_low_cut_hz"))
        self.spike_filter_high_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("spike_filter_high_cut_hz"))
        self.spike_filter_sample_rate_combo.currentIndexChanged.connect(lambda _index: self._mark_control_dirty("spike_filter_sample_rate_hz"))
        self.spike_filter_apply_btn.clicked.connect(self._apply_spike_filter_command)
        self.impedance_series_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("rc_series_resistor_kohm"))
        self.impedance_shunt_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("rc_shunt_cap_pf"))
        self.impedance_apply_btn.clicked.connect(self._apply_impedance_compensation_command)
        self.save_on_btn.clicked.connect(self._toggle_save)
        self.apply_relay_channel_btn.clicked.connect(
            lambda: self._send(
                make_slot_command(
                    "apply_relay_esb_channel",
                    channel=self.relay_channel_combo.currentData(),
                )
            )
        )
        self.restart_relay_btn.clicked.connect(
            lambda: self._send(make_slot_command("restart_relay"))
        )
        self.restart_device_btn.clicked.connect(
            lambda: self._send(make_slot_command("restart_device"))
        )
        for mode, button in self.esb_config_apply_buttons.items():
            button.clicked.connect(
                lambda _checked=False, mode=mode: self._apply_device_esb_mode_config(mode)
            )
        self.mode0_quant_bits_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("mode0_quant_bits"))
        self.mode0_quant_range_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("mode0_quant_full_scale_uv"))
        self.mode0_quant_apply_btn.clicked.connect(self._apply_mode0_quant_config)
        self.rf_on_btn.clicked.connect(self._toggle_rf_power)
        self.rf_query_btn.clicked.connect(
            lambda: self._send(make_slot_command("rf_query"))
        )
        self.charging_guard_checkbox.toggled.connect(self._on_charging_guard_toggled)
        self.charging_guard_config_btn.clicked.connect(self._open_charging_guard_config_dialog)
        self.detail_toggle_btn.clicked.connect(self._toggle_detail_view)
        self.reload_btn.clicked.connect(
            lambda: self._send(make_slot_command("reload_profile"))
        )
        self.habits_data_dir_btn.clicked.connect(self._select_habits_data_directory)
        self.habits_start_date_edit.dateChanged.connect(self._on_habits_start_date_changed)
        self.reward_left_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("reward"))
        self.reward_middle_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("reward"))
        self.reward_right_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("reward"))
        self.light_low_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("light"))
        self.light_high_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("light"))
        self.protocol_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("protocol"))
        self.inter_block_spin.valueChanged.connect(lambda _value: self._mark_control_dirty("inter_block"))

        self.habits_pause_btn.clicked.connect(
            lambda: self._send(make_slot_command("habits_serial", action="pause_toggle"))
        )
        self.habits_read_all_btn.clicked.connect(
            lambda: self._send(make_slot_command("habits_serial", action="read_all"))
        )
        self.habits_time_btn.clicked.connect(
            lambda: self._send(make_slot_command("habits_serial", action="time_sync"))
        )
        self.habits_handshake_btn.clicked.connect(
            lambda: self._send(make_slot_command("habits_serial", action="handshake"))
        )
        self.habits_cap_btn.clicked.connect(
            lambda: self._send(make_slot_command("habits_serial", action="cap_snapshot"))
        )
        self.protocol_flow_btn.clicked.connect(self._open_protocol_flow_dialog)
        self.post_training_btn.clicked.connect(
            lambda: self._send(
                make_slot_command("habits_serial", action="post_training_protocol0")
            )
        )
        self.clear_system_log_btn.clicked.connect(
            lambda: self._send(make_slot_command("clear_system_log"))
        )
        self.reward_apply_btn.clicked.connect(self._apply_reward_command)
        self.light_apply_btn.clicked.connect(self._apply_light_command)
        self.protocol_apply_btn.clicked.connect(self._apply_protocol_command)
        self.inter_block_apply_btn.clicked.connect(self._apply_inter_block_command)

    def _send(self, command: Dict[str, object]) -> None:
        self.command_sender(self.slot_config.slot_id, command)

    def _open_protocol_flow_dialog(self) -> None:
        def send_habits_action(action: str, **payload: object) -> None:
            self._send(make_slot_command("habits_serial", action=action, **payload))

        if self._protocol_flow_dialog is not None and self._protocol_flow_dialog.isVisible():
            self._protocol_flow_dialog.raise_()
            self._protocol_flow_dialog.activateWindow()
            return
        dialog = ProtocolFlowDialog(
            self.current_status,
            send_habits_action,
            parent=self.window(),
        )
        self._protocol_flow_dialog = dialog
        dialog.finished.connect(lambda _result: setattr(self, "_protocol_flow_dialog", None))
        dialog.show()

    def _toggle_neural_connection(self) -> None:
        neural = self.current_status.get("neural", {})
        connected = bool(neural.get("connected")) if isinstance(neural, dict) else False
        self._send(
            make_slot_command(
                "disconnect_subsystem" if connected else "connect_subsystem",
                subsystem="neural",
            )
        )

    def _toggle_rf_connection(self) -> None:
        rf = self.current_status.get("rf", {})
        connected = bool(rf.get("connected")) if isinstance(rf, dict) else False
        self._send(
            make_slot_command(
                "disconnect_subsystem" if connected else "connect_subsystem",
                subsystem="rf",
            )
        )

    def _toggle_habits_connection(self) -> None:
        habits = self.current_status.get("habits", {})
        connected = bool(habits.get("connected")) if isinstance(habits, dict) else False
        self._send(
            make_slot_command(
                "disconnect_subsystem" if connected else "connect_subsystem",
                subsystem="habits",
            )
        )

    def _toggle_camera_open(self) -> None:
        camera = self.current_status.get("camera", {})
        is_open = bool(camera.get("open")) if isinstance(camera, dict) else False
        self._send(make_slot_command("camera_close" if is_open else "camera_open"))

    def _toggle_camera_record(self) -> None:
        camera = self.current_status.get("camera", {})
        is_recording = bool(camera.get("recording")) if isinstance(camera, dict) else False
        self._send(
            make_slot_command(
                "camera_record_stop" if is_recording else "camera_record_start"
            )
        )

    def _toggle_camera_preview(self, checked: bool) -> None:
        self._pending_control_values["camera_preview"] = bool(checked)
        self.camera_preview_toggle(self.slot_config.slot_id, bool(checked))

    def force_camera_preview_state(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._pending_control_values["camera_preview"] = enabled
        self._control_dirty["camera_preview"] = False
        self.camera_show_btn.blockSignals(True)
        self.camera_show_btn.setChecked(enabled)
        self.camera_show_btn.setText("Hide Camera" if enabled else "Show Camera")
        self.camera_show_btn.blockSignals(False)
        _set_button_tone(self.camera_show_btn, "danger" if enabled else "accent")

    def _toggle_sampling(self) -> None:
        neural = self.current_status.get("neural", {})
        sampling = bool(neural.get("sampling")) if isinstance(neural, dict) else False
        if sampling:
            self._send(make_slot_command("sample_stop"))
            return
        self._start_sampling_selected_mode()

    def _toggle_save(self) -> None:
        neural = self.current_status.get("neural", {})
        save_flags = neural.get("save_flags", {}) if isinstance(neural, dict) else {}
        selected_mode = str(self.save_combo.currentData() or "")
        selected_mode_saving = bool(save_flags.get(selected_mode, False)) if isinstance(save_flags, dict) else False
        self._send(
            make_slot_command(
                "set_save",
                mode=selected_mode,
                enabled=not selected_mode_saving,
            )
        )

    def _toggle_rf_power(self) -> None:
        rf = self.current_status.get("rf", {})
        power_state = str(rf.get("power_state", "unknown") or "unknown").strip().lower() if isinstance(rf, dict) else "unknown"
        self._send(make_slot_command("rf_power", enabled=(power_state != "on")))

    def _apply_device_esb_mode_config(self, mode: int) -> None:
        normalized_mode = int(mode)
        tx_power_combo = self.esb_tx_power_combos.get(normalized_mode)
        retransmit_spin = self.esb_retransmit_spins.get(normalized_mode)
        ack_window_spin = self.esb_ack_window_spins.get(normalized_mode)
        noack_spin = self.esb_noack_spins.get(normalized_mode)
        if tx_power_combo is None or retransmit_spin is None or ack_window_spin is None or noack_spin is None:
            return
        self._send(
            make_slot_command(
                "set_device_esb_mode_config",
                mode=normalized_mode,
                tx_power_code=int(tx_power_combo.currentData()),
                retransmit_count=int(retransmit_spin.value()),
                ack_window_us=int(ack_window_spin.value()),
                noack_percent=int(noack_spin.value()),
            )
        )

    def _apply_mode0_quant_config(self) -> None:
        full_scale_uv = int(round(float(self.mode0_quant_range_spin.value()) / 500.0) * 500)
        full_scale_uv = max(500, min(10000, full_scale_uv))
        if int(self.mode0_quant_range_spin.value()) != full_scale_uv:
            self.mode0_quant_range_spin.setValue(full_scale_uv)
        bit_depth = int(self.mode0_quant_bits_spin.value())
        self._pending_control_values["mode0_quant_bits"] = bit_depth
        self._pending_control_values["mode0_quant_full_scale_uv"] = full_scale_uv
        self._control_dirty["mode0_quant_bits"] = False
        self._control_dirty["mode0_quant_full_scale_uv"] = False
        self._send(
            make_slot_command(
                "set_mode0_quant_config",
                bit_depth=bit_depth,
                full_scale_uv=float(full_scale_uv),
            )
        )

    def _mark_control_dirty(self, key: str) -> None:
        if key in self._programmatic_control_keys:
            return
        self._control_dirty[str(key)] = True
        self._pending_control_values.pop(str(key), None)

    def _set_control_programmatic(self, key: str, callback) -> None:
        normalized = str(key)
        self._programmatic_control_keys.add(normalized)
        try:
            callback()
        finally:
            self._programmatic_control_keys.discard(normalized)

    @staticmethod
    def _combo_is_user_editing(combo: QComboBox) -> bool:
        try:
            if combo.hasFocus():
                return True
        except Exception:
            pass
        try:
            view = combo.view()
            if view is not None and view.isVisible():
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def _widget_is_user_editing(widget) -> bool:
        try:
            if widget.hasFocus():
                return True
        except Exception:
            pass
        try:
            line_edit = widget.lineEdit()
            if line_edit is not None and line_edit.hasFocus():
                return True
        except Exception:
            pass
        return False

    def _resolve_pending_value(self, key: str, reported_value):
        normalized = str(key)
        if normalized not in self._pending_control_values:
            return reported_value
        pending_value = self._pending_control_values.get(normalized)
        if pending_value == reported_value:
            self._pending_control_values.pop(normalized, None)
            self._control_dirty[normalized] = False
            return reported_value
        return pending_value

    def _sync_combo_control(self, key: str, combo: QComboBox, reported_value) -> None:
        normalized = str(key)
        if reported_value is None and normalized not in self._pending_control_values:
            return
        target_value = self._resolve_pending_value(normalized, reported_value)
        current_value = combo.currentData()
        editing = self._combo_is_user_editing(combo)
        if self._control_dirty.get(normalized, False):
            if current_value == reported_value and not editing:
                self._control_dirty[normalized] = False
            else:
                return
        if editing or current_value == target_value:
            return
        self._set_control_programmatic(
            normalized,
            lambda: self._set_combo_value(combo, target_value),
        )

    def _sync_spin_control(self, key: str, widget: QSpinBox, reported_value) -> None:
        normalized = str(key)
        if reported_value is None and normalized not in self._pending_control_values:
            return
        try:
            normalized_reported = int(reported_value)
        except Exception:
            if normalized not in self._pending_control_values:
                return
            normalized_reported = None
        target_value = self._resolve_pending_value(normalized, normalized_reported)
        editing = self._widget_is_user_editing(widget)
        current_value = int(widget.value())
        if self._control_dirty.get(normalized, False):
            if normalized_reported is not None and current_value == normalized_reported and not editing:
                self._control_dirty[normalized] = False
            else:
                return
        if editing or target_value is None or current_value == int(target_value):
            return
        self._set_control_programmatic(
            normalized,
            lambda: widget.setValue(int(target_value)),
        )

    @staticmethod
    def _effective_habits_protocol(habits: Dict[str, Any]) -> Any:
        if not isinstance(habits, dict):
            return 0
        return habits.get("protocol", habits.get("last_protocol", 0))

    def _sync_double_spin_control(self, key: str, widget: QDoubleSpinBox, reported_value) -> None:
        normalized = str(key)
        if reported_value is None and normalized not in self._pending_control_values:
            return
        try:
            normalized_reported = float(reported_value)
        except Exception:
            if normalized not in self._pending_control_values:
                return
            normalized_reported = None
        target_value = self._resolve_pending_value(normalized, normalized_reported)
        editing = self._widget_is_user_editing(widget)
        current_value = float(widget.value())
        if self._control_dirty.get(normalized, False):
            if normalized_reported is not None and abs(current_value - normalized_reported) < 1e-6 and not editing:
                self._control_dirty[normalized] = False
            else:
                return
        if editing or target_value is None or abs(current_value - float(target_value)) < 1e-6:
            return
        self._set_control_programmatic(
            normalized,
            lambda: widget.setValue(float(target_value)),
        )

    def _sync_checkbox_control(self, key: str, widget: QCheckBox, reported_value) -> None:
        normalized = str(key)
        if reported_value is None and normalized not in self._pending_control_values:
            return
        normalized_reported = bool(reported_value)
        target_value = bool(self._resolve_pending_value(normalized, normalized_reported))
        current_value = bool(widget.isChecked())
        if self._control_dirty.get(normalized, False):
            if current_value == normalized_reported:
                self._control_dirty[normalized] = False
            else:
                return
        if current_value == target_value:
            return
        self._set_control_programmatic(
            normalized,
            lambda: widget.setChecked(bool(target_value)),
        )

    def _sync_spin_group_control(self, key: str, controls_and_values) -> None:
        normalized = str(key)
        try:
            reported_values = tuple(int(value) for _widget, value in controls_and_values)
        except Exception:
            if normalized not in self._pending_control_values:
                return
            reported_values = None
        target_values = self._resolve_pending_value(normalized, reported_values)
        widgets = [widget for widget, _value in controls_and_values]
        current_values = tuple(int(widget.value()) for widget in widgets)
        editing = any(self._widget_is_user_editing(widget) for widget in widgets)
        if self._control_dirty.get(normalized, False):
            if reported_values is not None and current_values == reported_values and not editing:
                self._control_dirty[normalized] = False
            else:
                return
        if editing or target_values is None or current_values == tuple(int(value) for value in target_values):
            return

        def _apply_group() -> None:
            for widget, value in zip(widgets, list(target_values)):
                widget.setValue(int(value))

        self._set_control_programmatic(normalized, _apply_group)

    def _clear_habits_config_edit_guards(self) -> None:
        for key in ("reward", "light", "protocol", "inter_block"):
            self._control_dirty[key] = False
            self._pending_control_values.pop(key, None)

    def _sync_date_control(self, key: str, widget: QDateEdit, date_text: str) -> None:
        normalized = str(key)
        normalized_reported = str(date_text or "").strip()
        if not normalized_reported and normalized not in self._pending_control_values:
            return
        target_text = str(self._resolve_pending_value(normalized, normalized_reported) or "").strip()
        current_text = widget.date().toString("yyyy-MM-dd")
        editing = self._widget_is_user_editing(widget)
        if self._control_dirty.get(normalized, False):
            if normalized_reported and current_text == normalized_reported and not editing:
                self._control_dirty[normalized] = False
            else:
                return
        if editing or not target_text or current_text == target_text:
            return
        parsed = QDate.fromString(target_text, "yyyy-MM-dd")
        if not parsed.isValid():
            parsed = QDate.fromString(target_text)
        if not parsed.isValid():
            return
        def _apply_date() -> None:
            previous = widget.blockSignals(True)
            try:
                widget.setDate(parsed)
            finally:
                widget.blockSignals(previous)

        self._set_control_programmatic(normalized, _apply_date)

    def _on_camera_selection_changed(self, index: int) -> None:
        if index < 0:
            return
        camera_id = self.camera_combo.currentData()
        if camera_id is None:
            return
        selected_camera_id = int(camera_id)
        self._pending_control_values["camera_combo"] = selected_camera_id
        self._control_dirty["camera_combo"] = False
        self._send(make_slot_command("set_camera_id", camera_id=selected_camera_id))

    def _start_sampling_selected_mode(self) -> None:
        try:
            self._pending_sample_mode = int(self.mode_combo.currentData())
        except Exception:
            self._pending_sample_mode = None
        self._send(make_slot_command("sample_start", mode=self.mode_combo.currentData()))

    def _reported_mode_value(self, neural: Dict[str, object]) -> Optional[int]:
        active_mode = neural.get("active_mode")
        try:
            numeric_mode = int(active_mode)
        except Exception:
            numeric_mode = None
        if numeric_mode in dict(MODE_OPTIONS).values():
            return int(numeric_mode)
        normalized = str(active_mode or "").strip().lower()
        return MODE_LABEL_TO_VALUE.get(normalized)

    def _on_mode_combo_changed(self, _index: int) -> None:
        if self._mode_combo_syncing:
            return
        neural = self.current_status.get("neural", {})
        if not isinstance(neural, dict):
            neural = {}
        if not bool(neural.get("connected", False)) or not bool(neural.get("sampling", False)):
            self._pending_sample_mode = None
            return
        try:
            selected_mode = int(self.mode_combo.currentData())
        except Exception:
            return
        reported_mode = self._reported_mode_value(neural)
        if reported_mode == selected_mode:
            self._pending_sample_mode = None
            return
        self._pending_sample_mode = selected_mode
        self._send(make_slot_command("set_mode", mode=selected_mode))

    def _toggle_detail_view(self, checked: bool) -> None:
        self.detail_toggle(self.slot_config.slot_id, bool(checked))

    def _send_mode3_reref_command(self, mode: int) -> None:
        normalized_mode = normalize_mode3_reref_mode(mode, fallback=0)
        self._send(make_slot_command("set_mode3_reref", mode=normalized_mode))

    def _on_global_save_toggled(self, checked: bool) -> None:
        self._pending_control_values["global_save"] = bool(checked)
        self._send(make_slot_command("set_global_save", enabled=bool(checked)))

    def _on_charging_guard_toggled(self, checked: bool) -> None:
        self._pending_control_values["charging_guard"] = bool(checked)
        self._send(make_slot_command("set_charging_guard", enabled=bool(checked)))

    def _open_charging_guard_config_dialog(self) -> None:
        current = dict(self.current_status.get("charging_guard", {}) or {})
        dialog = QDialog(self)
        dialog.setWindowTitle(f"{self.slot_config.label} Charging Guard Config")
        dialog.setModal(True)
        layout = QVBoxLayout(dialog)

        form = QFormLayout()
        enabled_box = QCheckBox("Enabled")
        enabled_box.setChecked(bool(current.get("enabled", False)))
        roi_edit = QLineEdit(
            ",".join(str(float(v)) for v in list(current.get("roi_norm", [0.1, 0.5, 0.3, 0.2]))[:4])
        )
        legacy_dark_threshold = int(current.get("dark_threshold", 90) or 90)
        day_dark_spin = QSpinBox()
        day_dark_spin.setRange(0, 255)
        day_dark_spin.setValue(int(current.get("day_dark_threshold", legacy_dark_threshold) or legacy_dark_threshold))
        night_dark_spin = QSpinBox()
        night_dark_spin.setRange(0, 255)
        night_dark_spin.setValue(int(current.get("night_dark_threshold", legacy_dark_threshold) or legacy_dark_threshold))
        scene_brightness_spin = QSpinBox()
        scene_brightness_spin.setRange(0, 255)
        scene_brightness_spin.setValue(int(current.get("scene_brightness_threshold", 80) or 80))
        min_area_edit = QLineEdit(str(float(current.get("min_area_ratio", 0.30) or 0.30)))
        interval_spin = QSpinBox()
        interval_spin.setRange(1000, 600000)
        interval_spin.setValue(int(current.get("check_interval_ms", 10000) or 10000))
        required_hits_spin = QSpinBox()
        required_hits_spin.setRange(1, 50)
        required_hits_spin.setValue(int(current.get("required_hits", 6) or 6))
        warning_spin = QSpinBox()
        warning_spin.setRange(0, 120000)
        warning_spin.setValue(int(current.get("warning_duration_ms", 3000) or 3000))
        fan_box = QCheckBox("Fan airflow")
        fan_box.setChecked(bool(current.get("fan_enabled", True)))

        form.addRow("Enable", enabled_box)
        form.addRow("ROI norm", roi_edit)
        form.addRow("Day dark threshold", day_dark_spin)
        form.addRow("Night dark threshold", night_dark_spin)
        form.addRow("Day/night brightness split", scene_brightness_spin)
        form.addRow("Min area ratio", min_area_edit)
        form.addRow("Check interval (ms)", interval_spin)
        form.addRow("Required hits", required_hits_spin)
        form.addRow("Warning duration (ms)", warning_spin)
        form.addRow("Fan airflow", fan_box)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        save_btn = QPushButton("Save && Apply")
        cancel_btn = QPushButton("Cancel")
        _set_button_tone(save_btn, "accent")
        _set_button_tone(cancel_btn, "ghost")
        buttons.addWidget(save_btn)
        buttons.addWidget(cancel_btn)
        layout.addLayout(buttons)

        def _save() -> None:
            try:
                roi_values = [float(part.strip()) for part in roi_edit.text().split(",") if part.strip()]
                if len(roi_values) != 4:
                    raise ValueError("ROI must contain 4 values: x,y,w,h")
                min_area_ratio = float(min_area_edit.text().strip())
                if min_area_ratio < 0:
                    raise ValueError("Min area ratio must be >= 0")
                self._send(
                    make_slot_command(
                        "set_charging_guard_config",
                        config={
                            "enabled": bool(enabled_box.isChecked()),
                            "roi_norm": roi_values,
                            "dark_threshold": int(day_dark_spin.value()),
                            "day_dark_threshold": int(day_dark_spin.value()),
                            "night_dark_threshold": int(night_dark_spin.value()),
                            "scene_brightness_threshold": int(scene_brightness_spin.value()),
                            "min_area_ratio": min_area_ratio,
                            "check_interval_ms": int(interval_spin.value()),
                            "required_hits": int(required_hits_spin.value()),
                            "warning_duration_ms": int(warning_spin.value()),
                            "fan_enabled": bool(fan_box.isChecked()),
                        },
                    )
                )
                dialog.accept()
            except Exception as exc:
                QMessageBox.warning(dialog, "Invalid Config", str(exc))

        save_btn.clicked.connect(_save)
        cancel_btn.clicked.connect(dialog.reject)
        dialog.exec()

    def _select_habits_data_directory(self) -> None:
        current_dir = str(self.current_status.get("habits", {}).get("selected_data_dir", "") or "").strip()
        directory = QFileDialog.getExistingDirectory(self, "Select Data Directory", current_dir or os.path.expanduser("~"))
        if not directory:
            return
        self._send(make_slot_command("set_habits_data_directory", directory=directory))

    def _on_habits_start_date_changed(self, date_value) -> None:
        if "habits_start_date" in self._programmatic_control_keys:
            return
        if hasattr(date_value, "toString"):
            date_text = date_value.toString("yyyy-MM-dd")
        else:
            date_text = str(date_value or "")
        self._pending_control_values["habits_start_date"] = str(date_text or "")
        self._control_dirty["habits_start_date"] = False
        self._send(make_slot_command("set_habits_start_date", start_date=date_text))

    def _apply_reward_command(self) -> None:
        pending_reward = (
            int(self.reward_left_spin.value()),
            int(self.reward_middle_spin.value()),
            int(self.reward_right_spin.value()),
        )
        self._pending_control_values["reward"] = pending_reward
        self._control_dirty["reward"] = False
        self._send(
            make_slot_command(
                "habits_serial",
                action="reward",
                left=pending_reward[0],
                middle=pending_reward[1],
                right=pending_reward[2],
            )
        )

    def _apply_light_command(self) -> None:
        pending_light = (
            int(self.light_low_spin.value()),
            int(self.light_high_spin.value()),
        )
        self._pending_control_values["light"] = pending_light
        self._control_dirty["light"] = False
        self._send(
            make_slot_command(
                "habits_serial",
                action="light",
                low=pending_light[0],
                high=pending_light[1],
            )
        )

    def _apply_protocol_command(self) -> None:
        pending_protocol = int(self.protocol_spin.value())
        if pending_protocol < 0 or pending_protocol > 10:
            QMessageBox.information(
                self,
                "Protocol",
                "Manual protocol commands are limited to P0-P10. Low-battery training is handled automatically as Mode0 saved to the Mode3 folder.",
            )
            return
        self._pending_control_values["protocol"] = pending_protocol
        self._control_dirty["protocol"] = False
        self._send(
            make_slot_command(
                "habits_serial",
                action="protocol",
                protocol=pending_protocol,
            )
        )

    def _apply_inter_block_command(self) -> None:
        pending_minutes = int(self.inter_block_spin.value())
        self._pending_control_values["inter_block"] = pending_minutes
        self._control_dirty["inter_block"] = False
        self._send(
            make_slot_command(
                "habits_serial",
                action="inter_block_interval",
                minutes=pending_minutes,
            )
        )

    def _apply_imu_command(self) -> None:
        pending_mode = _normalize_imu_mode(self.imu_mode_combo.currentData())
        self._pending_control_values["imu_mode"] = pending_mode
        self._control_dirty["imu_mode"] = False
        self._send(
            make_slot_command(
                "set_imu_mode",
                mode=pending_mode,
            )
        )

    def _apply_spike_filter_command(self) -> None:
        pending_enabled = bool(self.spike_filter_checkbox.isChecked())
        pending_low_cut_hz = float(self.spike_filter_low_spin.value())
        pending_high_cut_hz = float(self.spike_filter_high_spin.value())
        pending_sample_rate_hz = float(self.spike_filter_sample_rate_combo.currentData() or 20000.0)
        self._pending_control_values["spike_filter_enabled"] = pending_enabled
        self._pending_control_values["spike_filter_low_cut_hz"] = pending_low_cut_hz
        self._pending_control_values["spike_filter_high_cut_hz"] = pending_high_cut_hz
        self._pending_control_values["spike_filter_sample_rate_hz"] = pending_sample_rate_hz
        self._control_dirty["spike_filter_enabled"] = False
        self._control_dirty["spike_filter_low_cut_hz"] = False
        self._control_dirty["spike_filter_high_cut_hz"] = False
        self._control_dirty["spike_filter_sample_rate_hz"] = False
        self._send(
            make_slot_command(
                "set_spike_filter_config",
                enabled=pending_enabled,
                low_cut_hz=pending_low_cut_hz,
                high_cut_hz=pending_high_cut_hz,
                sample_rate_hz=pending_sample_rate_hz,
            )
        )

    def _apply_impedance_compensation_command(self) -> None:
        pending_series_resistor_kohm = float(self.impedance_series_spin.value())
        pending_shunt_cap_pf = float(self.impedance_shunt_spin.value())
        self._pending_control_values["rc_series_resistor_kohm"] = pending_series_resistor_kohm
        self._pending_control_values["rc_shunt_cap_pf"] = pending_shunt_cap_pf
        self._control_dirty["rc_series_resistor_kohm"] = False
        self._control_dirty["rc_shunt_cap_pf"] = False
        self._send(
            make_slot_command(
                "set_impedance_compensation",
                series_resistor_kohm=pending_series_resistor_kohm,
                shunt_cap_pf=pending_shunt_cap_pf,
            )
        )

    def _on_imu_mode_selection_changed(self, _index: int) -> None:
        self._mark_control_dirty("imu_mode")
        self._refresh_imu_apply_button()

    def _refresh_imu_apply_button(self) -> None:
        neural = self.current_status.get("neural", {})
        if not isinstance(neural, dict):
            neural = {}
        connected = bool(neural.get("connected", False))
        current_mode = _normalize_imu_mode(neural.get("imu_mode", 2))
        selected_mode = _normalize_imu_mode(self.imu_mode_combo.currentData())
        pending_mode = self._pending_control_values.get("imu_mode")
        applying = pending_mode is not None and int(pending_mode) != current_mode
        is_synced = selected_mode == current_mode and not applying
        self._set_button_state(
            self.imu_apply_btn,
            text="Applying IMU Mode..." if connected and applying else ("IMU Synced" if connected and is_synced else "Apply IMU Mode"),
            tone="warning" if connected and applying else ("success" if connected and is_synced else "accent"),
            enabled=connected and not is_synced and not applying,
        )
        self.imu_apply_btn.setToolTip(
            (
                f"Waiting for device confirmation. Current IMU mode: {_imu_mode_label(current_mode)}"
                if connected and applying
                else f"Device IMU mode: {_imu_mode_label(current_mode)}"
            )
            if connected
            else "Connect Neural before changing IMU mode."
        )

    def _refresh_mode3_reref_button(
        self,
        *,
        mode: int,
        connected: bool,
        pending: bool,
        target_mode: Optional[int] = None,
    ) -> None:
        normalized_mode = normalize_mode3_reref_mode(mode, fallback=0)
        pending_mode = normalized_mode
        if pending and target_mode is not None:
            pending_mode = normalize_mode3_reref_mode(target_mode, fallback=normalized_mode)

        button_text = _mode3_reref_label(pending_mode if pending else normalized_mode)
        if pending:
            button_text = f"Applying: {button_text.split(': ', 1)[-1]}"
        self.mode3_reref_button.setText(button_text)
        set_tone(self.mode3_reref_button, "warning" if pending else _mode3_reref_tone(normalized_mode))
        self.mode3_reref_button.setEnabled(bool(connected) and not pending)
        tooltip = (
            f"Waiting for device confirmation, target {button_text.split(': ', 1)[-1]}."
            if pending
            else f"Device reported state: {button_text.split(': ', 1)[-1]}."
        )
        self.mode3_reref_button.setToolTip(tooltip)
        for value, action in self._mode3_reref_actions.items():
            if action is None:
                continue
            action.setCheckable(True)
            action.setChecked(value == normalized_mode)

    def set_camera_catalog(self, cameras: List[Dict[str, object]]) -> None:
        self._camera_catalog = [dict(item) for item in (cameras or [])]
        current_camera_id = None
        camera = self.current_status.get("camera", {})
        if isinstance(camera, dict):
            current_camera_id = camera.get("camera_id")
        if "camera_combo" in self._pending_control_values:
            current_camera_id = self._pending_control_values.get("camera_combo")
        elif self._control_dirty.get("camera_combo", False):
            current_camera_id = self.camera_combo.currentData()
        if current_camera_id is None:
            current_camera_id = self.camera_combo.currentData()

        self.camera_combo.blockSignals(True)
        self.camera_combo.clear()
        if not self._camera_catalog:
            self.camera_combo.addItem("No cameras found", None)
        else:
            for camera_info in self._camera_catalog:
                display_text = str(camera_info.get("display", camera_info.get("name", "Camera")))
                self.camera_combo.addItem(display_text, camera_info.get("id"))
            self._set_control_programmatic(
                "camera_combo",
                lambda: self._set_combo_value(self.camera_combo, current_camera_id),
            )
        self.camera_combo.blockSignals(False)

    def update_status(self, status: Dict[str, object]) -> None:
        self.current_status = dict(status)
        if self._protocol_flow_dialog is not None and self._protocol_flow_dialog.isVisible():
            self._protocol_flow_dialog.update_status(self.current_status)
        neural = status.get("neural", {})
        rf = status.get("rf", {})
        habits = status.get("habits", {})
        camera = status.get("camera", {})
        battery = status.get("battery", {})
        charging_guard = status.get("charging_guard", {})
        save_flags = neural.get("save_flags", {})
        save_progress = neural.get("save_progress", {})
        save_route = neural.get("save_route", {})
        active_save_label = _save_mode_display_label(neural.get("save_mode", ""), save_route)
        neural_connected = bool(neural.get("connected", False))
        mouse_id = str(habits.get("mouse_id", "") or "").strip()
        if not mouse_id:
            mouse_id = extract_mouse_id_from_directory(habits.get("mouse_directory", ""))
        self.mouse_id_badge.setText(mouse_id or "Mouse --")
        self.mouse_id_badge.setToolTip(str(habits.get("mouse_directory", "") or "").strip() or (mouse_id or "Mouse --"))

        packet_loss_percent_current = float(neural.get("packet_loss_percent_current", 0.0) or 0.0)
        packet_loss_expected_current = int(neural.get("packet_loss_expected_current", 0) or 0)
        packet_loss_percent_1h = float(neural.get("packet_loss_percent_1h", 0.0) or 0.0)
        packet_loss_expected_1h = int(neural.get("packet_loss_expected_1h", 0) or 0)
        packet_loss_state = classify_packet_loss_state(packet_loss_percent_current, packet_loss_expected_current)
        packet_loss_rollup_state = str(
            neural.get("packet_loss_state")
            or classify_packet_loss_state(packet_loss_percent_1h, packet_loss_expected_1h)
        )
        battery_percent = float(
            battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0
        )
        battery_voltage = battery.get("voltage", 0.0)
        battery_stat = battery.get("stat")
        battery_has_data = battery_percent > 0.0 or float(battery_voltage or 0.0) > 0.0
        battery_display_ok = battery_percent >= 10.0 if battery_has_data else None
        battery_display_status = self._battery_status_smoother.update(battery_stat)
        charge_text = str(battery.get("bq25176_charge_state_text") or battery_display_status.charge_text)
        charge_state = str(battery.get("bq25176_charge_state_level") or battery_display_status.charge_state)
        power_text = str(battery.get("bq25176_power_state_text") or battery_display_status.power_text)
        power_state = str(battery.get("bq25176_power_state_level") or battery_display_status.power_state)
        bq_status_text = str(battery.get("bq25176_status_text", "") or "").strip()
        if bool(battery.get("bq25176_fault_possible", False)):
            charge_text = "Possible charge fault"
            charge_state = "danger"
        rssi_value = float(neural.get("rssi", 0.0) or 0.0)
        rssi_state = None if abs(rssi_value) <= 0.0 else ("ok" if abs(rssi_value) < 70.0 else "danger")
        rf_power_on = str(rf.get("power_state", "unknown") or "unknown").strip().lower() == "on"

        state_text = str(status.get("state", "unknown") or "unknown").strip().title()
        self.state_pill.setText(state_text)
        set_tone(self.state_pill, _state_to_tone(state_text))
        self.state_pill.setToolTip(_state_explanation(status))

        detail_attached = bool(status.get("detail_attached", False))
        self.chart_pill.setText("Charts On" if detail_attached else "Charts Off")
        set_tone(self.chart_pill, "ok" if detail_attached else "danger")

        devices_summary = join_status_fragments(
            f"Neural {format_on_off_text(neural.get('connected'))}",
            f"RF {format_on_off_text(rf.get('connected'))}",
            f"Habits {format_on_off_text(habits.get('connected'))}",
            f"Camera {format_on_off_text(camera.get('open'))}",
        )
        self.devices_tile.set_value(devices_summary)

        displayed_active_mode = str(neural.get("active_mode", "Idle") or "Idle").strip() or "Idle"
        if not bool(neural.get("sampling", False)):
            displayed_active_mode = "Idle"

        recording_summary = (
            f"{displayed_active_mode}  |  "
            f"Save {active_save_label}  |  "
            f"Run {format_run_duration(neural.get('run_time_min', 0.0))}"
        )
        self.recording_tile.set_value(recording_summary)
        self.recording_tile.set_tone("ok" if neural.get("sampling") else "neutral")

        current_imu_mode = _normalize_imu_mode(neural.get("imu_mode", 2))
        current_imu_text = str(neural.get("imu_mode_text", _imu_mode_label(current_imu_mode)) or _imu_mode_label(current_imu_mode))
        reported_mode = self._reported_mode_value(neural if isinstance(neural, dict) else {})
        neural_sampling = bool(neural.get("sampling", False))
        if self._pending_sample_mode is not None and reported_mode == self._pending_sample_mode:
            self._pending_sample_mode = None
        active_mode_text = displayed_active_mode
        self.sample_mode_status_label.setText(
            f"Current: {format_status_text(active_mode_text, 'ok' if neural_sampling else 'neutral')}"
        )
        self.sample_mode_status_label.setToolTip(
            "This is the actual mode reported by the device. The Sample Mode dropdown above is kept as your target selection and is not auto-overwritten."
        )
        self.imu_status_label.setText(
            f"Current: {format_status_text(current_imu_text, 'ok' if neural_connected else 'neutral')}"
        )
        self.imu_status_label.setToolTip("IMU mode control mirrored from the original neural recorder GUI.")

        battery_percent_text = "--%" if not battery_has_data else f"{battery_percent:.1f}%"
        battery_summary = join_status_fragments(
            format_status_text(
                battery_percent_text,
                None if battery_display_ok is None else ("ok" if battery_display_ok else "danger"),
            ),
            format_status_text(format_voltage_text(battery_voltage), "neutral"),
            format_status_text(charge_text, charge_state),
            format_status_text(power_text, power_state),
        )
        self.battery_tile.set_value(battery_summary)
        self.battery_tile.set_tone(
            "neutral" if battery_display_ok is None else ("ok" if battery_display_ok else "danger")
        )
        battery_tooltip = battery_display_status.tooltip
        if bq_status_text:
            battery_tooltip = f"{battery_tooltip}\n{bq_status_text}"
        fault_reason = str(battery.get("bq25176_fault_reason", "") or "").strip()
        if fault_reason:
            battery_tooltip = f"{battery_tooltip}\n{fault_reason}"
        self.battery_tile.setToolTip(battery_tooltip)

        loss_summary = format_status_text("--", "neutral")
        if packet_loss_expected_current > 0:
            loss_summary = format_status_text(
                f"{packet_loss_percent_current:.2f}%",
                "ok" if packet_loss_percent_current < 1.0 else "danger",
            )
        wireless_summary = join_status_fragments(
            f"Loss {loss_summary}",
            f"RSSI {format_status_text(f'{rssi_value:.1f} dBm', rssi_state)}",
            f"RF {format_on_off_text(rf_power_on, 'ON', 'OFF')}",
        )
        self.wireless_tile.set_value(wireless_summary)
        self.wireless_tile.set_tone(
            "danger"
            if packet_loss_state == "alert" or rssi_state == "danger"
            else ("ok" if packet_loss_state == "ok" else "neutral")
        )
        self.wireless_tile.setToolTip(
            f"Current cumulative loss: {packet_loss_percent_current:.2f}% "
            f"({int(neural.get('packet_loss', 0) or 0)}/{packet_loss_expected_current}) | "
            f"1h rollup: {packet_loss_percent_1h:.2f}% ({packet_loss_rollup_state})"
        )
        mode0_power = neural.get("mode0_power", {})
        if not isinstance(mode0_power, dict):
            mode0_power = {}
        mode0_power_valid = bool(mode0_power.get("valid", False))
        mode0_estimated_mw = mode0_power.get("estimated_mw")
        try:
            mode0_estimated_mw_float = None if mode0_estimated_mw is None else float(mode0_estimated_mw)
        except Exception:
            mode0_estimated_mw_float = None
        mode0_sleep_ratio = float(mode0_power.get("sleep_ratio_percent", 0.0) or 0.0)
        mode0_retransmit_rate = float(mode0_power.get("retransmit_rate_percent", 0.0) or 0.0)
        mode0_tx_power_dbm = int(mode0_power.get("tx_power_dbm", 0) or 0)
        mode0_tx_power_code = int(mode0_power.get("tx_power_code", 0) or 0)
        mode0_retransmit_count = int(mode0_power.get("retransmit_count", 0) or 0)
        mode0_flags = int(mode0_power.get("flags", 0) or 0)
        mode0_warnings = mode0_power.get("warnings", [])
        if not isinstance(mode0_warnings, list):
            mode0_warnings = []
        if not mode0_power_valid:
            mode0_power_summary = format_status_text("-- mW", "neutral")
            mode0_power_tone = "neutral"
        else:
            mw_text = "-- mW" if mode0_estimated_mw_float is None else f"{mode0_estimated_mw_float:.1f} mW"
            mode0_power_tone = "danger" if mode0_warnings else "ok"
            mode0_power_summary = join_status_fragments(
                format_status_text(mw_text, mode0_power_tone),
                format_status_text(f"TX {mode0_tx_power_dbm} dBm", "neutral"),
                format_status_text(f"Retry {mode0_retransmit_rate:.1f}%", "ok" if mode0_retransmit_rate < 20.0 else "danger"),
            )
        self.mode0_power_tile.set_value(mode0_power_summary)
        self.mode0_power_tile.set_tone(mode0_power_tone)
        tooltip_mw_text = "--" if mode0_estimated_mw_float is None else f"{mode0_estimated_mw_float:.1f} mW"
        self.mode0_power_tile.setToolTip(
            "Mode0 compact power telemetry.\n"
            f"estimated={tooltip_mw_text}, tx_power_dbm={mode0_tx_power_dbm}, "
            f"retry={mode0_retransmit_rate:.1f}%, retransmit_rate={mode0_retransmit_rate:.1f}%, "
            f"sleep_ratio={mode0_sleep_ratio:.1f}%, "
            f"tx_power_code={mode0_tx_power_code}, "
            f"retransmit_count={mode0_retransmit_count}, flags=0x{mode0_flags:04X}, "
            f"warning={str(mode0_power.get('warning', '') or '-')}"
        )
        read_batch_bytes_by_mode = neural.get("read_batch_bytes_by_mode", {})
        if not isinstance(read_batch_bytes_by_mode, dict):
            read_batch_bytes_by_mode = {}
        read_batch_packets_by_mode = neural.get("read_batch_packets_by_mode", {})
        if not isinstance(read_batch_packets_by_mode, dict):
            telemetry_packets = dict(neural.get("telemetry", {}) or {}).get("read_batch_packets_by_mode", {})
            read_batch_packets_by_mode = telemetry_packets if isinstance(telemetry_packets, dict) else {}
        read_packet_redline_by_mode = neural.get("read_batch_packet_redline_by_mode", {})
        if not isinstance(read_packet_redline_by_mode, dict):
            telemetry_redline = dict(neural.get("telemetry", {}) or {}).get("read_batch_packet_redline_by_mode", {})
            read_packet_redline_by_mode = telemetry_redline if isinstance(telemetry_redline, dict) else {}
        read_packet_warn_by_mode = neural.get("read_batch_packet_warn_by_mode", {})
        if not isinstance(read_packet_warn_by_mode, dict):
            telemetry_warn = dict(neural.get("telemetry", {}) or {}).get("read_batch_packet_warn_by_mode", {})
            read_packet_warn_by_mode = telemetry_warn if isinstance(telemetry_warn, dict) else {}
        try:
            active_stream_mode = int(neural.get("stream_mode", reported_mode) if neural.get("stream_mode", None) is not None else reported_mode)
        except Exception:
            active_stream_mode = reported_mode
        serial_backlog_active = bool(neural.get("serial_backlog_active", False))
        read_tiles = (
            ("mode0", 0, self.read_mode0_tile),
            ("mode1", 1, self.read_mode1_tile),
            ("mode2", 2, self.read_mode2_tile),
            ("mode3", 3, self.read_mode3_tile),
        )
        for mode_key, mode_value, tile in read_tiles:
            packets_value = read_batch_packets_by_mode.get(mode_key, 0.0)
            warn_value = read_packet_warn_by_mode.get(mode_key, 0)
            redline_value = read_packet_redline_by_mode.get(mode_key, 0)
            tile.set_value(_format_read_batch_packets(packets_value))
            tile.set_tone(
                _read_batch_pressure_tone(
                    packets_value,
                    warn_value,
                    redline_value,
                    force_danger=serial_backlog_active and int(active_stream_mode) == int(mode_value),
                )
            )
            tile.setToolTip(
                _read_batch_pressure_tooltip(
                    mode_key,
                    packets_value,
                    warn_value,
                    redline_value,
                    read_batch_bytes_by_mode.get(mode_key, 0.0),
                )
            )
        gui_intervals = neural.get("gui_update_intervals_by_mode", {})
        gui_stream_intervals = neural.get("gui_update_intervals_by_stream", {})
        if isinstance(gui_stream_intervals, dict) and gui_stream_intervals:
            interval_tooltip = (
                f"M0 LFP/MAND/Raw interval {int(gui_stream_intervals.get('mode0_lfp', gui_intervals.get('mode0', 0)) or 0)} / "
                f"M1 interval {int(gui_stream_intervals.get('mode1_raw', gui_intervals.get('mode1', 0)) or 0)} / "
                f"M2 interval {int(gui_stream_intervals.get('mode2_raw', gui_intervals.get('mode2', 0)) or 0)} / "
                f"M3 LFP/ESA {int(gui_stream_intervals.get('mode3_lfp_esa', gui_intervals.get('mode3', 0)) or 0)} / "
                f"Raw {int(gui_stream_intervals.get('mode3_raw', gui_intervals.get('mode3', 0)) or 0)}"
            )
            for _mode_key, _mode_value, tile in read_tiles:
                existing_tooltip = str(tile.toolTip() or "")
                tile.setToolTip(existing_tooltip + "\n" + interval_tooltip if existing_tooltip else interval_tooltip)
        elif isinstance(gui_intervals, dict):
            interval_tooltip = " / ".join(
                f"{mode_key.upper()} interval {int(gui_intervals.get(mode_key, 0) or 0)}"
                for mode_key, _mode_value, _tile in read_tiles
            )
            for _mode_key, _mode_value, tile in read_tiles:
                existing_tooltip = str(tile.toolTip() or "")
                tile.setToolTip(existing_tooltip + "\n" + interval_tooltip if existing_tooltip else interval_tooltip)
        rf_connection_state = str(rf.get("connection_state", "idle") or "idle").strip().lower()
        camera_health_state = str(camera.get("health_state", "ok") or "ok").strip().lower()
        rf_port = str(rf.get("port", "") or "").strip() or "-"
        camera_last_frame_age = float(camera.get("last_frame_age_sec", 0.0) or 0.0)
        telemetry = dict(neural.get("telemetry", {}) or {})
        reported_save_mode = str(neural.get("save_mode", "") or "").strip()
        reported_save_label = _save_mode_display_label(reported_save_mode, save_route)
        recovery_tooltip = (
            f"RF {rf_connection_state} on {rf_port} | "
            f"Camera {camera_health_state} ({camera_last_frame_age:.1f}s)\n"
            f"Detail payload: {int(telemetry.get('detail_payload_bytes', 0) or 0)} bytes | "
            f"{float(telemetry.get('detail_payload_interval_ms', 0.0) or 0.0):.1f} ms | "
            f"skipped {int(telemetry.get('detail_dropped_frames', 0) or 0)} "
            f"{str(telemetry.get('detail_last_skip_reason', '') or '').strip()}\n"
            f"Cache flush: {float(telemetry.get('runtime_cache_flush_duration_ms', 0.0) or 0.0):.1f} ms | "
            f"failures {int(telemetry.get('runtime_cache_write_failures', 0) or 0)}"
        )

        trial_trigger_gate_text = str(habits.get("mode3_trial_trigger_gate_text", "") or "").strip()
        habits_summary = (
            f"{mouse_id or 'Mouse --'}  |  "
            f"Trial {int(habits.get('current_trial', 0) or 0)}  |  "
            f"{habits.get('outcome_text', '-')}  |  "
            f"Today {int(habits.get('trials_today', 0) or 0)}"
        )
        if trial_trigger_gate_text:
            habits_summary = f"{habits_summary}  |  {trial_trigger_gate_text}"
        self.habits_tile.set_value(habits_summary)
        low_battery_mode0_target = bool(habits.get("low_battery_mode0_training_target", False))
        low_battery_mode0_active = bool(habits.get("low_battery_mode0_training_active", False))
        self.habits_tile.set_tone(
            "warning"
            if habits.get("paused") or habits.get("mode3_trial_trigger_target_paused") or low_battery_mode0_target
            else "ok"
        )
        trial_trigger_paused = bool(habits.get("mode3_trial_trigger_paused", False))
        trial_trigger_target_paused = bool(habits.get("mode3_trial_trigger_target_paused", False))
        if low_battery_mode0_active or low_battery_mode0_target:
            trial_trigger_state_text = "LOW BATTERY MODE0"
            trial_trigger_state = "danger" if low_battery_mode0_active else "warning"
        elif trial_trigger_paused:
            trial_trigger_state_text = "FIRMWARE LOW-BATT PAUSE"
            trial_trigger_state = "danger"
        elif trial_trigger_target_paused:
            trial_trigger_state_text = "ARMED, waiting for RF ON"
            trial_trigger_state = "danger"
        else:
            trial_trigger_state_text = "READY"
            trial_trigger_state = "ok"
        last_check_text = _format_epoch_clock(habits.get("mode3_trial_trigger_last_check_epoch"))
        last_apply_text = _format_epoch_clock(habits.get("mode3_trial_trigger_last_apply_epoch"))
        last_check_source = str(habits.get("mode3_trial_trigger_last_check_source", "") or "").strip() or "-"
        gate_detail_text = trial_trigger_gate_text or "Trigger ready"
        self.trial_trigger_gate_label.setText(
            join_status_fragments(
                f"State {format_status_text(trial_trigger_state_text, trial_trigger_state)}",
                format_status_text(gate_detail_text, trial_trigger_state),
                f"Last check {format_status_text(last_check_text, 'neutral')}",
            )
        )
        self.trial_trigger_gate_label.setToolTip(
            f"Source: {last_check_source}\n"
            f"Last check: {last_check_text}\n"
            f"Last apply: {last_apply_text}\n"
            f"Low-battery Mode0 route: <{int(habits.get('mode3_trial_trigger_pause_threshold', 10) or 10)}%\n"
            f"Normal route resumes: >{int(habits.get('mode3_trial_trigger_resume_threshold', 20) or 20)}%"
        )
        if trial_trigger_paused or trial_trigger_target_paused or low_battery_mode0_target:
            self.trial_trigger_gate_label.setStyleSheet(
                "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        else:
            self.trial_trigger_gate_label.setStyleSheet(
                "background-color: #F0FDF4; color: #166534; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        recovery_summary = (
            f"RF {rf_connection_state} on {rf_port}  |  "
            f"Camera {camera_health_state} ({camera_last_frame_age:.1f}s)"
        )
        self.recovery_tile.set_value(recovery_summary)
        self.recovery_tile.set_tone(_recovery_tone(rf_connection_state, camera_health_state))
        self.recovery_tile.setToolTip(recovery_tooltip)
        self.status_chip_grid.set_chip(
            "sampling",
            f"Sampling {'ON' if neural_sampling else 'OFF'}",
            "ok" if neural_sampling else "danger",
        )
        self.status_chip_grid.set_chip(
            "saving",
            f"Save {reported_save_label}",
            "ok" if reported_save_mode else "danger",
        )
        self.status_chip_grid.set_chip(
            "charts",
            "Charts ON" if detail_attached else "Charts OFF",
            "ok" if detail_attached else "danger",
        )

        selected_camera_id = camera.get("camera_id")
        camera_status_text = join_status_fragments(
            f"Camera ID: {format_status_text(selected_camera_id if selected_camera_id is not None else '--', 'neutral')}",
            f"Preview {format_on_off_text(camera.get('preview_enabled'))}",
            f"Recording path: {format_status_text(str(camera.get('recording_path', '') or '-').strip() or '-', 'neutral')}",
        )
        self.camera_status_label.setText(camera_status_text)
        guard_status_text = str(charging_guard.get("status_text", "Off") or "Off")
        guard_area_percent = float(charging_guard.get("area_ratio", 0.0) or 0.0) * 100.0
        mouse_present = bool(charging_guard.get("mouse_present", False))
        guard_lighting = str(charging_guard.get("lighting_state", "unknown") or "unknown")
        guard_active_threshold = int(charging_guard.get("active_dark_threshold", charging_guard.get("dark_threshold", 90)) or 90)
        guard_hit_count = int(charging_guard.get("hit_count", 0) or 0)
        guard_required_hits = max(1, int(charging_guard.get("required_hits", 0) or 0))
        self.charging_guard_status_label.setText(f"Guard: {guard_status_text}")
        self.charging_guard_progress_label.setText(f"Hits: {guard_hit_count}/{guard_required_hits}")
        self.mouse_detection_status_label.setText(
            f"Mouse detected: {'YES' if mouse_present else 'NO'} {guard_area_percent:.1f}% | {guard_lighting} th{guard_active_threshold}"
        )
        self.mouse_detection_status_label.setToolTip(
            str(charging_guard.get("detection_text", "") or "")
        )
        if "warning" in guard_status_text.lower():
            self.charging_guard_status_label.setStyleSheet(
                "background-color: #FEE2E2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
            self.charging_guard_progress_label.setStyleSheet(
                "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        elif guard_status_text.lower().startswith("reset"):
            self.charging_guard_status_label.setStyleSheet(
                "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
            self.charging_guard_progress_label.setStyleSheet(
                "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        elif bool(charging_guard.get("enabled", False)):
            self.charging_guard_status_label.setStyleSheet(
                "background-color: #F0FDF4; color: #166534; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
            self.charging_guard_progress_label.setStyleSheet(
                "background-color: #F8FAFC; color: #0F172A; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        else:
            self.charging_guard_status_label.setStyleSheet(
                "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
            self.charging_guard_progress_label.setStyleSheet(
                "background-color: #F8FAFC; color: #475569; border-radius: 8px; padding: 6px; font-weight: 700;"
            )
        self.mouse_detection_status_label.setStyleSheet(
            "background-color: #DCFCE7; color: #166534; border-radius: 8px; padding: 6px; font-weight: 600;"
            if mouse_present
            else "background-color: #FEF2F2; color: #991B1B; border-radius: 8px; padding: 6px; font-weight: 700;"
        )

        self.lfp_progress_bar.setValue(int(max(0.0, min(100.0, float(save_progress.get("lfp", 0.0) or 0.0)))))
        self.mode3_progress_bar.setValue(int(max(0.0, min(100.0, float(save_progress.get("mode3", 0.0) or 0.0)))))
        self.mode1_progress_bar.setValue(int(max(0.0, min(100.0, float(save_progress.get("mode1", 0.0) or 0.0)))))
        self.mode2_progress_bar.setValue(int(max(0.0, min(100.0, float(save_progress.get("mode2", 0.0) or 0.0)))))
        self._set_progress_bar_style(self.lfp_progress_bar, bool(save_flags.get("lfp", False)))
        self._set_progress_bar_style(self.mode3_progress_bar, bool(save_flags.get("mode3", False)))
        self._set_progress_bar_style(self.mode1_progress_bar, bool(save_flags.get("mode1", False)))
        self._set_progress_bar_style(self.mode2_progress_bar, bool(save_flags.get("mode2", False)))
        self.save_progress_summary.setText(
            f"Writer lag: {int(neural.get('writer_lag', 0) or 0)}  |  "
            f"Active save: {active_save_label}"
        )

        last_error = str(status.get("last_error", "") or "").strip()
        if last_error:
            self.error_banner.setText(f"Last error: {last_error}")
            self.error_banner.show()
        else:
            self.error_banner.setText("Last error: -")
            self.error_banner.hide()

        habits_connected = bool(habits.get("connected", False))
        self._set_button_state(
            self.habits_connect_btn,
            text="Disconnect Habits" if habits_connected else "Connect Habits",
            tone="danger" if habits_connected else "success",
            enabled=True,
        )
        selected_dir = str(habits.get("selected_data_dir", "") or habits.get("mouse_directory", "") or "").strip()
        self.habits_data_dir_label.setText(selected_dir or "Not selected")
        self.habits_data_dir_label.setToolTip(selected_dir or "Not selected")
        self._sync_date_control(
            "habits_start_date",
            self.habits_start_date_edit,
            str(habits.get("start_date", "") or "").strip(),
        )
        self.habits_sync_label.setText(str(habits.get("last_sync_text", "Not synced") or "Not synced"))
        read_all_seq = int(habits.get("read_all_seq", 0) or 0)
        if read_all_seq > self._last_habits_read_all_seq:
            self._last_habits_read_all_seq = read_all_seq
            self._clear_habits_config_edit_guards()
        self.habits_pause_btn.setText("Resume Habits" if habits.get("paused") else "Pause Habits")
        _set_button_tone(self.habits_pause_btn, "warning" if habits.get("paused") else "ghost")
        self._sync_spin_group_control(
            "reward",
            (
                (self.reward_left_spin, habits.get("reward_left")),
                (self.reward_middle_spin, habits.get("reward_middle")),
                (self.reward_right_spin, habits.get("reward_right")),
            ),
        )
        self._sync_spin_group_control(
            "light",
            (
                (self.light_low_spin, habits.get("low_light")),
                (self.light_high_spin, habits.get("high_light")),
            ),
        )
        self._sync_spin_control(
            "protocol",
            self.protocol_spin,
            self._effective_habits_protocol(habits),
        )
        self._sync_spin_control(
            "inter_block",
            self.inter_block_spin,
            habits.get("inter_block_interval_minutes"),
        )

        camera_open = bool(camera.get("open", False))
        camera_recording = bool(camera.get("recording", False))
        self._set_button_state(
            self.camera_open_btn,
            text="Close Camera" if camera_open else "Open Camera",
            tone="danger" if camera_open else "ghost",
            enabled=True,
        )
        preview_enabled = bool(self._resolve_pending_value("camera_preview", bool(camera.get("preview_enabled", False))))
        self.camera_show_btn.blockSignals(True)
        self.camera_show_btn.setChecked(preview_enabled)
        self.camera_show_btn.setText(
            "Hide Camera" if preview_enabled else "Show Camera"
        )
        self.camera_show_btn.blockSignals(False)
        _set_button_tone(
            self.camera_show_btn,
            "danger" if preview_enabled else "accent",
        )
        self._set_button_state(
            self.camera_record_btn,
            text="Stop Recording" if camera_recording else "Start Recording",
            tone="danger" if camera_recording else "accent",
            enabled=camera_open,
        )
        self._sync_combo_control("camera_combo", self.camera_combo, selected_camera_id)

        global_save_enabled = bool(self._resolve_pending_value("global_save", bool(neural.get("global_save_enabled", True))))
        self.global_save_toggle.blockSignals(True)
        self.global_save_toggle.setChecked(global_save_enabled)
        self.global_save_toggle.setText(
            "Global Save On" if global_save_enabled else "Global Save Off"
        )
        self.global_save_toggle.blockSignals(False)
        _set_button_tone(self.global_save_toggle, "success" if global_save_enabled else "danger")

        charging_guard_enabled = bool(self._resolve_pending_value("charging_guard", bool(charging_guard.get("enabled", False))))
        self.charging_guard_checkbox.blockSignals(True)
        self.charging_guard_checkbox.setChecked(charging_guard_enabled)
        self.charging_guard_checkbox.blockSignals(False)
        self._sync_combo_control("imu_mode", self.imu_mode_combo, current_imu_mode)
        self._sync_checkbox_control(
            "spike_filter_enabled",
            self.spike_filter_checkbox,
            neural.get("spike_filter_enabled", False),
        )
        self._sync_double_spin_control(
            "spike_filter_low_cut_hz",
            self.spike_filter_low_spin,
            neural.get("spike_filter_low_cut_hz"),
        )
        self._sync_double_spin_control(
            "spike_filter_high_cut_hz",
            self.spike_filter_high_spin,
            neural.get("spike_filter_high_cut_hz"),
        )
        self._sync_combo_control(
            "spike_filter_sample_rate_hz",
            self.spike_filter_sample_rate_combo,
            int(float(neural.get("spike_filter_sample_rate_hz", 20000.0) or 20000.0)),
        )
        self._sync_double_spin_control(
            "rc_series_resistor_kohm",
            self.impedance_series_spin,
            neural.get("rc_series_resistor_kohm"),
        )
        self._sync_double_spin_control(
            "rc_shunt_cap_pf",
            self.impedance_shunt_spin,
            neural.get("rc_shunt_cap_pf"),
        )
        mode0_quant = neural.get("mode0_quant", {}) if isinstance(neural.get("mode0_quant", {}), dict) else {}
        self._sync_spin_control(
            "mode0_quant_bits",
            self.mode0_quant_bits_spin,
            mode0_quant.get("bit_depth", 12),
        )
        self._sync_spin_control(
            "mode0_quant_full_scale_uv",
            self.mode0_quant_range_spin,
            mode0_quant.get("full_scale_uv", 1000),
        )
        reported_reref_mode = normalize_mode3_reref_mode(
            neural.get("mode3_reref_mode", 0),
            fallback=0,
        )
        pending_reref = bool(neural.get("mode3_reref_pending", False))
        target_reref_mode = normalize_mode3_reref_mode(
            neural.get("mode3_reref_target_mode", reported_reref_mode),
            fallback=reported_reref_mode,
        )
        self._refresh_mode3_reref_button(
            mode=reported_reref_mode,
            connected=neural_connected,
            pending=pending_reref,
            target_mode=target_reref_mode,
        )
        rf_connected = bool(rf.get("connected", False))
        neural_command_busy = bool(neural.get("command_busy", False))
        neural_command_action = str(neural.get("command_action", "") or "").strip().lower()
        rf_busy = bool(rf.get("command_busy", False))
        rf_busy_action = str(rf.get("command_action", "") or "").strip().lower()
        rf_power_state = str(rf.get("power_state", "unknown") or "unknown").strip().lower()
        selected_save_mode = str(self.save_combo.currentData() or "")
        selected_mode_saving = bool(save_flags.get(selected_save_mode, False))

        self._set_button_state(
            self.neural_connect_btn,
            text=(
                "Disconnecting Neural..."
                if neural_command_busy and neural_command_action == "disconnect"
                else (
                    "Connecting Neural..."
                    if neural_command_busy and neural_command_action == "connect"
                    else ("Disconnect Neural" if neural_connected else "Connect Neural")
                )
            ),
            tone="warning" if neural_command_busy else ("danger" if neural_connected else "success"),
            enabled=not neural_command_busy,
        )
        self._set_button_state(
            self.rf_connect_btn,
            text=(
                "RF Connecting..."
                if rf_busy and rf_busy_action == "connect"
                else ("Disconnect RF" if rf_connected else "Connect RF")
            ),
            tone="warning" if rf_busy else ("danger" if rf_connected else "success"),
            enabled=not rf_busy,
        )
        self._set_button_state(
            self.sample_start_btn,
            text="Stop Sampling" if neural_sampling else "Start Sampling",
            tone="danger" if neural_sampling else "accent",
            enabled=neural_connected,
        )
        self._set_button_state(
            self.save_on_btn,
            text="Save Off" if selected_mode_saving else "Save On",
            tone="danger" if selected_mode_saving else "success",
            enabled=neural_connected and global_save_enabled,
        )
        self._refresh_imu_apply_button()
        self.global_save_toggle.setEnabled(neural_connected)
        self.camera_show_btn.setEnabled(camera_open)
        self._set_button_state(
            self.rf_on_btn,
            text="RF Off" if rf_power_state == "on" else "RF On",
            tone="danger" if rf_power_state == "on" else "success",
            enabled=rf_connected and not rf_busy,
        )
        self.rf_off_btn.setEnabled(False)
        self.rf_query_btn.setEnabled(rf_connected and not rf_busy)
        spike_filter_supported = bool(neural.get("ui_capabilities", {}).get("remote_spike_filter_config", True))
        impedance_rc_supported = bool(neural.get("ui_capabilities", {}).get("remote_impedance_compensation", True))
        self.spike_filter_checkbox.setEnabled(neural_connected and spike_filter_supported)
        self.spike_filter_low_spin.setEnabled(neural_connected and spike_filter_supported)
        self.spike_filter_high_spin.setEnabled(neural_connected and spike_filter_supported)
        self.spike_filter_sample_rate_combo.setEnabled(neural_connected and spike_filter_supported)
        self._set_button_state(
            self.spike_filter_apply_btn,
            text="Apply Filter",
            tone="accent" if spike_filter_supported else "ghost",
            enabled=neural_connected and spike_filter_supported,
        )
        self.impedance_series_spin.setEnabled(impedance_rc_supported)
        self.impedance_shunt_spin.setEnabled(impedance_rc_supported)
        self._set_button_state(
            self.impedance_apply_btn,
            text="Apply RC",
            tone="accent" if impedance_rc_supported else "ghost",
            enabled=impedance_rc_supported,
        )
        self.charging_guard_checkbox.setEnabled(camera_open)
        self.charging_guard_config_btn.setEnabled(True)
        for widget in (
            self.habits_pause_btn,
            self.habits_time_btn,
            self.habits_handshake_btn,
            self.habits_cap_btn,
            self.protocol_flow_btn,
            self.post_training_btn,
            self.reward_apply_btn,
            self.light_apply_btn,
            self.protocol_apply_btn,
            self.inter_block_apply_btn,
        ):
            widget.setEnabled(habits_connected)
        self.protocol_apply_btn.setEnabled(
            habits_connected and int(self.protocol_spin.value()) <= 10
        )
        self._set_button_state(
            self.habits_read_all_btn,
            text="Read All Values",
            tone="accent",
            enabled=habits_connected,
        )
        self.habits_data_dir_btn.setEnabled(True)
        self.habits_start_date_edit.setEnabled(True)

        self.detail_toggle_btn.blockSignals(True)
        self.detail_toggle_btn.setChecked(detail_attached)
        self.detail_toggle_btn.setText("Close Charts" if detail_attached else "Open Charts")
        self.detail_toggle_btn.blockSignals(False)

    def refresh_runtime_views(self, runtime_cache: SlotRuntimeCache) -> None:
        now_mono = time.monotonic()
        if (now_mono - self._last_timeline_refresh_epoch) >= float(self.timeline_refresh_s):
            self._last_timeline_refresh_epoch = now_mono
            self._refresh_timeline(runtime_cache)
        if (now_mono - self._last_log_refresh_epoch) >= float(self.system_log_refresh_s):
            self._last_log_refresh_epoch = now_mono
            self._refresh_system_log(runtime_cache)

    def _refresh_timeline(self, runtime_cache: SlotRuntimeCache) -> None:
        try:
            mtime = os.path.getmtime(runtime_cache.battery_history_path)
        except OSError:
            mtime = 0.0
        live_record = self._read_live_battery_record(runtime_cache)
        if mtime <= self._last_timeline_mtime:
            self._append_live_battery_record(live_record)
            return
        self._last_timeline_mtime = mtime
        raw_records = read_json(runtime_cache.battery_history_path, None)
        if not isinstance(raw_records, list):
            self._append_live_battery_record(live_record)
            return
        if not raw_records and self._timeline_has_records and live_record is None:
            return
        records, anomalies = build_battery_timeline_payload(runtime_cache.battery_history_path)
        if live_record is not None:
            latest_history_ts = 0.0
            if records:
                try:
                    latest_history_ts = float(records[-1].get("timestamp_epoch", 0.0) or 0.0)
                except Exception:
                    latest_history_ts = 0.0
            if float(live_record.get("timestamp_epoch", 0.0) or 0.0) > latest_history_ts:
                records = list(records) + [dict(live_record)]
        try:
            self.timeline_widget.rebuild_from_history(records, anomalies)
            self._timeline_has_records = bool(records)
            if records:
                self._last_timeline_live_epoch = max(
                    float(self._last_timeline_live_epoch or 0.0),
                    float(records[-1].get("timestamp_epoch", 0.0) or 0.0),
                )
        except Exception:
            return

    def _read_live_battery_record(self, runtime_cache: SlotRuntimeCache) -> Optional[Dict[str, object]]:
        record = {}
        try:
            record = runtime_cache.read_battery_latest({})
        except Exception:
            record = {}
        if not isinstance(record, dict) or not record:
            status_battery = self.current_status.get("battery", {})
            if not isinstance(status_battery, dict):
                return None
            try:
                timestamp_epoch = float(
                    status_battery.get("timestamp_epoch", self.current_status.get("last_update_epoch", 0.0)) or 0.0
                )
            except Exception:
                timestamp_epoch = 0.0
            if timestamp_epoch <= 0.0:
                return None
            neural = self.current_status.get("neural", {})
            rf = self.current_status.get("rf", {})
            habits = self.current_status.get("habits", {})
            record = {
                "timestamp_epoch": timestamp_epoch,
                "rsoc": status_battery.get("reported_rsoc", status_battery.get("rsoc", 0.0)),
                "voltage_mv": status_battery.get("voltage", 0.0),
                "battery_stat": status_battery.get("stat", 0.0),
                "capacity_mAh": status_battery.get("capacity_mAh", 24.0),
                "mode": str(neural.get("active_mode", "Idle") if isinstance(neural, dict) else "Idle"),
                "rf_status": 2 if isinstance(rf, dict) and str(rf.get("power_state", "")).lower() == "on" else 1,
                "trial_paused": bool(habits.get("paused", False)) if isinstance(habits, dict) else False,
            }
        try:
            timestamp_epoch = float(record.get("timestamp_epoch", 0.0) or 0.0)
            rsoc = float(record.get("rsoc", record.get("reported_rsoc", 0.0)) or 0.0)
        except Exception:
            return None
        if timestamp_epoch <= 0.0:
            return None
        return {
            "timestamp_epoch": timestamp_epoch,
            "timestamp_iso": str(record.get("timestamp_iso", "") or ""),
            "rsoc": rsoc,
            "voltage_mv": record.get("voltage_mv", record.get("voltage", 0.0)),
            "battery_stat": record.get("battery_stat", record.get("stat", 0.0)),
            "capacity_mAh": record.get("capacity_mAh", record.get("battery_capacity_mAh", 24.0)),
            "mode": str(record.get("mode", "Idle") or "Idle"),
            "rf_status": int(record.get("rf_status", 1) or 1),
            "trial_paused": bool(record.get("trial_paused", False)),
        }

    def _append_live_battery_record(self, record: Optional[Dict[str, object]]) -> bool:
        if not isinstance(record, dict):
            return False
        try:
            timestamp_epoch = float(record.get("timestamp_epoch", 0.0) or 0.0)
        except Exception:
            return False
        if timestamp_epoch <= float(self._last_timeline_live_epoch or 0.0):
            return False
        try:
            self.timeline_widget.update_data(
                timestamp_epoch,
                float(record.get("rsoc", 0.0) or 0.0),
                str(record.get("mode", "Idle") or "Idle"),
                int(record.get("rf_status", 1) or 1),
                trial_paused=bool(record.get("trial_paused", False)),
                battery_capacity_mAh=float(record.get("capacity_mAh", 24.0) or 24.0),
            )
        except TypeError:
            self.timeline_widget.update_data(
                timestamp_epoch,
                float(record.get("rsoc", 0.0) or 0.0),
                str(record.get("mode", "Idle") or "Idle"),
                int(record.get("rf_status", 1) or 1),
                trial_paused=bool(record.get("trial_paused", False)),
            )
        try:
            self._last_timeline_live_epoch = timestamp_epoch
            self._timeline_has_records = True
            return True
        except Exception:
            return False

    def _refresh_system_log(self, runtime_cache: SlotRuntimeCache) -> None:
        try:
            mtime = os.path.getmtime(runtime_cache.system_log_path)
        except OSError:
            mtime = 0.0
        if mtime <= self._last_log_mtime:
            return
        self._last_log_mtime = mtime
        entries = runtime_cache.read_system_log([])
        if not isinstance(entries, list):
            entries = []
        self.system_log_view.set_entries(entries[-20:])

    def _set_combo_value(self, combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _sync_spin_value(self, widget: QSpinBox, value) -> None:
        if value is None or widget.hasFocus():
            return
        try:
            normalized = int(value)
        except Exception:
            return
        if widget.value() == normalized:
            return
        widget.blockSignals(True)
        widget.setValue(normalized)
        widget.blockSignals(False)

    def _sync_date_value(self, widget: QDateEdit, date_text: str) -> None:
        if not date_text or widget.hasFocus():
            return
        parsed = QDate.fromString(str(date_text), "yyyy-MM-dd")
        if not parsed.isValid():
            parsed = QDate.fromString(str(date_text))
        if not parsed.isValid() or widget.date() == parsed:
            return
        widget.blockSignals(True)
        widget.setDate(parsed)
        widget.blockSignals(False)

    def _set_button_state(self, button: QPushButton, *, text: str, tone: str, enabled: bool) -> None:
        set_text_if_changed(button, text)
        _set_button_tone(button, tone)
        set_enabled_if_changed(button, bool(enabled))

    def _set_progress_bar_style(self, progress_bar: QProgressBar, active: bool) -> None:
        tone = "active" if active else "idle"
        if progress_bar.property("progressTone") == tone:
            return
        progress_bar.setProperty("progressTone", tone)
        if active:
            progress_bar.setStyleSheet(
                "QProgressBar { background: #E2E8F0; border: 1px solid #CBD5E1; border-radius: 8px; text-align: center; }"
                "QProgressBar::chunk { background: #0F766E; border-radius: 7px; }"
            )
            return
        progress_bar.setStyleSheet(
            "QProgressBar { background: #F1F5F9; border: 1px solid #E2E8F0; border-radius: 8px; text-align: center; color: #64748B; }"
            "QProgressBar::chunk { background: #94A3B8; border-radius: 7px; }"
        )


class MasterConsoleWindow(QMainWindow):
    def __init__(self, config_path: str):
        super().__init__()
        self.setWindowTitle("Neural Recorder Master Console")
        self.resize(1880, 1080)
        self.setStyleSheet(MASTER_CONSOLE_STYLESHEET)
        apply_windows_process_role("master_console")
        self.ctx = mp.get_context("spawn")
        self.package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.runtime_root = os.path.join(get_application_directory(), "runtime_cache")
        os.makedirs(self.runtime_root, exist_ok=True)
        self.runtime_config = load_runtime_config(self.package_root)
        self.master_runtime_config = dict(self.runtime_config.get("master_console", {}))
        self.alarm_config_path = os.path.join(self.package_root, ALARM_CONFIG_FILENAME)
        self.alarm_config = load_alarm_config(self.alarm_config_path)
        self.slot_configs = load_master_console_config(config_path)
        self.slot_handles: Dict[str, SlotHandle] = {}
        self.slot_cards: Dict[str, SlotCard] = {}
        self.camera_catalog: List[Dict[str, object]] = []
        self.camera_preview_windows: Dict[str, CameraPreviewWindow] = {}
        self._clear_transient_runtime_cache()
        self._build_ui()
        self.refresh_camera_catalog()
        self._start_slots()

        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._refresh_cards)
        self.status_timer.start(int(self.master_runtime_config.get("status_refresh_ms", 1000)))

        self.alarm_timer = QTimer(self)
        self.alarm_timer.timeout.connect(self._run_alarm_check)
        if self.alarm_config.enabled:
            self.alarm_timer.start(max(60000, int(self.alarm_config.check_interval_minutes * 60 * 1000)))

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("MasterConsoleRoot")
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(10, 10, 10, 10)
        outer_layout.setSpacing(0)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        scroll_content = QWidget()
        cards_layout = QHBoxLayout(scroll_content)
        cards_layout.setContentsMargins(12, 12, 12, 12)
        cards_layout.setSpacing(16)
        cards_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        for slot_config in self.slot_configs:
            card = SlotCard(
                slot_config,
                self.send_slot_command,
                self.set_detail_enabled,
                self.refresh_camera_catalog,
                self.set_camera_preview_enabled,
            )
            card.system_log_refresh_s = max(
                0.05,
                float(self.master_runtime_config.get("system_log_refresh_ms", 2000) or 2000) / 1000.0,
            )
            card.timeline_refresh_s = max(
                0.05,
                float(self.master_runtime_config.get("timeline_refresh_ms", 5000) or 5000) / 1000.0,
            )
            card.set_camera_catalog(self.camera_catalog)
            cards_layout.addWidget(card, 1)
            self.slot_cards[slot_config.slot_id] = card

        scroll_area.setWidget(scroll_content)
        outer_layout.addWidget(scroll_area)
        self.setCentralWidget(central)
        self.statusBar().hide()

    def _start_slots(self) -> None:
        for slot_config in self.slot_configs:
            command_queue = self.ctx.Queue()
            priority_command_queue = self.ctx.Queue()
            slot_payload = {
                "slot_id": slot_config.slot_id,
                "label": slot_config.label,
                "profile_path": slot_config.profile_path,
                "enabled": slot_config.enabled,
                "auto_start": slot_config.auto_start,
            }
            process = self.ctx.Process(
                target=run_slot_service,
                args=(slot_payload, self.runtime_root, command_queue, priority_command_queue),
                daemon=False,
            )
            process.start()
            self.slot_handles[slot_config.slot_id] = SlotHandle(
                slot_config=slot_config,
                runtime_cache=SlotRuntimeCache(self.runtime_root, slot_config.slot_id),
                command_queue=command_queue,
                service_process=process,
                priority_command_queue=priority_command_queue,
            )

    def _clear_transient_runtime_cache(self) -> None:
        for slot_config in self.slot_configs:
            try:
                SlotRuntimeCache(self.runtime_root, slot_config.slot_id).clear_transient_files()
            except Exception:
                pass

    def send_slot_command(self, slot_id: str, command: Dict[str, object]) -> None:
        handle = self.slot_handles.get(slot_id)
        if handle is None:
            return
        try:
            target_queue = (
                handle.priority_command_queue
                if _is_priority_slot_command(command) and handle.priority_command_queue is not None
                else handle.command_queue
            )
            target_queue.put(command, block=False)
        except Exception:
            pass

    def refresh_camera_catalog(self, _slot_id: Optional[str] = None) -> None:
        try:
            detected = list(get_available_cameras() or [])
        except Exception:
            detected = []
        self.camera_catalog = []
        for camera in detected:
            camera_id = camera.get("id")
            display = (
                f"Camera {camera_id} "
                f"({camera.get('resolution', '--')}, {float(camera.get('fps', 0.0) or 0.0):.1f} fps)"
            )
            self.camera_catalog.append({
                "id": camera_id,
                "name": camera.get("name", f"Camera {camera_id}"),
                "display": display,
            })
        for card in self.slot_cards.values():
            card.set_camera_catalog(self.camera_catalog)

    def set_camera_preview_enabled(self, slot_id: str, enabled: bool) -> None:
        handle = self.slot_handles.get(slot_id)
        if handle is None:
            return
        self.send_slot_command(slot_id, make_slot_command("set_camera_preview", enabled=bool(enabled)))
        if enabled:
            existing = self.camera_preview_windows.get(slot_id)
            if existing is not None:
                existing.show()
                existing.raise_()
                existing.activateWindow()
                return
            window = CameraPreviewWindow(
                handle.slot_config.label,
                handle.runtime_cache,
                poll_interval_ms=int(self.master_runtime_config.get("camera_preview_poll_ms", 120)),
            )
            window.closed.connect(lambda sid=slot_id: self._on_camera_preview_closed(sid))
            self.camera_preview_windows[slot_id] = window
            window.show()
            return
        self._close_camera_preview_window(slot_id)

    def _on_camera_preview_closed(self, slot_id: str) -> None:
        if slot_id in self.camera_preview_windows:
            self.camera_preview_windows.pop(slot_id, None)
        self.send_slot_command(slot_id, make_slot_command("set_camera_preview", enabled=False))
        card = self.slot_cards.get(slot_id)
        if card is not None:
            card.force_camera_preview_state(False)

    def _close_camera_preview_window(self, slot_id: str) -> None:
        window = self.camera_preview_windows.pop(slot_id, None)
        if window is None:
            return
        try:
            window.blockSignals(True)
            window.close()
        except Exception:
            pass

    def set_detail_enabled(self, slot_id: str, enabled: bool) -> None:
        handle = self.slot_handles.get(slot_id)
        if handle is None:
            return
        self.send_slot_command(slot_id, make_slot_command("detail_attach", attached=bool(enabled)))

    def _refresh_cards(self) -> None:
        for slot_id, handle in self.slot_handles.items():
            status = handle.runtime_cache.read_status({})
            card = self.slot_cards.get(slot_id)
            if card is not None:
                if status:
                    card.update_status(status)
                card.refresh_runtime_views(handle.runtime_cache)

    def _run_alarm_check(self) -> None:
        if not self.alarm_config.enabled:
            return
        active_issues = []
        for index, slot_config in enumerate(self.slot_configs):
            handle = self.slot_handles.get(slot_config.slot_id)
            if handle is None:
                continue
            status = handle.runtime_cache.read_status({})
            if not status:
                continue
            active_issues.extend(
                evaluate_slot_alarm_conditions(
                    slot_label=slot_config.label,
                    slot_id=slot_config.slot_id,
                    card_position=build_card_position(index),
                    status=status,
                    thresholds=self.alarm_config.thresholds,
                    service_alive=bool(handle.service_process.is_alive()),
                )
            )
        if active_issues:
            send_alarm_email(self.alarm_config, active_issues)

    def closeEvent(self, event) -> None:
        self.status_timer.stop()
        self.alarm_timer.stop()
        progress_total = max(1, len(self.slot_handles) * 2 + len(self.camera_preview_windows) + 2)
        progress = QProgressDialog("Stopping slot services...", None, 0, progress_total, self)
        progress.setWindowTitle("Closing Master Console")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        QApplication.processEvents()
        progress_step = 0
        for slot_id in list(self.camera_preview_windows.keys()):
            progress_step += 1
            progress.setLabelText(f"Closing camera preview {slot_id}...")
            progress.setValue(progress_step)
            QApplication.processEvents()
            self._close_camera_preview_window(slot_id)
        for handle in self.slot_handles.values():
            try:
                handle.command_queue.put(make_slot_command("shutdown"), block=False)
            except Exception:
                pass
        try:
            graceful_timeout_s = max(
                2.0,
                float(self.master_runtime_config.get("slot_shutdown_join_timeout_s", 12.0) or 12.0),
            )
        except Exception:
            graceful_timeout_s = 12.0
        try:
            terminate_timeout_s = max(
                0.5,
                float(self.master_runtime_config.get("slot_shutdown_terminate_join_timeout_s", 2.0) or 2.0),
            )
        except Exception:
            terminate_timeout_s = 2.0
        for handle in self.slot_handles.values():
            progress_step += 1
            progress.setLabelText(f"Waiting for {handle.slot_config.label} to stop...")
            progress.setValue(progress_step)
            QApplication.processEvents()
            try:
                handle.service_process.join(timeout=graceful_timeout_s)
            except Exception:
                pass
            try:
                if handle.service_process.is_alive():
                    progress.setLabelText(f"Force stopping {handle.slot_config.label}...")
                    QApplication.processEvents()
                    handle.service_process.terminate()
                    handle.service_process.join(timeout=terminate_timeout_s)
            except Exception:
                pass
            try:
                handle.runtime_cache.clear_transient_files()
            except Exception:
                pass
            for command_queue in (handle.command_queue, handle.priority_command_queue):
                try:
                    command_queue.cancel_join_thread()
                except Exception:
                    pass
                try:
                    command_queue.close()
                except Exception:
                    pass
            progress_step += 1
            progress.setValue(progress_step)
            QApplication.processEvents()
        progress.setLabelText("Closing runtime cache...")
        progress.setValue(progress_total)
        QApplication.processEvents()
        progress.close()
        super().closeEvent(event)


def main() -> None:
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    config_path = os.path.join(package_root, MASTER_CONFIG_FILENAME)
    configure_qt_runtime()
    app = QApplication(sys.argv)
    window = MasterConsoleWindow(config_path)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    mp.freeze_support()
    main()
