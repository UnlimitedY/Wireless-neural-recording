import os
from collections.abc import Mapping as MappingABC
from copy import deepcopy
from typing import Any, Dict, Mapping

try:
    from ..storage.runtime_cache import read_json
except ImportError:
    from storage.runtime_cache import read_json


RUNTIME_CONFIG_FILENAME = "master_console_runtime.json"

DEFAULT_RUNTIME_CONFIG: Dict[str, Dict[str, Any]] = {
    "master_console": {
        "status_refresh_ms": 1000,
        "camera_preview_poll_ms": 90,
        "system_log_refresh_ms": 2000,
        "timeline_refresh_ms": 5000,
        "slot_shutdown_join_timeout_s": 12.0,
        "slot_shutdown_terminate_join_timeout_s": 2.0,
        "ui_diff_render_enabled": True,
    },
    "detail_view": {
        "status_poll_ms": 700,
        "detail_target_fps": 12,
        "detail_payload_poll_ms": 16,
        "habits_cache_poll_ms": 2000,
        "local_habits_cache_poll_ms": 10000,
        "detail_render_min_interval_ms": 83,
    },
    "mode0_power_estimator": {
        "enabled": True,
        "warn_retransmit_rate_percent": 20.0,
        "calibration_rows": [
            {
                "tx_power_dbm": 0,
                "retransmit_rate_percent": 0.0,
                "mw": 15.0,
            }
        ],
    },
    "slot_service": {
        "command_poll_ms": 10,
        "command_poll_max_commands_per_tick": 8,
        "command_poll_budget_ms": 4.0,
        "command_poll_scan_limit": 64,
        "timer_stagger_step_ms": 170,
        "runtime_cache_flush_ms": 1000,
        "health_rollup_ms": 10000,
        "health_check_ms": 1500,
        "battery_latest_flush_ms": 2000,
        "habits_live_flush_ms": 1000,
        "habits_live_update_tail": 20000,
        "camera_preview_publish_ms": 120,
        "camera_preview_max_width": 640,
        "camera_preview_jpeg_quality": 72,
        "camera_background_preview_fps": 1,
        "camera_preview_active_fps": 8,
        "camera_preview_recording_fps": 5,
        "camera_preview_hidden_fps": 1,
        "preview_detection_min_interval_ms": 300,
        "charging_guard_frame_max_width": 640,
        "history_flush_ms": 5000,
        "history_flush_max_tasks_per_tick": 2,
        "defer_large_history_flush_while_sampling": True,
        "status_cache_cap_history_tail": 20,
        "detail_publish_min_interval_ms": 0,
        "detail_attach_warmup_ms": 3000,
        "local_chart_render_max_frames_per_tick": 1,
        "local_chart_render_timer_ms": 16,
        "detail_render_backpressure_ms": 20,
        "detail_render_backpressure_s": 2,
        "detail_read_gap_backpressure_ms": 15,
        "detail_max_payload_bytes": 4000000,
        "serial_read_gap_backpressure_s": 2.0,
        "slot_scheduler_trace_max_entries": 80,
        "slot_scheduler_trace_min_duration_ms": 1,
        "slot_scheduler_trace_near_window_ms": 250,
        "save_chunk_packets_mode0": 100,
        "save_chunk_packets_mode3": 100,
        "save_chunk_packets_mode3_raw": 100,
        "telemetry_warn_thresholds": {
            "runtime_cache_flush_ms": 120.0,
            "detail_payload_bytes": 8000000,
            "detail_payload_interval_ms": 800.0,
            "camera_preview_payload_bytes": 600000,
            "packet_decode_ms": 40.0,
            "serial_read_gap_ms": 80.0,
            "save_enqueue_ms": 20.0,
            "save_writer_lag": 100,
        },
    },
}


def detail_frame_interval_ms(config: Mapping[str, Any]) -> int:
    if isinstance(config.get("detail_view") if isinstance(config, MappingABC) else None, MappingABC):
        detail_config = config.get("detail_view", {})
    else:
        detail_config = config
    if not isinstance(detail_config, MappingABC):
        detail_config = {}
    try:
        target_fps = float(detail_config.get("detail_target_fps", 0) or 0)
    except Exception:
        target_fps = 0.0
    if target_fps > 0.0:
        target_fps = min(30.0, target_fps)
        return max(1, int(round(1000.0 / target_fps)))
    try:
        return max(0, int(detail_config.get("detail_render_min_interval_ms", 0) or 0))
    except Exception:
        return 0


def get_runtime_config_path(base_dir: str) -> str:
    normalized_base = os.path.abspath(base_dir)
    direct_path = os.path.join(normalized_base, RUNTIME_CONFIG_FILENAME)
    if os.path.exists(direct_path):
        return direct_path
    return os.path.join(os.path.dirname(normalized_base), RUNTIME_CONFIG_FILENAME)


def _normalize_positive_int(value: Any, fallback: int, minimum: int) -> int:
    try:
        normalized = int(value)
    except Exception:
        normalized = int(fallback)
    return max(int(minimum), normalized)


def _normalize_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return bool(fallback)


def _normalize_mapping(value: Any, fallback: Mapping[str, Any]) -> Dict[str, Any]:
    merged = dict(fallback)
    if not isinstance(value, MappingABC):
        return merged
    for key, default_value in fallback.items():
        incoming = value.get(key, default_value)
        if isinstance(default_value, bool):
            merged[key] = _normalize_bool(incoming, default_value)
        elif isinstance(default_value, int):
            merged[key] = _normalize_positive_int(incoming, default_value, 0)
        elif isinstance(default_value, float):
            try:
                merged[key] = max(0.0, float(incoming))
            except Exception:
                merged[key] = float(default_value)
        else:
            merged[key] = incoming
    return merged


def load_runtime_config(base_dir: str) -> Dict[str, Dict[str, Any]]:
    config = deepcopy(DEFAULT_RUNTIME_CONFIG)
    path = get_runtime_config_path(base_dir)
    payload = read_json(path, {})
    if not isinstance(payload, MappingABC):
        return config

    for section_name, defaults in DEFAULT_RUNTIME_CONFIG.items():
        section_payload = payload.get(section_name, {})
        if not isinstance(section_payload, MappingABC):
            continue
        for key, fallback in defaults.items():
            incoming = section_payload.get(key, fallback)
            if isinstance(fallback, bool):
                config[section_name][key] = _normalize_bool(incoming, fallback)
                continue
            if isinstance(fallback, MappingABC):
                config[section_name][key] = _normalize_mapping(incoming, fallback)
                continue
            if isinstance(fallback, list):
                config[section_name][key] = incoming if isinstance(incoming, list) else deepcopy(fallback)
                continue
            if isinstance(fallback, float):
                try:
                    config[section_name][key] = max(0.0, float(incoming))
                except Exception:
                    config[section_name][key] = float(fallback)
                continue
            minimum = 1
            if section_name == "detail_view" and key == "detail_target_fps":
                config[section_name][key] = min(
                    30,
                    _normalize_positive_int(incoming, fallback=fallback, minimum=1),
                )
                continue
            if key.endswith("_ms"):
                minimum = 10
            elif "max_payload" in key:
                minimum = 0
            elif "max_width" in key:
                minimum = 160
            elif "jpeg_quality" in key:
                minimum = 40
            config[section_name][key] = _normalize_positive_int(
                incoming,
                fallback=fallback,
                minimum=minimum,
            )
    return config
