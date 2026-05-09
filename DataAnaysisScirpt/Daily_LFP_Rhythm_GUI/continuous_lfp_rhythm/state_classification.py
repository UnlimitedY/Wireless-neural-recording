import json
import math

import h5py
import numpy as np
from scipy import ndimage, signal

from .config import resolve_config
from .mode3_working import build_working_mask_from_mode3, get_working_window_mask, mask_to_bouts
from .preprocess import apply_lowpass_filter, apply_notch_filter, common_median_reference, interpolate_short_nan_gaps_1d


STATE_CODES = {
    "Wake": 0,
    "MiniWake": 1,
    "NREM": 2,
    "REM": 3,
    "Working": 4,
}
ANALYSIS_STATE_NAMES = ["Wake", "NREM", "REM", "Working"]
OCCUPANCY_STATE_NAMES = ["Wake", "MiniWake", "NREM", "REM", "Working"]


def _replace_dataset(group, name, data, **kwargs):
    if name in group:
        del group[name]
    return group.create_dataset(name, data=data, **kwargs)


def _clear_group(group):
    for key in list(group.keys()):
        del group[key]
    for key in list(group.attrs.keys()):
        del group.attrs[key]


def _string_array(strings):
    return np.asarray(strings, dtype=h5py.string_dtype(encoding="utf-8"))


def _normalize_channel_groups(groups, n_channels):
    normalized_groups = []
    assigned = set()
    for group in groups or []:
        if not isinstance(group, (list, tuple)):
            continue
        valid = []
        for value in group:
            try:
                idx = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n_channels and idx not in valid:
                valid.append(idx)
        if valid:
            normalized_groups.append(valid)
            assigned.update(valid)
    for idx in range(n_channels):
        if idx not in assigned:
            normalized_groups.append([idx])
    if not normalized_groups:
        normalized_groups = [list(range(n_channels))]
    return normalized_groups


def _window_fraction(mask, start_times_s, window_s, fs):
    mask_arr = np.asarray(mask, dtype=np.float64)
    window_samples = max(1, int(round(window_s * fs)))
    prefix = np.concatenate([[0.0], np.cumsum(mask_arr)])
    starts = np.clip(np.round(start_times_s * fs).astype(np.int64), 0, max(mask_arr.size - 1, 0))
    ends = np.clip(starts + window_samples, 0, mask_arr.size)
    counts = prefix[ends] - prefix[starts]
    denom = np.maximum(ends - starts, 1)
    return counts / denom


def _compute_imu_scores(accel_xyz, imu_fs, start_times_s, window_s, method, batch_size=512):
    accel = np.asarray(accel_xyz, dtype=np.float64)
    if accel.ndim != 2 or accel.shape[0] < 3:
        return np.full(start_times_s.shape, np.nan, dtype=np.float64)
    magnitude = np.sqrt(np.nansum(accel[:3] ** 2, axis=0))
    window_samples = max(1, int(round(window_s * imu_fs)))
    scores = np.full(start_times_s.shape, np.nan, dtype=np.float64)
    starts = np.clip(
        np.round(start_times_s * imu_fs).astype(np.int64),
        0,
        max(magnitude.size - window_samples, 0),
    )
    for batch_start in range(0, starts.size, batch_size):
        batch_idx = starts[batch_start : batch_start + batch_size]
        segments = np.stack([magnitude[idx : idx + window_samples] for idx in batch_idx], axis=0)
        finite = np.isfinite(segments)
        medians = np.nanmedian(segments, axis=1, keepdims=True)
        centered = segments - medians
        if method == "magnitude_std":
            batch_scores = np.nanstd(centered, axis=1)
        else:
            batch_scores = np.sqrt(np.nanmean(centered ** 2, axis=1))
        batch_scores[~np.any(finite, axis=1)] = np.nan
        scores[batch_start : batch_start + batch_scores.size] = batch_scores
    return scores


def _gaussian_smooth_nan(values, sigma_epochs):
    arr = np.asarray(values, dtype=np.float64)
    if arr.size == 0 or sigma_epochs <= 0:
        return arr.copy()
    finite = np.isfinite(arr)
    if not np.any(finite):
        return np.full(arr.shape, np.nan, dtype=np.float64)
    filled = np.where(finite, arr, 0.0)
    weights = finite.astype(np.float64)
    smooth_values = ndimage.gaussian_filter1d(filled, sigma=sigma_epochs, mode="nearest")
    smooth_weights = ndimage.gaussian_filter1d(weights, sigma=sigma_epochs, mode="nearest")
    out = np.divide(
        smooth_values,
        np.maximum(smooth_weights, 1e-12),
        out=np.full(arr.shape, np.nan, dtype=np.float64),
        where=smooth_weights > 1e-12,
    )
    out[~finite & (smooth_weights <= 1e-12)] = np.nan
    return out


def _get_band_limits(cfg, band_name, default_low, default_high):
    bands = cfg.get("features.bands", {}) or {}
    band = bands.get(band_name, None) if isinstance(bands, dict) else None
    if isinstance(band, (list, tuple)) and len(band) >= 2:
        try:
            return float(band[0]), float(band[1])
        except (TypeError, ValueError):
            pass
    return float(default_low), float(default_high)


def _resample_if_needed(signal_1d, fs, target_fs):
    if fs <= target_fs:
        return np.asarray(signal_1d, dtype=np.float64), float(fs)
    up = int(round(target_fs))
    down = int(round(fs))
    g = math.gcd(up, down)
    up //= g
    down //= g
    return signal.resample_poly(np.asarray(signal_1d, dtype=np.float64), up, down), float(target_fs)


def _resolve_bipolar_partner(channel_idx, channel_groups, edge_mode):
    for group in channel_groups:
        if channel_idx not in group:
            continue
        if len(group) == 1:
            return None, 0.0
        pos = group.index(channel_idx)
        if pos < len(group) - 1:
            return group[pos + 1], -1.0
        if edge_mode == "nan":
            return None, np.nan
        return group[pos - 1], -1.0
    return None, 0.0


def _load_channel_signal(h5f, channel_idx, config, channel_groups, chunk_samples=600000, apply_state_lowpass=True):
    raw = h5f["lfp/raw_lfp"]
    classification_use_raw_signal = bool(config.get("states.classification_use_raw_signal", True))
    rereference_method = str(config.get("preprocess.rereference_method", "none")).strip().lower()
    edge_mode = str(config.get("preprocess.adjacent_bipolar_edge_mode", "repeat_last_pair")).strip().lower()
    short_gap_s = float(config.get("preprocess.short_gap_interpolate_max_s", 2.0))
    lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
    short_gap_samples = max(0, int(round(short_gap_s * lfp_fs)))
    notch_enable = bool(config.get("preprocess.notch_enable", False))
    notch_freq_hz = float(config.get("preprocess.notch_freq_hz", 50.0))
    do_median_ref = bool(config.get("preprocess.median_reference", False))
    lowpass_enable = bool(config.get("states.classification_lowpass_enable", True))
    lowpass_order = int(config.get("states.classification_lowpass_order", 4))
    lowpass_cutoff = float(config.get("states.classification_lowpass_cutoff_hz", 55.0))

    if classification_use_raw_signal:
        signal_1d = np.asarray(raw[channel_idx], dtype=np.float64)
    elif rereference_method == "adjacent_bipolar":
        partner_idx, partner_weight = _resolve_bipolar_partner(channel_idx, channel_groups, edge_mode)
        signal_1d = np.asarray(raw[channel_idx], dtype=np.float64)
        if partner_idx is not None:
            signal_1d = signal_1d + partner_weight * np.asarray(raw[partner_idx], dtype=np.float64)
        elif np.isnan(partner_weight):
            signal_1d[:] = np.nan
    elif rereference_method in {"common_median", "median"} or do_median_ref:
        signal_1d = np.empty(raw.shape[1], dtype=np.float64)
        for start in range(0, raw.shape[1], chunk_samples):
            end = min(raw.shape[1], start + chunk_samples)
            chunk = np.asarray(raw[:, start:end], dtype=np.float64)
            referenced = common_median_reference(chunk)
            signal_1d[start:end] = referenced[channel_idx]
    else:
        signal_1d = np.asarray(raw[channel_idx], dtype=np.float64)

    signal_1d = interpolate_short_nan_gaps_1d(signal_1d, short_gap_samples)
    if notch_enable and np.isfinite(signal_1d).any():
        signal_1d = apply_notch_filter(signal_1d[np.newaxis, :], lfp_fs, notch_freq_hz)[0]
    if apply_state_lowpass and lowpass_enable and np.isfinite(signal_1d).any():
        signal_1d = apply_lowpass_filter(signal_1d[np.newaxis, :], lfp_fs, lowpass_cutoff, order=lowpass_order)[0]
    return signal_1d


def _prepare_interp_weights(source_freqs, target_freqs):
    valid = np.asarray(source_freqs, dtype=np.float64)
    targets = np.asarray(target_freqs, dtype=np.float64)
    upper = np.searchsorted(valid, targets, side="left")
    upper = np.clip(upper, 1, len(valid) - 1)
    lower = upper - 1
    denom = np.maximum(valid[upper] - valid[lower], 1e-12)
    weight = (targets - valid[lower]) / denom
    return lower, upper, weight


def _compute_log_spectrogram(signal_1d, fs, start_times_s, window_s, log_freqs, batch_size=64):
    signal_arr = np.asarray(signal_1d, dtype=np.float64)
    if signal_arr.size == 0:
        return np.full((start_times_s.size, log_freqs.size), np.nan, dtype=np.float64)
    window_samples = max(8, int(round(window_s * fs)))
    starts = np.clip(
        np.round(start_times_s * fs).astype(np.int64),
        0,
        max(signal_arr.size - window_samples, 0),
    )
    fft_freqs = np.fft.rfftfreq(window_samples, d=1.0 / fs)
    freq_mask = (fft_freqs >= np.min(log_freqs)) & (fft_freqs <= np.max(log_freqs))
    if not np.any(freq_mask):
        return np.full((start_times_s.size, log_freqs.size), np.nan, dtype=np.float64)
    masked_freqs = fft_freqs[freq_mask]
    low_idx, high_idx, weight = _prepare_interp_weights(masked_freqs, log_freqs)
    output = np.full((starts.size, log_freqs.size), np.nan, dtype=np.float64)
    finite_signal = signal_arr[np.isfinite(signal_arr)]
    fill_value = float(np.nanmedian(finite_signal)) if finite_signal.size else 0.0
    window_fn = signal.windows.hann(window_samples, sym=False)

    for batch_start in range(0, starts.size, batch_size):
        batch_starts = starts[batch_start : batch_start + batch_size]
        segments = np.stack([signal_arr[idx : idx + window_samples] for idx in batch_starts], axis=0)
        segments = np.where(np.isfinite(segments), segments, fill_value)
        segments = segments * window_fn[np.newaxis, :]
        fft_vals = np.fft.rfft(segments, axis=1)
        power = (np.abs(fft_vals) ** 2)[:, freq_mask]
        interp_power = power[:, low_idx] * (1.0 - weight[np.newaxis, :]) + power[:, high_idx] * weight[np.newaxis, :]
        output[batch_start : batch_start + interp_power.shape[0]] = interp_power
    return output


def _compute_band_power_score(signal_1d, fs, start_times_s, window_s, low_hz, high_hz, batch_size=64):
    signal_arr = np.asarray(signal_1d, dtype=np.float64)
    if signal_arr.size == 0:
        return np.full(start_times_s.shape, np.nan, dtype=np.float64)
    window_samples = max(8, int(round(window_s * fs)))
    starts = np.clip(
        np.round(start_times_s * fs).astype(np.int64),
        0,
        max(signal_arr.size - window_samples, 0),
    )
    fft_freqs = np.fft.rfftfreq(window_samples, d=1.0 / fs)
    band_mask = (fft_freqs >= float(low_hz)) & (fft_freqs <= float(high_hz))
    if not np.any(band_mask):
        return np.full(starts.shape, np.nan, dtype=np.float64)
    output = np.full(starts.shape, np.nan, dtype=np.float64)
    finite_signal = signal_arr[np.isfinite(signal_arr)]
    fill_value = float(np.nanmedian(finite_signal)) if finite_signal.size else 0.0
    window_fn = signal.windows.hann(window_samples, sym=False)

    for batch_start in range(0, starts.size, batch_size):
        batch_starts = starts[batch_start : batch_start + batch_size]
        segments = np.stack([signal_arr[idx : idx + window_samples] for idx in batch_starts], axis=0)
        segments = np.where(np.isfinite(segments), segments, fill_value)
        segments = segments * window_fn[np.newaxis, :]
        fft_vals = np.fft.rfft(segments, axis=1)
        power = (np.abs(fft_vals) ** 2)[:, band_mask]
        output[batch_start : batch_start + power.shape[0]] = np.nanmean(power, axis=1)
    return output


def _otsu_threshold(values, bins):
    vals = np.asarray(values, dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return np.nan
    hist, edges = np.histogram(vals, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    weight1 = np.cumsum(hist)
    weight2 = np.cumsum(hist[::-1])[::-1]
    mean1 = np.divide(np.cumsum(hist * centers), weight1, out=np.zeros_like(centers), where=weight1 > 0)
    mean2 = np.divide(
        np.cumsum((hist * centers)[::-1])[::-1],
        weight2,
        out=np.zeros_like(centers),
        where=weight2 > 0,
    )
    variance = weight1[:-1] * weight2[1:] * (mean1[:-1] - mean2[1:]) ** 2
    if variance.size == 0 or not np.isfinite(variance).any():
        return float(np.nanmedian(vals))
    idx = int(np.nanargmax(variance))
    return float(centers[idx])


def _estimate_hist_bins(values, cfg):
    vals = np.asarray(values, dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    manual_bins = int(cfg.get("states.threshold_hist_bins", 200))
    auto_enable = bool(cfg.get("states.threshold_hist_bins_auto", True))
    min_bins = max(10, int(cfg.get("states.threshold_hist_bins_min", 40)))
    max_bins = max(min_bins, int(cfg.get("states.threshold_hist_bins_max", 320)))
    if vals.size < 2:
        return int(np.clip(manual_bins, min_bins, max_bins))
    if not auto_enable:
        return int(np.clip(manual_bins, min_bins, max_bins))
    q25, q75 = np.nanpercentile(vals, [25.0, 75.0])
    iqr = float(q75 - q25)
    data_range = float(np.nanmax(vals) - np.nanmin(vals))
    if data_range <= 1e-12:
        return min_bins
    if iqr > 1e-12:
        bin_width = 2.0 * iqr / max(vals.size ** (1.0 / 3.0), 1e-12)
        bins = int(math.ceil(data_range / max(bin_width, 1e-12)))
    else:
        bins = int(math.ceil(np.sqrt(vals.size)))
    return int(np.clip(bins, min_bins, max_bins))


def _find_bimodal_threshold(values, cfg, bins_override=None):
    vals = np.asarray(values, dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size < 10:
        threshold = float(np.nanmedian(vals)) if vals.size else np.nan
        return threshold, {
            "method": "median_fallback",
            "peak_count": 0,
            "hist_bins": int(bins_override) if bins_override is not None else _estimate_hist_bins(vals, cfg),
        }

    bins = int(bins_override) if bins_override is not None else _estimate_hist_bins(vals, cfg)
    sigma = float(cfg.get("states.threshold_hist_smoothing_sigma_bins", 2.0))
    prominence_fraction = float(cfg.get("states.threshold_min_peak_prominence_fraction", 0.05))
    distance_fraction = float(cfg.get("states.threshold_min_peak_distance_fraction", 0.15))

    hist, edges = np.histogram(vals, bins=bins)
    centers = (edges[:-1] + edges[1:]) / 2.0
    smooth = ndimage.gaussian_filter1d(hist.astype(np.float64), sigma=sigma)
    min_prominence = max(np.max(smooth) * prominence_fraction, 1e-6)
    min_distance = max(1, int(round(bins * distance_fraction)))
    peaks, _ = signal.find_peaks(smooth, prominence=min_prominence, distance=min_distance)

    if peaks.size >= 2:
        peak_order = np.argsort(smooth[peaks])[::-1][:2]
        peak_positions = np.sort(peaks[peak_order])
        left, right = int(peak_positions[0]), int(peak_positions[1])
        trough_offset = int(np.argmin(smooth[left : right + 1]))
        trough_idx = left + trough_offset
        threshold = float(centers[trough_idx])
        valley_height = float(smooth[trough_idx])
        peak_heights = [float(smooth[left]), float(smooth[right])]
        valley_ratio = valley_height / max(min(peak_heights), 1e-12)
        peak_sep = float(abs(centers[right] - centers[left]) / max(np.nanstd(vals), 1e-12))
        return threshold, {
            "method": "trough",
            "peak_count": int(peaks.size),
            "hist_bins": int(bins),
            "peak_positions": [float(centers[left]), float(centers[right])],
            "trough_position": threshold,
            "valley_ratio": valley_ratio,
            "peak_separation_std": peak_sep,
        }

    threshold = _otsu_threshold(vals, bins)
    return threshold, {
        "method": "otsu_fallback",
        "peak_count": int(peaks.size),
        "hist_bins": int(bins),
        "trough_position": float(threshold) if np.isfinite(threshold) else np.nan,
    }


def preview_bimodal_threshold(values, cfg, bins_override=None):
    return _find_bimodal_threshold(values, cfg, bins_override=bins_override)


def _compute_lfhf_band_powers(spectrogram, low_mask, high_mask):
    spec = np.asarray(spectrogram, dtype=np.float64)
    low_power = np.nanmean(spec[:, low_mask], axis=1) if np.any(low_mask) else np.full(spec.shape[0], np.nan, dtype=np.float64)
    high_power = np.nanmean(spec[:, high_mask], axis=1) if np.any(high_mask) else np.full(spec.shape[0], np.nan, dtype=np.float64)
    return low_power, high_power


def _zscore_against_mask(values, norm_mask):
    vals = np.asarray(values, dtype=np.float64)
    mask = np.asarray(norm_mask, dtype=bool)
    ref_mask = mask & np.isfinite(vals)
    if np.count_nonzero(ref_mask) < 2:
        ref_mask = np.isfinite(vals)
    if np.count_nonzero(ref_mask) == 0:
        return np.full(vals.shape, np.nan, dtype=np.float64)
    mean = float(np.nanmean(vals[ref_mask]))
    std = float(np.nanstd(vals[ref_mask]))
    if not np.isfinite(std) or std < 1e-12:
        out = vals - mean
        out[~np.isfinite(vals)] = np.nan
        return out
    out = (vals - mean) / std
    out[~np.isfinite(vals)] = np.nan
    return out


def _compute_lfhf_score_from_band_powers(low_power, high_power, norm_mask):
    low_arr = np.asarray(low_power, dtype=np.float64)
    high_arr = np.asarray(high_power, dtype=np.float64)
    z_low = _zscore_against_mask(low_arr, norm_mask)
    z_high = _zscore_against_mask(high_arr, norm_mask)
    score = z_low - z_high
    score[~(np.isfinite(low_arr) & np.isfinite(high_arr))] = np.nan
    return score


def _auto_sleep_imu_mask(imu_score_smoothed, base_valid_mask, cfg):
    sleep_imu_threshold_auto, sleep_imu_diag = _find_bimodal_threshold(imu_score_smoothed[base_valid_mask], cfg)
    low_imu_auto = base_valid_mask & np.isfinite(imu_score_smoothed) & (imu_score_smoothed <= sleep_imu_threshold_auto)
    return low_imu_auto, float(sleep_imu_threshold_auto), sleep_imu_diag


def _zscore_spectrogram_by_freq(spectrogram, norm_mask):
    spec = np.asarray(spectrogram, dtype=np.float64)
    if spec.ndim != 2:
        return np.full_like(spec, np.nan, dtype=np.float64)
    mask = np.asarray(norm_mask, dtype=bool)
    if mask.shape[0] != spec.shape[0]:
        mask = np.isfinite(spec).all(axis=1)
    ref_mask = mask & np.isfinite(spec).all(axis=1)
    if np.count_nonzero(ref_mask) < 2:
        ref_mask = np.isfinite(spec).all(axis=1)
    out = np.full(spec.shape, np.nan, dtype=np.float64)
    if np.count_nonzero(ref_mask) == 0:
        return out
    ref = spec[ref_mask]
    mean = np.nanmean(ref, axis=0)
    std = np.nanstd(ref, axis=0)
    safe_std = np.where(np.isfinite(std) & (std >= 1e-12), std, np.nan)
    out = (spec - mean[np.newaxis, :]) / safe_std[np.newaxis, :]
    fallback = spec - mean[np.newaxis, :]
    bad = ~np.isfinite(safe_std)
    if np.any(bad):
        out[:, bad] = fallback[:, bad]
    out[~np.isfinite(spec)] = np.nan
    return out


def _selection_score_from_ratio(ratio_scores, coverage, diag):
    if not np.isfinite(ratio_scores).any():
        return -np.inf
    contrast = float(np.nanpercentile(ratio_scores, 90.0) - np.nanpercentile(ratio_scores, 10.0))
    if not np.isfinite(contrast):
        contrast = 0.0
    bimodality = 0.0
    if diag.get("method") == "trough":
        valley_ratio = float(diag.get("valley_ratio", 1.0))
        peak_sep = float(diag.get("peak_separation_std", 0.0))
        bimodality = max(0.0, peak_sep) * max(0.0, 1.0 - valley_ratio)
    return float(coverage * (0.25 + contrast) * (0.25 + bimodality))


def _select_representative_channel(h5f, cfg, selection_starts_s, valid_fraction_threshold, channel_groups, selection_low_imu_mask, selection_score_norm_mask):
    raw = h5f["lfp/raw_lfp"]
    n_channels = raw.shape[0]
    lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
    target_fs = float(cfg.get("states.spectrogram_target_fs_hz", 1250.0))
    window_s = float(cfg.get("states.scoring_window_s", 10.0))
    missing_mask = np.asarray(h5f["lfp/missing_mask"][:], dtype=bool)
    working_mask = np.asarray(h5f["task/working_mask"][:], dtype=bool)
    log_freqs = np.geomspace(
        float(cfg.get("states.spectrogram_freq_min_hz", 1.0)),
        float(cfg.get("states.spectrogram_freq_max_hz", 100.0)),
        int(cfg.get("states.spectrogram_n_freq_bins", 50)),
    )
    low_mask = log_freqs < 20.0
    high_mask = log_freqs > 32.0
    working_fraction = _window_fraction(
        working_mask,
        selection_starts_s,
        window_s,
        lfp_fs,
    )
    base_valid = (
        1.0
        - _window_fraction(
            missing_mask,
            selection_starts_s,
            window_s,
            lfp_fs,
        )
        >= valid_fraction_threshold
    )

    scores = np.full(n_channels, -np.inf, dtype=np.float64)
    manual_idx = cfg.get("states.representative_channel_index", None)
    if manual_idx is not None:
        idx = int(manual_idx)
        if 0 <= idx < n_channels:
            scores[idx] = np.inf
            return idx, scores

    for channel_idx in range(n_channels):
        channel_signal = _load_channel_signal(h5f, channel_idx, cfg, channel_groups)
        channel_signal, spec_fs = _resample_if_needed(channel_signal, lfp_fs, target_fs)
        spectrogram = _compute_log_spectrogram(channel_signal, spec_fs, selection_starts_s, window_s, log_freqs)
        non_working_valid = base_valid & (working_fraction < float(cfg.get("working.working_window_min_fraction", 0.5)))
        low_power, high_power = _compute_lfhf_band_powers(spectrogram, low_mask, high_mask)
        lfhf_ratio = _compute_lfhf_score_from_band_powers(low_power, high_power, selection_score_norm_mask)
        diag_threshold, diag = _find_bimodal_threshold(lfhf_ratio[selection_low_imu_mask], cfg)
        _ = diag_threshold
        coverage = float(np.mean(selection_low_imu_mask)) if selection_low_imu_mask.size else 0.0
        scores[channel_idx] = _selection_score_from_ratio(lfhf_ratio, coverage, diag)

    best_idx = int(np.nanargmax(scores)) if np.isfinite(scores).any() else 0
    return best_idx, scores


def _epoch_bouts(mask):
    return mask_to_bouts(np.asarray(mask, dtype=bool))


def _mask_to_bouts_seconds(mask, fs):
    bouts = mask_to_bouts(np.asarray(mask, dtype=bool))
    if not bouts:
        return np.zeros((0, 2), dtype=np.float32)
    return (np.asarray(bouts, dtype=np.float64) / float(fs)).astype(np.float32)


def _feature_window_mask_from_sample_mask(feature_time_s, working_mask, lfp_fs, window_s, threshold):
    result = np.zeros(feature_time_s.shape, dtype=bool)
    window_half = int(round(window_s * lfp_fs / 2.0))
    for idx, center_s in enumerate(feature_time_s):
        center = int(round(center_s * lfp_fs))
        start = max(0, center - window_half)
        end = min(working_mask.size, center + window_half)
        if end <= start:
            continue
        result[idx] = float(np.mean(working_mask[start:end])) >= threshold
    return result


def _split_wake_masks(wake_candidate_mask, min_wake_episode_s, step_s):
    min_wake_epochs = max(1, int(math.ceil(float(min_wake_episode_s) / max(float(step_s), 1e-6))))
    wake_mask = np.zeros_like(wake_candidate_mask, dtype=bool)
    miniwake_mask = np.zeros_like(wake_candidate_mask, dtype=bool)
    for start, end in _epoch_bouts(wake_candidate_mask):
        if (end - start) >= min_wake_epochs:
            wake_mask[start:end] = True
        else:
            miniwake_mask[start:end] = True
    return wake_mask, miniwake_mask


def _state_name_to_codes(names):
    return np.asarray([STATE_CODES[name] for name in names], dtype=np.int16)


def _safe_threshold(value, fallback):
    if value is None:
        return float(fallback)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return float(fallback)
    if not np.isfinite(value):
        return float(fallback)
    return float(value)


def _load_existing_confirmed_working(h5f):
    if "working_mask_confirmed" in h5f["task"]:
        return np.asarray(h5f["task/working_mask_confirmed"][:], dtype=bool)
    if "working_mask" in h5f["task"]:
        return np.asarray(h5f["task/working_mask"][:], dtype=bool)
    return None


def compute_state_review_inputs(h5_path, config=None, working_mask_override=None):
    cfg = resolve_config(config)
    build_working_mask_from_mode3(h5_path, cfg)

    valid_fraction_threshold = float(cfg.get("missing_data.valid_fraction_threshold", 0.8))
    window_s = float(cfg.get("states.scoring_window_s", 10.0))
    step_s = float(cfg.get("states.scoring_step_s", 10.0))
    selection_step_s = float(cfg.get("states.representative_channel_selection_step_s", 1.0))
    target_fs = float(cfg.get("states.spectrogram_target_fs_hz", 1250.0))
    freq_min = float(cfg.get("states.spectrogram_freq_min_hz", 1.0))
    freq_max = float(cfg.get("states.spectrogram_freq_max_hz", 100.0))
    n_freq_bins = int(cfg.get("states.spectrogram_n_freq_bins", 50))
    rem_highfreq_low_hz = float(cfg.get("states.rem_highfreq_low_hz", 80.0))
    rem_highfreq_high_hz = float(cfg.get("states.rem_highfreq_high_hz", 250.0))
    imu_method = str(cfg.get("states.imu_proxy_method", "rms_centered_magnitude")).strip().lower()
    imu_smoothing_sigma_s = float(cfg.get("states.imu_smoothing_sigma_s", 10.0))
    unknown_if_missing_imu = bool(cfg.get("states.unknown_if_missing_imu", True))

    with h5py.File(h5_path, "a") as h5f:
        raw = h5f["lfp/raw_lfp"]
        n_channels = int(raw.shape[0])
        lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
        imu_fs = float(h5f["metadata"].attrs["imu_target_rate_hz"])
        total_duration_s = raw.shape[1] / lfp_fs
        if total_duration_s < window_s:
            raise ValueError("LFP recording is shorter than one state scoring window.")

        mode3_mask = np.asarray(h5f["task/mode3_active_mask"][:], dtype=bool)
        missing_mask = np.asarray(h5f["lfp/missing_mask"][:], dtype=bool)
        accel_xyz = np.asarray(h5f["imu/accel_xyz"][:], dtype=np.float64)
        working_mask_auto = np.asarray(h5f["task/working_mask"][:], dtype=bool)
        if working_mask_override is None:
            existing_confirmed = _load_existing_confirmed_working(h5f)
            working_mask_confirmed = (
                np.asarray(existing_confirmed, dtype=bool)
                if existing_confirmed is not None
                else working_mask_auto.copy()
            )
        else:
            working_mask_confirmed = np.asarray(working_mask_override, dtype=bool).copy()

        task_grp = h5f["task"]
        _replace_dataset(task_grp, "working_mask_auto", data=working_mask_auto.astype(bool), compression="gzip")
        _replace_dataset(task_grp, "working_bouts_auto_seconds", data=_mask_to_bouts_seconds(working_mask_auto, lfp_fs))

        feature_time_s = (
            np.asarray(h5f["time/feature_time_s"][:], dtype=np.float64)
            if "feature_time_s" in h5f["time"]
            else np.zeros((0,), dtype=np.float64)
        )
        feature_working_window_mask = (
            _feature_window_mask_from_sample_mask(
                feature_time_s,
                working_mask_confirmed,
                lfp_fs,
                float(cfg.get("features.feature_window_s", 60.0)),
                float(cfg.get("working.working_window_min_fraction", 0.5)),
            )
            if feature_time_s.size
            else np.zeros((0,), dtype=bool)
        )

        scoring_starts_s = np.arange(0.0, total_duration_s - window_s + 1e-9, step_s, dtype=np.float64)
        selection_starts_s = np.arange(
            0.0,
            total_duration_s - window_s + 1e-9,
            max(selection_step_s, step_s),
            dtype=np.float64,
        )
        state_time_s = scoring_starts_s + window_s / 2.0
        log_freqs = np.geomspace(freq_min, freq_max, n_freq_bins)
        low_mask = log_freqs < 20.0
        high_mask = log_freqs > 32.0
        channel_groups = _normalize_channel_groups(
            cfg.get("rhythm.average_channel_groups", [list(range(n_channels))]),
            n_channels,
        )
        selection_lfp_valid_fraction = 1.0 - _window_fraction(missing_mask, selection_starts_s, window_s, lfp_fs)
        selection_working_fraction = _window_fraction(
            working_mask_confirmed,
            selection_starts_s,
            window_s,
            lfp_fs,
        )
        selection_valid = selection_lfp_valid_fraction >= valid_fraction_threshold
        selection_valid &= selection_working_fraction < float(cfg.get("working.working_window_min_fraction", 0.5))
        selection_imu_score = _compute_imu_scores(accel_xyz, imu_fs, selection_starts_s, window_s, imu_method)
        selection_sigma_epochs = max(0.0, imu_smoothing_sigma_s / max(step_s, 1e-12))
        selection_imu_smoothed = _gaussian_smooth_nan(selection_imu_score, selection_sigma_epochs)
        if unknown_if_missing_imu:
            selection_valid &= np.isfinite(selection_imu_smoothed)
        selection_low_imu_mask, _, _ = _auto_sleep_imu_mask(selection_imu_smoothed, selection_valid, cfg)

        temp_task_mask = np.asarray(h5f["task/working_mask"][:], dtype=bool)
        if working_mask_override is not None:
            del h5f["task"]["working_mask"]
            h5f["task"].create_dataset("working_mask", data=working_mask_confirmed.astype(bool), compression="gzip")
        try:
            representative_idx, channel_scores = _select_representative_channel(
                h5f,
                cfg,
                selection_starts_s,
                valid_fraction_threshold,
                channel_groups,
                selection_low_imu_mask,
                selection_valid,
            )
        finally:
            if working_mask_override is not None:
                del h5f["task"]["working_mask"]
                h5f["task"].create_dataset("working_mask", data=temp_task_mask.astype(bool), compression="gzip")

        representative_signal = _load_channel_signal(h5f, representative_idx, cfg, channel_groups, apply_state_lowpass=True)
        representative_signal, spec_fs = _resample_if_needed(representative_signal, lfp_fs, target_fs)
        representative_signal_raw = _load_channel_signal(h5f, representative_idx, cfg, channel_groups, apply_state_lowpass=False)

        lfp_valid_fraction = 1.0 - _window_fraction(missing_mask, scoring_starts_s, window_s, lfp_fs)
        working_fraction = _window_fraction(
            working_mask_confirmed,
            scoring_starts_s,
            window_s,
            lfp_fs,
        )
        working_epoch_mask = working_fraction >= float(cfg.get("working.working_window_min_fraction", 0.5))

        spectrogram = _compute_log_spectrogram(representative_signal, spec_fs, scoring_starts_s, window_s, log_freqs)
        review_freq_max = min(250.0, max(2.0, spec_fs / 2.0))
        review_log_freqs = np.geomspace(1.0, review_freq_max, 72)
        review_spectrogram = _compute_log_spectrogram(
            representative_signal_raw,
            spec_fs,
            scoring_starts_s,
            window_s,
            review_log_freqs,
        )
        imu_score = _compute_imu_scores(accel_xyz, imu_fs, scoring_starts_s, window_s, imu_method)
        imu_valid = np.isfinite(imu_score)
        imu_smoothing_sigma_epochs = max(0.0, imu_smoothing_sigma_s / max(step_s, 1e-12))
        imu_score_smoothed = _gaussian_smooth_nan(imu_score, imu_smoothing_sigma_epochs)

        global_lfp_valid = lfp_valid_fraction >= valid_fraction_threshold
        rem_highfreq_power_raw = _compute_band_power_score(
            representative_signal_raw,
            lfp_fs,
            scoring_starts_s,
            window_s,
            rem_highfreq_low_hz,
            rem_highfreq_high_hz,
        )

        low_band_power, high_band_power = _compute_lfhf_band_powers(spectrogram, low_mask, high_mask)
        score_valid_mask = (
            (lfp_valid_fraction >= valid_fraction_threshold)
            & np.isfinite(low_band_power)
            & np.isfinite(high_band_power)
            & np.isfinite(rem_highfreq_power_raw)
        )
        if unknown_if_missing_imu:
            score_valid_mask &= np.isfinite(imu_score_smoothed)
        review_spectrogram_z = _zscore_spectrogram_by_freq(review_spectrogram, score_valid_mask)

        non_working_valid = score_valid_mask & (~working_epoch_mask)
        low_imu_auto, sleep_imu_threshold_auto, sleep_imu_diag = _auto_sleep_imu_mask(
            imu_score_smoothed,
            non_working_valid,
            cfg,
        )
        lfhf_score_raw = _compute_lfhf_score_from_band_powers(low_band_power, high_band_power, score_valid_mask)
        lfhf_ratio_score = _gaussian_smooth_nan(lfhf_score_raw, imu_smoothing_sigma_epochs)
        nrem_lfhf_threshold_auto, nrem_diag = _find_bimodal_threshold(lfhf_ratio_score[low_imu_auto], cfg)
        nrem_auto = low_imu_auto & np.isfinite(lfhf_ratio_score) & (lfhf_ratio_score >= nrem_lfhf_threshold_auto)
        rem_pool_auto = low_imu_auto & (~nrem_auto)
        rem_highfreq_score_raw = _zscore_against_mask(rem_highfreq_power_raw, score_valid_mask)
        rem_highfreq_power = _gaussian_smooth_nan(rem_highfreq_score_raw, imu_smoothing_sigma_epochs)
        rem_highfreq_threshold_auto, rem_hf_diag = _find_bimodal_threshold(rem_highfreq_power[rem_pool_auto], cfg)
        rem_auto = (
            rem_pool_auto
            & np.isfinite(rem_highfreq_power)
            & (rem_highfreq_power <= rem_highfreq_threshold_auto)
        )
        wake_candidate_auto = non_working_valid & (~nrem_auto) & (~rem_auto)
        wake_auto, miniwake_auto = _split_wake_masks(
            wake_candidate_auto,
            float(cfg.get("states.min_wake_episode_s", 420.0)),
            step_s,
        )

        review_state = {
            "h5_path": h5_path,
            "total_duration_s": float(total_duration_s),
            "lfp_fs": float(lfp_fs),
            "imu_fs": float(imu_fs),
            "scoring_window_s": float(window_s),
            "scoring_step_s": float(step_s),
            "state_time_s": state_time_s.astype(np.float64),
            "scoring_starts_s": scoring_starts_s.astype(np.float64),
            "feature_time_s": feature_time_s.astype(np.float64),
            "feature_working_window_mask": feature_working_window_mask.astype(bool),
            "mode3_mask": mode3_mask.astype(bool),
            "working_mask_auto": working_mask_auto.astype(bool),
            "working_mask_confirmed": working_mask_confirmed.astype(bool),
            "working_epoch_mask_confirmed": working_epoch_mask.astype(bool),
            "lfp_valid_fraction": lfp_valid_fraction.astype(np.float64),
            "score_valid_mask": score_valid_mask.astype(bool),
            "representative_channel_index": int(representative_idx),
            "representative_channel_scores": channel_scores.astype(np.float64),
            "lf_band_power": low_band_power.astype(np.float64),
            "hf_band_power": high_band_power.astype(np.float64),
            "lfhf_ratio_score_raw": lfhf_score_raw.astype(np.float64),
            "lfhf_ratio_score": lfhf_ratio_score.astype(np.float64),
            "rem_highfreq_score_raw": rem_highfreq_score_raw.astype(np.float64),
            "rem_highfreq_power": rem_highfreq_power.astype(np.float64),
            "review_spectrogram_z": review_spectrogram_z.astype(np.float32),
            "review_spectrogram_freqs_hz": review_log_freqs.astype(np.float32),
            "imu_score": imu_score.astype(np.float64),
            "imu_score_smoothed": imu_score_smoothed.astype(np.float64),
            "imu_smoothing_sigma_s": float(imu_smoothing_sigma_s),
            "low_imu_mask_confirmed": low_imu_auto.astype(bool),
            "sleep_imu_threshold": float(sleep_imu_threshold_auto),
            "nrem_lfhf_threshold": float(nrem_lfhf_threshold_auto),
            "rem_highfreq_threshold": float(rem_highfreq_threshold_auto),
            "wake_min_duration_s": float(cfg.get("states.min_wake_episode_s", 420.0)),
            "threshold_diags": {
                "sleep_imu": sleep_imu_diag,
                "nrem_lfhf": nrem_diag,
                "rem_highfreq": rem_hf_diag,
            },
            "wake_candidate_mask_confirmed": wake_candidate_auto.astype(bool),
            "nrem_mask_confirmed": nrem_auto.astype(bool),
            "rem_mask_confirmed": rem_auto.astype(bool),
            "wake_mask_confirmed": wake_auto.astype(bool),
            "miniwake_mask_confirmed": miniwake_auto.astype(bool),
            "final_override_labels": np.full(state_time_s.shape, -1, dtype=np.int16),
            "final_review_confirmed_hours": np.zeros(int(math.ceil(total_duration_s / 3600.0)), dtype=bool),
            "review_stage": "working_review",
            "review_complete": False,
        }
        return recalculate_review_state(review_state, cfg)


def recalculate_review_state(review_state, config=None):
    cfg = resolve_config(config)
    state_time_s = np.asarray(review_state["state_time_s"], dtype=np.float64)
    working_epoch_mask = _window_fraction(
        np.asarray(review_state["working_mask_confirmed"], dtype=bool),
        np.asarray(review_state["scoring_starts_s"], dtype=np.float64),
        float(review_state["scoring_window_s"]),
        float(review_state["lfp_fs"]),
    ) >= float(cfg.get("working.working_window_min_fraction", 0.5))
    review_state["working_epoch_mask_confirmed"] = working_epoch_mask.astype(bool)

    score_valid_mask = np.asarray(review_state["score_valid_mask"], dtype=bool)
    non_working_valid = score_valid_mask & (~working_epoch_mask)
    imu_score = np.asarray(review_state["imu_score"], dtype=np.float64)
    lfhf_score_raw = np.asarray(review_state["lfhf_ratio_score_raw"], dtype=np.float64)
    rem_highfreq_score_raw = np.asarray(review_state["rem_highfreq_score_raw"], dtype=np.float64)
    rem_highfreq_power = np.asarray(review_state["rem_highfreq_power"], dtype=np.float64)
    imu_smoothing_sigma_s = _safe_threshold(
        review_state.get("imu_smoothing_sigma_s"),
        float(cfg.get("states.imu_smoothing_sigma_s", 10.0)),
    )
    imu_smoothing_sigma_epochs = max(0.0, imu_smoothing_sigma_s / max(float(review_state["scoring_step_s"]), 1e-12))
    imu_score_smoothed = _gaussian_smooth_nan(imu_score, imu_smoothing_sigma_epochs)

    sleep_imu_threshold = _safe_threshold(
        review_state.get("sleep_imu_threshold"),
        np.nanmedian(imu_score_smoothed[non_working_valid]) if np.any(non_working_valid) else 0.0,
    )
    low_imu_mask = non_working_valid & np.isfinite(imu_score_smoothed) & (imu_score_smoothed <= sleep_imu_threshold)
    lfhf_ratio_score = _gaussian_smooth_nan(lfhf_score_raw, imu_smoothing_sigma_epochs)
    nrem_lfhf_threshold = _safe_threshold(
        review_state.get("nrem_lfhf_threshold"),
        np.nanmedian(lfhf_ratio_score[low_imu_mask]) if np.any(low_imu_mask) else 0.0,
    )
    nrem_mask = low_imu_mask & np.isfinite(lfhf_ratio_score) & (lfhf_ratio_score >= nrem_lfhf_threshold)
    rem_pool = low_imu_mask & (~nrem_mask)
    rem_highfreq_power = _gaussian_smooth_nan(rem_highfreq_score_raw, imu_smoothing_sigma_epochs)

    rem_highfreq_threshold = _safe_threshold(
        review_state.get("rem_highfreq_threshold"),
        np.nanmedian(rem_highfreq_power[rem_pool]) if np.any(rem_pool) else 0.0,
    )
    rem_mask = (
        rem_pool
        & np.isfinite(rem_highfreq_power)
        & (rem_highfreq_power <= rem_highfreq_threshold)
    )
    wake_candidate = non_working_valid & (~nrem_mask) & (~rem_mask)

    wake_min_duration_s = _safe_threshold(
        review_state.get("wake_min_duration_s"),
        float(cfg.get("states.min_wake_episode_s", 420.0)),
    )
    wake_mask, miniwake_mask = _split_wake_masks(
        wake_candidate,
        wake_min_duration_s,
        float(review_state["scoring_step_s"]),
    )

    labels = np.full(state_time_s.shape, STATE_CODES["Wake"], dtype=np.int16)
    labels[miniwake_mask] = STATE_CODES["MiniWake"]
    labels[nrem_mask] = STATE_CODES["NREM"]
    labels[rem_mask] = STATE_CODES["REM"]
    labels[working_epoch_mask] = STATE_CODES["Working"]

    final_override_labels = np.asarray(
        review_state.get("final_override_labels", np.full(state_time_s.shape, -1, dtype=np.int16)),
        dtype=np.int16,
    )
    if final_override_labels.shape != labels.shape:
        final_override_labels = np.full(labels.shape, -1, dtype=np.int16)
    final_labels = labels.copy()
    override_mask = final_override_labels >= 0
    final_labels[override_mask] = final_override_labels[override_mask]

    review_state.update(
        {
            "imu_score_smoothed": imu_score_smoothed.astype(np.float64),
            "imu_smoothing_sigma_s": float(imu_smoothing_sigma_s),
            "low_imu_mask_confirmed": low_imu_mask.astype(bool),
            "sleep_imu_threshold": float(sleep_imu_threshold),
            "nrem_lfhf_threshold": float(nrem_lfhf_threshold),
            "rem_highfreq_power": rem_highfreq_power.astype(np.float64),
            "rem_highfreq_threshold": float(rem_highfreq_threshold),
            "wake_min_duration_s": float(wake_min_duration_s),
            "wake_candidate_mask_confirmed": wake_candidate.astype(bool),
            "nrem_mask_confirmed": nrem_mask.astype(bool),
            "rem_mask_confirmed": rem_mask.astype(bool),
            "wake_mask_confirmed": wake_mask.astype(bool),
            "miniwake_mask_confirmed": miniwake_mask.astype(bool),
            "final_override_labels": final_override_labels.astype(np.int16),
            "auto_labels": labels.astype(np.int16),
            "final_labels": final_labels.astype(np.int16),
        }
    )
    return review_state


def refresh_review_state_after_working(review_state, config=None):
    cfg = resolve_config(config)
    next_state = dict(review_state)
    feature_time_s = np.asarray(next_state.get("feature_time_s", np.zeros((0,), dtype=np.float64)), dtype=np.float64)
    if feature_time_s.size:
        next_state["feature_working_window_mask"] = _feature_window_mask_from_sample_mask(
            feature_time_s,
            np.asarray(next_state["working_mask_confirmed"], dtype=bool),
            float(next_state["lfp_fs"]),
            float(cfg.get("features.feature_window_s", 60.0)),
            float(cfg.get("working.working_window_min_fraction", 0.5)),
        ).astype(bool)
    else:
        next_state["feature_working_window_mask"] = np.zeros((0,), dtype=bool)
    return recalculate_review_state(next_state, cfg)


def load_review_progress(h5_path):
    with h5py.File(h5_path, "r") as h5f:
        if "states" not in h5f or "review" not in h5f["states"]:
            return None
        review_grp = h5f["states/review"]
        out = {}
        for key in review_grp.keys():
            out[key] = np.asarray(review_grp[key][:]) if isinstance(review_grp[key], h5py.Dataset) and review_grp[key].shape != () else review_grp[key][()]
        out["review_stage"] = _decode_review_attr(review_grp.attrs.get("review_stage", "working_review"))
        out["review_complete"] = bool(review_grp.attrs.get("review_complete", False))
        thresholds_json = _decode_review_attr(review_grp.attrs.get("thresholds_json", "{}"))
        try:
            out["thresholds_json"] = json.loads(thresholds_json)
        except Exception:
            out["thresholds_json"] = {}
        return out


def _decode_review_attr(value):
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.ndarray) and value.shape == ():
        return _decode_review_attr(value.item())
    return value


def merge_saved_review_state(review_state, saved_state, config=None):
    if not saved_state:
        return recalculate_review_state(review_state, config)
    thresholds_json = saved_state.get("thresholds_json", {}) if isinstance(saved_state, dict) else {}
    if isinstance(thresholds_json, dict):
        review_state["nrem_lfhf_threshold"] = _safe_threshold(
            thresholds_json.get("nrem_lfhf", {}).get("current_threshold", review_state["nrem_lfhf_threshold"]),
            review_state["nrem_lfhf_threshold"],
        )
        review_state["sleep_imu_threshold"] = _safe_threshold(
            thresholds_json.get("sleep_imu", {}).get("current_threshold", thresholds_json.get("rem_imu", {}).get("current_threshold", review_state["sleep_imu_threshold"])),
            review_state["sleep_imu_threshold"],
        )
        review_state["rem_highfreq_threshold"] = _safe_threshold(
            thresholds_json.get("rem_highfreq", {}).get("current_threshold", review_state["rem_highfreq_threshold"]),
            review_state["rem_highfreq_threshold"],
        )
        review_state["imu_smoothing_sigma_s"] = _safe_threshold(
            thresholds_json.get("sleep_imu", {}).get("smoothing_sigma_s", thresholds_json.get("rem_imu", {}).get("smoothing_sigma_s", review_state["imu_smoothing_sigma_s"])),
            review_state["imu_smoothing_sigma_s"],
        )
        review_state["wake_min_duration_s"] = _safe_threshold(
            thresholds_json.get("wake_split", {}).get("current_threshold_s", review_state["wake_min_duration_s"]),
            review_state["wake_min_duration_s"],
        )
    if "working_mask_confirmed" in saved_state:
        review_state["working_mask_confirmed"] = np.asarray(saved_state["working_mask_confirmed"], dtype=bool)
    if "final_override_labels" in saved_state:
        review_state["final_override_labels"] = np.asarray(saved_state["final_override_labels"], dtype=np.int16)
    if "final_review_confirmed_hours" in saved_state:
        review_state["final_review_confirmed_hours"] = np.asarray(saved_state["final_review_confirmed_hours"], dtype=bool)
    review_state["review_stage"] = str(saved_state.get("review_stage", review_state.get("review_stage", "working_review")))
    review_state["review_complete"] = bool(saved_state.get("review_complete", False))
    return recalculate_review_state(review_state, config)


def save_review_progress(h5_path, review_state, config=None, stage=None, review_complete=None, recalculate_before_save=True):
    cfg = resolve_config(config)
    review_state = dict(review_state)
    if recalculate_before_save:
        review_state = recalculate_review_state(review_state, cfg)
    if stage is not None:
        review_state["review_stage"] = str(stage)
    if review_complete is not None:
        review_state["review_complete"] = bool(review_complete)

    thresholds_json = json.dumps(
        {
            "working": {
                "merge_gap_s": float(cfg.get("working.working_merge_gap_s", 5.0)),
                "min_bout_s": float(cfg.get("working.working_min_bout_s", 120.0)),
                "tail_exclude_s": float(cfg.get("working.working_tail_exclude_s", 30.0)),
                "window_min_fraction": float(cfg.get("working.working_window_min_fraction", 0.5)),
            },
            "nrem_lfhf": {
                "current_threshold": float(review_state["nrem_lfhf_threshold"]),
                "method": review_state.get("threshold_diags", {}).get("nrem_lfhf", {}).get("method", "manual_override"),
            },
            "sleep_imu": {
                "current_threshold": float(review_state["sleep_imu_threshold"]),
                "smoothing_sigma_s": float(review_state["imu_smoothing_sigma_s"]),
                "method": review_state.get("threshold_diags", {}).get("sleep_imu", {}).get("method", "manual_override"),
            },
            "rem_highfreq": {
                "current_threshold": float(review_state["rem_highfreq_threshold"]),
                "method": review_state.get("threshold_diags", {}).get("rem_highfreq", {}).get("method", "manual_override"),
            },
            "wake_split": {
                "current_threshold_s": float(review_state["wake_min_duration_s"]),
            },
        },
        ensure_ascii=False,
    )

    with h5py.File(h5_path, "a") as h5f:
        task_grp = h5f["task"]
        current_stage = str(review_state.get("review_stage", stage or "working_review"))
        should_persist_working = (
            current_stage == "working_review"
            or bool(review_state.get("review_complete", False))
            or "working_mask_confirmed" not in task_grp
        )
        if should_persist_working:
            _replace_dataset(task_grp, "working_mask_auto", data=np.asarray(review_state["working_mask_auto"], dtype=bool), compression="gzip")
            _replace_dataset(task_grp, "working_mask_confirmed", data=np.asarray(review_state["working_mask_confirmed"], dtype=bool), compression="gzip")
            _replace_dataset(task_grp, "working_bouts_auto_seconds", data=_mask_to_bouts_seconds(review_state["working_mask_auto"], review_state["lfp_fs"]))
            _replace_dataset(task_grp, "working_bouts_confirmed_seconds", data=_mask_to_bouts_seconds(review_state["working_mask_confirmed"], review_state["lfp_fs"]))

        states_grp = h5f["states"]
        if "review" in states_grp:
            review_grp = states_grp["review"]
            _clear_group(review_grp)
        else:
            review_grp = states_grp.create_group("review")
        _replace_dataset(review_grp, "final_override_labels", data=np.asarray(review_state["final_override_labels"], dtype=np.int16), compression="gzip")
        _replace_dataset(review_grp, "final_labels_preview", data=np.asarray(review_state["final_labels"], dtype=np.int16), compression="gzip")
        _replace_dataset(review_grp, "final_review_confirmed_hours", data=np.asarray(review_state["final_review_confirmed_hours"], dtype=bool), compression="gzip")
        review_grp.attrs["review_stage"] = str(review_state.get("review_stage", "working_review"))
        review_grp.attrs["review_complete"] = bool(review_state.get("review_complete", False))
        review_grp.attrs["thresholds_json"] = thresholds_json
    return h5_path


def commit_reviewed_states(h5_path, review_state, config=None):
    cfg = resolve_config(config)
    review_state = recalculate_review_state(dict(review_state), cfg)
    review_state["review_complete"] = True
    review_state["review_stage"] = "complete"
    save_review_progress(h5_path, review_state, cfg, stage="complete", review_complete=True, recalculate_before_save=False)

    with h5py.File(h5_path, "a") as h5f:
        states_grp = h5f["states"]
        review_grp = states_grp["review"] if "review" in states_grp else None
        if review_grp is not None:
            preserved_review = {key: np.asarray(review_grp[key][:]) if review_grp[key].shape != () else review_grp[key][()] for key in review_grp.keys()}
            preserved_attrs = {key: review_grp.attrs[key] for key in review_grp.attrs.keys()}
        else:
            preserved_review = None
            preserved_attrs = None
        _clear_group(states_grp)

        labels = np.asarray(review_state["final_labels"], dtype=np.int16)
        state_valid_mask = np.asarray(review_state["score_valid_mask"], dtype=bool) | np.asarray(
            review_state["working_epoch_mask_confirmed"], dtype=bool
        )
        state_analysis_mask = state_valid_mask & np.isin(labels, _state_name_to_codes(ANALYSIS_STATE_NAMES))
        _replace_dataset(states_grp, "state_label", data=labels.astype(np.int16), compression="gzip")
        _replace_dataset(states_grp, "state_valid_mask", data=state_valid_mask, compression="gzip")
        _replace_dataset(states_grp, "state_analysis_mask", data=state_analysis_mask, compression="gzip")
        _replace_dataset(states_grp, "working_epoch_mask", data=np.asarray(review_state["working_epoch_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(states_grp, "wake_candidate_mask", data=np.asarray(review_state["wake_candidate_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(states_grp, "miniwake_mask", data=np.asarray(review_state["miniwake_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(states_grp, "representative_channel_index", data=np.int16(review_state["representative_channel_index"]))
        _replace_dataset(states_grp, "representative_channel_scores", data=np.asarray(review_state["representative_channel_scores"], dtype=np.float32), compression="gzip")
        _replace_dataset(states_grp, "imu_score", data=np.asarray(review_state["imu_score"], dtype=np.float32), compression="gzip")
        _replace_dataset(states_grp, "imu_score_smoothed", data=np.asarray(review_state["imu_score_smoothed"], dtype=np.float32), compression="gzip")
        _replace_dataset(states_grp, "low_imu_mask", data=np.asarray(review_state["low_imu_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(states_grp, "lfhf_ratio_score", data=np.asarray(review_state["lfhf_ratio_score"], dtype=np.float32), compression="gzip")
        _replace_dataset(states_grp, "rem_highfreq_power", data=np.asarray(review_state["rem_highfreq_power"], dtype=np.float32), compression="gzip")
        _replace_dataset(states_grp, "sleep_imu_threshold", data=np.float32(review_state["sleep_imu_threshold"]))
        _replace_dataset(states_grp, "nrem_lfhf_threshold", data=np.float32(review_state["nrem_lfhf_threshold"]))
        _replace_dataset(states_grp, "rem_highfreq_threshold", data=np.float32(review_state["rem_highfreq_threshold"]))
        _replace_dataset(states_grp, "imu_smoothing_sigma_s", data=np.float32(review_state["imu_smoothing_sigma_s"]))
        _replace_dataset(states_grp, "state_names", data=_string_array(OCCUPANCY_STATE_NAMES))
        _replace_dataset(states_grp, "state_analysis_names", data=_string_array(ANALYSIS_STATE_NAMES))
        thresholds_json = preserved_attrs["thresholds_json"] if preserved_attrs and "thresholds_json" in preserved_attrs else "{}"
        _replace_dataset(states_grp, "threshold_method_json", data=np.asarray(str(thresholds_json), dtype=h5py.string_dtype(encoding="utf-8")))
        states_grp.attrs["state_name_map"] = json.dumps(STATE_CODES, ensure_ascii=False)
        states_grp.attrs["state_classifier"] = "interactive_state_review_wizard"
        states_grp.attrs["representative_channel_index"] = int(review_state["representative_channel_index"])

        if preserved_review is not None:
            review_subgrp = states_grp.create_group("review")
            for key, value in preserved_review.items():
                _replace_dataset(review_subgrp, key, data=value, compression="gzip" if isinstance(value, np.ndarray) and value.shape != () else None)
            for key, value in preserved_attrs.items():
                review_subgrp.attrs[key] = value

        time_grp = h5f["time"]
        _replace_dataset(time_grp, "state_time_s", data=np.asarray(review_state["state_time_s"], dtype=np.float32), compression="gzip")

        task_grp = h5f["task"]
        _replace_dataset(task_grp, "working_mask", data=np.asarray(review_state["working_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(task_grp, "working_mask_confirmed", data=np.asarray(review_state["working_mask_confirmed"], dtype=bool), compression="gzip")
        _replace_dataset(task_grp, "working_mask_auto", data=np.asarray(review_state["working_mask_auto"], dtype=bool), compression="gzip")
        _replace_dataset(task_grp, "working_bouts_seconds", data=_mask_to_bouts_seconds(review_state["working_mask_confirmed"], review_state["lfp_fs"]))
        _replace_dataset(task_grp, "working_bouts_confirmed_seconds", data=_mask_to_bouts_seconds(review_state["working_mask_confirmed"], review_state["lfp_fs"]))
        _replace_dataset(task_grp, "working_bouts_auto_seconds", data=_mask_to_bouts_seconds(review_state["working_mask_auto"], review_state["lfp_fs"]))
        _replace_dataset(task_grp, "working_window_mask", data=np.asarray(review_state["feature_working_window_mask"], dtype=bool), compression="gzip")

        if "rhythm" in h5f:
            _clear_group(h5f["rhythm"])
        if "hourly_time_s" in h5f["time"]:
            del h5f["time"]["hourly_time_s"]

    return h5_path


def classify_states(h5_path, config=None):
    cfg = resolve_config(config)
    review_state = compute_state_review_inputs(h5_path, cfg)
    return commit_reviewed_states(h5_path, review_state, cfg)
