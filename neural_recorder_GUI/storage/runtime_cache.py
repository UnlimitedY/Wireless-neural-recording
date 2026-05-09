import json
import os
import tempfile
import time
from typing import Any, Dict, Optional


_RETRY_DELAYS_SECONDS = (
    [0.02] * 10
    + [0.05] * 20
    + [0.1] * 20
)


def _files_have_same_content(path_a: str, path_b: str) -> bool:
    try:
        if not (os.path.exists(path_a) and os.path.exists(path_b)):
            return False
        if os.path.getsize(path_a) != os.path.getsize(path_b):
            return False
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            while True:
                chunk_a = fa.read(65536)
                chunk_b = fb.read(65536)
                if chunk_a != chunk_b:
                    return False
                if not chunk_a:
                    return True
    except Exception:
        return False


def _atomic_replace_file(temp_path: str, path: str) -> None:
    last_error = None
    for attempt, delay in enumerate(_RETRY_DELAYS_SECONDS):
        try:
            os.replace(temp_path, path)
            last_error = None
            return
        except PermissionError as exc:
            last_error = exc
            if _files_have_same_content(temp_path, path):
                return
            time.sleep(delay)
    try:
        os.replace(temp_path, path)
        return
    except PermissionError as exc:
        last_error = exc
        if _files_have_same_content(temp_path, path):
            return
    if last_error is not None:
        raise last_error


def atomic_write_json(path: str, payload: Any, durable: bool = True) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if durable:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            else:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            if durable:
                f.flush()
                os.fsync(f.fileno())
        # On Windows, a concurrent reader can briefly prevent os.replace()
        # from swapping the file. Retry a few times so polling readers in the
        # master console do not crash the slot writer during shutdown.
        _atomic_replace_file(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def atomic_write_bytes(path: str, payload: bytes, durable: bool = True) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".bin", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            if durable:
                f.flush()
                os.fsync(f.fileno())
        _atomic_replace_file(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def read_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def read_bytes(path: str, default: Optional[bytes] = None) -> Optional[bytes]:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return default


class SlotRuntimeCache:
    def __init__(self, root_dir: str, slot_id: str):
        self.root_dir = os.path.join(os.path.abspath(root_dir), slot_id)
        self.slot_id = slot_id
        self._json_write_cache: Dict[str, str] = {}
        self._bytes_write_cache: Dict[str, bytes] = {}
        self._dirty_flags: Dict[str, bool] = {}
        self._sequences: Dict[str, int] = {}
        self.status_path = os.path.join(self.root_dir, "status.json")
        self.battery_history_path = os.path.join(self.root_dir, "battery_history.json")
        self.battery_latest_path = os.path.join(self.root_dir, "battery_latest.json")
        self.impedance_history_path = os.path.join(self.root_dir, "impedance_history.json")
        self.habits_live_path = os.path.join(self.root_dir, "habits_live.json")
        self.habits_trial_data_path = os.path.join(self.root_dir, "habits_trial_data.json")
        self.habits_event_data_path = os.path.join(self.root_dir, "habits_event_data.json")
        self.habits_state_data_path = os.path.join(self.root_dir, "habits_state_data.json")
        self.mode_switches_path = os.path.join(self.root_dir, "mode_switches.json")
        self.last_profile_path = os.path.join(self.root_dir, "last_profile.json")
        self.detail_buffer_meta_path = os.path.join(self.root_dir, "detail_buffer.json")
        self.detail_buffer_payload_path = os.path.join(self.root_dir, "detail_buffer_payload.bin")
        self.system_log_path = os.path.join(self.root_dir, "system_log.json")
        self.mode3_thresholds_path = os.path.join(self.root_dir, "mode3_thresholds.json")
        self.camera_preview_meta_path = os.path.join(self.root_dir, "camera_preview.json")
        self.camera_preview_frame_path = os.path.join(self.root_dir, "camera_preview.jpg")
        os.makedirs(self.root_dir, exist_ok=True)

    def mark_dirty(self, key: str) -> None:
        self._dirty_flags[str(key)] = True

    def consume_dirty(self, key: str, default: bool = False) -> bool:
        normalized = str(key)
        dirty = bool(self._dirty_flags.get(normalized, default))
        self._dirty_flags[normalized] = False
        return dirty

    def is_dirty(self, key: str, default: bool = False) -> bool:
        return bool(self._dirty_flags.get(str(key), default))

    def next_sequence(self, key: str) -> int:
        normalized = str(key)
        value = int(self._sequences.get(normalized, 0) or 0) + 1
        self._sequences[normalized] = value
        return value

    def clear_transient_files(self) -> None:
        transient_json_paths = (
            self.status_path,
            self.battery_latest_path,
            self.habits_live_path,
            self.detail_buffer_meta_path,
            self.camera_preview_meta_path,
        )
        transient_bytes_paths = (
            self.detail_buffer_payload_path,
            self.camera_preview_frame_path,
        )
        for path in transient_json_paths + transient_bytes_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        try:
            for filename in os.listdir(self.root_dir):
                if filename.startswith("detail_buffer_payload_") and filename.endswith(".bin"):
                    try:
                        os.remove(os.path.join(self.root_dir, filename))
                    except OSError:
                        pass
        except OSError:
            pass
        for path in transient_json_paths:
            self._json_write_cache.pop(path, None)
        for path in transient_bytes_paths:
            self._bytes_write_cache.pop(path, None)

    def clear_mouse_scoped_history(self) -> None:
        mouse_scoped_paths = (
            self.battery_history_path,
            self.impedance_history_path,
            self.habits_trial_data_path,
            self.habits_event_data_path,
            self.habits_state_data_path,
            self.mode_switches_path,
            self.system_log_path,
        )
        for path in mouse_scoped_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
            self._json_write_cache.pop(path, None)

    def _write_json_if_changed(self, path: str, payload: Any, durable: bool = True) -> None:
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if self._json_write_cache.get(path) == normalized:
            return
        atomic_write_json(path, payload, durable=durable)
        self._json_write_cache[path] = normalized

    def _write_bytes_if_changed(self, path: str, payload: bytes, durable: bool = True) -> None:
        payload = payload or b""
        if self._bytes_write_cache.get(path) == payload:
            return
        atomic_write_bytes(path, payload, durable=durable)
        self._bytes_write_cache[path] = payload

    def write_status(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.status_path, payload, durable=False)

    def read_status(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.status_path, default or {})

    def write_battery_history(self, payload: Any) -> None:
        self._write_json_if_changed(self.battery_history_path, payload, durable=False)

    def read_battery_history(self, default: Optional[Any] = None) -> Any:
        return read_json(self.battery_history_path, default if default is not None else [])

    def write_battery_latest(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.battery_latest_path, payload, durable=False)

    def read_battery_latest(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.battery_latest_path, default or {})

    def write_habits_live(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.habits_live_path, payload, durable=False)

    def read_habits_live(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.habits_live_path, default or {})

    def write_impedance_history(self, payload: Any) -> None:
        self._write_json_if_changed(self.impedance_history_path, payload, durable=False)

    def read_impedance_history(self, default: Optional[Any] = None) -> Any:
        return read_json(self.impedance_history_path, default if default is not None else [])

    def write_habits_trial_data(self, payload: Any) -> None:
        self._write_json_if_changed(self.habits_trial_data_path, payload, durable=False)

    def read_habits_trial_data(self, default: Optional[Any] = None) -> Any:
        return read_json(self.habits_trial_data_path, default if default is not None else [])

    def write_habits_event_data(self, payload: Any) -> None:
        self._write_json_if_changed(self.habits_event_data_path, payload, durable=False)

    def read_habits_event_data(self, default: Optional[Any] = None) -> Any:
        return read_json(self.habits_event_data_path, default if default is not None else [])

    def write_habits_state_data(self, payload: Any) -> None:
        self._write_json_if_changed(self.habits_state_data_path, payload, durable=False)

    def read_habits_state_data(self, default: Optional[Any] = None) -> Any:
        return read_json(self.habits_state_data_path, default if default is not None else [])

    def write_mode_switches(self, payload: Any) -> None:
        self._write_json_if_changed(self.mode_switches_path, payload, durable=False)

    def read_mode_switches(self, default: Optional[Any] = None) -> Any:
        return read_json(self.mode_switches_path, default if default is not None else [])

    def write_last_profile(self, payload: Any) -> None:
        self._write_json_if_changed(self.last_profile_path, payload)

    def read_last_profile(self, default: Optional[Any] = None) -> Any:
        return read_json(self.last_profile_path, default if default is not None else {})

    def write_detail_buffer_meta(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.detail_buffer_meta_path, payload, durable=False)

    def read_detail_buffer_meta(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.detail_buffer_meta_path, default or {})

    def write_system_log(self, payload: Any) -> None:
        self._write_json_if_changed(self.system_log_path, payload, durable=False)

    def read_system_log(self, default: Optional[Any] = None) -> Any:
        return read_json(self.system_log_path, default if default is not None else [])

    def clear_system_log(self) -> None:
        try:
            if os.path.exists(self.system_log_path):
                os.remove(self.system_log_path)
        except OSError:
            pass
        self._json_write_cache.pop(self.system_log_path, None)

    def write_mode3_thresholds(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.mode3_thresholds_path, payload)

    def read_mode3_thresholds(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.mode3_thresholds_path, default or {})

    def write_camera_preview_meta(self, payload: Dict[str, Any]) -> None:
        self._write_json_if_changed(self.camera_preview_meta_path, payload, durable=False)

    def read_camera_preview_meta(self, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return read_json(self.camera_preview_meta_path, default or {})

    def write_camera_preview_frame(self, payload: bytes) -> None:
        self._write_bytes_if_changed(self.camera_preview_frame_path, payload, durable=False)
