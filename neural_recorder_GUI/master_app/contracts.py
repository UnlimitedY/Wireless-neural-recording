import json
import os
import re
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional


MASTER_SLOT_COUNT = 3
MASTER_CONFIG_FILENAME = "master_console_cages.json"

REQUIRED_PROFILE_FIELDS = (
    "main_serial_port",
    "rf_serial_port",
    "habits_serial_port",
    "camera_id",
    "lfp_save_path",
    "mode3_save_path",
    "mode1_save_path",
    "mode2_save_path",
    "video_save_path",
    "mice_id_directory",
)

OPTIONAL_PROFILE_FIELDS = (
    "cage_index",
    "recommended_esb_channel",
    "auto_connect_main_serial",
    "auto_connect_rf",
    "auto_connect_habits",
    "charging_guard",
)


@dataclass(frozen=True)
class SlotConfig:
    slot_id: str
    label: str
    profile_path: str
    enabled: bool
    auto_start: bool


def _load_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _normalize_slot_id(value: Any, index: int) -> str:
    candidate = str(value or "").strip()
    if candidate:
        return candidate
    return f"cage_{index + 1}"


def _normalize_label(value: Any, index: int) -> str:
    candidate = str(value or "").strip()
    if candidate:
        return candidate
    return f"Cage {index + 1}"


def _infer_profile_index(profile_path: str, payload: Mapping[str, Any]) -> Optional[int]:
    raw_value = payload.get("cage_index", None)
    if raw_value is not None and str(raw_value).strip() != "":
        try:
            return int(raw_value)
        except Exception:
            pass
    basename = os.path.basename(str(profile_path or ""))
    for pattern in (r"cage[_-]?(\d+)", r"slot[_-]?(\d+)"):
        match = re.search(pattern, basename, flags=re.IGNORECASE)
        if match:
            try:
                return int(match.group(1))
            except Exception:
                pass
    return None


def _resolve_path(base_dir: str, path_value: Any) -> str:
    path_str = str(path_value or "").strip()
    if not path_str:
        raise ValueError("profile_path is required")
    if os.path.isabs(path_str):
        return os.path.normpath(path_str)
    return os.path.normpath(os.path.join(base_dir, path_str))


def extract_mouse_id_from_directory(path_value: Any) -> str:
    path_str = str(path_value or "").strip()
    if not path_str:
        return ""
    normalized = os.path.normpath(path_str)
    mouse_id = os.path.basename(normalized)
    if not mouse_id:
        mouse_id = os.path.basename(os.path.dirname(normalized))
    return str(mouse_id or "").strip()


def validate_slot_profile(payload: Mapping[str, Any], profile_path: str) -> Dict[str, Any]:
    if not isinstance(payload, MappingABC):
        raise ValueError(f"Profile must be a JSON object: {profile_path}")

    normalized = dict(payload)
    main_serial_port = (
        normalized.get("main_serial_port")
        or normalized.get("neural_serial_port")
        or normalized.get("serial_port")
    )
    if main_serial_port is not None:
        normalized["main_serial_port"] = main_serial_port
    inferred_index = _infer_profile_index(profile_path, normalized)
    if ("camera_id" not in normalized or str(normalized.get("camera_id", "")).strip() == "") and inferred_index is not None:
        normalized["camera_id"] = inferred_index - 1
    if inferred_index is not None:
        normalized.setdefault("cage_index", inferred_index)
    missing = [field for field in REQUIRED_PROFILE_FIELDS if field not in normalized]
    if missing:
        raise ValueError(
            f"Profile {profile_path} is missing required fields: {', '.join(missing)}"
        )

    for field in REQUIRED_PROFILE_FIELDS:
        value = normalized.get(field)
        if field == "camera_id":
            try:
                normalized[field] = int(value)
            except Exception as exc:
                raise ValueError(f"Invalid camera_id in {profile_path}: {value!r}") from exc
            continue
        if value is None or str(value).strip() == "":
            raise ValueError(f"Profile field {field} must not be empty: {profile_path}")
        normalized[field] = str(value).strip()

    for field in OPTIONAL_PROFILE_FIELDS:
        if field in normalized and field.startswith("auto_connect_"):
            normalized[field] = _normalize_bool(normalized.get(field), default=False)

    normalized.setdefault("auto_connect_main_serial", False)
    normalized.setdefault("auto_connect_rf", False)
    normalized.setdefault("auto_connect_habits", False)
    normalized["profile_path"] = os.path.normpath(profile_path)
    return normalized


def load_slot_profile(profile_path: str) -> Dict[str, Any]:
    payload = _load_json_file(profile_path)
    return validate_slot_profile(payload, profile_path)


def load_master_console_config(config_path: str) -> List[SlotConfig]:
    base_dir = os.path.dirname(os.path.abspath(config_path))
    payload = _load_json_file(config_path)
    if not isinstance(payload, list):
        raise ValueError(f"Master config must be a list: {config_path}")
    if len(payload) != MASTER_SLOT_COUNT:
        raise ValueError(
            f"Master config must define exactly {MASTER_SLOT_COUNT} cages: {config_path}"
        )

    seen_ids = set()
    slots: List[SlotConfig] = []
    for index, item in enumerate(payload):
        if not isinstance(item, MappingABC):
            raise ValueError(f"Cage entry #{index + 1} must be a JSON object")
        slot_id = _normalize_slot_id(item.get("id"), index)
        if slot_id in seen_ids:
            raise ValueError(f"Duplicate cage id in master config: {slot_id}")
        seen_ids.add(slot_id)
        slot = SlotConfig(
            slot_id=slot_id,
            label=_normalize_label(item.get("label"), index),
            profile_path=_resolve_path(base_dir, item.get("profile_path")),
            enabled=_normalize_bool(item.get("enabled"), default=True),
            auto_start=_normalize_bool(item.get("auto_start"), default=False),
        )
        slots.append(slot)
    return slots


def build_default_slot_status(slot_id: str, label: str) -> Dict[str, Any]:
    return {
        "slot_id": slot_id,
        "label": label,
        "state": "idle",
        "profile_loaded": False,
        "detail_attached": False,
        "last_error": "",
        "last_update_epoch": 0.0,
        "ui_busy_states": {
            "neural_connection": False,
            "rf_connection": False,
            "camera": False,
            "sampling": False,
            "saving": False,
            "detail": False,
        },
        "telemetry": {
            "performance": {
                "runtime_cache_flush_duration_ms": 0.0,
                "runtime_cache_history_flush_duration_ms": 0.0,
                "detail_payload_bytes": 0,
                "detail_payload_interval_ms": 0.0,
                "detail_throttled": False,
                "detail_dropped_frames": 0,
                "detail_last_skip_reason": "",
                "slot_scheduler_trace_seq": 0,
                "slot_scheduler_trace": [],
                "camera_preview_payload_bytes": 0,
                "packet_decode_ms": 0.0,
                "save_writer_lag": 0,
            },
        },
        "neural": {
            "connected": False,
            "command_busy": False,
            "command_action": "",
            "connection_state": "idle",
            "port": "",
            "active_mode": "Idle",
            "sampling": False,
            "save_mode": "",
            "save_route": {
                "data_mode": "",
                "path_role": "",
            },
            "imu_mode": 2,
            "imu_mode_text": "Accel",
            "mode3_reref_pending": False,
            "mode3_reref_target_mode": 0,
            "spike_filter_enabled": False,
            "spike_filter_low_cut_hz": 300.0,
            "spike_filter_high_cut_hz": 3000.0,
            "spike_filter_sample_rate_hz": 20000.0,
            "rc_series_resistor_kohm": 220.0,
            "rc_shunt_cap_pf": 47.0,
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
            "global_save_enabled": True,
            "mode3_reref_mode": 0,
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
            "serial_read_gap_trace_seq": 0,
            "serial_read_gap_trace": [],
            "detail_throttled": False,
            "detail_dropped_frames": 0,
            "detail_last_skip_reason": "",
            "read_batch_bytes_by_mode": {
                "mode0": 0.0,
                "mode1": 0.0,
                "mode2": 0.0,
                "mode3": 0.0,
            },
            "read_batch_packets_by_mode": {
                "mode0": 0.0,
                "mode1": 0.0,
                "mode2": 0.0,
                "mode3": 0.0,
            },
            "read_batch_packet_redline_by_mode": {
                "mode0": 122,
                "mode1": 133,
                "mode2": 116,
                "mode3": 168,
            },
            "read_batch_packet_warn_by_mode": {
                "mode0": 98,
                "mode1": 107,
                "mode2": 93,
                "mode3": 135,
            },
            "gui_update_intervals_by_mode": {
                "mode0": 20,
                "mode1": 19,
                "mode2": 57,
                "mode3": 41,
            },
            "gui_update_interval_best_by_mode": {
                "mode0": 20,
                "mode1": 19,
                "mode2": 57,
                "mode3": 41,
            },
            "gui_update_interval_stats": {},
            "gui_update_intervals_by_stream": {
                "mode0_lfp": 20,
                "mode1_raw": 19,
                "mode2_raw": 57,
                "mode3_lfp_esa": 41,
                "mode3_raw": 10,
            },
            "gui_update_interval_best_by_stream": {
                "mode0_lfp": 20,
                "mode1_raw": 19,
                "mode2_raw": 57,
                "mode3_lfp_esa": 41,
                "mode3_raw": 10,
            },
            "gui_update_interval_stream_stats": {},
            "packet_loss_percent_1h": 0.0,
            "packet_loss_expected_1h": 0,
            "packet_loss_state": "unknown",
            "writer_lag": 0,
            "save_queue_drop_count": 0,
            "packet_gap_event_count": 0,
            "packet_gap_missing": 0,
            "packet_gap_summary": "",
            "packet_gap_diagnostics": {},
            "progress_percent": 0.0,
            "run_time_min": 0.0,
            "impedance_test_running": False,
            "impedance_progress_percent": 0.0,
            "impedance_status_text": "Ready",
            "impedance_last_result": {},
            "impedance_history_count": 0,
            "impedance_result_seq": 0,
            "mode1_raw_channel": 0,
            "mode3_raw_channel": 0,
            "mode2_channels": list(range(16)),
            "auto_threshold_running": False,
            "auto_threshold_channel": -1,
            "rssi": 0.0,
            "power_guard": {
                "valid": False,
                "state": 0,
                "state_name": "unknown",
                "low_stop_counter": 0,
                "flags": 0,
                "low_battery_hold": False,
                "recovering": False,
                "reboot_pending": False,
                "skip_auto_recovery": False,
                "auto_recovery_attempted": False,
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
                "read_batch_bytes_by_mode": {
                    "mode0": 0.0,
                    "mode1": 0.0,
                    "mode2": 0.0,
                    "mode3": 0.0,
                },
                "read_batch_packets_by_mode": {
                    "mode0": 0.0,
                    "mode1": 0.0,
                    "mode2": 0.0,
                    "mode3": 0.0,
                },
                "read_batch_packet_redline_by_mode": {
                    "mode0": 122,
                    "mode1": 133,
                    "mode2": 116,
                    "mode3": 168,
                },
                "read_batch_packet_warn_by_mode": {
                    "mode0": 98,
                    "mode1": 107,
                    "mode2": 93,
                    "mode3": 135,
                },
                "gui_update_intervals_by_mode": {
                    "mode0": 20,
                    "mode1": 19,
                    "mode2": 57,
                    "mode3": 41,
                },
                "gui_update_intervals_by_stream": {
                    "mode0_lfp": 20,
                    "mode1_raw": 19,
                    "mode2_raw": 57,
                    "mode3_lfp_esa": 41,
                    "mode3_raw": 10,
                },
                "runtime_cache_flush_duration_ms": 0.0,
                "runtime_cache_write_failures": 0,
                "packet_gap_event_count": 0,
                "packet_gap_missing": 0,
                "packet_gap_diagnostics": {},
            },
        },
        "rf": {
            "connected": False,
            "port": "",
            "power_state": "unknown",
            "recommended_esb_channel": None,
            "command_busy": False,
            "command_action": "",
            "connection_state": "idle",
            "reconnect_attempts": 0,
            "last_reconnect_epoch": 0.0,
            "last_failure_reason": "",
            "desired_power_state": "unknown",
            "consecutive_query_failures": 0,
            "last_query_attempt_epoch": 0.0,
            "last_success_query_epoch": 0.0,
            "last_recovery_diagnosis": "",
        },
        "habits": {
            "connected": False,
            "port": "",
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
            "mouse_directory": "",
            "mouse_id": "",
            "selected_data_dir": "",
            "start_date": "",
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
            "mode3_trial_trigger_last_check_epoch": 0.0,
            "mode3_trial_trigger_last_check_source": "",
            "mode3_trial_trigger_last_apply_epoch": 0.0,
            "low_battery_mode0_training_target": False,
            "low_battery_mode0_training_active": False,
            "low_battery_mode0_training_reason": "",
            "read_all_seq": 0,
            "protocol_flow": {},
            "sd_download_active": False,
            "sd_download_status_text": "Idle",
            "sd_download_target_dir": "",
            "sd_download_last_error": "",
        },
        "camera": {
            "open": False,
            "recording": False,
            "camera_id": None,
            "preview_enabled": False,
            "recording_path": "",
            "health_state": "ok",
            "reopen_attempts": 0,
            "last_frame_age_sec": 0.0,
            "last_recovery_epoch": 0.0,
            "recovery_segment_count": 0,
        },
        "battery": {
            "rsoc": 0.0,
            "reported_rsoc": 0.0,
            "voltage": 0.0,
            "stat": 0.0,
            "capacity_mAh": 24.0,
            "bq25176_valid": False,
            "bq25176_raw_stat": None,
            "bq25176_pg_raw": None,
            "bq25176_stat_raw": None,
            "bq25176_power_good": False,
            "bq25176_charging": False,
            "bq25176_power_state_text": "Power --",
            "bq25176_power_state_level": "neutral",
            "bq25176_charge_state_text": "Charge --",
            "bq25176_charge_state_level": "neutral",
            "bq25176_status_text": "BQ25176 status unknown",
            "bq25176_fault_possible": False,
            "bq25176_fault_reason": "",
            "health_state": "unknown",
            "low_threshold_percent": 20.0,
        },
        "charging_guard": {
            "enabled": False,
            "status_text": "Off",
            "roi_norm": [0.1, 0.5, 0.3, 0.2],
            "dark_threshold": 90,
            "min_area_ratio": 0.30,
            "check_interval_ms": 10000,
            "required_hits": 6,
            "warning_duration_ms": 3000,
            "fan_enabled": True,
            "mouse_present": False,
            "area_ratio": 0.0,
            "reason": "not checked",
            "hit_count": 0,
            "warning_active": False,
            "detection_text": "Charging Guard OFF | Mouse: NO | area 0.0% | not checked",
            "preview_last_detection_epoch": 0.0,
        },
    }


def make_slot_command(command_type: str, **kwargs: Any) -> Dict[str, Any]:
    payload = {"type": str(command_type)}
    payload.update(kwargs)
    return payload


def merge_slot_status(base_status: Dict[str, Any], updates: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    merged = dict(base_status)
    if not updates:
        return merged
    for key, value in updates.items():
        if isinstance(value, MappingABC) and isinstance(merged.get(key), MappingABC):
            nested = dict(merged[key])
            nested.update(dict(value))
            merged[key] = nested
        else:
            merged[key] = value
    return merged
