import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Optional


PACKET_LOSS_ALERT_THRESHOLD_PERCENT = 1.0
BATTERY_LOW_THRESHOLD_PERCENT = 20.0
ROLLUP_WINDOW_SECONDS = 3600.0
VALID_BQ25176_STATS = {0, 1, 256, 257}


@dataclass(frozen=True)
class BQ25176DecodedStatus:
    raw_stat: int
    pg_raw: int
    stat_raw: int
    power_good: bool
    stat_reports_charging: bool
    power_state_text: str
    power_state_level: str
    charge_state_text: str
    charge_state_level: str
    status_text: str


def normalize_bq25176_stat(value: Any) -> Optional[int]:
    try:
        stat = int(float(value))
    except Exception:
        return None
    if stat in VALID_BQ25176_STATS:
        return stat
    return None


def decode_bq25176_stat(value: Any) -> Optional[BQ25176DecodedStatus]:
    stat = normalize_bq25176_stat(value)
    if stat is None:
        return None

    pg_raw = (stat >> 8) & 0x01
    stat_raw = stat & 0x01
    power_good = pg_raw == 0
    stat_reports_charging = stat_raw == 0
    if power_good:
        power_state_text = "Power good"
        power_state_level = "ok"
        if stat_reports_charging:
            charge_state_text = "Charging"
            charge_state_level = "ok"
            status_text = "VIN good, charge in progress"
        else:
            charge_state_text = "Not charging"
            charge_state_level = "warning"
            status_text = "VIN good, not charging or charge complete"
    else:
        power_state_text = "Power not good"
        power_state_level = "danger"
        charge_state_text = "Charge unavailable"
        charge_state_level = "danger"
        status_text = "VIN not power-good, charger unavailable"
    return BQ25176DecodedStatus(
        raw_stat=stat,
        pg_raw=pg_raw,
        stat_raw=stat_raw,
        power_good=power_good,
        stat_reports_charging=stat_reports_charging,
        power_state_text=power_state_text,
        power_state_level=power_state_level,
        charge_state_text=charge_state_text,
        charge_state_level=charge_state_level,
        status_text=status_text,
    )


def detect_bq25176_charge_fault_blink(samples: Iterable[Any], min_transitions: int = 3) -> bool:
    """Detect a possible STAT blink pattern from recent PG/STAT samples.

    BQ25176J exposes faults through the STAT pin blink pattern, so one
    instantaneous sample cannot prove a fault. A short rolling window with PG
    good and repeated STAT transitions is a useful GUI-side "possible fault"
    indicator without changing firmware.
    """
    decoded = [
        item
        for item in (decode_bq25176_stat(sample) for sample in samples)
        if item is not None and item.power_good
    ]
    if len(decoded) < max(3, int(min_transitions) + 1):
        return False
    stat_values = [int(item.stat_raw) for item in decoded]
    if len(set(stat_values)) < 2:
        return False
    transitions = sum(
        1
        for previous, current in zip(stat_values, stat_values[1:])
        if int(previous) != int(current)
    )
    return transitions >= int(min_transitions)


def compute_packet_loss_rollup(
    samples: Iterable[Mapping[str, Any]],
    now_epoch: Optional[float] = None,
    window_seconds: float = ROLLUP_WINDOW_SECONDS,
) -> Dict[str, Any]:
    now_epoch = float(now_epoch if now_epoch is not None else time.time())
    cutoff = now_epoch - float(window_seconds)
    total_missing = 0
    total_received = 0
    sample_count = 0

    for sample in samples:
        timestamp = float(sample.get("timestamp_epoch", 0.0) or 0.0)
        if timestamp < cutoff or timestamp > now_epoch:
            continue
        missing = max(0, int(sample.get("packet_loss", 0) or 0))
        received = max(0, int(sample.get("packet_count", 0) or 0))
        if missing <= 0 and received <= 0:
            continue
        total_missing += missing
        total_received += received
        sample_count += 1

    expected_packets = total_missing + total_received
    percent = (100.0 * total_missing / expected_packets) if expected_packets > 0 else 0.0
    return {
        "percent": float(percent),
        "missing_packets": int(total_missing),
        "received_packets": int(total_received),
        "expected_packets": int(expected_packets),
        "sample_count": int(sample_count),
    }


def classify_packet_loss_state(
    packet_loss_percent: Any,
    expected_packets: Any,
    threshold_percent: float = PACKET_LOSS_ALERT_THRESHOLD_PERCENT,
) -> str:
    try:
        expected_packets = int(expected_packets or 0)
    except Exception:
        expected_packets = 0
    if expected_packets <= 0:
        return "unknown"

    try:
        packet_loss_percent = float(packet_loss_percent or 0.0)
    except Exception:
        packet_loss_percent = 0.0
    return "ok" if packet_loss_percent < float(threshold_percent) else "alert"


def classify_battery_state(
    rsoc: Any,
    voltage: Any = 0.0,
    low_threshold_percent: float = BATTERY_LOW_THRESHOLD_PERCENT,
) -> str:
    try:
        rsoc = float(rsoc or 0.0)
    except Exception:
        rsoc = 0.0
    try:
        voltage = float(voltage or 0.0)
    except Exception:
        voltage = 0.0

    if rsoc <= 0.0 and voltage <= 0.0:
        return "unknown"
    return "low" if rsoc < float(low_threshold_percent) else "ok"
