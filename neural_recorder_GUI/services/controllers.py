import json
import math
import os
import queue
import threading
import time
from collections import deque
from contextlib import nullcontext
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import cv2
import serial
import serial.tools.list_ports
from PyQt6.QtCore import QObject, pyqtSignal

try:
    from ..hardware.camera_module import CameraModule
    from ..master_app.contracts import extract_mouse_id_from_directory
    from ..hardware.neural_reader import (
        GUI_INTERVAL_DEFAULTS,
        GUI_STREAM_INTERVAL_DEFAULTS,
        GUI_STREAM_TARGET_FPS_DEFAULTS,
        RELAY_RECOMMENDED_ESB_CHANNELS,
        SerialPort,
        STREAM_MODE_IDLE,
        _default_power_guard_status,
        build_mode0_quant_config_command,
        build_runtime_device_esb_channel_command,
        build_runtime_device_esb_mode_config_command,
        build_runtime_device_reboot_command,
    )
    from ..recorder_app.optimized_habits_panel import SerialWorker, TrialDataManager
    from ..storage.runtime_cache import atomic_write_json, read_json
    from ..support.path_utils import build_daily_file_path
    from .protocol_flow import (
        ProtocolFlowStore,
        condition_to_firmware_payload,
        flow_state_from_firmware_fields,
        flow_to_firmware_payload,
    )
    from .recovery_utils import extract_port_fingerprint, resolve_reconnect_port
except ImportError:
    from hardware.camera_module import CameraModule
    from master_app.contracts import extract_mouse_id_from_directory
    from hardware.neural_reader import (
        GUI_INTERVAL_DEFAULTS,
        GUI_STREAM_INTERVAL_DEFAULTS,
        GUI_STREAM_TARGET_FPS_DEFAULTS,
        RELAY_RECOMMENDED_ESB_CHANNELS,
        SerialPort,
        STREAM_MODE_IDLE,
        _default_power_guard_status,
        build_mode0_quant_config_command,
        build_runtime_device_esb_channel_command,
        build_runtime_device_esb_mode_config_command,
        build_runtime_device_reboot_command,
    )
    from recorder_app.optimized_habits_panel import SerialWorker, TrialDataManager
    from storage.runtime_cache import atomic_write_json, read_json
    from support.path_utils import build_daily_file_path
    from services.protocol_flow import (
        ProtocolFlowStore,
        condition_to_firmware_payload,
        flow_state_from_firmware_fields,
        flow_to_firmware_payload,
    )
    from services.recovery_utils import extract_port_fingerprint, resolve_reconnect_port


MODE_LABELS = {
    0: "Mode0 LFP+MAND+Raw",
    1: "single channel Spike",
    2: "16 channels Spike",
    3: "ESA&MUA",
}
MODE_LABEL_TO_VALUE = {
    str(label).strip().lower(): int(value) for value, label in MODE_LABELS.items()
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
IMU_MODE_LABELS = {
    1: "low-power",
    2: "Accel",
    3: "Accel+gyro",
}
CAP_WARNING_THRESHOLD = 8
CAP_ALARM_THRESHOLD = 12
CAP_HISTORY_LIMIT = 512
HABITS_LIVE_UPDATE_LIMIT = 20000
IMPEDANCE_HISTORY_LIMIT = 512
AUTO_THRESHOLD_CHANNEL_COUNT = 16
MODE2_FIXED_CHANNELS = list(range(16))
MODE1_SPIKE_SAMPLE_RATE_HZ = 20833
AUTO_THRESHOLD_WINDOW_SECONDS = 5
# Match the original GUI sweep window: 5 seconds of Mode1 raw data.
AUTO_THRESHOLD_SAMPLE_TARGET = (MODE1_SPIKE_SAMPLE_RATE_HZ * AUTO_THRESHOLD_WINDOW_SECONDS) // 2
DEFAULT_SPIKE_FILTER_LOW_CUT_HZ = 300.0
DEFAULT_SPIKE_FILTER_HIGH_CUT_HZ = 3000.0
DEFAULT_SPIKE_FILTER_SAMPLE_RATE_HZ = 20000.0
DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM = 220.0
DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF = 47.0
DEFAULT_MODE3_SPIKE_THRESHOLD_UV = 60.0
LEGACY_GUI_INTERVAL_DEFAULTS = {"mode0": 20, "mode1": 20, "mode2": 4, "mode3": 20}
LEGACY_GUI_STREAM_INTERVAL_DEFAULTS = {
    "mode0_lfp": 20,
    "mode1_raw": 20,
    "mode2_raw": 4,
    "mode3_lfp_esa": 20,
    "mode3_raw": 20,
}


def _mode_json_key(mode: Any) -> str:
    try:
        return f"mode{int(mode)}"
    except Exception:
        return "mode0"


def _normalize_mode_float_mapping(mapping: Any, default: float = 0.0) -> Dict[str, float]:
    normalized = {_mode_json_key(mode): float(default) for mode in sorted(GUI_INTERVAL_DEFAULTS)}
    if not isinstance(mapping, dict):
        return normalized
    for key, value in mapping.items():
        try:
            if isinstance(key, str) and key.lower().startswith("mode"):
                mode = int(key[4:])
            else:
                mode = int(key)
        except Exception:
            continue
        if mode not in GUI_INTERVAL_DEFAULTS:
            continue
        try:
            normalized[_mode_json_key(mode)] = float(value or 0.0)
        except Exception:
            normalized[_mode_json_key(mode)] = float(default)
    return normalized


def _normalize_gui_interval_mapping(mapping: Any) -> Dict[str, int]:
    normalized = {
        _mode_json_key(mode): int(default)
        for mode, default in sorted(GUI_INTERVAL_DEFAULTS.items())
    }
    if not isinstance(mapping, dict):
        return normalized
    for key, value in mapping.items():
        try:
            if isinstance(key, str) and key.lower().startswith("mode"):
                mode = int(key[4:])
            else:
                mode = int(key)
        except Exception:
            continue
        if mode not in GUI_INTERVAL_DEFAULTS:
            continue
        try:
            normalized[_mode_json_key(mode)] = max(1, int(value))
        except Exception:
            pass
    return normalized


def _stream_mode_key(stream_key: str) -> str:
    if stream_key == "mode0_lfp":
        return "mode0"
    if stream_key == "mode1_raw":
        return "mode1"
    if stream_key == "mode2_raw":
        return "mode2"
    return "mode3"


def _normalize_gui_stream_interval_mapping(mapping: Any, mode_intervals: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    explicit_modes: Dict[str, int] = {}
    if isinstance(mode_intervals, dict):
        for key, value in mode_intervals.items():
            try:
                if isinstance(key, str) and key.lower().startswith("mode"):
                    mode = int(key[4:])
                else:
                    mode = int(key)
            except Exception:
                continue
            if mode not in GUI_INTERVAL_DEFAULTS:
                continue
            try:
                explicit_modes[_mode_json_key(mode)] = max(1, int(value))
            except Exception:
                continue
    normalized = {
        stream_key: int(explicit_modes.get(_stream_mode_key(stream_key), default))
        for stream_key, default in sorted(GUI_STREAM_INTERVAL_DEFAULTS.items())
    }
    if not isinstance(mapping, dict):
        return normalized
    aliases = {
        "0": "mode0_lfp",
        "mode0": "mode0_lfp",
        "mode0_lfp": "mode0_lfp",
        "mode0_lfp_mand_raw": "mode0_lfp",
        "mode0_lfp+mand+raw": "mode0_lfp",
        "mode0_lfp_mand": "mode0_lfp",
        "lfp": "mode0_lfp",
        "1": "mode1_raw",
        "mode1": "mode1_raw",
        "mode1_raw": "mode1_raw",
        "raw": "mode1_raw",
        "spike": "mode1_raw",
        "2": "mode2_raw",
        "mode2": "mode2_raw",
        "mode2_raw": "mode2_raw",
        "3": "mode3_lfp_esa",
        "mode3": "mode3_lfp_esa",
        "mode3_lfp": "mode3_lfp_esa",
        "mode3_esa": "mode3_lfp_esa",
        "mode3_lfp_esa": "mode3_lfp_esa",
        "mode3_raw": "mode3_raw",
    }
    for key, value in mapping.items():
        stream_key = aliases.get(str(key).strip().lower(), str(key).strip().lower())
        if stream_key not in normalized:
            continue
        try:
            normalized[stream_key] = max(1, int(value))
        except Exception:
            pass
    return normalized


def _normalize_gui_interval_stats(mapping: Any, intervals: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, Any]]:
    current_intervals = _normalize_gui_interval_mapping(intervals or {})
    stats: Dict[str, Dict[str, Any]] = {}
    incoming = mapping if isinstance(mapping, dict) else {}
    for mode in sorted(GUI_INTERVAL_DEFAULTS):
        key = _mode_json_key(mode)
        item = incoming.get(key, incoming.get(mode, {}))
        if not isinstance(item, dict):
            item = {}
        interval = int(current_intervals.get(key, GUI_INTERVAL_DEFAULTS[mode]))
        try:
            best_interval = max(1, int(item.get("best_interval", interval)))
        except Exception:
            best_interval = interval
        best_loss = item.get("best_loss_percent")
        try:
            best_loss = None if best_loss is None else float(best_loss)
        except Exception:
            best_loss = None
        stats[key] = {
            "interval": interval,
            "best_interval": best_interval,
            "best_loss_percent": best_loss,
            "last_loss_percent": float(item.get("last_loss_percent", 0.0) or 0.0),
            "last_expected_packets": int(item.get("last_expected_packets", 0) or 0),
            "updated_epoch": float(item.get("updated_epoch", 0.0) or 0.0),
        }
    return stats


def _normalize_gui_stream_interval_stats(mapping: Any, intervals: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, Any]]:
    current_intervals = _normalize_gui_stream_interval_mapping(intervals or {})
    stats: Dict[str, Dict[str, Any]] = {}
    incoming = mapping if isinstance(mapping, dict) else {}
    for stream_key in sorted(GUI_STREAM_INTERVAL_DEFAULTS):
        item = incoming.get(stream_key, {})
        if not isinstance(item, dict):
            item = {}
        interval = int(current_intervals.get(stream_key, GUI_STREAM_INTERVAL_DEFAULTS[stream_key]))
        try:
            best_interval = max(1, int(item.get("best_interval", interval)))
        except Exception:
            best_interval = interval
        best_loss = item.get("best_loss_percent")
        try:
            best_loss = None if best_loss is None else float(best_loss)
        except Exception:
            best_loss = None
        stats[stream_key] = {
            "interval": interval,
            "best_interval": best_interval,
            "best_loss_percent": best_loss,
            "last_loss_percent": float(item.get("last_loss_percent", 0.0) or 0.0),
            "loss_ewma_percent": float(item.get("loss_ewma_percent", 0.0) or 0.0),
            "last_expected_packets": int(item.get("last_expected_packets", 0) or 0),
            "stable_windows": int(item.get("stable_windows", 0) or 0),
            "updated_epoch": float(item.get("updated_epoch", 0.0) or 0.0),
            "last_action": str(item.get("last_action", "") or ""),
            "pressure_reason": str(item.get("pressure_reason", "") or ""),
        }
    return stats


def _normalize_gui_interval_control(payload: Any) -> Dict[str, Any]:
    incoming = payload if isinstance(payload, dict) else {}
    enabled = incoming.get("target_fps_enabled", incoming.get("enabled", True))
    targets = dict(GUI_STREAM_TARGET_FPS_DEFAULTS)
    target_payload = incoming.get("target_fps_by_stream", {})
    if not isinstance(target_payload, dict):
        target_payload = {}
    aliases = {
        "mode0": "mode0_lfp",
        "mode0_lfp": "mode0_lfp",
        "mode0_lfp_mand_raw": "mode0_lfp",
        "mode0_lfp+mand+raw": "mode0_lfp",
        "mode0_lfp_mand": "mode0_lfp",
        "lfp": "mode0_lfp",
        "mode1": "mode1_raw",
        "mode1_raw": "mode1_raw",
        "raw": "mode1_raw",
        "spike": "mode1_raw",
        "mode2": "mode2_raw",
        "mode2_raw": "mode2_raw",
        "mode3": "mode3_lfp_esa",
        "mode3_lfp": "mode3_lfp_esa",
        "mode3_esa": "mode3_lfp_esa",
        "mode3_lfp_esa": "mode3_lfp_esa",
        "mode3_raw": "mode3_raw",
    }
    for key, value in {**incoming, **target_payload}.items():
        stream_key = aliases.get(str(key).strip().lower())
        if stream_key not in targets:
            continue
        try:
            target_fps = max(0.0, float(value))
            targets[stream_key] = min(30.0, target_fps)
        except Exception:
            continue
    return {
        "target_fps_enabled": bool(enabled),
        "target_fps_by_stream": targets,
    }


def _looks_like_untuned_legacy_gui_profile(intervals: Any, stats: Any, legacy_defaults: Dict[str, int]) -> bool:
    if not isinstance(intervals, dict):
        return False
    for key, default in legacy_defaults.items():
        try:
            value = intervals.get(key, intervals.get(int(key[4:]) if key.startswith("mode") else key))
        except Exception:
            value = intervals.get(key)
        try:
            if int(value) != int(default):
                return False
        except Exception:
            return False
    if not isinstance(stats, dict):
        return True
    for key in legacy_defaults:
        try:
            item = stats.get(key, stats.get(int(key[4:]) if key.startswith("mode") else key, {}))
        except Exception:
            item = stats.get(key, {})
        if not isinstance(item, dict):
            continue
        try:
            if float(item.get("updated_epoch", 0.0) or 0.0) > 0.0:
                return False
        except Exception:
            pass
        try:
            if int(item.get("last_expected_packets", 0) or 0) > 0:
                return False
        except Exception:
            pass
        if item.get("best_loss_percent") is not None:
            return False
        if str(item.get("last_action", "") or "") not in {"", "init"}:
            return False
    return True


def normalize_imu_mode(mode: Any) -> int:
    try:
        normalized = int(mode)
    except Exception:
        normalized = 1
    if normalized not in IMU_MODE_LABELS:
        normalized = 1
    return normalized


def imu_mode_label(mode: Any) -> str:
    return IMU_MODE_LABELS.get(normalize_imu_mode(mode), IMU_MODE_LABELS[1])


def normalize_mode3_reref_mode(mode: Any, fallback: int = 0) -> int:
    try:
        normalized = int(mode)
    except Exception:
        normalized = int(fallback)
    if normalized not in (0, 1, 2):
        try:
            normalized = int(fallback)
        except Exception:
            normalized = 0
    if normalized not in (0, 1, 2):
        normalized = 0
    return normalized


class NeuralController(QObject):
    status_changed = pyqtSignal(dict)
    detail_payload_ready = pyqtSignal(object)
    video_rollover_requested = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, profile: Dict[str, Any]):
        super().__init__()
        self.profile = dict(profile)
        self.serial_thread: Optional[SerialPort] = None
        self._mode0_power_estimator_config: Dict[str, Any] = {}
        initial_imu_mode = normalize_imu_mode(self.profile.get("imu_mode", 2))
        initial_spike_filter_enabled = bool(self.profile.get("spike_filter_enabled", False))
        initial_spike_filter_low_cut_hz = float(
            self.profile.get("spike_filter_low_cut_hz", DEFAULT_SPIKE_FILTER_LOW_CUT_HZ)
            or DEFAULT_SPIKE_FILTER_LOW_CUT_HZ
        )
        initial_spike_filter_high_cut_hz = float(
            self.profile.get("spike_filter_high_cut_hz", DEFAULT_SPIKE_FILTER_HIGH_CUT_HZ)
            or DEFAULT_SPIKE_FILTER_HIGH_CUT_HZ
        )
        initial_spike_filter_sample_rate_hz = float(
            self.profile.get("spike_filter_sample_rate_hz", DEFAULT_SPIKE_FILTER_SAMPLE_RATE_HZ)
            or DEFAULT_SPIKE_FILTER_SAMPLE_RATE_HZ
        )
        initial_rc_series_resistor_kohm = float(
            self.profile.get("impedance_series_resistor_kohm", DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM)
            or DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM
        )
        initial_rc_shunt_cap_pf = float(
            self.profile.get("impedance_shunt_cap_pf", DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF)
            or DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF
        )
        initial_gui_interval_profile = self.profile.get(
            "gui_update_intervals_by_mode",
            self.profile.get("gui_update_intervals", {}),
        )
        initial_gui_interval_stats_profile = self.profile.get("gui_update_interval_stats", {})
        initial_gui_stream_interval_profile = self.profile.get("gui_update_intervals_by_stream", {})
        initial_gui_stream_interval_stats_profile = self.profile.get("gui_update_interval_stream_stats", {})
        stream_mode_profile = initial_gui_interval_profile
        if _looks_like_untuned_legacy_gui_profile(
            initial_gui_interval_profile,
            initial_gui_interval_stats_profile,
            LEGACY_GUI_INTERVAL_DEFAULTS,
        ):
            initial_gui_interval_profile = {}
            stream_mode_profile = {}
        if _looks_like_untuned_legacy_gui_profile(
            initial_gui_stream_interval_profile,
            initial_gui_stream_interval_stats_profile,
            LEGACY_GUI_STREAM_INTERVAL_DEFAULTS,
        ):
            initial_gui_stream_interval_profile = {}
        initial_gui_intervals = _normalize_gui_interval_mapping(initial_gui_interval_profile)
        initial_gui_interval_stats = _normalize_gui_interval_stats(
            initial_gui_interval_stats_profile,
            initial_gui_intervals,
        )
        initial_gui_stream_intervals = _normalize_gui_stream_interval_mapping(
            initial_gui_stream_interval_profile,
            stream_mode_profile,
        )
        initial_gui_stream_interval_stats = _normalize_gui_stream_interval_stats(
            initial_gui_stream_interval_stats_profile,
            initial_gui_stream_intervals,
        )
        initial_gui_interval_control = _normalize_gui_interval_control(
            self.profile.get("gui_update_interval_control", {})
        )
        self.status: Dict[str, Any] = {
            "connected": False,
            "command_busy": False,
            "command_action": "",
            "connection_state": "idle",
            "port": str(profile.get("main_serial_port", "")),
            "active_mode": "Idle",
            "sampling": False,
            "save_mode": "",
            "save_route": {
                "data_mode": "",
                "path_role": "",
            },
            "mode3_reref_pending": False,
            "mode3_reref_target_mode": 0,
            "spike_filter_enabled": initial_spike_filter_enabled,
            "spike_filter_low_cut_hz": initial_spike_filter_low_cut_hz,
            "spike_filter_high_cut_hz": initial_spike_filter_high_cut_hz,
            "spike_filter_sample_rate_hz": initial_spike_filter_sample_rate_hz,
            "rc_series_resistor_kohm": initial_rc_series_resistor_kohm,
            "rc_shunt_cap_pf": initial_rc_shunt_cap_pf,
            "save_flags": {
                "lfp": False,
                "mode1": False,
                "mode2": False,
                "mode3": False,
            },
            "save_progress": {
                "lfp": 0.0,
                "mode1": 0.0,
                "mode2": 0.0,
                "mode3": 0.0,
            },
            "packet_loss": 0,
            "packet_count": 0,
            "packet_loss_batch": 0,
            "packet_count_batch": 0,
            "packet_loss_percent_current": 0.0,
            "packet_loss_expected_current": 0,
            "packet_metrics_seq": 0,
            "serial_backlog_event_count": 0,
            "serial_backlog_last_bytes": 0,
            "serial_backlog_peak_bytes": 0,
            "serial_backlog_active": False,
            "serial_backlog_last_epoch": 0.0,
            "serial_read_gap_ms": 0.0,
            "serial_read_gap_peak_ms": 0.0,
            "serial_read_gap_event_count": 0,
            "serial_read_gap_last_epoch": 0.0,
            "serial_read_gap_trace_seq": 0,
            "serial_read_gap_trace": [],
            "read_batch_bytes": 0,
            "read_batch_bytes_by_mode": _normalize_mode_float_mapping({}),
            "read_batch_packets_by_mode": _normalize_mode_float_mapping({}),
            "read_batch_packet_redline_by_mode": _normalize_mode_float_mapping(
                SerialPort._read_batch_packet_redline_status_payload()
            ),
            "read_batch_packet_warn_by_mode": _normalize_mode_float_mapping(
                SerialPort._read_batch_packet_warn_status_payload()
            ),
            "port_in_waiting_before_read": 0,
            "writer_lag": 0,
            "save_queue_drop_count": 0,
            "save_enqueue_ms": 0.0,
            "save_enqueue_type": "",
            "packet_gap_event_count": 0,
            "packet_gap_missing": 0,
            "packet_gap_summary": "",
            "packet_gap_diagnostics": {},
            "packet_out_of_order_event_count": 0,
            "packet_out_of_order_summary": "",
            "packet_counter_reset_event_count": 0,
            "packet_counter_reset_summary": "",
            "progress_percent": 0.0,
            "run_time_min": 0.0,
            "detail_enabled": False,
            "detail_throttled": False,
            "detail_dropped_frames": 0,
            "detail_last_skip_reason": "",
            "gui_update_intervals_by_mode": dict(initial_gui_intervals),
            "gui_update_interval_best_by_mode": dict(initial_gui_intervals),
            "gui_update_interval_stats": dict(initial_gui_interval_stats),
            "gui_update_intervals_by_stream": dict(initial_gui_stream_intervals),
            "gui_update_interval_best_by_stream": dict(initial_gui_stream_intervals),
            "gui_update_interval_stream_stats": dict(initial_gui_stream_interval_stats),
            "gui_update_interval_control": dict(initial_gui_interval_control),
            "pipeline": {
                "running": False,
                "worker_alive": False,
                "raw_queue_depth": 0,
                "event_queue_depth": 0,
                "submitted_frames": 0,
                "processed_frames": 0,
                "emitted_events": 0,
                "dropped_raw_frames": 0,
                "dropped_events": 0,
                "coalesced_events": 0,
                "last_error": "",
            },
            "global_save_enabled": True,
            "imu_mode": initial_imu_mode,
            "imu_mode_text": imu_mode_label(initial_imu_mode),
            "mode3_reref_mode": 0,
            "rssi": 0.0,
            "mode1_raw_channel": 0,
            "mode3_raw_channel": 0,
            "mode0_mand_lag_samples": 7,
            "mode0_mand_window_ms": 4,
            "mode2_channels": list(MODE2_FIXED_CHANNELS),
            "mode3_thresholds": [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16,
            "mode3_threshold_cache_path": "",
            "mode3_threshold_cache_mouse_id": "",
            "mode3_threshold_cache_updated_at": "",
            "mode3_threshold_cache_source": "",
            "auto_threshold_running": False,
            "auto_threshold_channel": -1,
            "impedance_test_running": False,
            "impedance_progress_percent": 0.0,
            "impedance_status_text": "Ready",
            "impedance_last_result": {},
            "impedance_history_count": 0,
            "impedance_result_seq": 0,
            "battery": {
                "rsoc": 0.0,
                "reported_rsoc": 0.0,
                "stat": 0.0,
                "voltage": 0.0,
                "capacity_mAh": 24.0,
            },
            "power_guard": _default_power_guard_status(),
            "mode0_quant": {
                "bit_depth": 12,
                "full_scale_uv": 1000.0,
                "raw_full_scale_uv": 500.0,
                "min_bit_depth": 8,
                "max_bit_depth": 12,
                "min_full_scale_uv": 500.0,
                "max_full_scale_uv": 10000.0,
                "full_scale_step_uv": 500.0,
            },
            "mode0_power": {
                "valid": False,
                "estimated_mw": None,
                "sleep_ratio_percent": 0.0,
                "fail_rate_percent": 0.0,
                "tx_attempt_count": 0,
                "tx_retransmit_count": 0,
                "retransmit_rate_percent": 0.0,
                "avg_retransmits_per_packet": 0.0,
                "tx_power_code": 0,
                "tx_power_dbm": 0,
                "retransmit_count": 0,
                "flags": 0,
                "warning": "",
                "warnings": [],
            },
            "ui_capabilities": {
                "remote_mode1_channel": True,
                "remote_mode3_raw_channel": True,
                "remote_mode2_channels": False,
                "remote_spike_threshold": True,
                "remote_auto_threshold": True,
                "remote_impedance_test": True,
                "remote_impedance_compensation": True,
                "remote_spike_filter_config": True,
                "local_spike_spectrum": True,
                "local_lfp_spectrum": True,
            },
            "ui_busy": {
                "auto_threshold": False,
                "impedance_test": False,
                "sampling": False,
            },
            "ui_pending": {
                "mode3_reref": False,
            },
            "telemetry": {
                "detail_payload_bytes": 0,
                "detail_payload_interval_ms": 0.0,
                "detail_throttled": False,
                "detail_dropped_frames": 0,
                "detail_last_skip_reason": "",
                "serial_read_gap_ms": 0.0,
                "serial_read_gap_peak_ms": 0.0,
                "serial_read_gap_event_count": 0,
                "serial_read_gap_trace_seq": 0,
                "serial_read_gap_trace": [],
                "read_batch_bytes_by_mode": _normalize_mode_float_mapping({}),
                "read_batch_packets_by_mode": _normalize_mode_float_mapping({}),
                "read_batch_packet_redline_by_mode": _normalize_mode_float_mapping(
                    SerialPort._read_batch_packet_redline_status_payload()
                ),
                "read_batch_packet_warn_by_mode": _normalize_mode_float_mapping(
                    SerialPort._read_batch_packet_warn_status_payload()
                ),
                "gui_update_intervals_by_mode": dict(initial_gui_intervals),
                "gui_update_intervals_by_stream": dict(initial_gui_stream_intervals),
                "save_enqueue_ms": 0.0,
                "packet_gap_event_count": 0,
                "packet_gap_missing": 0,
                "packet_gap_diagnostics": {},
                "packet_out_of_order_event_count": 0,
                "packet_counter_reset_event_count": 0,
                "runtime_cache_flush_duration_ms": 0.0,
                "runtime_cache_write_failures": 0,
            },
        }
        self.impedance_history: List[Dict[str, Any]] = []
        self._mode3_reref_pending_deadline_monotonic = 0.0
        self._auto_threshold_expected_gui_channel: Optional[int] = None
        self._auto_threshold_sample_buffer: List[float] = []
        self._mode1_threshold_window_gui_channel: Optional[int] = None
        self._mode1_threshold_window_samples: List[float] = []
        self._last_detail_payload_monotonic = 0.0
        self._detail_publish_min_interval_ms = 0
        self._threshold_samples_use_dedicated_signal = False
        self._save_chunk_config = {
            "mode0": 50,
            "mode3": 25,
            "mode3_raw": 25,
        }
        self._load_threshold_cache()

    def _emit_status(self) -> None:
        self._refresh_ui_state_surfaces()
        self.status_changed.emit(dict(self.status))

    def _clear_packet_loss_status_fields(self) -> None:
        self.status["packet_loss"] = 0
        self.status["packet_count"] = 0
        self.status["packet_loss_batch"] = 0
        self.status["packet_count_batch"] = 0
        self.status["packet_loss_percent_current"] = 0.0
        self.status["packet_loss_expected_current"] = 0
        try:
            self.status["packet_metrics_seq"] = int(self.status.get("packet_metrics_seq", 0) or 0) + 1
        except Exception:
            self.status["packet_metrics_seq"] = 1

    def _refresh_ui_state_surfaces(self) -> None:
        self.status["ui_busy"] = {
            "auto_threshold": bool(self.status.get("auto_threshold_running", False)),
            "impedance_test": bool(self.status.get("impedance_test_running", False)),
            "sampling": bool(self.status.get("sampling", False)),
        }
        self.status["ui_pending"] = {
            "mode3_reref": bool(self.status.get("mode3_reref_pending", False)),
        }

    def set_spike_filter_config(
        self,
        *,
        enabled: Optional[bool] = None,
        low_cut_hz: Optional[float] = None,
        high_cut_hz: Optional[float] = None,
        sample_rate_hz: Optional[float] = None,
    ) -> None:
        if enabled is not None:
            self.status["spike_filter_enabled"] = bool(enabled)
        if low_cut_hz is not None:
            self.status["spike_filter_low_cut_hz"] = max(1.0, float(low_cut_hz))
        if high_cut_hz is not None:
            self.status["spike_filter_high_cut_hz"] = max(10.0, float(high_cut_hz))
        if sample_rate_hz is not None:
            normalized_sample_rate = float(sample_rate_hz)
            if normalized_sample_rate not in {12500.0, 20000.0}:
                normalized_sample_rate = DEFAULT_SPIKE_FILTER_SAMPLE_RATE_HZ
            self.status["spike_filter_sample_rate_hz"] = normalized_sample_rate
        self._emit_status()

    def set_impedance_compensation_params(
        self,
        *,
        series_resistor_kohm: Optional[float] = None,
        shunt_cap_pf: Optional[float] = None,
    ) -> None:
        if series_resistor_kohm is not None:
            self.status["rc_series_resistor_kohm"] = max(0.0, float(series_resistor_kohm))
        if shunt_cap_pf is not None:
            self.status["rc_shunt_cap_pf"] = max(0.0, float(shunt_cap_pf))
        self._emit_status()

    def _threshold_cache_mouse_id(self) -> str:
        for key in ("mouse_id", "mice_id", "mouse"):
            value = str(self.profile.get(key, "") or "").strip()
            if value:
                return value
        directory = str(self.profile.get("mice_id_directory", "") or "").strip()
        if directory:
            try:
                return str(extract_mouse_id_from_directory(directory) or "").strip()
            except Exception:
                return os.path.basename(os.path.normpath(directory))
        return ""

    def _threshold_cache_path(self) -> str:
        paths = self._threshold_cache_paths()
        return paths[0] if paths else ""

    def _threshold_cache_paths(self) -> List[str]:
        paths: List[str] = []
        directory = str(self.profile.get("mice_id_directory", "") or "").strip()
        if directory:
            paths.append(os.path.join(directory, "mode3_thresholds.json"))
        fallback = str(
            self.profile.get("mode3_thresholds_fallback_path", "")
            or self.profile.get("threshold_cache_fallback_path", "")
            or ""
        ).strip()
        if fallback and fallback not in paths:
            paths.append(fallback)
        return paths

    def _threshold_cache_is_mouse_directory_path(self, path: str) -> bool:
        directory = str(self.profile.get("mice_id_directory", "") or "").strip()
        if not directory:
            return False
        expected = os.path.join(directory, "mode3_thresholds.json")
        try:
            expected_norm = os.path.normpath(expected).replace("\\", "/").casefold()
            path_norm = os.path.normpath(str(path or "")).replace("\\", "/").casefold()
        except Exception:
            return False
        return bool(path_norm and path_norm == expected_norm)

    def _threshold_cache_payload_mouse_id(self, payload: Any) -> str:
        if not isinstance(payload, dict):
            return ""
        for key in ("mouse_id", "mice_id", "mouse"):
            value = str(payload.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _threshold_cache_payload_matches_current_mouse(self, payload: Any, path: str) -> bool:
        current_mouse = self._threshold_cache_mouse_id()
        if not current_mouse:
            return True
        payload_mouse = self._threshold_cache_payload_mouse_id(payload)
        if payload_mouse:
            return payload_mouse.casefold() == current_mouse.casefold()
        # Older mouse-directory threshold files did not always carry a mouse_id.
        # Keep those compatible, but do not trust anonymous runtime fallback files.
        return self._threshold_cache_is_mouse_directory_path(path)

    def _normalize_threshold_cache_values(self, values: Any) -> List[float]:
        try:
            normalized = [float(value or 0.0) for value in list(values or [])[:16]]
        except Exception:
            normalized = []
        if len(normalized) < 16:
            normalized.extend([DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * (16 - len(normalized)))
        return normalized[:16]

    def _load_threshold_cache(self) -> bool:
        paths = self._threshold_cache_paths()
        path = paths[0] if paths else ""
        self.status["mode3_threshold_cache_path"] = path
        self.status["mode3_threshold_cache_mouse_id"] = self._threshold_cache_mouse_id()
        if not paths:
            return False
        for path in paths:
            payload = read_json(path, {})
            if not isinstance(payload, dict):
                continue
            if not self._threshold_cache_payload_matches_current_mouse(payload, path):
                continue
            values = (
                payload.get("mode3_raw_channel_thresholds_uv")
                or payload.get("mode3_thresholds")
                or payload.get("thresholds")
            )
            thresholds = self._normalize_threshold_cache_values(values)
            if not any(abs(float(value)) > 0.0 for value in thresholds):
                continue
            self.status["mode3_thresholds"] = thresholds
            self.status["mode3_threshold_cache_path"] = path
            self.status["mode3_threshold_cache_updated_at"] = str(payload.get("updated_at", "") or "")
            self.status["mode3_threshold_cache_source"] = str(payload.get("source", "cache") or "cache")
            thread = self.serial_thread
            if thread is not None:
                try:
                    thread.mode3_thresholds_uv = list(thresholds)
                except Exception:
                    pass
            return True
        return False

    def _persist_threshold_cache(self, source: str = "manual", updated_channel: Optional[int] = None) -> None:
        paths = self._threshold_cache_paths()
        if not paths:
            return
        thresholds = self._normalized_mode3_thresholds()
        now = datetime.now().astimezone()
        payload = {
            "version": 1,
            "mouse_id": self._threshold_cache_mouse_id(),
            "updated_at": now.isoformat(timespec="seconds"),
            "updated_epoch": now.timestamp(),
            "source": str(source or "manual"),
            "updated_channel": None if updated_channel is None else int(updated_channel),
            "channel_space": "rhd2132_channel_index",
            "mode3_raw_channel_thresholds_uv": thresholds,
        }
        last_error: Optional[Exception] = None
        written_path = ""
        wrote_any = False
        for path in paths:
            try:
                atomic_write_json(path, payload)
                if not written_path:
                    written_path = path
                last_error = None
                wrote_any = True
            except Exception as exc:
                if not wrote_any:
                    last_error = exc
        if not wrote_any and last_error is not None:
            raise last_error
        self.status["mode3_threshold_cache_path"] = written_path
        self.status["mode3_threshold_cache_mouse_id"] = str(payload["mouse_id"])
        self.status["mode3_threshold_cache_updated_at"] = str(payload["updated_at"])
        self.status["mode3_threshold_cache_source"] = str(payload["source"])

    def _threshold_sample_stream_enabled(self) -> bool:
        return bool(self.status.get("auto_threshold_running", False))

    def _sync_threshold_sample_stream(self) -> None:
        thread = self.serial_thread
        if thread is None or not hasattr(thread, "configure_detail_stream"):
            return
        try:
            thread.configure_detail_stream(
                min_interval_ms=self._detail_publish_min_interval_ms,
                threshold_samples_enabled=self._threshold_sample_stream_enabled(),
            )
        except TypeError:
            thread.configure_detail_stream(min_interval_ms=self._detail_publish_min_interval_ms)

    def configure_save_chunks(self, *, mode0=None, mode3=None, mode3_raw=None) -> None:
        for key, value in (("mode0", mode0), ("mode3", mode3), ("mode3_raw", mode3_raw)):
            if value is None:
                continue
            try:
                self._save_chunk_config[key] = max(1, min(5000, int(value)))
            except Exception:
                pass
        if self.serial_thread is not None and hasattr(self.serial_thread, "configure_save_chunks"):
            self.serial_thread.configure_save_chunks(**self._save_chunk_config)

    def configure_mode0_power_estimator(self, config: Optional[Dict[str, Any]] = None) -> None:
        self._mode0_power_estimator_config = dict(config or {})
        if self.serial_thread is not None and hasattr(self.serial_thread, "configure_mode0_power_estimator"):
            self.serial_thread.configure_mode0_power_estimator(self._mode0_power_estimator_config)

    @staticmethod
    def _threshold_source_from_action(action_name: str) -> str:
        normalized = str(action_name or "").strip().lower()
        if "auto threshold" in normalized:
            return "auto_threshold"
        if "mode1" in normalized:
            return "mode1_channel"
        if "mode3" in normalized:
            return "mode3_raw_channel"
        if "spike threshold" in normalized:
            return "manual_threshold"
        return "manual"

    def connect_port(self) -> bool:
        if self.serial_thread is not None:
            return bool(self.status["connected"])
        port_name = str(self.profile.get("main_serial_port", "")).strip()
        if not port_name:
            self.error.emit("Neural serial port is empty")
            return False
        try:
            self.status["command_busy"] = True
            self.status["command_action"] = "connect"
            self.status["connection_state"] = "connecting"
            self._emit_status()
            self._load_threshold_cache()
            serial_thread = SerialPort(port_name, 2000000)
            serial_thread.mode3_thresholds_uv = list(self.status.get("mode3_thresholds", [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16))
            if hasattr(serial_thread, "configure_mode0_power_estimator"):
                serial_thread.configure_mode0_power_estimator(self._mode0_power_estimator_config)
            serial_thread.GUIUpdate.connect(self._on_gui_update)
            if hasattr(serial_thread, "ThresholdSamplesUpdate"):
                serial_thread.ThresholdSamplesUpdate.connect(self._on_threshold_samples_update)
                self._threshold_samples_use_dedicated_signal = True
            serial_thread.EmptyGUIUpdate.connect(self._on_idle_update)
            serial_thread.ProgressUpdate.connect(self._on_progress_update)
            serial_thread.StatusUpdate.connect(self._on_status_update)
            serial_thread.ImpedanceProgress.connect(self._on_impedance_progress)
            serial_thread.ImpedanceResult.connect(self._on_impedance_result)
            if hasattr(serial_thread, "CameraGUIUpdate"):
                serial_thread.CameraGUIUpdate.connect(self._on_video_rollover_requested)
            serial_thread.SerialDisconnected.connect(self._on_serial_disconnected)
            serial_thread.port_open()
            if hasattr(serial_thread, "configure_detail_stream"):
                serial_thread.configure_detail_stream(
                    min_interval_ms=self._detail_publish_min_interval_ms,
                    threshold_samples_enabled=False,
                )
            if hasattr(serial_thread, "configure_save_chunks"):
                serial_thread.configure_save_chunks(**self._save_chunk_config)
            if hasattr(serial_thread, "configure_gui_update_intervals"):
                try:
                    serial_thread.configure_gui_update_intervals(
                        self.status.get("gui_update_intervals_by_mode", {}),
                        self.status.get("gui_update_interval_stats", {}),
                        self.status.get("gui_update_intervals_by_stream", {}),
                        self.status.get("gui_update_interval_stream_stats", {}),
                    )
                except TypeError:
                    serial_thread.configure_gui_update_intervals(
                        self.status.get("gui_update_intervals_by_mode", {}),
                        self.status.get("gui_update_interval_stats", {}),
                    )
            if hasattr(serial_thread, "configure_gui_update_interval_control"):
                serial_thread.configure_gui_update_interval_control(
                    self.status.get("gui_update_interval_control", {})
                )
            serial_thread.set_detail_enabled(bool(self.status["detail_enabled"]))
            serial_thread.mode3_thresholds_uv = list(self.status.get("mode3_thresholds", [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16))
            serial_thread.start()
            self.serial_thread = serial_thread
            self.status.update({
                "connected": True,
                "command_busy": False,
                "command_action": "",
                "connection_state": "connected",
                "port": port_name,
            })
            self._emit_status()
            return True
        except Exception as exc:
            self.serial_thread = None
            self.status["command_busy"] = False
            self.status["command_action"] = ""
            self.status["connection_state"] = "failed"
            self.error.emit(f"Failed to connect neural serial {port_name}: {exc}")
            self._emit_status()
            return False

    def disconnect_port(self) -> None:
        thread = self.serial_thread
        self.serial_thread = None
        self._threshold_samples_use_dedicated_signal = False
        self.status["command_busy"] = True
        self.status["command_action"] = "disconnect"
        self.status["connection_state"] = "disconnecting"
        self._emit_status()
        self._stop_auto_threshold_update(emit_status=False)
        self._clear_mode1_threshold_window()
        if thread is not None:
            try:
                thread.stop(finalize=False)
            except TypeError:
                try:
                    thread.stop()
                except Exception:
                    pass
            except Exception:
                pass
            try:
                thread.wait(1000)
            except Exception:
                pass
            self._finalize_thread_save_buffers(thread, self._thread_active_save_modes(thread))
            try:
                thread.port_close(finalize=False)
            except TypeError:
                try:
                    thread.port_close()
                except Exception:
                    pass
            except Exception:
                pass
        self.status.update({
            "connected": False,
            "command_busy": False,
            "command_action": "",
            "connection_state": "idle",
            "sampling": False,
            "save_mode": "",
            "active_mode": "Idle",
            "save_flags": {
                "lfp": False,
                "mode1": False,
                "mode2": False,
                "mode3": False,
            },
            "save_progress": {
                "lfp": 0.0,
                "mode1": 0.0,
                "mode2": 0.0,
                "mode3": 0.0,
            },
            "impedance_test_running": False,
            "impedance_progress_percent": 0.0,
            "impedance_status_text": "Ready",
        })
        self._emit_status()

    @staticmethod
    def _normalize_impedance_history_record(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(record, dict):
            return None
        try:
            raw_values = [
                float(v)
                for v in list(record.get("raw_impedance_magnitude_ohm", record.get("impedance_ohm", [])) or [])[:16]
            ]
        except Exception:
            return None
        if not raw_values:
            return None

        raw_phase = record.get("raw_impedance_phase_deg")
        if raw_phase is None:
            raw_phase = record.get("impedance_phase_deg")
        if raw_phase is None:
            raw_phase = [float(v) / 100.0 for v in list(record.get("impedance_phase_cdeg", []) or [])[:16]]
        try:
            raw_phase_values = [float(v) for v in list(raw_phase or [])[: len(raw_values)]]
        except Exception:
            raw_phase_values = []
        if len(raw_phase_values) < len(raw_values):
            raw_phase_values.extend([0.0] * (len(raw_values) - len(raw_phase_values)))

        timestamp_epoch = record.get("timestamp_epoch")
        if timestamp_epoch is None:
            try:
                timestamp_epoch = datetime.fromisoformat(str(record.get("timestamp"))).timestamp()
            except Exception:
                timestamp_epoch = time.time()

        timestamp = str(record.get("timestamp") or "").strip()
        if not timestamp:
            try:
                timestamp = datetime.fromtimestamp(float(timestamp_epoch)).astimezone().isoformat(timespec="seconds")
            except Exception:
                timestamp = datetime.now().astimezone().isoformat(timespec="seconds")

        local_time_display = str(record.get("local_time_display") or "").strip()
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
            "channel_count": int(record.get("channel_count", len(raw_values)) or len(raw_values)),
            "raw_impedance_magnitude_ohm": list(raw_values),
            "raw_impedance_phase_deg": list(raw_phase_values),
            "impedance_ohm": list(record.get("impedance_ohm", raw_values) or raw_values),
            "impedance_phase_deg": list(record.get("impedance_phase_deg", raw_phase_values) or raw_phase_values),
            "impedance_phase_cdeg": list(
                record.get(
                    "impedance_phase_cdeg",
                    [int(round(value * 100.0)) for value in raw_phase_values],
                )
                or [int(round(value * 100.0)) for value in raw_phase_values]
            ),
        }
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
            "rc_series_resistor_kohm",
            "rc_shunt_cap_pf",
            "compensation_frequency_hz",
        ):
            if key not in record:
                continue
            value = record.get(key)
            if isinstance(value, list):
                normalized[key] = list(value)
            elif isinstance(value, dict):
                normalized[key] = dict(value)
            else:
                normalized[key] = value
        return normalized

    @staticmethod
    def _is_duplicate_impedance_record(previous: Dict[str, Any], current: Dict[str, Any]) -> bool:
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return False
        return (
            int(previous.get("device_timestamp_ms", 0) or 0) == int(current.get("device_timestamp_ms", 0) or 0)
            and list(previous.get("raw_impedance_magnitude_ohm", []) or [])
            == list(current.get("raw_impedance_magnitude_ohm", []) or [])
            and list(previous.get("raw_impedance_phase_deg", []) or [])
            == list(current.get("raw_impedance_phase_deg", []) or [])
        )

    def _sync_impedance_status_from_history(self, bump_sequence: bool = False) -> None:
        self.status["impedance_history_count"] = len(self.impedance_history)
        self.status["impedance_last_result"] = (
            dict(self.impedance_history[-1]) if self.impedance_history else {}
        )
        if bump_sequence:
            self.status["impedance_result_seq"] = int(self.status.get("impedance_result_seq", 0) or 0) + 1

    def load_impedance_history(self, records: List[Dict[str, Any]]) -> None:
        loaded = []
        for item in list(records or []):
            normalized = self._normalize_impedance_history_record(item)
            if normalized is not None:
                loaded.append(normalized)
        loaded.sort(key=lambda item: float(item.get("timestamp_epoch", 0.0)))
        self.impedance_history = loaded[-IMPEDANCE_HISTORY_LIMIT:]
        if self.impedance_history:
            latest = dict(self.impedance_history[-1])
            if "rc_series_resistor_kohm" in latest:
                self.status["rc_series_resistor_kohm"] = float(
                    latest.get("rc_series_resistor_kohm", self.status.get("rc_series_resistor_kohm", DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM))
                    or self.status.get("rc_series_resistor_kohm", DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM)
                )
            if "rc_shunt_cap_pf" in latest:
                self.status["rc_shunt_cap_pf"] = float(
                    latest.get("rc_shunt_cap_pf", self.status.get("rc_shunt_cap_pf", DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF))
                    or self.status.get("rc_shunt_cap_pf", DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF)
                )
        self._sync_impedance_status_from_history(bump_sequence=False)
        self._emit_status()

    def get_impedance_history_for_cache(self) -> List[Dict[str, Any]]:
        return [dict(record) for record in list(self.impedance_history)]

    def _build_impedance_record(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            values = [float(v) for v in list(payload.get("impedance_ohm", []) or [])[:16]]
        except Exception:
            return None
        if not values:
            return None
        phase_values = list(payload.get("impedance_phase_deg", []) or [])
        if not phase_values:
            phase_values = [float(v) / 100.0 for v in list(payload.get("impedance_phase_cdeg", []) or [])[:16]]
        try:
            phase_values = [float(v) for v in phase_values[: len(values)]]
        except Exception:
            phase_values = []
        if len(phase_values) < len(values):
            phase_values.extend([0.0] * (len(values) - len(phase_values)))

        local_dt = datetime.now().astimezone()
        return self._normalize_impedance_history_record({
            "timestamp": local_dt.isoformat(timespec="seconds"),
            "timestamp_epoch": local_dt.timestamp(),
            "local_time_display": local_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "device_timestamp_ms": int(payload.get("timestamp_ms", 0) or 0),
            "sample_mode": int(payload.get("sample_mode", 0) or 0),
            "channel_count": int(payload.get("channel_count", len(values)) or len(values)),
            "raw_impedance_magnitude_ohm": list(values),
            "raw_impedance_phase_deg": list(phase_values),
            "impedance_ohm": list(values),
            "impedance_phase_deg": list(phase_values),
            "impedance_phase_cdeg": list(
                payload.get("impedance_phase_cdeg", [int(round(value * 100.0)) for value in phase_values])
                or [int(round(value * 100.0)) for value in phase_values]
            ),
            "rc_series_resistor_kohm": float(self.status.get("rc_series_resistor_kohm", DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM) or DEFAULT_IMPEDANCE_RC_SERIES_RESISTOR_KOHM),
            "rc_shunt_cap_pf": float(self.status.get("rc_shunt_cap_pf", DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF) or DEFAULT_IMPEDANCE_RC_SHUNT_CAP_PF),
        })

    def _send_command(self, payload: List[int], action_name: str, retries: int = 1) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit(f"Neural serial is not connected before {action_name}")
            return False
        try:
            return bool(thread.send_command_frame(payload, repeats=max(1, int(retries))))
        except Exception as exc:
            self.error.emit(f"Failed during {action_name}: {exc}")
            return False

    def set_detail_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self.status["detail_enabled"] = enabled
        if self.serial_thread is not None:
            self._sync_threshold_sample_stream()
            self.serial_thread.set_detail_enabled(enabled)
        self._emit_status()

    def configure_detail_stream(self, min_interval_ms: Optional[float] = None) -> None:
        if min_interval_ms is not None:
            try:
                self._detail_publish_min_interval_ms = max(0, int(float(min_interval_ms)))
            except Exception:
                pass
        if self.serial_thread is not None and hasattr(self.serial_thread, "configure_detail_stream"):
            self._sync_threshold_sample_stream()

    def set_detail_backpressure(self, active: bool, reason: str = "") -> None:
        active = bool(active)
        reason = str(reason or "")
        self.status["detail_throttled"] = active
        self.status["detail_last_skip_reason"] = reason if active else ""
        telemetry = dict(self.status.get("telemetry", {}) or {})
        telemetry["detail_throttled"] = active
        telemetry["detail_last_skip_reason"] = self.status["detail_last_skip_reason"]
        self.status["telemetry"] = telemetry
        if self.serial_thread is not None and hasattr(self.serial_thread, "set_detail_backpressure"):
            self.serial_thread.set_detail_backpressure(active, reason)

    def apply_gui_interval_pressure(self, reason: str, stream_key: Optional[str] = None) -> bool:
        thread = self.serial_thread
        if thread is None or not hasattr(thread, "apply_gui_interval_pressure"):
            return False
        try:
            return bool(thread.apply_gui_interval_pressure(reason, stream_key=stream_key))
        except TypeError:
            return bool(thread.apply_gui_interval_pressure(reason))
        except Exception:
            return False

    def _stop_auto_threshold_update(self, emit_status: bool = True) -> None:
        changed = bool(self.status.get("auto_threshold_running", False)) or int(self.status.get("auto_threshold_channel", -1) or -1) != -1
        self._auto_threshold_expected_gui_channel = None
        self._auto_threshold_sample_buffer = []
        self.status["auto_threshold_running"] = False
        self.status["auto_threshold_channel"] = -1
        self._sync_threshold_sample_stream()
        if emit_status and changed:
            self._emit_status()

    def _clear_mode1_threshold_window(self) -> None:
        self._mode1_threshold_window_gui_channel = None
        self._mode1_threshold_window_samples = []

    def _normalize_threshold_samples(self, samples: Any) -> List[float]:
        normalized: List[float] = []
        try:
            iterable = list(samples or [])
        except Exception:
            return normalized
        for value in iterable:
            try:
                numeric = float(value)
            except Exception:
                continue
            if math.isfinite(numeric):
                normalized.append(numeric)
        return normalized

    def _update_mode1_threshold_window(self, reported_gui_channel: int, samples: Any) -> None:
        normalized = self._normalize_threshold_samples(samples)
        if not normalized:
            return
        reported_gui_channel = max(0, min(15, int(reported_gui_channel)))
        if self._mode1_threshold_window_gui_channel != reported_gui_channel:
            self._mode1_threshold_window_gui_channel = reported_gui_channel
            self._mode1_threshold_window_samples = []
        self._mode1_threshold_window_samples.extend(normalized)
        if len(self._mode1_threshold_window_samples) > AUTO_THRESHOLD_SAMPLE_TARGET:
            self._mode1_threshold_window_samples = self._mode1_threshold_window_samples[-AUTO_THRESHOLD_SAMPLE_TARGET:]

    def _calculate_mode1_threshold_uv(self, samples: Any) -> float:
        thread = self.serial_thread
        if thread is None:
            raise ValueError("Neural serial thread is unavailable")
        normalized = self._normalize_threshold_samples(samples)
        if not normalized:
            raise ValueError("No finite Mode1 raw samples available for threshold calculation")
        return float(thread.calc_SpikeThreshold(normalized))

    def _active_mode_to_index(self, active_mode: Any) -> Optional[int]:
        try:
            numeric_mode = int(active_mode)
        except Exception:
            numeric_mode = None
        if numeric_mode in MODE_LABELS:
            return int(numeric_mode)
        normalized = str(active_mode or "").strip().lower()
        return MODE_LABEL_TO_VALUE.get(normalized)

    def set_mode(self, mode: int) -> bool:
        mode = int(mode)
        if mode not in MODE_LABELS:
            self.error.emit(f"Unsupported sample mode: {mode}")
            return False
        if mode != 1:
            self._stop_auto_threshold_update(emit_status=False)
            self._clear_mode1_threshold_window()
        if not self._send_command([0x00, 0x03, mode, 0x00], f"switching mode to {mode}", retries=1):
            return False
        if self.serial_thread is not None:
            self.serial_thread.begin_mode_transition(100)
            self._clear_packet_loss_status_fields()
        self._emit_status()
        return True

    def sample_start(self) -> bool:
        if self.serial_thread is not None:
            try:
                self.serial_thread.flush()
            except Exception:
                pass
        if not self._send_command([0x01, 0x00], "starting sampling", retries=2):
            return False
        if self.serial_thread is not None:
            self.serial_thread.begin_mode_transition(100)
            self._clear_packet_loss_status_fields()
        self.status["sampling"] = True
        self._emit_status()
        return True

    def sample_stop(self) -> bool:
        if not self._send_command([0x02, 0x00], "stopping sampling", retries=2):
            return False
        if self.serial_thread is not None:
            self.serial_thread.begin_mode_transition(100)
            self._clear_packet_loss_status_fields()
        self._stop_auto_threshold_update(emit_status=False)
        self._clear_mode1_threshold_window()
        self.status["sampling"] = False
        self.status["active_mode"] = "Idle"
        self._emit_status()
        return True

    def trigger_alignment(self, trial_num: int = 1) -> bool:
        thread = self.serial_thread
        if thread is None or not bool(self.status.get("connected", False)):
            return False
        try:
            thread.trigger_alignment(float(trial_num))
        except Exception as exc:
            self.error.emit(f"Failed to trigger alignment for trial {int(trial_num)}: {exc}")
            return False
        return True

    @staticmethod
    def _thread_active_save_modes(thread: Any) -> List[str]:
        active = []
        if bool(getattr(thread, "save_file_lfp_flag", False)):
            active.append("lfp")
        if bool(getattr(thread, "save_file_mode1_flag", False)):
            active.append("mode1")
        if bool(getattr(thread, "save_file_mode2_flag", False)):
            active.append("mode2")
        if bool(getattr(thread, "save_file_mode3_flag", False)):
            active.append("mode3")
        return active

    def _finalize_thread_save_buffers(self, thread: Any, modes: List[str]) -> List[str]:
        normalized = [str(mode) for mode in list(modes or []) if str(mode)]
        if not normalized or not hasattr(thread, "finalize_save_buffers"):
            return []
        try:
            return list(thread.finalize_save_buffers(modes=normalized) or [])
        except Exception as exc:
            self.error.emit(f"Failed to finalize save buffers for {', '.join(normalized)}: {exc}")
            return []

    def finalize_save_buffers(self, modes: Optional[Any] = None) -> List[str]:
        thread = self.serial_thread
        if thread is None:
            return []
        if modes is None:
            normalized = self._thread_active_save_modes(thread)
        elif isinstance(modes, str):
            normalized = [str(modes)]
        else:
            normalized = [str(mode) for mode in list(modes or []) if str(mode)]
        return self._finalize_thread_save_buffers(thread, normalized)

    def set_save_mode(self, mode_name: str, enabled: bool) -> bool:
        thread = self.serial_thread
        if thread is None:
            self.error.emit("Neural serial is not connected before save toggle")
            return False
        if enabled and not bool(self.status.get("global_save_enabled", True)):
            self.error.emit("Global save is disabled")
            return False

        enabled = bool(enabled)
        mode_name = str(mode_name)
        file_addr = {
            "lfp": self.profile.get("lfp_save_path", ""),
            "mode3": self.profile.get("mode3_save_path", ""),
            "mode1": self.profile.get("mode1_save_path", ""),
            "mode2": self.profile.get("mode2_save_path", ""),
        }.get(mode_name, "")

        if mode_name not in {"lfp", "mode1", "mode2", "mode3"}:
            self.error.emit(f"Unsupported save mode: {mode_name}")
            return False

        lock = getattr(thread, "_data_lock", None)
        with lock if lock is not None else nullcontext():
            previous_modes = set(self._thread_active_save_modes(thread))
            previous_route = dict(self.status.get("save_route", {}) or {})
            if (
                bool(enabled)
                and mode_name == "lfp"
                and "lfp" in previous_modes
                and str(previous_route.get("path_role", "") or "") == "mode3_training"
            ):
                self._finalize_thread_save_buffers(thread, ["lfp"])
            thread.save_file_lfp_flag = False
            thread.save_file_mode1_flag = False
            thread.save_file_mode2_flag = False
            thread.save_file_mode3_flag = False
            save_mode = ""
            if enabled:
                if mode_name == "lfp":
                    thread.save_file_lfp_flag = True
                    thread.lfp_file_addr = file_addr
                elif mode_name == "mode1":
                    thread.save_file_mode1_flag = True
                    thread.mode1_file_addr = file_addr
                elif mode_name == "mode2":
                    thread.save_file_mode2_flag = True
                    thread.mode2_file_addr = file_addr
                elif mode_name == "mode3":
                    thread.save_file_mode3_flag = True
                    thread.mode3_file_addr = file_addr
                save_mode = mode_name
            current_modes = set(self._thread_active_save_modes(thread))
            if enabled:
                try:
                    SerialPort._reset_packet_loss_streams(thread, streams=mode_name)
                    self._clear_packet_loss_status_fields()
                except Exception:
                    pass
            self._finalize_thread_save_buffers(thread, sorted(previous_modes - current_modes))
        self.status["save_mode"] = save_mode
        route_by_mode = {
            "lfp": {"data_mode": "mode0", "path_role": "lfp"},
            "mode1": {"data_mode": "mode1", "path_role": "mode1"},
            "mode2": {"data_mode": "mode2", "path_role": "mode2"},
            "mode3": {"data_mode": "mode3", "path_role": "mode3"},
        }
        self.status["save_route"] = (
            dict(route_by_mode.get(mode_name, {"data_mode": "", "path_role": ""}))
            if enabled
            else {"data_mode": "", "path_role": ""}
        )
        self.status["save_flags"] = {
            "lfp": bool(enabled and mode_name == "lfp"),
            "mode1": bool(enabled and mode_name == "mode1"),
            "mode2": bool(enabled and mode_name == "mode2"),
            "mode3": bool(enabled and mode_name == "mode3"),
        }
        self._emit_status()
        return True

    def set_mode0_training_save(self, enabled: bool) -> bool:
        thread = self.serial_thread
        if thread is None:
            self.error.emit("Neural serial is not connected before Mode0 training save toggle")
            return False
        if enabled and not bool(self.status.get("global_save_enabled", True)):
            self.error.emit("Global save is disabled")
            return False

        enabled = bool(enabled)
        file_addr = str(self.profile.get("mode3_save_path", "") or "")
        if enabled and not file_addr:
            self.error.emit("Mode3 save path is empty before Mode0 training save toggle")
            return False

        lock = getattr(thread, "_data_lock", None)
        with lock if lock is not None else nullcontext():
            previous_modes = set(self._thread_active_save_modes(thread))
            previous_route = dict(self.status.get("save_route", {}) or {})
            finalized_before_switch = set()
            if "lfp" in previous_modes and (
                enabled
                or str(previous_route.get("path_role", "") or "") == "mode3_training"
            ):
                self._finalize_thread_save_buffers(thread, ["lfp"])
                finalized_before_switch.add("lfp")
            thread.save_file_lfp_flag = False
            thread.save_file_mode1_flag = False
            thread.save_file_mode2_flag = False
            thread.save_file_mode3_flag = False
            save_mode = ""
            if enabled:
                thread.save_file_lfp_flag = True
                thread.lfp_file_addr = file_addr
                save_mode = "lfp"
                try:
                    SerialPort._reset_packet_loss_streams(thread, streams="lfp")
                    self._clear_packet_loss_status_fields()
                except Exception:
                    pass
            current_modes = set(self._thread_active_save_modes(thread))
            self._finalize_thread_save_buffers(thread, sorted((previous_modes - current_modes) - finalized_before_switch))

        self.status["save_mode"] = save_mode
        self.status["save_route"] = (
            {"data_mode": "mode0", "path_role": "mode3_training"}
            if enabled
            else {"data_mode": "", "path_role": ""}
        )
        self.status["save_flags"] = {
            "lfp": bool(enabled),
            "mode1": False,
            "mode2": False,
            "mode3": False,
        }
        self._emit_status()
        return True

    def set_global_save_enabled(self, enabled: bool) -> None:
        self.status["global_save_enabled"] = bool(enabled)
        if not self.status["global_save_enabled"]:
            thread = self.serial_thread
            if thread is not None:
                lock = getattr(thread, "_data_lock", None)
                with lock if lock is not None else nullcontext():
                    previous_modes = self._thread_active_save_modes(thread)
                    thread.save_file_lfp_flag = False
                    thread.save_file_mode1_flag = False
                    thread.save_file_mode2_flag = False
                    thread.save_file_mode3_flag = False
                    self._finalize_thread_save_buffers(thread, previous_modes)
            self.status["save_mode"] = ""
            self.status["save_route"] = {"data_mode": "", "path_role": ""}
            self.status["save_flags"] = {
                "lfp": False,
                "mode1": False,
                "mode2": False,
                "mode3": False,
            }
            self.status["save_progress"] = {
                "lfp": 0.0,
                "mode1": 0.0,
                "mode2": 0.0,
                "mode3": 0.0,
            }
        self._emit_status()

    def set_imu_mode(self, mode: int) -> bool:
        normalized_mode = normalize_imu_mode(mode)
        if not self._send_command([0x03, 0x00, normalized_mode, 0x00], f"setting IMU mode to {normalized_mode}", retries=1):
            return False
        self.status["imu_mode"] = normalized_mode
        self.status["imu_mode_text"] = imu_mode_label(normalized_mode)
        self._emit_status()
        return True

    def set_mode3_reref_mode(self, mode: int) -> bool:
        mode = normalize_mode3_reref_mode(mode, fallback=0)
        if not self._send_command([0x00, 0x06, mode, 0x00], f"setting Mode0/3 re-reference {mode}", retries=1):
            return False
        self.status["mode3_reref_pending"] = True
        self.status["mode3_reref_target_mode"] = mode
        self._mode3_reref_pending_deadline_monotonic = time.monotonic() + 3.0
        self._emit_status()
        return True

    def restart_relay_device(self) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit("Neural serial is not connected before restarting relay")
            return False
        try:
            ok = bool(thread.send_relay_control_command("reboot", repeats=1))
        except Exception as exc:
            self.error.emit(f"Failed to send relay reboot command: {exc}")
            return False
        if not ok:
            self.error.emit("Failed to send relay reboot command")
            return False
        return True

    def restart_peripheral_firmware(self) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit("Neural serial is not connected before restarting peripheral firmware")
            return False
        try:
            thread.begin_mode_transition(1200)
        except Exception:
            pass
        reboot_command = build_runtime_device_reboot_command()
        return self._send_command(
            list(reboot_command),
            "restarting peripheral firmware",
            retries=2,
        )

    def set_device_esb_mode_config(
        self,
        mode: int,
        tx_power_code: int,
        retransmit_count: int,
        ack_window_us: int = 1200,
        noack_percent: int = 0,
    ) -> bool:
        try:
            mode = int(mode)
            tx_power_code = int(tx_power_code)
            retransmit_count = int(retransmit_count)
            ack_window_us = int(ack_window_us)
            noack_percent = int(noack_percent)
            device_command = build_runtime_device_esb_mode_config_command(
                mode,
                tx_power_code,
                retransmit_count,
                ack_window_us=ack_window_us,
                noack_percent=noack_percent,
            )
        except Exception as exc:
            self.error.emit(f"Invalid ESB mode config: {exc}")
            return False
        return self._send_command(
            list(device_command),
            f"setting Mode{mode} ESB TX/retransmit/ACK",
            retries=2,
        )

    def set_mode0_quant_config(
        self,
        bit_depth: int = 12,
        full_scale_uv: float = 1000.0,
    ) -> bool:
        try:
            bit_depth = int(bit_depth)
            full_scale_uv = float(full_scale_uv)
            device_command = build_mode0_quant_config_command(
                bit_depth=bit_depth,
                full_scale_uv=full_scale_uv,
            )
        except Exception as exc:
            self.error.emit(f"Invalid Mode0 quantization config: {exc}")
            return False
        ok = self._send_command(
            list(device_command),
            f"setting Mode0 quantization {bit_depth}-bit LFP +/-{full_scale_uv:g} uV, raw +/-500 uV",
            retries=2,
        )
        if ok:
            self.status["mode0_quant"] = {
                **dict(self.status.get("mode0_quant", {}) or {}),
                "bit_depth": bit_depth,
                "full_scale_uv": full_scale_uv,
            }
            thread = self.serial_thread
            if thread is not None:
                try:
                    thread.mode0_quant_bits = bit_depth
                    thread.mode0_quant_full_scale_uv = full_scale_uv
                except Exception:
                    pass
        return ok

    def apply_relay_esb_channel(self, channel: int) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit("Neural serial is not connected before setting ESB channel")
            return False
        try:
            channel = int(channel)
        except Exception:
            channel = -1
        if channel not in RELAY_RECOMMENDED_ESB_CHANNELS:
            self.error.emit(f"Unsupported ESB channel: {channel}")
            return False
        device_command = build_runtime_device_esb_channel_command(channel)
        try:
            thread.begin_mode_transition(600)
        except Exception:
            pass
        if not self._send_command(
            list(device_command),
            f"setting peripheral ESB channel CH{channel}",
            retries=2,
        ):
            return False
        try:
            ok = bool(thread.send_relay_control_command(f"set_esb_channel_{channel}", repeats=1))
        except Exception as exc:
            self.error.emit(f"Failed to set relay ESB channel CH{channel}: {exc}")
            return False
        if not ok:
            self.error.emit(f"Failed to set relay ESB channel CH{channel}")
            return False
        return True

    def _dac_resolution(self) -> float:
        thread = self.serial_thread
        if thread is not None:
            try:
                return float(thread.DAC_resolution)
            except Exception:
                pass
        return (1.0 / float(int("ffff", 16))) * 1.225 * 2.0

    def _clamp_neural_channel(self, channel_idx: int) -> int:
        channel_idx = max(0, min(15, int(channel_idx)))
        return int(channel_idx)

    def _encode_absolute_threshold(self, threshold_uv: float):
        threshold_uv_abs = abs(float(threshold_uv))
        dac_resolution = self._dac_resolution()
        temp_threshold = int((threshold_uv_abs / 1000.0 / 1000.0 * 192.0 + 1.225) / dac_resolution) - int("0x8000", 16)
        clipped = False
        if temp_threshold < 0:
            temp_threshold = 0
            clipped = True
        elif temp_threshold > int("0x7FFF", 16):
            temp_threshold = int("0x7FFF", 16)
            clipped = True
        return temp_threshold, threshold_uv_abs, clipped

    def _normalized_mode3_thresholds(self) -> List[float]:
        thresholds = list(self.status.get("mode3_thresholds", [DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * 16))
        if len(thresholds) < 16:
            thresholds.extend([DEFAULT_MODE3_SPIKE_THRESHOLD_UV] * (16 - len(thresholds)))
        return [float(value or 0.0) for value in thresholds[:16]]

    def _threshold_for_mode3_raw_channel(self, raw_channel_idx: int) -> float:
        raw_channel_idx = max(0, min(15, int(raw_channel_idx)))
        thresholds = self._normalized_mode3_thresholds()
        return float(thresholds[raw_channel_idx])

    def _threshold_for_mode1_channel(self, channel_idx: int) -> float:
        return self._threshold_for_mode3_raw_channel(self._clamp_neural_channel(channel_idx))

    def _store_threshold_for_mode3_raw_channel(
        self,
        raw_channel_idx: int,
        threshold_uv_abs: float,
        source: str = "manual",
    ) -> None:
        raw_channel_idx = max(0, min(15, int(raw_channel_idx)))
        thresholds = self._normalized_mode3_thresholds()
        thresholds[raw_channel_idx] = float(threshold_uv_abs)
        self.status["mode3_thresholds"] = thresholds[:16]
        thread = self.serial_thread
        if thread is not None:
            try:
                thread.mode3_thresholds_uv = list(self.status["mode3_thresholds"])
            except Exception:
                pass
        try:
            self._persist_threshold_cache(source=source, updated_channel=raw_channel_idx)
        except Exception as exc:
            self.error.emit(f"Failed to save Mode3 threshold cache: {exc}")

    def _send_raw_channel_with_threshold(
        self,
        gui_channel: int,
        threshold_uv: float,
        use_mode1_mapping: bool,
        action_name: str,
        persist_threshold: bool = True,
    ) -> bool:
        _ = use_mode1_mapping
        gui_channel = self._clamp_neural_channel(gui_channel)
        command_channel = gui_channel
        temp_threshold, threshold_uv_abs, clipped = self._encode_absolute_threshold(threshold_uv)
        threshold_hex = format(temp_threshold & int("0xFFFF", 16), "04x")
        payload = [0x00, 0x04, int(command_channel), 0x00, int(threshold_hex[2:4], 16), int(threshold_hex[0:2], 16)]
        ok = self._send_command(payload, action_name, retries=1)
        if ok and persist_threshold:
            self._store_threshold_for_mode3_raw_channel(
                command_channel,
                threshold_uv_abs,
                source=self._threshold_source_from_action(action_name),
            )
        return bool(ok)

    def _recent_mode1_raw_samples(self, gui_channel: Optional[int] = None):
        if (
            gui_channel is not None
            and self._mode1_threshold_window_gui_channel == int(gui_channel)
            and self._mode1_threshold_window_samples
        ):
            return list(self._mode1_threshold_window_samples)
        thread = self.serial_thread
        if thread is None:
            return []
        try:
            samples = self._normalize_threshold_samples(getattr(thread, "spikedata_GUI", []) or [])
        except Exception:
            samples = []
        if len(samples) >= 30:
            return samples[-min(len(samples), AUTO_THRESHOLD_SAMPLE_TARGET):]
        try:
            ap_data = getattr(thread, "AP_data", {}) or {}
            fallback = self._normalize_threshold_samples(ap_data.get("Raw_data", []) or [])
            if fallback:
                return fallback[-min(len(fallback), AUTO_THRESHOLD_SAMPLE_TARGET):]
        except Exception:
            pass
        return samples

    def _set_mode1_channel_internal(self, gui_channel: int, auto_threshold: bool = True, preserve_auto_state: bool = False) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit("Neural serial is not connected before setting Mode1 channel")
            return False
        if not preserve_auto_state:
            self._stop_auto_threshold_update(emit_status=False)
        if auto_threshold:
            samples = self._recent_mode1_raw_samples(gui_channel)
            if not samples:
                self.error.emit("No recent Mode1 raw samples available for auto threshold")
                return False
            try:
                threshold_uv = self._calculate_mode1_threshold_uv(samples)
            except Exception as exc:
                self.error.emit(f"Failed to calculate Mode1 auto threshold: {exc}")
                return False
        else:
            threshold_uv = self._threshold_for_mode1_channel(gui_channel)
        ok = self._send_raw_channel_with_threshold(
            gui_channel,
            threshold_uv,
            use_mode1_mapping=True,
            action_name="setting mode1 spike channel",
            persist_threshold=bool(auto_threshold),
        )
        if ok:
            self.status["mode1_raw_channel"] = int(gui_channel)
            self._emit_status()
        return ok

    def set_mode1_channel(self, gui_channel: int, auto_threshold: bool = False) -> bool:
        return self._set_mode1_channel_internal(gui_channel, auto_threshold=auto_threshold, preserve_auto_state=False)

    def start_auto_threshold_update(self) -> bool:
        thread = self.serial_thread
        if thread is None or not self.status["connected"]:
            self.error.emit("Neural serial is not connected before auto threshold update")
            return False
        if not bool(self.status.get("sampling", False)) or self._active_mode_to_index(self.status.get("active_mode")) != 1:
            self.error.emit("Auto threshold update requires Single Channel Spike sampling mode")
            return False
        self._auto_threshold_expected_gui_channel = 0
        self._auto_threshold_sample_buffer = []
        self.status["auto_threshold_running"] = True
        self.status["auto_threshold_channel"] = 0
        self._sync_threshold_sample_stream()
        if not self._set_mode1_channel_internal(0, auto_threshold=False, preserve_auto_state=True):
            self._stop_auto_threshold_update(emit_status=False)
            self._emit_status()
            return False
        return True

    def set_mode3_raw_channel(self, gui_channel: int) -> bool:
        self._stop_auto_threshold_update(emit_status=False)
        gui_channel = max(0, min(15, int(gui_channel)))
        threshold_uv = self._threshold_for_mode3_raw_channel(gui_channel)
        ok = self._send_raw_channel_with_threshold(
            gui_channel,
            threshold_uv,
            use_mode1_mapping=False,
            action_name="setting mode0/mode3 raw channel",
            persist_threshold=False,
        )
        if ok:
            self.status["mode3_raw_channel"] = int(gui_channel)
            self._emit_status()
        return ok

    def set_mode2_channels(self, channels: List[int]) -> bool:
        _ = channels
        self._stop_auto_threshold_update(emit_status=False)
        self.status["mode2_channels"] = list(MODE2_FIXED_CHANNELS)
        try:
            self.status.setdefault("ui_capabilities", {})["remote_mode2_channels"] = False
        except Exception:
            pass
        self._emit_status()
        return True

    def set_spike_threshold(self, gui_channel: int, threshold_uv: float, use_mode1_mapping: bool) -> bool:
        self._stop_auto_threshold_update(emit_status=False)
        ok = self._send_raw_channel_with_threshold(
            gui_channel,
            threshold_uv,
            use_mode1_mapping=bool(use_mode1_mapping),
            action_name="setting spike threshold",
        )
        if ok:
            self._emit_status()
        return ok

    def start_impedance_test(self) -> bool:
        if self.status.get("impedance_test_running"):
            return True
        ok = self._send_command([0x00, 0x09], "starting impedance test", retries=1)
        if ok:
            self.status["impedance_test_running"] = True
            self.status["impedance_progress_percent"] = 0.0
            self.status["impedance_status_text"] = "Command sent, waiting for device"
            self._emit_status()
        return ok

    def _process_mode1_threshold_samples(self, reported_fw_channel: int, samples: Any) -> None:
        expected_channel = self._auto_threshold_expected_gui_channel
        try:
            fallback_channel = 0 if expected_channel is None else int(expected_channel)
            reported_channel = self._clamp_neural_channel(reported_fw_channel)
        except Exception:
            reported_channel = self._clamp_neural_channel(fallback_channel)
        samples = self._normalize_threshold_samples(samples)
        if samples:
            self._update_mode1_threshold_window(reported_channel, samples)
        if not bool(self.status.get("auto_threshold_running", False)):
            return
        if expected_channel is None:
            return
        if reported_channel != int(expected_channel):
            return
        if not samples:
            return
        self._auto_threshold_sample_buffer.extend(samples)
        if len(self._auto_threshold_sample_buffer) > AUTO_THRESHOLD_SAMPLE_TARGET:
            self._auto_threshold_sample_buffer = self._auto_threshold_sample_buffer[-AUTO_THRESHOLD_SAMPLE_TARGET:]
        if len(self._auto_threshold_sample_buffer) < AUTO_THRESHOLD_SAMPLE_TARGET:
            return
        thread = self.serial_thread
        if thread is None or not bool(self.status.get("connected", False)):
            self._stop_auto_threshold_update()
            return
        try:
            threshold_uv = self._calculate_mode1_threshold_uv(self._auto_threshold_sample_buffer)
        except Exception as exc:
            self.error.emit(f"Failed to calculate Mode1 auto threshold: {exc}")
            self._stop_auto_threshold_update()
            return
        ok = self._send_raw_channel_with_threshold(
            expected_channel,
            threshold_uv,
            use_mode1_mapping=True,
            action_name=f"auto threshold update channel {expected_channel}",
        )
        if not ok:
            self._stop_auto_threshold_update()
            return
        self.status["mode1_raw_channel"] = int(expected_channel)
        next_channel = int(expected_channel) + 1
        if next_channel >= AUTO_THRESHOLD_CHANNEL_COUNT:
            self._stop_auto_threshold_update(emit_status=False)
            self._emit_status()
            return
        self._auto_threshold_expected_gui_channel = next_channel
        self._auto_threshold_sample_buffer = []
        self.status["auto_threshold_running"] = True
        self.status["auto_threshold_channel"] = int(next_channel)
        if not self._set_mode1_channel_internal(next_channel, auto_threshold=False, preserve_auto_state=True):
            self._stop_auto_threshold_update()

    def _on_threshold_samples_update(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        self._process_mode1_threshold_samples(
            payload.get("reported_fw_channel", 0),
            payload.get("samples", []),
        )

    def _on_gui_update(self, payload: object) -> None:
        self.detail_payload_ready.emit(payload)
        if self._threshold_samples_use_dedicated_signal:
            return
        if not isinstance(payload, list) or not payload:
            return
        header = payload[0]
        if not isinstance(header, (list, tuple)) or not header:
            return
        try:
            mode = int(header[0])
        except Exception:
            return
        if mode != 1:
            return
        try:
            fallback_channel = 0 if self._auto_threshold_expected_gui_channel is None else int(self._auto_threshold_expected_gui_channel)
            reported_channel = int(header[1]) if len(header) > 1 else fallback_channel
        except Exception:
            reported_channel = 0
        self._process_mode1_threshold_samples(
            reported_channel,
            payload[2] if len(payload) > 2 else [],
        )

    def _on_idle_update(self, payload: List[float]) -> None:
        if len(payload) >= 3:
            reported_rsoc = float(payload[0])
            self.status["battery"] = {
                "rsoc": reported_rsoc,
                "reported_rsoc": reported_rsoc,
                "stat": float(payload[1]),
                "voltage": float(payload[2]),
            }
            if len(payload) >= 4 and isinstance(payload[3], dict):
                self.status["power_guard"] = dict(payload[3])
            if not bool(self.status.get("sampling", False)):
                self.status["active_mode"] = "Idle"
            self._emit_status()

    def _on_progress_update(self, mode: int, percent: float, run_time_min: float) -> None:
        mode = int(mode)
        self.status["progress_percent"] = float(percent)
        self.status["run_time_min"] = float(run_time_min)
        mode_key = {
            0: "lfp",
            1: "mode1",
            2: "mode2",
            3: "mode3",
        }.get(mode)
        if mode_key:
            save_progress = dict(self.status.get("save_progress", {}))
            save_progress[mode_key] = float(percent)
            self.status["save_progress"] = save_progress
        self._emit_status()

    def _on_video_rollover_requested(self, *_args) -> None:
        self.video_rollover_requested.emit()

    def _on_status_update(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        battery = payload.get("battery", {})
        if isinstance(battery, dict):
            reported_rsoc = float(battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0)
            self.status["battery"] = {
                "rsoc": reported_rsoc,
                "reported_rsoc": reported_rsoc,
                "stat": float(battery.get("stat", 0.0) or 0.0),
                "voltage": float(battery.get("voltage", 0.0) or 0.0),
            }
        mode0_power = payload.get("mode0_power")
        if isinstance(mode0_power, dict):
            self.status["mode0_power"] = dict(mode0_power)
        mode0_quant = payload.get("mode0_quant")
        if isinstance(mode0_quant, dict):
            self.status["mode0_quant"] = dict(mode0_quant)
        power_guard = payload.get("power_guard")
        if isinstance(power_guard, dict):
            self.status["power_guard"] = dict(power_guard)
        self.status["packet_loss"] = int(payload.get("packet_loss", 0) or 0)
        self.status["packet_count"] = int(payload.get("packet_count", 0) or 0)
        self.status["packet_loss_batch"] = int(payload.get("packet_loss_batch", self.status.get("packet_loss_batch", 0)) or 0)
        self.status["packet_count_batch"] = int(payload.get("packet_count_batch", self.status.get("packet_count_batch", 0)) or 0)
        self.status["packet_loss_percent_current"] = float(
            payload.get("packet_loss_percent_current", self.status.get("packet_loss_percent_current", 0.0)) or 0.0
        )
        self.status["packet_loss_expected_current"] = int(
            payload.get("packet_loss_expected_current", self.status.get("packet_loss_expected_current", 0)) or 0
        )
        self.status["packet_metrics_seq"] = int(
            payload.get("packet_metrics_seq", self.status.get("packet_metrics_seq", 0)) or 0
        )
        self.status["serial_backlog_event_count"] = int(
            payload.get("serial_backlog_event_count", self.status.get("serial_backlog_event_count", 0)) or 0
        )
        self.status["serial_backlog_last_bytes"] = int(
            payload.get("serial_backlog_last_bytes", self.status.get("serial_backlog_last_bytes", 0)) or 0
        )
        self.status["serial_backlog_peak_bytes"] = int(
            payload.get("serial_backlog_peak_bytes", self.status.get("serial_backlog_peak_bytes", 0)) or 0
        )
        self.status["serial_backlog_active"] = bool(
            payload.get("serial_backlog_active", self.status.get("serial_backlog_active", False))
        )
        self.status["serial_backlog_last_epoch"] = float(
            payload.get("serial_backlog_last_epoch", self.status.get("serial_backlog_last_epoch", 0.0)) or 0.0
        )
        self.status["serial_read_gap_ms"] = float(
            payload.get("serial_read_gap_ms", self.status.get("serial_read_gap_ms", 0.0)) or 0.0
        )
        self.status["serial_read_gap_peak_ms"] = float(
            payload.get("serial_read_gap_peak_ms", self.status.get("serial_read_gap_peak_ms", 0.0)) or 0.0
        )
        self.status["serial_read_gap_event_count"] = int(
            payload.get("serial_read_gap_event_count", self.status.get("serial_read_gap_event_count", 0)) or 0
        )
        self.status["serial_read_gap_last_epoch"] = float(
            payload.get("serial_read_gap_last_epoch", self.status.get("serial_read_gap_last_epoch", 0.0)) or 0.0
        )
        self.status["serial_read_gap_trace_seq"] = int(
            payload.get("serial_read_gap_trace_seq", self.status.get("serial_read_gap_trace_seq", 0)) or 0
        )
        serial_read_gap_trace = payload.get("serial_read_gap_trace")
        if isinstance(serial_read_gap_trace, list):
            self.status["serial_read_gap_trace"] = [
                dict(item) for item in serial_read_gap_trace[-20:] if isinstance(item, dict)
            ]
        self.status["read_batch_bytes"] = int(
            payload.get("read_batch_bytes", self.status.get("read_batch_bytes", 0)) or 0
        )
        read_batch_by_mode = payload.get("read_batch_bytes_by_mode")
        if isinstance(read_batch_by_mode, dict):
            self.status["read_batch_bytes_by_mode"] = _normalize_mode_float_mapping(read_batch_by_mode)
        read_batch_packets_by_mode = payload.get("read_batch_packets_by_mode")
        if isinstance(read_batch_packets_by_mode, dict):
            self.status["read_batch_packets_by_mode"] = _normalize_mode_float_mapping(read_batch_packets_by_mode)
        read_batch_redline_by_mode = payload.get("read_batch_packet_redline_by_mode")
        if isinstance(read_batch_redline_by_mode, dict):
            self.status["read_batch_packet_redline_by_mode"] = _normalize_mode_float_mapping(read_batch_redline_by_mode)
        read_batch_warn_by_mode = payload.get("read_batch_packet_warn_by_mode")
        if isinstance(read_batch_warn_by_mode, dict):
            self.status["read_batch_packet_warn_by_mode"] = _normalize_mode_float_mapping(read_batch_warn_by_mode)
        self.status["port_in_waiting_before_read"] = int(
            payload.get("port_in_waiting_before_read", self.status.get("port_in_waiting_before_read", 0)) or 0
        )
        self.status["writer_lag"] = int(payload.get("writer_lag", 0) or 0)
        self.status["save_queue_drop_count"] = int(payload.get("save_queue_drop_count", 0) or 0)
        self.status["save_enqueue_ms"] = float(payload.get("save_enqueue_ms", self.status.get("save_enqueue_ms", 0.0)) or 0.0)
        self.status["save_enqueue_type"] = str(payload.get("save_enqueue_type", self.status.get("save_enqueue_type", "")) or "")
        self.status["packet_gap_event_count"] = int(
            payload.get("packet_gap_event_count", self.status.get("packet_gap_event_count", 0)) or 0
        )
        self.status["packet_gap_missing"] = int(
            payload.get("packet_gap_missing", self.status.get("packet_gap_missing", 0)) or 0
        )
        self.status["packet_gap_summary"] = str(
            payload.get("packet_gap_summary", self.status.get("packet_gap_summary", "")) or ""
        )
        packet_gap_diagnostics = payload.get("packet_gap_diagnostics")
        if isinstance(packet_gap_diagnostics, dict):
            self.status["packet_gap_diagnostics"] = dict(packet_gap_diagnostics)
        self.status["packet_out_of_order_event_count"] = int(
            payload.get(
                "packet_out_of_order_event_count",
                self.status.get("packet_out_of_order_event_count", 0),
            )
            or 0
        )
        self.status["packet_out_of_order_summary"] = str(
            payload.get(
                "packet_out_of_order_summary",
                self.status.get("packet_out_of_order_summary", ""),
            )
            or ""
        )
        self.status["packet_counter_reset_event_count"] = int(
            payload.get(
                "packet_counter_reset_event_count",
                self.status.get("packet_counter_reset_event_count", 0),
            )
            or 0
        )
        self.status["packet_counter_reset_summary"] = str(
            payload.get(
                "packet_counter_reset_summary",
                self.status.get("packet_counter_reset_summary", ""),
            )
            or ""
        )
        self.status["progress_percent"] = float(payload.get("progress_percent", 0.0) or 0.0)
        self.status["run_time_min"] = float(payload.get("run_time_min", 0.0) or 0.0)
        self.status["rssi"] = float(payload.get("rssi", 0.0) or 0.0)
        self.status["detail_enabled"] = bool(payload.get("detail_enabled", self.status.get("detail_enabled", False)))
        self.status["detail_throttled"] = bool(payload.get("detail_throttled", self.status.get("detail_throttled", False)))
        self.status["detail_dropped_frames"] = int(
            payload.get("detail_dropped_frames", self.status.get("detail_dropped_frames", 0)) or 0
        )
        self.status["detail_last_skip_reason"] = str(
            payload.get("detail_last_skip_reason", self.status.get("detail_last_skip_reason", "")) or ""
        )
        gui_intervals = payload.get("gui_update_intervals_by_mode")
        if isinstance(gui_intervals, dict):
            self.status["gui_update_intervals_by_mode"] = _normalize_gui_interval_mapping(gui_intervals)
        gui_best = payload.get("gui_update_interval_best_by_mode")
        if isinstance(gui_best, dict):
            self.status["gui_update_interval_best_by_mode"] = _normalize_gui_interval_mapping(gui_best)
        gui_stats = payload.get("gui_update_interval_stats")
        if isinstance(gui_stats, dict):
            self.status["gui_update_interval_stats"] = _normalize_gui_interval_stats(
                gui_stats,
                self.status.get("gui_update_intervals_by_mode", {}),
            )
        gui_stream_intervals = payload.get("gui_update_intervals_by_stream")
        if isinstance(gui_stream_intervals, dict):
            self.status["gui_update_intervals_by_stream"] = _normalize_gui_stream_interval_mapping(
                gui_stream_intervals,
                self.status.get("gui_update_intervals_by_mode", {}),
            )
        gui_stream_best = payload.get("gui_update_interval_best_by_stream")
        if isinstance(gui_stream_best, dict):
            self.status["gui_update_interval_best_by_stream"] = _normalize_gui_stream_interval_mapping(
                gui_stream_best,
                self.status.get("gui_update_interval_best_by_mode", {}),
            )
        gui_stream_stats = payload.get("gui_update_interval_stream_stats")
        if isinstance(gui_stream_stats, dict):
            self.status["gui_update_interval_stream_stats"] = _normalize_gui_stream_interval_stats(
                gui_stream_stats,
                self.status.get("gui_update_intervals_by_stream", {}),
            )
        gui_control = payload.get("gui_update_interval_control")
        if isinstance(gui_control, dict):
            self.status["gui_update_interval_control"] = _normalize_gui_interval_control(gui_control)
        pipeline_payload = payload.get("pipeline", self.status.get("pipeline", {}))
        if isinstance(pipeline_payload, dict):
            self.status["pipeline"] = {
                "running": bool(pipeline_payload.get("running", False)),
                "worker_alive": bool(pipeline_payload.get("worker_alive", False)),
                "raw_queue_depth": int(pipeline_payload.get("raw_queue_depth", 0) or 0),
                "event_queue_depth": int(pipeline_payload.get("event_queue_depth", 0) or 0),
                "submitted_frames": int(pipeline_payload.get("submitted_frames", 0) or 0),
                "processed_frames": int(pipeline_payload.get("processed_frames", 0) or 0),
                "emitted_events": int(pipeline_payload.get("emitted_events", 0) or 0),
                "dropped_raw_frames": int(pipeline_payload.get("dropped_raw_frames", 0) or 0),
                "dropped_events": int(pipeline_payload.get("dropped_events", 0) or 0),
                "coalesced_events": int(pipeline_payload.get("coalesced_events", 0) or 0),
                "last_error": str(pipeline_payload.get("last_error", "") or ""),
            }
        telemetry = dict(self.status.get("telemetry", {}) or {})
        telemetry.update({
            "detail_throttled": bool(self.status.get("detail_throttled", False)),
            "detail_dropped_frames": int(self.status.get("detail_dropped_frames", 0) or 0),
            "detail_last_skip_reason": str(self.status.get("detail_last_skip_reason", "") or ""),
            "serial_read_gap_ms": float(self.status.get("serial_read_gap_ms", 0.0) or 0.0),
            "serial_read_gap_peak_ms": float(self.status.get("serial_read_gap_peak_ms", 0.0) or 0.0),
            "serial_read_gap_event_count": int(self.status.get("serial_read_gap_event_count", 0) or 0),
            "serial_read_gap_last_epoch": float(self.status.get("serial_read_gap_last_epoch", 0.0) or 0.0),
            "serial_read_gap_trace_seq": int(self.status.get("serial_read_gap_trace_seq", 0) or 0),
            "serial_read_gap_trace": list(self.status.get("serial_read_gap_trace", []) or []),
            "read_batch_bytes_by_mode": dict(self.status.get("read_batch_bytes_by_mode", {}) or {}),
            "read_batch_packets_by_mode": dict(self.status.get("read_batch_packets_by_mode", {}) or {}),
            "read_batch_packet_redline_by_mode": dict(self.status.get("read_batch_packet_redline_by_mode", {}) or {}),
            "read_batch_packet_warn_by_mode": dict(self.status.get("read_batch_packet_warn_by_mode", {}) or {}),
            "gui_update_intervals_by_mode": dict(self.status.get("gui_update_intervals_by_mode", {}) or {}),
            "gui_update_intervals_by_stream": dict(self.status.get("gui_update_intervals_by_stream", {}) or {}),
            "save_enqueue_ms": float(self.status.get("save_enqueue_ms", 0.0) or 0.0),
            "packet_gap_event_count": int(self.status.get("packet_gap_event_count", 0) or 0),
            "packet_gap_missing": int(self.status.get("packet_gap_missing", 0) or 0),
            "packet_gap_summary": str(self.status.get("packet_gap_summary", "") or ""),
            "packet_gap_diagnostics": dict(self.status.get("packet_gap_diagnostics", {}) or {}),
            "packet_out_of_order_event_count": int(self.status.get("packet_out_of_order_event_count", 0) or 0),
            "packet_out_of_order_summary": str(self.status.get("packet_out_of_order_summary", "") or ""),
            "packet_counter_reset_event_count": int(self.status.get("packet_counter_reset_event_count", 0) or 0),
            "packet_counter_reset_summary": str(self.status.get("packet_counter_reset_summary", "") or ""),
            "pipeline_raw_queue_depth": int(self.status.get("pipeline", {}).get("raw_queue_depth", 0) or 0),
            "pipeline_dropped_raw_frames": int(self.status.get("pipeline", {}).get("dropped_raw_frames", 0) or 0),
        })
        self.status["telemetry"] = telemetry
        try:
            stream_mode = int(payload.get("stream_mode", STREAM_MODE_IDLE))
        except Exception:
            stream_mode = STREAM_MODE_IDLE
        if not bool(self.status.get("sampling", False)) or stream_mode == STREAM_MODE_IDLE:
            self.status["active_mode"] = "Idle"
            self._clear_mode1_threshold_window()
        elif stream_mode in MODE_LABELS:
            self.status["active_mode"] = MODE_LABELS.get(stream_mode, self.status["active_mode"])
        if self._active_mode_to_index(self.status.get("active_mode")) != 1:
            self._stop_auto_threshold_update(emit_status=False)
            self._clear_mode1_threshold_window()
        save_flags = payload.get("save_flags", {})
        if isinstance(save_flags, dict):
            active = [name for name in ("lfp", "mode1", "mode2", "mode3") if save_flags.get(name)]
            self.status["save_mode"] = active[0] if active else ""
            normalized_flags = {
                "lfp": bool(save_flags.get("lfp", False)),
                "mode1": bool(save_flags.get("mode1", False)),
                "mode2": bool(save_flags.get("mode2", False)),
                "mode3": bool(save_flags.get("mode3", False)),
            }
            self.status["save_flags"] = normalized_flags
            save_progress = dict(self.status.get("save_progress", {}))
            for key, enabled in normalized_flags.items():
                if not enabled:
                    save_progress[key] = 0.0
            self.status["save_progress"] = save_progress
        if "mode3_reref_mode" in payload:
            current_mode = normalize_mode3_reref_mode(self.status.get("mode3_reref_mode", 0), fallback=0)
            reported_mode = normalize_mode3_reref_mode(
                payload.get("mode3_reref_mode", current_mode),
                fallback=current_mode,
            )
            self.status["mode3_reref_mode"] = reported_mode
            if bool(self.status.get("mode3_reref_pending", False)):
                target_mode = normalize_mode3_reref_mode(
                    self.status.get("mode3_reref_target_mode", reported_mode),
                    fallback=reported_mode,
                )
                if (
                    reported_mode == target_mode
                    or time.monotonic() >= float(self._mode3_reref_pending_deadline_monotonic or 0.0)
                ):
                    self.status["mode3_reref_pending"] = False
                    self._mode3_reref_pending_deadline_monotonic = 0.0
        if "mode1_raw_channel" in payload:
            try:
                self.status["mode1_raw_channel"] = max(0, min(15, int(payload.get("mode1_raw_channel", 0) or 0)))
            except Exception:
                pass
        if "mode3_raw_channel" in payload:
            try:
                self.status["mode3_raw_channel"] = max(0, min(15, int(payload.get("mode3_raw_channel", 0) or 0)))
            except Exception:
                pass
        if "mode2_channels" in payload:
            self.status["mode2_channels"] = list(MODE2_FIXED_CHANNELS)
        thresholds = payload.get("mode3_thresholds", [])
        if isinstance(thresholds, list) and thresholds:
            normalized_thresholds = [float(value or 0.0) for value in thresholds[:16]]
            if len(normalized_thresholds) < 16:
                normalized_thresholds.extend([0.0] * (16 - len(normalized_thresholds)))
            self.status["mode3_thresholds"] = normalized_thresholds[:16]
        self._emit_status()

    def _on_impedance_progress(self, payload: Dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            return
        self.status["impedance_test_running"] = True
        self.status["impedance_progress_percent"] = float(payload.get("progress_percent", 0.0) or 0.0)
        self.status["impedance_status_text"] = str(payload.get("status_text", "Preparing impedance test") or "Preparing impedance test")
        self._emit_status()

    def _on_impedance_result(self, payload: Dict[str, Any]) -> None:
        self.status["impedance_test_running"] = False
        record = self._build_impedance_record(payload if isinstance(payload, dict) else {})
        if record is None:
            self.status["impedance_progress_percent"] = 0.0
            self.status["impedance_status_text"] = "No impedance data received"
            self._emit_status()
            return
        if self.impedance_history and self._is_duplicate_impedance_record(self.impedance_history[-1], record):
            record = dict(self.impedance_history[-1])
        else:
            self.impedance_history.append(dict(record))
            if len(self.impedance_history) > IMPEDANCE_HISTORY_LIMIT:
                self.impedance_history = self.impedance_history[-IMPEDANCE_HISTORY_LIMIT:]
            self.impedance_history.sort(key=lambda item: float(item.get("timestamp_epoch", 0.0)))
            record = dict(self.impedance_history[-1])
        self.status["impedance_progress_percent"] = 100.0
        self.status["impedance_status_text"] = "Impedance test completed"
        self._sync_impedance_status_from_history(bump_sequence=True)
        self._emit_status()

    def _on_serial_disconnected(self, reason: str) -> None:
        self._stop_auto_threshold_update(emit_status=False)
        self.status["connected"] = False
        self.status["sampling"] = False
        self.status["active_mode"] = "Idle"
        self.status["save_mode"] = ""
        self.status["save_flags"] = {
            "lfp": False,
            "mode1": False,
            "mode2": False,
            "mode3": False,
        }
        self.status["save_progress"] = {
            "lfp": 0.0,
            "mode1": 0.0,
            "mode2": 0.0,
            "mode3": 0.0,
        }
        self.status["mode3_reref_pending"] = False
        self._mode3_reref_pending_deadline_monotonic = 0.0
        self.status["impedance_test_running"] = False
        self.status["impedance_progress_percent"] = 0.0
        self.status["impedance_status_text"] = "Ready"
        self.error.emit(str(reason))
        self._emit_status()


class RFController(QObject):
    status_changed = pyqtSignal(dict)
    error = pyqtSignal(str)
    RECONNECT_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 30.0)
    SERIAL_READ_TIMEOUT_SECONDS = 0.1
    SERIAL_WRITE_TIMEOUT_SECONDS = 0.2
    QUERY_INTERVAL_SECONDS = 15.0
    QUERY_TIMEOUT_SECONDS = 0.35
    FAST_QUERY_TIMEOUT_SECONDS = 0.2
    FAST_QUERY_SETTLE_SECONDS = 0.05
    QUERY_RETRY_COUNT = 2
    QUERY_SOFT_FAILURE_THRESHOLD = 3
    SOFT_FAILURE_RECHECK_SECONDS = 2.0
    MANUAL_OPERATION_GRACE_SECONDS = 5.0

    def __init__(self, profile: Dict[str, Any]):
        super().__init__()
        self.profile = dict(profile)
        self.connection = None
        self._lock = threading.Lock()
        self._device_fingerprint: Dict[str, Any] = {}
        self._next_reconnect_monotonic = 0.0
        self._last_query_monotonic = 0.0
        self._reconnect_blocked = False
        self._manual_operation_guard_until_monotonic = 0.0
        self.status: Dict[str, Any] = {
            "connected": False,
            "port": str(profile.get("rf_serial_port", "")),
            "power_state": "unknown",
            "recommended_esb_channel": profile.get("recommended_esb_channel"),
            "connection_state": "idle",
            "reconnect_attempts": 0,
            "last_reconnect_epoch": 0.0,
            "last_failure_reason": "",
            "desired_power_state": "unknown",
            "consecutive_query_failures": 0,
            "last_query_attempt_epoch": 0.0,
            "last_success_query_epoch": 0.0,
            "last_recovery_diagnosis": "",
        }

    def _emit_status(self) -> None:
        self.status_changed.emit(dict(self.status))

    def _available_ports(self) -> List[Dict[str, Any]]:
        ports = []
        try:
            for item in serial.tools.list_ports.comports():
                ports.append(extract_port_fingerprint(item))
        except Exception:
            return []
        return ports

    def _preferred_port(self) -> str:
        status_port = str(self.status.get("port", "") or "").strip()
        if status_port:
            return status_port
        return str(self.profile.get("rf_serial_port", "") or "").strip()

    def _record_fingerprint(self, port_name: str, matched_port: Optional[Dict[str, Any]] = None) -> None:
        matched = dict(matched_port or {})
        if not matched:
            for item in self._available_ports():
                if str(item.get("device", "") or "").strip() == str(port_name or "").strip():
                    matched = item
                    break
        if matched:
            self._device_fingerprint = dict(matched)

    def _reset_recovery_state(self) -> None:
        self._next_reconnect_monotonic = 0.0
        self._last_query_monotonic = time.monotonic()
        self._reconnect_blocked = False
        self._manual_operation_guard_until_monotonic = 0.0
        self.status["connection_state"] = "connected"
        self.status["reconnect_attempts"] = 0
        self.status["last_failure_reason"] = ""
        self.status["consecutive_query_failures"] = 0
        self.status["last_reconnect_epoch"] = time.time()
        self.status["last_success_query_epoch"] = self.status["last_reconnect_epoch"]
        self.status["last_recovery_diagnosis"] = ""

    def _close_connection(self) -> None:
        conn = self.connection
        self.connection = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _set_manual_operation_guard(self) -> None:
        self._manual_operation_guard_until_monotonic = (
            time.monotonic() + float(self.MANUAL_OPERATION_GRACE_SECONDS)
        )

    def _mark_query_attempt(self) -> None:
        self.status["last_query_attempt_epoch"] = time.time()

    def _mark_query_success(self, parsed: str) -> None:
        self._last_query_monotonic = time.monotonic()
        self.status["power_state"] = parsed or "unknown"
        self.status["consecutive_query_failures"] = 0
        self.status["last_success_query_epoch"] = time.time()
        self.status["last_failure_reason"] = ""
        if str(self.status.get("connection_state", "") or "").strip().lower() != "connected":
            self.status["connection_state"] = "connected"
            self.status["last_recovery_diagnosis"] = ""

    def _record_query_failure(self, reason: str) -> int:
        self._last_query_monotonic = time.monotonic()
        failures = int(self.status.get("consecutive_query_failures", 0) or 0) + 1
        self.status["consecutive_query_failures"] = failures
        self.status["last_failure_reason"] = str(reason or "").strip()
        return failures

    def _schedule_reconnect(
        self,
        reason: str,
        failed: bool = False,
        connection_state: Optional[str] = None,
    ) -> None:
        self._close_connection()
        self.status["connected"] = False
        self.status["power_state"] = "unknown"
        self.status["last_failure_reason"] = str(reason or "").strip()
        normalized_state = str(connection_state or "").strip().lower()
        if failed:
            normalized_state = "failed"
        elif normalized_state not in {"reconnecting", "port_missing", "device_unresponsive"}:
            normalized_state = "reconnecting"
        self.status["connection_state"] = normalized_state
        self.status["last_recovery_diagnosis"] = normalized_state
        if failed:
            self._reconnect_blocked = True
            self._next_reconnect_monotonic = 0.0
        else:
            attempt_index = max(0, int(self.status.get("reconnect_attempts", 0) or 0))
            delay_index = min(attempt_index, len(self.RECONNECT_BACKOFF_SECONDS) - 1)
            self._next_reconnect_monotonic = time.monotonic() + float(self.RECONNECT_BACKOFF_SECONDS[delay_index])
        self._emit_status()

    def connect_port(self, manual: bool = True, port_name: Optional[str] = None) -> bool:
        if self.connection is not None:
            return bool(self.status["connected"])
        port_name = str(port_name or self._preferred_port() or self.profile.get("rf_serial_port", "")).strip()
        if not port_name:
            self.error.emit("RF serial port is empty")
            return False

        matched_port = None
        ports = self._available_ports()
        for item in ports:
            if str(item.get("device", "") or "").strip() == port_name:
                matched_port = item
                break
        if ports and matched_port is None:
            message = f"RF serial port {port_name} was not found"
            if manual:
                self.error.emit(message)
            self._schedule_reconnect(message, failed=True, connection_state="port_missing")
            return False

        try:
            self.connection = serial.Serial(
                port_name,
                115200,
                timeout=float(self.SERIAL_READ_TIMEOUT_SECONDS),
                write_timeout=float(self.SERIAL_WRITE_TIMEOUT_SECONDS),
            )
            self.status["connected"] = True
            self.status["port"] = port_name
            self._record_fingerprint(port_name, matched_port)
            self._reset_recovery_state()
            self._emit_status()
            return True
        except Exception as exc:
            self.connection = None
            message = f"Failed to connect RF serial {port_name}: {exc}"
            if manual:
                self.error.emit(message)
            self._schedule_reconnect(message)
            return False

    def disconnect_port(self) -> None:
        self._close_connection()
        self.status["connected"] = False
        self.status["power_state"] = "unknown"
        self.status["connection_state"] = "idle"
        self.status["reconnect_attempts"] = 0
        self.status["last_failure_reason"] = ""
        self.status["consecutive_query_failures"] = 0
        self.status["last_recovery_diagnosis"] = ""
        self._next_reconnect_monotonic = 0.0
        self._reconnect_blocked = False
        self._manual_operation_guard_until_monotonic = 0.0
        self._emit_status()

    def _build_cmd(self, addr: int, op: int) -> bytes:
        start = 0xA0
        checksum = (start + int(addr) + int(op)) & 0xFF
        return bytes([start, int(addr) & 0xFF, int(op) & 0xFF, checksum])

    def _addr(self) -> int:
        return 1

    def _parse_status(self, resp: bytes) -> Optional[str]:
        if not resp or len(resp) < 3:
            return None
        addr = self._addr()
        for idx in range(len(resp) - 3, -1, -1):
            if resp[idx] == 0xA0 and resp[idx + 1] == addr:
                state = resp[idx + 2]
                if state == 0x01:
                    return "on"
                if state == 0x00:
                    return "off"
        return None

    def _send_cmd(
        self,
        op: int,
        emit_error: bool = True,
        timeout_seconds: Optional[float] = None,
        schedule_recovery_on_exception: bool = True,
    ) -> bytes:
        conn = self.connection
        if conn is None or not self.status["connected"]:
            if emit_error:
                self.error.emit("RF serial is not connected")
            return b""
        cmd = self._build_cmd(self._addr(), op)
        try:
            with self._lock:
                try:
                    conn.reset_input_buffer()
                except Exception:
                    pass
                try:
                    conn.reset_output_buffer()
                except Exception:
                    pass
                conn.write(cmd)
                conn.flush()
                deadline = time.monotonic() + float(
                    timeout_seconds if timeout_seconds is not None else self.QUERY_TIMEOUT_SECONDS
                )
                response = bytearray()
                while time.monotonic() < deadline:
                    waiting = getattr(conn, "in_waiting", 0)
                    if waiting and waiting > 0:
                        response.extend(conn.read(waiting))
                        time.sleep(0.02)
                    else:
                        time.sleep(0.02)
                return bytes(response)
        except Exception as exc:
            message = f"RF command failed: {exc}"
            if emit_error:
                self.error.emit(message)
            if schedule_recovery_on_exception:
                self._schedule_reconnect(message, connection_state="device_unresponsive")
            return b""

    def _query_once(self, emit_error: bool = True) -> Optional[str]:
        self._mark_query_attempt()
        return self._parse_status(
            self._send_cmd(
                0x05,
                emit_error=emit_error,
                timeout_seconds=self.QUERY_TIMEOUT_SECONDS,
                schedule_recovery_on_exception=False,
            )
        )

    def _query_once_with_timeout(self, timeout_seconds: float, emit_error: bool = False) -> Optional[str]:
        self._mark_query_attempt()
        return self._parse_status(
            self._send_cmd(
                0x05,
                emit_error=emit_error,
                timeout_seconds=float(timeout_seconds),
                schedule_recovery_on_exception=False,
            )
        )

    def _attempt_same_port_reopen_recovery(self, reason: str) -> bool:
        port_name = self._preferred_port()
        if not port_name:
            self._schedule_reconnect(reason, connection_state="device_unresponsive")
            return False
        self._close_connection()
        if not self.connect_port(manual=False, port_name=port_name):
            self.status["last_failure_reason"] = str(reason or "").strip()
            self.status["connection_state"] = "device_unresponsive"
            self.status["last_recovery_diagnosis"] = "device_unresponsive"
            self._emit_status()
            return False
        parsed = self._query_once(emit_error=False)
        if parsed is None:
            self._schedule_reconnect(reason, connection_state="device_unresponsive")
            return False
        self._mark_query_success(parsed)
        self._restore_desired_power_state()
        self._emit_status()
        return True

    def query_status(self, emit_error: bool = True) -> bool:
        parsed = None
        for attempt in range(max(1, int(self.QUERY_RETRY_COUNT))):
            parsed = self._query_once(emit_error=emit_error and attempt == 0)
            if parsed is not None:
                break
        if parsed is None and self.status["connected"]:
            message = (
                "RF query timeout or malformed response after "
                f"{max(1, int(self.QUERY_RETRY_COUNT))} attempt(s)"
            )
            failures = self._record_query_failure(message)
            if emit_error:
                self.error.emit(f"{message} (soft failure {failures}/{self.QUERY_SOFT_FAILURE_THRESHOLD})")
            if failures >= int(self.QUERY_SOFT_FAILURE_THRESHOLD):
                recovered = self._attempt_same_port_reopen_recovery(
                    "RF device unresponsive after repeated query failures"
                )
                if recovered:
                    return True
            self._emit_status()
            return False
        if parsed is None:
            self.status["power_state"] = "unknown"
            self._emit_status()
            return False
        self._mark_query_success(parsed)
        self._emit_status()
        return True

    def set_power(self, enabled: bool) -> bool:
        desired_state = "on" if enabled else "off"
        self.status["desired_power_state"] = desired_state
        self._set_manual_operation_guard()
        parsed = self._parse_status(
            self._send_cmd(
                0x03 if enabled else 0x02,
                emit_error=True,
                timeout_seconds=self.QUERY_TIMEOUT_SECONDS,
                schedule_recovery_on_exception=False,
            )
        )
        if parsed is None:
            self.query_status(emit_error=False)
            parsed = str(self.status.get("power_state", "unknown") or "unknown").strip().lower()
        if parsed in {"on", "off"}:
            self._mark_query_success(parsed)
        else:
            self._record_query_failure("RF power confirmation timeout or malformed response")
            self.status["power_state"] = "unknown"
        self._emit_status()
        return parsed == desired_state

    def query_status_fast(self, emit_error: bool = False) -> bool:
        parsed = self._query_once_with_timeout(self.FAST_QUERY_TIMEOUT_SECONDS, emit_error=emit_error)
        if parsed is None:
            self.status["power_state"] = "unknown"
            self._emit_status()
            return False
        self._mark_query_success(parsed)
        self._emit_status()
        return True

    def set_power_for_habits_confirmation(self, enabled: bool) -> bool:
        desired_state = "on" if enabled else "off"
        self.status["desired_power_state"] = desired_state
        self._set_manual_operation_guard()
        parsed = self._parse_status(
            self._send_cmd(
                0x03 if enabled else 0x02,
                emit_error=False,
                timeout_seconds=self.FAST_QUERY_TIMEOUT_SECONDS,
                schedule_recovery_on_exception=False,
            )
        )
        if parsed is None:
            try:
                time.sleep(float(self.FAST_QUERY_SETTLE_SECONDS))
            except Exception:
                pass
            parsed = self._query_once_with_timeout(self.FAST_QUERY_TIMEOUT_SECONDS, emit_error=False)
        if parsed in {"on", "off"}:
            self._mark_query_success(parsed)
        else:
            self.status["power_state"] = "unknown"
        self._emit_status()
        return parsed == desired_state

    def _current_port_present(self) -> bool:
        current_port = str(self.status.get("port", "") or "").strip()
        if not current_port:
            return False
        for item in self._available_ports():
            if str(item.get("device", "") or "").strip() == current_port:
                return True
        return False

    def _restore_desired_power_state(self) -> None:
        desired_state = str(self.status.get("desired_power_state", "unknown") or "unknown").strip().lower()
        current_state = str(self.status.get("power_state", "unknown") or "unknown").strip().lower()
        if desired_state not in {"on", "off"}:
            return
        if current_state == desired_state:
            return
        self.set_power(desired_state == "on")

    def recover_if_needed(self) -> bool:
        if self.status.get("connected"):
            return True
        if self._reconnect_blocked:
            return False
        if self.status.get("connection_state") not in {"recovering", "reconnecting", "port_missing", "device_unresponsive", "failed"}:
            return False
        now_mono = time.monotonic()
        if self._next_reconnect_monotonic > now_mono:
            return False

        self.status["reconnect_attempts"] = int(self.status.get("reconnect_attempts", 0) or 0) + 1
        preferred_port = self._preferred_port()
        if not preferred_port:
            self._schedule_reconnect("RF reconnect waiting for configured port name", connection_state="reconnecting")
            return False
        available_ports = self._available_ports()
        reconnect_target = resolve_reconnect_port(preferred_port, self._device_fingerprint, available_ports)
        target_port = str(reconnect_target.get("port", "") or "").strip()
        if not target_port:
            self._schedule_reconnect(
                f"RF reconnect waiting for device matching {preferred_port}",
                connection_state="port_missing",
            )
            return False
        self.status["connection_state"] = "reconnecting"
        self._emit_status()
        if not self.connect_port(manual=False, port_name=target_port):
            return False
        if not self.query_status(emit_error=False):
            self._schedule_reconnect(
                f"RF reconnect query failed on {target_port}",
                connection_state="device_unresponsive",
            )
            return False
        self._restore_desired_power_state()
        self._emit_status()
        return True

    def health_tick(self) -> None:
        if self.status.get("connected"):
            if not self._current_port_present():
                self._schedule_reconnect(
                    "RF serial device disappeared from the system",
                    connection_state="port_missing",
                )
                return
            if time.monotonic() < self._manual_operation_guard_until_monotonic:
                return
            query_interval_seconds = float(self.QUERY_INTERVAL_SECONDS)
            if int(self.status.get("consecutive_query_failures", 0) or 0) > 0:
                query_interval_seconds = float(self.SOFT_FAILURE_RECHECK_SECONDS)
            if (time.monotonic() - self._last_query_monotonic) >= query_interval_seconds:
                self.query_status(emit_error=False)
            return
        if self.status.get("connection_state") in {"recovering", "reconnecting", "port_missing", "device_unresponsive"}:
            self.recover_if_needed()


class _BackgroundTextFileWriter:
    def __init__(self, error_callback=None):
        self._queue = queue.Queue()
        self._error_callback = error_callback
        self._stop = threading.Event()
        self._current_file = None
        self._thread = threading.Thread(target=self._run, name="habits-sd-writer", daemon=True)
        self._thread.start()

    def open(self, filepath: str) -> None:
        self._queue.put({"type": "open", "filepath": str(filepath)})

    def write_line(self, text: str) -> None:
        self._queue.put({"type": "write", "text": str(text)})

    def close(self) -> None:
        self._queue.put({"type": "close"})

    def wait_idle(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout or 0.0))
        while True:
            unfinished = int(getattr(self._queue, "unfinished_tasks", 0) or 0)
            if unfinished <= 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def stop(self, timeout: float = 2.0) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self._queue.put(None)
        self._thread.join(timeout=max(0.0, float(timeout or 0.0)))

    def _emit_error(self, message: str) -> None:
        callback = self._error_callback
        if callback is None:
            return
        try:
            callback(str(message))
        except Exception:
            pass

    def _close_current_file(self) -> None:
        current_file = self._current_file
        self._current_file = None
        if current_file is None:
            return
        try:
            current_file.close()
        except Exception as exc:
            self._emit_error(f"Failed to close SD download file: {exc}")

    def _run(self) -> None:
        while True:
            task = self._queue.get()
            try:
                if task is None:
                    self._close_current_file()
                    return
                if not isinstance(task, dict):
                    continue
                task_type = str(task.get("type", "") or "")
                if task_type == "open":
                    filepath = str(task.get("filepath", "") or "").strip()
                    if not filepath:
                        continue
                    self._close_current_file()
                    os.makedirs(os.path.dirname(filepath), exist_ok=True)
                    self._current_file = open(filepath, "w", encoding="utf-8")
                    continue
                if task_type == "write":
                    if self._current_file is not None:
                        self._current_file.write(str(task.get("text", "") or "") + "\n")
                    continue
                if task_type == "close":
                    self._close_current_file()
                    continue
            except Exception as exc:
                self._close_current_file()
                self._emit_error(f"Failed to write SD download file: {exc}")
            finally:
                try:
                    self._queue.task_done()
                except Exception:
                    pass


class HabitsController(QObject):
    status_changed = pyqtSignal(dict)
    error = pyqtSignal(str)
    mode_switch_requested = pyqtSignal(str)
    trial_started = pyqtSignal(int)

    def __init__(self, profile: Dict[str, Any]):
        super().__init__()
        self.profile = dict(profile)
        self.serial_worker: Optional[SerialWorker] = None
        self.data_manager = TrialDataManager()
        self.current_esa_start_time = None
        self.connected_port_name = None
        self.cap_history = deque(maxlen=CAP_HISTORY_LIMIT)
        self._live_update_seq = 0
        self._live_updates = deque(maxlen=HABITS_LIVE_UPDATE_LIMIT)
        mouse_directory = str(profile.get("mice_id_directory", ""))
        self.protocol_flow_store = ProtocolFlowStore(mouse_directory)
        self.status: Dict[str, Any] = {
            "connected": False,
            "port": str(profile.get("habits_serial_port", "")),
            "paused": False,
            "current_trial": 0,
            "trials_today": 0,
            "last_protocol": 0,
            "protocol": 0,
            "performance": 0.0,
            "protocol_trials": 0,
            "trial_type": 0,
            "protocol_perf": 0.0,
            "outcome_code": -1,
            "outcome_text": "-",
            "protocol_progress_raw": "",
            "cache_update_seq": 0,
            "mouse_directory": mouse_directory,
            "mouse_id": extract_mouse_id_from_directory(mouse_directory),
            "selected_data_dir": mouse_directory,
            "start_date": str(profile.get("habits_start_date", "") or ""),
            "last_sync_text": "Not synced",
            "reward_left": 30,
            "reward_middle": 30,
            "reward_right": 30,
            "low_light": 1,
            "high_light": 255,
            "inter_block_interval_ms": 3600000,
            "inter_block_interval_minutes": 60,
            "cap_detect_text": "Disabled",
            "cap_baseline_text": "- / -",
            "cap_filtered_text": "- / -",
            "cap_delta_text": "- / -",
            "cap_drift_text": "Unknown",
            "cap_drift_state": "unknown",
            "cap_history": [],
            "rf_off_confirmed": False,
            "mode3_trial_trigger_paused": False,
            "mode3_trial_trigger_target_paused": False,
            "mode3_trial_trigger_free_water_protocol": 0,
            "mode3_trial_trigger_gate_text": "Ready",
            "mode3_trial_trigger_reason": "",
            "mode3_trial_trigger_pause_threshold": 10,
            "mode3_trial_trigger_resume_threshold": 20,
            "low_battery_mode0_training_target": False,
            "low_battery_mode0_training_active": False,
            "low_battery_mode0_training_reason": "",
            "read_all_seq": 0,
            "protocol_flow": self.protocol_flow_store.get_state(),
        }
        self.cap_status = {
            "baseline_left": 0,
            "baseline_right": 0,
            "delta_left": 0,
            "delta_right": 0,
        }
        self.sd_upload_active = False
        self.sd_writer = _BackgroundTextFileWriter(self.error.emit)
        self.sd_current_file = None
        self.sd_save_dir = None
        self.status.update({
            "sd_download_active": False,
            "sd_download_status_text": "Idle",
            "sd_download_target_dir": "",
            "sd_download_last_error": "",
        })

    def _emit_status(self) -> None:
        self.status["cap_history"] = list(self.cap_history)
        self.status["protocol_flow"] = self.protocol_flow_store.get_state()
        self.status_changed.emit(dict(self.status))

    def _sync_protocol_flow_status(self, emit: bool = True) -> None:
        self.status["protocol_flow"] = self.protocol_flow_store.get_state()
        if emit:
            self._mark_cache_update()
            self._emit_status()

    def _align_flow_to_status_protocol(self) -> None:
        try:
            protocol = int(self.status.get("protocol", self.status.get("last_protocol", 0)) or 0)
        except Exception:
            return
        if protocol < 0 or protocol > 10:
            return
        self.protocol_flow_store.observe_current_protocol(
            protocol,
            {
                "source": "status_alignment",
                "protocol_trials": int(self.status.get("protocol_trials", 0) or 0),
                "trial_num": int(self.status.get("current_trial", 0) or 0),
            },
        )
        self.status["protocol_flow"] = self.protocol_flow_store.get_state()

    def _send_current_protocol_condition(self) -> bool:
        self._align_flow_to_status_protocol()
        block = self.protocol_flow_store.current_block()
        if not block:
            return False
        command = condition_to_firmware_payload(block)
        ok = self._send_serial_command(command)
        if ok:
            state = self.protocol_flow_store.get_state()
            state["last_synced_conditions"] = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "block_id": str(block.get("block_id", "")),
                "protocol": int(block.get("protocol", 0) or 0),
                "command": command,
            }
            self.protocol_flow_store.replace_state(state)
            self._sync_protocol_flow_status(emit=False)
        return ok

    def _send_protocol_flow(self) -> bool:
        self._align_flow_to_status_protocol()
        state = self.protocol_flow_store.get_state()
        command = flow_to_firmware_payload(state)
        if not command:
            return False
        ok = self._send_serial_command(command)
        if ok:
            state = self.protocol_flow_store.get_state()
            state["last_synced_conditions"] = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "command": "PFLOW",
                "flow_length": len(list(state.get("blocks", []) or [])),
                "current_block_id": str(state.get("current_block_id", "") or ""),
            }
            self.protocol_flow_store.replace_state(state)
            self._sync_protocol_flow_status(emit=False)
        return ok

    def sync_current_protocol_condition(self) -> bool:
        return self._send_protocol_flow()

    def _observe_firmware_protocol(self, protocol: int, metrics: Optional[Dict[str, Any]] = None) -> None:
        before = str(self.protocol_flow_store.get_state().get("current_block_id", "") or "")
        block = self.protocol_flow_store.observe_current_protocol(int(protocol), metrics or {})
        after = str(self.protocol_flow_store.get_state().get("current_block_id", "") or "")
        if block and after != before:
            self._sync_protocol_flow_status(emit=False)

    def _mark_cache_update(self) -> None:
        self.status["cache_update_seq"] = int(self.status.get("cache_update_seq", 0) or 0) + 1

    def _serialize_habits_live_value(self, value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {
                str(key): self._serialize_habits_live_value(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self._serialize_habits_live_value(item) for item in value]
        return value

    def _record_live_update(self, kind: str, payload: Any) -> None:
        self._live_update_seq = int(self._live_update_seq or 0) + 1
        self._live_updates.append({
            "sequence": int(self._live_update_seq),
            "kind": str(kind or ""),
            "timestamp_epoch": time.time(),
            "payload": self._serialize_habits_live_value(payload),
        })

    def get_live_updates_since(self, sequence: int = 0, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        try:
            threshold = int(sequence or 0)
        except Exception:
            threshold = 0
        updates = [
            dict(item)
            for item in list(self._live_updates)
            if int(item.get("sequence", 0) or 0) > threshold
        ]
        if limit is not None:
            try:
                limit_value = max(0, int(limit))
            except Exception:
                limit_value = 0
            if limit_value > 0:
                updates = updates[-limit_value:]
        return updates

    def _close_sd_file(self) -> None:
        self.sd_current_file = None
        try:
            self.sd_writer.close()
        except Exception:
            pass

    def wait_for_sd_writer_idle(self, timeout: float = 2.0) -> bool:
        try:
            return bool(self.sd_writer.wait_idle(timeout=timeout))
        except Exception:
            return False

    def _set_sd_download_status(
        self,
        *,
        active: bool,
        status_text: str,
        target_dir: Optional[str] = None,
        error_text: Optional[str] = None,
        emit: bool = True,
    ) -> None:
        self.sd_upload_active = bool(active)
        self.status["sd_download_active"] = bool(active)
        self.status["sd_download_status_text"] = str(status_text or "Idle")
        if target_dir is not None:
            self.sd_save_dir = str(target_dir or "").strip() or None
            self.status["sd_download_target_dir"] = str(target_dir or "").strip()
        if error_text is not None:
            self.status["sd_download_last_error"] = str(error_text or "").strip()
        if emit:
            self._emit_status()

    def start_sd_download(self, target_dir: str) -> bool:
        target_dir = str(target_dir or "").strip()
        if not target_dir:
            self.error.emit("SD download target directory is empty")
            return False
        if self.sd_upload_active:
            self.error.emit("SD download is already in progress")
            return False
        if self.serial_worker is None or not self.status["connected"]:
            self.error.emit("Habits serial is not connected before SD download")
            return False
        try:
            os.makedirs(target_dir, exist_ok=True)
        except Exception as exc:
            self.error.emit(f"Failed to create SD download directory: {exc}")
            return False
        self._close_sd_file()
        self._set_sd_download_status(
            active=True,
            status_text=f"Downloading to {target_dir}",
            target_dir=target_dir,
            error_text="",
            emit=False,
        )
        self._mark_cache_update()
        self._emit_status()
        if not self._send_serial_command("U"):
            self._set_sd_download_status(
                active=False,
                status_text="Failed to start SD download",
                target_dir=target_dir,
                error_text="Failed to send U command",
                emit=False,
            )
            self._mark_cache_update()
            self._emit_status()
            return False
        return True

    def _sync_config_status(
        self,
        *,
        reward_left: Optional[int] = None,
        reward_middle: Optional[int] = None,
        reward_right: Optional[int] = None,
        low_light: Optional[int] = None,
        high_light: Optional[int] = None,
        protocol: Optional[int] = None,
        inter_block_interval_ms: Optional[int] = None,
        teensy_timestamp: Optional[int] = None,
        emit: bool = True,
    ) -> None:
        if reward_left is not None:
            self.status["reward_left"] = int(reward_left)
        if reward_middle is not None:
            self.status["reward_middle"] = int(reward_middle)
        if reward_right is not None:
            self.status["reward_right"] = int(reward_right)
        if low_light is not None:
            self.status["low_light"] = int(low_light)
        if high_light is not None:
            self.status["high_light"] = int(high_light)
        if protocol is not None:
            self.status["protocol"] = int(protocol)
            self.status["last_protocol"] = int(protocol)
        if inter_block_interval_ms is not None:
            interval_ms = max(0, int(inter_block_interval_ms))
            self.status["inter_block_interval_ms"] = interval_ms
            self.status["inter_block_interval_minutes"] = int(round(interval_ms / 60000.0))
        if teensy_timestamp:
            try:
                synced_at = datetime.fromtimestamp(float(teensy_timestamp)).strftime("%H:%M:%S")
                self.status["last_sync_text"] = f"Synced: {synced_at}"
            except Exception:
                pass
        if emit:
            self._emit_status()

    @staticmethod
    def _parse_cap_pair_text(value: Any, fallback: str = "- / -") -> str:
        text = str(value or "").strip()
        if not text:
            return fallback
        if "/" in text:
            left, right = [part.strip() for part in text.split("/", 1)]
            if left and right:
                return f"{left} / {right}"
        return fallback

    def _update_cap_status(
        self,
        *,
        detect_text: Optional[str] = None,
        baseline_text: Optional[str] = None,
        filtered_text: Optional[str] = None,
        delta_text: Optional[str] = None,
        drift_text: Optional[str] = None,
        drift_state: Optional[str] = None,
        history_entry: Optional[Dict[str, Any]] = None,
        emit: bool = True,
    ) -> None:
        if detect_text is not None:
            self.status["cap_detect_text"] = str(detect_text)
        if baseline_text is not None:
            self.status["cap_baseline_text"] = str(baseline_text)
        if filtered_text is not None:
            self.status["cap_filtered_text"] = str(filtered_text)
        if delta_text is not None:
            self.status["cap_delta_text"] = str(delta_text)
        if drift_text is not None:
            self.status["cap_drift_text"] = str(drift_text)
        if drift_state is not None:
            self.status["cap_drift_state"] = str(drift_state)
        if isinstance(history_entry, dict):
            self.cap_history.append(dict(history_entry))
        if emit:
            self._emit_status()

    def refresh_profile_metadata(self) -> None:
        self.status["mouse_directory"] = str(self.profile.get("mice_id_directory", ""))
        self.status["mouse_id"] = extract_mouse_id_from_directory(self.status["mouse_directory"])
        self.status["selected_data_dir"] = self.status["mouse_directory"]
        self.status["start_date"] = str(self.profile.get("habits_start_date", "") or "")
        self.protocol_flow_store.set_mouse_directory(self.status["mouse_directory"])
        self.status["protocol_flow"] = self.protocol_flow_store.get_state()
        if not self.status.get("connected"):
            self.status["port"] = str(self.profile.get("habits_serial_port", ""))
        self._emit_status()

    def set_data_directory(self, directory: str) -> None:
        directory = str(directory or "").strip()
        self.profile["mice_id_directory"] = directory
        self.status["mouse_directory"] = directory
        self.status["selected_data_dir"] = directory
        self.status["mouse_id"] = extract_mouse_id_from_directory(directory)
        self.protocol_flow_store.set_mouse_directory(directory)
        self.status["protocol_flow"] = self.protocol_flow_store.get_state()
        self._emit_status()

    def set_start_date(self, value: str) -> None:
        value = str(value or "").strip()
        self.profile["habits_start_date"] = value
        self.status["start_date"] = value
        self._emit_status()

    def load_cached_payloads(self, trials: List[Dict[str, Any]], mode_switches: List[Dict[str, Any]]) -> None:
        self.load_cached_payloads_with_streams(trials, mode_switches, [], [])

    def load_cached_payloads_with_streams(
        self,
        trials: List[Dict[str, Any]],
        mode_switches: List[Dict[str, Any]],
        event_data: List[Dict[str, Any]],
        state_data: List[Dict[str, Any]],
    ) -> None:
        try:
            if isinstance(trials, list) and trials:
                self.data_manager.load_from_list([dict(item) for item in trials if isinstance(item, dict)])
            else:
                self.data_manager.clear_data()
        except Exception:
            self.data_manager.clear_data()

        self.data_manager.mode_switches = []
        for item in list(mode_switches or []):
            if not isinstance(item, dict):
                continue
            start_raw = item.get("start")
            end_raw = item.get("end")
            if not start_raw or not end_raw:
                continue
            try:
                self.data_manager.mode_switches.append({
                    "start": datetime.fromisoformat(str(start_raw)),
                    "end": datetime.fromisoformat(str(end_raw)),
                })
            except Exception:
                continue
        self.data_manager.state_data.clear()
        for item in list(state_data or []):
            if isinstance(item, dict):
                self.data_manager.state_data.append(dict(item))
        self.data_manager.event_data.clear()
        for item in list(event_data or []):
            if isinstance(item, dict):
                self.data_manager.event_data.append(dict(item))
        self.status["trials_today"] = int(self.data_manager.get_daily_trial_count())
        if self.data_manager.trial_data:
            latest = self.data_manager.trial_data[-1]
            self.status["current_trial"] = int(latest.get("trial_num", self.status.get("current_trial", 0)) or 0)
            self.status["last_protocol"] = int(latest.get("protocol", self.status.get("last_protocol", 0)) or 0)
            self.status["protocol"] = int(self.status["last_protocol"] or 0)
            self.status["performance"] = float(latest.get("performance", self.status.get("performance", 0.0)) or 0.0)
            self.status["protocol_trials"] = int(latest.get("protocol_trials", self.status.get("protocol_trials", 0)) or 0)
            self.status["trial_type"] = int(latest.get("trial_type", self.status.get("trial_type", 0)) or 0)
            self.status["protocol_perf"] = float(latest.get("protocol_perf", self.status.get("protocol_perf", 0.0)) or 0.0)
            outcome = int(latest.get("outcome", -1) or -1)
            self.status["outcome_code"] = outcome
            self.status["outcome_text"] = {
                0: "No Resp",
                1: "Correct",
                2: "Error",
                3: "Early Lick",
            }.get(outcome, self.status.get("outcome_text", "-"))
        else:
            self.status["current_trial"] = 0
            self.status["trials_today"] = 0
            self.status["performance"] = 0.0
            self.status["protocol_trials"] = 0
            self.status["trial_type"] = 0
            self.status["protocol_perf"] = 0.0
            self.status["outcome_code"] = -1
            self.status["outcome_text"] = "-"
        self._emit_status()

    def load_persisted_history(self) -> bool:
        directory = str(self.status.get("selected_data_dir") or self.status.get("mouse_directory") or "").strip()
        if not directory:
            return False

        trial_data_file = os.path.join(directory, "trial_data.json")
        mode_switch_file = os.path.join(directory, "mode_switch_data.json")
        params_file = os.path.join(directory, "habits_params.json")

        trials = []
        mode_switches = []
        loaded = False

        if os.path.exists(params_file):
            try:
                with open(params_file, "r", encoding="utf-8") as f:
                    params = dict(json.load(f) or {})
                if not self.status.get("start_date"):
                    self.status["start_date"] = str(params.get("start_date", "") or "")
                if params.get("mouse_id"):
                    self.status["mouse_id"] = str(params.get("mouse_id") or self.status["mouse_id"])
                self._sync_config_status(
                    reward_left=params.get("reward_left"),
                    reward_middle=params.get("reward_middle"),
                    reward_right=params.get("reward_right"),
                    low_light=params.get("low_light"),
                    high_light=params.get("high_light"),
                    protocol=params.get("protocol"),
                    inter_block_interval_ms=max(
                        0,
                        int(params.get("inter_block_interval_minutes", self.status.get("inter_block_interval_minutes", 60)) or 0),
                    ) * 60000,
                    emit=False,
                )
                cap_history = list(params.get("cap_history", []) or [])
                self.cap_history.clear()
                for item in cap_history[-CAP_HISTORY_LIMIT:]:
                    if isinstance(item, dict):
                        self.cap_history.append(dict(item))
                self._update_cap_status(
                    detect_text="Enabled" if str(params.get("cap_detect", "") or "").strip().lower() == "enabled" else "Disabled",
                    baseline_text=self._parse_cap_pair_text(params.get("cap_baseline")),
                    filtered_text=self._parse_cap_pair_text(params.get("cap_filtered")),
                    delta_text=self._parse_cap_pair_text(params.get("cap_delta")),
                    drift_text=str(params.get("cap_drift", "Unknown") or "Unknown"),
                    drift_state=str(params.get("cap_drift_state", "unknown") or "unknown"),
                    emit=False,
                )
                loaded = True
            except Exception:
                pass

        if os.path.exists(trial_data_file):
            try:
                with open(trial_data_file, "r", encoding="utf-8") as f:
                    trials = list(json.load(f) or [])
                loaded = loaded or bool(trials)
            except Exception:
                trials = []

        if os.path.exists(mode_switch_file):
            try:
                with open(mode_switch_file, "r", encoding="utf-8") as f:
                    mode_switches = list(json.load(f) or [])
                loaded = loaded or bool(mode_switches)
            except Exception:
                mode_switches = []

        if loaded:
            self.load_cached_payloads(trials, mode_switches)
        return loaded

    def connect_port(self) -> bool:
        if self.serial_worker is not None:
            if bool(self.status["connected"]):
                return True
            stale_worker = self.serial_worker
            self.serial_worker = None
            try:
                stale_worker.stop()
            except Exception:
                pass
        port_name = str(self.profile.get("habits_serial_port", "")).strip()
        if not port_name:
            self.error.emit("Habits serial port is empty")
            return False
        try:
            worker = SerialWorker(port_name)
            worker.connected.connect(self._on_connected)
            worker.data_received.connect(self._handle_serial_data)
            worker.error_occurred.connect(self._on_serial_error)
            worker.disconnected.connect(self._on_serial_disconnected)
            worker.start()
            self.serial_worker = worker
            return True
        except Exception as exc:
            self.serial_worker = None
            self.error.emit(f"Failed to connect Habits serial {port_name}: {exc}")
            return False

    def disconnect_port(self) -> None:
        worker = self.serial_worker
        self.serial_worker = None
        self._close_sd_file()
        self._set_sd_download_status(
            active=False,
            status_text="Idle",
            error_text="",
            emit=False,
        )
        if worker is not None:
            try:
                worker.stop()
            except Exception:
                pass
        self.connected_port_name = None
        self.status["connected"] = False
        self._emit_status()

    def _send_serial_command(self, command: str) -> bool:
        if self.serial_worker is None or not self.status["connected"]:
            self.error.emit("Habits serial is not connected")
            return False
        return bool(self.serial_worker.send_data(command))

    def send_pause(self) -> bool:
        paused = bool(self.status.get("paused", False))
        ok = self._send_serial_command("M" if paused else "P")
        if ok:
            self.status["paused"] = not paused
            self._emit_status()
        return ok

    def send_reward(self, left: int, middle: int, right: int) -> bool:
        ok = self._send_serial_command(f"R{int(left)},{int(right)},{int(middle)},")
        if ok:
            self._sync_config_status(
                reward_left=int(left),
                reward_middle=int(middle),
                reward_right=int(right),
            )
        return ok

    def send_light(self, low: int, high: int) -> bool:
        ok = self._send_serial_command(f"L{int(low)},{int(high)},")
        if ok:
            self._sync_config_status(low_light=int(low), high_light=int(high))
        return ok

    def send_protocol(self, protocol: int) -> bool:
        protocol = int(protocol)
        if protocol < 0 or protocol > 10:
            self.error.emit(
                "Manual protocol commands are limited to P0-P10; low-battery training is routed automatically as Mode0 saved to the Mode3 folder"
            )
            return False
        ok = self._send_serial_command(f"Z{protocol},")
        if ok:
            self.protocol_flow_store.set_current_block_by_protocol(protocol)
            self._sync_config_status(protocol=protocol)
            self._send_protocol_flow()
        return ok

    def send_block_switch_mode(self) -> bool:
        self.error.emit("BlockSwitchMode is disabled for the current DBRuleSwitch protocol flow")
        return False

    def replace_protocol_flow(self, flow_state: Dict[str, Any]) -> bool:
        self.protocol_flow_store.replace_state(flow_state if isinstance(flow_state, dict) else {})
        self._align_flow_to_status_protocol()
        self._sync_protocol_flow_status(emit=False)
        if self.status.get("connected"):
            self._send_protocol_flow()
        self._mark_cache_update()
        self._emit_status()
        return True

    def set_current_protocol_flow_conditions(self, conditions: Dict[str, Any]) -> bool:
        block = self.protocol_flow_store.update_current_conditions(conditions if isinstance(conditions, dict) else {})
        if not block:
            self.error.emit("Cannot update conditions for a fixed or missing protocol block")
            return False
        if self.status.get("connected"):
            self._send_protocol_flow()
        self._sync_protocol_flow_status()
        return True

    def set_current_protocol_from_flow(self, protocol: int) -> bool:
        ok = self.send_protocol(int(protocol))
        if ok:
            self._sync_protocol_flow_status()
        return ok

    def send_post_training_protocol0(self) -> bool:
        return self._send_serial_command("G")

    def send_inter_block_interval(self, interval_minutes: int) -> bool:
        interval_ms = max(0, int(interval_minutes)) * 60 * 1000
        ok = self._send_serial_command(f"I{interval_ms},")
        if ok:
            self._sync_config_status(inter_block_interval_ms=interval_ms)
        return ok

    def send_time_sync(self) -> bool:
        ok = self._send_serial_command(f"T{int(time.time())}")
        if ok:
            self.status["last_sync_text"] = f"Synced: {datetime.now().strftime('%H:%M:%S')}"
            self._emit_status()
        return ok

    def send_read_all(self) -> bool:
        return self._send_serial_command("A")

    def send_handshake(self) -> bool:
        return self._send_serial_command("H")

    def send_cap_snapshot(self) -> bool:
        return self._send_serial_command("C")

    def send_rf_off_ready(self) -> bool:
        ok = self._send_serial_command("F")
        if ok:
            self.status["rf_off_confirmed"] = True
            self._emit_status()
        return ok

    def send_mode3_trial_trigger_pause(self, paused: bool) -> bool:
        desired = bool(paused)
        ok = self._send_serial_command("D0," if desired else "D1,")
        if ok:
            self.status["mode3_trial_trigger_paused"] = desired
            self.status["mode3_trial_trigger_free_water_protocol"] = 0
            self._emit_status()
        return ok

    def send_charging_warning(self, duration_ms: int = 3000, fan_enabled: bool = True) -> bool:
        duration_ms = max(0, int(duration_ms))
        fan_value = 1 if bool(fan_enabled) else 0
        return self._send_serial_command(f"W{duration_ms},{fan_value},")

    def stop_charging_warning(self) -> bool:
        return self._send_serial_command("W0,")

    def _on_connected(self, port: str) -> None:
        self.connected_port_name = port
        self.status["connected"] = True
        self.status["port"] = port
        self._emit_status()
        self.send_time_sync()
        self.send_read_all()

    def _on_serial_error(self, reason: str) -> None:
        self.error.emit(str(reason))

    def _on_serial_disconnected(self, reason: str) -> None:
        self.serial_worker = None
        self._close_sd_file()
        self._set_sd_download_status(
            active=False,
            status_text="Idle",
            error_text=str(reason or ""),
            emit=False,
        )
        self.status["connected"] = False
        self.connected_port_name = None
        self.error.emit(str(reason))
        self._emit_status()

    def _handle_serial_data(self, data: str) -> None:
        data = str(data or "").strip()
        if not data:
            return
        if data == "SH":
            self._record_live_update("handshake", {"raw": data})
            self._mark_cache_update()
            self._emit_status()
            return
        if data.startswith("SD_"):
            self._handle_sd_serial_data(data)
            return
        if data in {"SD0", "SD1"}:
            free_water_active = data == "SD0"
            self.status["mode3_trial_trigger_paused"] = free_water_active
            self.status["mode3_trial_trigger_free_water_protocol"] = 0
            self._record_live_update("firmware_low_battery_pause", {
                "active": bool(free_water_active),
                "protocol": int(self.status.get("protocol", 0) or 0),
                "raw": data,
            })
            self._mark_cache_update()
            self._emit_status()
            return
        if data in {"SDP0", "SDP1"}:
            self.status["mode3_trial_trigger_target_paused"] = data == "SDP0"
            self._record_live_update("firmware_low_battery_pause_target", {
                "target_paused": bool(data == "SDP0"),
                "raw": data,
            })
            self._mark_cache_update()
            self._emit_status()
            return
        if data.startswith("C:"):
            try:
                trial_num = int(data[2:])
            except Exception:
                trial_num = 0
            self.status["current_trial"] = max(int(trial_num), self.status["current_trial"])
            self.trial_started.emit(int(trial_num))
            self._mark_cache_update()
            self._emit_status()
            return
        if data.startswith("Tevent:"):
            self._parse_tevent_data(data)
            return
        if data.startswith("Trial:"):
            self._parse_trial_state_data(data)
            return
        if data.startswith("ModeSwitch:"):
            command = data[11:].strip()
            if command == "ESA":
                self.current_esa_start_time = datetime.now()
                self._record_live_update("mode_switch_state", {
                    "state": "ESA",
                    "timestamp": self.current_esa_start_time,
                })
            elif command == "LFP" and self.current_esa_start_time is not None:
                self.data_manager.add_mode_switch({
                    "start": self.current_esa_start_time,
                    "end": datetime.now(),
                })
                self._record_live_update("mode_switch", self.data_manager.mode_switches[-1])
                self.current_esa_start_time = None
            self._mark_cache_update()
            self._emit_status()
            self.mode_switch_requested.emit(command)
            return
        self._parse_received_data(data)

    def _parse_received_data(self, data: str) -> None:
        try:
            if data.startswith("T"):
                parts = data[1:].split(",")
                if len(parts) < 8:
                    return
                trial_num = int(parts[0])
                trial_type = int(parts[1])
                protocol_index = int(parts[2])
                outcome = int(parts[3])
                early_lick_rate = float(parts[4])
                perf_100 = float(parts[5])
                protocol_trials = int(parts[6])
                protocol_perf = float(parts[7])
                trial_data = {
                    "timestamp": datetime.now(),
                    "trial_num": trial_num,
                    "trial_type": trial_type,
                    "protocol": protocol_index,
                    "outcome": outcome,
                    "early_lick_rate": early_lick_rate,
                    "performance": perf_100,
                    "protocol_trials": protocol_trials,
                    "protocol_perf": protocol_perf,
                }
                if len(parts) > 8 and parts[8] != "":
                    try:
                        trial_data["rule"] = int(parts[8])
                    except ValueError:
                        pass
                if len(parts) > 9 and parts[9] != "":
                    try:
                        trial_data["curr_stimu0"] = float(parts[9])
                    except ValueError:
                        pass
                if len(parts) > 10 and parts[10] != "":
                    try:
                        trial_data["curr_stimu1"] = float(parts[10])
                    except ValueError:
                        pass
                self.data_manager.add_trial(trial_data)
                self._record_live_update("trial", trial_data)
                self.status["current_trial"] = int(trial_num)
                self.status["trials_today"] = int(self.data_manager.get_daily_trial_count())
                self.status["last_protocol"] = int(protocol_index)
                self.status["protocol"] = int(protocol_index)
                self.status["performance"] = float(perf_100)
                self.status["protocol_trials"] = int(protocol_trials)
                self.status["trial_type"] = int(trial_type)
                self.status["protocol_perf"] = float(protocol_perf)
                self.status["outcome_code"] = int(outcome)
                self.status["outcome_text"] = {
                    0: "No Resp",
                    1: "Correct",
                    2: "Error",
                    3: "Early Lick",
                }.get(outcome, "Other")
                self._observe_firmware_protocol(
                    protocol_index,
                    {
                        "trial_num": trial_num,
                        "protocol_trials": protocol_trials,
                        "protocol_perf": protocol_perf,
                        "source": "trial_packet",
                    },
                )
                self._mark_cache_update()
                self._emit_status()
            elif data.startswith("A"):
                data_part = data[1:-1] if data.endswith(";") else data[1:]
                parts = data_part.split(";")
                if len(parts) < 9:
                    return
                inter_block_interval_ms = 3600000
                if len(parts) >= 10 and str(parts[9]).strip() != "":
                    inter_block_interval_ms = int(parts[9])
                self._sync_config_status(
                    reward_left=int(parts[0]),
                    reward_right=int(parts[1]),
                    reward_middle=int(parts[2]),
                    low_light=int(parts[3]),
                    high_light=int(parts[4]),
                    protocol=int(parts[5]),
                    teensy_timestamp=int(parts[7]),
                    inter_block_interval_ms=inter_block_interval_ms,
                    emit=False,
                )
                self._observe_firmware_protocol(int(parts[5]), {"source": "read_all"})
                self.status["read_all_seq"] = int(self.status.get("read_all_seq", 0) or 0) + 1
                self._mark_cache_update()
                self._emit_status()
            elif data.startswith("PG,"):
                self.status["protocol_progress_raw"] = str(data)
                fields = {}
                for item in data[3:].split(";"):
                    if "=" not in item:
                        continue
                    key, value = item.split("=", 1)
                    fields[str(key).strip()] = str(value).strip()
                if fields:
                    free_water_active = fields.get("trial_trigger_free_water") == "1"
                    if free_water_active:
                        self.status["mode3_trial_trigger_paused"] = True
                        self.status["mode3_trial_trigger_free_water_protocol"] = 0
                    elif "trial_trigger_free_water" in fields:
                        self.status["mode3_trial_trigger_paused"] = False
                        self.status["mode3_trial_trigger_free_water_protocol"] = 0
                    if fields.get("curr"):
                        try:
                            self.status["protocol"] = int(fields["curr"])
                            self.status["last_protocol"] = int(fields["curr"])
                        except Exception:
                            pass
                    if fields.get("trial_count"):
                        try:
                            self.status["protocol_trials"] = int(float(fields["trial_count"]))
                        except Exception:
                            pass
                    firmware_flow = flow_state_from_firmware_fields(
                        self.protocol_flow_store.get_state(),
                        fields,
                    )
                    if firmware_flow is not None:
                        self.protocol_flow_store.replace_state(firmware_flow)
                        self.status["protocol_flow"] = self.protocol_flow_store.get_state()
                    elif fields.get("curr"):
                        try:
                            self._observe_firmware_protocol(
                                int(fields["curr"]),
                                {
                                    "source": "protocol_progress",
                                    "trial_count": fields.get("trial_count", ""),
                                },
                            )
                        except Exception:
                            pass
                self._record_live_update("protocol_progress", {
                    "raw": str(data),
                    "fields": fields,
                    "protocol": int(self.status.get("protocol", 0) or 0),
                    "free_water_active": bool(self.status.get("mode3_trial_trigger_paused", False)),
                })
                self._mark_cache_update()
                self._emit_status()
            elif data.startswith("CD,"):
                parts = data.split(",")
                if len(parts) >= 2:
                    detect_enabled = int(parts[1])
                    self._update_cap_status(
                        detect_text="Enabled" if detect_enabled else "Disabled",
                        emit=False,
                    )
                    self._mark_cache_update()
                    self._emit_status()
            elif data.startswith("CB,"):
                parts = data.split(",")
                if len(parts) < 7:
                    return

                detect_text = self.status.get("cap_detect_text", "Disabled")
                trial_num = None
                baseline_fallback_flag = 0
                if len(parts) >= 10:
                    trial_num = int(parts[1])
                    detect_enabled = int(parts[2])
                    detect_text = "Enabled" if detect_enabled else "Disabled"
                    baseline_left = int(parts[3])
                    baseline_right = int(parts[4])
                    filtered_left = int(parts[5])
                    filtered_right = int(parts[6])
                    delta_left = int(parts[7])
                    delta_right = int(parts[8])
                    if parts[9] != "":
                        baseline_fallback_flag = int(parts[9])
                elif len(parts) == 9:
                    detect_like = parts[2] in ("0", "1")
                    fallback_like = parts[8] in ("0", "1")
                    trial_num = int(parts[1])
                    if detect_like and not fallback_like:
                        detect_enabled = int(parts[2])
                        detect_text = "Enabled" if detect_enabled else "Disabled"
                        baseline_left = int(parts[3])
                        baseline_right = int(parts[4])
                        filtered_left = int(parts[5])
                        filtered_right = int(parts[6])
                        delta_left = int(parts[7])
                        delta_right = int(parts[8])
                    else:
                        baseline_left = int(parts[2])
                        baseline_right = int(parts[3])
                        filtered_left = int(parts[4])
                        filtered_right = int(parts[5])
                        delta_left = int(parts[6])
                        delta_right = int(parts[7])
                        if parts[8] != "":
                            baseline_fallback_flag = int(parts[8])
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

                self.cap_status.update({
                    "baseline_left": baseline_left,
                    "baseline_right": baseline_right,
                    "delta_left": delta_left,
                    "delta_right": delta_right,
                })
                max_delta = max(abs(int(delta_left)), abs(int(delta_right)))
                if max_delta >= CAP_ALARM_THRESHOLD:
                    drift_state = "alarm"
                    drift_text = f"Alarm (Δ={max_delta})"
                elif max_delta >= CAP_WARNING_THRESHOLD:
                    drift_state = "warning"
                    drift_text = f"Warning (Δ={max_delta})"
                else:
                    drift_state = "normal"
                    drift_text = f"Normal (Δ={max_delta})"
                history_entry = None
                if trial_num is not None:
                    history_entry = {
                        "trial_num": int(trial_num),
                        "baseline_left": int(baseline_left),
                        "baseline_right": int(baseline_right),
                        "filtered_left": int(filtered_left),
                        "filtered_right": int(filtered_right),
                        "delta_left": int(delta_left),
                        "delta_right": int(delta_right),
                        "detect_enabled": detect_text == "Enabled",
                        "timestamp": datetime.now().isoformat(),
                    }
                self._update_cap_status(
                    detect_text=detect_text,
                    baseline_text=f"{int(baseline_left)} / {int(baseline_right)}",
                    filtered_text=f"{int(filtered_left)} / {int(filtered_right)}",
                    delta_text=f"{int(delta_left):+d} / {int(delta_right):+d}",
                    drift_text=(
                        f"{drift_text} | fallback"
                        if baseline_fallback_flag
                        else drift_text
                    ),
                    drift_state=drift_state,
                    history_entry=history_entry,
                    emit=False,
                )
                self._mark_cache_update()
                self._emit_status()
            elif data.startswith("SF1"):
                self.status["rf_off_confirmed"] = True
                self._mark_cache_update()
                self._emit_status()
        except Exception as exc:
            self.error.emit(f"Failed to parse Habits payload: {exc}")

    def _handle_sd_serial_data(self, data: str) -> None:
        try:
            if data == "SD_UPLOAD_START":
                self._set_sd_download_status(
                    active=True,
                    status_text=f"Downloading to {self.sd_save_dir or '-'}",
                    emit=False,
                )
                self._mark_cache_update()
                self._emit_status()
                return
            if data.startswith("SD_FILE_START:"):
                filename = data[14:].split(",", 1)[0].strip()
                if not filename:
                    return
                target_dir = str(self.sd_save_dir or "").strip()
                if not target_dir:
                    self._set_sd_download_status(
                        active=False,
                        status_text="Download failed",
                        error_text="Received SD file payload without a target directory",
                        emit=False,
                    )
                    self._mark_cache_update()
                    self._emit_status()
                    self.error.emit("Received SD file payload without a target directory")
                    return
                os.makedirs(target_dir, exist_ok=True)
                self._close_sd_file()
                filepath = os.path.join(target_dir, filename)
                self.sd_current_file = filepath
                self.sd_writer.open(filepath)
                self._set_sd_download_status(
                    active=True,
                    status_text=f"Downloading {filename}",
                    emit=False,
                )
                self._mark_cache_update()
                self._emit_status()
                return
            if data.startswith("SD_DATA:"):
                if self.sd_current_file is not None:
                    self.sd_writer.write_line(data[8:])
                return
            if data.startswith("SD_FILE_END:"):
                self._close_sd_file()
                return
            if data == "SD_UPLOAD_END":
                self._close_sd_file()
                self._set_sd_download_status(
                    active=False,
                    status_text="Download complete",
                    error_text="",
                    emit=False,
                )
                self._mark_cache_update()
                self._emit_status()
                return
        except Exception as exc:
            self._close_sd_file()
            self._set_sd_download_status(
                active=False,
                status_text="Download failed",
                error_text=str(exc),
                emit=False,
            )
            self._mark_cache_update()
            self._emit_status()
            self.error.emit(f"Failed to handle SD download payload: {exc}")

    def _parse_tevent_data(self, data: str) -> None:
        try:
            parts = data.split()
            if len(parts) < 2:
                return
            trial_num = int(parts[0][7:])
            n_events = int(parts[1])
            events = []
            idx = 2
            while idx + 1 < len(parts) and len(events) < n_events:
                events.append({
                    "id": int(parts[idx]),
                    "time": int(parts[idx + 1]),
                })
                idx += 2
            self.data_manager.add_event_data(trial_num, events)
            self._record_live_update("event", {
                "trial_num": int(trial_num),
                "events": events,
            })
            self._mark_cache_update()
            self._emit_status()
        except Exception:
            pass

    def _parse_trial_state_data(self, data: str) -> None:
        try:
            content = data[6:].strip()
            parts = content.split()
            if len(parts) < 3:
                return
            trial_num = int(parts[0])
            outcome = int(parts[1])
            n_visited = int(parts[2])
            states = []
            idx = 3
            while idx + 1 < len(parts) and len(states) < n_visited:
                states.append({
                    "id": int(parts[idx]),
                    "time": int(parts[idx + 1]),
                })
                idx += 2
            self.data_manager.add_state_data(trial_num, outcome, states)
            self._record_live_update("state", {
                "trial_num": int(trial_num),
                "outcome": int(outcome),
                "states": states,
            })
            self._mark_cache_update()
            self._emit_status()
        except Exception:
            pass

    def get_trial_data_for_cache(self) -> List[Dict[str, Any]]:
        serialized = []
        for trial in self.data_manager.get_all_trial_data():
            item = dict(trial)
            timestamp = item.get("timestamp")
            if isinstance(timestamp, datetime):
                item["timestamp"] = timestamp.isoformat()
            serialized.append(item)
        return serialized

    def get_event_data_for_cache(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.data_manager.get_event_data_raw()]

    def get_state_data_for_cache(self) -> List[Dict[str, Any]]:
        return [dict(item) for item in self.data_manager.get_state_data()]

    def get_mode_switches_for_cache(self) -> List[Dict[str, Any]]:
        serialized = []
        for item in list(self.data_manager.mode_switches):
            serialized.append({
                "start": item["start"].isoformat() if isinstance(item.get("start"), datetime) else str(item.get("start")),
                "end": item["end"].isoformat() if isinstance(item.get("end"), datetime) else str(item.get("end")),
            })
        if self.current_esa_start_time is not None:
            serialized.append({
                "start": self.current_esa_start_time.isoformat(),
                "end": datetime.now().isoformat(),
            })
        return serialized


class CameraController(QObject):
    status_changed = pyqtSignal(dict)
    error = pyqtSignal(str)

    def __init__(self, profile: Dict[str, Any]):
        super().__init__()
        self.profile = dict(profile)
        self.module = CameraModule()
        self.module.set_preview_enabled(False)
        self.preview_max_width = 640
        self.preview_jpeg_quality = 72
        self.preview_background_rate = 1.0
        self.preview_active_rate = 8.0
        self.preview_recording_rate = 5.0
        self.status: Dict[str, Any] = {
            "open": False,
            "recording": False,
            "camera_id": int(profile.get("camera_id", 0) or 0),
            "preview_enabled": False,
            "recording_path": "",
            "health_state": "ok",
            "reopen_attempts": 0,
            "last_frame_age_sec": 0.0,
            "last_recovery_epoch": 0.0,
            "recovery_segment_count": 0,
        }

    def configure_preview_stream(
        self,
        max_width: int = 640,
        jpeg_quality: int = 72,
        preview_background_rate: float = 1.0,
        preview_active_rate: float = 8.0,
        preview_recording_rate: float = 5.0,
    ) -> None:
        self.preview_max_width = max(160, int(max_width or self.preview_max_width))
        self.preview_jpeg_quality = max(40, min(90, int(jpeg_quality or self.preview_jpeg_quality)))
        self.preview_background_rate = max(0.2, float(preview_background_rate or self.preview_background_rate))
        self.preview_active_rate = max(0.2, float(preview_active_rate or 8.0))
        self.preview_recording_rate = max(0.2, float(preview_recording_rate or 5.0))
        if hasattr(self.module, "configure_preview_stream"):
            try:
                self.module.configure_preview_stream(
                    max_width=self.preview_max_width,
                    jpeg_quality=self.preview_jpeg_quality,
                    preview_background_rate=self.preview_background_rate,
                    preview_active_rate=self.preview_active_rate,
                    preview_recording_rate=self.preview_recording_rate,
                )
            except Exception:
                pass

    def _sync_recovery_status(self) -> None:
        self.status["health_state"] = str(getattr(self.module, "health_state", "ok") or "ok")
        self.status["reopen_attempts"] = int(getattr(self.module, "reopen_attempts", 0) or 0)
        self.status["last_frame_age_sec"] = float(self.module.get_last_frame_age_seconds() or 0.0)
        self.status["last_recovery_epoch"] = float(getattr(self.module, "last_recovery_epoch", 0.0) or 0.0)
        self.status["recovery_segment_count"] = int(getattr(self.module, "recovery_segment_count", 0) or 0)
        self.status["recording_path"] = str(getattr(self.module, "current_recording_path", "") or self.status.get("recording_path", ""))

    def _emit_status(self) -> None:
        self._sync_recovery_status()
        self.status_changed.emit(dict(self.status))

    def open_camera(self) -> bool:
        camera_id = int(self.profile.get("camera_id", 0) or 0)
        ok = bool(self.module.open_camera(camera_id))
        if not ok:
            self.status["open"] = False
            self.status["recording"] = False
            self._emit_status()
            self.error.emit(self.module.last_error or f"Failed to open camera {camera_id}")
            return False
        self.status["open"] = True
        self.status["camera_id"] = camera_id
        self._emit_status()
        return True

    def set_camera_id(self, camera_id: int) -> bool:
        try:
            camera_id = int(camera_id)
        except Exception:
            self.error.emit(f"Invalid camera id: {camera_id!r}")
            return False
        was_open = bool(self.status.get("open", False))
        was_recording = bool(self.status.get("recording", False))
        previous_preview = bool(self.status.get("preview_enabled", False))
        if was_recording:
            self.stop_recording()
        if was_open:
            self.close_camera()
        self.profile["camera_id"] = camera_id
        self.status["camera_id"] = camera_id
        if not was_open:
            self._emit_status()
            return True
        if not self.open_camera():
            return False
        self.set_preview_enabled(previous_preview)
        if was_recording:
            return self.start_recording()
        return True

    def close_camera(self) -> None:
        try:
            self.module.close_camera()
        except Exception as exc:
            self.error.emit(str(exc))
        try:
            self.module.set_preview_enabled(False)
        except Exception:
            pass
        self.status["open"] = False
        self.status["recording"] = False
        self.status["preview_enabled"] = False
        self.status["recording_path"] = ""
        self._emit_status()

    def set_preview_enabled(self, enabled: bool) -> bool:
        requested = bool(enabled)
        if requested and not bool(self.status.get("open")):
            self.status["preview_enabled"] = False
            self._emit_status()
            self.error.emit("Camera preview cannot be enabled while camera is closed")
            return False
        ok = self.module.set_preview_enabled(requested)
        if ok is False:
            self.status["preview_enabled"] = False
            self._emit_status()
            self.error.emit(getattr(self.module, "last_error", "") or "Failed to update camera preview")
            return False
        self.status["preview_enabled"] = requested
        self._emit_status()
        return True

    def configure_charging_guard_detection(
        self,
        enabled: bool = False,
        config: Optional[Dict[str, Any]] = None,
        interval_ms: int = 10000,
    ) -> None:
        if hasattr(self.module, "configure_charging_guard_detection"):
            self.module.configure_charging_guard_detection(
                enabled=bool(enabled),
                config=dict(config or {}),
                interval_ms=int(interval_ms or 10000),
            )

    def get_charging_guard_detection(self, max_age_seconds: float = 30.0) -> Optional[Dict[str, Any]]:
        if not hasattr(self.module, "get_charging_guard_detection"):
            return None
        try:
            detection = self.module.get_charging_guard_detection(max_age_seconds=max_age_seconds)
        except Exception:
            return None
        return None if detection is None else dict(detection)

    def get_latest_frame(self, max_width: int = 0):
        frame = self.module.get_frame()
        if frame is None:
            return None
        preview = frame
        try:
            height, width = preview.shape[:2]
            if width > int(max_width) > 0:
                scale = float(max_width) / float(width)
                preview = cv2.resize(
                    preview,
                    (int(width * scale), int(height * scale)),
                    interpolation=cv2.INTER_AREA,
                )
        except Exception:
            pass
        return preview

    def get_preview_jpeg_bytes(self, max_width: int = 640, jpeg_quality: int = 80) -> Optional[bytes]:
        target_width = int(max_width or self.preview_max_width)
        target_quality = int(jpeg_quality or self.preview_jpeg_quality)
        if (
            target_width == int(self.preview_max_width)
            and target_quality == int(self.preview_jpeg_quality)
            and hasattr(self.module, "get_preview_jpeg_bytes")
        ):
            try:
                cached_payload = self.module.get_preview_jpeg_bytes()
            except Exception:
                cached_payload = None
            if cached_payload:
                return cached_payload
        frame = self.get_latest_frame(max_width=target_width)
        if frame is None:
            return None
        try:
            ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), int(target_quality)],
            )
            if not ok:
                return None
            return bytes(encoded)
        except Exception:
            return None

    def start_recording(self) -> bool:
        if not self.status["open"] and not self.open_camera():
            return False
        save_path = str(self.profile.get("video_save_path", "")).strip()
        if save_path:
            try:
                save_path = build_daily_file_path(save_path, datetime.now())
            except Exception:
                save_path = str(self.profile.get("video_save_path", "")).strip()
        ok = bool(self.module.start_recording(save_path or None))
        if not ok:
            self._emit_status()
            self.error.emit(self.module.last_error or "Failed to start camera recording")
            return False
        self.status["recording"] = True
        self.status["recording_path"] = str(self.module.current_recording_path or "")
        self._emit_status()
        return True

    def stop_recording(self) -> None:
        try:
            self.module.stop_recording()
        except Exception as exc:
            self.error.emit(str(exc))
        try:
            self.module.current_recording_path = None
            self.module.recording_base_path = None
        except Exception:
            pass
        self.status["recording"] = False
        self.status["recording_path"] = ""
        self._emit_status()

    def check_health(self, max_stale_seconds: float = 4.0) -> Tuple[bool, str]:
        if not self.status.get("open"):
            self._emit_status()
            return True, "camera closed"
        try:
            healthy, reason = self.module.check_health(max_stale_seconds=max_stale_seconds)
        except Exception as exc:
            healthy, reason = False, str(exc)
        if healthy:
            self.module.health_state = "ok"
        self._emit_status()
        return bool(healthy), str(reason or "")

    def recover_if_needed(self, reason: str = "", max_stale_seconds: float = 4.0) -> bool:
        if not self.status.get("open"):
            self._emit_status()
            return True
        reason_text = str(reason or "")
        if not reason_text:
            healthy, reason_text = self.check_health(max_stale_seconds=max_stale_seconds)
            if healthy:
                return True
        ok = bool(self.module.recover_if_needed(reason=reason_text))
        self.status["open"] = bool(self.module.is_camera_open)
        self.status["recording"] = bool(self.module.is_recording)
        self.status["recording_path"] = str(self.module.current_recording_path or "")
        if not ok:
            self.error.emit(self.module.last_error or f"Camera recovery failed: {reason_text}")
        self._emit_status()
        return ok
