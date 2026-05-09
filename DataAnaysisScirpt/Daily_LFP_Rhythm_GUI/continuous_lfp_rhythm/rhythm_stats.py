import json

import h5py
import numpy as np

from .config import resolve_config
from .state_classification import ANALYSIS_STATE_NAMES, OCCUPANCY_STATE_NAMES, STATE_CODES


def _replace_dataset(group, name, data, **kwargs):
    if name in group:
        del group[name]
    return group.create_dataset(name, data=data, **kwargs)


def _safe_nanmean(data, axis=0):
    valid_count = np.sum(np.isfinite(data), axis=axis)
    summed = np.nansum(data, axis=axis)
    return np.divide(
        summed,
        valid_count,
        out=np.full_like(summed, np.nan, dtype=np.float64),
        where=valid_count > 0,
    )


def _window_fraction_from_sample_mask(sample_mask, centers_s, window_s, fs):
    mask = np.asarray(sample_mask, dtype=bool)
    centers = np.asarray(centers_s, dtype=np.float64)
    fractions = np.full(centers.shape, np.nan, dtype=np.float64)
    if mask.size == 0 or centers.size == 0 or window_s <= 0 or fs <= 0:
        return fractions

    cumulative = np.concatenate([[0], np.cumsum(mask.astype(np.int64))])
    half_window_s = float(window_s) / 2.0
    start_idx = np.floor((centers - half_window_s) * float(fs)).astype(np.int64)
    end_idx = np.ceil((centers + half_window_s) * float(fs)).astype(np.int64)
    start_idx = np.clip(start_idx, 0, mask.size)
    end_idx = np.clip(end_idx, 0, mask.size)
    counts = np.maximum(end_idx - start_idx, 0)
    sums = cumulative[end_idx] - cumulative[start_idx]
    fractions = np.divide(
        sums,
        counts,
        out=np.full(centers.shape, np.nan, dtype=np.float64),
        where=counts > 0,
    )
    return fractions


def _normalize_channel_groups(groups, names, n_channels):
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

    normalized_names = []
    for idx, group in enumerate(normalized_groups):
        if idx < len(names or []) and str(names[idx]).strip():
            normalized_names.append(str(names[idx]).strip())
        else:
            normalized_names.append(f"group_{idx}")
    return normalized_groups, normalized_names


def _compute_feature_window_state_membership(feature_time_s, state_time_s, state_labels, state_valid_mask, window_s):
    counts = np.zeros((feature_time_s.shape[0], len(ANALYSIS_STATE_NAMES)), dtype=np.int32)
    fractions = np.zeros((feature_time_s.shape[0], len(ANALYSIS_STATE_NAMES)), dtype=np.float32)
    dominant = np.full(feature_time_s.shape, -1, dtype=np.int16)
    valid_epoch_count = np.zeros(feature_time_s.shape, dtype=np.int32)
    for idx, center_s in enumerate(feature_time_s):
        start_s = center_s - window_s / 2.0
        end_s = center_s + window_s / 2.0
        epoch_mask = (state_time_s >= start_s) & (state_time_s < end_s) & state_valid_mask
        total = int(np.sum(epoch_mask))
        valid_epoch_count[idx] = total
        if total <= 0:
            continue
        best_state = -1
        best_fraction = -1.0
        for analysis_idx, state_name in enumerate(ANALYSIS_STATE_NAMES):
            code = STATE_CODES[state_name]
            count = int(np.sum(epoch_mask & (state_labels == code)))
            counts[idx, analysis_idx] = count
            fraction = count / total
            fractions[idx, analysis_idx] = fraction
            if fraction > best_fraction:
                best_fraction = fraction
                best_state = code
        dominant[idx] = best_state
    return counts, fractions, dominant, valid_epoch_count


def summarize_rhythm(h5_path, config=None):
    cfg = resolve_config(config)
    hour_bin_s = float(cfg.get("rhythm.hour_bin_s", 3600.0))
    do_state_norm = bool(cfg.get("rhythm.state_reference_normalization_enable", True))
    state_norm_method = cfg.get(
        "rhythm.state_reference_normalization_method",
        "divide_by_daily_state_band_mean",
    )
    feature_window_s = float(cfg.get("features.feature_window_s", 60.0))
    state_window_s = float(cfg.get("states.scoring_window_s", 10.0))
    valid_fraction_threshold = float(cfg.get("missing_data.valid_fraction_threshold", 0.8))

    with h5py.File(h5_path, "a") as h5f:
        band_power = np.asarray(h5f["features/band_power"][:], dtype=np.float64)
        feature_time_s = np.asarray(h5f["time/feature_time_s"][:], dtype=np.float64)
        state_time_s = np.asarray(h5f["time/state_time_s"][:], dtype=np.float64)
        state_labels = np.asarray(h5f["states/state_label"][:], dtype=np.int16)
        state_valid_mask = (
            np.asarray(h5f["states/state_valid_mask"][:], dtype=bool)
            if "state_valid_mask" in h5f["states"]
            else np.ones_like(state_labels, dtype=bool)
        )
        band_names = [str(v) for v in h5f["features/feature_band_names"][:].astype(str)]

        n_windows, n_channels, n_bands = band_power.shape
        lfp_fs = float(h5f["metadata"].attrs.get("lfp_sample_rate_hz", 1000.0))
        lfp_missing_mask = (
            np.asarray(h5f["lfp/missing_mask"][:], dtype=bool)
            if "lfp" in h5f and "missing_mask" in h5f["lfp"]
            else np.zeros((0,), dtype=bool)
        )
        if "lfp" in h5f and "file_coverage_mask" in h5f["lfp"]:
            lfp_coverage_mask = np.asarray(h5f["lfp/file_coverage_mask"][:], dtype=bool)
        elif lfp_missing_mask.size:
            lfp_coverage_mask = ~lfp_missing_mask
        else:
            lfp_coverage_mask = np.ones((0,), dtype=bool)
        sample_count = min(lfp_coverage_mask.size, lfp_missing_mask.size) if lfp_missing_mask.size else lfp_coverage_mask.size
        if sample_count > 0:
            lfp_coverage_mask = lfp_coverage_mask[:sample_count]
            lfp_missing_mask = lfp_missing_mask[:sample_count] if lfp_missing_mask.size else np.zeros(sample_count, dtype=bool)
            lfp_valid_sample_mask = lfp_coverage_mask & (~lfp_missing_mask)
        else:
            lfp_valid_sample_mask = np.zeros((0,), dtype=bool)
        has_lfp_sample_qc = sample_count > 0

        if has_lfp_sample_qc:
            feature_coverage_fraction = _window_fraction_from_sample_mask(
                lfp_coverage_mask,
                feature_time_s,
                feature_window_s,
                lfp_fs,
            )
            feature_lfp_valid_fraction = _window_fraction_from_sample_mask(
                lfp_valid_sample_mask,
                feature_time_s,
                feature_window_s,
                lfp_fs,
            )
        else:
            feature_coverage_fraction = np.ones(feature_time_s.shape, dtype=np.float64)
            feature_lfp_valid_fraction = np.ones(feature_time_s.shape, dtype=np.float64)
        feature_analysis_valid_mask = (
            (feature_coverage_fraction >= valid_fraction_threshold)
            & (feature_lfp_valid_fraction >= valid_fraction_threshold)
        )
        if "valid_fraction" in h5f["features"]:
            stored_feature_valid = np.asarray(h5f["features/valid_fraction"][:], dtype=np.float64)
            if stored_feature_valid.shape == feature_analysis_valid_mask.shape:
                feature_analysis_valid_mask &= stored_feature_valid >= valid_fraction_threshold

        if has_lfp_sample_qc:
            state_coverage_fraction = _window_fraction_from_sample_mask(
                lfp_coverage_mask,
                state_time_s,
                state_window_s,
                lfp_fs,
            )
            state_lfp_valid_fraction = _window_fraction_from_sample_mask(
                lfp_valid_sample_mask,
                state_time_s,
                state_window_s,
                lfp_fs,
            )
        else:
            state_coverage_fraction = np.ones(state_time_s.shape, dtype=np.float64)
            state_lfp_valid_fraction = np.ones(state_time_s.shape, dtype=np.float64)
        state_analysis_valid_mask = (
            state_valid_mask
            & (state_coverage_fraction >= valid_fraction_threshold)
            & (state_lfp_valid_fraction >= valid_fraction_threshold)
        )
        average_channel_groups, average_group_names = _normalize_channel_groups(
            cfg.get("rhythm.average_channel_groups", [list(range(n_channels))]),
            cfg.get("rhythm.average_group_names", ["All channels"]),
            n_channels,
        )

        (
            feature_state_counts,
            feature_state_fractions,
            feature_state_labels,
            feature_state_valid_epoch_count,
        ) = _compute_feature_window_state_membership(
            feature_time_s,
            state_time_s,
            state_labels,
            state_analysis_valid_mask,
            feature_window_s,
        )

        total_extent_s = float(h5f["time"].attrs.get("lfp_time_s_extent", [0.0, 86400.0])[1])
        n_hours = int(np.ceil(total_extent_s / hour_bin_s))
        hour_edges = np.arange(0, n_hours + 1, dtype=np.float64) * hour_bin_s
        hour_centers = hour_edges[:-1] + hour_bin_s / 2.0
        if has_lfp_sample_qc:
            hourly_lfp_coverage_fraction = _window_fraction_from_sample_mask(
                lfp_coverage_mask,
                hour_centers,
                hour_bin_s,
                lfp_fs,
            ).astype(np.float32)
            hourly_lfp_valid_fraction = _window_fraction_from_sample_mask(
                lfp_valid_sample_mask,
                hour_centers,
                hour_bin_s,
                lfp_fs,
            ).astype(np.float32)
        else:
            hourly_lfp_coverage_fraction = np.ones((n_hours,), dtype=np.float32)
            hourly_lfp_valid_fraction = np.ones((n_hours,), dtype=np.float32)

        n_occ_states = len(OCCUPANCY_STATE_NAMES)
        n_analysis_states = len(ANALYSIS_STATE_NAMES)
        hour_valid_mask = (
            (hourly_lfp_coverage_fraction >= valid_fraction_threshold)
            & (hourly_lfp_valid_fraction >= valid_fraction_threshold)
        )
        hourly_feature = np.full((n_hours, n_channels, n_bands), np.nan, dtype=np.float32)
        hourly_count = np.zeros((n_hours, n_channels, n_bands), dtype=np.int32)
        hourly_band_daily_reference = np.full((n_channels, n_bands), np.nan, dtype=np.float32)
        hourly_feature_normalized = np.full((n_hours, n_channels, n_bands), np.nan, dtype=np.float32)
        state_occupancy = np.full((n_hours, n_occ_states), np.nan, dtype=np.float32)
        state_epoch_count = np.zeros((n_hours, n_occ_states), dtype=np.int32)
        state_stratified = np.full(
            (n_hours, n_analysis_states, n_channels, n_bands),
            np.nan,
            dtype=np.float32,
        )
        state_stratified_count = np.zeros((n_hours, n_analysis_states, n_channels, n_bands), dtype=np.int32)
        state_band_daily_reference = np.full((n_analysis_states, n_channels, n_bands), np.nan, dtype=np.float32)
        state_stratified_normalized = np.full(
            (n_hours, n_analysis_states, n_channels, n_bands),
            np.nan,
            dtype=np.float32,
        )

        for hour_idx in range(n_hours):
            start = hour_edges[hour_idx]
            end = hour_edges[hour_idx + 1]
            if not hour_valid_mask[hour_idx]:
                continue

            feature_mask = (feature_time_s >= start) & (feature_time_s < end) & feature_analysis_valid_mask
            if np.any(feature_mask):
                hour_data = band_power[feature_mask]
                if np.isfinite(hour_data).any():
                    hourly_feature[hour_idx] = _safe_nanmean(hour_data, axis=0).astype(np.float32)
                hourly_count[hour_idx] = np.sum(np.isfinite(hour_data), axis=0).astype(np.int32)

            epoch_mask = (state_time_s >= start) & (state_time_s < end) & state_analysis_valid_mask
            total_valid_epochs = int(np.sum(epoch_mask))
            for occ_idx, state_name in enumerate(OCCUPANCY_STATE_NAMES):
                code = STATE_CODES[state_name]
                count = int(np.sum(epoch_mask & (state_labels == code)))
                state_epoch_count[hour_idx, occ_idx] = count
                if total_valid_epochs > 0:
                    state_occupancy[hour_idx, occ_idx] = count / total_valid_epochs

            for analysis_idx, state_name in enumerate(ANALYSIS_STATE_NAMES):
                code = STATE_CODES[state_name]
                if not np.any(feature_mask):
                    continue
                hour_data = band_power[feature_mask]
                hour_state_count = feature_state_counts[feature_mask, analysis_idx].astype(np.int32)
                positive = hour_state_count > 0
                if not np.any(positive):
                    continue
                repeated_sum = np.nansum(
                    hour_data[positive] * hour_state_count[positive, np.newaxis, np.newaxis].astype(np.float64),
                    axis=0,
                )
                repeated_count = np.nansum(
                    np.isfinite(hour_data[positive]).astype(np.int32)
                    * hour_state_count[positive, np.newaxis, np.newaxis],
                    axis=0,
                ).astype(np.int32)
                state_stratified_count[hour_idx, analysis_idx] = repeated_count
                state_stratified[hour_idx, analysis_idx] = np.divide(
                    repeated_sum,
                    repeated_count.astype(np.float64),
                    out=np.full((n_channels, n_bands), np.nan, dtype=np.float64),
                    where=repeated_count > 0,
                ).astype(np.float32)

        hourly_weighted_sum = np.nansum(
            hourly_feature.astype(np.float64) * hourly_count.astype(np.float64),
            axis=0,
        )
        hourly_total_count = np.sum(hourly_count.astype(np.float64), axis=0)
        hourly_band_daily_reference = np.divide(
            hourly_weighted_sum,
            hourly_total_count,
            out=np.full((n_channels, n_bands), np.nan, dtype=np.float64),
            where=hourly_total_count > 0,
        ).astype(np.float32)
        hourly_feature_normalized = np.divide(
            hourly_feature.astype(np.float64),
            hourly_band_daily_reference[np.newaxis, :, :].astype(np.float64),
            out=np.full((n_hours, n_channels, n_bands), np.nan, dtype=np.float64),
            where=np.isfinite(hourly_band_daily_reference[np.newaxis, :, :])
            & (hourly_band_daily_reference[np.newaxis, :, :] > 0),
        ).astype(np.float32)

        if do_state_norm:
            weighted_sum = np.nansum(
                state_stratified.astype(np.float64) * state_stratified_count.astype(np.float64),
                axis=0,
            )
            total_count = np.sum(state_stratified_count.astype(np.float64), axis=0)
            state_band_daily_reference = np.divide(
                weighted_sum,
                total_count,
                out=np.full((n_analysis_states, n_channels, n_bands), np.nan, dtype=np.float64),
                where=total_count > 0,
            ).astype(np.float32)
            state_stratified_normalized = np.divide(
                state_stratified.astype(np.float64),
                state_band_daily_reference[np.newaxis, :, :, :].astype(np.float64),
                out=np.full((n_hours, n_analysis_states, n_channels, n_bands), np.nan, dtype=np.float64),
                where=np.isfinite(state_band_daily_reference[np.newaxis, :, :, :])
                & (state_band_daily_reference[np.newaxis, :, :, :] > 0),
            ).astype(np.float32)

        rhythm_grp = h5f["rhythm"]
        _replace_dataset(rhythm_grp, "hour_valid_mask", data=hour_valid_mask.astype(bool), compression="gzip")
        _replace_dataset(
            rhythm_grp,
            "hourly_lfp_coverage_fraction",
            data=hourly_lfp_coverage_fraction,
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "hourly_lfp_valid_fraction",
            data=hourly_lfp_valid_fraction,
            compression="gzip",
        )
        _replace_dataset(rhythm_grp, "hourly_feature_table", data=hourly_feature, compression="gzip")
        _replace_dataset(rhythm_grp, "hourly_feature_count", data=hourly_count, compression="gzip")
        _replace_dataset(rhythm_grp, "hourly_band_daily_reference", data=hourly_band_daily_reference, compression="gzip")
        _replace_dataset(rhythm_grp, "hourly_feature_table_normalized", data=hourly_feature_normalized, compression="gzip")
        _replace_dataset(rhythm_grp, "state_occupancy_table", data=state_occupancy, compression="gzip")
        _replace_dataset(rhythm_grp, "state_epoch_count", data=state_epoch_count, compression="gzip")
        _replace_dataset(rhythm_grp, "state_stratified_feature_table", data=state_stratified, compression="gzip")
        _replace_dataset(rhythm_grp, "state_stratified_count", data=state_stratified_count, compression="gzip")
        _replace_dataset(rhythm_grp, "state_band_daily_reference", data=state_band_daily_reference, compression="gzip")
        _replace_dataset(
            rhythm_grp,
            "state_stratified_feature_table_normalized",
            data=state_stratified_normalized,
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "feature_lfp_coverage_fraction",
            data=feature_coverage_fraction.astype(np.float32),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "feature_lfp_valid_fraction",
            data=feature_lfp_valid_fraction.astype(np.float32),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "feature_analysis_valid_mask",
            data=feature_analysis_valid_mask.astype(bool),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "state_lfp_coverage_fraction",
            data=state_coverage_fraction.astype(np.float32),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "state_lfp_valid_fraction",
            data=state_lfp_valid_fraction.astype(np.float32),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "state_analysis_valid_mask",
            data=state_analysis_valid_mask.astype(bool),
            compression="gzip",
        )
        _replace_dataset(rhythm_grp, "band_names", data=np.asarray(band_names, dtype=h5py.string_dtype(encoding="utf-8")))
        _replace_dataset(rhythm_grp, "state_names", data=np.asarray(ANALYSIS_STATE_NAMES, dtype=h5py.string_dtype(encoding="utf-8")))
        _replace_dataset(
            rhythm_grp,
            "state_names_occupancy",
            data=np.asarray(OCCUPANCY_STATE_NAMES, dtype=h5py.string_dtype(encoding="utf-8")),
        )
        _replace_dataset(
            rhythm_grp,
            "feature_state_count",
            data=feature_state_counts.astype(np.int32),
            compression="gzip",
        )
        _replace_dataset(
            rhythm_grp,
            "feature_state_fraction",
            data=feature_state_fractions.astype(np.float32),
            compression="gzip",
        )
        _replace_dataset(rhythm_grp, "feature_state_label", data=feature_state_labels.astype(np.int16), compression="gzip")
        _replace_dataset(
            rhythm_grp,
            "feature_state_valid_epoch_count",
            data=feature_state_valid_epoch_count.astype(np.int32),
            compression="gzip",
        )
        rhythm_grp.attrs["analysis_channel_axis"] = "channel_0_to_15_probe_shallow_to_deep"
        rhythm_grp.attrs["hourly_reference_normalization_method"] = "divide_by_daily_band_mean"
        rhythm_grp.attrs["state_reference_normalization_method"] = state_norm_method
        rhythm_grp.attrs["coverage_valid_fraction_threshold"] = float(valid_fraction_threshold)
        rhythm_grp.attrs["coverage_filter_method"] = "exclude_hours_features_and_state_epochs_without_lfp_file_coverage_or_valid_lfp_samples"
        rhythm_grp.attrs["feature_window_state_assignment_method"] = "mean_over_10s_state_bins_within_feature_window"
        rhythm_grp.attrs["average_channel_groups_json"] = json.dumps(average_channel_groups)
        rhythm_grp.attrs["average_group_names_json"] = json.dumps(average_group_names, ensure_ascii=False)

        time_grp = h5f["time"]
        _replace_dataset(time_grp, "hourly_time_s", data=hour_centers.astype(np.float32))
        _replace_dataset(rhythm_grp, "hour_edges_s", data=hour_edges.astype(np.float32))

    return h5_path
