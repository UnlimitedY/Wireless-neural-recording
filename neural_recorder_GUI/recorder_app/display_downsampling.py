import numpy as np


def normalize_downsample_factor(value, minimum=1, maximum=64):
    try:
        factor = int(value)
    except Exception:
        factor = int(minimum)
    return max(int(minimum), min(int(maximum), factor))


def _coerce_xy(x_values, y_values):
    try:
        x_arr = np.asarray(x_values).reshape(-1)
        y_arr = np.asarray(y_values).reshape(-1)
    except Exception:
        return None, None, 0
    length = min(int(x_arr.size), int(y_arr.size))
    if length <= 0:
        return x_arr[:0], y_arr[:0], 0
    return x_arr[:length], y_arr[:length], length


def _gap_boundary_indices(y_arr):
    nonfinite = ~np.isfinite(y_arr)
    if not nonfinite.any():
        return []
    finite = ~nonfinite
    previous_finite = np.concatenate(([False], finite[:-1]))
    next_finite = np.concatenate((finite[1:], [False]))
    return np.flatnonzero(nonfinite & (previous_finite | next_finite)).tolist()


def _downsample_xy_peak_envelope(x_arr, y_arr, factor):
    factor = normalize_downsample_factor(factor)
    length = min(int(x_arr.size), int(y_arr.size))
    bucket_size = int(factor) * 2
    if length <= bucket_size * 2:
        return x_arr, y_arr
    bucket_count = length // bucket_size
    if bucket_count <= 0:
        return x_arr, y_arr

    view_len = bucket_count * bucket_size
    tail_start = view_len
    buckets = y_arr[:view_len].reshape(bucket_count, bucket_size)
    finite = np.isfinite(buckets)
    has_finite = finite.any(axis=1)
    min_positions = np.argmin(np.where(finite, buckets, np.inf), axis=1)
    max_positions = np.argmax(np.where(finite, buckets, -np.inf), axis=1)
    base_indices = np.arange(bucket_count, dtype=np.int64) * bucket_size
    min_indices = base_indices + min_positions
    max_indices = base_indices + max_positions
    min_indices = np.where(has_finite, min_indices, base_indices)
    max_indices = np.where(has_finite, max_indices, base_indices + bucket_size - 1)
    first_indices = np.minimum(min_indices, max_indices)
    second_indices = np.maximum(min_indices, max_indices)
    same_indices = first_indices == second_indices
    second_indices = np.where(same_indices, base_indices + bucket_size - 1, second_indices)
    out_indices = np.column_stack((first_indices, second_indices)).reshape(-1).tolist()

    if tail_start < length:
        tail = y_arr[tail_start:length]
        finite = np.isfinite(tail)
        if finite.any():
            finite_positions = np.flatnonzero(finite)
            finite_values = tail[finite]
            min_idx = tail_start + int(finite_positions[int(np.argmin(finite_values))])
            max_idx = tail_start + int(finite_positions[int(np.argmax(finite_values))])
            if min_idx <= max_idx:
                out_indices.extend((min_idx, max_idx))
            else:
                out_indices.extend((max_idx, min_idx))
        elif tail.size:
            out_indices.extend((tail_start, length - 1))

    if out_indices and out_indices[-1] != length - 1:
        out_indices.append(length - 1)

    out_indices.extend(_gap_boundary_indices(y_arr[:length]))

    indices = np.asarray(sorted(set(out_indices)), dtype=np.int64)
    return x_arr[indices], y_arr[indices]


def _downsample_xy_block_mean(x_arr, y_arr, factor):
    factor = normalize_downsample_factor(factor)
    length = min(int(x_arr.size), int(y_arr.size))
    if length <= factor * 2:
        return x_arr, y_arr

    bucket_size = int(factor)
    bucket_count = int(np.ceil(float(length) / float(bucket_size)))
    out_x = []
    out_y = []
    for bucket_index in range(bucket_count):
        start = bucket_index * bucket_size
        end = min(length, start + bucket_size)
        if start >= end:
            continue
        x_bucket = x_arr[start:end]
        y_bucket = y_arr[start:end]
        out_x.append(float(np.mean(x_bucket)))
        finite = np.isfinite(y_bucket)
        if finite.any():
            out_y.append(float(np.mean(y_bucket[finite])))
        else:
            out_y.append(np.nan)

    gap_indices = _gap_boundary_indices(y_arr[:length])
    if gap_indices:
        out_indices = np.asarray(sorted(set(gap_indices)), dtype=np.int64)
        combined_x = np.concatenate((np.asarray(out_x), x_arr[out_indices]))
        combined_y = np.concatenate((np.asarray(out_y), y_arr[out_indices]))
        order = np.argsort(combined_x, kind="mergesort")
        return combined_x[order], combined_y[order]
    return np.asarray(out_x), np.asarray(out_y)


def _downsample_xy_lttb(x_arr, y_arr, factor):
    factor = normalize_downsample_factor(factor)
    length = min(int(x_arr.size), int(y_arr.size))
    target = max(3, int(np.ceil(float(length) / float(factor))))
    if length <= target or target >= length or length <= 3:
        return x_arr, y_arr

    y_for_area = np.asarray(y_arr, dtype=np.float64)
    finite_y = np.isfinite(y_for_area)
    if finite_y.any():
        fill_value = float(np.median(y_for_area[finite_y]))
    else:
        fill_value = 0.0
    y_for_area = np.where(finite_y, y_for_area, fill_value)
    x_for_area = np.asarray(x_arr, dtype=np.float64)
    finite_x = np.isfinite(x_for_area)
    if not finite_x.all():
        x_for_area = np.arange(length, dtype=np.float64)

    sampled = [0]
    a = 0
    every = float(length - 2) / float(target - 2)
    for out_index in range(target - 2):
        avg_start = int(np.floor((out_index + 1) * every)) + 1
        avg_end = int(np.floor((out_index + 2) * every)) + 1
        avg_end = min(length, avg_end)
        if avg_start >= avg_end:
            avg_start = min(length - 1, avg_start)
            avg_end = min(length, avg_start + 1)
        avg_x = float(np.mean(x_for_area[avg_start:avg_end]))
        avg_y = float(np.mean(y_for_area[avg_start:avg_end]))

        bucket_start = int(np.floor(out_index * every)) + 1
        bucket_end = int(np.floor((out_index + 1) * every)) + 1
        bucket_start = max(1, min(length - 2, bucket_start))
        bucket_end = max(bucket_start + 1, min(length - 1, bucket_end))
        bucket_indices = np.arange(bucket_start, bucket_end, dtype=np.int64)
        ax = x_for_area[a]
        ay = y_for_area[a]
        bx = x_for_area[bucket_indices]
        by = y_for_area[bucket_indices]
        area = np.abs((ax - avg_x) * (by - ay) - (ax - bx) * (avg_y - ay))
        if area.size:
            chosen = int(bucket_indices[int(np.argmax(area))])
        else:
            chosen = bucket_start
        sampled.append(chosen)
        a = chosen
    sampled.append(length - 1)
    sampled.extend(_gap_boundary_indices(y_arr[:length]))
    indices = np.asarray(sorted(set(sampled)), dtype=np.int64)
    return x_arr[indices], y_arr[indices]


def downsample_xy_for_display(x_values, y_values, factor, strategy="peak"):
    factor = normalize_downsample_factor(factor)
    x_arr, y_arr, length = _coerce_xy(x_values, y_values)
    if x_arr is None:
        return x_values, y_values
    if length <= 0:
        return x_arr[:0], y_arr[:0]
    if factor <= 1:
        if int(np.asarray(x_values).size) == int(np.asarray(y_values).size):
            return x_values, y_values
        return x_arr, y_arr

    strategy = str(strategy or "peak").strip().lower()
    if strategy in {"mean", "block_mean", "continuous", "lfp"}:
        return _downsample_xy_block_mean(x_arr, y_arr, factor)
    if strategy in {"lttb", "peak_aware", "raw", "spike", "ap"}:
        return _downsample_xy_lttb(x_arr, y_arr, factor)
    return _downsample_xy_peak_envelope(x_arr, y_arr, factor)


def display_downsample_strategy_for_stream(stream_key):
    stream_key = str(stream_key or "").strip().lower()
    if stream_key in {"lfp", "mode2", "mode3_lfp_esa", "esa", "mand", "sensor"}:
        return "mean"
    if stream_key in {"spike", "mode3_raw", "mode0_raw", "raw", "ap"}:
        return "lttb"
    return "peak"


def adjust_downsample_factor(
    current_factor,
    stable_windows,
    loss_percent,
    render_ms,
    *,
    loss_threshold_percent=0.1,
    render_threshold_ms=83.0,
    max_factor=64,
    stable_windows_required=30,
):
    current_factor = normalize_downsample_factor(current_factor, maximum=max_factor)
    stable_windows = max(0, int(stable_windows or 0))
    try:
        render_ms = float(render_ms or 0.0)
    except Exception:
        render_ms = 0.0

    if render_ms > float(render_threshold_ms):
        return normalize_downsample_factor(current_factor * 2, maximum=max_factor), 0, "increase_render"

    if current_factor > 1 and render_ms <= (float(render_threshold_ms) * 0.5):
        stable_windows += 1
        if stable_windows >= int(stable_windows_required):
            return normalize_downsample_factor(max(1, current_factor // 2), maximum=max_factor), 0, "decrease_stable"
        return current_factor, stable_windows, "hold_stable"

    return current_factor, 0, "hold"


def adjust_coupled_downsample_factors(
    current_factors,
    stable_windows,
    stream_metrics,
    *,
    loss_threshold_percent=0.1,
    render_threshold_ms=83.0,
    max_factor=64,
    max_factor_ratio=4,
    stable_windows_required=30,
):
    keys = [str(key) for key in stream_metrics.keys()]
    factors = {
        key: normalize_downsample_factor(
            current_factors.get(key, 1) if isinstance(current_factors, dict) else 1,
            maximum=max_factor,
        )
        for key in keys
    }
    metrics = {}
    for key in keys:
        raw_metric = stream_metrics.get(key, {}) if isinstance(stream_metrics, dict) else {}
        try:
            render_ms = float(raw_metric.get("render_ms", 0.0) or 0.0)
        except Exception:
            render_ms = 0.0
        metrics[key] = {
            "loss_percent": 0.0,
            "render_ms": max(0.0, render_ms),
        }

    try:
        stable_windows = max(0, int(stable_windows or 0))
    except Exception:
        stable_windows = 0
    try:
        max_factor_ratio = max(1, int(max_factor_ratio or 1))
    except Exception:
        max_factor_ratio = 4

    actions = {key: "hold" for key in keys}
    if not keys:
        return factors, stable_windows, actions

    render_threshold = float(render_threshold_ms)
    total_render_ms = sum(metric["render_ms"] for metric in metrics.values())
    max_render_ms = max((metric["render_ms"] for metric in metrics.values()), default=0.0)
    render_shares = {
        key: (metrics[key]["render_ms"] / total_render_ms) if total_render_ms > 0 else 0.0
        for key in keys
    }
    render_bad_keys = [key for key in keys if metrics[key]["render_ms"] > render_threshold]
    total_render_bad = total_render_ms > render_threshold
    candidates = set()
    for key in render_bad_keys:
        candidates.add(key)

    if total_render_bad and max_render_ms > 0:
        for key in keys:
            if metrics[key]["render_ms"] >= max_render_ms * 0.75 or render_shares.get(key, 0.0) >= 0.35:
                candidates.add(key)

    forced_targets = {}
    ratio_hold_keys = set()
    ratio_boost_keys = set()
    if candidates and len(keys) > 1:
        planned_factors = dict(factors)
        for key in candidates:
            planned_factors[key] = normalize_downsample_factor(factors[key] * 2, maximum=max_factor)
        while True:
            max_key = max(keys, key=lambda key: planned_factors.get(key, 1))
            min_key = min(keys, key=lambda key: planned_factors.get(key, 1))
            max_value = int(planned_factors.get(max_key, 1) or 1)
            min_value = int(planned_factors.get(min_key, 1) or 1)
            if max_value <= min_value * max_factor_ratio or min_value >= int(max_factor):
                break
            if max_key in candidates:
                candidates.discard(max_key)
                planned_factors[max_key] = factors[max_key]
                ratio_hold_keys.add(max_key)
            boosted_min = normalize_downsample_factor(min_value * 2, maximum=max_factor)
            if boosted_min <= min_value:
                break
            candidates.add(min_key)
            ratio_boost_keys.add(min_key)
            forced_targets[min_key] = boosted_min
            planned_factors[min_key] = boosted_min

    if candidates:
        next_factors = dict(factors)
        for key in candidates:
            next_factors[key] = normalize_downsample_factor(
                forced_targets.get(key, factors[key] * 2),
                maximum=max_factor,
            )
            if key in ratio_boost_keys:
                actions[key] = "increase_coupled_balance"
            elif key in render_bad_keys:
                actions[key] = "increase_render"
            elif total_render_bad:
                actions[key] = "increase_coupled_render"
            else:
                actions[key] = "increase_coupled_loss"
        for key in ratio_hold_keys:
            if key not in candidates:
                actions[key] = "hold_coupled_balance"
        return next_factors, 0, actions

    stable_enough = total_render_ms <= render_threshold * 0.5
    if stable_enough and max(factors.values(), default=1) > 1:
        stable_windows += 1
        if stable_windows >= int(stable_windows_required):
            max_factor_value = max(factors.values())
            decrease_candidates = [key for key in keys if factors[key] == max_factor_value]
            decrease_key = max(decrease_candidates, key=lambda key: metrics[key]["render_ms"])
            next_factors = dict(factors)
            next_factors[decrease_key] = normalize_downsample_factor(max(1, factors[decrease_key] // 2), maximum=max_factor)
            actions[decrease_key] = "decrease_coupled_stable"
            return next_factors, 0, actions
        for key in keys:
            actions[key] = "hold_coupled_stable"
        return factors, stable_windows, actions

    return factors, 0, actions
