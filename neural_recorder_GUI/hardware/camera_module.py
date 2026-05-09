import cv2
import time
import os
import datetime
import threading
import multiprocessing as mp
import queue
import subprocess
import sys
import numpy as np
import gc
from typing import Dict, Tuple, Optional

try:
    from ..support.path_utils import build_daily_file_path, get_recordings_directory
    from ..support.windows_runtime import apply_windows_process_role
    from ..services.recovery_utils import build_recovery_segment_path
    from ..services.mouse_platform_monitor import MousePlatformDetector
except ImportError:
    from support.path_utils import build_daily_file_path, get_recordings_directory
    from support.windows_runtime import apply_windows_process_role
    from services.recovery_utils import build_recovery_segment_path
    from services.mouse_platform_monitor import MousePlatformDetector


def _preferred_camera_backends():
    if os.name == "nt":
        return [cv2.CAP_DSHOW, cv2.CAP_MSMF]
    return [cv2.CAP_ANY]

def get_available_cameras():
    """Detect available cameras"""
    available_cameras = []
    
    # 检测前10个摄像头索引
    for i in range(4):
        try:
            cap = None
            for backend in _preferred_camera_backends():
                cap = cv2.VideoCapture(i, backend)
                if cap.isOpened():
                    break
                cap.release()
                cap = None
            if cap is None or not cap.isOpened():
                continue
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            # 尝试读取一帧来确认摄像头工作正常
            ret, frame = cap.read()
            if ret and frame is not None:
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                
                camera_info = {
                    'id': i,
                    'name': f"Camera {i}",
                    'resolution': f"{width}x{height}",
                    'fps': fps if fps > 0 else 30.0
                }
                available_cameras.append(camera_info)
                print(f"Camera {i} detected: {width}x{height}, FPS: {fps}")
            cap.release()
            time.sleep(0.03)
            
        except Exception as e:
            print(f"Error detecting camera {i}: {e}")
            continue
    
    return available_cameras


def _video_compress_worker_entry(video_path: str, bitrate: str = "256k"):
    try:
        apply_windows_process_role("video_compress")
    except Exception:
        pass
    try:
        try:
            from ..workers.video_moviepy_compress_worker import compress_and_replace
        except ImportError:
            from workers.video_moviepy_compress_worker import compress_and_replace
        compress_and_replace(video_path, bitrate=bitrate)
    except Exception:
        pass


def _unique_video_output_path(path: str, max_attempts: int = 999) -> str:
    candidate = str(path or "").strip()
    if not candidate:
        return candidate
    directory = os.path.dirname(candidate)
    if directory:
        try:
            os.makedirs(directory, exist_ok=True)
        except Exception:
            pass
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(candidate)
    for index in range(1, int(max_attempts) + 1):
        next_candidate = f"{stem}_{index:03d}{ext}"
        if not os.path.exists(next_candidate):
            return next_candidate
    suffix = datetime.datetime.now().strftime("%f")
    return f"{stem}_{suffix}{ext}"


def _camera_capture_worker(cmd_q: "mp.Queue", frame_q: "mp.Queue", status_q: "mp.Queue"):
    try:
        apply_windows_process_role("camera_capture", active_recording=False)
    except Exception:
        pass
    camera = None
    camera_id = 0
    is_open = False
    is_recording = False
    video_writer = None
    recording_params = None
    current_recording_path = None
    stop_event = threading.Event()
    frame_lock = threading.Lock()
    shared = {"frame": None, "last_ok": 0.0}
    capture_failure_reason = None

    def capture_loop():
        nonlocal capture_failure_reason
        consecutive_failures = 0
        last_frame_ok_report = 0.0
        while not stop_event.is_set():
            if camera is None or (hasattr(camera, "isOpened") and not camera.isOpened()):
                time.sleep(0.01)
                continue
            try:
                ret, frm = camera.read()
                if ret and frm is not None and getattr(frm, "size", 0) > 0:
                    now_perf = time.perf_counter()
                    with frame_lock:
                        shared["frame"] = frm
                        shared["last_ok"] = now_perf
                    if (now_perf - last_frame_ok_report) >= 1.0:
                        try:
                            status_q.put(("frame_ok", now_perf))
                        except Exception:
                            pass
                        last_frame_ok_report = now_perf
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    last_ok = float(shared.get("last_ok", 0.0) or 0.0)
                    if consecutive_failures >= 40 and last_ok > 0 and (time.perf_counter() - last_ok) > 2.5:
                        capture_failure_reason = f"camera {camera_id} frame capture stalled"
                        try:
                            status_q.put(("capture_failed", False, capture_failure_reason))
                        except Exception:
                            pass
                        stop_event.set()
                        break
                    time.sleep(0.01)
            except Exception as e:
                capture_failure_reason = str(e)
                try:
                    status_q.put(("capture_failed", False, capture_failure_reason))
                except Exception:
                    pass
                stop_event.set()
                break

    capture_thread = None
    pending_record_start = None
    next_write_ts = None
    last_preview_ts = 0.0
    preview_enabled = True
    preview_background_rate = 1.0
    preview_active_rate = 8.0
    preview_recording_rate = 5.0
    preview_max_width = 640
    preview_jpeg_quality = 72
    guard_enabled = False
    guard_check_interval_s = 10.0
    guard_detector = MousePlatformDetector()
    last_guard_check_ts = 0.0
    last_guard_detection: Dict[str, object] = {}
    last_gc_monotonic = time.perf_counter()

    def try_put_latest_frame(payload: bytes):
        try:
            while True:
                frame_q.get_nowait()
        except Exception:
            pass
        try:
            frame_q.put_nowait(payload)
        except Exception:
            pass

    def overlay_timestamp(bgr_frame: np.ndarray, ts: float):
        try:
            dt = datetime.datetime.fromtimestamp(ts)
            text = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            cv2.putText(
                bgr_frame,
                text,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 255, 255),
                2,
                cv2.LINE_AA,
            )
        except Exception:
            print("error: put text on frame")
            pass

    def configure_guard_detector(config: Dict[str, object]):
        try:
            guard_detector.configure(
                roi_norm=config.get("roi_norm"),
                dark_threshold=config.get("dark_threshold"),
                day_dark_threshold=config.get("day_dark_threshold"),
                night_dark_threshold=config.get("night_dark_threshold"),
                scene_brightness_threshold=config.get("scene_brightness_threshold"),
                min_area_ratio=config.get("min_area_ratio"),
            )
        except Exception:
            pass

    def annotate_guard_preview(bgr_frame: np.ndarray, detection: Dict[str, object], original_shape) -> np.ndarray:
        if bgr_frame is None or not detection:
            return bgr_frame
        try:
            out = bgr_frame
            roi = detection.get("roi", (0, 0, 0, 0))
            x, y, w, h = [int(v) for v in roi]
            if original_shape is not None:
                original_h, original_w = int(original_shape[0]), int(original_shape[1])
                preview_h, preview_w = int(out.shape[0]), int(out.shape[1])
                if original_w > 0 and original_h > 0:
                    sx = float(preview_w) / float(original_w)
                    sy = float(preview_h) / float(original_h)
                    x = int(round(x * sx))
                    y = int(round(y * sy))
                    w = int(round(w * sx))
                    h = int(round(h * sy))
            present = bool(detection.get("mouse_present", False))
            color = (0, 220, 0) if present else (0, 180, 255)
            if w > 0 and h > 0:
                cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
            ratio = float(detection.get("area_ratio", 0.0) or 0.0) * 100.0
            lighting = str(detection.get("lighting_state", "unknown") or "unknown")
            active_threshold = int(detection.get("active_dark_threshold", 0) or 0)
            text = f"Mouse {'YES' if present else 'NO'} {ratio:.1f}% {lighting} th{active_threshold}"
            cv2.putText(
                out,
                text,
                (max(10, x), max(30, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                color,
                2,
                cv2.LINE_AA,
            )
        except Exception:
            return bgr_frame
        return out

    def open_writer(save_path: str, fourcc_str: str, fps: float, scale: float, frame_shape):
        nonlocal video_writer, recording_params, current_recording_path
        save_path = _unique_video_output_path(save_path)
        h0, w0 = int(frame_shape[0]), int(frame_shape[1])
        scale = float(scale) if scale else 1.0
        w = int(w0 * scale)
        h = int(h0 * scale)
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1
        try:
            fourcc = cv2.VideoWriter_fourcc(*fourcc_str)
            vw = cv2.VideoWriter(save_path, fourcc, float(fps), (w, h))
            if not vw.isOpened():
                vw.release()
                raise RuntimeError("VideoWriter open failed")
            video_writer = vw
            recording_params = {"w": w, "h": h, "scale": scale, "fps": float(fps)}
            current_recording_path = save_path
            return True, save_path
        except Exception:
            try:
                fallback_path = _unique_video_output_path(os.path.splitext(save_path)[0] + ".avi")
                fourcc = cv2.VideoWriter_fourcc(*"XVID")
                vw = cv2.VideoWriter(fallback_path, fourcc, float(fps), (w, h))
                if not vw.isOpened():
                    vw.release()
                    return False, save_path
                video_writer = vw
                recording_params = {"w": w, "h": h, "scale": scale, "fps": float(fps)}
                current_recording_path = fallback_path
                return True, fallback_path
            except Exception:
                return False, save_path

    try:
        while True:
            if (time.perf_counter() - last_gc_monotonic) >= 30.0:
                gc.collect()
                last_gc_monotonic = time.perf_counter()

            try:
                cmd = cmd_q.get(timeout=0.02)
            except queue.Empty:
                cmd = None

            if cmd:
                cmd_type = cmd.get("type")
                if cmd_type == "open":
                    camera_id = int(cmd.get("camera_id", 0))
                    preview_max_width = max(160, int(cmd.get("preview_max_width", preview_max_width) or preview_max_width))
                    preview_jpeg_quality = max(40, min(90, int(cmd.get("preview_jpeg_quality", preview_jpeg_quality) or preview_jpeg_quality)))
                    preview_background_rate = max(0.2, float(cmd.get("preview_background_rate", preview_background_rate) or preview_background_rate))
                    preview_active_rate = max(0.2, float(cmd.get("preview_active_rate", preview_active_rate) or preview_active_rate))
                    preview_recording_rate = max(0.2, float(cmd.get("preview_recording_rate", preview_recording_rate) or preview_recording_rate))
                    try:
                        backends = _preferred_camera_backends()
                        opened = False
                        for backend in backends:
                            cap = cv2.VideoCapture(camera_id, backend)
                            if cap.isOpened():
                                try:
                                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                                except Exception:
                                    pass
                                try:
                                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                                except Exception:
                                    pass
                                ret, _ = cap.read()
                                if ret:
                                    camera = cap
                                    opened = True
                                    break
                                cap.release()
                            else:
                                cap.release()
                        if not opened:
                            cap = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
                            if cap.isOpened():
                                try:
                                    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                                except Exception:
                                    pass
                                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)
                                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)
                                ret, _ = cap.read()
                                if ret:
                                    camera = cap
                                    opened = True
                                else:
                                    cap.release()
                            else:
                                cap.release()

                        if opened:
                            is_open = True
                            capture_failure_reason = None
                            stop_event.clear()
                            capture_thread = threading.Thread(target=capture_loop, daemon=True)
                            capture_thread.start()
                            status_q.put(("opened", True, f"camera {camera_id} opened"))
                        else:
                            status_q.put(("opened", False, f"camera {camera_id} open failed"))
                    except Exception as e:
                        status_q.put(("opened", False, str(e)))

                elif cmd_type == "close":
                    break

                elif cmd_type == "start_recording":
                    if not is_open:
                        status_q.put(("recording_started", False, "camera not open", None))
                    else:
                        pending_record_start = cmd

                elif cmd_type == "set_preview_enabled":
                    preview_enabled = bool(cmd.get("enabled", True))
                elif cmd_type == "set_preview_stream":
                    preview_max_width = max(160, int(cmd.get("preview_max_width", preview_max_width) or preview_max_width))
                    preview_jpeg_quality = max(40, min(90, int(cmd.get("preview_jpeg_quality", preview_jpeg_quality) or preview_jpeg_quality)))
                    preview_background_rate = max(
                        0.2,
                        float(cmd.get("preview_background_rate", preview_background_rate) or preview_background_rate),
                    )
                    preview_active_rate = max(
                        0.2,
                        float(cmd.get("preview_active_rate", preview_active_rate) or preview_active_rate),
                    )
                    preview_recording_rate = max(
                        0.2,
                        float(cmd.get("preview_recording_rate", preview_recording_rate) or preview_recording_rate),
                    )
                elif cmd_type == "set_charging_guard_detection":
                    guard_enabled = bool(cmd.get("enabled", False))
                    guard_check_interval_s = max(1.0, float(cmd.get("interval_ms", 10000) or 10000) / 1000.0)
                    configure_guard_detector(dict(cmd.get("config", {}) or {}))
                    if not guard_enabled:
                        last_guard_detection = {}
                    last_guard_check_ts = 0.0

                elif cmd_type == "stop_recording":
                    if is_recording:
                        is_recording = False
                        try:
                            apply_windows_process_role("camera_capture", active_recording=False)
                        except Exception:
                            pass
                        next_write_ts = None
                        if video_writer is not None:
                            try:
                                video_writer.release()
                            except Exception:
                                pass
                        video_writer = None
                        recording_params = None
                        status_q.put(("recording_stopped", True, current_recording_path))
                        current_recording_path = None
                    else:
                        status_q.put(("recording_stopped", True, None))

            if not is_open:
                continue

            now_perf = time.perf_counter()

            with frame_lock:
                latest = shared["frame"]
            if latest is None:
                continue

            if guard_enabled and now_perf - last_guard_check_ts >= guard_check_interval_s:
                try:
                    detection = guard_detector.detect(latest)
                    detection = dict(detection or {})
                    detection["timestamp_epoch"] = time.time()
                    last_guard_detection = detection
                    status_q.put(("charging_guard_detection", dict(last_guard_detection)))
                except Exception as exc:
                    last_guard_detection = {
                        "mouse_present": False,
                        "area_ratio": 0.0,
                        "roi": (0, 0, 0, 0),
                        "reason": f"detection failed: {exc}",
                        "timestamp_epoch": time.time(),
                    }
                    try:
                        status_q.put(("charging_guard_detection", dict(last_guard_detection)))
                    except Exception:
                        pass
                last_guard_check_ts = now_perf

            if pending_record_start and not is_recording:
                save_path = pending_record_start.get("save_path")
                fourcc_str = pending_record_start.get("fourcc", "mp4v")
                fps = float(pending_record_start.get("fps", 30.0))
                scale = float(pending_record_start.get("scale", 1.0))
                ok, actual_path = open_writer(save_path, fourcc_str, fps, scale, latest.shape)
                if ok:
                    is_recording = True
                    try:
                        apply_windows_process_role("camera_capture", active_recording=True)
                    except Exception:
                        pass
                    next_write_ts = now_perf
                    status_q.put(("recording_started", True, "ok", actual_path))
                else:
                    status_q.put(("recording_started", False, "writer open failed", save_path))
                pending_record_start = None

            preview_rate = preview_recording_rate if is_recording else preview_active_rate
            target_preview_rate = preview_rate if preview_enabled else preview_background_rate
            if target_preview_rate > 0 and now_perf - last_preview_ts >= (1.0 / target_preview_rate):
                original_shape = latest.shape
                preview = latest.copy()
                try:
                    height, width = preview.shape[:2]
                    if width > int(preview_max_width) > 0:
                        scale = float(preview_max_width) / float(width)
                        preview = cv2.resize(
                            preview,
                            (int(width * scale), int(height * scale)),
                            interpolation=cv2.INTER_AREA,
                        )
                except Exception:
                    pass
                if guard_enabled and last_guard_detection:
                    preview = annotate_guard_preview(preview, last_guard_detection, original_shape)
                overlay_timestamp(preview, time.time())
                jpeg_quality = max(
                    40,
                    min(
                        90,
                        int(preview_jpeg_quality)
                        - (4 if is_recording else 0)
                        - (8 if not preview_enabled else 0),
                    ),
                )
                ok, buf = cv2.imencode(".jpg", preview, [int(cv2.IMWRITE_JPEG_QUALITY), int(jpeg_quality)])
                if ok:
                    try_put_latest_frame(buf.tobytes())
                last_preview_ts = now_perf

            if is_recording and video_writer is not None and recording_params is not None:
                fps = float(recording_params.get("fps", 30.0))
                if fps <= 0:
                    fps = 30.0
                if next_write_ts is None:
                    next_write_ts = now_perf
                if now_perf >= next_write_ts:
                    out = latest
                    if recording_params.get("scale", 1.0) != 1.0:
                        out = cv2.resize(out, (int(recording_params["w"]), int(recording_params["h"])), interpolation=cv2.INTER_AREA)
                    else:
                        if out.shape[1] != int(recording_params["w"]) or out.shape[0] != int(recording_params["h"]):
                            out = cv2.resize(out, (int(recording_params["w"]), int(recording_params["h"])), interpolation=cv2.INTER_AREA)
                    out = out.copy()
                    overlay_timestamp(out, time.time())
                    try:
                        video_writer.write(out)
                    except Exception:
                        pass
                    next_write_ts += 1.0 / fps
                else:
                    time.sleep(min(0.002, max(0.0, next_write_ts - now_perf)))

    finally:
        try:
            apply_windows_process_role("camera_capture", active_recording=False)
        except Exception:
            pass
        stop_event.set()
        try:
            if capture_thread is not None and capture_thread.is_alive():
                capture_thread.join(timeout=1.0)
        except Exception:
            pass
        try:
            if video_writer is not None:
                video_writer.release()
        except Exception:
            pass
        try:
            if camera is not None:
                camera.release()
        except Exception:
            pass
        try:
            status_q.put(("closed", True, None))
        except Exception:
            pass

class CameraModule:
    def __init__(self):
        self.camera = None
        self.is_camera_open = False
        self.is_recording = False
        self.frame = None
        self.frame_processed = False
        self._process = None
        self._cmd_q = None
        self._frame_q = None
        self._status_q = None
        self._consumer_stop = threading.Event()
        self._consumer_thread = None
        self._mp_ctx = mp.get_context("spawn")
        self._status_backlog = []
        self.current_camera_id = None
        self.last_frame_monotonic = 0.0
        self.last_open_monotonic = 0.0
        self.last_status = "idle"
        self.last_error = ""
        self.current_recording_path = None
        self.recording_base_path = None
        self.preview_enabled = True
        self.preview_max_width = 640
        self.preview_jpeg_quality = 72
        self.preview_background_rate = 1.0
        self.preview_active_rate = 8.0
        self.preview_recording_rate = 5.0
        self.health_state = "ok"
        self.reopen_attempts = 0
        self.last_recovery_epoch = 0.0
        self.recovery_segment_count = 0
        self._last_recovery_attempt_monotonic = 0.0
        self._preview_jpeg = None
        self._charging_guard_detection = {}
        self._charging_guard_detection_enabled = False
        self._charging_guard_config = {}
        self._charging_guard_interval_ms = 10000
        
        # 视频压缩配置
        self.compression_config = {
            'codec': 'h264',  # 默认使用H.264编码器
            'quality': 'medium',  # 质量等级: low, medium, high, lossless
            'crf': 23,  # 恒定质量因子 (0-51, 越小质量越高)
            'preset': 'medium',  # 编码预设: ultrafast, superfast, veryfast, faster, fast, medium, slow, slower, veryslow
            'format': 'mp4',  # 输出格式
            'resolution_scale': 1.0,  # 分辨率缩放因子
            'fps_limit': None  # 帧率限制，None表示使用摄像头原始帧率
        }
        
        # 支持的编码器配置
        self.codec_configs = {
            'h264': {
                'fourcc': 'mp4v',  # 使用mp4v作为fourcc，更兼容
                'extension': '.mp4',
                'quality_settings': {
                    'lossless': {'crf': 0},
                    'high': {'crf': 18},
                    'medium': {'crf': 23},
                    'low': {'crf': 28}
                }
            },
            'h265': {
                'fourcc': 'HEVC',
                'extension': '.mp4',
                'quality_settings': {
                    'lossless': {'crf': 0},
                    'high': {'crf': 20},
                    'medium': {'crf': 25},
                    'low': {'crf': 30}
                }
            },
            'xvid': {
                'fourcc': 'XVID',
                'extension': '.avi',
                'quality_settings': {
                    'high': {'bitrate': 5000},
                    'medium': {'bitrate': 3000},
                    'low': {'bitrate': 1500}
                }
            }
        }
        self.lock = threading.Lock()
        self.video_post_compress_script = self._resolve_post_compress_script_path()

    def _close_queue(self, q):
        if q is None:
            return
        try:
            q.cancel_join_thread()
        except Exception:
            pass
        try:
            q.close()
        except Exception:
            pass

    def _process_status_event(self, event):
        if not event:
            return None
        evt = event[0]
        self.last_status = str(evt)
        if evt == "frame_ok":
            self.last_frame_monotonic = time.monotonic()
            return None
        if evt == "charging_guard_detection":
            self._charging_guard_detection = dict(event[1] or {}) if len(event) > 1 else {}
            return None
        if evt == "capture_failed":
            self.last_error = str(event[2]) if len(event) > 2 else "capture failed"
        elif evt == "closed":
            self.last_error = ""
        elif evt == "opened" and len(event) > 2:
            self.last_error = ""
        return event

    def _get_next_status_event(self, timeout=None):
        if self._status_backlog:
            return self._process_status_event(self._status_backlog.pop(0))
        if self._status_q is None:
            return None
        try:
            if timeout is None:
                event = self._status_q.get_nowait()
            else:
                event = self._status_q.get(timeout=timeout)
        except Exception:
            return None
        return self._process_status_event(event)

    def _wait_for_status(self, expected_events, timeout):
        deadline = time.monotonic() + float(timeout)
        expected = set(expected_events)
        while time.monotonic() < deadline:
            remaining = max(0.0, deadline - time.monotonic())
            event = self._get_next_status_event(timeout=min(0.2, remaining))
            if event is None:
                continue
            if event[0] in expected:
                return event
            self._status_backlog.append(event)
        return None

    def _drain_status_queue(self):
        while True:
            if self._status_q is None:
                break
            try:
                event = self._status_q.get_nowait()
            except Exception:
                break
            processed = self._process_status_event(event)
            if processed is not None:
                self._status_backlog.append(processed)

    def check_health(self, max_stale_seconds: float = 4.0):
        self._drain_status_queue()
        if not self.is_camera_open:
            return True, "camera closed"
        if self._process is None:
            return False, "camera process is missing"
        try:
            if not self._process.is_alive():
                return False, "camera process exited unexpectedly"
        except Exception:
            return False, "camera process handle is invalid"
        if self._consumer_thread is None or not self._consumer_thread.is_alive():
            return False, "camera frame consumer stopped"
        for event in self._status_backlog:
            if event and event[0] == "capture_failed":
                return False, str(event[2]) if len(event) > 2 else "camera capture failed"
        now = time.monotonic()
        if self.last_open_monotonic > 0 and self.last_frame_monotonic <= 0:
            if (now - self.last_open_monotonic) > float(max_stale_seconds):
                return False, "camera opened but no frames were received"
        if self.last_frame_monotonic > 0 and (now - self.last_frame_monotonic) > float(max_stale_seconds):
            return False, f"camera frame stream stalled for {now - self.last_frame_monotonic:.1f}s"
        return True, ""

    def get_last_frame_age_seconds(self) -> float:
        if self.last_frame_monotonic <= 0:
            return 0.0
        return max(0.0, time.monotonic() - self.last_frame_monotonic)
    
    def set_compression_config(self, **kwargs):
        """Set video compression config
        
        Args:
            codec (str): Codec type ('h264', 'h265', 'xvid')
            quality (str): Quality level ('low', 'medium', 'high', 'lossless')
            resolution_scale (float): Resolution scale factor (0.1-1.0)
            fps_limit (int): FPS limit
            crf (int): Constant Rate Factor (H.264/H.265 only)
            preset (str): Encoder preset (H.264/H.265 only)
        """
        for key, value in kwargs.items():
            if key in self.compression_config:
                self.compression_config[key] = value
                print(f"Compression config updated: {key} = {value}")
            else:
                print(f"Unknown compression config key: {key}")
    
    def get_compression_info(self) -> Dict:
        """Get current compression config info"""
        codec = self.compression_config['codec']
        quality = self.compression_config['quality']
        
        info = {
            'codec': codec,
            'quality': quality,
            'format': self.codec_configs[codec]['extension'],
            'resolution_scale': self.compression_config['resolution_scale'],
            'fps_limit': self.compression_config['fps_limit']
        }
        
        if codec in ['h264', 'h265']:
            info['crf'] = self.compression_config['crf']
            info['preset'] = self.compression_config['preset']
        
        return info
    
    def _get_optimal_codec_settings(self) -> Tuple[str, Dict]:
        """Get optimal encoder settings for current config"""
        codec = self.compression_config['codec']
        quality = self.compression_config['quality']
        
        if codec not in self.codec_configs:
            print(f"Unsupported codec: {codec}; falling back to XVID")
            codec = 'xvid'
        
        codec_config = self.codec_configs[codec]
        fourcc_str = codec_config['fourcc']
        
        # 获取质量设置
        quality_settings = codec_config['quality_settings'].get(quality, 
                                                               codec_config['quality_settings']['medium'])
        
        return fourcc_str, quality_settings
        
    def open_camera(self, camera_id=0):
        """Open camera"""
        camera_id = int(camera_id)
        if self.is_camera_open:
            healthy, _ = self.check_health()
            if healthy and self.current_camera_id == camera_id:
                return True
            self.close_camera()
        try:
            self._cmd_q = self._mp_ctx.Queue()
            self._frame_q = self._mp_ctx.Queue(maxsize=2)
            self._status_q = self._mp_ctx.Queue()
            self._status_backlog = []
            self.current_camera_id = camera_id
            self.last_frame_monotonic = 0.0
            self.last_open_monotonic = time.monotonic()
            self.last_error = ""
            self._process = self._mp_ctx.Process(
                target=_camera_capture_worker,
                args=(self._cmd_q, self._frame_q, self._status_q),
                daemon=True,
            )
            self._process.start()
            self._cmd_q.put({
                "type": "open",
                "camera_id": camera_id,
                "preview_max_width": self.preview_max_width,
                "preview_jpeg_quality": self.preview_jpeg_quality,
                "preview_background_rate": self.preview_background_rate,
                "preview_active_rate": self.preview_active_rate,
                "preview_recording_rate": self.preview_recording_rate,
            })
            ok = False
            msg = ""
            event = self._wait_for_status({"opened", "capture_failed", "closed"}, timeout=3.0)
            if event:
                evt = event[0]
                if evt == "opened":
                    ok = bool(event[1])
                    msg = event[2] if len(event) > 2 else ""
                elif evt == "capture_failed":
                    msg = event[2] if len(event) > 2 else "camera capture failed"
                elif evt == "closed":
                    msg = "camera worker closed unexpectedly"

            if not ok:
                self.last_error = msg
                self.close_camera()
                self.health_state = "failed"
                return False

            self.is_camera_open = True
            self.health_state = "ok"
            self._consumer_stop.clear()
            self._consumer_thread = threading.Thread(target=self._consume_frames, daemon=True)
            self._consumer_thread.start()
            self.configure_preview_stream(
                self.preview_max_width,
                self.preview_jpeg_quality,
                self.preview_background_rate,
                self.preview_active_rate,
                self.preview_recording_rate,
            )
            self.set_preview_enabled(self.preview_enabled)
            self.configure_charging_guard_detection(
                enabled=self._charging_guard_detection_enabled,
                config=self._charging_guard_config,
                interval_ms=self._charging_guard_interval_ms,
            )
            return True
        except Exception as e:
            self.last_error = str(e)
            self.close_camera()
            self.health_state = "failed"
            return False
        
    def close_camera(self):
        """Close camera"""
        if self.is_recording:
            try:
                self.stop_recording()
            except Exception:
                pass

        self._consumer_stop.set()
        try:
            if self._consumer_thread is not None and self._consumer_thread.is_alive():
                self._consumer_thread.join(timeout=0.3)
        except Exception:
            pass
        self._consumer_thread = None

        if self._cmd_q is not None:
            try:
                self._cmd_q.put({"type": "close"})
            except Exception:
                pass

        cmd_q = self._cmd_q
        frame_q = self._frame_q
        status_q = self._status_q
        process = self._process

        self._close_queue(cmd_q)
        self._close_queue(frame_q)
        self._close_queue(status_q)

        if process is not None:
            try:
                process.join(timeout=0.35)
            except Exception:
                pass
            try:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.35)
            except Exception:
                pass
            try:
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=0.35)
            except Exception:
                pass
        self._process = None
        self._cmd_q = None
        self._frame_q = None
        self._status_q = None
        self._status_backlog = []
        self.is_camera_open = False
        self.current_camera_id = None
        self.last_open_monotonic = 0.0
        self.last_frame_monotonic = 0.0
        self.last_status = "closed"
        self.health_state = "ok"
        self._charging_guard_detection = {}
        with self.lock:
            self.frame = None
            self.frame_processed = False
            self._preview_jpeg = None
    
    def get_frame(self):
        """Get latest frame"""
        if not self.is_camera_open:
            return None
        with self.lock:
            cached_frame = self.frame
            cached_preview = self._preview_jpeg
        if cached_frame is not None:
            return cached_frame
        if not cached_preview:
            return None
        try:
            arr = np.frombuffer(cached_preview, dtype=np.uint8)
            if arr.size == 0:
                return None
            decoded = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if decoded is None:
                return None
            with self.lock:
                if self._preview_jpeg == cached_preview:
                    self.frame = decoded
                    self.frame_processed = False
            return decoded
        except Exception as e:
            self.last_error = str(e)
            return None

    def configure_preview_stream(
        self,
        max_width: int = 640,
        jpeg_quality: int = 72,
        preview_background_rate: float = 1.0,
        preview_active_rate: float = 8.0,
        preview_recording_rate: float = 5.0,
    ):
        self.preview_max_width = max(160, int(max_width or self.preview_max_width))
        self.preview_jpeg_quality = max(40, min(90, int(jpeg_quality or self.preview_jpeg_quality)))
        self.preview_background_rate = max(0.2, float(preview_background_rate or 1.0))
        self.preview_active_rate = max(0.2, float(preview_active_rate or 8.0))
        self.preview_recording_rate = max(0.2, float(preview_recording_rate or 5.0))
        if self._cmd_q is not None:
            try:
                self._cmd_q.put({
                    "type": "set_preview_stream",
                    "preview_max_width": self.preview_max_width,
                    "preview_jpeg_quality": self.preview_jpeg_quality,
                    "preview_background_rate": self.preview_background_rate,
                    "preview_active_rate": self.preview_active_rate,
                    "preview_recording_rate": self.preview_recording_rate,
                })
            except Exception:
                pass

    def get_preview_jpeg_bytes(self, *_args, **_kwargs) -> Optional[bytes]:
        with self.lock:
            if not self._preview_jpeg:
                return None
            return bytes(self._preview_jpeg)

    def configure_charging_guard_detection(
        self,
        enabled: bool = False,
        config: Optional[Dict[str, object]] = None,
        interval_ms: int = 10000,
    ):
        self._charging_guard_detection_enabled = bool(enabled)
        self._charging_guard_config = dict(config or {})
        self._charging_guard_interval_ms = max(1000, int(interval_ms or 10000))
        if not self._charging_guard_detection_enabled:
            self._charging_guard_detection = {}
        if self._cmd_q is not None:
            try:
                self._cmd_q.put({
                    "type": "set_charging_guard_detection",
                    "enabled": self._charging_guard_detection_enabled,
                    "config": dict(self._charging_guard_config),
                    "interval_ms": int(self._charging_guard_interval_ms),
                })
            except Exception:
                pass

    def get_charging_guard_detection(self, max_age_seconds: float = 30.0) -> Optional[Dict[str, object]]:
        self._drain_status_queue()
        detection = dict(self._charging_guard_detection or {})
        if not detection:
            return None
        timestamp_epoch = float(detection.get("timestamp_epoch", 0.0) or 0.0)
        if timestamp_epoch > 0 and max_age_seconds > 0:
            if time.time() - timestamp_epoch > float(max_age_seconds):
                return None
        return detection

    def set_preview_enabled(self, enabled: bool):
        requested = bool(enabled)
        if requested and not self.is_camera_open:
            self.preview_enabled = False
            self.last_error = "Camera preview cannot be enabled while camera is closed"
            return False
        self.preview_enabled = requested
        if self._cmd_q is not None:
            try:
                self._cmd_q.put({"type": "set_preview_enabled", "enabled": self.preview_enabled})
            except Exception:
                return False
        if not self.preview_enabled:
            with self.lock:
                self.frame = None
                self.frame_processed = False
        return True

    def _consume_frames(self):
        last_gc_monotonic = time.perf_counter()
        while not self._consumer_stop.is_set():
            if (time.perf_counter() - last_gc_monotonic) >= 60.0:
                gc.collect()
                last_gc_monotonic = time.perf_counter()

            self._drain_status_queue()
            if self._frame_q is None:
                time.sleep(0.05)
                continue
            try:
                payload = self._frame_q.get(timeout=0.2)
            except queue.Empty:
                continue
            except (EOFError, OSError, ValueError) as e:
                self.last_error = str(e)
                break
            try:
                with self.lock:
                    self._preview_jpeg = bytes(payload)
                    self.frame = None
                    self.frame_processed = False
                self.last_frame_monotonic = time.monotonic()
            except Exception as e:
                self.last_error = str(e)
                continue
    
    def start_recording(self, save_path=None, exact_path: bool = False):
        """Start recording"""
        if not self.is_camera_open or self.is_recording:
            return False
        if self._cmd_q is None or self._status_q is None:
            return False
        healthy, _ = self.check_health()
        if not healthy:
            return False

        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fourcc_str, _ = self._get_optimal_codec_settings()
        codec = self.compression_config['codec']
        extension = self.codec_configs[codec]['extension']

        if save_path is None:
            default_base_path = build_daily_file_path(
                os.path.join(get_recordings_directory(), "video"),
                datetime.datetime.now(),
            )
            save_path = f"{os.path.splitext(default_base_path)[0]}_{timestamp}{extension}"
        elif exact_path:
            save_path = str(save_path)
        else:
            base_path = build_daily_file_path(str(save_path), datetime.datetime.now())
            base_path = os.path.splitext(base_path)[0]
            save_path = f"{base_path}_{timestamp}{extension}"

        fps_limit = self.compression_config.get('fps_limit', None)
        fps = float(fps_limit) if fps_limit else 30.0
        if fps <= 0:
            fps = 30.0
        scale = float(self.compression_config.get('resolution_scale', 1.0) or 1.0)

        try:
            self._cmd_q.put({
                "type": "start_recording",
                "save_path": save_path,
                "fourcc": fourcc_str,
                "fps": fps,
                "scale": scale,
            })
            event = self._wait_for_status({"recording_started", "capture_failed", "closed"}, timeout=5.0)
            if event and event[0] == "recording_started" and event[1]:
                self.is_recording = True
                self.current_recording_path = event[3]
                actual_path = str(event[3] or "")
                if actual_path and "_recovery_" not in os.path.basename(actual_path):
                    self.recording_base_path = actual_path
                return True
            if event and len(event) > 2:
                self.last_error = str(event[2])
        except Exception as e:
            self.last_error = str(e)
            pass
        return False
    
    def stop_recording(self):
        """Stop recording"""
        if not self.is_recording:
            return False
        if self._cmd_q is None or self._status_q is None:
            self.is_recording = False
            self.current_recording_path = None
            self.recording_base_path = None
            return True

        current_file_path = getattr(self, "current_recording_path", None)
        try:
            self._cmd_q.put({"type": "stop_recording"})
            event = self._wait_for_status({"recording_stopped", "closed"}, timeout=5.0)
            if event and event[0] == "recording_stopped" and len(event) > 2 and event[2]:
                current_file_path = event[2]
        except Exception:
            pass

        self.is_recording = False

        if current_file_path and os.path.exists(current_file_path):
            stats = self._get_quick_stats(current_file_path)
            if stats:
                pass
                # print(f"\nRecording finished: {os.path.basename(current_file_path)}")
                # print(f"File size: {stats['file_size_mb']:.2f} MB")
                # print(f"Duration: {stats['duration_seconds']:.1f} s")
                # print(f"Resolution: {stats['resolution']}")
                # print(f"FPS: {stats['fps']:.1f} fps")
                # print(f"Bitrate: {stats['bitrate_kbps']:.1f} kbps")
            self._start_post_compress_process(current_file_path)
        self.current_recording_path = None
        self.recording_base_path = None
        return True

    def recover_if_needed(self, reason: str = "") -> bool:
        if not self.is_camera_open:
            self.health_state = "ok"
            return True
        now_mono = time.monotonic()
        if self._last_recovery_attempt_monotonic > 0 and (now_mono - self._last_recovery_attempt_monotonic) < 2.0:
            return False
        self._last_recovery_attempt_monotonic = now_mono
        self.health_state = "recovering"
        self.reopen_attempts += 1
        self.last_error = str(reason or "camera recovery requested")

        previous_camera_id = int(self.current_camera_id if self.current_camera_id is not None else 0)
        previous_preview_enabled = bool(self.preview_enabled)
        was_recording = bool(self.is_recording)
        base_recording_path = str(self.recording_base_path or self.current_recording_path or "").strip()
        recovery_path = None
        if was_recording and base_recording_path:
            recovery_path = build_recovery_segment_path(
                base_recording_path,
                int(self.recovery_segment_count or 0) + 1,
            )

        self.set_preview_enabled(False)
        self.close_camera()

        if not self.open_camera(previous_camera_id):
            self.health_state = "failed"
            self.set_preview_enabled(previous_preview_enabled)
            return False

        if was_recording:
            restarted = self.start_recording(recovery_path or base_recording_path or None, exact_path=bool(recovery_path))
            if not restarted:
                self.health_state = "failed"
                self.set_preview_enabled(previous_preview_enabled)
                return False
            self.recovery_segment_count = int(self.recovery_segment_count or 0) + 1

        self.health_state = "ok"
        self.last_recovery_epoch = time.time()
        self.last_error = ""
        self.set_preview_enabled(previous_preview_enabled)
        return True

    def _resolve_post_compress_script_path(self):
        candidate_paths = []
        current_dir = os.path.dirname(os.path.abspath(__file__))
        package_root = os.path.dirname(current_dir)
        candidate_paths.append(os.path.join(package_root, "workers", "video_moviepy_compress_worker.py"))
        candidate_paths.append(os.path.join(current_dir, "video_moviepy_compress_worker.py"))
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            candidate_paths.append(os.path.join(sys._MEIPASS, "workers", "video_moviepy_compress_worker.py"))
            candidate_paths.append(os.path.join(sys._MEIPASS, "video_moviepy_compress_worker.py"))
        for path in candidate_paths:
            if os.path.isfile(path):
                return path
        return candidate_paths[0]

    def _compress_video_in_background(self, video_path: str):
        try:
            process = self._mp_ctx.Process(
                target=_video_compress_worker_entry,
                args=(video_path,),
                daemon=True,
            )
            process.start()
        except Exception:
            pass

    def _start_post_compress_process(self, video_path: str):
        if getattr(sys, "frozen", False):
            self._compress_video_in_background(video_path)
            return
        worker_script = getattr(self, "video_post_compress_script", None) or self._resolve_post_compress_script_path()
        if not os.path.isfile(worker_script):
            return
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
            )
        try:
            subprocess.Popen(
                [sys.executable, worker_script, video_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=os.path.dirname(worker_script),
                startupinfo=startupinfo,
                creationflags=creationflags,
            )
        except Exception:
            pass
    
    def _get_quick_stats(self, file_path: str) -> Dict:
        """Get basic video stats (internal use)"""
        if not os.path.exists(file_path):
            return {}
        
        try:
            file_size = os.path.getsize(file_path)
            
            # 使用OpenCV获取视频信息
            cap = cv2.VideoCapture(file_path)
            if not cap.isOpened():
                return {'file_size_mb': file_size / (1024 * 1024)}
            
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps = cap.get(cv2.CAP_PROP_FPS)
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            duration = frame_count / fps if fps > 0 else 0
            
            cap.release()
            
            return {
                'file_size_mb': file_size / (1024 * 1024),
                'duration_seconds': duration,
                'fps': fps,
                'resolution': f"{width}x{height}",
                'bitrate_kbps': (file_size * 8) / (duration * 1000) if duration > 0 else 0
            }
        except Exception as e:
            print(f"Error getting video stats: {e}")
            return {}
    
    def get_compression_stats(self, file_path: str = None) -> Dict:
        """Get detailed compression stats for a video file
        
        Args:
            file_path: Video file path; uses latest recording if None
        """
        if file_path is None:
            if hasattr(self, 'current_recording_path'):
                file_path = self.current_recording_path
            else:
                return {'error': 'No recording files available'}
        
        stats = self._get_quick_stats(file_path)
        if not stats:
            return {'error': 'Unable to get file stats'}
        
        # 添加详细的压缩信息
        try:
            file_size = os.path.getsize(file_path)
            frame_count = int(stats['fps'] * stats['duration_seconds']) if stats['duration_seconds'] > 0 else 0
            
            # 估算未压缩大小（假设24位RGB）
            resolution_parts = stats['resolution'].split('x')
            if len(resolution_parts) == 2:
                width, height = int(resolution_parts[0]), int(resolution_parts[1])
                uncompressed_size = frame_count * width * height * 3
                compression_ratio = uncompressed_size / file_size if file_size > 0 else 0
                stats['compression_ratio'] = compression_ratio
                stats['frame_count'] = frame_count
            
            # 添加编码器信息
            stats['codec'] = self.compression_config.get('codec', 'unknown')
            stats['quality'] = self.compression_config.get('quality', 'unknown')
            
        except Exception as e:
            stats['compression_error'] = str(e)
        
        return stats
