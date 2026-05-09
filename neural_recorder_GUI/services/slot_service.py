import json
import multiprocessing as mp
import os
import pickle
import queue
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import cv2
from PyQt6.QtCore import QCoreApplication, QObject, QTimer
from PyQt6.QtWidgets import QApplication

try:
    from .controllers import (
        CameraController,
        HabitsController,
        NeuralController,
        RFController,
        imu_mode_label,
        normalize_imu_mode,
    )
    from .health_metrics import (
        BATTERY_LOW_THRESHOLD_PERCENT,
        ROLLUP_WINDOW_SECONDS,
        classify_battery_state,
        classify_packet_loss_state,
        compute_packet_loss_rollup,
        decode_bq25176_stat,
        detect_bq25176_charge_fault_blink,
    )
    from ..master_app.contracts import (
        build_default_slot_status,
        extract_mouse_id_from_directory,
        load_slot_profile,
        merge_slot_status,
    )
    from ..master_app.runtime_config import load_runtime_config
    from .mouse_platform_monitor import ChargingGuardState, MousePlatformDetector
    from ..storage.runtime_cache import SlotRuntimeCache, atomic_write_bytes, atomic_write_json
    from ..support.windows_runtime import apply_windows_process_role, configure_qt_runtime
except ImportError:
    from services.controllers import (
        CameraController,
        HabitsController,
        NeuralController,
        RFController,
        imu_mode_label,
        normalize_imu_mode,
    )
    from services.health_metrics import (
        BATTERY_LOW_THRESHOLD_PERCENT,
        ROLLUP_WINDOW_SECONDS,
        classify_battery_state,
        classify_packet_loss_state,
        compute_packet_loss_rollup,
        decode_bq25176_stat,
        detect_bq25176_charge_fault_blink,
    )
    from master_app.contracts import (
        build_default_slot_status,
        extract_mouse_id_from_directory,
        load_slot_profile,
        merge_slot_status,
    )
    from master_app.runtime_config import load_runtime_config
    from services.mouse_platform_monitor import ChargingGuardState, MousePlatformDetector
    from storage.runtime_cache import SlotRuntimeCache, atomic_write_bytes, atomic_write_json
    from support.windows_runtime import apply_windows_process_role, configure_qt_runtime


SYSTEM_LOG_MAX_ENTRIES = 200
SYSTEM_LOG_RETENTION_SECONDS = 24 * 3600
BATTERY_HISTORY_RETENTION_SECONDS = 24 * 3600
BATTERY_HISTORY_MAX_ENTRIES = 50000
HABITS_COMMAND_RETRY_LIMIT = 8
HABITS_COMMAND_RETRY_BASE_DELAY_MS = 150
MODE3_TRIGGER_PAUSE_THRESHOLD_PERCENT = 10
MODE3_TRIGGER_RESUME_THRESHOLD_PERCENT = 20
MODE3_TRIGGER_GUARD_INTERVAL_MS = 10000
NEURAL_READER_TIMEOUT_SUSPECT_WINDOW_SECONDS = 5.5
RF_TASK_TIMEOUT_SECONDS = {
    "connect": 3.0,
    "disconnect": 1.0,
    "query": 1.2,
    "power": 1.5,
}
RUNTIME_CACHE_FAILURE_LOG_THRESHOLD = 3
RUNTIME_CACHE_FAILURE_LOG_WINDOW_SECONDS = 30.0
GUI_INTERVAL_STREAM_KEYS = (
    "mode0_lfp",
    "mode1_raw",
    "mode2_raw",
    "mode3_lfp_esa",
    "mode3_raw",
)
GUI_INTERVAL_STREAM_FALLBACK_MODE = {
    "mode0_lfp": "mode0",
    "mode1_raw": "mode1",
    "mode2_raw": "mode2",
    "mode3_lfp_esa": "mode3",
    "mode3_raw": "mode3",
}
HISTORY_CACHE_LABELS = (
    "battery_history",
    "impedance_history",
    "habits_trial_data",
    "habits_event_data",
    "habits_state_data",
    "mode_switches",
    "system_log",
)
SAVE_MODE_FRONTEND_LABELS = {
    "lfp": "Mode0 LFP+MAND+Raw",
    "mode1": "single channel Spike",
    "mode2": "16 channels Spike",
    "mode3": "ESA&MUA",
}
HABITS_HISTORY_CACHE_LABELS = (
    "habits_trial_data",
    "habits_event_data",
    "habits_state_data",
    "mode_switches",
)


def _save_mode_frontend_label(mode_name: object) -> str:
    normalized = str(mode_name or "").strip().lower()
    return SAVE_MODE_FRONTEND_LABELS.get(normalized, normalized or "off")
LARGE_HISTORY_CACHE_LABELS = (
    "battery_history",
    "impedance_history",
    "habits_trial_data",
    "habits_event_data",
    "habits_state_data",
    "mode_switches",
)


def _timer_stagger_delay_ms(slot_id: str, timer_name: str, interval_ms: int, step_ms: int = 170) -> int:
    interval = max(1, int(interval_ms or 1))
    step = max(0, int(step_ms or 0))
    if interval <= 1 or step <= 0:
        return 0
    token = f"{slot_id}:{timer_name}"
    checksum = sum((index + 1) * ord(char) for index, char in enumerate(token))
    return int((checksum * step) % interval)


class DetailPayloadPublisher:
    def __init__(self, runtime_cache: SlotRuntimeCache):
        self.runtime_cache = runtime_cache
        self.sequence = 0
        self.last_metrics: Dict[str, Any] = {}
        self._payload_paths = []
        self._payload_queue = deque()

    def _payload_path(self, sequence: int) -> str:
        return os.path.join(
            self.runtime_cache.root_dir,
            f"detail_buffer_payload_{os.getpid()}_{int(sequence)}.bin",
        )

    def _prune_consumed_payloads(self) -> None:
        self._payload_paths = [
            path for path in self._payload_paths if os.path.exists(path)
        ]
        self._payload_queue = deque(
            entry for entry in self._payload_queue
            if os.path.exists(str(entry.get("payload_path", "")))
        )

    def publish(self, payload: object, stream_key: str = "") -> Dict[str, Any]:
        started = time.monotonic()
        raw = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
        serialized_at = time.monotonic()
        next_sequence = int(self.sequence) + 1
        payload_path = self._payload_path(next_sequence)
        atomic_write_bytes(payload_path, raw, durable=False)
        written_at = time.monotonic()
        self.sequence = next_sequence
        self._payload_paths.append(payload_path)
        entry = {
            "sequence": int(next_sequence),
            "timestamp_epoch": time.time(),
            "transport": "file",
            "payload_path": payload_path,
            "payload_size": int(len(raw)),
            "stream_key": str(stream_key or ""),
        }
        self._payload_queue.append(entry)
        self._prune_consumed_payloads()
        meta = {
            "attached": True,
            "sequence": int(self.sequence),
            "timestamp_epoch": entry["timestamp_epoch"],
            "transport": "file",
            "payload_path": payload_path,
            "payload_size": int(len(raw)),
            "stream_key": str(stream_key or ""),
            "payload_queue": list(self._payload_queue),
        }
        self.runtime_cache.write_detail_buffer_meta(meta)
        finished_at = time.monotonic()
        self.last_metrics = {
            "detail_ipc_payload_bytes": int(len(raw)),
            "detail_ipc_serialize_ms": max(0.0, (serialized_at - started) * 1000.0),
            "detail_ipc_write_ms": max(0.0, (written_at - serialized_at) * 1000.0),
            "detail_ipc_publish_ms": max(0.0, (finished_at - started) * 1000.0),
        }
        return meta

    def close(self) -> None:
        self.sequence += 1
        self.runtime_cache.write_detail_buffer_meta({
            "attached": False,
            "sequence": int(self.sequence),
            "timestamp_epoch": time.time(),
            "payload_size": 0,
            "transport": "file",
            "payload_path": self.runtime_cache.detail_buffer_payload_path,
            "payload_queue": [],
        })
        while self._payload_paths:
            old_path = self._payload_paths.pop(0)
            try:
                if os.path.exists(old_path):
                    os.remove(old_path)
            except OSError:
                pass


class SlotLocalChartWindow:
    """Process-backed detail chart handle.

    The detail window used to live inside SlotService and pyqtgraph rendering
    could block the slot Qt loop. This handle keeps the public methods used by
    SlotService while moving the actual widget into a child process.
    """

    def __init__(self, service):
        self.service = service
        self.publisher = DetailPayloadPublisher(service.runtime_cache)
        self.last_detail_render_metrics: Dict[str, Any] = {}
        self._closed = False
        self._process = None
        self._start_process()

    @staticmethod
    def _load_runner():
        try:
            from neural_recorder_GUI.master_app.detail_view import run_detail_window
        except ImportError:
            try:
                from ..master_app.detail_view import run_detail_window
            except ImportError:
                from master_app.detail_view import run_detail_window
        return run_detail_window

    def _slot_payload(self) -> Dict[str, Any]:
        return {
            "slot_id": self.service.slot_id,
            "label": self.service.label,
            "profile_path": self.service.profile_path,
        }

    def _runtime_root(self) -> str:
        return os.path.dirname(self.service.runtime_cache.root_dir)

    def _start_process(self) -> None:
        runner = self._load_runner()
        ctx = mp.get_context("spawn")
        self._process = ctx.Process(
            target=runner,
            args=(self._slot_payload(), self._runtime_root(), self.service.command_queue),
            daemon=False,
        )
        self._process.start()

    def is_alive(self) -> bool:
        process = self._process
        try:
            return bool(process is not None and process.is_alive())
        except Exception:
            return False

    def show(self):
        if self._closed:
            return None
        if not self.is_alive():
            self._start_process()
        return None

    def raise_(self):
        return None

    def activateWindow(self):
        return None

    def update_slot_status(self, _status):
        return None

    def handle_detail_payload(self, payload):
        stream_key = SlotService._detail_payload_stream_key(payload)
        meta = self.publisher.publish(payload, stream_key=stream_key)
        self.last_detail_render_metrics = dict(self.publisher.last_metrics)
        return meta

    def close_from_service(self):
        self._closed = True
        try:
            self.publisher.close()
        except Exception:
            pass
        process = self._process
        if process is None:
            return None
        try:
            if process.is_alive():
                process.terminate()
        except Exception:
            pass
        try:
            process.join(timeout=1.0)
        except Exception:
            pass
        return None


def _default_charging_guard_config() -> Dict[str, Any]:
    return {
        "enabled": False,
        "roi_norm": [0.1, 0.5, 0.3, 0.2],
        "dark_threshold": 90,
        "day_dark_threshold": 90,
        "night_dark_threshold": 90,
        "scene_brightness_threshold": 80,
        "min_area_ratio": 0.30,
        "check_interval_ms": 10000,
        "required_hits": 6,
        "warning_duration_ms": 3000,
        "fan_enabled": True,
    }


def _coerce_int(value: Any, fallback: int, minimum: int, maximum: int) -> int:
    try:
        coerced = int(value)
    except Exception:
        coerced = int(fallback)
    return int(max(minimum, min(maximum, coerced)))


def _normalize_charging_guard_config(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    merged = _default_charging_guard_config()
    if isinstance(config, dict):
        merged.update(dict(config))

    roi_values = [float(value) for value in list(merged.get("roi_norm", [0.1, 0.5, 0.3, 0.2]))[:4]]
    if len(roi_values) != 4:
        raise ValueError("roi_norm must contain 4 numbers")

    legacy_threshold = _coerce_int(merged.get("dark_threshold", 90), 90, 0, 255)
    day_threshold = _coerce_int(merged.get("day_dark_threshold", legacy_threshold), legacy_threshold, 0, 255)
    night_threshold = _coerce_int(merged.get("night_dark_threshold", legacy_threshold), legacy_threshold, 0, 255)
    scene_threshold = _coerce_int(merged.get("scene_brightness_threshold", 80), 80, 0, 255)

    merged["roi_norm"] = roi_values
    merged["enabled"] = bool(merged.get("enabled", False))
    merged["dark_threshold"] = day_threshold
    merged["day_dark_threshold"] = day_threshold
    merged["night_dark_threshold"] = night_threshold
    merged["scene_brightness_threshold"] = scene_threshold
    merged["min_area_ratio"] = max(0.0, min(1.0, float(merged.get("min_area_ratio", 0.30) or 0.30)))
    merged["check_interval_ms"] = max(1000, int(merged.get("check_interval_ms", 10000) or 10000))
    merged["required_hits"] = max(1, int(merged.get("required_hits", 6) or 6))
    merged["warning_duration_ms"] = max(0, int(merged.get("warning_duration_ms", 3000) or 3000))
    merged["fan_enabled"] = bool(merged.get("fan_enabled", True))
    return merged


class CameraPreviewPublisher:
    def __init__(self, runtime_cache: SlotRuntimeCache):
        self.runtime_cache = runtime_cache
        self.sequence = 0
        self._last_enabled = False

    def publish(
        self,
        payload: Optional[bytes],
        enabled: bool,
        camera_id: Optional[int],
        preview_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        enabled = bool(enabled and payload)
        if not enabled:
            if not self._last_enabled:
                return {"payload_size": 0, "published": False}
            self._last_enabled = False
            disabled_meta = {
                "enabled": False,
                "sequence": self.sequence,
                "timestamp_epoch": time.time(),
                "camera_id": camera_id,
            }
            if isinstance(preview_meta, dict):
                disabled_meta.update(dict(preview_meta))
            self.runtime_cache.write_camera_preview_meta(disabled_meta)
            return {"payload_size": 0, "published": True}
        self.sequence += 1
        self.runtime_cache.write_camera_preview_frame(payload or b"")
        meta_payload = {
            "enabled": True,
            "sequence": self.sequence,
            "timestamp_epoch": time.time(),
            "camera_id": camera_id,
            "frame_path": self.runtime_cache.camera_preview_frame_path,
            "payload_size": len(payload or b""),
        }
        if isinstance(preview_meta, dict):
            meta_payload.update(dict(preview_meta))
        self.runtime_cache.write_camera_preview_meta(meta_payload)
        self._last_enabled = True
        return {"payload_size": int(len(payload or b"")), "published": True}


class SlotService(QObject):
    def __init__(self, slot_payload: Dict[str, Any], runtime_root: str, command_queue, priority_command_queue=None):
        super().__init__()
        self.slot_payload = dict(slot_payload)
        self.command_queue = command_queue
        self.priority_command_queue = priority_command_queue
        self.slot_id = str(self.slot_payload["slot_id"])
        self.label = str(self.slot_payload["label"])
        self.profile_path = str(self.slot_payload["profile_path"])
        self.runtime_cache = SlotRuntimeCache(runtime_root, self.slot_id)
        self.runtime_cache.clear_transient_files()
        self.camera_preview_publisher = CameraPreviewPublisher(self.runtime_cache)
        self.runtime_config = load_runtime_config(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.slot_runtime_config = dict(self.runtime_config.get("slot_service", {}))
        apply_windows_process_role("slot_service", active_recording=False)
        self.status = build_default_slot_status(self.slot_id, self.label)
        self.status["profile_loaded"] = False
        self.status["state"] = "initializing"
        self.status["service_pid"] = int(os.getpid())
        self.profile = load_slot_profile(self.profile_path)
        self._cached_runtime_profile = self.runtime_cache.read_last_profile({})
        self._runtime_cache_mouse_matches = self._runtime_cache_matches_current_mouse(
            self._cached_runtime_profile,
            self.profile,
        )
        self.status["profile_loaded"] = True
        self.status["state"] = "ready"
        self.status["last_update_epoch"] = time.time()
        self.status["rf"]["recommended_esb_channel"] = self.profile.get("recommended_esb_channel")
        self.battery_capacity_mAh = self._profile_battery_capacity_mAh(self.profile)
        self.status["battery"]["capacity_mAh"] = float(self.battery_capacity_mAh)
        self.battery_history = deque(maxlen=BATTERY_HISTORY_MAX_ENTRIES)
        self._battery_stat_window = deque(maxlen=16)
        self.packet_loss_history = deque(maxlen=7200)
        self._last_packet_metrics_seq = -1
        self.system_log = deque(maxlen=SYSTEM_LOG_MAX_ENTRIES)
        self._last_battery_snapshot = None
        self._last_battery_latest_payload = ""
        self._last_battery_latest_flush_monotonic = 0.0
        self._last_habits_live_payload = ""
        self._last_habits_live_flush_monotonic = 0.0
        self._last_habits_live_sequence = 0
        self._last_local_chart_habits_live_sequence = 0
        self._last_error = ""
        self._last_cache_flush_warning = ""
        self._runtime_cache_failure_counts: Dict[str, int] = {}
        self._runtime_cache_failure_first_epoch: Dict[str, float] = {}
        self._runtime_cache_logged_failure_labels = set()
        self._last_state_reason = ""
        self._last_priority_flush_monotonic = 0.0
        self._last_detail_publish_monotonic = 0.0
        self._last_detail_publish_monotonic_by_stream: Dict[str, float] = {}
        self._pending_detail_payload_by_stream: Dict[str, object] = {}
        self._detail_render_round_robin_index = 0
        self._detail_attach_generation = 0
        self._detail_attach_warmup_ms = max(
            0,
            int(self.slot_runtime_config.get("detail_attach_warmup_ms", 3000) or 0),
        )
        self._detail_render_max_frames_per_tick = max(
            1,
            int(self.slot_runtime_config.get("local_chart_render_max_frames_per_tick", 1) or 1),
        )
        self._detail_render_timer_ms = max(
            1,
            int(self.slot_runtime_config.get("local_chart_render_timer_ms", 16) or 16),
        )
        self.local_chart_window = None
        self._local_chart_closing = False
        self._status_dirty = True
        self._history_dirty = False
        self._history_dirty_generation = 0
        self._history_dirty_labels = set()
        self._history_flush_cursor = 0
        self._history_flush_cycle_generation = 0
        self._last_performance_warning_epoch: Dict[str, float] = {}
        self._last_gui_interval_profile_payload = ""
        self._last_gui_interval_profile_persist_monotonic = 0.0
        self._last_detail_display_profile_payload = ""
        self._last_detail_display_profile_persist_monotonic = 0.0
        self._camera_recording_follow_save = False
        self._camera_recording_follow_save_mode = ""
        self._camera_recording_rotation_in_progress = False
        self._video_segment_start_monotonic = 0.0
        self._video_segment_max_seconds = max(
            0.0,
            float(self.slot_runtime_config.get("video_segment_max_seconds", 60 * 60) or 0.0),
        )
        self._rf_task_queue = queue.Queue()
        self._rf_worker_stop = threading.Event()
        self._rf_worker_busy = False
        self._rf_worker_current_action = ""
        self._rf_worker_current_visible = False
        self._habits_mode_switch_in_flight = False
        self._last_habits_esa_monotonic = 0.0
        self._last_habits_trial_started_monotonic = 0.0
        self._last_low_battery_finalize_counter = None
        self._low_battery_rf_manual_off = False
        self._last_low_battery_rf_hold_monotonic = 0.0
        self._shutdown_requested = False
        self.mode3_trial_trigger_pause_threshold = int(
            self.profile.get("mode3_battery_pause_threshold", MODE3_TRIGGER_PAUSE_THRESHOLD_PERCENT)
            or MODE3_TRIGGER_PAUSE_THRESHOLD_PERCENT
        )
        self.mode3_trial_trigger_resume_threshold = int(
            self.profile.get("mode3_battery_resume_threshold", MODE3_TRIGGER_RESUME_THRESHOLD_PERCENT)
            or MODE3_TRIGGER_RESUME_THRESHOLD_PERCENT
        )
        self.mode3_trial_trigger_guard_interval_ms = MODE3_TRIGGER_GUARD_INTERVAL_MS
        self.charging_guard_config = _default_charging_guard_config()
        profile_guard = self.profile.get("charging_guard", {})
        if isinstance(profile_guard, dict):
            self.charging_guard_config.update(dict(profile_guard))
            if "dark_threshold" in profile_guard:
                if "day_dark_threshold" not in profile_guard:
                    self.charging_guard_config["day_dark_threshold"] = profile_guard.get("dark_threshold")
                if "night_dark_threshold" not in profile_guard:
                    self.charging_guard_config["night_dark_threshold"] = profile_guard.get("dark_threshold")
        self.charging_guard_config = _normalize_charging_guard_config(self.charging_guard_config)
        self.charging_guard_detector = MousePlatformDetector(
            roi_norm=self.charging_guard_config.get("roi_norm", [0.1, 0.5, 0.3, 0.2]),
            dark_threshold=self.charging_guard_config.get("dark_threshold", 90),
            day_dark_threshold=self.charging_guard_config.get("day_dark_threshold", 90),
            night_dark_threshold=self.charging_guard_config.get("night_dark_threshold", 90),
            scene_brightness_threshold=self.charging_guard_config.get("scene_brightness_threshold", 80),
            min_area_ratio=self.charging_guard_config.get("min_area_ratio", 0.30),
        )
        self.charging_guard_state = ChargingGuardState(
            required_hits=int(self.charging_guard_config.get("required_hits", 6) or 6)
        )
        self.charging_guard_last_detection = {
            "mouse_present": False,
            "area_ratio": 0.0,
            "roi": (0, 0, 0, 0),
            "reason": "not checked",
            "lighting_state": "unknown",
            "scene_brightness": 0.0,
            "active_dark_threshold": int(self.charging_guard_config.get("dark_threshold", 90) or 90),
        }
        self._charging_guard_last_preview_detection_monotonic = 0.0
        self._pending_neural_packet_gap_log = {
            "events": 0,
            "missing": 0,
            "summary": "",
        }
        self._last_neural_packet_gap_log_epoch = 0.0
        self._neural_gap_log_interval_s = 60.0
        self._slot_task_trace = deque(
            maxlen=max(10, int(self.slot_runtime_config.get("slot_scheduler_trace_max_entries", 80) or 80))
        )
        self._slot_task_trace_seq = 0
        self._slot_task_trace_min_duration_ms = max(
            0.0,
            float(self.slot_runtime_config.get("slot_scheduler_trace_min_duration_ms", 1) or 0),
        )
        self._command_poll_max_commands_per_tick = max(
            1,
            int(self.slot_runtime_config.get("command_poll_max_commands_per_tick", 8) or 8),
        )
        self._command_poll_budget_ms = max(
            0.0,
            float(self.slot_runtime_config.get("command_poll_budget_ms", 4.0) or 0.0),
        )
        self._command_poll_scan_limit = max(
            self._command_poll_max_commands_per_tick,
            int(self.slot_runtime_config.get("command_poll_scan_limit", 64) or 64),
        )
        self._last_command_poll_summary = ""

        self.profile["mode3_thresholds_fallback_path"] = self.runtime_cache.mode3_thresholds_path
        self.neural = NeuralController(self.profile)
        self.rf = RFController(self.profile)
        self.habits = HabitsController(self.profile)
        self.camera = CameraController(self.profile)
        if hasattr(self.neural, "configure_mode0_power_estimator"):
            try:
                self.neural.configure_mode0_power_estimator(
                    self.runtime_config.get("mode0_power_estimator", {})
                )
            except Exception:
                pass
        if hasattr(self.neural, "configure_detail_stream"):
            try:
                self.neural.configure_detail_stream(
                    min_interval_ms=float(self.slot_runtime_config.get("detail_publish_min_interval_ms", 0) or 0)
                )
            except Exception:
                pass
        if hasattr(self.neural, "configure_save_chunks"):
            try:
                self.neural.configure_save_chunks(
                    mode0=int(self.slot_runtime_config.get("save_chunk_packets_mode0", 100) or 100),
                    mode3=int(self.slot_runtime_config.get("save_chunk_packets_mode3", 100) or 100),
                    mode3_raw=int(self.slot_runtime_config.get("save_chunk_packets_mode3_raw", 100) or 100),
                )
            except Exception:
                pass
        if hasattr(self.camera, "configure_preview_stream"):
            try:
                self.camera.configure_preview_stream(
                    max_width=int(self.slot_runtime_config.get("camera_preview_max_width", 640)),
                    jpeg_quality=int(self.slot_runtime_config.get("camera_preview_jpeg_quality", 72)),
                    preview_background_rate=float(self.slot_runtime_config.get("camera_preview_hidden_fps", self.slot_runtime_config.get("camera_background_preview_fps", 1)) or 1),
                    preview_active_rate=float(self.slot_runtime_config.get("camera_preview_active_fps", 8) or 8),
                    preview_recording_rate=float(self.slot_runtime_config.get("camera_preview_recording_fps", 5) or 5),
                )
            except Exception:
                pass
        self._sync_camera_charging_guard_config()
        self.status["neural"] = merge_slot_status(self.status["neural"], self.neural.status)
        self.status["rf"] = merge_slot_status(self.status["rf"], self.rf.status)
        self.status["habits"] = merge_slot_status(self.status["habits"], self.habits.status)
        self.status["camera"] = merge_slot_status(self.status["camera"], self.camera.status)
        if not bool(self._runtime_cache_mouse_matches):
            cached_mouse = self._runtime_cache_mouse_id(self._cached_runtime_profile)
            current_mouse = self._runtime_cache_mouse_id(self.profile)
            self.runtime_cache.clear_mouse_scoped_history()
            self._append_system_log(
                "Runtime cache reset for mouse change "
                f"({cached_mouse or 'unknown'} -> {current_mouse or 'unknown'})",
                level="info",
            )
        self._restore_cached_state()
        self.runtime_cache.write_last_profile(self.profile)
        self.status["neural"] = merge_slot_status(self.status["neural"], self.neural.status)
        self.status["habits"] = merge_slot_status(self.status["habits"], self.habits.status)
        self._sync_mode3_trial_trigger_status()

        self.neural.status_changed.connect(self._on_neural_status)
        self.neural.detail_payload_ready.connect(self._on_detail_payload)
        if hasattr(self.neural, "video_rollover_requested"):
            self.neural.video_rollover_requested.connect(self._on_neural_video_rollover_requested)
        self.neural.error.connect(self._on_error)
        self.rf.status_changed.connect(self._on_rf_status)
        self.rf.error.connect(self._on_error)
        self.habits.status_changed.connect(self._on_habits_status)
        self.habits.error.connect(self._on_error)
        self.habits.mode_switch_requested.connect(self._on_habits_mode_switch)
        self.habits.trial_started.connect(self._on_habits_trial_started)
        self.camera.status_changed.connect(self._on_camera_status)
        self.camera.error.connect(self._on_error)

        self.detail_render_timer = QTimer(self)
        self.detail_render_timer.timeout.connect(
            lambda: self._run_timed_task("detail_render", self._drain_detail_payloads_to_chart)
        )
        self.detail_render_timer.setInterval(int(self._detail_render_timer_ms))

        self.command_timer = QTimer(self)
        self.command_timer.timeout.connect(
            lambda: self._run_timed_task("command_poll", self._drain_commands)
        )
        self.command_timer.start(int(self.slot_runtime_config.get("command_poll_ms", 25)))

        self.publish_timer = QTimer(self)
        self.publish_timer.timeout.connect(
            lambda: self._run_timed_task("runtime_cache", self._flush_status_cache)
        )
        self._start_staggered_timer(
            self.publish_timer,
            "runtime_cache",
            int(self.slot_runtime_config.get("runtime_cache_flush_ms", 1000)),
        )

        self.history_timer = QTimer(self)
        self.history_timer.timeout.connect(
            lambda: self._run_timed_task("history", self._flush_history_cache)
        )
        self._start_staggered_timer(
            self.history_timer,
            "history",
            int(self.slot_runtime_config.get("history_flush_ms", 5000)),
        )

        self.rollup_timer = QTimer(self)
        self.rollup_timer.timeout.connect(
            lambda: self._run_timed_task("health_rollup", self._refresh_health_rollups)
        )
        self._start_staggered_timer(
            self.rollup_timer,
            "health_rollup",
            int(self.slot_runtime_config.get("health_rollup_ms", 10000)),
        )

        self.health_timer = QTimer(self)
        self.health_timer.timeout.connect(
            lambda: self._run_timed_task("health_check", self._check_health)
        )
        self._start_staggered_timer(
            self.health_timer,
            "health_check",
            int(self.slot_runtime_config.get("health_check_ms", 1500)),
        )

        self.camera_preview_timer = QTimer(self)
        self.camera_preview_timer.timeout.connect(
            lambda: self._run_timed_task("camera_preview", self._publish_camera_preview)
        )
        self._start_staggered_timer(
            self.camera_preview_timer,
            "camera_preview",
            int(self.slot_runtime_config.get("camera_preview_publish_ms", 180)),
        )

        self.charging_guard_timer = QTimer(self)
        self.charging_guard_timer.timeout.connect(
            lambda: self._run_timed_task("charging_guard", self._poll_charging_guard)
        )
        self._start_staggered_timer(
            self.charging_guard_timer,
            "charging_guard",
            max(1000, int(self.charging_guard_config.get("check_interval_ms", 10000) or 10000)),
        )

        self.mode3_trial_trigger_guard_timer = QTimer(self)
        self.mode3_trial_trigger_guard_timer.timeout.connect(
            lambda: self._run_timed_task("mode3_trial_trigger_guard", self._poll_mode3_trial_trigger_gate)
        )
        self._start_staggered_timer(
            self.mode3_trial_trigger_guard_timer,
            "mode3_trial_trigger_guard",
            max(1000, int(self.mode3_trial_trigger_guard_interval_ms or MODE3_TRIGGER_GUARD_INTERVAL_MS)),
        )
        self._rf_worker_thread = threading.Thread(
            target=self._rf_worker_loop,
            name=f"slot-rf-worker-{self.slot_id}",
            daemon=True,
        )
        self._rf_worker_thread.start()

        self._sync_charging_guard_status()
        self._append_system_log("Slot service initialized", level="info")
        self._refresh_health_rollups()
        self._flush_runtime_cache()

    def _set_rf_command_busy(self, busy: bool, action: str = "") -> None:
        rf_status = dict(self.status.get("rf", {}) or {})
        rf_status["command_busy"] = bool(busy)
        rf_status["command_action"] = str(action or "")
        if busy and str(action or "").strip().lower() == "connect" and not bool(rf_status.get("connected", False)):
            rf_status["connection_state"] = "connecting"
        elif not busy and str(rf_status.get("connection_state", "") or "").strip().lower() in {
            "connecting",
            "disconnecting",
            "busy",
        }:
            rf_status["connection_state"] = "connected" if bool(rf_status.get("connected", False)) else "idle"
        self.status["rf"] = merge_slot_status(self.status.get("rf", {}), rf_status)
        self._mark_updated()
        self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _set_neural_command_busy(self, busy: bool, action: str = "") -> None:
        action_text = str(action or "").strip().lower()
        neural_status = dict(self.status.get("neural", {}) or {})
        neural_status["command_busy"] = bool(busy)
        neural_status["command_action"] = action_text if busy else ""
        if busy:
            neural_status["connection_state"] = f"{action_text}ing" if action_text else "busy"
        elif bool(neural_status.get("connected", False)):
            neural_status["connection_state"] = "connected"
        else:
            neural_status["connection_state"] = "idle"
        self.status["neural"] = merge_slot_status(self.status.get("neural", {}), neural_status)
        try:
            self.neural.status["command_busy"] = bool(busy)
            self.neural.status["command_action"] = action_text if busy else ""
            self.neural.status["connection_state"] = neural_status["connection_state"]
        except Exception:
            pass
        self._refresh_ui_busy_states()
        self._mark_updated()
        self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _submit_rf_task(self, action: str, func: Callable[[], Any], visible: bool = True) -> None:
        normalized_action = str(action or "").strip().lower()
        if visible:
            self._set_rf_command_busy(True, normalized_action)
        self._rf_task_queue.put((normalized_action, func, bool(visible)))

    def _next_visible_rf_action(self) -> str:
        try:
            queued_items = list(self._rf_task_queue.queue)
        except Exception:
            queued_items = []
        for item in queued_items:
            try:
                action = str(item[0] or "")
                visible = bool(item[2]) if len(item) >= 3 else True
            except Exception:
                continue
            if visible:
                return action
        return ""

    def _rf_task_timeout_seconds(self, action: str) -> float:
        action_text = str(action or "").strip().lower()
        if action_text.startswith("power"):
            action_text = "power"
        try:
            return max(0.1, float(RF_TASK_TIMEOUT_SECONDS.get(action_text, 1.5)))
        except Exception:
            return 1.5

    def _run_rf_task_with_timeout(self, action: str, func: Callable[[], Any]) -> bool:
        errors = []

        def task_runner() -> None:
            try:
                func()
            except Exception as exc:
                errors.append(exc)

        task_thread = threading.Thread(
            target=task_runner,
            name=f"slot-rf-{self.slot_id}-{str(action or 'task')}",
            daemon=True,
        )
        task_thread.start()
        task_thread.join(timeout=self._rf_task_timeout_seconds(action))
        if task_thread.is_alive():
            message = f"RF {str(action or 'command')} timed out"
            self._append_system_log(message, level="warning")
            rf_status = dict(self.status.get("rf", {}) or {})
            rf_status["last_failure_reason"] = message
            if str(action or "").strip().lower() == "connect":
                rf_status["connection_state"] = "failed"
            self.status["rf"] = merge_slot_status(self.status.get("rf", {}), rf_status)
            self._mark_updated()
            self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return False
        if errors:
            raise errors[0]
        return True

    def _rf_worker_loop(self) -> None:
        while not self._rf_worker_stop.is_set():
            try:
                item = self._rf_task_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                action, func, visible = item
            except ValueError:
                action, func = item
                visible = True
            self._rf_worker_busy = True
            self._rf_worker_current_action = str(action or "")
            self._rf_worker_current_visible = bool(visible)
            task_started_epoch = time.time()
            task_started_mono = time.monotonic()
            try:
                self._run_rf_task_with_timeout(self._rf_worker_current_action, func)
            except Exception as exc:
                self._append_system_log(
                    f"RF {self._rf_worker_current_action or 'command'} failed: {exc}",
                    level="error",
                )
            finally:
                task_name = f"rf_{self._rf_worker_current_action or 'task'}_worker"
                self._record_slot_task_trace(
                    task_name,
                    start_epoch=task_started_epoch,
                    end_epoch=time.time(),
                    duration_ms=max(0.0, (time.monotonic() - task_started_mono) * 1000.0),
                )
                self._rf_worker_busy = False
                self._rf_worker_current_action = ""
                self._rf_worker_current_visible = False
                try:
                    next_action = self._next_visible_rf_action()
                    if next_action:
                        self._set_rf_command_busy(True, next_action)
                    elif visible or bool(self.status.get("rf", {}).get("command_busy", False)):
                        self._set_rf_command_busy(False, "")
                except Exception:
                    if visible:
                        self._set_rf_command_busy(False, "")

    def _rf_connect_task(self) -> None:
        self.rf.connect_port()

    def _rf_disconnect_task(self) -> None:
        self.rf.disconnect_port()

    def _rf_power_task(self, enabled: bool) -> None:
        if self.rf.connection is None:
            self.rf.connect_port()
        self.rf.set_power(bool(enabled))

    def _rf_query_task(self) -> None:
        if self.rf.connection is None:
            self.rf.connect_port()
        self.rf.query_status()

    def _save_mode_requires_camera_recording(self, mode_name: str, enabled: bool) -> bool:
        normalized_mode = str(mode_name or "").strip().lower()
        return bool(enabled) and normalized_mode in {"lfp", "mode2", "mode3"}

    def _start_camera_recording_for_save(self, mode_name: str) -> bool:
        normalized_mode = str(mode_name or "").strip().lower()
        camera_status = self.status.get("camera", {})
        if bool(camera_status.get("recording", False)):
            if self._camera_recording_follow_save:
                self._camera_recording_follow_save_mode = normalized_mode
            if float(getattr(self, "_video_segment_start_monotonic", 0.0) or 0.0) <= 0.0:
                self._video_segment_start_monotonic = time.monotonic()
            return True
        ok = bool(self.camera.start_recording())
        if ok:
            self._video_segment_start_monotonic = time.monotonic()
            self._camera_recording_follow_save = True
            self._camera_recording_follow_save_mode = normalized_mode
            self._append_system_log(
                f"Camera recording auto-started for {_save_mode_frontend_label(normalized_mode)} save",
                level="info",
            )
            return True
        self._append_system_log(
            f"Camera recording auto-start failed for {_save_mode_frontend_label(normalized_mode)} save",
            level="warning",
        )
        return False

    def _stop_camera_recording_follow_save(self, reason: str) -> None:
        was_following = bool(self._camera_recording_follow_save)
        self._camera_recording_follow_save = False
        self._camera_recording_follow_save_mode = ""
        if not was_following:
            return
        if bool(self.status.get("camera", {}).get("recording", False)):
            self.camera.stop_recording()
            self._video_segment_start_monotonic = 0.0
            self._append_system_log(
                f"Camera recording auto-stopped ({str(reason or 'save sync')})",
                level="info",
            )

    def _sync_camera_recording_with_save(self, mode_name: str, enabled: bool) -> None:
        if self._save_mode_requires_camera_recording(mode_name, enabled):
            self._start_camera_recording_for_save(mode_name)
            return
        self._stop_camera_recording_follow_save(
            f"save {'enabled' if enabled else 'disabled'} -> {_save_mode_frontend_label(mode_name)}"
        )

    def _rotate_camera_recording_segment(self, reason: str) -> bool:
        if bool(getattr(self, "_camera_recording_rotation_in_progress", False)):
            return False
        if not bool(self.status.get("camera", {}).get("recording", False)):
            self._video_segment_start_monotonic = 0.0
            return False
        self._camera_recording_rotation_in_progress = True
        follow_save = bool(self._camera_recording_follow_save)
        follow_mode = str(self._camera_recording_follow_save_mode or "")
        try:
            self.camera.stop_recording()
            ok = bool(self.camera.start_recording())
            if ok:
                self._camera_recording_follow_save = follow_save
                self._camera_recording_follow_save_mode = follow_mode
                self._video_segment_start_monotonic = time.monotonic()
                self._append_system_log(
                    f"Camera recording segment rotated ({str(reason or 'segment')})",
                    level="info",
                )
                return True
            self._video_segment_start_monotonic = 0.0
            self._append_system_log(
                f"Camera recording segment rotate failed ({str(reason or 'segment')})",
                level="warning",
            )
            return False
        finally:
            self._camera_recording_rotation_in_progress = False

    def _on_neural_video_rollover_requested(self) -> None:
        camera_recording = bool(self.status.get("camera", {}).get("recording", False))
        if camera_recording:
            self._rotate_camera_recording_segment("neural file rollover")
            return
        save_mode = str(self.status.get("neural", {}).get("save_mode", "") or "").strip().lower()
        if self._save_mode_requires_camera_recording(save_mode, True):
            self._start_camera_recording_for_save(save_mode)

    def _poll_video_segment_rotation(self) -> None:
        max_seconds = float(getattr(self, "_video_segment_max_seconds", 0.0) or 0.0)
        if max_seconds <= 0.0:
            return
        if not bool(self.status.get("camera", {}).get("recording", False)):
            self._video_segment_start_monotonic = 0.0
            return
        start_mono = float(getattr(self, "_video_segment_start_monotonic", 0.0) or 0.0)
        if start_mono <= 0.0:
            self._video_segment_start_monotonic = time.monotonic()
            return
        if (time.monotonic() - start_mono) >= max_seconds:
            self._rotate_camera_recording_segment("time segment")

    @staticmethod
    def _profile_battery_capacity_mAh(profile: Optional[Dict[str, Any]]) -> float:
        profile = profile if isinstance(profile, dict) else {}
        for key in ("battery_capacity_mAh", "battery_capacity_mah", "battery_capacity"):
            try:
                value = float(profile.get(key, 0.0) or 0.0)
            except Exception:
                value = 0.0
            if value > 0.0:
                return value
        return 24.0

    def _sync_bq25176_status(self) -> None:
        battery = self.status.get("battery", {})
        if not isinstance(battery, dict):
            return
        battery["capacity_mAh"] = float(getattr(self, "battery_capacity_mAh", 24.0) or 24.0)
        decoded = decode_bq25176_stat(battery.get("stat"))
        now_epoch = time.time()
        if decoded is None:
            battery.update({
                "bq25176_valid": False,
                "bq25176_status_text": "BQ25176 status unknown",
                "bq25176_fault_possible": False,
                "bq25176_fault_reason": "",
            })
            self.status["battery"] = battery
            return
        self._battery_stat_window.append({
            "timestamp_epoch": now_epoch,
            "stat": int(decoded.raw_stat),
        })
        recent_stats = [
            item.get("stat")
            for item in list(self._battery_stat_window)
            if now_epoch - float(item.get("timestamp_epoch", 0.0) or 0.0) <= 20.0
        ]
        fault_possible = detect_bq25176_charge_fault_blink(recent_stats)
        charge_text = str(decoded.charge_state_text)
        charge_level = str(decoded.charge_state_level)
        status_text = str(decoded.status_text)
        fault_reason = ""
        if fault_possible:
            charge_text = "Possible charge fault"
            charge_level = "danger"
            status_text = "VIN good, STAT pin blink pattern suggests charger fault"
            fault_reason = "STAT toggled repeatedly while PG stayed power-good"
        battery.update({
            "bq25176_valid": True,
            "bq25176_raw_stat": int(decoded.raw_stat),
            "bq25176_pg_raw": int(decoded.pg_raw),
            "bq25176_stat_raw": int(decoded.stat_raw),
            "bq25176_power_good": bool(decoded.power_good),
            "bq25176_charging": bool(decoded.stat_reports_charging),
            "bq25176_power_state_text": str(decoded.power_state_text),
            "bq25176_power_state_level": str(decoded.power_state_level),
            "bq25176_charge_state_text": charge_text,
            "bq25176_charge_state_level": charge_level,
            "bq25176_status_text": status_text,
            "bq25176_fault_possible": bool(fault_possible),
            "bq25176_fault_reason": fault_reason,
        })
        self.status["battery"] = battery

    @staticmethod
    def _power_guard_low_battery_active(power_guard: Any) -> bool:
        if not isinstance(power_guard, dict):
            return False
        state_name = str(power_guard.get("state_name", "") or "").strip().lower()
        return bool(
            power_guard.get("low_battery_hold", False)
            or power_guard.get("reboot_pending", False)
            or state_name in {"low_vbat_hold", "reboot_pending"}
        )

    def _active_neural_save_modes_from_status(self) -> list:
        neural_status = dict(self.status.get("neural", {}) or {})
        save_flags = dict(neural_status.get("save_flags", {}) or {})
        modes = [
            mode_name
            for mode_name in ("lfp", "mode1", "mode2", "mode3")
            if bool(save_flags.get(mode_name, False))
        ]
        if modes:
            return modes
        save_mode = str(neural_status.get("save_mode", "") or "").strip()
        return [save_mode] if save_mode else []

    def _handle_low_battery_power_guard(self) -> None:
        neural_status = dict(self.status.get("neural", {}) or {})
        power_guard = dict(neural_status.get("power_guard", {}) or {})
        if not self._power_guard_low_battery_active(power_guard):
            return
        try:
            low_stop_counter = int(power_guard.get("low_stop_counter", 0) or 0)
        except Exception:
            low_stop_counter = 0
        if low_stop_counter <= 0:
            return
        if self._last_low_battery_finalize_counter == low_stop_counter:
            return

        self._last_low_battery_finalize_counter = low_stop_counter
        modes = self._active_neural_save_modes_from_status()
        finalized = []
        if hasattr(self.neural, "finalize_save_buffers"):
            finalized = list(self.neural.finalize_save_buffers(modes or None) or [])
        self.status["neural"] = merge_slot_status(
            self.status["neural"],
            {
                "low_battery_finalize_counter": low_stop_counter,
                "low_battery_finalized_modes": list(finalized),
            },
        )
        battery = dict(self.status.get("battery", {}) or {})
        mode_text = ", ".join(modes) if modes else "no active save mode"
        finalized_text = ", ".join(finalized) if finalized else "no pending buffers"
        self._append_system_log(
            "Firmware low-battery stop "
            f"#{low_stop_counter}: finalized {finalized_text} ({mode_text}); "
            f"RSOC {float(battery.get('reported_rsoc', battery.get('rsoc', 0.0)) or 0.0):.0f}% "
            f"VBAT {float(battery.get('voltage', 0.0) or 0.0):.0f} mV",
            level="warning",
        )
        self._flush_runtime_cache(include_battery_history=True)

    def _sync_mode3_trial_trigger_status(
        self,
        reason_text: Optional[str] = None,
        refresh_rf_status: bool = False,
    ) -> None:
        habits_status = dict(self.status.get("habits", {}) or {})
        battery = dict(self.status.get("battery", {}) or {})
        reported_rsoc = float(battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0)
        rf_on = self._charging_guard_rf_is_on(query_device=refresh_rf_status)
        paused = bool(habits_status.get("mode3_trial_trigger_paused", False))
        target_paused = bool(habits_status.get("mode3_trial_trigger_target_paused", paused))
        low_mode0_target = bool(habits_status.get("low_battery_mode0_training_target", False))
        low_mode0_active = bool(habits_status.get("low_battery_mode0_training_active", False))
        low_mode0_reason = str(habits_status.get("low_battery_mode0_training_reason", "") or "").strip()
        current_reason = str(
            reason_text if reason_text is not None else habits_status.get("mode3_trial_trigger_reason", "") or ""
        ).strip()

        if target_paused and paused:
            gate_text = (
                f"Firmware low-battery pause reported at {reported_rsoc:.0f}% | "
                f"GUI low-battery route stays Mode0 | wait >{int(self.mode3_trial_trigger_resume_threshold)}%"
            )
        elif target_paused:
            gate_text = (
                f"Trigger gate armed at {reported_rsoc:.0f}% | waits for RF ON"
                if rf_on is False
                else f"Trigger pause pending at {reported_rsoc:.0f}%"
            )
        elif paused:
            gate_text = "Trigger resume pending"
        elif low_mode0_active:
            resume_text = (
                f"wait >{int(self.mode3_trial_trigger_resume_threshold)}%"
                if low_mode0_target
                else "return to normal LFP path on block end"
            )
            gate_text = (
                "LOW BATTERY MODE0 TRAINING | RF kept ON | "
                f"Mode0 EDF -> Mode3 folder | {resume_text}"
            )
        elif low_mode0_target:
            gate_text = (
                f"LOW BATTERY MODE0 TRAINING armed at {reported_rsoc:.0f}% | "
                "next Habits trial stays Mode0 | RF kept ON"
            )
        else:
            gate_text = (
                f"Trigger ready | low battery <{int(self.mode3_trial_trigger_pause_threshold)}% "
                f"uses Mode0 training | normal >{int(self.mode3_trial_trigger_resume_threshold)}%"
            )

        payload = {
            "mode3_trial_trigger_paused": paused,
            "mode3_trial_trigger_target_paused": target_paused,
            "mode3_trial_trigger_free_water_protocol": 11 if paused else 0,
            "mode3_trial_trigger_gate_text": gate_text,
            "mode3_trial_trigger_reason": current_reason,
            "mode3_trial_trigger_pause_threshold": int(self.mode3_trial_trigger_pause_threshold),
            "mode3_trial_trigger_resume_threshold": int(self.mode3_trial_trigger_resume_threshold),
            "low_battery_mode0_training_target": low_mode0_target,
            "low_battery_mode0_training_active": low_mode0_active,
            "low_battery_mode0_training_reason": low_mode0_reason,
        }
        self.status["habits"] = merge_slot_status(self.status["habits"], payload)
        if hasattr(self.habits, "status") and isinstance(self.habits.status, dict):
            self.habits.status.update(payload)

    def _mark_mode3_trial_trigger_policy_check(self, trigger_source: str = "") -> None:
        payload = {
            "mode3_trial_trigger_last_check_epoch": time.time(),
            "mode3_trial_trigger_last_check_source": str(trigger_source or "policy check"),
        }
        self.status["habits"] = merge_slot_status(self.status["habits"], payload)
        if hasattr(self.habits, "status") and isinstance(self.habits.status, dict):
            self.habits.status.update(payload)
        self._mark_updated()

    def _refresh_mode3_trial_trigger_target(self) -> Tuple[bool, str]:
        habits_status = dict(self.status.get("habits", {}) or {})
        battery = dict(self.status.get("battery", {}) or {})
        rsoc = int(round(float(battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0)))
        voltage_mv = float(battery.get("voltage", 0.0) or 0.0)
        has_battery_signal = bool(rsoc > 0 or voltage_mv > 0.0)
        low_mode0_target = bool(
            habits_status.get(
                "low_battery_mode0_training_target",
                False,
            )
        )
        previous_low_mode0_target = bool(low_mode0_target)
        previous_low_mode0_active = bool(habits_status.get("low_battery_mode0_training_active", False))
        reason = ""
        power_guard = dict(self.status.get("neural", {}).get("power_guard", {}) or {})
        if self._power_guard_low_battery_active(power_guard):
            low_mode0_target = True
            reason = "firmware low battery hold"
        elif not has_battery_signal:
            reason = ""
        elif rsoc < int(self.mode3_trial_trigger_pause_threshold):
            low_mode0_target = True
            reason = f"RSOC<{int(self.mode3_trial_trigger_pause_threshold)}%"
        elif rsoc > int(self.mode3_trial_trigger_resume_threshold):
            low_mode0_target = False
            reason = f"RSOC>{int(self.mode3_trial_trigger_resume_threshold)}%"

        if bool(low_mode0_target) and not previous_low_mode0_target:
            self._low_battery_rf_manual_off = False
        elif not bool(low_mode0_target) and not previous_low_mode0_active:
            self._low_battery_rf_manual_off = False

        self.status["habits"] = merge_slot_status(
            self.status["habits"],
            {
                "mode3_trial_trigger_target_paused": False,
                "low_battery_mode0_training_target": low_mode0_target,
                "low_battery_mode0_training_reason": reason,
            },
        )
        if hasattr(self.habits, "status") and isinstance(self.habits.status, dict):
            self.habits.status["mode3_trial_trigger_target_paused"] = False
            self.habits.status["low_battery_mode0_training_target"] = low_mode0_target
            self.habits.status["low_battery_mode0_training_reason"] = reason
        return low_mode0_target, reason

    def _set_mode3_trial_trigger_paused(self, paused: bool, reason: str = "") -> bool:
        desired = bool(paused)
        habits_status = dict(self.status.get("habits", {}) or {})
        if desired == bool(habits_status.get("mode3_trial_trigger_paused", False)):
            self._sync_mode3_trial_trigger_status(reason, refresh_rf_status=False)
            return True

        ok = bool(self.habits.send_mode3_trial_trigger_pause(desired))
        if not ok:
            self._append_system_log(
                f"Failed to {'pause' if desired else 'resume'} Mode3 trial trigger",
                level="warning",
            )
            self._sync_mode3_trial_trigger_status(reason, refresh_rf_status=False)
            return False

        battery = dict(self.status.get("battery", {}) or {})
        rsoc = int(round(float(battery.get("reported_rsoc", battery.get("rsoc", 0.0)) or 0.0)))
        reason_text = f" ({reason})" if str(reason or "").strip() else ""
        self.status["habits"] = merge_slot_status(
            self.status["habits"],
            {
                "mode3_trial_trigger_paused": desired,
                "mode3_trial_trigger_last_apply_epoch": time.time(),
            },
        )
        if desired:
            self._append_system_log(
                f"Habits firmware low-battery pause entered at battery {rsoc}%{reason_text}; GUI low-battery route stays Mode0",
                level="warning",
            )
        else:
            self._append_system_log(
                f"Habits firmware low-battery pause exited at battery {rsoc}%{reason_text}",
                level="info",
            )
        self._sync_mode3_trial_trigger_status(reason, refresh_rf_status=False)
        return True

    def _set_low_battery_mode0_training_active(self, active: bool, reason: str = "") -> None:
        payload = {
            "low_battery_mode0_training_active": bool(active),
            "low_battery_mode0_training_reason": str(reason or ""),
        }
        self.status["habits"] = merge_slot_status(self.status["habits"], payload)
        if hasattr(self.habits, "status") and isinstance(self.habits.status, dict):
            self.habits.status.update(payload)
        self._sync_mode3_trial_trigger_status(reason, refresh_rf_status=False)
        if bool(active):
            self._suspend_charging_guard_for_low_battery_training("low-battery Mode0 training")
        elif self.charging_guard_config.get("enabled", False):
            self._refresh_charging_guard_status("Watching")

    def _update_mode3_battery_trial_gate(
        self,
        trigger_source: str = "",
        refresh_rf_status: bool = False,
        mark_policy_check: bool = False,
    ) -> bool:
        if mark_policy_check:
            self._mark_mode3_trial_trigger_policy_check(trigger_source)
        _low_mode0_target, reason = self._refresh_mode3_trial_trigger_target()
        self._sync_mode3_trial_trigger_status(reason, refresh_rf_status=refresh_rf_status)
        self._ensure_low_battery_rf_power_on(reason)
        return True

    def _poll_mode3_trial_trigger_gate(self) -> None:
        self._update_mode3_battery_trial_gate(
            trigger_source="10s policy check",
            mark_policy_check=True,
        )

    @staticmethod
    def _runtime_cache_mouse_id(profile: Optional[Dict[str, Any]]) -> str:
        if not isinstance(profile, dict):
            return ""
        for key in ("mouse_id", "mice_id", "mouse"):
            value = str(profile.get(key, "") or "").strip()
            if value:
                return value
        directory = str(profile.get("mice_id_directory", "") or "").strip()
        if directory:
            return str(extract_mouse_id_from_directory(directory) or "").strip()
        return ""

    @classmethod
    def _runtime_cache_matches_current_mouse(
        cls,
        cached_profile: Optional[Dict[str, Any]],
        current_profile: Optional[Dict[str, Any]],
    ) -> bool:
        cached_mouse = cls._runtime_cache_mouse_id(cached_profile)
        current_mouse = cls._runtime_cache_mouse_id(current_profile)
        if not cached_mouse or not current_mouse:
            return True
        return cached_mouse.strip().casefold() == current_mouse.strip().casefold()

    def _restore_cached_state(self) -> None:
        cached_battery_history = self.runtime_cache.read_battery_history([])
        cached_impedance_history = self.runtime_cache.read_impedance_history([])
        cached_system_log = self.runtime_cache.read_system_log([])
        restored_anything = False
        if isinstance(cached_system_log, list):
            for item in self._filter_recent_system_log_entries(cached_system_log):
                self.system_log.append(dict(item))
        if isinstance(cached_battery_history, list):
            for item in cached_battery_history[-self.battery_history.maxlen :]:
                if isinstance(item, dict):
                    self.battery_history.append(dict(item))
            self._prune_battery_history(time.time())
            if self.battery_history:
                self._last_battery_snapshot = dict(self.battery_history[-1])
                restored_anything = True
        if isinstance(cached_impedance_history, list) and hasattr(self.neural, "load_impedance_history"):
            try:
                self.neural.load_impedance_history(list(cached_impedance_history))
                if cached_impedance_history:
                    restored_anything = True
            except Exception:
                pass

        cached_trials = self.runtime_cache.read_habits_trial_data([])
        cached_event_data = self.runtime_cache.read_habits_event_data([])
        cached_state_data = self.runtime_cache.read_habits_state_data([])
        cached_mode_switches = self.runtime_cache.read_mode_switches([])
        restored_from_runtime = bool(
            cached_trials or cached_mode_switches or cached_event_data or cached_state_data
        )
        restored_anything = restored_anything or restored_from_runtime
        if restored_from_runtime:
            trials_payload = list(cached_trials) if isinstance(cached_trials, list) else []
            mode_switch_payload = list(cached_mode_switches) if isinstance(cached_mode_switches, list) else []
            event_payload = list(cached_event_data) if isinstance(cached_event_data, list) else []
            state_payload = list(cached_state_data) if isinstance(cached_state_data, list) else []
            if hasattr(self.habits, "load_cached_payloads_with_streams"):
                self.habits.load_cached_payloads_with_streams(
                    trials_payload,
                    mode_switch_payload,
                    event_payload,
                    state_payload,
                )
            else:
                self.habits.load_cached_payloads(
                    trials_payload,
                    mode_switch_payload,
                )
        else:
            self.habits.load_persisted_history()

        if restored_anything:
            self._append_system_log("Runtime cache restored", level="info")

    def _parse_system_log_timestamp(self, value: Any) -> float:
        text = str(value or "").strip()
        if not text:
            return 0.0
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt).timestamp()
            except Exception:
                pass
        try:
            return float(text)
        except Exception:
            return 0.0

    def _filter_recent_system_log_entries(self, entries) -> list:
        cutoff = time.time() - SYSTEM_LOG_RETENTION_SECONDS
        recent = []
        for item in entries or []:
            if not isinstance(item, dict):
                continue
            timestamp_epoch = self._parse_system_log_timestamp(item.get("timestamp", ""))
            if timestamp_epoch <= 0 or timestamp_epoch >= cutoff:
                recent.append(dict(item))
        return recent[-SYSTEM_LOG_MAX_ENTRIES:]

    def _prune_system_log(self) -> None:
        self.system_log = deque(
            self._filter_recent_system_log_entries(list(self.system_log)),
            maxlen=SYSTEM_LOG_MAX_ENTRIES,
        )

    def _append_system_log(self, message: str, level: str = "info") -> None:
        self.system_log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": str(level or "info"),
            "message": str(message or ""),
        })
        self._prune_system_log()
        self._mark_history_dirty("system_log")
        # System Log is a master-card concern. Pushing a full local-chart status
        # sync from acquisition/log telemetry couples packet-gap events to chart
        # redraws, which shows up as visible stutter exactly when a packet is
        # missed. Keep the cache/status dirty, but do not wake chart controls.
        self._mark_updated(sync_local_chart=False)

    def _clear_system_log(self) -> None:
        self.system_log.clear()
        self.runtime_cache.clear_system_log()
        self._mark_history_dirty("system_log")
        self._mark_updated(sync_local_chart=False)

    def _mark_history_dirty(self, labels: Optional[Any] = None) -> None:
        if labels is None:
            normalized_labels = set(HISTORY_CACHE_LABELS)
        elif isinstance(labels, str):
            normalized_labels = {labels}
        else:
            try:
                normalized_labels = {str(label) for label in labels}
            except TypeError:
                normalized_labels = {str(labels)}
        normalized_labels = {
            label for label in normalized_labels
            if label in HISTORY_CACHE_LABELS
        }
        if not normalized_labels:
            return
        self._history_dirty = True
        self._history_dirty_labels.update(normalized_labels)
        self._history_dirty_generation = int(getattr(self, "_history_dirty_generation", 0) or 0) + 1

    def _sync_charging_guard_status(self) -> None:
        enabled = bool(self.charging_guard_config.get("enabled", False))
        area_ratio = float(self.charging_guard_last_detection.get("area_ratio", 0.0) or 0.0)
        reason = str(self.charging_guard_last_detection.get("reason", "not checked") or "not checked")
        lighting_state = str(self.charging_guard_last_detection.get("lighting_state", "unknown") or "unknown")
        scene_brightness = float(self.charging_guard_last_detection.get("scene_brightness", 0.0) or 0.0)
        active_dark_threshold = int(
            self.charging_guard_last_detection.get(
                "active_dark_threshold",
                self.charging_guard_config.get("dark_threshold", 90),
            )
            or 90
        )
        detection_text = self._charging_guard_detection_text()
        self.status["charging_guard"] = {
            "enabled": enabled,
            "status_text": "Watching" if enabled else "Off",
            "roi_norm": list(self.charging_guard_config.get("roi_norm", [0.1, 0.5, 0.3, 0.2])),
            "dark_threshold": int(self.charging_guard_config.get("dark_threshold", 90) or 90),
            "day_dark_threshold": int(self.charging_guard_config.get("day_dark_threshold", 90) or 90),
            "night_dark_threshold": int(self.charging_guard_config.get("night_dark_threshold", 90) or 90),
            "scene_brightness_threshold": int(self.charging_guard_config.get("scene_brightness_threshold", 80) or 80),
            "active_dark_threshold": active_dark_threshold,
            "lighting_state": lighting_state,
            "scene_brightness": scene_brightness,
            "min_area_ratio": float(self.charging_guard_config.get("min_area_ratio", 0.30) or 0.30),
            "check_interval_ms": int(self.charging_guard_config.get("check_interval_ms", 10000) or 10000),
            "required_hits": int(self.charging_guard_config.get("required_hits", 6) or 6),
            "warning_duration_ms": int(self.charging_guard_config.get("warning_duration_ms", 3000) or 3000),
            "fan_enabled": bool(self.charging_guard_config.get("fan_enabled", True)),
            "mouse_present": bool(self.charging_guard_last_detection.get("mouse_present", False)),
            "area_ratio": area_ratio,
            "reason": reason,
            "hit_count": int(getattr(self.charging_guard_state, "hit_count", 0) or 0),
            "warning_active": bool(getattr(self.charging_guard_state, "warning_active", False)),
            "detection_text": detection_text,
            "preview_last_detection_epoch": float(self.charging_guard_last_detection.get("timestamp_epoch", 0.0) or 0.0),
        }
        self._mark_updated()

    def _persist_profile(self) -> None:
        try:
            atomic_write_json(self.profile_path, self.profile)
            self.runtime_cache.write_last_profile(self.profile)
        except Exception as exc:
            self._set_last_error(f"Failed to persist profile: {exc}")

    @staticmethod
    def _normalize_gui_interval_profile_mapping(payload: Any) -> Dict[str, int]:
        normalized: Dict[str, int] = {}
        if not isinstance(payload, dict):
            return normalized
        for mode in range(4):
            key = f"mode{mode}"
            raw_value = payload.get(key, payload.get(mode, None))
            if raw_value is None:
                continue
            try:
                normalized[key] = max(1, int(raw_value))
            except Exception:
                continue
        return normalized

    @staticmethod
    def _normalize_gui_interval_profile_stats(payload: Any) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return normalized
        for mode in range(4):
            key = f"mode{mode}"
            item = payload.get(key, payload.get(mode, None))
            if not isinstance(item, dict):
                continue
            normalized_item: Dict[str, Any] = {}
            for value_key in (
                "interval",
                "best_interval",
                "best_loss_percent",
                "last_loss_percent",
                "last_expected_packets",
                "updated_epoch",
            ):
                value = item.get(value_key)
                if value is None and value_key == "best_loss_percent":
                    normalized_item[value_key] = None
                    continue
                try:
                    if value_key in {"interval", "best_interval", "last_expected_packets"}:
                        normalized_item[value_key] = int(value or 0)
                    else:
                        normalized_item[value_key] = float(value or 0.0)
                except Exception:
                    normalized_item[value_key] = None if value_key == "best_loss_percent" else 0
            normalized[key] = normalized_item
        return normalized

    @staticmethod
    def _normalize_gui_stream_interval_profile_mapping(payload: Any, mode_payload: Any = None) -> Dict[str, int]:
        mode_intervals = SlotService._normalize_gui_interval_profile_mapping(mode_payload)
        normalized: Dict[str, int] = {}
        for stream_key in GUI_INTERVAL_STREAM_KEYS:
            fallback_mode = GUI_INTERVAL_STREAM_FALLBACK_MODE[stream_key]
            if fallback_mode in mode_intervals:
                normalized[stream_key] = int(mode_intervals[fallback_mode])
        if not isinstance(payload, dict):
            return normalized
        aliases = {
            "mode0": "mode0_lfp",
            "mode1": "mode1_raw",
            "mode2": "mode2_raw",
            "mode3": "mode3_lfp_esa",
            "mode3_lfp": "mode3_lfp_esa",
            "mode3_esa": "mode3_lfp_esa",
            "mode3_lfp_esa": "mode3_lfp_esa",
            "mode3_raw": "mode3_raw",
        }
        for key, value in payload.items():
            stream_key = aliases.get(str(key).strip().lower(), str(key).strip().lower())
            if stream_key not in GUI_INTERVAL_STREAM_KEYS:
                continue
            try:
                normalized[stream_key] = max(1, int(value))
            except Exception:
                continue
        return normalized

    @staticmethod
    def _normalize_gui_stream_interval_profile_stats(payload: Any) -> Dict[str, Dict[str, Any]]:
        normalized: Dict[str, Dict[str, Any]] = {}
        if not isinstance(payload, dict):
            return normalized
        for stream_key in GUI_INTERVAL_STREAM_KEYS:
            item = payload.get(stream_key, None)
            if not isinstance(item, dict):
                continue
            normalized_item: Dict[str, Any] = {}
            for value_key in (
                "interval",
                "best_interval",
                "best_loss_percent",
                "last_loss_percent",
                "loss_ewma_percent",
                "last_expected_packets",
                "stable_windows",
                "updated_epoch",
            ):
                value = item.get(value_key)
                if value is None and value_key == "best_loss_percent":
                    normalized_item[value_key] = None
                    continue
                try:
                    if value_key in {"interval", "best_interval", "last_expected_packets", "stable_windows"}:
                        normalized_item[value_key] = int(value or 0)
                    else:
                        normalized_item[value_key] = float(value or 0.0)
                except Exception:
                    normalized_item[value_key] = None if value_key == "best_loss_percent" else 0
            for text_key in ("last_action", "pressure_reason"):
                normalized_item[text_key] = str(item.get(text_key, "") or "")
            normalized[stream_key] = normalized_item
        return normalized

    @staticmethod
    def _normalize_gui_interval_control_profile(payload: Any) -> Dict[str, Any]:
        incoming = payload if isinstance(payload, dict) else {}
        enabled = incoming.get("target_fps_enabled", incoming.get("enabled", True))
        targets = {stream_key: 12.0 for stream_key in GUI_INTERVAL_STREAM_KEYS}
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

    @staticmethod
    def _profile_signature_without_timestamps(payload: Any) -> str:
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {
                    str(key): clean(item)
                    for key, item in value.items()
                    if str(key) != "updated_epoch"
                }
            if isinstance(value, list):
                return [clean(item) for item in value]
            return value

        return json.dumps(clean(payload), sort_keys=True, separators=(",", ":"))

    def _maybe_persist_gui_update_intervals(self, neural_status: Dict[str, Any]) -> None:
        intervals = self._normalize_gui_interval_profile_mapping(
            neural_status.get("gui_update_intervals_by_mode", {})
        )
        if not intervals:
            return
        stats = self._normalize_gui_interval_profile_stats(
            neural_status.get("gui_update_interval_stats", {})
        )
        stream_intervals = self._normalize_gui_stream_interval_profile_mapping(
            neural_status.get("gui_update_intervals_by_stream", {}),
            intervals,
        )
        stream_stats = self._normalize_gui_stream_interval_profile_stats(
            neural_status.get("gui_update_interval_stream_stats", {})
        )
        interval_control = self._normalize_gui_interval_control_profile(
            neural_status.get("gui_update_interval_control", self.profile.get("gui_update_interval_control", {}))
        )
        signature_payload = {
            "intervals": intervals,
            "stats": stats,
            "stream_intervals": stream_intervals,
            "stream_stats": stream_stats,
            "interval_control": interval_control,
        }
        signature = self._profile_signature_without_timestamps(signature_payload)
        durable_signature = json.dumps(
            {
                "intervals": intervals,
                "stream_intervals": stream_intervals,
                "interval_control": interval_control,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        if signature == self._last_gui_interval_profile_payload:
            return
        now_mono = time.monotonic()
        profile_intervals = self._normalize_gui_interval_profile_mapping(
            self.profile.get("gui_update_intervals_by_mode", {})
        )
        profile_stats = self._normalize_gui_interval_profile_stats(
            self.profile.get("gui_update_interval_stats", {})
        )
        profile_stream_intervals = self._normalize_gui_stream_interval_profile_mapping(
            self.profile.get("gui_update_intervals_by_stream", {}),
            profile_intervals,
        )
        profile_stream_stats = self._normalize_gui_stream_interval_profile_stats(
            self.profile.get("gui_update_interval_stream_stats", {})
        )
        profile_interval_control = self._normalize_gui_interval_control_profile(
            self.profile.get("gui_update_interval_control", {})
        )
        durable_profile_changed = (
            intervals != profile_intervals
            or stream_intervals != profile_stream_intervals
            or interval_control != profile_interval_control
        )
        profile_changed = (
            durable_profile_changed
            or stats != profile_stats
            or stream_stats != profile_stream_stats
        )
        if (
            not durable_profile_changed
            and now_mono - float(self._last_gui_interval_profile_persist_monotonic or 0.0) < 9.5
        ):
            return
        if not profile_changed and durable_signature == self._last_gui_interval_profile_payload:
            return
        self.profile["gui_update_intervals_by_mode"] = dict(intervals)
        self.profile["gui_update_interval_stats"] = dict(stats)
        self.profile["gui_update_intervals_by_stream"] = dict(stream_intervals)
        self.profile["gui_update_interval_stream_stats"] = dict(stream_stats)
        self.profile["gui_update_interval_control"] = dict(interval_control)
        self._last_gui_interval_profile_payload = signature
        self._last_gui_interval_profile_persist_monotonic = now_mono
        self._persist_profile()

    @staticmethod
    def _normalize_detail_display_tuning_profile(payload: Any) -> Dict[str, Any]:
        incoming = payload if isinstance(payload, dict) else {}
        strategy = str(incoming.get("strategy", incoming.get("mode", "balanced")) or "balanced").strip().lower()
        if strategy not in {"balanced", "interval_only"}:
            strategy = "balanced"
        keys = ("lfp", "spike", "mode2", "mode3_lfp_esa", "mode3_raw")
        aliases = {
            "mode0_lfp": "lfp",
            "mode1_raw": "spike",
            "mode2_raw": "mode2",
            "mode3_lfp": "mode3_lfp_esa",
            "mode3_esa": "mode3_lfp_esa",
            "mode3_lfp_esa": "mode3_lfp_esa",
            "mode3_raw": "mode3_raw",
        }
        factors = {key: 1 for key in keys}
        incoming_factors = incoming.get("factors", incoming.get("display_downsample_factors", {}))
        if isinstance(incoming_factors, dict):
            for key, value in incoming_factors.items():
                stream_key = aliases.get(str(key).strip().lower(), str(key).strip().lower())
                if stream_key not in factors:
                    continue
                try:
                    factors[stream_key] = max(1, min(16, int(value)))
                except Exception:
                    continue
        actions = {}
        incoming_actions = incoming.get("last_action", {})
        if isinstance(incoming_actions, dict):
            actions = {key: str(incoming_actions.get(key, "") or "") for key in keys}
        try:
            stable_windows = max(0, int(incoming.get("mode3_stable_windows", 0) or 0))
        except Exception:
            stable_windows = 0
        return {
            "strategy": strategy,
            "factors": factors,
            "last_action": actions,
            "mode3_stable_windows": stable_windows,
            "updated_epoch": float(incoming.get("updated_epoch", time.time()) or time.time()),
        }

    def _persist_detail_display_tuning(self, payload: Any, force: bool = False) -> None:
        tuning = self._normalize_detail_display_tuning_profile(payload)
        signature = self._profile_signature_without_timestamps(tuning)
        if not bool(force) and signature == self._last_detail_display_profile_payload:
            return
        now_mono = time.monotonic()
        if (
            not bool(force)
            and now_mono - float(self._last_detail_display_profile_persist_monotonic or 0.0) < 20.0
        ):
            return
        current = self._normalize_detail_display_tuning_profile(
            self.profile.get("detail_display_tuning", {})
        )
        current_signature = self._profile_signature_without_timestamps(current)
        if not bool(force) and signature == current_signature:
            self._last_detail_display_profile_payload = signature
            return
        self.profile["detail_display_tuning"] = tuning
        self._last_detail_display_profile_payload = signature
        self._last_detail_display_profile_persist_monotonic = now_mono
        self._persist_profile()

    def _start_staggered_timer(self, timer: QTimer, timer_name: str, interval_ms: int) -> None:
        interval = max(10, int(interval_ms or 10))
        timer.setInterval(interval)
        step = int(self.slot_runtime_config.get("timer_stagger_step_ms", 170) or 170)
        delay_ms = _timer_stagger_delay_ms(self.slot_id, timer_name, interval, step)
        if delay_ms <= 0:
            timer.start()
            return

        def _start_timer():
            if getattr(self, "_shutdown_requested", False):
                return
            try:
                timer.start()
            except RuntimeError:
                pass

        QTimer.singleShot(delay_ms, _start_timer)

    def _run_timed_task(self, task_name: str, func: Callable[[], Any]) -> Any:
        start_epoch = time.time()
        start_mono = time.monotonic()
        try:
            return func()
        finally:
            end_epoch = time.time()
            duration_ms = max(0.0, (time.monotonic() - start_mono) * 1000.0)
            self._record_slot_task_trace(
                task_name,
                start_epoch=start_epoch,
                end_epoch=end_epoch,
                duration_ms=duration_ms,
            )

    def _record_slot_task_trace(
        self,
        task_name: str,
        start_epoch: Optional[float] = None,
        end_epoch: Optional[float] = None,
        duration_ms: Optional[float] = None,
    ) -> bool:
        try:
            duration = float(duration_ms if duration_ms is not None else 0.0)
        except Exception:
            duration = 0.0
        if duration < float(getattr(self, "_slot_task_trace_min_duration_ms", 1.0) or 0.0):
            return False
        try:
            end_time = float(end_epoch if end_epoch is not None else time.time())
        except Exception:
            end_time = time.time()
        try:
            start_time = float(start_epoch if start_epoch is not None else end_time - (duration / 1000.0))
        except Exception:
            start_time = end_time
        self._slot_task_trace_seq = int(getattr(self, "_slot_task_trace_seq", 0) or 0) + 1
        camera_status = dict(self.status.get("camera", {}) or {})
        neural_status = dict(self.status.get("neural", {}) or {})
        try:
            command_queue_depth = int(self.command_queue.qsize())
        except Exception:
            command_queue_depth = 0
        self._slot_task_trace.append({
            "seq": int(self._slot_task_trace_seq),
            "task": str(task_name or "task"),
            "start_epoch": float(start_time),
            "end_epoch": float(end_time),
            "duration_ms": float(duration),
            "detail_attached": bool(self.status.get("detail_attached", False)),
            "neural_sampling": bool(neural_status.get("sampling", False)),
            "save_mode": str(neural_status.get("save_mode", "") or ""),
            "camera_open": bool(camera_status.get("open", False)),
            "camera_recording": bool(camera_status.get("recording", False)),
            "camera_preview_enabled": bool(camera_status.get("preview_enabled", False)),
            "status_dirty": bool(getattr(self, "_status_dirty", False)),
            "history_dirty": bool(getattr(self, "_history_dirty", False)),
            "command_queue_depth": int(command_queue_depth),
            "command_poll_summary": str(getattr(self, "_last_command_poll_summary", "") or "")
            if str(task_name or "") == "command_poll"
            else "",
        })
        return True

    def _slot_scheduler_trace_payload(self, limit: int = 20) -> list:
        try:
            limit = max(0, int(limit or 0))
        except Exception:
            limit = 20
        trace = list(getattr(self, "_slot_task_trace", []) or [])
        if limit > 0:
            trace = trace[-limit:]
        return [dict(item) for item in trace if isinstance(item, dict)]

    def _sync_camera_charging_guard_config(self) -> None:
        if not hasattr(self.camera, "configure_charging_guard_detection"):
            return
        try:
            self.camera.configure_charging_guard_detection(
                enabled=bool(self.charging_guard_config.get("enabled", False)),
                config=dict(self.charging_guard_config),
                interval_ms=int(self.charging_guard_config.get("check_interval_ms", 10000) or 10000),
            )
        except Exception as exc:
            self._append_performance_warning(
                "charging_guard_camera_config",
                f"Charging guard camera detection config failed: {exc}",
                level="warning",
            )

    def _apply_charging_guard_config(self, config: Optional[Dict[str, Any]], persist: bool = True) -> None:
        previous_enabled = bool(self.charging_guard_config.get("enabled", False))
        merged = dict(self.charging_guard_config)
        if isinstance(config, dict):
            incoming = dict(config)
            if "dark_threshold" in incoming:
                if "day_dark_threshold" not in incoming:
                    incoming["day_dark_threshold"] = incoming.get("dark_threshold")
                if "night_dark_threshold" not in incoming:
                    incoming["night_dark_threshold"] = incoming.get("dark_threshold")
            merged.update(incoming)
        try:
            merged = _normalize_charging_guard_config(merged)
        except Exception as exc:
            raise ValueError(f"Invalid charging guard config: {exc}") from exc

        self.charging_guard_config = dict(merged)
        self.profile["charging_guard"] = dict(self.charging_guard_config)
        self.charging_guard_detector.configure(
            roi_norm=self.charging_guard_config["roi_norm"],
            dark_threshold=self.charging_guard_config["dark_threshold"],
            day_dark_threshold=self.charging_guard_config["day_dark_threshold"],
            night_dark_threshold=self.charging_guard_config["night_dark_threshold"],
            scene_brightness_threshold=self.charging_guard_config["scene_brightness_threshold"],
            min_area_ratio=self.charging_guard_config["min_area_ratio"],
        )
        self.charging_guard_state.configure(int(self.charging_guard_config["required_hits"]))
        self.charging_guard_timer.setInterval(int(self.charging_guard_config["check_interval_ms"]))
        self._sync_camera_charging_guard_config()
        enabled = bool(self.charging_guard_config.get("enabled", False))
        enabled_toggled_on = bool(enabled and not previous_enabled and isinstance(config, dict) and "enabled" in config)
        if not enabled:
            self._reset_charging_guard("disabled")
        elif enabled_toggled_on:
            self.charging_guard_state.reset("enabled")
            self._refresh_charging_guard_status("Watching")
        else:
            self._reset_charging_guard("config updated")
            self._refresh_charging_guard_status("Watching")
        if persist:
            self._persist_profile()

    def _charging_guard_rf_is_on(self, query_device: bool = True) -> bool:
        if not bool(self.status.get("rf", {}).get("connected", False)):
            return False
        if query_device:
            try:
                self.rf.query_status(emit_error=False)
            except TypeError:
                try:
                    self.rf.query_status()
                except Exception:
                    return False
            except Exception:
                return False
        return str(self.status.get("rf", {}).get("power_state", "unknown") or "unknown").strip().lower() == "on"

    def _refresh_charging_guard_status(self, status_text: str) -> None:
        self._sync_charging_guard_status()
        self.status["charging_guard"]["status_text"] = str(status_text or "Off")
        self.status["charging_guard"]["detection_text"] = self._charging_guard_detection_text()
        self._mark_updated()

    def _charging_guard_detection_text(self) -> str:
        detection = dict(self.charging_guard_last_detection or {})
        present = bool(detection.get("mouse_present", False))
        ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
        reason = str(detection.get("reason", "not checked") or "not checked")
        lighting = str(detection.get("lighting_state", "unknown") or "unknown")
        scene_brightness = float(detection.get("scene_brightness", 0.0) or 0.0)
        active_threshold = int(detection.get("active_dark_threshold", self.charging_guard_config.get("dark_threshold", 90)) or 90)
        guard_state = "ON" if self.charging_guard_config.get("enabled", False) else "OFF"
        return (
            f"Charging Guard {guard_state} | Mouse: {'YES' if present else 'NO'} | "
            f"{lighting} brightness {scene_brightness:.1f} threshold {active_threshold} | "
            f"area {ratio:.1f}% | {reason}"
        )

    def _annotate_charging_guard_frame(self, frame):
        if frame is None:
            return None
        try:
            display_frame = frame.copy()
        except Exception:
            display_frame = frame
        detection = dict(self.charging_guard_last_detection or {})
        roi = detection.get("roi", (0, 0, 0, 0))
        x, y, w, h = [int(v) for v in roi]
        present = bool(detection.get("mouse_present", False))
        color = (0, 220, 0) if present else (0, 180, 255)
        if w > 0 and h > 0:
            cv2.rectangle(display_frame, (x, y), (x + w, y + h), color, 2)
        ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
        lighting = str(detection.get("lighting_state", "unknown") or "unknown")
        active_threshold = int(detection.get("active_dark_threshold", self.charging_guard_config.get("dark_threshold", 90)) or 90)
        text = f"Mouse {'YES' if present else 'NO'} {ratio:.1f}% {lighting} th{active_threshold}"
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

    def _update_charging_guard_detection(self, frame, min_interval_seconds: float = 0.25) -> Dict[str, Any]:
        configured_interval = float(self.slot_runtime_config.get("preview_detection_min_interval_ms", 250) or 250) / 1000.0
        min_interval_seconds = max(float(min_interval_seconds), configured_interval)
        now_mono = time.monotonic()
        if now_mono - float(self._charging_guard_last_preview_detection_monotonic or 0.0) < float(min_interval_seconds):
            return dict(self.charging_guard_last_detection)
        detection = self.charging_guard_detector.detect(frame)
        self.charging_guard_last_detection = dict(detection)
        self.charging_guard_last_detection["timestamp_epoch"] = time.time()
        self._charging_guard_last_preview_detection_monotonic = now_mono
        self._sync_charging_guard_status()
        return dict(detection)

    def _reset_charging_guard(self, reason: str = "reset", send_stop: bool = True) -> None:
        decision = self.charging_guard_state.reset(str(reason or "reset"))
        if send_stop and decision.get("action") == "stop":
            try:
                self.habits.stop_charging_warning()
            except Exception:
                pass
        self._refresh_charging_guard_status(
            decision.get("status", "Off") if self.charging_guard_config.get("enabled", False) else "Off"
        )

    def _low_battery_mode0_training_active(self) -> bool:
        return bool(self.status.get("habits", {}).get("low_battery_mode0_training_active", False))

    def _low_battery_mode0_training_target_or_active(self) -> bool:
        habits = self.status.get("habits", {})
        if not isinstance(habits, dict):
            return False
        return bool(
            habits.get("low_battery_mode0_training_target", False)
            or habits.get("low_battery_mode0_training_active", False)
        )

    def _ensure_low_battery_rf_power_on(self, reason: str = "") -> bool:
        if not self._low_battery_mode0_training_target_or_active():
            return False
        if bool(getattr(self, "_low_battery_rf_manual_off", False)):
            return False
        rf_status = dict(self.status.get("rf", {}) or {})
        rf_connected = bool(rf_status.get("connected", False) or self.rf.status.get("connected", False))
        if not rf_connected:
            return False
        rf_power_state = str(
            self.rf.status.get("power_state", rf_status.get("power_state", "unknown")) or "unknown"
        ).strip().lower()
        if rf_power_state == "on":
            return True
        now_mono = time.monotonic()
        if now_mono - float(getattr(self, "_last_low_battery_rf_hold_monotonic", 0.0) or 0.0) < 1.0:
            return False
        self._last_low_battery_rf_hold_monotonic = now_mono
        confirmed = self._confirm_rf_power_state_for_habits(True)
        if confirmed:
            self._append_system_log(
                "RF power held ON for low-battery Mode0 training"
                + (f" ({reason})" if str(reason or "").strip() else ""),
                level="info",
            )
        return bool(confirmed)

    def _suspend_charging_guard_for_low_battery_training(self, reason: str = "low-battery Mode0 training") -> None:
        self._reset_charging_guard(str(reason or "low-battery Mode0 training"), send_stop=True)
        if self.charging_guard_config.get("enabled", False):
            self._refresh_charging_guard_status("Suspended: low-battery Mode0 training")

    def _poll_charging_guard(self) -> None:
        if not self.charging_guard_config.get("enabled", False):
            return
        if self._low_battery_mode0_training_active():
            self._suspend_charging_guard_for_low_battery_training("low-battery Mode0 training")
            return

        reason = ""
        detection = None
        if not bool(self.status.get("camera", {}).get("open", False)):
            reason = "camera off"
        else:
            interval_s = max(1.0, float(self.charging_guard_config.get("check_interval_ms", 10000) or 10000) / 1000.0)
            if hasattr(self.camera, "get_charging_guard_detection"):
                detection = self.camera.get_charging_guard_detection(max_age_seconds=max(30.0, interval_s * 2.5))
            if detection is None:
                reason = "no detection"

        if detection is None:
            detection = {
                "mouse_present": False,
                "area_ratio": 0.0,
                "roi": tuple(self.charging_guard_last_detection.get("roi", (0, 0, 0, 0))),
                "reason": reason or "no detection",
                "lighting_state": self.charging_guard_last_detection.get("lighting_state", "unknown"),
                "scene_brightness": float(self.charging_guard_last_detection.get("scene_brightness", 0.0) or 0.0),
                "active_dark_threshold": int(
                    self.charging_guard_last_detection.get(
                        "active_dark_threshold",
                        self.charging_guard_config.get("dark_threshold", 90),
                    )
                    or 90
                ),
            }
        self.charging_guard_last_detection = dict(detection)
        self.charging_guard_last_detection["timestamp_epoch"] = time.time()
        mouse_present = bool(detection.get("mouse_present", False))
        if not reason and not mouse_present:
            reason = "mouse absent"

        rf_on = False
        if not reason:
            rf_on = self._charging_guard_rf_is_on()
            if not rf_on:
                reason = "RF off"

        decision = self.charging_guard_state.update(mouse_present and rf_on, reason)
        action = str(decision.get("action") or "")
        if action == "warning":
            try:
                self.habits.send_charging_warning(
                    int(self.charging_guard_config.get("warning_duration_ms", 3000) or 3000),
                    bool(self.charging_guard_config.get("fan_enabled", True)),
                )
            except Exception:
                pass
        elif action == "stop":
            try:
                self.habits.stop_charging_warning()
            except Exception:
                pass
        self._refresh_charging_guard_status(decision.get("status", "Watching"))

    def _maybe_log_transition(
        self,
        section: str,
        previous: Optional[Dict[str, Any]],
        current: Optional[Dict[str, Any]],
        key: str,
        formatter,
    ) -> None:
        previous_value = None if not isinstance(previous, dict) else previous.get(key)
        current_value = None if not isinstance(current, dict) else current.get(key)
        if previous_value == current_value:
            return
        message = formatter(current_value, previous_value)
        if message:
            self._append_system_log(str(message), level="info")

    def _mark_updated(self, sync_local_chart: bool = True) -> None:
        self.status["last_update_epoch"] = time.time()
        self._status_dirty = True
        if sync_local_chart:
            self._sync_local_chart_status()

    def get_chart_status_snapshot(self) -> Dict[str, Any]:
        return dict(self.status)

    def handle_chart_command(self, command: Dict[str, Any]) -> None:
        self._handle_command(command)

    def _sync_local_chart_status(self) -> None:
        chart = self.local_chart_window
        if chart is None:
            return
        try:
            chart.update_slot_status(dict(self.status))
        except Exception as exc:
            self._append_performance_warning(
                "local_chart_status_sync",
                f"Local chart status sync failed: {exc}",
                level="warning",
            )

    def _open_local_chart_window(self) -> None:
        self._publish_habits_live(force=True)
        chart = self.local_chart_window
        if chart is None:
            chart = SlotLocalChartWindow(self)
            self.local_chart_window = chart
            try:
                chart.destroyed.connect(lambda *_args, service=self: service.on_local_chart_closed())
            except Exception:
                pass
        try:
            chart.update_slot_status(dict(self.status))
        except Exception:
            pass
        for method_name in ("show", "raise_", "activateWindow"):
            try:
                getattr(chart, method_name)()
            except Exception:
                pass
        try:
            if not self.detail_render_timer.isActive():
                self.detail_render_timer.start()
        except Exception:
            pass

    def _active_acquisition_for_detail_warmup(self) -> bool:
        neural_status = dict(self.status.get("neural", {}) or {})
        camera_status = dict(self.status.get("camera", {}) or {})
        return (
            bool(neural_status.get("sampling", False))
            or bool(neural_status.get("save_mode", ""))
            or bool(camera_status.get("recording", False))
        )

    def _detail_attach_warmup_delay_ms(self) -> int:
        if not self._active_acquisition_for_detail_warmup():
            return 0
        try:
            return max(0, int(getattr(self, "_detail_attach_warmup_ms", 0) or 0))
        except Exception:
            return 0

    def _enable_detail_stream_after_warmup(self, generation: int) -> None:
        if bool(getattr(self, "_shutdown_requested", False)):
            return
        if int(generation) != int(getattr(self, "_detail_attach_generation", 0) or 0):
            return
        if not bool(self.status.get("detail_attached", False)):
            return
        if self.local_chart_window is None:
            return
        self.neural.set_detail_enabled(True)
        self._mark_updated(sync_local_chart=True)
        self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _close_local_chart_window(self) -> None:
        chart = self.local_chart_window
        self.local_chart_window = None
        self._pending_detail_payload_by_stream.clear()
        try:
            self.detail_render_timer.stop()
        except Exception:
            pass
        if chart is None:
            return
        self._local_chart_closing = True
        try:
            if hasattr(chart, "close_from_service"):
                chart.close_from_service()
            else:
                chart.close()
        except Exception as exc:
            self._append_performance_warning(
                "local_chart_close",
                f"Local chart close failed: {exc}",
                level="warning",
            )
        finally:
            try:
                if hasattr(chart, "deleteLater"):
                    chart.deleteLater()
            except Exception:
                pass
            self._local_chart_closing = False

    def on_local_chart_closed(self) -> None:
        if bool(self._local_chart_closing):
            return
        self.local_chart_window = None
        self._pending_detail_payload_by_stream.clear()
        try:
            self.detail_render_timer.stop()
        except Exception:
            pass
        if bool(self.status.get("detail_attached", False)):
            self.status["detail_attached"] = False
            self.neural.set_detail_enabled(False)
            self._append_system_log("Detail viewer detached", level="info")
            self._mark_updated()
            self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _log_habits_config_summary(
        self,
        previous: Optional[Dict[str, Any]],
        current: Optional[Dict[str, Any]],
    ) -> None:
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return
        config_keys = (
            "reward_left",
            "reward_middle",
            "reward_right",
            "low_light",
            "high_light",
            "protocol",
            "inter_block_interval_minutes",
        )
        if not any(previous.get(key) != current.get(key) for key in config_keys):
            return
        self._append_system_log(
            "Habits config synced -> "
            f"Reward {int(current.get('reward_left', 0) or 0)}/"
            f"{int(current.get('reward_middle', 0) or 0)}/"
            f"{int(current.get('reward_right', 0) or 0)}, "
            f"Light {int(current.get('low_light', 0) or 0)}-"
            f"{int(current.get('high_light', 0) or 0)}, "
            f"Protocol P{int(current.get('protocol', 0) or 0)}, "
            f"IBI {int(current.get('inter_block_interval_minutes', 0) or 0)} min",
            level="info",
        )

    def _flush_runtime_cache_priority(self, minimum_interval_seconds: float = 0.15) -> None:
        now_mono = time.monotonic()
        if float(minimum_interval_seconds) > 0 and (
            now_mono - float(self._last_priority_flush_monotonic or 0.0)
        ) < float(minimum_interval_seconds):
            return
        self._last_priority_flush_monotonic = now_mono
        self._flush_history_cache(force=False, priority_labels=("system_log",))
        self._flush_status_cache(force=True)

    @staticmethod
    def _neural_update_requires_fast_flush(
        previous: Optional[Dict[str, Any]],
        current: Optional[Dict[str, Any]],
    ) -> bool:
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return False
        important_keys = (
            "connected",
            "command_busy",
            "command_action",
            "connection_state",
            "active_mode",
            "save_mode",
            "imu_mode_text",
            "auto_threshold_running",
            "auto_threshold_channel",
            "impedance_test_running",
            "impedance_progress_percent",
            "impedance_status_text",
            "impedance_history_count",
            "impedance_result_seq",
        )
        for key in important_keys:
            if previous.get(key) != current.get(key):
                return True
        prev_last = previous.get("impedance_last_result", {})
        curr_last = current.get("impedance_last_result", {})
        if not isinstance(prev_last, dict):
            prev_last = {}
        if not isinstance(curr_last, dict):
            curr_last = {}
        return (
            float(prev_last.get("timestamp_epoch", 0.0) or 0.0)
            != float(curr_last.get("timestamp_epoch", 0.0) or 0.0)
        )

    @staticmethod
    def _habits_update_requires_fast_flush(
        previous: Optional[Dict[str, Any]],
        current: Optional[Dict[str, Any]],
    ) -> bool:
        if not isinstance(previous, dict) or not isinstance(current, dict):
            return False
        important_keys = (
            "connected",
            "paused",
            "current_trial",
            "trials_today",
            "protocol",
            "last_protocol",
            "performance",
            "protocol_trials",
            "trial_type",
            "protocol_perf",
            "outcome_code",
            "outcome_text",
            "protocol_progress_raw",
            "cache_update_seq",
            "read_all_seq",
            "reward_left",
            "reward_middle",
            "reward_right",
            "low_light",
            "high_light",
            "inter_block_interval_ms",
            "inter_block_interval_minutes",
            "cap_detect_text",
            "cap_baseline_text",
            "cap_filtered_text",
            "cap_delta_text",
            "cap_drift_text",
            "cap_drift_state",
            "last_sync_text",
            "rf_off_confirmed",
            "sd_download_active",
            "sd_download_status_text",
            "sd_download_target_dir",
            "sd_download_last_error",
        )
        for key in important_keys:
            if previous.get(key) != current.get(key):
                return True
        prev_cap_tail = list(previous.get("cap_history", []) or [])[-1:] if isinstance(previous.get("cap_history"), list) else []
        curr_cap_tail = list(current.get("cap_history", []) or [])[-1:] if isinstance(current.get("cap_history"), list) else []
        return prev_cap_tail != curr_cap_tail

    def _describe_service_state(self) -> Tuple[str, str]:
        rf_state = str(self.status.get("rf", {}).get("connection_state", "idle") or "idle").strip().lower()
        camera_state = str(self.status.get("camera", {}).get("health_state", "ok") or "ok").strip().lower()
        rf_reason = str(self.status.get("rf", {}).get("last_failure_reason", "") or "").strip()
        if rf_state == "failed" or camera_state == "failed":
            reasons = []
            if rf_state == "failed":
                reasons.append("RF recovery failed")
            if camera_state == "failed":
                reasons.append("Camera recovery failed")
            return "failed", "; ".join(reasons)
        if rf_state in {"recovering", "reconnecting", "port_missing", "device_unresponsive"} or camera_state == "recovering":
            reasons = []
            if rf_state == "port_missing":
                reasons.append(rf_reason or "RF serial device missing from the system")
            elif rf_state == "device_unresponsive":
                reasons.append(rf_reason or "RF device is connected but not responding")
            elif rf_state in {"recovering", "reconnecting"}:
                reasons.append(rf_reason or "RF auto-recovery in progress")
            if camera_state == "recovering":
                reasons.append("Camera auto-recovery in progress")
            return "degraded", "; ".join(reasons)
        if self._last_error:
            return "error", str(self._last_error or "").strip()
        return "ready", "All subsystems healthy"

    def _refresh_service_state(self) -> None:
        previous_state = str(self.status.get("state", "") or "").strip().lower()
        current_state, reason = self._describe_service_state()
        self.status["state"] = current_state
        normalized_reason = str(reason or "").strip()
        if current_state == "degraded" and normalized_reason and normalized_reason != self._last_state_reason:
            self._append_system_log(f"Service degraded: {normalized_reason}", level="warning")
        elif current_state == "failed" and (
            current_state != previous_state or normalized_reason != self._last_state_reason
        ):
            self._append_system_log(
                f"Service failed: {normalized_reason or 'hardware recovery failed'}",
                level="error",
            )
        elif current_state == "ready" and previous_state and previous_state != "ready":
            self._append_system_log("Service ready: all tracked subsystems healthy", level="success")
        elif previous_state == "degraded" and current_state != "degraded":
            self._append_system_log(f"Service recovered: {current_state}", level="success")
        self._last_state_reason = normalized_reason

    def _clear_last_error_if_matches(self, keywords) -> None:
        last_error = str(self._last_error or "").strip()
        if not last_error:
            return
        lowered = last_error.lower()
        for keyword in keywords:
            if str(keyword or "").strip().lower() in lowered:
                self._last_error = ""
                self.status["last_error"] = ""
                self._refresh_service_state()
                self._mark_updated()
                return

    def _set_last_error(self, message: str) -> None:
        self._last_error = str(message or "")
        self.status["last_error"] = self._last_error
        self._refresh_service_state()
        self._mark_updated()

    def _on_error(self, message: str) -> None:
        self._set_last_error(message)
        self._append_system_log(str(message or ""), level="error")
        self._flush_runtime_cache()

    def _on_neural_status(self, update: Dict[str, Any]) -> None:
        previous = dict(self.status.get("neural", {}))
        self.status["neural"] = merge_slot_status(self.status["neural"], update)
        self._append_packet_loss_history(update)
        battery = update.get("battery")
        if isinstance(battery, dict):
            normalized_battery = dict(battery)
            normalized_battery["reported_rsoc"] = float(
                normalized_battery.get("reported_rsoc", normalized_battery.get("rsoc", 0.0)) or 0.0
            )
            self.status["battery"] = merge_slot_status(self.status["battery"], normalized_battery)
            self._sync_bq25176_status()
            self._append_battery_history()
            self._update_mode3_battery_trial_gate(trigger_source="battery update")
        if isinstance(self.status.get("neural", {}).get("power_guard"), dict):
            self._handle_low_battery_power_guard()
        # Passive low-battery recovery is disabled; low battery routing uses Mode0 training.
        if any(key in update for key in ("impedance_history_count", "impedance_result_seq", "impedance_last_result")):
            self._mark_history_dirty("impedance_history")
        current = dict(self.status.get("neural", {}))
        self._maybe_persist_gui_update_intervals(current)
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "connected",
            lambda value, _prev: f"Neural serial {'connected' if value else 'disconnected'}",
        )
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "active_mode",
            lambda value, _prev: f"Neural mode -> {value}",
        )
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "save_mode",
            lambda value, _prev: f"Save mode -> {_save_mode_frontend_label(value)}",
        )
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "imu_mode_text",
            lambda value, _prev: f"IMU mode -> {value or imu_mode_label(current.get('imu_mode', 1))}",
        )
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "auto_threshold_running",
            lambda value, _prev: "Auto spike threshold update started" if value else "Auto spike threshold update completed",
        )
        self._maybe_log_transition(
            "neural",
            previous,
            current,
            "impedance_test_running",
            lambda value, _prev: "Impedance test started" if value else "Impedance test finished",
        )
        previous_queue_drops = int(previous.get("save_queue_drop_count", 0) or 0)
        current_queue_drops = int(current.get("save_queue_drop_count", 0) or 0)
        if current_queue_drops > previous_queue_drops:
            dropped_chunks = current_queue_drops - previous_queue_drops
            writer_lag = int(current.get("writer_lag", 0) or 0)
            self._append_system_log(
                f"EDF save queue overload: dropped {dropped_chunks} chunk(s) "
                f"(total {current_queue_drops}, writer lag {writer_lag})",
                level="error",
            )
        previous_serial_backlog_events = int(previous.get("serial_backlog_event_count", 0) or 0)
        current_serial_backlog_events = int(current.get("serial_backlog_event_count", 0) or 0)
        if current_serial_backlog_events > previous_serial_backlog_events:
            backlog_bytes = int(current.get("serial_backlog_last_bytes", 0) or 0)
            peak_bytes = int(current.get("serial_backlog_peak_bytes", 0) or 0)
            self._append_system_log(
                f"Neural serial RX backlog critical: drained {backlog_bytes} byte(s) late "
                f"(event {current_serial_backlog_events}, peak {peak_bytes})",
                level="critical",
            )
        # Read-gap telemetry is intentionally kept out of System Log. A soft serial
        # scheduling delay is useful for monitoring/backpressure, but writing a log
        # line for every event can itself become acquisition load.
        previous_packet_gap_events = int(previous.get("packet_gap_event_count", 0) or 0)
        current_packet_gap_events = int(current.get("packet_gap_event_count", 0) or 0)
        if current_packet_gap_events > previous_packet_gap_events:
            missing = int(current.get("packet_gap_missing", 0) or 0)
            summary = str(current.get("packet_gap_summary", "") or "").strip()
            self._queue_neural_packet_gap_log(
                missing,
                summary,
                event_delta=current_packet_gap_events - previous_packet_gap_events,
                diagnostics=current.get("packet_gap_diagnostics", {}),
            )
        previous_out_of_order_events = int(previous.get("packet_out_of_order_event_count", 0) or 0)
        current_out_of_order_events = int(current.get("packet_out_of_order_event_count", 0) or 0)
        if current_out_of_order_events > previous_out_of_order_events:
            summary = str(current.get("packet_out_of_order_summary", "") or "").strip()
            self._append_performance_warning(
                "packet_out_of_order",
                "Neural packet counter out-of-order sample ignored"
                + (f": {summary}" if summary else ""),
                level="warning",
            )
        previous_counter_reset_events = int(previous.get("packet_counter_reset_event_count", 0) or 0)
        current_counter_reset_events = int(current.get("packet_counter_reset_event_count", 0) or 0)
        if current_counter_reset_events > previous_counter_reset_events:
            summary = str(current.get("packet_counter_reset_summary", "") or "").strip()
            self._append_performance_warning(
                "packet_counter_reset",
                "Neural packet counter reset detected"
                + (f": {summary}" if summary else ""),
                level="warning",
            )
        packet_decode_ms = float(current.get("packet_decode_ms", 0.0) or 0.0)
        writer_lag = int(current.get("writer_lag", 0) or 0)
        save_enqueue_ms = float(current.get("save_enqueue_ms", 0.0) or 0.0)
        detail_dropped_frames = int(current.get("detail_dropped_frames", 0) or 0)
        detail_throttled = bool(current.get("detail_throttled", False))
        detail_last_skip_reason = str(current.get("detail_last_skip_reason", "") or "")
        self._update_performance_status(
            packet_decode_ms=packet_decode_ms,
            save_writer_lag=writer_lag,
            serial_read_gap_ms=float(current.get("serial_read_gap_ms", 0.0) or 0.0),
            save_enqueue_ms=save_enqueue_ms,
            detail_throttled=detail_throttled,
            detail_dropped_frames=detail_dropped_frames,
            detail_last_skip_reason=detail_last_skip_reason,
        )
        decode_threshold = self._telemetry_warn_threshold("packet_decode_ms", 40.0)
        writer_lag_threshold = self._telemetry_warn_threshold("save_writer_lag", 100.0)
        save_enqueue_threshold = self._telemetry_warn_threshold("save_enqueue_ms", 20.0)
        if decode_threshold > 0 and packet_decode_ms > decode_threshold:
            self._append_performance_warning(
                "packet_decode_ms",
                f"Neural packet decode is slow: {packet_decode_ms:.1f} ms",
                level="warning",
            )
        if writer_lag_threshold > 0 and writer_lag > writer_lag_threshold:
            self._append_performance_warning(
                "save_writer_lag",
                f"EDF writer queue lag is high: {writer_lag}",
                level="warning",
            )
        if save_enqueue_threshold > 0 and save_enqueue_ms > save_enqueue_threshold:
            self._append_performance_warning(
                "save_enqueue_ms",
                f"EDF save enqueue is slow: {save_enqueue_ms:.1f} ms",
                level="warning",
            )
        previous_detail_drops = int(previous.get("detail_dropped_frames", 0) or 0)
        if detail_dropped_frames > previous_detail_drops and detail_last_skip_reason:
            self._append_performance_warning(
                "detail_dropped_frames",
                f"Detail chart frame skipped ({detail_last_skip_reason}); dropped {detail_dropped_frames} frame(s)",
                level="warning",
            )
        self._apply_detail_backpressure(self._detail_backpressure_reason(current))
        self._refresh_service_state()
        chart_sync_required = self._neural_update_requires_fast_flush(previous, current)
        self._mark_updated(sync_local_chart=chart_sync_required)
        if chart_sync_required:
            self._flush_runtime_cache_priority()

    def _on_rf_status(self, update: Dict[str, Any]) -> None:
        previous = dict(self.status.get("rf", {}))
        self.status["rf"] = merge_slot_status(self.status["rf"], update)
        self._update_mode3_battery_trial_gate(trigger_source="RF status update")
        # Passive low-battery recovery is disabled; low battery routing uses Mode0 training.
        current = dict(self.status.get("rf", {}))
        self._maybe_log_transition(
            "rf",
            previous,
            current,
            "connected",
            lambda value, _prev: f"RF serial {'connected' if value else 'disconnected'}",
        )
        self._maybe_log_transition(
            "rf",
            previous,
            current,
            "power_state",
            lambda value, _prev: f"RF power -> {value}",
        )
        self._maybe_log_transition(
            "rf",
            previous,
            current,
            "connection_state",
            lambda value, _prev: f"RF connection state -> {value}",
        )
        rf_state = str(self.status["rf"].get("connection_state", "idle") or "idle").strip().lower()
        if rf_state == "connected" and self.status["rf"].get("connected"):
            self._clear_last_error_if_matches(("rf ", "ch340", "failed to connect rf", "rf reconnect", "rf command"))
        self._refresh_service_state()
        self._mark_updated()

    def _on_habits_status(self, update: Dict[str, Any]) -> None:
        previous = dict(self.status.get("habits", {}))
        self.status["habits"] = merge_slot_status(self.status["habits"], update)
        if update:
            self._mark_history_dirty(HABITS_HISTORY_CACHE_LABELS)
        current = dict(self.status.get("habits", {}))
        if bool(current.get("connected", False)):
            self._clear_last_error_if_matches((
                "habits serial",
                "habits command",
                "failed to connect habits",
                "failed to parse habits payload",
                "serial connection lost",
                "serial connection error",
                "serial is not connected",
                "could not open port",
                "clearcommerror",
                "access is denied",
            ))
        elif int(current.get("cache_update_seq", 0) or 0) > int(previous.get("cache_update_seq", 0) or 0):
            self._clear_last_error_if_matches((
                "failed to parse habits payload",
                "habits command",
            ))
        if bool(previous.get("connected", False)) != bool(current.get("connected", False)):
            self._update_mode3_battery_trial_gate(trigger_source="Habits status update")
            current = dict(self.status.get("habits", {}))
        else:
            self._sync_mode3_trial_trigger_status()
        # Passive low-battery recovery is disabled; low battery routing uses Mode0 training.
        self._maybe_log_transition(
            "habits",
            previous,
            current,
            "connected",
            lambda value, _prev: f"Habits serial {'connected' if value else 'disconnected'}",
        )
        self._maybe_log_transition(
            "habits",
            previous,
            current,
            "paused",
            lambda value, _prev: f"Habits {'paused' if value else 'resumed'}",
        )
        self._maybe_log_transition(
            "habits",
            previous,
            current,
            "sd_download_active",
            lambda value, _prev: "Habits SD download started" if value else "Habits SD download finished",
        )
        self._maybe_log_transition(
            "habits",
            previous,
            current,
            "sd_download_status_text",
            lambda value, prev: (
                f"Habits SD status -> {value}"
                if str(value or "").strip()
                and str(value or "").strip().lower() not in {"idle", str(prev or "").strip().lower()}
                else ""
            ),
        )
        self._maybe_log_transition(
            "habits",
            previous,
            current,
            "sd_download_last_error",
            lambda value, prev: (
                f"Habits SD download error -> {value}"
                if str(value or "").strip() and str(value or "").strip() != str(prev or "").strip()
                else ""
            ),
        )
        self._log_habits_config_summary(previous, current)
        self._refresh_service_state()
        self._mark_updated()
        if int(current.get("cache_update_seq", 0) or 0) > int(previous.get("cache_update_seq", 0) or 0):
            self._push_habits_live_to_local_chart()
            self._publish_habits_live(force=False)
        if self._habits_update_requires_fast_flush(previous, current):
            self._flush_runtime_cache_priority()

    def _on_camera_status(self, update: Dict[str, Any]) -> None:
        previous = dict(self.status.get("camera", {}))
        self.status["camera"] = merge_slot_status(self.status["camera"], update)
        current = dict(self.status.get("camera", {}))
        if bool(current.get("recording", False)):
            if (
                not bool(previous.get("recording", False))
                or float(getattr(self, "_video_segment_start_monotonic", 0.0) or 0.0) <= 0.0
            ):
                self._video_segment_start_monotonic = time.monotonic()
        else:
            self._video_segment_start_monotonic = 0.0
        self._maybe_log_transition(
            "camera",
            previous,
            current,
            "open",
            lambda value, _prev: f"Camera {'opened' if value else 'closed'}",
        )
        self._maybe_log_transition(
            "camera",
            previous,
            current,
            "recording",
            lambda value, _prev: f"Camera recording {'started' if value else 'stopped'}",
        )
        self._maybe_log_transition(
            "camera",
            previous,
            current,
            "health_state",
            lambda value, _prev: f"Camera health -> {value}",
        )
        camera_state = str(self.status["camera"].get("health_state", "ok") or "ok").strip().lower()
        if camera_state == "ok":
            self._clear_last_error_if_matches(("camera", "frame", "preview"))
        self._refresh_service_state()
        self._mark_updated()

    def _record_camera_preview_telemetry(self, publish_meta: Optional[Dict[str, Any]]) -> None:
        payload_size = int((publish_meta or {}).get("payload_size", 0) or 0)
        self._update_performance_status(camera_preview_payload_bytes=payload_size)
        threshold = self._telemetry_warn_threshold("camera_preview_payload_bytes", 600000.0)
        if threshold > 0 and payload_size > threshold:
            self._append_performance_warning(
                "camera_preview_payload_bytes",
                f"Camera preview payload is large: {payload_size} bytes",
                level="warning",
            )

    def _publish_camera_preview(self) -> None:
        camera_status = self.status.get("camera", {})
        preview_enabled = bool(camera_status.get("preview_enabled", False))
        camera_id = camera_status.get("camera_id")
        preview_max_width = int(self.slot_runtime_config.get("camera_preview_max_width", 640))
        preview_quality = int(self.slot_runtime_config.get("camera_preview_jpeg_quality", 72))
        if not preview_enabled or not bool(camera_status.get("open", False)):
            publish_meta = self.camera_preview_publisher.publish(
                None,
                enabled=False,
                camera_id=camera_id,
                preview_meta={"charging_guard": dict(self.status.get("charging_guard", {}))},
            )
            self._record_camera_preview_telemetry(publish_meta)
            return
        guard_enabled = bool(self.charging_guard_config.get("enabled", False))
        payload = None
        try:
            if hasattr(self.camera, "get_preview_jpeg_bytes"):
                payload = self.camera.get_preview_jpeg_bytes(
                    max_width=preview_max_width,
                    jpeg_quality=preview_quality,
                )
        except Exception:
            payload = None

        detection = None
        if guard_enabled and hasattr(self.camera, "get_charging_guard_detection"):
            try:
                interval_s = max(
                    1.0,
                    float(self.charging_guard_config.get("check_interval_ms", 10000) or 10000) / 1000.0,
                )
                detection = self.camera.get_charging_guard_detection(max_age_seconds=max(30.0, interval_s * 2.5))
            except Exception:
                detection = None
            if detection is not None:
                self.charging_guard_last_detection = dict(detection)
                self._sync_charging_guard_status()

        if not payload:
            publish_meta = self.camera_preview_publisher.publish(
                None,
                enabled=False,
                camera_id=camera_id,
                preview_meta={
                    "charging_guard": dict(self.status.get("charging_guard", {})),
                    "detection": dict(detection or {}),
                },
            )
            self._record_camera_preview_telemetry(publish_meta)
            return

        preview_meta = {
            "charging_guard": dict(self.status.get("charging_guard", {})),
            "detection": dict(detection or {}),
        }
        publish_meta = self.camera_preview_publisher.publish(
            payload,
            enabled=bool(payload),
            camera_id=camera_id,
            preview_meta=preview_meta,
        )
        self._record_camera_preview_telemetry(publish_meta)

    def _append_battery_history(self) -> None:
        reported_rsoc = float(
            self.status["battery"].get("reported_rsoc", self.status["battery"].get("rsoc", 0.0)) or 0.0
        )
        voltage_mv = float(self.status["battery"].get("voltage", 0.0) or 0.0)
        battery_stat = int(float(self.status["battery"].get("stat", 0.0) or 0.0))
        battery_capacity_mAh = float(
            self.status["battery"].get(
                "capacity_mAh",
                getattr(self, "battery_capacity_mAh", 24.0),
            )
            or 24.0
        )
        timestamp_epoch = time.time()
        snapshot = {
            "timestamp_epoch": timestamp_epoch,
            "timestamp_iso": datetime.fromtimestamp(timestamp_epoch).isoformat(),
            "rsoc": reported_rsoc,
            "voltage_mv": int(round(voltage_mv)),
            "battery_stat": battery_stat,
            "voltage": voltage_mv,
            "stat": battery_stat,
            "capacity_mAh": battery_capacity_mAh,
            "bq25176_status_text": str(self.status["battery"].get("bq25176_status_text", "") or ""),
            "bq25176_charge_state_text": str(self.status["battery"].get("bq25176_charge_state_text", "") or ""),
            "bq25176_power_state_text": str(self.status["battery"].get("bq25176_power_state_text", "") or ""),
            "bq25176_fault_possible": bool(self.status["battery"].get("bq25176_fault_possible", False)),
            "bq25176_fault_reason": str(self.status["battery"].get("bq25176_fault_reason", "") or ""),
            "rf_status": 2 if self.status["rf"].get("power_state") == "on" else 1,
            "mode": str(self.status["neural"].get("active_mode", "Idle")),
            "trial_paused": bool(self.status["habits"].get("paused", False)),
        }
        self.status["battery"]["timestamp_epoch"] = float(timestamp_epoch)
        self.status["battery"]["timestamp_iso"] = snapshot["timestamp_iso"]
        self._publish_battery_latest(snapshot)
        self._prune_battery_history(timestamp_epoch)
        if self.battery_history:
            last_record = self.battery_history[-1]
            same_state = (
                float(last_record.get("rsoc", -1.0) or 0.0) == float(snapshot["rsoc"])
                and int(float(last_record.get("battery_stat", last_record.get("stat", -1)) or 0)) == battery_stat
                and int(float(last_record.get("voltage_mv", last_record.get("voltage", -1)) or 0)) == int(snapshot["voltage_mv"])
                and int(last_record.get("rf_status", 0) or 0) == int(snapshot["rf_status"])
                and str(last_record.get("mode", "") or "") == str(snapshot["mode"])
                and bool(last_record.get("trial_paused", False)) == bool(snapshot["trial_paused"])
                and str(last_record.get("bq25176_status_text", "") or "") == str(snapshot["bq25176_status_text"])
            )
            delta_t = timestamp_epoch - float(last_record.get("timestamp_epoch", 0.0) or 0.0)
            if same_state and delta_t < 60.0:
                return
        if self._last_battery_snapshot == snapshot:
            return
        self._last_battery_snapshot = dict(snapshot)
        self.battery_history.append(snapshot)
        self._prune_battery_history(timestamp_epoch)
        self._mark_history_dirty("battery_history")

    def _prune_battery_history(self, now_epoch: Optional[float] = None) -> None:
        try:
            reference_epoch = float(time.time() if now_epoch is None else now_epoch)
        except Exception:
            reference_epoch = time.time()
        cutoff_epoch = reference_epoch - float(BATTERY_HISTORY_RETENTION_SECONDS)
        while self.battery_history:
            try:
                first_epoch = float(self.battery_history[0].get("timestamp_epoch", 0.0) or 0.0)
            except Exception:
                first_epoch = 0.0
            if first_epoch >= cutoff_epoch:
                break
            self.battery_history.popleft()

    def _battery_history_payload_for_cache(self) -> list:
        self._prune_battery_history(time.time())
        return [dict(item) if isinstance(item, dict) else item for item in self.battery_history]

    def _publish_battery_latest(self, snapshot: Dict[str, Any]) -> None:
        try:
            interval_ms = float(self.slot_runtime_config.get("battery_latest_flush_ms", 2000) or 0.0)
        except Exception:
            interval_ms = 2000.0
        interval_s = max(0.0, interval_ms / 1000.0)
        now_mono = time.monotonic()
        last_flush_mono = float(getattr(self, "_last_battery_latest_flush_monotonic", 0.0) or 0.0)
        if interval_s > 0.0 and last_flush_mono > 0.0 and (now_mono - last_flush_mono) < interval_s:
            return
        try:
            payload = dict(snapshot)
            payload["sequence"] = self.runtime_cache.next_sequence("battery_latest")
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if serialized == str(getattr(self, "_last_battery_latest_payload", "") or ""):
                return
            self.runtime_cache.write_battery_latest(payload)
            self._last_battery_latest_payload = serialized
            self._last_battery_latest_flush_monotonic = now_mono
            self._note_runtime_cache_write_success("battery_latest")
        except Exception as exc:
            self._note_runtime_cache_write_failure("battery_latest", exc)

    def _publish_habits_live(self, force: bool = False) -> None:
        try:
            interval_ms = float(self.slot_runtime_config.get("habits_live_flush_ms", 1000) or 0.0)
        except Exception:
            interval_ms = 1000.0
        interval_s = max(0.0, interval_ms / 1000.0)
        now_mono = time.monotonic()
        last_flush_mono = float(getattr(self, "_last_habits_live_flush_monotonic", 0.0) or 0.0)
        if (
            not force
            and interval_s > 0.0
            and last_flush_mono > 0.0
            and (now_mono - last_flush_mono) < interval_s
        ):
            return
        getter = getattr(self.habits, "get_live_updates_since", None)
        if not callable(getter):
            return
        try:
            tail_limit = max(1, int(self.slot_runtime_config.get("habits_live_update_tail", 20000) or 20000))
        except Exception:
            tail_limit = 20000
        try:
            updates = list(getter(0, limit=tail_limit))
        except Exception as exc:
            self._note_runtime_cache_write_failure("habits_live", exc)
            return
        if not updates:
            return
        try:
            last_update_sequence = max(
                int(item.get("sequence", 0) or 0)
                for item in updates
                if isinstance(item, dict)
            )
        except Exception:
            last_update_sequence = int(getattr(self, "_last_habits_live_sequence", 0) or 0)
        if (
            not force
            and int(last_update_sequence) <= int(getattr(self, "_last_habits_live_sequence", 0) or 0)
            and str(getattr(self, "_last_habits_live_payload", "") or "")
        ):
            return
        self._last_habits_live_sequence = int(last_update_sequence)
        try:
            payload = {
                "sequence": self.runtime_cache.next_sequence("habits_live"),
                "last_update_sequence": int(last_update_sequence),
                "timestamp_epoch": time.time(),
                "updates": updates,
            }
            serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if serialized == str(getattr(self, "_last_habits_live_payload", "") or ""):
                return
            self.runtime_cache.write_habits_live(payload)
            self._last_habits_live_payload = serialized
            self._last_habits_live_flush_monotonic = now_mono
            self._note_runtime_cache_write_success("habits_live")
        except Exception as exc:
            self._note_runtime_cache_write_failure("habits_live", exc)

    def _push_habits_live_to_local_chart(self) -> None:
        chart = self.local_chart_window
        handler = getattr(chart, "handle_habits_live_payload", None)
        if chart is None or not callable(handler):
            return
        getter = getattr(self.habits, "get_live_updates_since", None)
        if not callable(getter):
            return
        try:
            tail_limit = max(1, int(self.slot_runtime_config.get("habits_live_update_tail", 20000) or 20000))
        except Exception:
            tail_limit = 20000
        try:
            updates = list(
                getter(
                    int(getattr(self, "_last_local_chart_habits_live_sequence", 0) or 0),
                    limit=tail_limit,
                )
            )
        except Exception as exc:
            self._append_performance_warning(
                "local_chart_habits_live",
                f"Local chart Habits live update fetch failed: {exc}",
                level="warning",
            )
            return
        if not updates:
            return
        try:
            last_sequence = max(
                int(item.get("sequence", 0) or 0)
                for item in updates
                if isinstance(item, dict)
            )
        except Exception:
            last_sequence = int(getattr(self, "_last_local_chart_habits_live_sequence", 0) or 0)
        payload = {
            "last_update_sequence": int(last_sequence),
            "timestamp_epoch": time.time(),
            "updates": updates,
        }
        try:
            handler(payload)
            self._last_local_chart_habits_live_sequence = max(
                int(getattr(self, "_last_local_chart_habits_live_sequence", 0) or 0),
                int(last_sequence),
            )
        except Exception as exc:
            self._append_performance_warning(
                "local_chart_habits_live",
                f"Local chart Habits live update failed: {exc}",
                level="warning",
            )

    def _append_packet_loss_history(self, update: Dict[str, Any]) -> None:
        packet_metrics_seq = update.get("packet_metrics_seq", None)
        try:
            packet_metrics_seq = int(packet_metrics_seq)
        except Exception:
            packet_metrics_seq = None
        if packet_metrics_seq is not None:
            if packet_metrics_seq <= int(self._last_packet_metrics_seq):
                self._prune_packet_loss_history(time.time())
                return
            self._last_packet_metrics_seq = packet_metrics_seq

        packet_loss = max(0, int(update.get("packet_loss_batch", update.get("packet_loss", 0)) or 0))
        packet_count = max(0, int(update.get("packet_count_batch", update.get("packet_count", 0)) or 0))
        if packet_loss <= 0 and packet_count <= 0:
            self._prune_packet_loss_history(time.time())
            return
        self.packet_loss_history.append({
            "timestamp_epoch": time.time(),
            "packet_loss": packet_loss,
            "packet_count": packet_count,
        })
        self._prune_packet_loss_history(time.time())

    def _prune_packet_loss_history(self, now_epoch: float) -> None:
        cutoff = float(now_epoch) - float(ROLLUP_WINDOW_SECONDS)
        while self.packet_loss_history:
            head = self.packet_loss_history[0]
            if float(head.get("timestamp_epoch", 0.0) or 0.0) >= cutoff:
                break
            self.packet_loss_history.popleft()

    def _refresh_health_rollups(self) -> None:
        now_epoch = time.time()
        self._prune_packet_loss_history(now_epoch)
        rollup = compute_packet_loss_rollup(
            self.packet_loss_history,
            now_epoch=now_epoch,
            window_seconds=ROLLUP_WINDOW_SECONDS,
        )
        packet_loss_percent = float(rollup["percent"])
        expected_packets = int(rollup["expected_packets"])
        self.status["neural"].update({
            "packet_loss_percent_1h": packet_loss_percent,
            "packet_loss_expected_1h": expected_packets,
            "packet_loss_state": classify_packet_loss_state(packet_loss_percent, expected_packets),
        })
        battery = self.status.get("battery", {})
        self.status["battery"].update({
            "health_state": classify_battery_state(
                battery.get("reported_rsoc", battery.get("rsoc", 0.0)),
                battery.get("voltage", 0.0),
                low_threshold_percent=BATTERY_LOW_THRESHOLD_PERCENT,
            ),
            "low_threshold_percent": float(BATTERY_LOW_THRESHOLD_PERCENT),
        })
        self._mark_updated()

    @staticmethod
    def _detail_payload_stream_key(payload: object) -> str:
        try:
            header = payload[0] if isinstance(payload, (list, tuple)) and payload else None
            mode = int(header[0]) if isinstance(header, (list, tuple)) and header else -1
        except Exception:
            mode = -1
        if mode == 0:
            return "lfp"
        if mode == 1:
            return "spike"
        if mode == 2:
            return "mode2"
        return "unknown"

    @staticmethod
    def _detail_payload_queue_length(value: object) -> int:
        if isinstance(value, deque):
            return len(value)
        if value is None:
            return 0
        return 1

    @staticmethod
    def _detail_pending_frame_count(pending_payloads: object) -> int:
        if not isinstance(pending_payloads, dict):
            return 0
        return sum(
            SlotService._detail_payload_queue_length(value)
            for value in pending_payloads.values()
        )

    @staticmethod
    def _detail_payload_queue_for_stream(pending_payloads: Dict[str, object], stream_key: str) -> deque:
        existing = pending_payloads.get(stream_key)
        if isinstance(existing, deque):
            return existing
        queue_for_stream = deque()
        if existing is not None:
            queue_for_stream.append(existing)
        pending_payloads[stream_key] = queue_for_stream
        return queue_for_stream

    @staticmethod
    def _merge_detail_value(first: object, second: object) -> object:
        if isinstance(first, list) and isinstance(second, list):
            if len(first) == len(second) and any(isinstance(item, list) for item in first + second):
                return [
                    SlotService._merge_detail_value(left, right)
                    for left, right in zip(first, second)
                ]
            return list(first) + list(second)
        return second

    @staticmethod
    def _merge_detail_payloads(payloads: list) -> Optional[object]:
        if not payloads:
            return None
        merged = payloads[0]
        for payload in payloads[1:]:
            if not isinstance(merged, list) or not isinstance(payload, list):
                merged = payload
                continue
            max_len = max(len(merged), len(payload))
            next_payload = []
            for index in range(max_len):
                if index == 0 and index < len(merged):
                    next_payload.append(merged[index])
                elif index >= len(merged):
                    next_payload.append(payload[index])
                elif index >= len(payload):
                    next_payload.append(merged[index])
                else:
                    next_payload.append(SlotService._merge_detail_value(merged[index], payload[index]))
            merged = next_payload
        return merged

    @staticmethod
    def _pop_next_detail_payload(pending_payloads: Dict[str, object], stream_key: str) -> Optional[object]:
        if stream_key not in pending_payloads:
            return None
        value = pending_payloads.get(stream_key)
        if isinstance(value, deque):
            if not value:
                pending_payloads.pop(stream_key, None)
                return None
            payloads = list(value)
            pending_payloads.pop(stream_key, None)
            return SlotService._merge_detail_payloads(payloads)
        pending_payloads.pop(stream_key, None)
        return value

    def _on_detail_payload(self, payload: object) -> None:
        attached = bool(self.status.get("detail_attached", False))
        if not attached:
            return
        now_mono = time.monotonic()
        stream_key = self._detail_payload_stream_key(payload)
        last_by_stream = getattr(self, "_last_detail_publish_monotonic_by_stream", None)
        if not isinstance(last_by_stream, dict):
            last_by_stream = {}
            self._last_detail_publish_monotonic_by_stream = last_by_stream
        last_publish = float(
            last_by_stream.get(
                stream_key,
                float(self._last_detail_publish_monotonic or 0.0) if not last_by_stream else 0.0,
            )
            or 0.0
        )
        telemetry = dict(self.status["neural"].get("telemetry", {}) or {})
        payload_bytes = max(1, int(sys.getsizeof(payload)))
        max_payload_bytes = max(0, int(self.slot_runtime_config.get("detail_max_payload_bytes", 0) or 0))
        if max_payload_bytes > 0 and payload_bytes > max_payload_bytes:
            dropped_frames = self._record_detail_skip("payload_budget")
            self._append_performance_warning(
                "detail_payload_budget",
                f"Detail payload skipped: {payload_bytes} bytes exceeds configured budget "
                f"(dropped {dropped_frames} frame(s)); neural read/EDF save keep priority",
                level="warning",
            )
            return
        pending_payloads = getattr(self, "_pending_detail_payload_by_stream", None)
        if not isinstance(pending_payloads, dict):
            pending_payloads = {}
            self._pending_detail_payload_by_stream = pending_payloads
        stream_queue = SlotService._detail_payload_queue_for_stream(pending_payloads, stream_key)
        stream_queue.append(payload)
        telemetry["detail_queued_frames"] = int(telemetry.get("detail_queued_frames", 0) or 0) + 1
        telemetry["detail_pending_frames"] = SlotService._detail_pending_frame_count(pending_payloads)
        try:
            if self.local_chart_window is not None and not self.detail_render_timer.isActive():
                self.detail_render_timer.start()
        except Exception:
            pass
        previous_publish_monotonic = float(last_publish or 0.0)
        interval_ms = max(0.0, (now_mono - previous_publish_monotonic) * 1000.0) if previous_publish_monotonic > 0.0 else 0.0
        last_by_stream[stream_key] = now_mono
        self._last_detail_publish_monotonic = now_mono
        telemetry.update({
            "detail_payload_bytes": payload_bytes,
            "detail_payload_interval_ms": interval_ms,
            "detail_pending_frames": SlotService._detail_pending_frame_count(pending_payloads),
            "detail_throttled": False,
            "detail_last_skip_reason": "",
        })
        self.status["neural"]["detail_throttled"] = False
        self.status["neural"]["detail_last_skip_reason"] = ""
        self.status["neural"]["telemetry"] = telemetry
        self._update_performance_status(
            detail_payload_bytes=payload_bytes,
            detail_payload_interval_ms=interval_ms,
            detail_throttled=False,
            detail_dropped_frames=int(telemetry.get("detail_dropped_frames", self.status["neural"].get("detail_dropped_frames", 0)) or 0),
            detail_last_skip_reason="",
        )
        payload_threshold = self._telemetry_warn_threshold("detail_payload_bytes", 8000000.0)
        if payload_threshold > 0 and payload_bytes > payload_threshold:
            self._append_performance_warning(
                "detail_payload_bytes",
                f"Detail payload is large: {payload_bytes} bytes",
                level="warning",
            )
        # Detail frames are high-frequency display data. Do not call
        # _mark_updated() here because it pushes a full status/control sync back
        # into the local chart on every frame, which can recursively stall Qt.
        self.status["last_update_epoch"] = time.time()
        self._status_dirty = True

    def _drain_detail_payloads_to_chart(self) -> None:
        if not bool(self.status.get("detail_attached", False)):
            self._pending_detail_payload_by_stream.clear()
            try:
                self.detail_render_timer.stop()
            except Exception:
                pass
            return
        chart = self.local_chart_window
        if chart is None:
            self._pending_detail_payload_by_stream.clear()
            try:
                self.detail_render_timer.stop()
            except Exception:
                pass
            return
        pending_payloads = getattr(self, "_pending_detail_payload_by_stream", None)
        if not isinstance(pending_payloads, dict) or not pending_payloads:
            return
        render_started = time.monotonic()
        rendered = 0
        base_order = ["lfp", "spike", "mode2", "unknown"]
        try:
            start_index = int(getattr(self, "_detail_render_round_robin_index", 0) or 0) % len(base_order)
        except Exception:
            start_index = 0
        ordered_keys = base_order[start_index:] + base_order[:start_index]
        rendered_keys = []
        detail_render_metrics = {}
        for key in ordered_keys + [key for key in list(pending_payloads.keys()) if key not in ordered_keys]:
            if rendered >= int(self._detail_render_max_frames_per_tick):
                break
            if key not in pending_payloads:
                continue
            payload = SlotService._pop_next_detail_payload(pending_payloads, key)
            if payload is None:
                continue
            try:
                chart.handle_detail_payload(payload)
                rendered += 1
                rendered_keys.append(str(key))
                chart_metrics = getattr(chart, "last_detail_render_metrics", None)
                if isinstance(chart_metrics, dict):
                    detail_render_metrics = dict(chart_metrics)
            except Exception as exc:
                self._append_performance_warning(
                    "local_chart_payload",
                    f"Local chart payload render failed: {exc}",
                    level="warning",
                )
        if rendered_keys:
            for key in reversed(rendered_keys):
                if key in base_order:
                    self._detail_render_round_robin_index = (base_order.index(key) + 1) % len(base_order)
                    break
        render_ms = max(0.0, (time.monotonic() - render_started) * 1000.0)
        pending_count = SlotService._detail_pending_frame_count(pending_payloads)
        self._update_performance_status(
            detail_render_ms=render_ms,
            detail_render_epoch=time.time(),
            detail_pending_frames=pending_count,
            detail_render_stream=",".join(rendered_keys),
            detail_render_metrics=detail_render_metrics,
        )
        neural_status = dict(self.status.get("neural", {}) or {})
        telemetry = dict(neural_status.get("telemetry", {}) or {})
        telemetry["detail_pending_frames"] = pending_count
        telemetry["detail_backlog_frames"] = pending_count
        neural_status["telemetry"] = telemetry
        self.status["neural"] = neural_status
        if pending_count:
            self._update_performance_status(
                detail_backlog_frames=pending_count,
                detail_deferred_frames=pending_count,
            )

    def _confirm_rf_power_state_for_habits(self, enabled: bool, *, force_command: bool = False) -> bool:
        desired_state = "on" if enabled else "off"
        action_text = "on" if enabled else "off"
        if not bool(self.rf.status.get("connected", False)):
            self._append_system_log(
                f"RF control serial not connected; cannot turn RF power {action_text}",
                level="warning",
            )
            return False
        current_state = str(self.rf.status.get("power_state", "") or "").strip().lower()
        if current_state == desired_state and not bool(force_command):
            self._append_system_log(f"RF power {action_text} already confirmed", level="info")
            return True
        fast_set_power = getattr(self.rf, "set_power_for_habits_confirmation", None)
        if callable(fast_set_power):
            confirmed = bool(fast_set_power(enabled))
        else:
            confirmed = bool(self.rf.set_power(enabled))
        if not confirmed:
            fast_query = getattr(self.rf, "query_status_fast", None)
            try:
                if callable(fast_query):
                    fast_query(emit_error=False)
                else:
                    self.rf.query_status(emit_error=False)
            except TypeError:
                self.rf.query_status()
            except Exception:
                pass
            confirmed = str(self.rf.status.get("power_state", "") or "").strip().lower() == desired_state
        if confirmed:
            self._append_system_log(f"RF power {action_text} confirmed", level="info")
        else:
            self._append_system_log(
                f"RF power {action_text} confirmation unavailable",
                level="warning",
            )
        return confirmed

    def _ensure_rf_off_before_habits_resume(self) -> bool:
        self._drop_pending_rf_tasks(("query", "power_on"))
        confirmed = self._confirm_rf_power_state_for_habits(False, force_command=True)
        if confirmed:
            self._append_system_log("RF off confirmed before Resume Habits", level="info")
            return True
        self._append_system_log(
            "Resume Habits blocked because RF OFF was not confirmed",
            level="warning",
        )
        return False

    def _send_habits_pause_toggle_with_rf_guard(self) -> bool:
        currently_paused = bool(self.status.get("habits", {}).get("paused", False))
        if currently_paused and not self._ensure_rf_off_before_habits_resume():
            return False
        return bool(self.habits.send_pause())

    def _drop_pending_rf_tasks(self, actions: Tuple[str, ...]) -> int:
        normalized_actions = {
            str(action or "").strip().lower()
            for action in tuple(actions or ())
            if str(action or "").strip()
        }
        if not normalized_actions:
            return 0
        dropped = 0
        mutex = getattr(self._rf_task_queue, "mutex", None)
        queue_items = getattr(self._rf_task_queue, "queue", None)
        if mutex is None or queue_items is None:
            return 0
        with mutex:
            retained = []
            while queue_items:
                item = queue_items.popleft()
                action_name = ""
                if isinstance(item, tuple) and item:
                    action_name = str(item[0] or "").strip().lower()
                if action_name in normalized_actions:
                    dropped += 1
                    continue
                retained.append(item)
            queue_items.extend(retained)
        if dropped > 0:
            self._append_system_log(
                f"Dropped {dropped} pending RF task(s) before Habits ESA handling",
                level="info",
            )
            if not bool(self._rf_worker_busy) and self._rf_task_queue.empty():
                self._set_rf_command_busy(False, "")
        return dropped

    def _on_habits_mode_switch(self, command: str) -> None:
        command = str(command or "").strip().upper()
        self._append_system_log(f"Habits mode switch -> {command}", level="info")
        if command == "ESA":
            self._append_system_log("ESA received", level="info")
            self._habits_mode_switch_in_flight = True
            self._last_habits_esa_monotonic = time.monotonic()
            try:
                low_mode0_training = bool(
                    self.status.get("habits", {}).get("low_battery_mode0_training_target", False)
                )
                if low_mode0_training:
                    reason = str(
                        self.status.get("habits", {}).get("low_battery_mode0_training_reason", "")
                        or "low battery"
                    )
                    self._set_low_battery_mode0_training_active(True, reason)
                    self._drop_pending_rf_tasks(("query", "power_off"))
                    if bool(getattr(self, "_low_battery_rf_manual_off", False)):
                        self._append_system_log(
                            "Manual RF off override respected during low-battery Mode0 training route",
                            level="info",
                        )
                    else:
                        rf_on_confirmed = self._confirm_rf_power_state_for_habits(True)
                        if rf_on_confirmed:
                            self._append_system_log(
                                "RF on confirmed for low-battery Mode0 training route",
                                level="info",
                            )
                        else:
                            self._append_system_log(
                                "RF ON confirmation unavailable for low-battery Mode0 training route; continuing without blocking Habits",
                                level="warning",
                            )
                    if self.habits.send_rf_off_ready():
                        self._append_system_log(
                            "F sent for low-battery Mode0 training route; RF kept ON",
                            level="info",
                        )
                    else:
                        self._append_system_log(
                            "Failed to send Habits F for low-battery Mode0 training route",
                            level="warning",
                        )
                    save_ok = bool(self.neural.set_mode0_training_save(True))
                    if save_ok:
                        self._sync_camera_recording_with_save("lfp", True)
                    else:
                        self._append_system_log(
                            "Mode0 training save enable failed during low-battery Habits ESA switch",
                            level="warning",
                        )
                    mode_ok = bool(self.neural.set_mode(0))
                    if not mode_ok:
                        self._append_system_log(
                            "Failed to keep neural sample mode in Mode0 LFP+MAND+Raw during low-battery Habits ESA switch",
                            level="warning",
                        )
                        if save_ok and self.neural.set_mode0_training_save(False):
                            self._sync_camera_recording_with_save("lfp", False)
                            self._set_low_battery_mode0_training_active(False, "mode0 switch failed")
                            self._append_system_log(
                                "Mode0 training save rolled back because Mode0 switch failed",
                                level="warning",
                            )
                else:
                    self._drop_pending_rf_tasks(("query",))
                    rf_off_confirmed = self._confirm_rf_power_state_for_habits(False)
                    if rf_off_confirmed:
                        self._append_system_log("RF off confirmed", level="info")
                        if self.habits.send_rf_off_ready():
                            self._append_system_log("F sent", level="info")
                        else:
                            self._append_system_log(
                                "Failed to send Habits F after RF OFF confirmation",
                                level="warning",
                            )
                    else:
                        self._append_system_log(
                            "RF OFF confirmation unavailable, skip sending Habits F",
                            level="warning",
                        )
                    self._reset_charging_guard("RF off")
                    if bool(self.status.get("habits", {}).get("mode3_trial_trigger_paused", False)):
                        current_battery = int(round(float(
                            self.status.get("battery", {}).get(
                                "reported_rsoc",
                                self.status.get("battery", {}).get("rsoc", 0.0),
                            ) or 0.0
                        )))
                        self._append_system_log(
                            f"Battery RSOC is {current_battery}%; firmware reports low-battery pause, but normal ESA route remains enabled.",
                            level="info",
                        )
                    save_ok = bool(self.neural.set_save_mode("mode2", True))
                    if save_ok:
                        self._sync_camera_recording_with_save("mode2", True)
                    else:
                        self._append_system_log(
                            "Mode2 save enable failed during Habits ESA switch",
                            level="warning",
                        )
                    mode_ok = bool(self.neural.set_mode(2))
                    if not mode_ok:
                        self._append_system_log(
                            "Failed to switch neural sample mode to Mode2 during Habits ESA switch",
                            level="warning",
                        )
                        if save_ok and self.neural.set_save_mode("mode2", False):
                            self._sync_camera_recording_with_save("mode2", False)
                            self._append_system_log(
                                "Mode2 save rolled back because ESA mode switch failed",
                                level="warning",
                            )
            finally:
                self._habits_mode_switch_in_flight = False
        elif command == "LFP":
            now_mono = time.monotonic()
            if (
                self._last_habits_esa_monotonic > 0.0
                and self._last_habits_trial_started_monotonic < self._last_habits_esa_monotonic
                and (now_mono - self._last_habits_esa_monotonic) <= NEURAL_READER_TIMEOUT_SUSPECT_WINDOW_SECONDS
            ):
                self._append_system_log(
                    "NeuralReader timeout suspected: LFP returned before trial start",
                    level="warning",
                )
            save_ok = bool(self.neural.set_save_mode("lfp", True))
            if save_ok:
                self._sync_camera_recording_with_save("lfp", True)
                if bool(self.status.get("habits", {}).get("low_battery_mode0_training_active", False)):
                    self._set_low_battery_mode0_training_active(False, "Habits returned to LFP")
            else:
                self._append_system_log(
                    "Mode0 save enable failed during Habits LFP switch",
                    level="warning",
                )
            mode_ok = bool(self.neural.set_mode(0))
            if not mode_ok:
                self._append_system_log(
                    "Failed to switch neural sample mode to Mode0 LFP+MAND+Raw during Habits LFP switch",
                    level="warning",
                )
                if save_ok and self.neural.set_save_mode("lfp", False):
                    self._sync_camera_recording_with_save("lfp", False)
                    self._append_system_log(
                        "Mode0 save rolled back because Mode0 switch failed",
                        level="warning",
                    )
            else:
                try:
                    time.sleep(0.1)
                except Exception:
                    pass
            self._confirm_rf_power_state_for_habits(True)
        self._mark_updated()
        self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _on_habits_trial_started(self, trial_num: int) -> None:
        try:
            normalized_trial_num = max(0, int(trial_num))
        except Exception:
            normalized_trial_num = 0
        self._last_habits_trial_started_monotonic = time.monotonic()
        self._reset_charging_guard(f"trial {normalized_trial_num} start")
        if not bool(self.neural.trigger_alignment(normalized_trial_num)):
            self._append_system_log(
                f"Neural alignment trigger failed for Habits trial {normalized_trial_num}",
                level="warning",
            )
            self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)

    def _auto_start_from_profile(self) -> None:
        if bool(self.profile.get("auto_connect_main_serial", False)):
            self.neural.connect_port()
        if bool(self.profile.get("auto_connect_rf", False)):
            self.rf.connect_port()
        if bool(self.profile.get("auto_connect_habits", False)):
            self.habits.connect_port()

    @staticmethod
    def _command_priority(command: Dict[str, Any]) -> int:
        if not isinstance(command, dict):
            return 100
        command_type = str(command.get("type", "") or "").strip()
        if command_type == "shutdown":
            return 0
        if command_type == "detail_attach":
            return 1
        if command_type in {"connect_subsystem", "disconnect_subsystem"}:
            subsystem = str(command.get("subsystem", "") or "").strip().lower()
            if subsystem in {"neural", "habits", "rf"}:
                return 2
        if command_type in {"rf_query", "rf_power"}:
            return 2
        if command_type in {
            "habits_serial",
            "habits_sd_download",
            "habits_pause_toggle",
            "habits_read_all",
            "habits_time_sync",
            "set_habits_data_directory",
            "set_habits_start_date",
        }:
            return 3
        return 10

    def _drain_commands(self) -> None:
        drained_commands = []
        self._last_command_poll_summary = ""
        scan_limit = max(1, int(getattr(self, "_command_poll_scan_limit", 64) or 64))
        priority_queue = getattr(self, "priority_command_queue", None)
        if priority_queue is not None:
            while len(drained_commands) < scan_limit:
                try:
                    command = priority_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception:
                    break
                if isinstance(command, dict):
                    drained_commands.append(command)
        while len(drained_commands) < scan_limit:
            try:
                command = self.command_queue.get_nowait()
            except queue.Empty:
                break
            except Exception:
                return
            if isinstance(command, dict):
                drained_commands.append(command)
        if not drained_commands:
            return
        ordered_commands = [
            command
            for _, command in sorted(
                enumerate(drained_commands),
                key=lambda item: (self._command_priority(item[1]), item[0]),
            )
        ]
        max_commands = max(1, int(getattr(self, "_command_poll_max_commands_per_tick", 8) or 8))
        budget_ms = max(0.0, float(getattr(self, "_command_poll_budget_ms", 0.0) or 0.0))
        started = time.monotonic()
        processed_types = []
        processed_details = []
        remaining_commands = []
        for index, command in enumerate(ordered_commands):
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            if processed_types and (len(processed_types) >= max_commands or (budget_ms > 0.0 and elapsed_ms >= budget_ms)):
                remaining_commands.extend(ordered_commands[index:])
                break
            command_type = "unknown"
            command_started = time.monotonic()
            try:
                if isinstance(command, dict):
                    command_type = str(command.get("type", "") or "unknown")
                else:
                    command_type = "unknown"
                processed_types.append(command_type)
                self._handle_command(command)
            except Exception as exc:
                if isinstance(command, dict):
                    command_type = str(command.get("type", "") or command_type or "")
                self._set_last_error(f"Command {command_type or 'unknown'} failed: {exc}")
                self._append_system_log(
                    f"Command {command_type or 'unknown'} failed: {exc}",
                    level="error",
                )
            finally:
                command_duration_ms = max(0.0, (time.monotonic() - command_started) * 1000.0)
                processed_details.append((command_type or "unknown", command_duration_ms))
        for command in remaining_commands:
            try:
                target_queue = (
                    self.priority_command_queue
                    if self._command_priority(command) <= 3 and self.priority_command_queue is not None
                    else self.command_queue
                )
                target_queue.put(command, block=False)
            except Exception as exc:
                self._set_last_error(f"Failed to requeue command: {exc}")
                break
        if processed_types or remaining_commands:
            command_detail_parts = [
                f"{command_type}:{duration_ms:.1f}ms"
                for command_type, duration_ms in processed_details[:6]
            ]
            self._last_command_poll_summary = (
                f"processed={len(processed_types)}"
                + (f" types={','.join(processed_types[:6])}" if processed_types else "")
                + (f" command_ms={'|'.join(command_detail_parts)}" if command_detail_parts else "")
                + (f" deferred={len(remaining_commands)}" if remaining_commands else "")
            )

    def _is_habits_command_ready(self) -> bool:
        if not hasattr(self.habits, "serial_worker"):
            return True
        return bool(self.status.get("habits", {}).get("connected", False)) and bool(
            self.habits.status.get("connected", False)
        )

    def _queue_habits_command_retry(self, command: Dict[str, Any], reason: str) -> Optional[bool]:
        retry_count = int(command.get("_habits_retry_count", 0) or 0)
        action = str(command.get("action", command.get("type", "habits")) or "habits").strip() or "habits"
        if retry_count >= HABITS_COMMAND_RETRY_LIMIT:
            self._on_error(
                f"Habits command '{action}' failed after waiting for serial connection: {reason}"
            )
            return False
        retry_command = dict(command)
        retry_command["_habits_retry_count"] = retry_count + 1
        if getattr(self.habits, "serial_worker", None) is None:
            try:
                self.habits.connect_port()
            except Exception as exc:
                self._on_error(f"Habits reconnect before '{action}' failed: {exc}")
                return False
        delay_ms = min(1200, HABITS_COMMAND_RETRY_BASE_DELAY_MS * (retry_count + 1))
        if retry_count == 0:
            self._append_system_log(
                f"Habits command '{action}' waiting for serial connection",
                level="warning",
            )

        def _retry() -> None:
            try:
                target_queue = (
                    self.priority_command_queue
                    if getattr(self, "priority_command_queue", None) is not None
                    else self.command_queue
                )
                target_queue.put(retry_command, block=False)
            except Exception as exc:
                self._on_error(f"Failed to retry Habits command '{action}': {exc}")

        QTimer.singleShot(delay_ms, _retry)
        return None

    def _handle_habits_serial_command(self, action: str, command: Dict[str, Any]) -> Optional[bool]:
        action = str(action or "").strip().lower()
        if not self._is_habits_command_ready():
            return self._queue_habits_command_retry(command, "not connected yet")
        if action == "reward":
            return bool(self.habits.send_reward(
                int(command.get("left", 30) or 0),
                int(command.get("middle", 30) or 0),
                int(command.get("right", 30) or 0),
            ))
        if action == "light":
            return bool(self.habits.send_light(
                int(command.get("low", 1) or 0),
                int(command.get("high", 255) or 0),
            ))
        if action == "protocol":
            return bool(self.habits.send_protocol(int(command.get("protocol", 0) or 0)))
        if action == "block_switch":
            return bool(self.habits.send_block_switch_mode())
        if action == "protocol_flow_update":
            return bool(self.habits.replace_protocol_flow(dict(command.get("flow", {}) or {})))
        if action == "protocol_flow_conditions":
            return bool(self.habits.set_current_protocol_flow_conditions(dict(command.get("conditions", {}) or {})))
        if action == "protocol_flow_set_current":
            return bool(self.habits.set_current_protocol_from_flow(int(command.get("protocol", 0) or 0)))
        if action == "post_training_protocol0":
            return bool(self.habits.send_post_training_protocol0())
        if action == "inter_block_interval":
            return bool(self.habits.send_inter_block_interval(int(command.get("minutes", 60) or 0)))
        if action == "cap_snapshot":
            return bool(self.habits.send_cap_snapshot())
        if action == "pause_toggle":
            return bool(self._send_habits_pause_toggle_with_rf_guard())
        if action == "handshake":
            return bool(self.habits.send_handshake())
        if action == "time_sync":
            return bool(self.habits.send_time_sync())
        if action == "read_all":
            return bool(self.habits.send_read_all())
        if action == "rf_off_ready":
            return bool(self.habits.send_rf_off_ready())
        if action == "charging_warning":
            return bool(
                self.habits.send_charging_warning(
                    int(command.get("duration_ms", 3000) or 0),
                    bool(command.get("fan_enabled", True)),
                )
            )
        if action == "charging_warning_stop":
            return bool(self.habits.stop_charging_warning())
        raise ValueError(f"Unsupported habits action: {action}")

    def _handle_command(self, command: Dict[str, Any]) -> None:
        if not isinstance(command, dict):
            return
        command_type = str(command.get("type", "")).strip()
        if not command_type:
            return

        if command_type == "shutdown":
            self._append_system_log("Shutdown requested", level="info")
            self.shutdown()
            app = QCoreApplication.instance()
            if app is not None:
                app.quit()
            return

        if command_type == "reload_profile":
            self.profile = load_slot_profile(self.profile_path)
            self.runtime_cache.write_last_profile(self.profile)
            self.battery_capacity_mAh = self._profile_battery_capacity_mAh(self.profile)
            self.status["battery"]["capacity_mAh"] = float(self.battery_capacity_mAh)
            self.neural.profile = dict(self.profile)
            self.rf.profile = dict(self.profile)
            self.habits.profile = dict(self.profile)
            self.habits.refresh_profile_metadata()
            self.camera.profile = dict(self.profile)
            self.charging_guard_config = _default_charging_guard_config()
            if isinstance(self.profile.get("charging_guard"), dict):
                self.charging_guard_config.update(dict(self.profile.get("charging_guard") or {}))
            self.status["neural"] = merge_slot_status(self.status["neural"], {
                "port": str(self.profile.get("main_serial_port", "") or ""),
                "imu_mode": int(self.neural.status.get("imu_mode", 2) or 2),
                "imu_mode_text": str(
                    self.neural.status.get("imu_mode_text", imu_mode_label(self.neural.status.get("imu_mode", 2)))
                    or imu_mode_label(self.neural.status.get("imu_mode", 2))
                ),
            })
            self.status["rf"]["recommended_esb_channel"] = self.profile.get("recommended_esb_channel")
            self.status["rf"]["port"] = str(self.profile.get("rf_serial_port", "") or "")
            self.status["habits"] = merge_slot_status(self.status["habits"], {
                "port": str(self.profile.get("habits_serial_port", "") or ""),
                "mouse_directory": str(self.profile.get("mice_id_directory", "") or ""),
                "mouse_id": str(self.habits.status.get("mouse_id", "") or ""),
                "selected_data_dir": str(self.habits.status.get("selected_data_dir", "") or ""),
                "start_date": str(self.habits.status.get("start_date", "") or ""),
                "last_sync_text": str(self.habits.status.get("last_sync_text", "Not synced") or "Not synced"),
            })
            self.status["camera"]["camera_id"] = int(self.profile.get("camera_id", 0) or 0)
            self._apply_charging_guard_config(self.profile.get("charging_guard", {}), persist=False)
            self._append_system_log("Profile reloaded", level="info")
            self._flush_runtime_cache()
            return

        if command_type == "detail_attach":
            attached = bool(command.get("attached", False))
            if attached and bool(self.status.get("detail_attached", False)) and self.local_chart_window is not None:
                try:
                    self._open_local_chart_window()
                except Exception as exc:
                    self._append_performance_warning(
                        "local_chart_process",
                        f"Failed to focus detail chart process: {exc}",
                        level="warning",
                    )
                self._mark_updated(sync_local_chart=True)
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
                return
            self._detail_attach_generation += 1
            self.status["detail_attached"] = attached
            if attached:
                self.neural.set_detail_enabled(False)
                try:
                    self._open_local_chart_window()
                except Exception as exc:
                    self.status["detail_attached"] = False
                    self.neural.set_detail_enabled(False)
                    self._on_error(f"Failed to open local chart: {exc}")
                    return
                warmup_ms = self._detail_attach_warmup_delay_ms()
                if warmup_ms > 0:
                    generation = int(self._detail_attach_generation)
                    QTimer.singleShot(
                        warmup_ms,
                        lambda generation=generation: self._enable_detail_stream_after_warmup(generation),
                    )
                else:
                    self.neural.set_detail_enabled(True)
            else:
                self.neural.set_detail_enabled(False)
                # Detach is state-first: master must see "Open Charts" and the
                # serial reader must stop building chart frames even if Qt takes
                # time to destroy the window.
                self._mark_updated(sync_local_chart=False)
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
                self._close_local_chart_window()
            self._append_system_log(
                f"Detail viewer {'attached' if attached else 'detached'}",
                level="info",
            )
            self._mark_updated(sync_local_chart=attached)
            self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return

        if command_type == "detail_display_tuning_update":
            self._persist_detail_display_tuning(
                command.get("profile", {}),
                force=bool(command.get("force", False)),
            )
            return

        if command_type == "detail_display_interval_pressure":
            apply_pressure = getattr(self.neural, "apply_gui_interval_pressure", None)
            if callable(apply_pressure):
                apply_pressure(
                    str(command.get("reason", "detail_render_slow") or "detail_render_slow"),
                    stream_key=command.get("stream_key"),
                )
            return

        if command_type == "clear_system_log":
            self._clear_system_log()
            self._flush_runtime_cache()
            return

        if command_type == "connect_subsystem":
            subsystem = str(command.get("subsystem", ""))
            if subsystem == "neural":
                self._set_neural_command_busy(True, "connect")
                try:
                    self.neural.connect_port()
                finally:
                    self.status["neural"] = merge_slot_status(self.status["neural"], self.neural.status)
                    self._set_neural_command_busy(False, "")
            elif subsystem == "rf":
                self._submit_rf_task("connect", self._rf_connect_task)
            elif subsystem == "habits":
                self.habits.connect_port()
            elif subsystem == "camera":
                self.camera.open_camera()
                self._sync_camera_charging_guard_config()
            self._append_system_log(f"Connect command -> {subsystem}", level="info")
            return

        if command_type == "disconnect_subsystem":
            subsystem = str(command.get("subsystem", ""))
            if subsystem == "neural":
                self._set_neural_command_busy(True, "disconnect")
                try:
                    self.neural.disconnect_port()
                finally:
                    self.status["neural"] = merge_slot_status(self.status["neural"], self.neural.status)
                    self._set_neural_command_busy(False, "")
            elif subsystem == "rf":
                self._submit_rf_task("disconnect", self._rf_disconnect_task)
            elif subsystem == "habits":
                self.habits.disconnect_port()
            elif subsystem == "camera":
                self.camera.close_camera()
            self._append_system_log(f"Disconnect command -> {subsystem}", level="info")
            return

        if command_type == "set_mode":
            mode = int(command.get("mode", 0) or 0)
            ok = bool(self.neural.set_mode(mode))
            if ok:
                self._append_system_log(f"Set sample mode -> {mode}", level="info")
            else:
                self._on_error(f"Set sample mode command failed -> {mode}")
            return

        if command_type == "sample_start":
            if "mode" in command:
                mode = int(command.get("mode", 0) or 0)
                if not bool(self.neural.set_mode(mode)):
                    self._on_error(f"Set sample mode failed before sampling start -> {mode}")
                    return
                self._append_system_log(f"Set sample mode -> {mode}", level="info")
            ok = bool(self.neural.sample_start())
            if ok:
                self._append_system_log("Sampling started", level="info")
            else:
                self._on_error("Sampling start command failed")
            return

        if command_type == "sample_stop":
            ok = bool(self.neural.sample_stop())
            if ok:
                self._append_system_log("Sampling stopped", level="info")
            else:
                self._on_error("Sampling stop command failed")
            return

        if command_type == "set_save":
            mode_name = str(command.get("mode", "") or "")
            enabled = bool(command.get("enabled", False))
            ok = self.neural.set_save_mode(mode_name, enabled)
            mode_label = _save_mode_frontend_label(mode_name)
            self._append_system_log(
                (
                    f"Save {'enabled' if enabled else 'disabled'} -> {mode_label}"
                    if ok
                    else f"Save command failed -> {mode_label}"
                ),
                level="info" if ok else "warning",
            )
            if ok:
                self._sync_camera_recording_with_save(mode_name, enabled)
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return

        if command_type == "set_global_save":
            enabled = bool(command.get("enabled", True))
            self.neural.set_global_save_enabled(enabled)
            if not enabled:
                self._stop_camera_recording_follow_save("global save disabled")
                self.camera.stop_recording()
            self._append_system_log(
                f"Global save {'enabled' if enabled else 'disabled'}",
                level="info",
            )
            return

        if command_type == "set_imu_mode":
            mode = normalize_imu_mode(command.get("mode", 2))
            ok = self.neural.set_imu_mode(mode)
            self._append_system_log(
                (
                    f"IMU mode -> {imu_mode_label(mode)}"
                    if ok
                    else f"IMU mode command failed -> {imu_mode_label(mode)}"
                ),
                level="info" if ok else "warning",
            )
            if ok:
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return

        if command_type == "set_mode3_reref":
            mode = int(command.get("mode", 0) or 0)
            ok = self.neural.set_mode3_reref_mode(mode)
            self._append_system_log(
                f"Mode0/3 re-reference -> {mode}" if ok else f"Mode0/3 re-reference command failed -> {mode}",
                level="info" if ok else "warning",
            )
            if ok:
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return

        if command_type == "restart_relay":
            ok = bool(self.neural.restart_relay_device())
            if ok:
                self._append_system_log("Relay reboot command sent", level="warning")
            else:
                self._on_error("Relay reboot command failed")
            return

        if command_type == "restart_device":
            ok = bool(self.neural.restart_peripheral_firmware())
            if ok:
                self._append_system_log("Peripheral firmware reboot command sent", level="warning")
            else:
                self._on_error("Peripheral firmware reboot command failed")
            return

        if command_type == "set_device_esb_mode_config":
            mode = int(command.get("mode", 0) or 0)
            tx_power_code = int(command.get("tx_power_code", 0) or 0)
            retransmit_count = int(command.get("retransmit_count", 0) or 0)
            ack_window_us = int(command.get("ack_window_us", 1200) or 1200)
            noack_percent = int(command.get("noack_percent", 0) or 0)
            ok = bool(
                self.neural.set_device_esb_mode_config(
                    mode,
                    tx_power_code,
                    retransmit_count,
                    ack_window_us=ack_window_us,
                    noack_percent=noack_percent,
                )
            )
            if ok:
                self._append_system_log(
                    f"Mode{mode} ESB config -> tx_code {tx_power_code}, retransmit {retransmit_count}, "
                    f"ack {ack_window_us} us, noack {noack_percent}%",
                    level="info",
                )
            else:
                self._on_error(f"Mode{mode} ESB config command failed")
            return

        if command_type == "set_mode0_quant_config":
            bit_depth = int(command.get("bit_depth", 12) or 12)
            full_scale_uv = float(command.get("full_scale_uv", 1000.0) or 1000.0)
            ok = bool(self.neural.set_mode0_quant_config(bit_depth, full_scale_uv))
            if ok:
                self._append_system_log(
                    f"Mode0 quantization -> {bit_depth}-bit, LFP +/-{full_scale_uv:g} uV, raw +/-500 uV",
                    level="info",
                )
            else:
                self._on_error("Mode0 quantization command failed")
            return

        if command_type == "apply_relay_esb_channel":
            channel = int(command.get("channel", 0) or 0)
            ok = bool(self.neural.apply_relay_esb_channel(channel))
            if ok:
                self._append_system_log(f"Relay ESB channel apply -> CH{channel}", level="info")
            else:
                self._on_error(f"Relay ESB channel command failed -> CH{channel}")
            return

        if command_type == "rf_power":
            enabled = bool(command.get("enabled", False))
            if self._low_battery_mode0_training_target_or_active():
                self._low_battery_rf_manual_off = not bool(enabled)
                if not bool(enabled):
                    self._append_system_log(
                        "Manual RF off requested during low-battery Mode0 training; RF hold suspended",
                        level="warning",
                    )
                else:
                    self._append_system_log(
                        "Manual RF on requested; low-battery RF hold restored",
                        level="info",
                    )
            elif bool(enabled):
                self._low_battery_rf_manual_off = False
            self._submit_rf_task(
                f"power_{'on' if enabled else 'off'}",
                lambda enabled=enabled: self._rf_power_task(enabled),
            )
            self._append_system_log(f"RF power {'on' if enabled else 'off'} requested", level="info")
            return

        if command_type == "rf_query":
            self._submit_rf_task("query", self._rf_query_task)
            self._append_system_log("RF status query requested", level="info")
            return

        if command_type == "habits_pause_toggle":
            ok = bool(self._send_habits_pause_toggle_with_rf_guard())
            self._append_system_log(
                "Habits pause toggle" if ok else "Habits pause toggle blocked",
                level="info" if ok else "warning",
            )
            return

        if command_type == "habits_read_all":
            if not self._is_habits_command_ready():
                self._queue_habits_command_retry(
                    {"type": "habits_read_all", "_habits_retry_count": command.get("_habits_retry_count", 0)},
                    "not connected yet",
                )
                return
            ok = bool(self.habits.send_read_all())
            self._append_system_log(
                "Habits read-all command" if ok else "Habits read-all command failed",
                level="info" if ok else "warning",
            )
            return

        if command_type == "habits_time_sync":
            self.habits.send_time_sync()
            self._append_system_log("Habits time sync", level="info")
            return

        if command_type == "habits_serial":
            action = str(command.get("action", "") or "")
            try:
                ok = self._handle_habits_serial_command(action, command)
                if ok is None:
                    return
                if ok:
                    self._append_system_log(f"Habits command -> {action}", level="info")
                else:
                    self._append_system_log(f"Habits command failed -> {action}", level="warning")
            except ValueError as exc:
                self._on_error(str(exc))
            return

        if command_type == "habits_sd_download":
            target_dir = str(command.get("directory", "") or "").strip()
            try:
                ok = bool(self.habits.start_sd_download(target_dir))
            except Exception as exc:
                self._on_error(f"Habits SD download failed: {exc}")
                return
            self._append_system_log(
                (
                    f"Habits SD download -> {target_dir}"
                    if ok
                    else f"Habits SD download command failed -> {target_dir or '-'}"
                ),
                level="info" if ok else "warning",
            )
            if ok:
                self._flush_runtime_cache_priority(minimum_interval_seconds=0.0)
            return

        if command_type == "set_charging_guard":
            enabled = bool(command.get("enabled", False))
            self._apply_charging_guard_config({"enabled": enabled}, persist=True)
            self._append_system_log(
                f"Charging guard {'enabled' if enabled else 'disabled'}",
                level="info",
            )
            return

        if command_type == "set_charging_guard_config":
            config_payload = command.get("config", {})
            try:
                self._apply_charging_guard_config(
                    config_payload if isinstance(config_payload, dict) else {},
                    persist=True,
                )
                self._append_system_log("Charging guard config updated", level="info")
            except ValueError as exc:
                self._on_error(str(exc))
            return

        if command_type == "set_habits_data_directory":
            directory = str(command.get("directory", "") or "").strip()
            self.habits.set_data_directory(directory)
            self.profile["mice_id_directory"] = directory
            self._persist_profile()
            self._append_system_log(f"Habits data directory -> {directory}", level="info")
            return

        if command_type == "set_habits_start_date":
            start_date = str(command.get("start_date", "") or "").strip()
            self.habits.set_start_date(start_date)
            self.profile["habits_start_date"] = start_date
            self._persist_profile()
            self._append_system_log(f"Habits start date -> {start_date}", level="info")
            return

        if command_type == "set_camera_id":
            camera_id = int(command.get("camera_id", 0) or 0)
            self.camera.set_camera_id(camera_id)
            self._sync_camera_charging_guard_config()
            self.profile["camera_id"] = camera_id
            self._persist_profile()
            self._append_system_log(f"Camera id selected -> {camera_id}", level="info")
            return

        if command_type == "set_camera_preview":
            enabled = bool(command.get("enabled", False))
            if enabled and not bool(self.status.get("camera", {}).get("open", False)):
                if not bool(self.camera.open_camera()):
                    self.camera_preview_publisher.publish(
                        None,
                        enabled=False,
                        camera_id=self.status.get("camera", {}).get("camera_id"),
                    )
                    self._on_error("Camera preview cannot be enabled because camera open failed")
                    return
                self._sync_camera_charging_guard_config()
            preview_ok = self.camera.set_preview_enabled(enabled)
            if enabled and preview_ok is False:
                self.camera_preview_publisher.publish(
                    None,
                    enabled=False,
                    camera_id=self.status.get("camera", {}).get("camera_id"),
                )
                self._on_error("Camera preview cannot be enabled")
                return
            self._append_system_log(
                f"Camera preview {'enabled' if enabled else 'disabled'}",
                level="info",
            )
            if not enabled:
                self.camera_preview_publisher.publish(
                    None,
                    enabled=False,
                    camera_id=self.status.get("camera", {}).get("camera_id"),
                )
            return

        if command_type == "camera_open":
            self.camera.open_camera()
            self._sync_camera_charging_guard_config()
            self._append_system_log("Camera open requested", level="info")
            return

        if command_type == "camera_close":
            self._camera_recording_follow_save = False
            self._camera_recording_follow_save_mode = ""
            self.camera.close_camera()
            self.camera_preview_publisher.publish(
                None,
                enabled=False,
                camera_id=self.status.get("camera", {}).get("camera_id"),
            )
            self._append_system_log("Camera close requested", level="info")
            return

        if command_type == "camera_record_start":
            if not bool(self.status["neural"].get("global_save_enabled", True)):
                self._on_error("Global save is disabled")
                return
            self._camera_recording_follow_save = False
            self._camera_recording_follow_save_mode = ""
            self.camera.start_recording()
            self._append_system_log("Camera recording start requested", level="info")
            return

        if command_type == "camera_record_stop":
            self._camera_recording_follow_save = False
            self._camera_recording_follow_save_mode = ""
            self.camera.stop_recording()
            self._append_system_log("Camera recording stop requested", level="info")
            return

        if command_type == "set_mode1_channel":
            channel = int(command.get("channel", 0) or 0)
            auto_threshold = bool(command.get("auto_threshold", False))
            ok = bool(self.neural.set_mode1_channel(channel, auto_threshold=auto_threshold))
            if ok:
                self._append_system_log(f"Mode1 raw channel -> {channel}", level="info")
            else:
                self._on_error(f"Mode1 raw channel command failed -> {channel}")
            return

        if command_type == "set_mode3_raw_channel":
            channel = int(command.get("channel", 0) or 0)
            ok = bool(self.neural.set_mode3_raw_channel(channel))
            if ok:
                self._append_system_log(f"Mode0/3 raw channel -> {channel}", level="info")
            else:
                self._on_error(f"Mode0/3 raw channel command failed -> {channel}")
            return

        if command_type == "set_mode2_channels":
            channels = list(command.get("channels", []) or [])
            ok = bool(self.neural.set_mode2_channels(channels))
            if ok:
                self._append_system_log("Mode2 v2 uses fixed channels Ch0-Ch15; channel command ignored", level="info")
            else:
                self._on_error(f"Mode2 channel command failed -> {channels}")
            return

        if command_type == "set_spike_threshold":
            channel = int(command.get("channel", 0) or 0)
            threshold_uv = float(command.get("threshold_uv", 0.0) or 0.0)
            use_mode1_mapping = bool(command.get("use_mode1_mapping", False))
            ok = bool(self.neural.set_spike_threshold(channel, threshold_uv, use_mode1_mapping=use_mode1_mapping))
            if ok:
                self._append_system_log(
                    f"Spike threshold -> ch{channel} {threshold_uv:.1f}uV",
                    level="info",
                )
            else:
                self._on_error(f"Spike threshold command failed -> ch{channel} {threshold_uv:.1f}uV")
            return

        if command_type == "set_spike_filter_config":
            enabled = command.get("enabled")
            low_cut_hz = command.get("low_cut_hz")
            high_cut_hz = command.get("high_cut_hz")
            sample_rate_hz = command.get("sample_rate_hz")
            self.neural.set_spike_filter_config(
                enabled=enabled if enabled is not None else None,
                low_cut_hz=low_cut_hz if low_cut_hz is not None else None,
                high_cut_hz=high_cut_hz if high_cut_hz is not None else None,
                sample_rate_hz=sample_rate_hz if sample_rate_hz is not None else None,
            )
            if enabled is not None:
                self.profile["spike_filter_enabled"] = bool(enabled)
            if low_cut_hz is not None:
                self.profile["spike_filter_low_cut_hz"] = float(low_cut_hz)
            if high_cut_hz is not None:
                self.profile["spike_filter_high_cut_hz"] = float(high_cut_hz)
            if sample_rate_hz is not None:
                self.profile["spike_filter_sample_rate_hz"] = float(sample_rate_hz)
            self._persist_profile()
            self._append_system_log(
                "Spike filter config -> "
                f"{'On' if bool(self.neural.status.get('spike_filter_enabled', False)) else 'Off'}, "
                f"{float(self.neural.status.get('spike_filter_low_cut_hz', 0.0) or 0.0):.1f}-"
                f"{float(self.neural.status.get('spike_filter_high_cut_hz', 0.0) or 0.0):.1f} Hz, "
                f"{int(float(self.neural.status.get('spike_filter_sample_rate_hz', 20000.0) or 20000.0))} Hz",
                level="info",
            )
            return

        if command_type == "set_impedance_compensation":
            series_resistor_kohm = command.get("series_resistor_kohm")
            shunt_cap_pf = command.get("shunt_cap_pf")
            self.neural.set_impedance_compensation_params(
                series_resistor_kohm=series_resistor_kohm if series_resistor_kohm is not None else None,
                shunt_cap_pf=shunt_cap_pf if shunt_cap_pf is not None else None,
            )
            if series_resistor_kohm is not None:
                self.profile["impedance_series_resistor_kohm"] = float(series_resistor_kohm)
            if shunt_cap_pf is not None:
                self.profile["impedance_shunt_cap_pf"] = float(shunt_cap_pf)
            self._persist_profile()
            self._append_system_log(
                "Impedance RC compensation -> "
                f"R {float(self.neural.status.get('rc_series_resistor_kohm', 0.0) or 0.0):.2f} kOhm, "
                f"C {float(self.neural.status.get('rc_shunt_cap_pf', 0.0) or 0.0):.2f} pF",
                level="info",
            )
            return

        if command_type == "auto_spike_threshold":
            if self.neural.start_auto_threshold_update():
                self._append_system_log("Auto spike threshold sweep requested", level="info")
            return

        if command_type == "start_impedance_test":
            if self.neural.start_impedance_test():
                self._append_system_log("Impedance test requested", level="info")
            else:
                self._on_error("Impedance test command failed")
            return

    def _check_health(self) -> None:
        try:
            if not bool(self._habits_mode_switch_in_flight):
                self.rf.health_tick()
            if self.status["camera"].get("open"):
                if hasattr(self.camera, "check_health"):
                    healthy, reason = self.camera.check_health(max_stale_seconds=4.0)
                    if not healthy:
                        reason_text = str(reason or "camera health check failed")
                        self._append_performance_warning(
                            "camera_health",
                            f"Camera health degraded: {reason_text}",
                            level="warning",
                        )
                        self.camera.recover_if_needed(reason=reason_text)
                else:
                    self.camera.recover_if_needed(max_stale_seconds=4.0)
                self._poll_video_segment_rotation()
            self._refresh_service_state()
            self._apply_runtime_policy()
        except Exception as exc:
            self._set_last_error(f"Health check failed: {exc}")

    def _apply_runtime_policy(self) -> None:
        neural_status = self.status.get("neural", {})
        camera_status = self.status.get("camera", {})
        active_acquisition = (
            bool(neural_status.get("sampling"))
            or bool(neural_status.get("save_mode"))
            or bool(camera_status.get("recording"))
        )
        apply_windows_process_role("slot_service", active_recording=active_acquisition)

    def _telemetry_warn_threshold(self, key: str, fallback: float) -> float:
        thresholds = self.slot_runtime_config.get("telemetry_warn_thresholds", {})
        if not isinstance(thresholds, dict):
            return float(fallback)
        try:
            return float(thresholds.get(key, fallback))
        except Exception:
            return float(fallback)

    def _append_performance_warning(self, key: str, message: str, level: str = "warning") -> None:
        now_epoch = time.time()
        last_epoch = float(self._last_performance_warning_epoch.get(key, 0.0) or 0.0)
        if last_epoch > 0.0 and (now_epoch - last_epoch) < 60.0:
            return
        self._last_performance_warning_epoch[key] = now_epoch
        self._append_system_log(message, level=level)

    def _append_diagnostic_log(self, message: str, level: str = "info") -> None:
        """Write verbose diagnostics outside the GUI-loaded system log."""
        try:
            runtime_root = os.path.dirname(self.runtime_cache.root_dir)
            path = os.path.join(runtime_root, "system_diagnostics.log")
            os.makedirs(runtime_root, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            line = (
                f"[{timestamp}] [{str(level or 'info').upper()}] "
                f"{self.slot_id} {self.label}: {message}\n"
            )
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass

    @staticmethod
    def _format_scheduler_trace_near_packet_gap(diagnostics: Any) -> str:
        if not isinstance(diagnostics, dict):
            return ""
        trace = diagnostics.get("slot_scheduler_trace", [])
        if not isinstance(trace, list):
            return ""
        try:
            gap_epoch = float(diagnostics.get("timestamp_epoch", 0.0) or 0.0)
        except Exception:
            gap_epoch = 0.0
        if gap_epoch <= 0.0:
            return ""
        try:
            window_ms = max(1.0, float(diagnostics.get("slot_scheduler_near_window_ms", 250.0) or 250.0))
        except Exception:
            window_ms = 250.0
        nearby = []
        for item in trace:
            if not isinstance(item, dict):
                continue
            task = str(item.get("task", "") or "").strip()
            if not task:
                continue
            try:
                end_epoch = float(item.get("end_epoch", 0.0) or 0.0)
                duration_ms = float(item.get("duration_ms", 0.0) or 0.0)
            except Exception:
                continue
            if end_epoch <= 0.0:
                continue
            delta_ms = (gap_epoch - end_epoch) * 1000.0
            if abs(delta_ms) > window_ms:
                continue
            command_summary = str(item.get("command_poll_summary", "") or "").strip()
            nearby.append((abs(delta_ms), task, duration_ms, delta_ms, command_summary))
        if not nearby:
            return ""
        nearby.sort(key=lambda item: item[0])
        parts = []
        for _, task, duration_ms, delta_ms, command_summary in nearby[:3]:
            part = f"{task}:{duration_ms:.1f}ms@{abs(delta_ms):.0f}ms"
            if task == "command_poll" and command_summary:
                if len(command_summary) > 180:
                    command_summary = command_summary[:177] + "..."
                part = f"{part}[{command_summary}]"
            parts.append(part)
        return ";".join(parts)

    @staticmethod
    def _format_packet_gap_diagnostics(diagnostics: Any) -> str:
        if not isinstance(diagnostics, dict) or not diagnostics:
            return ""
        parts = []
        bucket = str(diagnostics.get("bucket", "") or "").strip()
        if bucket:
            parts.append(f"bucket={bucket}")
        parts.append(f"detail={'on' if bool(diagnostics.get('detail_enabled', False)) else 'off'}")
        parts.append(f"mode3_save={'on' if bool(diagnostics.get('mode3_save_enabled', False)) else 'off'}")
        try:
            parts.append(f"read_gap={float(diagnostics.get('serial_read_gap_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        try:
            parts.append(f"read_bytes={int(diagnostics.get('read_batch_bytes', 0) or 0)}")
        except Exception:
            pass
        try:
            parts.append(f"waiting={int(diagnostics.get('port_in_waiting_before_read', 0) or 0)}")
        except Exception:
            pass
        try:
            parts.append(f"enqueue={float(diagnostics.get('save_enqueue_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        try:
            parts.append(f"decode={float(diagnostics.get('data_process_ms', diagnostics.get('packet_decode_ms', 0.0)) or 0.0):.1f}ms")
        except Exception:
            pass
        try:
            parts.append(f"save_ctl={float(diagnostics.get('save_control_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        try:
            parts.append(f"gui={float(diagnostics.get('gui_update_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        try:
            parts.append(f"proc={float(diagnostics.get('pipeline_process_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        enqueue_type = str(diagnostics.get("save_enqueue_type", "") or "").strip()
        if enqueue_type:
            parts.append(f"enqueue_type={enqueue_type}")
        try:
            parts.append(f"writer_lag={int(diagnostics.get('writer_lag', 0) or 0)}")
        except Exception:
            pass
        try:
            parts.append(
                "pending="
                f"{int(diagnostics.get('mode3_pending_packets', 0) or 0)}/"
                f"{int(diagnostics.get('mode3_raw_pending_packets', 0) or 0)}"
            )
        except Exception:
            pass
        try:
            parts.append(
                "intervals="
                f"{int(diagnostics.get('mode3_lfp_esa_interval', 0) or 0)}/"
                f"{int(diagnostics.get('mode3_raw_interval', 0) or 0)}"
            )
        except Exception:
            pass
        try:
            parts.append(
                "pipe="
                f"{int(diagnostics.get('pipeline_raw_queue_depth', 0) or 0)}/"
                f"{int(diagnostics.get('pipeline_event_queue_depth', 0) or 0)}"
            )
        except Exception:
            pass
        render_stream = str(diagnostics.get("detail_render_stream", "") or "").strip()
        if render_stream:
            parts.append(f"render_stream={render_stream}")
        try:
            parts.append(f"render={float(diagnostics.get('detail_render_ms', 0.0) or 0.0):.1f}ms")
        except Exception:
            pass
        render_metrics = diagnostics.get("detail_render_metrics", {})
        if isinstance(render_metrics, dict) and render_metrics:
            metric_parts = []
            for metric_key, label in (
                ("lfp_buffer_update_ms", "lfp_buf"),
                ("lfp_setdata_ms", "lfp_set"),
                ("esa_setdata_ms", "esa_set"),
                ("raw_buffer_update_ms", "raw_buf"),
                ("raw_setdata_ms", "raw_set"),
                ("raster_setdata_ms", "raster"),
            ):
                try:
                    value = float(render_metrics.get(metric_key, 0.0) or 0.0)
                except Exception:
                    value = 0.0
                if value > 0.0:
                    metric_parts.append(f"{label}={value:.1f}ms")
            if metric_parts:
                parts.append("chart=" + ";".join(metric_parts))
        scheduler_text = SlotService._format_scheduler_trace_near_packet_gap(diagnostics)
        if scheduler_text:
            parts.append(f"slot_tasks={scheduler_text}")
        return ", ".join(parts)

    def _queue_neural_packet_gap_log(
        self,
        missing: int,
        summary: str,
        event_delta: int = 1,
        diagnostics: Optional[Dict[str, Any]] = None,
    ) -> None:
        pending = dict(getattr(self, "_pending_neural_packet_gap_log", {}) or {})
        pending["events"] = max(0, int(pending.get("events", 0) or 0)) + max(1, int(event_delta or 1))
        pending["missing"] = max(0, int(pending.get("missing", 0) or 0)) + max(0, int(missing or 0))
        summary_text = str(summary or "").strip()
        if summary_text:
            pending["summary"] = summary_text
        if isinstance(diagnostics, dict) and diagnostics:
            diagnostics_payload = dict(diagnostics)
            performance = dict(self.status.get("telemetry", {}).get("performance", {}) or {})
            diagnostics_payload["detail_render_ms"] = float(performance.get("detail_render_ms", 0.0) or 0.0)
            diagnostics_payload["detail_render_stream"] = str(performance.get("detail_render_stream", "") or "")
            render_metrics = performance.get("detail_render_metrics", {})
            if isinstance(render_metrics, dict):
                diagnostics_payload["detail_render_metrics"] = dict(render_metrics)
            diagnostics_payload["slot_scheduler_trace_seq"] = int(getattr(self, "_slot_task_trace_seq", 0) or 0)
            diagnostics_payload["slot_scheduler_trace"] = self._slot_scheduler_trace_payload(limit=20)
            diagnostics_payload["slot_scheduler_near_window_ms"] = float(
                self.slot_runtime_config.get("slot_scheduler_trace_near_window_ms", 250) or 250
            )
            pending["diagnostics"] = diagnostics_payload
        self._pending_neural_packet_gap_log = pending
        self._mark_history_dirty("system_log")
        self._mark_updated(sync_local_chart=False)

    def _flush_neural_packet_gap_log(self, force: bool = False) -> None:
        pending = dict(getattr(self, "_pending_neural_packet_gap_log", {}) or {})
        events = max(0, int(pending.get("events", 0) or 0))
        if events <= 0:
            return
        now_epoch = time.time()
        interval_s = max(1.0, float(getattr(self, "_neural_gap_log_interval_s", 60.0) or 60.0))
        last_epoch = float(getattr(self, "_last_neural_packet_gap_log_epoch", 0.0) or 0.0)
        if last_epoch > 0.0 and (now_epoch - last_epoch) < interval_s:
            return
        missing = max(0, int(pending.get("missing", 0) or 0))
        summary = str(pending.get("summary", "") or "").strip()
        diagnostics_text = self._format_packet_gap_diagnostics(pending.get("diagnostics", {}))
        gui_message = (
            f"Neural packet counter gap: {events} event(s), missing {missing} packet(s)"
            + (f" [latest: {summary}]" if summary else "")
        )
        diagnostic_message = (
            f"Neural packet counter gap detected: {events} event(s), missing {missing} packet(s)"
            + (f" [latest: {summary}]" if summary else "")
            + (f" [diag: {diagnostics_text}]" if diagnostics_text else "")
        )
        self._append_diagnostic_log(diagnostic_message, level="critical")
        self._append_system_log(
            gui_message,
            level="critical",
        )
        self._last_neural_packet_gap_log_epoch = now_epoch
        self._pending_neural_packet_gap_log = {
            "events": 0,
            "missing": 0,
            "summary": "",
            "diagnostics": {},
        }

    def _refresh_ui_busy_states(self) -> None:
        neural_status = dict(self.status.get("neural", {}) or {})
        rf_status = dict(self.status.get("rf", {}) or {})
        camera_status = dict(self.status.get("camera", {}) or {})
        self.status["ui_busy_states"] = {
            "neural_connection": bool(neural_status.get("command_busy", False)),
            "rf_connection": bool(rf_status.get("command_busy", False)),
            "camera": str(camera_status.get("health_state", "ok") or "ok").strip().lower() == "recovering",
            "sampling": bool(neural_status.get("ui_busy", {}).get("sampling", False)) if isinstance(neural_status.get("ui_busy"), dict) else False,
            "saving": int(neural_status.get("writer_lag", 0) or 0) > self._telemetry_warn_threshold("save_writer_lag", 100.0),
            "detail": bool(neural_status.get("detail_throttled", False)),
        }

    def _update_performance_status(self, **values: Any) -> None:
        telemetry = dict(self.status.get("telemetry", {}) or {})
        performance = dict(telemetry.get("performance", {}) or {})
        performance.update(values)
        telemetry["performance"] = performance
        self.status["telemetry"] = telemetry
        self._status_dirty = True

    def _record_detail_skip(self, reason: str) -> int:
        reason = str(reason or "throttled")
        neural_status = dict(self.status.get("neural", {}) or {})
        telemetry = dict(neural_status.get("telemetry", {}) or {})
        dropped_frames = int(
            telemetry.get("detail_dropped_frames", neural_status.get("detail_dropped_frames", 0)) or 0
        ) + 1
        telemetry.update({
            "detail_throttled": True,
            "detail_dropped_frames": dropped_frames,
            "detail_last_skip_reason": reason,
        })
        neural_status.update({
            "detail_throttled": True,
            "detail_dropped_frames": dropped_frames,
            "detail_last_skip_reason": reason,
            "telemetry": telemetry,
        })
        self.status["neural"] = neural_status
        self._update_performance_status(
            detail_throttled=True,
            detail_dropped_frames=dropped_frames,
            detail_last_skip_reason=reason,
        )
        return dropped_frames

    def _detail_backpressure_reason(self, neural_status: Dict[str, Any]) -> str:
        if not bool(self.status.get("detail_attached", False)):
            return ""
        performance = dict(dict(self.status.get("telemetry", {}) or {}).get("performance", {}) or {})
        neural_telemetry = dict(neural_status.get("telemetry", {}) or {})
        detail_pending_frames = max(
            int(performance.get("detail_pending_frames", 0) or 0),
            int(neural_telemetry.get("detail_pending_frames", 0) or 0),
        )
        detail_backlog_threshold = max(
            0,
            int(self.slot_runtime_config.get("detail_backlog_backpressure_frames", 8) or 0),
        )
        if detail_backlog_threshold > 0 and detail_pending_frames > detail_backlog_threshold:
            return "detail_backlog"
        if bool(neural_status.get("serial_backlog_active", False)):
            return "serial_backlog"
        read_gap_events = int(neural_status.get("serial_read_gap_event_count", 0) or 0)
        read_gap_ms = float(neural_status.get("serial_read_gap_ms", 0.0) or 0.0)
        read_gap_threshold = float(
            self.slot_runtime_config.get(
                "detail_read_gap_backpressure_ms",
                self._telemetry_warn_threshold("serial_read_gap_ms", 80.0),
            )
            or 0.0
        )
        read_gap_last_epoch = float(neural_status.get("serial_read_gap_last_epoch", 0.0) or 0.0)
        read_gap_window_s = max(
            0.5,
            float(self.slot_runtime_config.get("serial_read_gap_backpressure_s", 2.0) or 2.0),
        )
        read_gap_recent = read_gap_last_epoch > 0.0 and (time.time() - read_gap_last_epoch) <= read_gap_window_s
        if read_gap_events > 0 and read_gap_threshold > 0 and read_gap_ms > read_gap_threshold and read_gap_recent:
            return "serial_read_gap"
        pipeline_status = neural_status.get("pipeline", {})
        if isinstance(pipeline_status, dict):
            raw_queue_depth = int(pipeline_status.get("raw_queue_depth", 0) or 0)
            raw_queue_threshold = int(
                self.slot_runtime_config.get("pipeline_raw_queue_backpressure_depth", 512) or 512
            )
            if raw_queue_threshold > 0 and raw_queue_depth > raw_queue_threshold:
                return "pipeline_raw_queue"
        packet_decode_ms = float(neural_status.get("packet_decode_ms", 0.0) or 0.0)
        decode_threshold = self._telemetry_warn_threshold("packet_decode_ms", 40.0)
        if decode_threshold > 0 and packet_decode_ms > decode_threshold:
            return "packet_decode_slow"
        save_enqueue_ms = float(neural_status.get("save_enqueue_ms", 0.0) or 0.0)
        save_enqueue_threshold = self._telemetry_warn_threshold("save_enqueue_ms", 20.0)
        if save_enqueue_threshold > 0 and save_enqueue_ms > save_enqueue_threshold:
            return "edf_enqueue_slow"
        writer_lag = int(neural_status.get("writer_lag", 0) or 0)
        writer_lag_threshold = self._telemetry_warn_threshold("save_writer_lag", 100.0)
        if writer_lag_threshold > 0 and writer_lag > writer_lag_threshold:
            return "edf_writer_lag"
        return ""

    def _apply_detail_backpressure(self, reason: str) -> None:
        active = bool(reason)
        if hasattr(self.neural, "set_detail_backpressure"):
            try:
                self.neural.set_detail_backpressure(active, reason)
            except Exception:
                pass
        neural_status = dict(self.status.get("neural", {}) or {})
        telemetry = dict(neural_status.get("telemetry", {}) or {})
        if active:
            neural_status["detail_throttled"] = True
            neural_status["detail_last_skip_reason"] = reason
            telemetry["detail_throttled"] = True
            telemetry["detail_last_skip_reason"] = reason
            self._append_performance_warning(
                "detail_backpressure",
                f"Detail chart payload throttled because {reason}; neural read/EDF save keep priority",
                level="warning",
            )
        else:
            neural_status["detail_throttled"] = False
            if str(neural_status.get("detail_last_skip_reason", "")) in {
                "serial_backlog",
                "pipeline_raw_queue",
                "packet_decode_slow",
                "edf_writer_lag",
                "edf_enqueue_slow",
                "detail_render_slow",
                "backpressure",
                "serial_read_gap",
                "detail_backlog",
            }:
                neural_status["detail_last_skip_reason"] = ""
            telemetry["detail_throttled"] = False
            if str(telemetry.get("detail_last_skip_reason", "")) in {
                "serial_backlog",
                "pipeline_raw_queue",
                "packet_decode_slow",
                "edf_writer_lag",
                "edf_enqueue_slow",
                "detail_render_slow",
                "backpressure",
                "serial_read_gap",
                "detail_backlog",
            }:
                telemetry["detail_last_skip_reason"] = ""
        neural_status["telemetry"] = telemetry
        self.status["neural"] = neural_status
        self._update_performance_status(
            detail_throttled=bool(neural_status.get("detail_throttled", False)),
            detail_dropped_frames=int(neural_status.get("detail_dropped_frames", 0) or 0),
            detail_last_skip_reason=str(neural_status.get("detail_last_skip_reason", "") or ""),
        )

    def _flush_status_cache(self, force: bool = False) -> None:
        if not force and not bool(self._status_dirty):
            return
        flush_started = time.monotonic()
        self._refresh_ui_busy_states()
        self._publish_habits_live(force=force)
        try:
            self.runtime_cache.write_status(self._status_payload_for_cache())
            self._status_dirty = False
            self._note_runtime_cache_write_success("status")
        except Exception as exc:
            self._note_runtime_cache_write_failure("status", exc)
        duration_ms = max(0.0, (time.monotonic() - flush_started) * 1000.0)
        telemetry = dict(self.status["neural"].get("telemetry", {}) or {})
        telemetry["runtime_cache_flush_duration_ms"] = duration_ms
        self.status["neural"]["telemetry"] = telemetry
        self._update_performance_status(runtime_cache_flush_duration_ms=duration_ms)
        threshold_ms = self._telemetry_warn_threshold("runtime_cache_flush_ms", 120.0)
        if duration_ms > threshold_ms:
            self._append_performance_warning(
                "runtime_cache_flush_ms",
                f"Runtime status cache flush is slow: {duration_ms:.1f} ms",
                level="warning",
            )

    def _status_payload_for_cache(self) -> Dict[str, Any]:
        payload = dict(self.status)
        telemetry = dict(payload.get("telemetry", {}) or {})
        performance = dict(telemetry.get("performance", {}) or {})
        performance["slot_scheduler_trace_seq"] = int(getattr(self, "_slot_task_trace_seq", 0) or 0)
        performance["slot_scheduler_trace"] = self._slot_scheduler_trace_payload(limit=20)
        telemetry["performance"] = performance
        payload["telemetry"] = telemetry
        habits = dict(payload.get("habits", {}) or {})
        cap_history = habits.get("cap_history", [])
        if isinstance(cap_history, list):
            try:
                tail = max(0, int(self.slot_runtime_config.get("status_cache_cap_history_tail", 20) or 20))
            except Exception:
                tail = 20
            if tail <= 0:
                habits["cap_history"] = []
            else:
                habits["cap_history"] = [
                    dict(item) if isinstance(item, dict) else item
                    for item in cap_history[-tail:]
                ]
        payload["habits"] = habits
        return payload

    def _history_flush_tasks(self) -> Tuple[Tuple[str, Callable[[], None]], ...]:
        return (
            ("battery_history", lambda: self.runtime_cache.write_battery_history(self._battery_history_payload_for_cache())),
            ("impedance_history", lambda: self.runtime_cache.write_impedance_history(self.neural.get_impedance_history_for_cache() if hasattr(self.neural, "get_impedance_history_for_cache") else [])),
            ("habits_trial_data", lambda: self.runtime_cache.write_habits_trial_data(self.habits.get_trial_data_for_cache())),
            ("habits_event_data", lambda: self.runtime_cache.write_habits_event_data(self.habits.get_event_data_for_cache())),
            ("habits_state_data", lambda: self.runtime_cache.write_habits_state_data(self.habits.get_state_data_for_cache())),
            ("mode_switches", lambda: self.runtime_cache.write_mode_switches(self.habits.get_mode_switches_for_cache())),
            ("system_log", lambda: (self._prune_system_log(), self.runtime_cache.write_system_log(list(self.system_log)))),
        )

    def _deferred_history_labels_while_sampling(self) -> set:
        if not bool(self.slot_runtime_config.get("defer_large_history_flush_while_sampling", True)):
            return set()
        neural_status = dict(self.status.get("neural", {}) or {})
        if not bool(neural_status.get("sampling", False)):
            return set()
        return set(LARGE_HISTORY_CACHE_LABELS)

    def _flush_history_cache(
        self,
        force: bool = False,
        priority_labels: Optional[Tuple[str, ...]] = None,
        include_battery_history: bool = False,
        include_habits_history: bool = False,
    ) -> None:
        if not force and not bool(self._history_dirty):
            return
        flush_started = time.monotonic()
        self._flush_neural_packet_gap_log(force=force)
        flush_tasks = self._history_flush_tasks()
        if not flush_tasks:
            return
        dirty_labels = set(getattr(self, "_history_dirty_labels", set()) or set())
        if bool(self._history_dirty) and not dirty_labels:
            dirty_labels = set(HISTORY_CACHE_LABELS)
            self._history_dirty_labels = set(dirty_labels)
        deferred_labels = set()
        if not include_battery_history:
            deferred_labels.add("battery_history")
        if not include_habits_history:
            deferred_labels.update(HABITS_HISTORY_CACHE_LABELS)
        if not force:
            deferred_labels = self._deferred_history_labels_while_sampling()
            if not include_battery_history:
                deferred_labels.add("battery_history")
            if not include_habits_history:
                deferred_labels.update(HABITS_HISTORY_CACHE_LABELS)
        priority_set = {str(label) for label in (priority_labels or ())}
        if priority_set and not force:
            selected_tasks = tuple(
                (label, writer)
                for label, writer in flush_tasks
                if label in priority_set and label in dirty_labels
            )
            if not selected_tasks:
                return
        elif force:
            selected_tasks = tuple(
                (label, writer)
                for label, writer in flush_tasks
                if (include_battery_history or label != "battery_history")
                and (include_habits_history or label not in HABITS_HISTORY_CACHE_LABELS)
            )
            self._history_flush_cursor = 0
            self._history_flush_cycle_generation = int(self._history_dirty_generation)
        else:
            if not dirty_labels:
                self._history_dirty = False
                self._history_flush_cursor = 0
                return
            eligible_dirty_labels = set(dirty_labels)
            if deferred_labels:
                eligible_dirty_labels -= deferred_labels
            if not eligible_dirty_labels:
                self._history_dirty = True
                telemetry = dict(self.status["neural"].get("telemetry", {}) or {})
                telemetry["runtime_cache_history_deferred_labels"] = sorted(dirty_labels & deferred_labels)
                telemetry["runtime_cache_history_flush_duration_ms"] = max(
                    0.0,
                    (time.monotonic() - flush_started) * 1000.0,
                )
                self.status["neural"]["telemetry"] = telemetry
                self._update_performance_status(
                    runtime_cache_history_flush_duration_ms=telemetry["runtime_cache_history_flush_duration_ms"]
                )
                return
            if self._history_flush_cursor <= 0 or self._history_flush_cursor >= len(flush_tasks):
                self._history_flush_cursor = 0
                self._history_flush_cycle_generation = int(self._history_dirty_generation)
            max_tasks = max(
                1,
                int(self.slot_runtime_config.get("history_flush_max_tasks_per_tick", 2) or 2),
            )
            max_tasks = min(max_tasks, len(flush_tasks))
            start_index = int(self._history_flush_cursor)
            selected = []
            selected_indices = []
            task_count = len(flush_tasks)
            for offset in range(task_count):
                index = (start_index + offset) % task_count
                label, writer = flush_tasks[index]
                if label not in eligible_dirty_labels:
                    continue
                selected.append((label, writer))
                selected_indices.append(index)
                if len(selected) >= max_tasks:
                    break
            selected_tasks = tuple(selected)
            if not selected_tasks:
                self._history_dirty = bool(dirty_labels)
                return
        all_ok = True
        failure_count = 0
        succeeded_labels = []
        for label, writer in selected_tasks:
            try:
                writer()
                self._note_runtime_cache_write_success(label)
                succeeded_labels.append(label)
            except Exception as exc:
                all_ok = False
                failure_count += 1
                self._note_runtime_cache_write_failure(label, exc)
        if succeeded_labels:
            dirty_labels = set(getattr(self, "_history_dirty_labels", set()) or set())
            for label in succeeded_labels:
                dirty_labels.discard(label)
            self._history_dirty_labels = dirty_labels
        telemetry = dict(self.status["neural"].get("telemetry", {}) or {})
        duration_ms = max(0.0, (time.monotonic() - flush_started) * 1000.0)
        telemetry["runtime_cache_history_flush_duration_ms"] = duration_ms
        telemetry["runtime_cache_write_failures"] = int(failure_count)
        self.status["neural"]["telemetry"] = telemetry
        self._update_performance_status(runtime_cache_history_flush_duration_ms=duration_ms)
        if all_ok and self._runtime_cache_logged_failure_labels:
            self._append_system_log("Runtime cache writes recovered", level="info")
            self._last_cache_flush_warning = ""
            self._runtime_cache_logged_failure_labels.clear()
        if all_ok:
            if force:
                remaining_dirty = set(getattr(self, "_history_dirty_labels", set()) or set())
                if include_battery_history and include_habits_history:
                    remaining_dirty.clear()
                self._history_dirty_labels = remaining_dirty
                self._history_dirty = bool(remaining_dirty)
                self._history_flush_cursor = 0
                self._history_flush_cycle_generation = int(self._history_dirty_generation)
            elif priority_set:
                self._history_dirty = bool(getattr(self, "_history_dirty_labels", set()))
                return
            else:
                if selected_indices:
                    self._history_flush_cursor = (int(selected_indices[-1]) + 1) % len(flush_tasks)
                if not getattr(self, "_history_dirty_labels", set()):
                    self._history_dirty = False
                    self._history_flush_cursor = 0
                    self._history_flush_cycle_generation = int(self._history_dirty_generation)

    def _flush_runtime_cache(
        self,
        include_battery_history: bool = False,
        include_habits_history: bool = False,
    ) -> None:
        self._flush_history_cache(
            force=True,
            include_battery_history=include_battery_history,
            include_habits_history=include_habits_history,
        )
        self._flush_status_cache(force=True)

    def _note_runtime_cache_write_success(self, label: str) -> None:
        normalized_label = str(label or "")
        self._runtime_cache_failure_counts.pop(normalized_label, None)
        self._runtime_cache_failure_first_epoch.pop(normalized_label, None)
        if normalized_label in self._runtime_cache_logged_failure_labels:
            self._runtime_cache_logged_failure_labels.discard(normalized_label)
            if not self._runtime_cache_logged_failure_labels:
                self._append_system_log("Runtime cache writes recovered", level="info")
                self._last_cache_flush_warning = ""

    def _note_runtime_cache_write_failure(self, label: str, exc: Exception) -> None:
        normalized_label = str(label or "")
        now_epoch = time.time()
        count = int(self._runtime_cache_failure_counts.get(normalized_label, 0) or 0) + 1
        self._runtime_cache_failure_counts[normalized_label] = count
        self._runtime_cache_failure_first_epoch.setdefault(normalized_label, now_epoch)
        first_epoch = float(self._runtime_cache_failure_first_epoch.get(normalized_label, now_epoch) or now_epoch)
        should_log = (
            count >= RUNTIME_CACHE_FAILURE_LOG_THRESHOLD
            or (now_epoch - first_epoch) >= RUNTIME_CACHE_FAILURE_LOG_WINDOW_SECONDS
        )
        if not should_log:
            if normalized_label in HISTORY_CACHE_LABELS:
                self._mark_history_dirty(normalized_label)
                self._mark_updated(sync_local_chart=False)
            return
        message = f"Runtime cache write skipped for {label}: {exc}"
        if message == self._last_cache_flush_warning:
            if normalized_label in HISTORY_CACHE_LABELS:
                self._mark_history_dirty(normalized_label)
                self._mark_updated(sync_local_chart=False)
            return
        self._last_cache_flush_warning = message
        self._runtime_cache_logged_failure_labels.add(normalized_label)
        self.system_log.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": "warning",
            "message": message,
        })
        self._prune_system_log()
        labels = ["system_log"]
        if normalized_label in HISTORY_CACHE_LABELS:
            labels.append(normalized_label)
        self._mark_history_dirty(labels)
        self._mark_updated(sync_local_chart=False)

    def shutdown(self) -> None:
        self._shutdown_requested = True
        try:
            self._flush_runtime_cache(include_battery_history=True, include_habits_history=True)
        except Exception:
            pass
        self.command_timer.stop()
        self.publish_timer.stop()
        self.history_timer.stop()
        self.rollup_timer.stop()
        self.health_timer.stop()
        self.detail_render_timer.stop()
        self.camera_preview_timer.stop()
        self.charging_guard_timer.stop()
        self.mode3_trial_trigger_guard_timer.stop()
        self._rf_worker_stop.set()
        try:
            self._rf_task_queue.put_nowait(None)
        except Exception:
            pass
        try:
            if getattr(self, "_rf_worker_thread", None) is not None:
                self._rf_worker_thread.join(timeout=0.5)
        except Exception:
            pass
        try:
            self.camera.stop_recording()
        except Exception:
            pass
        try:
            self.camera.close_camera()
        except Exception:
            pass
        try:
            self.habits.disconnect_port()
        except Exception:
            pass
        try:
            self.rf.disconnect_port()
        except Exception:
            pass
        try:
            self.neural.disconnect_port()
        except Exception:
            pass
        self._close_local_chart_window()
        self.camera_preview_publisher.publish(
            None,
            enabled=False,
            camera_id=self.status.get("camera", {}).get("camera_id"),
        )
        self._flush_runtime_cache(include_battery_history=True, include_habits_history=True)
        self.runtime_cache.clear_transient_files()


def _configure_slot_service_app(app) -> None:
    try:
        app.setQuitOnLastWindowClosed(False)
    except Exception:
        pass


def run_slot_service(slot_payload: Dict[str, Any], runtime_root: str, command_queue, priority_command_queue=None) -> int:
    configure_qt_runtime()
    app = QApplication.instance() or QApplication(sys.argv[:1] or ["slot_service"])
    _configure_slot_service_app(app)
    service = SlotService(slot_payload, runtime_root, command_queue, priority_command_queue)
    app.aboutToQuit.connect(service.shutdown)
    return app.exec()
