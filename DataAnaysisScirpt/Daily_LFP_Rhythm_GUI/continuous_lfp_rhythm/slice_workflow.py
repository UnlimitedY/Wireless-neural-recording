import json
import os
from datetime import datetime, timezone

from .tz_compat import ZoneInfo


def load_slice_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    slices = payload.get("slices", [])
    if not isinstance(slices, list) or not slices:
        raise ValueError("Slice JSON does not contain any slices.")
    normalized = []
    for idx, item in enumerate(slices, start=1):
        if "start_epoch_ms" not in item or "end_epoch_ms" not in item:
            raise ValueError(f"Slice #{idx} is missing start_epoch_ms/end_epoch_ms.")
        start_ms = int(round(float(item["start_epoch_ms"])))
        end_ms = int(round(float(item["end_epoch_ms"])))
        if end_ms <= start_ms:
            raise ValueError(f"Slice #{idx} has non-positive duration.")
        normalized.append(
            {
                **item,
                "id": int(item.get("id", idx)),
                "label": str(item.get("label") or f"slice_{idx:03d}"),
                "start_epoch_ms": start_ms,
                "end_epoch_ms": end_ms,
                "duration_s": float((end_ms - start_ms) / 1000.0),
            }
        )
    payload = dict(payload)
    payload["slices"] = normalized
    return payload


def format_slice_option(slice_item, timezone_name="Asia/Shanghai"):
    tz = ZoneInfo(timezone_name)
    start_dt = datetime.fromtimestamp(slice_item["start_epoch_ms"] / 1000.0, tz=timezone.utc).astimezone(tz)
    end_dt = datetime.fromtimestamp(slice_item["end_epoch_ms"] / 1000.0, tz=timezone.utc).astimezone(tz)
    duration_s = float(slice_item.get("duration_s", 0.0))
    return (
        f"{slice_item.get('label', 'slice')} | "
        f"{start_dt.strftime('%Y-%m-%d %H:%M:%S')} -> {end_dt.strftime('%H:%M:%S')} "
        f"({duration_s:.1f}s)"
    )


def resolve_neural_dirs_from_slice_payload(payload):
    source_root = payload.get("source_root", "")
    if not source_root:
        raise ValueError("Slice JSON does not include source_root.")
    source_root = os.path.realpath(os.path.abspath(os.path.expanduser(str(source_root))))
    candidates = [source_root]
    if os.path.basename(source_root).upper().startswith("GUIBW"):
        candidates.append(os.path.dirname(source_root))

    for root in candidates:
        neural_dir = _child_dir_case_insensitive(root, "Neural")
        if neural_dir:
            return {
                "source_root": os.path.realpath(root),
                "neural_dir": neural_dir,
                "mode0_dir": _child_dir_case_insensitive(neural_dir, "Mode0"),
                "mode3_dir": _child_dir_case_insensitive(neural_dir, "Mode3"),
            }
    raise ValueError(f"Could not find Neural folder for source_root: {source_root}")


def filter_records_for_slice(records, slice_item):
    start_ms = float(slice_item["start_epoch_ms"])
    end_ms = float(slice_item["end_epoch_ms"])
    filtered = []
    for row in records:
        row_start = float(row.get("start_ts", 0.0))
        row_end = float(row.get("end_ts", 0.0))
        if row_end > start_ms and row_start < end_ms:
            filtered.append(row)
    return sorted(filtered, key=lambda item: item.get("start_ts", 0.0))


def _child_dir_case_insensitive(parent, expected_name):
    if not parent or not os.path.isdir(parent):
        return None
    expected = expected_name.lower()
    for name in os.listdir(parent):
        path = os.path.join(parent, name)
        if os.path.isdir(path) and name.lower() == expected:
            return os.path.realpath(os.path.abspath(path))
    return None
