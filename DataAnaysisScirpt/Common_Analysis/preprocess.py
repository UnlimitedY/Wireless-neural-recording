import numpy as np
from scipy import signal


def common_median_reference(lfp_matrix):
    """
    Computes and subtracts the common median from all channels.
    Ignores NaNs in computation.
    """
    # lfp_matrix: (channels, samples)
    if lfp_matrix is None or lfp_matrix.shape[0] == 0:
        return lfp_matrix
        
    with np.errstate(invalid='ignore'):
        # Calculate median across channels for each sample (ignoring NaNs)
        median_ref = np.nanmedian(lfp_matrix, axis=0)
        
    # Subtract median from each channel
    referenced = lfp_matrix - median_ref[np.newaxis, :]
    return referenced


def adjacent_bipolar_reference(lfp_matrix, edge_mode="repeat_last_pair", channel_groups=None):
    """
    Apply adjacent-channel bipolar rereferencing along probe depth.

    The output keeps the same channel count as the input and applies bipolar rereference
    independently within each provided channel group.
    """
    data = np.asarray(lfp_matrix, dtype=np.float64)
    if data.ndim != 2 or data.shape[0] == 0:
        return data
    if data.shape[0] == 1:
        return np.array(data, copy=True)

    reref = np.full_like(data, np.nan, dtype=np.float64)
    n_channels = data.shape[0]
    assigned = set()
    normalized_groups = []
    for group in channel_groups or []:
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

    for group in normalized_groups:
        if len(group) == 1:
            reref[group[0]] = data[group[0]]
            continue
        for pos, ch_idx in enumerate(group[:-1]):
            reref[ch_idx] = data[ch_idx] - data[group[pos + 1]]
        last_idx = group[-1]
        if edge_mode == "nan":
            reref[last_idx] = np.nan
        else:
            reref[last_idx] = data[last_idx] - data[group[-2]]
    return reref

def get_valid_fraction(mask_array, window_idx_start, window_idx_end):
    """
    Given a missing_mask where True = missing, returns the fraction of valid (False) data.
    """
    window = mask_array[window_idx_start:window_idx_end]
    if len(window) == 0:
        return 0.0
    valid_count = np.sum(~window)
    return valid_count / len(window)


def interpolate_short_nan_gaps_1d(data, max_gap_samples):
    array = np.asarray(data, dtype=np.float64).copy()
    if max_gap_samples <= 0:
        return array
    isnan = np.isnan(array)
    if not np.any(isnan):
        return array
    n = array.size
    idx = 0
    while idx < n:
        if not isnan[idx]:
            idx += 1
            continue
        start = idx
        while idx < n and isnan[idx]:
            idx += 1
        end = idx
        gap_len = end - start
        if gap_len > max_gap_samples:
            continue
        left = start - 1
        right = end
        if left < 0 or right >= n or np.isnan(array[left]) or np.isnan(array[right]):
            continue
        interp_x = np.array([left, right], dtype=np.float64)
        interp_y = np.array([array[left], array[right]], dtype=np.float64)
        fill_x = np.arange(start, end, dtype=np.float64)
        array[start:end] = np.interp(fill_x, interp_x, interp_y)
    return array


def interpolate_short_nan_gaps_matrix(matrix, max_gap_samples):
    data = np.asarray(matrix, dtype=np.float64)
    out = np.empty_like(data)
    for ch in range(data.shape[0]):
        out[ch] = interpolate_short_nan_gaps_1d(data[ch], max_gap_samples)
    return out


def apply_notch_filter(lfp_matrix, fs, notch_freq_hz, quality_factor=30.0):
    if notch_freq_hz <= 0 or fs <= 0 or notch_freq_hz >= (fs / 2.0):
        return np.asarray(lfp_matrix, dtype=np.float64)
    b, a = signal.iirnotch(notch_freq_hz, quality_factor, fs=fs)
    data = np.asarray(lfp_matrix, dtype=np.float64)
    out = np.empty_like(data)
    for ch in range(data.shape[0]):
        channel = data[ch].copy()
        nan_mask = np.isnan(channel)
        if np.all(nan_mask):
            out[ch] = channel
            continue
        if np.any(nan_mask):
            valid = ~nan_mask
            x = np.arange(channel.size)
            channel[nan_mask] = np.interp(x[nan_mask], x[valid], channel[valid])
        out[ch] = signal.filtfilt(b, a, channel)
        out[ch][nan_mask] = np.nan
    return out


def apply_lowpass_filter(lfp_matrix, fs, cutoff_hz, order=4):
    if cutoff_hz <= 0 or fs <= 0 or cutoff_hz >= (fs / 2.0):
        return np.asarray(lfp_matrix, dtype=np.float64)
    sos = signal.butter(int(order), float(cutoff_hz), btype="lowpass", fs=float(fs), output="sos")
    data = np.asarray(lfp_matrix, dtype=np.float64)
    out = np.empty_like(data)
    for ch in range(data.shape[0]):
        channel = data[ch].copy()
        nan_mask = np.isnan(channel)
        if np.all(nan_mask):
            out[ch] = channel
            continue
        if np.any(nan_mask):
            valid = ~nan_mask
            x = np.arange(channel.size)
            channel[nan_mask] = np.interp(x[nan_mask], x[valid], channel[valid])
        out[ch] = signal.sosfiltfilt(sos, channel)
        out[ch][nan_mask] = np.nan
    return out


def clean_data(data, threshold, replacement_val, method="fixed", window_size=10):
    """
    Replace dropped samples (data <= threshold) with a consistent policy.
    """
    data = np.asarray(data)
    mask = data <= threshold
    loss_count = int(np.sum(mask))
    if loss_count == 0:
        return data, 0

    cleaned_data = data.copy()
    if method == "fixed":
        cleaned_data[mask] = replacement_val
    elif method == "previous":
        valid_indices = np.where(~mask)[0]
        if len(valid_indices) == 0:
            cleaned_data[:] = replacement_val
        else:
            idx_all = np.arange(len(data))
            pos = np.searchsorted(valid_indices, idx_all, side="right") - 1
            valid_pos_mask = pos >= 0
            prev_valid_indices = valid_indices[pos[valid_pos_mask]]
            cleaned_data[valid_pos_mask] = data[prev_valid_indices]
            cleaned_data[~valid_pos_mask] = replacement_val
    elif method == "mean":
        half_win = int(window_size) // 2
        bad_indices = np.where(mask)[0]
        n_samples = len(data)
        for idx in bad_indices:
            start = max(0, idx - half_win)
            end = min(n_samples, idx + half_win + 1)
            valid_in_window = data[start:end][~mask[start:end]]
            cleaned_data[idx] = (
                float(np.mean(valid_in_window)) if len(valid_in_window) > 0 else replacement_val
            )
    else:
        raise ValueError(f"Unknown clean_data method: {method}")

    return cleaned_data, loss_count


def clean_data_linear(data, threshold=-1000):
    """
    Linearly interpolate dropped samples. Used by trial-level alignment paths.
    """
    data = np.asarray(data)
    mask = data <= threshold
    if not np.any(mask):
        return data
    cleaned = np.copy(data)
    valid_mask = ~mask
    x = np.arange(len(data))
    if np.any(valid_mask):
        cleaned[mask] = np.interp(x[mask], x[valid_mask], data[valid_mask])
    else:
        cleaned[:] = 0
    return cleaned
