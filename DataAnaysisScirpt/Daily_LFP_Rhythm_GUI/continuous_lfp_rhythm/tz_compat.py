from datetime import timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
except ImportError:  # Python 3.8
    try:
        from backports.zoneinfo import ZoneInfo as _ZoneInfo
    except ImportError:
        _ZoneInfo = None


def ZoneInfo(timezone_name):
    if _ZoneInfo is not None:
        return _ZoneInfo(timezone_name)

    fixed_offsets = {
        "Asia/Shanghai": timezone(timedelta(hours=8), "Asia/Shanghai"),
        "UTC": timezone.utc,
        "Etc/UTC": timezone.utc,
    }
    if timezone_name in fixed_offsets:
        return fixed_offsets[timezone_name]
    raise ImportError(
        "Python 3.8 needs backports.zoneinfo or tzdata for timezone "
        f"{timezone_name!r}. Install backports.zoneinfo, or use Asia/Shanghai/UTC."
    )
