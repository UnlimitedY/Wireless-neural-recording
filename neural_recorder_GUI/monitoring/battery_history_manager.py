import json
import math
import os
import time
from bisect import bisect_left
from datetime import datetime
from statistics import median
from typing import Callable, Dict, Iterable, List, Optional

try:
    from ..storage.runtime_cache import atomic_write_json
except ImportError:
    from storage.runtime_cache import atomic_write_json


def classify_battery_record(record: Dict) -> str:
    mode = str(record.get("mode", "") or "").strip()
    mode_normalized = mode.lower().replace(" ", "")
    rf_status = int(record.get("rf_status", 0) or 0)
    if mode_normalized in {"16channelslfp", "mode0", "mode0lfp+mand", "mode0lfp+mand+raw"} and rf_status == 2:
        return "mode0"
    if mode == "ESA&MUA" and rf_status == 1:
        return "mode3"
    return "ignored"


def _local_hour_start(timestamp_epoch: float) -> float:
    dt = datetime.fromtimestamp(float(timestamp_epoch))
    return dt.replace(minute=0, second=0, microsecond=0).timestamp()


def _local_day_key(timestamp_epoch: float):
    return datetime.fromtimestamp(float(timestamp_epoch)).date()


def _robust_scale(values: Iterable[float], floor_value: float) -> float:
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not values:
        return float(floor_value)
    center = median(values)
    deviations = [abs(v - center) for v in values]
    mad = median(deviations) if deviations else 0.0
    return max(1.4826 * mad, float(floor_value))


class BatteryHistoryManager:
    RETENTION_SECONDS = 30 * 24 * 3600
    SAVE_THROTTLE_SECONDS = 5.0
    MODE0_LOOKBACK_SECONDS = 7 * 24 * 3600
    MODE3_LOOKBACK_SECONDS = 14 * 24 * 3600
    MODE0_MIN_COVERAGE_SECONDS = 20 * 60
    MODE0_MIN_POINTS = 5
    MODE0_SCALE_FLOOR = 0.5
    MODE0_MIN_BASELINE_COUNT = 6
    MODE3_BIN_SECONDS = 10 * 60
    MODE3_SCALE_FLOOR = 0.3
    MODE3_MIN_BASELINE_COUNT = 8

    def __init__(self, storage_path_resolver: Optional[Callable[[], Optional[str]]] = None):
        self.storage_path_resolver = storage_path_resolver
        self.storage_path: Optional[str] = None
        self.records: List[Dict] = []
        self._dirty = False
        self._last_save_monotonic = 0.0
        self._sync_storage_path()

    @staticmethod
    def _coerce_voltage_mv(value) -> int:
        try:
            if isinstance(value, str):
                normalized = value.strip().lower()
                if normalized.endswith("mv"):
                    return int(round(float(normalized[:-2])))
                if normalized.endswith("v"):
                    return int(round(float(normalized[:-1]) * 1000.0))
                return int(round(float(normalized)))
            return int(round(float(value)))
        except Exception:
            return 0

    def _resolve_storage_path(self) -> Optional[str]:
        if self.storage_path_resolver is None:
            return None
        try:
            path = self.storage_path_resolver()
        except Exception:
            return None
        if not path:
            return None
        return os.path.abspath(path)

    def _sync_storage_path(self) -> bool:
        resolved_path = self._resolve_storage_path()
        if resolved_path == self.storage_path:
            return False
        previous_path = self.storage_path
        if previous_path and self._dirty:
            try:
                self.save_history(force=True)
            except Exception:
                pass
        self.storage_path = resolved_path
        self.records = self._load_records_from_path(self.storage_path)
        self._dirty = False
        return True

    def _load_records_from_path(self, path: Optional[str]) -> List[Dict]:
        if not path or not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        records: List[Dict] = []
        for item in payload:
            normalized = self._normalize_record(item)
            if normalized is not None:
                records.append(normalized)
        records.sort(key=lambda item: float(item["timestamp_epoch"]))
        return records

    def load_history(self) -> List[Dict]:
        self._sync_storage_path()
        return list(self.records)

    def save_history(self, force: bool = False) -> None:
        if not self.storage_path or not self._dirty:
            return
        now_mono = time.monotonic()
        if (
            not force
            and self._last_save_monotonic > 0.0
            and (now_mono - self._last_save_monotonic) < self.SAVE_THROTTLE_SECONDS
        ):
            return

        atomic_write_json(self.storage_path, self.records, durable=False)

        self._dirty = False
        self._last_save_monotonic = now_mono

    def flush(self) -> None:
        self.save_history(force=True)

    def _normalize_record(self, record: Dict) -> Optional[Dict]:
        if not isinstance(record, dict):
            return None
        try:
            timestamp_epoch = float(record.get("timestamp_epoch"))
            rsoc = float(record.get("rsoc"))
            battery_stat = int(record.get("battery_stat", 0))
            voltage_mv = self._coerce_voltage_mv(record.get("voltage_mv", 0))
            rf_status = int(record.get("rf_status", 0))
            capacity_mAh = float(record.get("capacity_mAh", record.get("battery_capacity_mAh", 24.0)) or 24.0)
        except Exception:
            return None
        if not math.isfinite(timestamp_epoch) or not math.isfinite(rsoc):
            return None
        if not math.isfinite(capacity_mAh) or capacity_mAh <= 0.0:
            capacity_mAh = 24.0
        rsoc = max(0.0, min(100.0, rsoc))
        timestamp_iso = record.get("timestamp_iso")
        if not timestamp_iso:
            timestamp_iso = datetime.fromtimestamp(timestamp_epoch).isoformat()
        return {
            "timestamp_epoch": float(timestamp_epoch),
            "timestamp_iso": str(timestamp_iso),
            "rsoc": float(rsoc),
            "battery_stat": battery_stat,
            "voltage_mv": voltage_mv,
            "capacity_mAh": float(capacity_mAh),
            "mode": str(record.get("mode", "") or ""),
            "rf_status": rf_status,
            "trial_paused": bool(record.get("trial_paused", False)),
        }

    def _prune_records(self, now_ts: Optional[float] = None) -> None:
        if not self.records:
            return
        reference_ts = float(now_ts) if now_ts is not None else time.time()
        cutoff_ts = reference_ts - self.RETENTION_SECONDS
        self.records = [
            record for record in self.records if float(record["timestamp_epoch"]) >= cutoff_ts
        ]

    def record_sample(self, sample: Dict) -> bool:
        self._sync_storage_path()
        normalized = self._normalize_record(sample)
        if normalized is None:
            return False
        self._prune_records(now_ts=normalized["timestamp_epoch"])
        if self.records:
            last_record = self.records[-1]
            same_state = (
                float(last_record["rsoc"]) == float(normalized["rsoc"])
                and int(last_record["battery_stat"]) == int(normalized["battery_stat"])
                and str(last_record["mode"]) == str(normalized["mode"])
                and int(last_record["rf_status"]) == int(normalized["rf_status"])
                and bool(last_record["trial_paused"]) == bool(normalized["trial_paused"])
            )
            delta_t = float(normalized["timestamp_epoch"]) - float(last_record["timestamp_epoch"])
            if same_state and delta_t < 60.0:
                return False
        if not self.records or float(normalized["timestamp_epoch"]) >= float(self.records[-1]["timestamp_epoch"]):
            self.records.append(normalized)
        else:
            timestamps = [float(item["timestamp_epoch"]) for item in self.records]
            insert_at = bisect_left(timestamps, float(normalized["timestamp_epoch"]))
            self.records.insert(insert_at, normalized)
        self._prune_records(now_ts=normalized["timestamp_epoch"])
        self._dirty = True
        self.save_history()
        return True

    def get_recent_records(self, hours: int = 24, now_ts: Optional[float] = None) -> List[Dict]:
        self._sync_storage_path()
        reference_ts = float(now_ts) if now_ts is not None else time.time()
        cutoff_ts = reference_ts - float(hours) * 3600.0
        return [
            dict(record)
            for record in self.records
            if float(record["timestamp_epoch"]) >= cutoff_ts
        ]

    def _get_mode0_hourly_buckets(self) -> List[Dict]:
        self._sync_storage_path()
        buckets: Dict[float, List[Dict]] = {}
        for record in self.records:
            if classify_battery_record(record) != "mode0":
                continue
            hour_start = _local_hour_start(record["timestamp_epoch"])
            buckets.setdefault(hour_start, []).append(record)
        results: List[Dict] = []
        for hour_start, bucket_records in sorted(buckets.items()):
            bucket_records = sorted(bucket_records, key=lambda item: float(item["timestamp_epoch"]))
            if len(bucket_records) < self.MODE0_MIN_POINTS:
                continue
            coverage_seconds = float(bucket_records[-1]["timestamp_epoch"]) - float(bucket_records[0]["timestamp_epoch"])
            if coverage_seconds < self.MODE0_MIN_COVERAGE_SECONDS:
                continue
            start_rsoc = float(bucket_records[0]["rsoc"])
            end_rsoc = float(bucket_records[-1]["rsoc"])
            delta_rsoc_per_hour = (end_rsoc - start_rsoc) * 3600.0 / coverage_seconds
            results.append({
                "class_label": "mode0",
                "hour_start": float(hour_start),
                "hour_mid": float(hour_start) + 1800.0,
                "real_start": float(bucket_records[0]["timestamp_epoch"]),
                "real_end": float(bucket_records[-1]["timestamp_epoch"]),
                "coverage_seconds": float(coverage_seconds),
                "sample_count": len(bucket_records),
                "start_rsoc": start_rsoc,
                "end_rsoc": end_rsoc,
                "delta_rsoc_per_hour": float(delta_rsoc_per_hour),
                "point_time": float(hour_start) + 1800.0,
                "point_y": (start_rsoc + end_rsoc) / 2.0,
            })
        return results

    def compute_mode0_hourly_anomalies(self) -> List[Dict]:
        self._sync_storage_path()
        buckets = self._get_mode0_hourly_buckets()
        anomalies: List[Dict] = []
        for index, bucket in enumerate(buckets):
            current_start = float(bucket["hour_start"])
            lookback = []
            for previous in buckets[:index]:
                delta_t = current_start - float(previous["hour_start"])
                if 0.0 < delta_t <= self.MODE0_LOOKBACK_SECONDS:
                    lookback.append(float(previous["delta_rsoc_per_hour"]))
            if len(lookback) < self.MODE0_MIN_BASELINE_COUNT:
                continue
            baseline_median = median(lookback)
            baseline_scale = _robust_scale(lookback, self.MODE0_SCALE_FLOOR)
            observed_delta = float(bucket["delta_rsoc_per_hour"])
            if abs(observed_delta - baseline_median) < (3.0 * baseline_scale):
                continue
            anomaly = dict(bucket)
            anomaly.update({
                "baseline_median": float(baseline_median),
                "baseline_scale": float(baseline_scale),
                "baseline_count": len(lookback),
                "metric_name": "delta_rsoc_per_hour",
            })
            anomalies.append(anomaly)
        return anomalies

    def _get_mode3_stitched_bins(self) -> List[Dict]:
        self._sync_storage_path()
        sorted_records = sorted(self.records, key=lambda item: float(item["timestamp_epoch"]))
        segments: List[Dict] = []
        current_segment: List[Dict] = []
        current_day = None

        def finalize_segment():
            if len(current_segment) >= 2:
                segments.append({
                    "day": current_day,
                    "records": list(current_segment),
                })

        for record in sorted_records:
            record_class = classify_battery_record(record)
            record_day = _local_day_key(record["timestamp_epoch"])
            if record_class == "mode3":
                if current_segment and record_day == current_day:
                    current_segment.append(record)
                else:
                    finalize_segment()
                    current_segment = [record]
                    current_day = record_day
            else:
                finalize_segment()
                current_segment = []
                current_day = None
        finalize_segment()

        segments_by_day: Dict = {}
        for segment_index, segment in enumerate(segments):
            segments_by_day.setdefault(segment["day"], []).append((segment_index, segment["records"]))

        bins: List[Dict] = []
        for _, day_segments in sorted(segments_by_day.items(), key=lambda item: item[0]):
            virtual_consumed = 0.0
            current_bin = None
            for segment_id, segment_records in day_segments:
                for record_index in range(len(segment_records) - 1):
                    start_record = segment_records[record_index]
                    end_record = segment_records[record_index + 1]
                    start_ts = float(start_record["timestamp_epoch"])
                    end_ts = float(end_record["timestamp_epoch"])
                    duration = end_ts - start_ts
                    if duration <= 0:
                        continue
                    start_rsoc = float(start_record["rsoc"])
                    end_rsoc = float(end_record["rsoc"])
                    current_piece_start_ts = start_ts
                    current_piece_start_rsoc = start_rsoc
                    current_piece_duration = duration
                    current_piece_end_ts = end_ts
                    current_piece_end_rsoc = end_rsoc

                    while current_piece_duration > 0:
                        if current_bin is None:
                            current_bin = {
                                "class_label": "mode3",
                                "bin_start_virtual_min": virtual_consumed / 60.0,
                                "start_rsoc": current_piece_start_rsoc,
                                "real_start": current_piece_start_ts,
                                "segment_ids": set(),
                                "record_keys": set(),
                                "duration_seconds": 0.0,
                            }
                        remaining = self.MODE3_BIN_SECONDS - float(current_bin["duration_seconds"])
                        consume = min(remaining, current_piece_duration)
                        ratio = consume / current_piece_duration if current_piece_duration > 0 else 0.0
                        consume_end_ts = current_piece_start_ts + consume
                        consume_end_rsoc = current_piece_start_rsoc + (
                            (current_piece_end_rsoc - current_piece_start_rsoc) * ratio
                        )
                        current_bin["segment_ids"].add(segment_id)
                        current_bin["record_keys"].add((segment_id, record_index))
                        current_bin["record_keys"].add((segment_id, record_index + 1))
                        current_bin["duration_seconds"] = float(current_bin["duration_seconds"]) + consume
                        current_bin["real_end"] = consume_end_ts
                        current_bin["end_rsoc"] = consume_end_rsoc

                        current_piece_start_ts = consume_end_ts
                        current_piece_start_rsoc = consume_end_rsoc
                        current_piece_duration -= consume

                        if float(current_bin["duration_seconds"]) + 1e-9 >= self.MODE3_BIN_SECONDS:
                            bins.append({
                                "class_label": "mode3",
                                "bin_start_virtual_min": float(current_bin["bin_start_virtual_min"]),
                                "bin_end_virtual_min": (virtual_consumed + self.MODE3_BIN_SECONDS) / 60.0,
                                "bin_real_start": float(current_bin["real_start"]),
                                "bin_real_end": float(current_bin["real_end"]),
                                "real_start": float(current_bin["real_start"]),
                                "real_end": float(current_bin["real_end"]),
                                "segment_count": len(current_bin["segment_ids"]),
                                "sample_count": len(current_bin["record_keys"]),
                                "start_rsoc": float(current_bin["start_rsoc"]),
                                "end_rsoc": float(current_bin["end_rsoc"]),
                                "delta_rsoc_per_10min": float(current_bin["end_rsoc"] - current_bin["start_rsoc"]),
                                "point_time": float(current_bin["real_end"]),
                                "point_y": float(current_bin["end_rsoc"] + current_bin["start_rsoc"]) / 2.0,
                            })
                            virtual_consumed += self.MODE3_BIN_SECONDS
                            current_bin = None
        return bins

    def compute_mode3_stitched_anomalies(self) -> List[Dict]:
        self._sync_storage_path()
        bins = self._get_mode3_stitched_bins()
        anomalies: List[Dict] = []
        for index, bucket in enumerate(bins):
            current_end = float(bucket["bin_real_end"])
            lookback = []
            for previous in bins[:index]:
                delta_t = current_end - float(previous["bin_real_end"])
                if 0.0 < delta_t <= self.MODE3_LOOKBACK_SECONDS:
                    lookback.append(float(previous["delta_rsoc_per_10min"]))
            if len(lookback) < self.MODE3_MIN_BASELINE_COUNT:
                continue
            baseline_median = median(lookback)
            baseline_scale = _robust_scale(lookback, self.MODE3_SCALE_FLOOR)
            observed_delta = float(bucket["delta_rsoc_per_10min"])
            if abs(observed_delta - baseline_median) < (3.0 * baseline_scale):
                continue
            anomaly = dict(bucket)
            anomaly.update({
                "baseline_median": float(baseline_median),
                "baseline_scale": float(baseline_scale),
                "baseline_count": len(lookback),
                "metric_name": "delta_rsoc_per_10min",
            })
            anomalies.append(anomaly)
        return anomalies
