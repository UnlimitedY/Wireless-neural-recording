import h5py
import numpy as np
from scipy import signal
import json

from .config import resolve_config
from .preprocess import (
    adjacent_bipolar_reference,
    apply_notch_filter,
    common_median_reference,
    interpolate_short_nan_gaps_matrix,
)


def _replace_dataset(group, name, data, **kwargs):
    if name in group:
        del group[name]
    return group.create_dataset(name, data=data, **kwargs)


def _string_array(strings):
    return np.asarray(strings, dtype=h5py.string_dtype(encoding="utf-8"))


def _clear_group(group):
    for key in list(group.keys()):
        del group[key]
    for key in list(group.attrs.keys()):
        del group.attrs[key]


def _integrate_trapezoid(y, x, axis=-1):
    trapezoid_fn = getattr(np, "trapezoid", None)
    if trapezoid_fn is not None:
        return trapezoid_fn(y, x, axis=axis)
    return np.trapz(y, x, axis=axis)


def _normalize_channel_groups(groups, n_channels):
    normalized_groups = []
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
    if not normalized_groups:
        normalized_groups = [list(range(n_channels))]
    return normalized_groups


def _compute_activity_metric(accel_window, method):
    accel = np.asarray(accel_window, dtype=np.float64)
    if accel.size == 0 or np.all(np.isnan(accel)):
        return np.nan
    magnitude = np.sqrt(np.nansum(accel ** 2, axis=0))
    if not np.isfinite(magnitude).any():
        return np.nan
    magnitude = magnitude - np.nanmedian(magnitude)
    if method == "magnitude_std":
        return float(np.nanstd(magnitude))
    diff = np.diff(magnitude, prepend=magnitude[0])
    return float(np.nanmean(np.abs(diff)))


def _compute_band_features(window_data, fs, bands, spectral_entropy_enable, welch_nperseg, welch_noverlap):
    nperseg = max(32, min(int(welch_nperseg), window_data.shape[1]))
    noverlap = max(0, min(int(welch_noverlap), nperseg - 1))
    freqs, psd = signal.welch(
        window_data,
        fs=fs,
        nperseg=nperseg,
        noverlap=noverlap,
        axis=-1,
        scaling="density",
    )

    band_names = list(bands.keys())
    band_power = np.full((window_data.shape[0], len(band_names)), np.nan, dtype=np.float64)
    for band_idx, band_name in enumerate(band_names):
        low, high = bands[band_name]
        band_mask = (freqs >= low) & (freqs < high)
        if np.any(band_mask):
            band_power[:, band_idx] = _integrate_trapezoid(
                psd[:, band_mask],
                freqs[band_mask],
                axis=-1,
            )

    theta_idx = band_names.index("theta") if "theta" in band_names else None
    delta_idx = band_names.index("delta") if "delta" in band_names else None
    theta_delta_ratio = np.full((window_data.shape[0],), np.nan, dtype=np.float64)
    if theta_idx is not None and delta_idx is not None:
        theta_delta_ratio = band_power[:, theta_idx] / np.maximum(band_power[:, delta_idx], 1e-12)

    entropy = np.full((window_data.shape[0],), np.nan, dtype=np.float64)
    if spectral_entropy_enable:
        psd_sum = np.sum(psd, axis=-1, keepdims=True)
        psd_norm = np.divide(psd, psd_sum, out=np.zeros_like(psd), where=psd_sum > 0)
        with np.errstate(divide="ignore", invalid="ignore"):
            entropy = -np.sum(psd_norm * np.log2(psd_norm + 1e-12), axis=-1)
        entropy /= np.log2(psd.shape[-1])

    band_power_db = 10.0 * np.log10(np.maximum(band_power, 1e-12))
    return band_names, band_power, band_power_db, theta_delta_ratio, entropy


def extract_features(h5_path, config=None):
    cfg = resolve_config(config)
    feature_window_s = float(cfg.get("features.feature_window_s", 60.0))
    feature_step_s = float(cfg.get("features.feature_step_s", 60.0))
    valid_fraction_threshold = float(cfg.get("missing_data.valid_fraction_threshold", 0.8))
    bands = cfg.get("features.bands", {})
    spectral_entropy_enable = bool(cfg.get("features.spectral_entropy_enable", True))
    welch_nperseg = int(cfg.get("features.welch_nperseg", 2048))
    welch_noverlap = int(cfg.get("features.welch_noverlap", 1024))
    short_gap_s = float(cfg.get("preprocess.short_gap_interpolate_max_s", 2.0))
    rereference_method = str(cfg.get("preprocess.rereference_method", "none")).strip().lower()
    bipolar_edge_mode = str(
        cfg.get("preprocess.adjacent_bipolar_edge_mode", "repeat_last_pair")
    ).strip().lower()
    do_median_ref = bool(cfg.get("preprocess.median_reference", False))
    notch_enable = bool(cfg.get("preprocess.notch_enable", False))
    notch_freq_hz = float(cfg.get("preprocess.notch_freq_hz", 50.0))
    activity_method = cfg.get("activity.activity_metric_method", "diff_mag_mean")

    with h5py.File(h5_path, "a") as h5f:
        if "raw_lfp" not in h5f["lfp"]:
            raise ValueError("Daily H5 does not contain /lfp/raw_lfp; cannot extract features.")

        lfp_fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
        imu_fs = float(h5f["metadata"].attrs["imu_target_rate_hz"])
        lfp_data = h5f["lfp/raw_lfp"]
        missing_mask = h5f["lfp/missing_mask"]
        accel_data = h5f["imu/accel_xyz"]

        window_samples = max(1, int(round(feature_window_s * lfp_fs)))
        step_samples = max(1, int(round(feature_step_s * lfp_fs)))
        short_gap_samples = max(0, int(round(short_gap_s * lfp_fs)))
        total_samples = int(lfp_data.shape[1])
        if total_samples < window_samples:
            raise ValueError("LFP recording is shorter than one feature window.")

        n_windows = 1 + ((total_samples - window_samples) // step_samples)
        n_channels = int(lfp_data.shape[0])
        n_bands = len(bands)
        average_channel_groups = _normalize_channel_groups(
            cfg.get("rhythm.average_channel_groups", [list(range(n_channels))]),
            n_channels,
        )

        band_power = np.full((n_windows, n_channels, n_bands), np.nan, dtype=np.float32)
        band_power_db = np.full((n_windows, n_channels, n_bands), np.nan, dtype=np.float32)
        theta_delta_ratio = np.full((n_windows, n_channels), np.nan, dtype=np.float32)
        spectral_entropy = np.full((n_windows, n_channels), np.nan, dtype=np.float32)
        valid_fraction = np.full((n_windows,), np.nan, dtype=np.float32)
        window_missing_fraction = np.full((n_windows,), np.nan, dtype=np.float32)
        activity_metric = np.full((n_windows,), np.nan, dtype=np.float32)
        feature_time_s = np.full((n_windows,), np.nan, dtype=np.float32)

        for win_idx in range(n_windows):
            start_idx = win_idx * step_samples
            end_idx = start_idx + window_samples
            feature_time_s[win_idx] = (start_idx + window_samples / 2.0) / lfp_fs

            raw_window = np.asarray(lfp_data[:, start_idx:end_idx], dtype=np.float64)
            missing_window = np.asarray(missing_mask[start_idx:end_idx], dtype=bool)
            valid_fraction[win_idx] = 1.0 - float(np.mean(missing_window))
            window_missing_fraction[win_idx] = float(np.mean(missing_window))

            imu_start = int(round(start_idx / lfp_fs * imu_fs))
            imu_end = int(round(end_idx / lfp_fs * imu_fs))
            accel_window = np.asarray(accel_data[:, imu_start:imu_end], dtype=np.float64)
            activity_metric[win_idx] = _compute_activity_metric(accel_window, activity_method)

            if valid_fraction[win_idx] < valid_fraction_threshold:
                continue

            proc_window = raw_window.copy()
            proc_window = interpolate_short_nan_gaps_matrix(proc_window, short_gap_samples)
            if rereference_method == "adjacent_bipolar":
                proc_window = adjacent_bipolar_reference(
                    proc_window,
                    edge_mode=bipolar_edge_mode,
                    channel_groups=average_channel_groups,
                )
            elif rereference_method in {"common_median", "median"} or do_median_ref:
                proc_window = common_median_reference(proc_window)
            if notch_enable:
                proc_window = apply_notch_filter(proc_window, lfp_fs, notch_freq_hz)
            if np.isnan(proc_window).any():
                continue

            (
                band_names,
                bp,
                bp_db,
                td_ratio,
                entropy,
            ) = _compute_band_features(
                proc_window,
                lfp_fs,
                bands,
                spectral_entropy_enable,
                welch_nperseg,
                welch_noverlap,
            )
            band_power[win_idx] = bp.astype(np.float32)
            band_power_db[win_idx] = bp_db.astype(np.float32)
            theta_delta_ratio[win_idx] = td_ratio.astype(np.float32)
            spectral_entropy[win_idx] = entropy.astype(np.float32)

        feat_grp = h5f["features"]
        _replace_dataset(feat_grp, "band_power", data=band_power, compression="gzip")
        _replace_dataset(feat_grp, "band_power_db", data=band_power_db, compression="gzip")
        _replace_dataset(feat_grp, "theta_delta_ratio", data=theta_delta_ratio, compression="gzip")
        _replace_dataset(feat_grp, "spectral_entropy", data=spectral_entropy, compression="gzip")
        _replace_dataset(feat_grp, "valid_fraction", data=valid_fraction, compression="gzip")
        _replace_dataset(
            feat_grp,
            "window_missing_fraction",
            data=window_missing_fraction,
            compression="gzip",
        )
        _replace_dataset(feat_grp, "activity_metric", data=activity_metric, compression="gzip")
        _replace_dataset(feat_grp, "feature_band_names", data=_string_array(list(bands.keys())))
        feat_grp.attrs["rereference_method"] = rereference_method
        feat_grp.attrs["adjacent_bipolar_edge_mode"] = bipolar_edge_mode
        feat_grp.attrs["bands_json"] = json.dumps(bands, ensure_ascii=False)

        time_grp = h5f["time"]
        _replace_dataset(time_grp, "feature_time_s", data=feature_time_s)
        if "states" in h5f:
            _clear_group(h5f["states"])
        if "rhythm" in h5f:
            _clear_group(h5f["rhythm"])
        if "hourly_time_s" in time_grp:
            del time_grp["hourly_time_s"]

    return h5_path
