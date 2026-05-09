import copy
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

try:
    from ..storage.runtime_cache import atomic_write_json, read_json
except ImportError:
    from storage.runtime_cache import atomic_write_json, read_json


PROTOCOL_FLOW_FILENAME = "dbruleswitch_protocol_flow.json"
DEFAULT_PROTOCOL_SEQUENCE = [0, 1, 2, 3, 4, 7, 8, 9, 10, 7, 8, 9, 10, 5, 6]
SIDE50_PROTOCOLS = {5, 7, 9}
RETENTION_PROTOCOLS = {6, 8, 10}
PERF_MODE_TO_CODE = {"none": 0, "side50": 1, "retention100": 2, "protocol0_time": 3}
CODE_TO_PERF_MODE = {value: key for key, value in PERF_MODE_TO_CODE.items()}


def utc_timestamp() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def default_condition(protocol: int) -> Dict[str, Any]:
    protocol = int(protocol)
    if protocol in SIDE50_PROTOCOLS:
        return {
            "trial_min": 250,
            "time_min_sec": 0,
            "perf_mode": "side50",
            "threshold": 75,
            "window_size": 50,
            "required_count": 1,
        }
    if protocol in RETENTION_PROTOCOLS:
        return {
            "trial_min": 1000,
            "time_min_sec": 0,
            "perf_mode": "retention100",
            "threshold": 75,
            "window_size": 100,
            "required_count": 10,
        }
    if protocol == 0:
        return {
            "trial_min": 0,
            "time_min_sec": 7 * 24 * 60 * 60,
            "perf_mode": "none",
            "threshold": 0,
            "window_size": 0,
            "required_count": 1,
        }
    if 1 <= protocol <= 4:
        return {
            "trial_min": 101,
            "time_min_sec": 0,
            "perf_mode": "none",
            "threshold": 0,
            "window_size": 0,
            "required_count": 1,
        }
    return {
        "trial_min": 0,
        "time_min_sec": 0,
        "perf_mode": "none",
        "threshold": 0,
        "window_size": 0,
        "required_count": 1,
    }


def normalize_condition(protocol: int, condition: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    normalized = default_condition(int(protocol))
    if isinstance(condition, dict):
        normalized.update({
            key: condition.get(key, normalized[key])
            for key in ("trial_min", "time_min_sec", "perf_mode", "threshold", "window_size", "required_count")
            if key in normalized
        })
    mode = str(normalized.get("perf_mode", "none") or "none").strip().lower()
    if mode not in {"none", "side50", "retention100", "protocol0_time"}:
        mode = "none"
    normalized["perf_mode"] = mode
    for key in ("trial_min", "time_min_sec", "threshold", "window_size", "required_count"):
        try:
            normalized[key] = max(0, int(float(normalized.get(key, 0) or 0)))
        except Exception:
            normalized[key] = int(default_condition(protocol).get(key, 0) or 0)
    if normalized["perf_mode"] == "side50":
        normalized["window_size"] = max(1, normalized["window_size"] or 50)
        normalized["required_count"] = 1
    elif normalized["perf_mode"] == "retention100":
        normalized["window_size"] = max(1, normalized["window_size"] or 100)
        normalized["required_count"] = max(1, normalized["required_count"] or 10)
    return normalized


def build_block(block_index: int, protocol: int, block_id: Optional[str] = None) -> Dict[str, Any]:
    protocol = int(protocol)
    return {
        "block_id": str(block_id or f"block_{int(block_index):03d}_p{protocol}"),
        "protocol": protocol,
        "fixed": False,
        "started_at": "",
        "completed_at": "",
        "conditions": default_condition(protocol),
        "completion_metrics": {},
        "transition_reason": "",
    }


def default_protocol_flow_state() -> Dict[str, Any]:
    blocks = [build_block(index, protocol) for index, protocol in enumerate(DEFAULT_PROTOCOL_SEQUENCE)]
    return {
        "version": 1,
        "created_at": utc_timestamp(),
        "updated_at": utc_timestamp(),
        "current_block_id": blocks[0]["block_id"] if blocks else "",
        "blocks": blocks,
        "transition_log": [],
        "last_request": {},
        "last_synced_conditions": {},
    }


def protocol_flow_path(mouse_directory: str) -> str:
    directory = str(mouse_directory or "").strip()
    if not directory:
        return ""
    return os.path.join(directory, PROTOCOL_FLOW_FILENAME)


def normalize_protocol_flow_state(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return default_protocol_flow_state()
    state = copy.deepcopy(payload)
    blocks = state.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        return default_protocol_flow_state()
    used_ids = set()
    normalized_blocks: List[Dict[str, Any]] = []
    for index, raw in enumerate(blocks):
        if not isinstance(raw, dict):
            continue
        try:
            protocol = int(raw.get("protocol", 0) or 0)
        except Exception:
            protocol = 0
        protocol = max(0, min(10, protocol))
        block_id = str(raw.get("block_id", "") or f"block_{index:03d}_p{protocol}").strip()
        if not block_id or block_id in used_ids:
            block_id = f"block_{index:03d}_p{protocol}"
        used_ids.add(block_id)
        block = build_block(index, protocol, block_id=block_id)
        block["fixed"] = bool(raw.get("fixed", False))
        block["started_at"] = str(raw.get("started_at", "") or "")
        block["completed_at"] = str(raw.get("completed_at", "") or "")
        block["conditions"] = normalize_condition(protocol, raw.get("conditions"))
        block["completion_metrics"] = dict(raw.get("completion_metrics", {}) or {})
        block["transition_reason"] = str(raw.get("transition_reason", "") or "")
        normalized_blocks.append(block)
    if not normalized_blocks:
        return default_protocol_flow_state()
    state["version"] = int(state.get("version", 1) or 1)
    state["blocks"] = normalized_blocks
    valid_ids = {block["block_id"] for block in normalized_blocks}
    current_block_id = str(state.get("current_block_id", "") or "")
    if current_block_id not in valid_ids:
        first_open = next((block for block in normalized_blocks if not bool(block.get("fixed"))), normalized_blocks[-1])
        current_block_id = str(first_open.get("block_id", ""))
    state["current_block_id"] = current_block_id
    state["created_at"] = str(state.get("created_at", "") or utc_timestamp())
    state["updated_at"] = str(state.get("updated_at", "") or utc_timestamp())
    state["transition_log"] = list(state.get("transition_log", []) or [])[-500:]
    state["last_request"] = dict(state.get("last_request", {}) or {})
    state["last_synced_conditions"] = dict(state.get("last_synced_conditions", {}) or {})
    return state


class ProtocolFlowStore:
    def __init__(self, mouse_directory: str = ""):
        self.mouse_directory = str(mouse_directory or "").strip()
        self.path = protocol_flow_path(self.mouse_directory)
        self.state = normalize_protocol_flow_state(None)
        self.load()

    def set_mouse_directory(self, mouse_directory: str) -> None:
        mouse_directory = str(mouse_directory or "").strip()
        if mouse_directory == self.mouse_directory:
            return
        self.mouse_directory = mouse_directory
        self.path = protocol_flow_path(self.mouse_directory)
        self.load()

    def load(self) -> Dict[str, Any]:
        if self.path and os.path.exists(self.path):
            payload = read_json(self.path, None)
            self.state = normalize_protocol_flow_state(payload if isinstance(payload, dict) else None)
        else:
            self.state = normalize_protocol_flow_state(None)
            self.save()
        return copy.deepcopy(self.state)

    def save(self) -> bool:
        if not self.path:
            return False
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.state["updated_at"] = utc_timestamp()
        atomic_write_json(self.path, self.state)
        return True

    def get_state(self) -> Dict[str, Any]:
        return copy.deepcopy(self.state)

    def replace_state(self, state: Dict[str, Any]) -> Dict[str, Any]:
        previous = self.state
        incoming = normalize_protocol_flow_state(state)
        previous_block_list = [
            block for block in list(previous.get("blocks", []) or [])
            if isinstance(block, dict)
        ]
        previous_blocks = {str(block.get("block_id", "")): block for block in previous_block_list}
        for block in incoming["blocks"]:
            previous_block = previous_blocks.get(str(block.get("block_id", "")))
            if previous_block and bool(previous_block.get("fixed", False)):
                block.clear()
                block.update(copy.deepcopy(previous_block))
        for fixed_index, fixed_block in enumerate(previous_block_list):
            if not bool(fixed_block.get("fixed", False)):
                continue
            fixed_id = str(fixed_block.get("block_id", ""))
            incoming["blocks"] = [
                block for block in incoming["blocks"]
                if str(block.get("block_id", "")) != fixed_id
            ]
            insert_at = min(fixed_index, len(incoming["blocks"]))
            incoming["blocks"].insert(insert_at, copy.deepcopy(fixed_block))
        self.state = normalize_protocol_flow_state(incoming)
        self.save()
        return self.get_state()

    def find_block_index(self, block_id: str) -> int:
        block_id = str(block_id or "")
        for index, block in enumerate(self.state.get("blocks", []) or []):
            if str(block.get("block_id", "")) == block_id:
                return index
        return -1

    def current_block(self) -> Optional[Dict[str, Any]]:
        index = self.find_block_index(str(self.state.get("current_block_id", "") or ""))
        if index < 0:
            return None
        return copy.deepcopy(self.state["blocks"][index])

    def set_current_block_by_protocol(self, protocol: int, *, reason: str = "manual_override") -> Optional[Dict[str, Any]]:
        protocol = max(0, min(10, int(protocol)))
        blocks = self.state.get("blocks", []) or []
        current_index = self.find_block_index(str(self.state.get("current_block_id", "") or ""))
        if current_index < 0:
            current_index = 0
        for index in range(current_index, len(blocks)):
            block = blocks[index]
            if bool(block.get("fixed", False)):
                continue
            if int(block.get("protocol", -1) or -1) == protocol:
                block["started_at"] = block.get("started_at") or utc_timestamp()
                self.state["current_block_id"] = str(block.get("block_id", ""))
                self._append_log({
                    "timestamp": utc_timestamp(),
                    "event": "manual_current_protocol",
                    "to_protocol": protocol,
                    "to_block_id": block.get("block_id", ""),
                    "reason": reason,
                })
                self.save()
                return copy.deepcopy(block)
        new_block = build_block(len(blocks), protocol, block_id=f"block_{len(blocks):03d}_p{protocol}")
        new_block["started_at"] = utc_timestamp()
        blocks.append(new_block)
        self.state["current_block_id"] = new_block["block_id"]
        self._append_log({
            "timestamp": utc_timestamp(),
            "event": "manual_current_protocol",
            "to_protocol": protocol,
            "to_block_id": new_block["block_id"],
            "reason": reason,
        })
        self.save()
        return copy.deepcopy(new_block)

    def update_current_conditions(self, conditions: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        index = self.find_block_index(str(self.state.get("current_block_id", "") or ""))
        if index < 0:
            return None
        block = self.state["blocks"][index]
        if bool(block.get("fixed", False)):
            return None
        protocol = int(block.get("protocol", 0) or 0)
        block["conditions"] = normalize_condition(protocol, conditions)
        if not block.get("started_at"):
            block["started_at"] = utc_timestamp()
        self.save()
        return copy.deepcopy(block)

    def observe_current_protocol(self, protocol: int, metrics: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        protocol = max(0, min(10, int(protocol)))
        blocks = self.state.get("blocks", []) or []
        current_index = self.find_block_index(str(self.state.get("current_block_id", "") or ""))
        if current_index < 0:
            current_index = 0
        current_block = blocks[current_index] if current_index < len(blocks) else None
        if current_block and int(current_block.get("protocol", -1) or -1) == protocol:
            if not current_block.get("started_at"):
                current_block["started_at"] = utc_timestamp()
                self.save()
            return copy.deepcopy(current_block)
        match_index = -1
        for index in range(current_index + 1, len(blocks)):
            block = blocks[index]
            if int(block.get("protocol", -1) or -1) == protocol:
                match_index = index
                break
        if match_index < 0:
            return None
        now = utc_timestamp()
        for index in range(current_index, match_index):
            block = blocks[index]
            if not bool(block.get("fixed", False)):
                block["fixed"] = True
                block["completed_at"] = block.get("completed_at") or now
                block["completion_metrics"] = dict(metrics or {})
                block["transition_reason"] = "firmware_local_fixed_segment"
                self._append_log({
                    "timestamp": now,
                    "event": "firmware_observed_transition",
                    "from_protocol": int(block.get("protocol", 0) or 0),
                    "to_protocol": int(blocks[index + 1].get("protocol", 0) or 0),
                    "from_block_id": block.get("block_id", ""),
                    "to_block_id": blocks[index + 1].get("block_id", ""),
                    "reason": "firmware_local_fixed_segment",
                    "metrics": dict(metrics or {}),
                })
        target = blocks[match_index]
        target["started_at"] = target.get("started_at") or now
        self.state["current_block_id"] = str(target.get("block_id", ""))
        self.save()
        return copy.deepcopy(target)

    def mark_transition(
        self,
        *,
        from_block_id: str,
        to_block_id: str,
        metrics: Dict[str, Any],
        request_id: Any = "",
        reason: str = "condition_met",
    ) -> Optional[Dict[str, Any]]:
        from_index = self.find_block_index(from_block_id)
        to_index = self.find_block_index(to_block_id)
        if from_index < 0 or to_index < 0:
            return None
        from_block = self.state["blocks"][from_index]
        to_block = self.state["blocks"][to_index]
        now = utc_timestamp()
        from_block["fixed"] = True
        from_block["completed_at"] = now
        from_block["completion_metrics"] = dict(metrics or {})
        from_block["transition_reason"] = str(reason or "condition_met")
        if not to_block.get("started_at"):
            to_block["started_at"] = now
        self.state["current_block_id"] = str(to_block.get("block_id", ""))
        entry = {
            "timestamp": now,
            "event": "transition",
            "from_protocol": int(from_block.get("protocol", 0) or 0),
            "to_protocol": int(to_block.get("protocol", 0) or 0),
            "from_block_id": from_block.get("block_id", ""),
            "to_block_id": to_block.get("block_id", ""),
            "request_id": request_id,
            "reason": reason,
            "metrics": dict(metrics or {}),
        }
        self._append_log(entry)
        self.save()
        return copy.deepcopy(to_block)

    def _append_log(self, entry: Dict[str, Any]) -> None:
        log = list(self.state.get("transition_log", []) or [])
        log.append(dict(entry))
        self.state["transition_log"] = log[-500:]


def condition_to_firmware_payload(block: Dict[str, Any]) -> str:
    protocol = int(block.get("protocol", 0) or 0)
    conditions = normalize_condition(protocol, block.get("conditions"))
    perf_mode = PERF_MODE_TO_CODE.get(str(conditions.get("perf_mode", "none")), 0)
    block_id = str(block.get("block_id", "") or "")
    return (
        f"PCOND,{block_id},{protocol},{int(conditions['trial_min'])},"
        f"{int(conditions['time_min_sec'])},"
        f"{perf_mode},{int(conditions['threshold'])},"
        f"{int(conditions['window_size'])},{int(conditions['required_count'])},"
    )


def _dot_join(values: List[Any]) -> str:
    return ".".join(str(int(value or 0)) for value in values)


def _parse_dot_ints(value: Any, length: int, default: int = 0) -> List[int]:
    parts = str(value or "").strip().split(".") if str(value or "").strip() else []
    parsed: List[int] = []
    for index in range(max(0, int(length))):
        try:
            parsed.append(int(float(parts[index])))
        except Exception:
            parsed.append(int(default))
    return parsed


def _field_int(fields: Dict[str, Any], key: str, default: int = 0) -> int:
    try:
        return int(float(fields.get(key, default) or default))
    except Exception:
        return int(default)


def flow_to_firmware_payload(flow_state: Dict[str, Any]) -> str:
    state = normalize_protocol_flow_state(flow_state)
    blocks = list(state.get("blocks", []) or [])[:24]
    if not blocks:
        return ""
    current_block_id = str(state.get("current_block_id", "") or "")
    current_index = 0
    for index, block in enumerate(blocks):
        if str(block.get("block_id", "") or "") == current_block_id:
            current_index = index
            break
    protocols: List[int] = []
    trial_min: List[int] = []
    time_min_sec: List[int] = []
    perf_mode: List[int] = []
    threshold: List[int] = []
    window_size: List[int] = []
    required_count: List[int] = []
    fixed: List[int] = []
    for block in blocks:
        protocol = max(0, min(10, int(block.get("protocol", 0) or 0)))
        conditions = normalize_condition(protocol, block.get("conditions"))
        protocols.append(protocol)
        trial_min.append(int(conditions.get("trial_min", 0) or 0))
        time_min_sec.append(int(conditions.get("time_min_sec", 0) or 0))
        perf_mode.append(PERF_MODE_TO_CODE.get(str(conditions.get("perf_mode", "none")), 0))
        threshold.append(int(conditions.get("threshold", 0) or 0))
        window_size.append(int(conditions.get("window_size", 0) or 0))
        required_count.append(max(1, int(conditions.get("required_count", 1) or 1)))
        fixed.append(1 if bool(block.get("fixed", False)) else 0)
    return (
        f"PFLOW,{current_index},{len(blocks)},"
        f"{_dot_join(protocols)},"
        f"{_dot_join(trial_min)},"
        f"{_dot_join(time_min_sec)},"
        f"{_dot_join(perf_mode)},"
        f"{_dot_join(threshold)},"
        f"{_dot_join(window_size)},"
        f"{_dot_join(required_count)},"
        f"{_dot_join(fixed)},"
    )


def flow_state_from_firmware_fields(
    previous_state: Dict[str, Any],
    fields: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(fields, dict) or "flow_len" not in fields or "flow_protocols" not in fields:
        return None
    previous = normalize_protocol_flow_state(previous_state)
    length = max(1, min(24, _field_int(fields, "flow_len", 0)))
    protocols = [max(0, min(10, value)) for value in _parse_dot_ints(fields.get("flow_protocols"), length, 0)]
    trial_min = [max(0, value) for value in _parse_dot_ints(fields.get("flow_trial_min"), length, 0)]
    time_min_sec = [max(0, value) for value in _parse_dot_ints(fields.get("flow_time_min_sec"), length, 0)]
    perf_mode_codes = [max(0, min(3, value)) for value in _parse_dot_ints(fields.get("flow_perf_mode"), length, 0)]
    thresholds = [max(0, min(100, value)) for value in _parse_dot_ints(fields.get("flow_threshold"), length, 0)]
    windows = [max(0, value) for value in _parse_dot_ints(fields.get("flow_window"), length, 0)]
    required = [max(1, min(100, value)) for value in _parse_dot_ints(fields.get("flow_required"), length, 1)]
    fixed_flags = [1 if value else 0 for value in _parse_dot_ints(fields.get("flow_fixed"), length, 0)]
    previous_blocks = list(previous.get("blocks", []) or [])
    blocks: List[Dict[str, Any]] = []
    transition_log = list(previous.get("transition_log", []) or [])[-500:]
    now = utc_timestamp()
    for index in range(length):
        protocol = int(protocols[index])
        previous_block = previous_blocks[index] if index < len(previous_blocks) else {}
        block_id = (
            str(previous_block.get("block_id", "") or "")
            if int(previous_block.get("protocol", protocol) or protocol) == protocol
            else ""
        )
        block = build_block(index, protocol, block_id=block_id or f"block_{index:03d}_p{protocol}")
        was_fixed = bool(previous_block.get("fixed", False)) if isinstance(previous_block, dict) else False
        is_fixed = bool(fixed_flags[index]) or was_fixed
        block["fixed"] = is_fixed
        block["started_at"] = str(previous_block.get("started_at", "") or "")
        block["completed_at"] = str(previous_block.get("completed_at", "") or "")
        block["completion_metrics"] = dict(previous_block.get("completion_metrics", {}) or {})
        block["transition_reason"] = str(previous_block.get("transition_reason", "") or "")
        block["conditions"] = normalize_condition(protocol, {
            "trial_min": trial_min[index],
            "time_min_sec": time_min_sec[index],
            "perf_mode": CODE_TO_PERF_MODE.get(perf_mode_codes[index], "none"),
            "threshold": thresholds[index],
            "window_size": windows[index],
            "required_count": required[index],
        })
        if is_fixed and not was_fixed:
            block["completed_at"] = block["completed_at"] or now
            block["transition_reason"] = block["transition_reason"] or "firmware_local_flow"
            block["completion_metrics"] = {
                **block["completion_metrics"],
                "source": "firmware_flow",
                "trial_count": _field_int(fields, "trial_count", 0),
                "current_protocol": _field_int(fields, "curr", protocol),
            }
            if index + 1 < length:
                transition_log.append({
                    "timestamp": now,
                    "event": "firmware_local_transition_observed",
                    "from_protocol": protocol,
                    "to_protocol": int(protocols[index + 1]),
                    "from_block_id": block["block_id"],
                    "to_block_id": (
                        str(previous_blocks[index + 1].get("block_id", "") or "")
                        if index + 1 < len(previous_blocks)
                        and int(previous_blocks[index + 1].get("protocol", protocols[index + 1]) or protocols[index + 1]) == int(protocols[index + 1])
                        else f"block_{index + 1:03d}_p{int(protocols[index + 1])}"
                    ),
                    "reason": "firmware_local_flow",
                    "metrics": dict(block["completion_metrics"]),
                })
        blocks.append(block)
    current_index = max(0, min(length - 1, _field_int(fields, "flow_current_index", 0)))
    current_block_id = str(blocks[current_index].get("block_id", "")) if blocks else ""
    if current_block_id:
        blocks[current_index]["started_at"] = blocks[current_index].get("started_at") or now
    state = dict(previous)
    state["blocks"] = blocks
    state["current_block_id"] = current_block_id
    state["transition_log"] = transition_log[-500:]
    state["last_request"] = {}
    state["firmware_progress"] = {
        "updated_at": now,
        "current_protocol": _field_int(fields, "curr", protocols[current_index] if protocols else 0),
        "current_block_index": current_index,
        "trial_count": _field_int(fields, "trial_count", 0),
        "trial_target": _field_int(fields, "trial_target", trial_min[current_index] if trial_min else 0),
        "trial_ready": bool(_field_int(fields, "trial_ready", 0)),
        "time_target_sec": _field_int(fields, "host_time_min_sec", time_min_sec[current_index] if time_min_sec else 0),
        "time_ready": bool(_field_int(fields, "host_time_ready", 0)),
        "host_ready": bool(_field_int(fields, "host_ready", 0)),
        "protocol_switch_safe": bool(_field_int(fields, "protocol_switch_safe", 0)),
        "flow_at_end": bool(_field_int(fields, "flow_at_end", 0)),
        "retention_hits": _field_int(fields, "retention_hits", 0),
        "retention_window_valid": _field_int(fields, "retention_window_valid", 0),
        "retention_window_correct": _field_int(fields, "retention_window_correct", 0),
        "retention_last_window_perf": _field_int(fields, "retention_last_window_perf", 0),
    }
    state["last_synced_conditions"] = dict(previous.get("last_synced_conditions", {}) or {})
    return normalize_protocol_flow_state(state)


def parse_key_value_metrics(parts: List[str]) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for item in parts:
        text = str(item or "").strip()
        if not text or "=" not in text:
            continue
        key, value = text.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            if "." in value:
                metrics[key] = float(value)
            else:
                metrics[key] = int(value)
        except Exception:
            metrics[key] = value
    return metrics
