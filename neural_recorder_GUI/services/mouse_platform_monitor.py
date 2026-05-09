from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Tuple

import numpy as np

try:
    import cv2
except Exception:  # Keep tests/imports working in environments without OpenCV.
    cv2 = None


DEFAULT_ROI_NORM = (0.15, 0.15, 0.70, 0.75)


@dataclass
class MousePlatformDetectionConfig:
    roi_norm: Tuple[float, float, float, float] = DEFAULT_ROI_NORM
    dark_threshold: int = 90
    day_dark_threshold: int = 90
    night_dark_threshold: int = 90
    scene_brightness_threshold: int = 80
    min_area_ratio: float = 0.02


class MousePlatformDetector:
    """Detect a dark mouse-shaped foreground blob inside a fixed platform ROI."""

    def __init__(
        self,
        roi_norm: Sequence[float] = DEFAULT_ROI_NORM,
        dark_threshold: int = 90,
        day_dark_threshold: Optional[int] = None,
        night_dark_threshold: Optional[int] = None,
        scene_brightness_threshold: int = 80,
        min_area_ratio: float = 0.02,
    ):
        self.configure(
            roi_norm=roi_norm,
            dark_threshold=dark_threshold,
            day_dark_threshold=day_dark_threshold,
            night_dark_threshold=night_dark_threshold,
            scene_brightness_threshold=scene_brightness_threshold,
            min_area_ratio=min_area_ratio,
        )

    def configure(
        self,
        roi_norm: Optional[Sequence[float]] = None,
        dark_threshold: Optional[int] = None,
        day_dark_threshold: Optional[int] = None,
        night_dark_threshold: Optional[int] = None,
        scene_brightness_threshold: Optional[int] = None,
        min_area_ratio: Optional[float] = None,
    ):
        if roi_norm is not None:
            self.roi_norm = self._normalize_roi(roi_norm)
        if dark_threshold is not None:
            legacy_threshold = self._normalize_threshold(dark_threshold, 90)
            self.dark_threshold = legacy_threshold
            if day_dark_threshold is None:
                self.day_dark_threshold = legacy_threshold
            if night_dark_threshold is None:
                self.night_dark_threshold = legacy_threshold
        if day_dark_threshold is not None:
            self.day_dark_threshold = self._normalize_threshold(day_dark_threshold, getattr(self, "dark_threshold", 90))
            self.dark_threshold = self.day_dark_threshold
        if night_dark_threshold is not None:
            self.night_dark_threshold = self._normalize_threshold(night_dark_threshold, getattr(self, "dark_threshold", 90))
        if scene_brightness_threshold is not None:
            self.scene_brightness_threshold = self._normalize_threshold(scene_brightness_threshold, 80)
        if min_area_ratio is not None:
            self.min_area_ratio = float(max(0.0, min(1.0, float(min_area_ratio))))

    def detect(self, frame) -> Dict[str, object]:
        if frame is None:
            return self._result(False, 0.0, (0, 0, 0, 0), "no frame")

        arr = np.asarray(frame)
        if arr.ndim < 2 or arr.size == 0:
            return self._result(False, 0.0, (0, 0, 0, 0), "invalid frame")

        height, width = int(arr.shape[0]), int(arr.shape[1])
        roi = self._roi_pixels(width, height)
        x, y, w, h = roi
        if w <= 0 or h <= 0:
            return self._result(False, 0.0, roi, "empty roi")

        gray = self._to_gray(arr)
        lighting_state, scene_brightness, active_threshold = self._classify_lighting(gray)
        roi_gray = gray[y : y + h, x : x + w]
        if roi_gray.size == 0:
            return self._result(
                False,
                0.0,
                roi,
                "empty roi",
                lighting_state=lighting_state,
                scene_brightness=scene_brightness,
                active_dark_threshold=active_threshold,
            )

        mask = roi_gray <= active_threshold
        mask = self._clean_mask(mask)
        area_ratio = float(np.count_nonzero(mask)) / float(mask.size)
        present = area_ratio >= self.min_area_ratio
        reason = "mouse present" if present else "below area threshold"
        return self._result(
            present,
            area_ratio,
            roi,
            reason,
            lighting_state=lighting_state,
            scene_brightness=scene_brightness,
            active_dark_threshold=active_threshold,
        )

    def _to_gray(self, frame: np.ndarray) -> np.ndarray:
        if frame.ndim == 2:
            return frame
        if cv2 is not None:
            return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        b = frame[:, :, 0].astype(np.float32)
        g = frame[:, :, 1].astype(np.float32)
        r = frame[:, :, 2].astype(np.float32)
        return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.uint8)

    def _clean_mask(self, mask: np.ndarray) -> np.ndarray:
        if cv2 is None:
            return mask
        kernel = np.ones((5, 5), dtype=np.uint8)
        cleaned = mask.astype(np.uint8) * 255
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)
        return cleaned > 0

    def _roi_pixels(self, width: int, height: int) -> Tuple[int, int, int, int]:
        x_norm, y_norm, w_norm, h_norm = self.roi_norm
        x = int(round(x_norm * width))
        y = int(round(y_norm * height))
        w = int(round(w_norm * width))
        h = int(round(h_norm * height))
        x = max(0, min(width, x))
        y = max(0, min(height, y))
        w = max(0, min(width - x, w))
        h = max(0, min(height - y, h))
        return x, y, w, h

    def _normalize_roi(self, roi_norm: Sequence[float]) -> Tuple[float, float, float, float]:
        if len(roi_norm) != 4:
            return DEFAULT_ROI_NORM
        x, y, w, h = [float(v) for v in roi_norm]
        x = max(0.0, min(1.0, x))
        y = max(0.0, min(1.0, y))
        w = max(0.0, min(1.0 - x, w))
        h = max(0.0, min(1.0 - y, h))
        if w <= 0.0 or h <= 0.0:
            return DEFAULT_ROI_NORM
        return x, y, w, h

    def _normalize_threshold(self, value, fallback: int) -> int:
        try:
            threshold = int(value)
        except Exception:
            threshold = int(fallback)
        return int(max(0, min(255, threshold)))

    def _classify_lighting(self, gray: np.ndarray) -> Tuple[str, float, int]:
        if gray is None or gray.size == 0:
            return "unknown", 0.0, int(getattr(self, "dark_threshold", 90))
        scene_brightness = float(np.mean(gray))
        brightness_threshold = int(getattr(self, "scene_brightness_threshold", 80))
        lighting_state = "day" if scene_brightness >= brightness_threshold else "night"
        active_threshold = (
            int(getattr(self, "day_dark_threshold", getattr(self, "dark_threshold", 90)))
            if lighting_state == "day"
            else int(getattr(self, "night_dark_threshold", getattr(self, "dark_threshold", 90)))
        )
        return lighting_state, scene_brightness, active_threshold

    def _result(
        self,
        present: bool,
        area_ratio: float,
        roi,
        reason: str,
        lighting_state: str = "unknown",
        scene_brightness: float = 0.0,
        active_dark_threshold: Optional[int] = None,
    ) -> Dict[str, object]:
        return {
            "mouse_present": bool(present),
            "area_ratio": float(area_ratio),
            "roi": tuple(int(v) for v in roi),
            "reason": str(reason),
            "lighting_state": str(lighting_state or "unknown"),
            "scene_brightness": float(scene_brightness or 0.0),
            "active_dark_threshold": int(
                active_dark_threshold
                if active_dark_threshold is not None
                else getattr(self, "dark_threshold", 90)
            ),
        }


class ChargingGuardState:
    """Small state machine for consecutive positive checks and warning stop."""

    def __init__(self, required_hits: int = 6):
        self.required_hits = max(1, int(required_hits))
        self.hit_count = 0
        self.warning_active = False

    def configure(self, required_hits: int):
        self.required_hits = max(1, int(required_hits))
        self.hit_count = min(self.hit_count, self.required_hits)

    def update(self, condition_true: bool, reason: str = "") -> Dict[str, object]:
        if condition_true:
            self.hit_count += 1
            if self.hit_count >= self.required_hits:
                self.warning_active = True
                return self._decision("warning", "Warning sent")
            return self._decision(None, f"{self.hit_count}/{self.required_hits}")

        return self.reset(reason or "condition false")

    def reset(self, reason: str = "reset") -> Dict[str, object]:
        action = "stop" if self.warning_active else None
        self.hit_count = 0
        self.warning_active = False
        return self._decision(action, f"Reset: {reason}")

    def _decision(self, action, status: str) -> Dict[str, object]:
        return {
            "action": action,
            "status": status,
            "hit_count": self.hit_count,
            "required_hits": self.required_hits,
            "warning_active": self.warning_active,
        }
