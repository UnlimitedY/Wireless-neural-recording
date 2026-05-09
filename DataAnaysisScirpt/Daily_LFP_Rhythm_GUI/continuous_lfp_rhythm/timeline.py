import datetime
import numpy as np

from .tz_compat import ZoneInfo


def group_by_day(file_records, timezone="Asia/Shanghai"):
    """
    Assign aligned file records to local calendar days.
    """
    if not file_records:
        return {}

    tz = ZoneInfo(timezone)
    daily_groups = {}
    for row in file_records:
        try:
            start_dt = datetime.datetime.fromtimestamp(row["start_ts"] / 1000.0, tz=datetime.timezone.utc).astimezone(tz)
            end_dt = datetime.datetime.fromtimestamp(row["end_ts"] / 1000.0, tz=datetime.timezone.utc).astimezone(tz)
        except Exception:
            continue

        for date_obj in {start_dt.date(), end_dt.date()}:
            date_str = date_obj.strftime("%Y-%m-%d")
            daily_groups.setdefault(date_str, []).append(row)
    return daily_groups


def get_mapping_indices(row_start_ts, n_samples, fs, target_date_str, timezone="Asia/Shanghai"):
    """
    Map an EDF segment onto a daily absolute time axis.
    """
    try:
        tz = ZoneInfo(timezone)
        daily_start = datetime.datetime.strptime(target_date_str, "%Y-%m-%d").replace(tzinfo=tz)
        daily_start_ts_ms = daily_start.timestamp() * 1000.0
    except Exception:
        return None

    abs_start_s = (row_start_ts - daily_start_ts_ms) / 1000.0
    # Use conventional half-up rounding instead of Python's bankers rounding so that
    # modality-specific mappings stay consistent at half-sample boundaries.
    start_idx = int(np.floor((abs_start_s * fs) + 0.5))
    end_idx = start_idx + int(n_samples)
    daily_max_samples = int(86400 * fs)

    write_start = max(0, start_idx)
    write_end = min(daily_max_samples, end_idx)
    if write_start >= daily_max_samples or write_end <= 0:
        return None

    return {
        "write_start": write_start,
        "write_end": write_end,
        "read_start": write_start - start_idx,
        "read_end": write_end - start_idx,
        "daily_max": daily_max_samples,
    }
