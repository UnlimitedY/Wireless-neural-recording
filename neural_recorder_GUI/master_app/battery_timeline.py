import time
from typing import Dict, List, Optional, Tuple

try:
    from ..monitoring.battery_history_manager import BatteryHistoryManager
except ImportError:
    from monitoring.battery_history_manager import BatteryHistoryManager


def build_battery_timeline_payload(
    history_path: str,
    now_ts: Optional[float] = None,
) -> Tuple[List[Dict], List[Dict]]:
    reference_ts = float(now_ts) if now_ts is not None else time.time()
    manager = BatteryHistoryManager(storage_path_resolver=lambda: history_path)
    recent_records = manager.get_recent_records(hours=24, now_ts=reference_ts)
    cutoff_ts = reference_ts - 24 * 3600
    anomalies = []
    anomalies.extend(manager.compute_mode0_hourly_anomalies())
    anomalies.extend(manager.compute_mode3_stitched_anomalies())
    recent_anomalies = [
        dict(anomaly)
        for anomaly in anomalies
        if float(anomaly.get("point_time", 0.0) or 0.0) >= cutoff_ts
    ]
    recent_anomalies.sort(key=lambda item: float(item.get("point_time", 0.0) or 0.0))
    return recent_records, recent_anomalies
