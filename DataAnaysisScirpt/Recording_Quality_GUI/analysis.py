import pyedflib
import numpy as np
from scipy import signal
import os
import datetime
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Common_Analysis.preprocess import clean_data as _shared_clean_data
from Common_Analysis.sync_detection import (
    detect_pulse_sequence as _shared_detect_pulse_sequence,
    detect_pulse_sequence_energy as _shared_detect_pulse_sequence_energy,
    detect_pulse_sequence_from_envelope as _shared_detect_pulse_sequence_from_envelope,
    get_pulse_band_hz as _shared_get_pulse_band_hz,
    match_alignment_to_neural as _shared_match_alignment_to_neural,
)

def clean_data(data, threshold, replacement_val, method='fixed', window_size=10):
    return _shared_clean_data(data, threshold, replacement_val, method, window_size)

def _legacy_clean_data(data, threshold, replacement_val, method='fixed', window_size=10):
    """
    Replaces values <= threshold with a value determined by `method`.
    
    Args:
        data (np.array): Signal data.
        threshold (float): Cutoff for identifying lost data.
        replacement_val (float): Value to use if method='fixed'.
        method (str): 'fixed', 'previous', or 'mean'.
        window_size (int): Size of window for 'mean' method (total samples around point).
        
    Returns:
        tuple: (cleaned_data, loss_count)
    """
    mask = data <= threshold
    loss_count = np.sum(mask)
    
    if loss_count == 0:
        return data, 0
        
    cleaned_data = data.copy()
    
    if method == 'fixed':
        cleaned_data[mask] = replacement_val
        
    elif method == 'previous':
        # Replace with last valid value
        # Forward fill
        # If start is bad, use 0 or first valid? 0 is safer.
        
        # We can use pandas ffill if available, but let's stick to numpy
        # Create an array of indices
        # Find valid indices
        valid_mask = ~mask
        valid_indices = np.where(valid_mask)[0]
        
        if len(valid_indices) == 0:
            # All bad
            cleaned_data[:] = replacement_val # Fallback
        else:
            # For each bad index, find the nearest previous valid index
            # This can be slow in pure python loop.
            # Efficient way:
            # 1. Get indices of all points
            idx = np.arange(len(data))
            # 2. Set bad indices to 0, valid to their index
            # We need to propagate the last valid index forward.
            # np.maximum.accumulate trick?
            
            # Let's simply iterate? For 1M points might be slow.
            # Using a simple loop for just the bad segments is better.
            
            # Actually, a simple forward fill:
            last_valid = replacement_val # Default if starts with bad
            
            # Optimization: only iterate if necessary?
            # Let's try a vectorized approach for forward fill
            # idx of valid data
            # Fill with nearest previous valid index
            
            # Create an array where valid entries hold their index, invalid hold 0
            # Then accumulate max? No, index increases.
            
            # Standard approach:
            # 1. Mask of valid values
            # 2. Indices of valid values
            # 3. Interp? 'previous' interpolation
            
            # x_valid = valid_indices
            # y_valid = data[valid_indices]
            # x_all = np.arange(len(data))
            
            # interp1d with kind='previous' or 'zero' (step function)
            # numpy.interp is linear.
            # We can use searchsorted to find the index of the previous valid point.
            
            idx_all = np.arange(len(data))
            # Find the insertion point of all indices into valid_indices
            # side='right' gives the index in valid_indices such that valid_indices[i-1] <= val < valid_indices[i]
            # So index-1 is the previous valid index.
            
            pos = np.searchsorted(valid_indices, idx_all, side='right') - 1
            
            # Handle start (pos == -1) -> Use replacement_val or first valid?
            # "Previous" implies before. If none before, use fixed replacement.
            
            # Map pos to data
            # Any pos == -1 is start bad data
            valid_pos_mask = pos >= 0
            
            # Where we have a previous valid index:
            prev_valid_indices = valid_indices[pos[valid_pos_mask]]
            cleaned_data[valid_pos_mask] = data[prev_valid_indices]
            
            # Where we don't (start of file is bad):
            cleaned_data[~valid_pos_mask] = replacement_val
            
    elif method == 'mean':
        # Replace with mean of valid data in window [i-w, i+w]
        # This is complex to do efficiently for random gaps.
        # Window size is "total samples". Let's assume symmetric +/- window_size/2.
        
        half_win = window_size // 2
        
        # We can iterate only over the bad indices?
        bad_indices = np.where(mask)[0]
        
        # If too many bad points, this loop is slow.
        # But usually loss is sparse.
        
        # Optimization:
        # If we have long gaps, the "mean of valid neighbors" might be far away.
        # "Window size can be regulated" -> User sets N.
        # We look at [i - N, i + N] (or just N total?). Let's say +/- N.
        # If window has no valid data? Fallback to replacement_val.
        
        N = len(data)
        
        for i in bad_indices:
            start = max(0, i - half_win)
            end = min(N, i + half_win + 1)
            
            window_data = data[start:end]
            # Check valid in window (original data)
            # We should use original data to determine valid neighbors?
            # "mean of valid points"
            win_mask = mask[start:end]
            valid_in_win = window_data[~win_mask]
            
            if len(valid_in_win) > 0:
                cleaned_data[i] = np.mean(valid_in_win)
            else:
                cleaned_data[i] = replacement_val

    return cleaned_data, loss_count

def calculate_data_loss(file_path, threshold_map, default_threshold=-1000, replacement_value=0, method='fixed', window_size=10):
    """
    Calculates the data loss rate for each channel in an EDF file.
    Data loss is defined as values <= a certain threshold.
    Also calculates CMR-based RMS for LFP channels using CLEANED data.
    
    Args:
        file_path (str): Path to the EDF file.
        threshold_map (dict): Dictionary mapping channel names to thresholds.
        default_threshold (float): Default threshold if channel not in map.
        replacement_value (float): Value to replace lost data with.
        method (str): Replacement method.
        window_size (int): Window size for 'mean' method.
        
    Returns:
        dict: A dictionary mapping channel names to data loss rate and other stats.
    """
    results = {}
    lfp_data = [] # List of (channel_name, data_array)
    lfp_indices = []
    
    try:
        f = pyedflib.EdfReader(file_path)
        channel_labels = f.getSignalLabels()
        n_samples = f.getNSamples()[0]
        
        # First pass: Read data, calculate loss, and collect CLEANED LFP data
        for i, label in enumerate(channel_labels):
            clean_label = label.strip()
            threshold = threshold_map.get(clean_label, default_threshold)
            
            data = f.readSignal(i)
            
            # Count values <= threshold
            min_val = np.min(data) if len(data) > 0 else 0
            max_val = np.max(data) if len(data) > 0 else 0
            
            # Clean Data
            cleaned_data, loss_count = clean_data(data, threshold, replacement_value, method, window_size)
            
            loss_rate = loss_count / len(data) if len(data) > 0 else 0
            
            results[clean_label] = {
                'loss_rate': loss_rate,
                'threshold': threshold,
                'loss_count': loss_count,
                'total_samples': len(data),
                'min_val': min_val,
                'max_val': max_val,
                'rms': 0.0 # Default RMS
            }
            
            # Check for Battery Status Channel
            # User says "battery_STAT", "Bq25176_PG_STAT" (battery charging state), "PG" (power good state)
            # The logic:
            # 0 -> PG=0, PG_STAT=0
            # 1 -> PG=1, PG_STAT=0
            # 256 -> PG=0, PG_STAT=1
            # 257 -> PG=1, PG_STAT=1
            
            if 'STAT' in clean_label or 'Charging' in clean_label:
                # We need to analyze this channel for charging stats
                # Valid data only (mask was computed in clean_data but not returned fully)
                # Let's re-compute mask for stats
                mask = data <= threshold
                valid_data = data[~mask]
                
                if len(valid_data) > 0:
                    # Round to nearest integer to handle float precision issues in EDF
                    valid_data_int = np.round(valid_data).astype(int)
                    
                    # New Logic:
                    # PG=0 means Power Good (Active)
                    # PG_STAT=0 means Charging (Active)
                    
                    # From previous context:
                    # 0   -> PG=0, PG_STAT=0  => Power Good YES, Charging YES
                    # 1   -> PG=1, PG_STAT=0  => Power Good NO,  Charging YES (Error state?)
                    # 256 -> PG=0, PG_STAT=1  => Power Good YES, Charging NO
                    # 257 -> PG=1, PG_STAT=1  => Power Good NO,  Charging NO
                    
                    # Count PG Active (0 or 256)
                    pg_active_mask = (valid_data_int == 0) | (valid_data_int == 256)
                    pg_ratio = np.sum(pg_active_mask) / len(valid_data)
                    
                    # Count Charging Active (0 or 1)
                    charging_active_mask = (valid_data_int == 0) | (valid_data_int == 1)
                    charging_ratio = np.sum(charging_active_mask) / len(valid_data)
                    
                    results[clean_label]['charging_stats'] = {
                        'pg_ratio': pg_ratio,
                        'charging_ratio': charging_ratio,
                        'valid_samples': len(valid_data)
                    }
            
            # Check for RSOC Channel
            if 'RSOC' in clean_label:
                # Calculate Start and End values (averaged)
                # Valid data only
                mask = data <= threshold
                valid_data = data[~mask]
                # print(f"Valid data for {clean_label}: {valid_data}")
                
                # Trim trailing zeros (EDF padding)
                # Assuming padding is exactly 0 or very close
                # Iterate from end backwards
                if len(valid_data) > 0:
                    trim_idx = len(valid_data)
                    for k in range(len(valid_data)-1, -1, -1):
                        if abs(valid_data[k]) > 1e-1: # Use a small epsilon
                            trim_idx = k + 1
                            break
                        else:
                            # It's zero, continue trimming
                            pass
                            
                    trimmed_data = valid_data[:trim_idx]
                else:
                    trimmed_data = valid_data
                
                if len(trimmed_data) > 0:
                    # Use 10% of data for averaging
                    n_avg = max(1, int(len(trimmed_data) * 0.10))
                    
                    rsoc_start = np.mean(trimmed_data[:n_avg])
                    rsoc_end = np.mean(trimmed_data[-n_avg:])
                    
                    results[clean_label]['rsoc_stats'] = {
                        'start': rsoc_start,
                        'end': rsoc_end,
                        'n_avg': n_avg
                    }
            
            # Check if it's an LFP channel (heuristic: starts with 'Ch' or contains 'LFP' but not 'ESA')
            if 'ESA' not in clean_label and ('Ch' in clean_label or 'LFP' in clean_label):
                lfp_data.append(cleaned_data)
                lfp_indices.append(clean_label)
                
        f.close()
        
        # Second pass: Calculate CMR and RMS for LFP channels (using CLEANED data)
        if lfp_data:
            lfp_matrix = np.array(lfp_data) # Shape (n_channels, n_samples)
            
            # Calculate Common Median Reference
            median_ref = np.median(lfp_matrix, axis=0)
            
            # Calculate RMS for each channel after CMR
            for idx, ch_name in enumerate(lfp_indices):
                referenced_data = lfp_matrix[idx] - median_ref
                rms = np.sqrt(np.mean(referenced_data**2))
                results[ch_name]['rms'] = rms
                
    except Exception as e:
        print(f"Error processing {file_path}: {e}")
        return {}
        
    return results

def get_lfp_spectrum_data(file_path, threshold_map, replacement_val=0, method='fixed', window_size=10):
    """
    Returns CLEANED LFP data for spectrum analysis.
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        fs = f.getSampleFrequency(0)
        
        lfp_data = []
        lfp_labels = []
        
        for i, label in enumerate(labels):
            clean_label = label.strip()
            if 'ESA' not in clean_label and ('Ch' in clean_label or 'LFP' in clean_label):
                threshold = threshold_map.get(clean_label, -1000)
                data = f.readSignal(i)
                cleaned_data, _ = clean_data(data, threshold, replacement_val, method, window_size)
                lfp_data.append(cleaned_data)
                lfp_labels.append(clean_label)
        
        f.close()
        
        if not lfp_data:
            return None, "No LFP channels found"
            
        return {
            'fs': fs,
            'data': np.array(lfp_data),
            'labels': lfp_labels
        }, "Success"
    except Exception as e:
        return None, str(e)

def get_esa_data(file_path, threshold_map, replacement_val=0, method='fixed', window_size=10):
    """
    Returns CLEANED ESA data for visualization.
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        fs = f.getSampleFrequency(0)
        
        esa_data = []
        esa_labels = []
        
        for i, label in enumerate(labels):
            clean_label = label.strip()
            if 'ESA' in clean_label:
                threshold = threshold_map.get(clean_label, -1000)
                data = f.readSignal(i)
                cleaned_data, _ = clean_data(data, threshold, replacement_val, method, window_size)
                esa_data.append(cleaned_data)
                esa_labels.append(clean_label)
        
        f.close()
        
        if not esa_data:
            return None, "No ESA channels found"
            
        return {
            'fs': fs,
            'data': np.array(esa_data),
            'labels': esa_labels
        }, "Success"
    except Exception as e:
        return None, str(e)

def calculate_esa_correlation(data_matrix, labels):
    """
    Calculates correlation matrix for ESA data.
    Ignores invalid data (user said "不考虑数据丢失的点").
    But data is already cleaned/replaced.
    If we want to ignore specific values (like replacement_val or threshold), we need mask.
    Assuming clean_data already handled it.
    If we want to ignore 0s or specific values, we can mask them.
    Standard np.corrcoef uses all data.
    
    Returns: correlation_matrix (N x N), labels
    """
    if data_matrix.shape[1] == 0:
        return np.zeros((data_matrix.shape[0], data_matrix.shape[0])), labels
        
    # Correlation
    # If we need to ignore certain values, it's complex for correlation.
    # User said "不考虑数据丢失的点".
    # If we replaced them with mean/previous, they are "valid" now.
    # If we replaced with fixed value (e.g. 0), they might skew correlation.
    # But clean_data is called before this.
    
    # Simple correlation for now
    corr_matrix = np.corrcoef(data_matrix)
    
    # Handle NaNs if constant data
    corr_matrix = np.nan_to_num(corr_matrix, nan=0.0)
    
    return corr_matrix, labels

def get_esa_baseline(data_matrix, labels):
    """
    Calculates baseline (mean) for each channel.
    """
    baselines = np.mean(data_matrix, axis=1)
    return baselines, labels

def get_raw_highpass_data(file_path, threshold_map, replacement_val=0, cutoff=300.0, method='fixed', window_size=10):
    """
    Returns CLEANED Raw data high-pass filtered at cutoff Hz.
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        
        raw_idx = -1
        for i, label in enumerate(labels):
            if 'RawData' in label:
                raw_idx = i
                break
        
        if raw_idx == -1:
            f.close()
            return None, "No RawData channel found"
            
        fs = f.getSampleFrequency(raw_idx)
        raw_data = f.readSignal(raw_idx)
        f.close()
        
        # Clean Data First
        clean_label = labels[raw_idx].strip()
        threshold = threshold_map.get(clean_label, -1000)
        cleaned_raw, _ = clean_data(raw_data, threshold, replacement_val, method, window_size)
        
        # High-pass Filter
        nyquist = 0.5 * fs
        normal_cutoff = cutoff / nyquist
        b, a = signal.butter(4, normal_cutoff, btype='high', analog=False)
        filtered_data = signal.filtfilt(b, a, cleaned_raw)
        
        # Calculate RMS
        rms = np.sqrt(np.mean(filtered_data**2))
        
        return {
            'fs': fs,
            'original': raw_data, # Return original for comparison? Or cleaned? Usually raw is raw.
            'cleaned': cleaned_raw,
            'filtered': filtered_data,
            'rms': rms
        }, "Success"
        
    except Exception as e:
        return None, str(e)

def get_raw_highpass_rms(file_path, threshold_map, replacement_val=0, cutoff=300.0, method='fixed', window_size=10):
    """
    Returns RMS of HighPass filtered data (optimized for memory).
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        
        raw_idx = -1
        for i, label in enumerate(labels):
            if 'RawData' in label:
                raw_idx = i
                break
        
        if raw_idx == -1:
            f.close()
            return None, "No RawData channel found"
            
        fs = f.getSampleFrequency(raw_idx)
        raw_data = f.readSignal(raw_idx)
        f.close()
        
        # Clean Data First
        clean_label = labels[raw_idx].strip()
        threshold = threshold_map.get(clean_label, -1000)
        cleaned_raw, _ = clean_data(raw_data, threshold, replacement_val, method, window_size)
        
        # High-pass Filter
        nyquist = 0.5 * fs
        normal_cutoff = cutoff / nyquist
        b, a = signal.butter(4, normal_cutoff, btype='high', analog=False)
        filtered_data = signal.filtfilt(b, a, cleaned_raw)
        
        # Calculate RMS
        rms = np.sqrt(np.mean(filtered_data**2))
        
        return rms, "Success"
        
    except Exception as e:
        return None, str(e)

def get_sensor_timeline_data(file_paths, threshold_map, replacement_val=0, method='fixed', window_size=10):
    """
    Reads multiple sensor files and returns aligned data for battery and IMU.
    Returns:
        dict: {
            'battery': list of (timestamp_array, voltage_array, filename),
            'imu': list of (timestamp_array, acc_x, acc_y, acc_z, filename),
            'min_time': float,
            'max_time': float
        }
    """
    battery_data = []
    imu_data = []
    
    min_ts = float('inf')
    max_ts = float('-inf')
    
    for fpath in file_paths:
        try:
            f = pyedflib.EdfReader(fpath)
            start_date = f.getStartdatetime()
            # Convert to timestamp (seconds)
            start_ts = start_date.timestamp()
            
            labels = f.getSignalLabels()
            
            # Find Battery (Prefer RSOC for "level", else Voltage)
            batt_idx = -1
            
            # Check for RSOC first
            for i, label in enumerate(labels):
                if 'RSOC' in label:
                    batt_idx = i
                    break
            
            # Fallback to Voltage if RSOC not found
            if batt_idx == -1:
                for i, label in enumerate(labels):
                    if 'Battery' in label:
                        batt_idx = i
                        break
            
            if batt_idx != -1:
                b_data = f.readSignal(batt_idx)
                
                # Clean Battery Data
                clean_label = labels[batt_idx].strip()
                threshold = threshold_map.get(clean_label, -1000)
                cleaned_b, _ = clean_data(b_data, threshold, replacement_val, method, window_size)
                
                fs_b = f.getSampleFrequency(batt_idx)
                t_b = start_ts + np.arange(len(cleaned_b)) / fs_b
                battery_data.append((t_b, cleaned_b, os.path.basename(fpath)))
                
                if len(t_b) > 0:
                    if t_b[0] < min_ts: min_ts = t_b[0]
                    if t_b[-1] > max_ts: max_ts = t_b[-1]
            
            # Find IMU (Acc X, Y, Z)
            acc_indices = []
            acc_labels = ['ACClX', 'ACClY', 'ACClZ']
            
            # Map labels
            idx_map = {}
            for i, label in enumerate(labels):
                for axis in acc_labels:
                    if axis.lower() in label.lower():
                        idx_map[axis] = i
                        break
            
            if len(idx_map) == 3:
                # Read 3 axes
                # Assuming same FS
                idx_x = idx_map['ACClX']
                idx_y = idx_map['ACClY']
                idx_z = idx_map['ACClZ']
                
                fs_imu = f.getSampleFrequency(idx_x)
                ax = f.readSignal(idx_x)
                ay = f.readSignal(idx_y)
                az = f.readSignal(idx_z)
                
                # Clean IMU Data
                # X
                label_x = labels[idx_x].strip()
                thresh_x = threshold_map.get(label_x, -1000)
                ax_clean, _ = clean_data(ax, thresh_x, replacement_val, method, window_size)
                
                # Y
                label_y = labels[idx_y].strip()
                thresh_y = threshold_map.get(label_y, -1000)
                ay_clean, _ = clean_data(ay, thresh_y, replacement_val, method, window_size)
                
                # Z
                label_z = labels[idx_z].strip()
                thresh_z = threshold_map.get(label_z, -1000)
                az_clean, _ = clean_data(az, thresh_z, replacement_val, method, window_size)
                
                t_imu = start_ts + np.arange(len(ax_clean)) / fs_imu
                imu_data.append((t_imu, ax_clean, ay_clean, az_clean, os.path.basename(fpath)))
                
                if len(t_imu) > 0:
                    if t_imu[0] < min_ts: min_ts = t_imu[0]
                    if t_imu[-1] > max_ts: max_ts = t_imu[-1]
                    
            f.close()
            
        except Exception as e:
            print(f"Error reading sensor file {fpath}: {e}")
            continue
            
    if min_ts == float('inf'):
        return None
        
    return {
        'battery': battery_data,
        'imu': imu_data,
        'min_time': min_ts,
        'max_time': max_ts
    }
def _get_pulse_band_hz(pulse_freq_hz, bandwidth_hz=1000.0):
    return _shared_get_pulse_band_hz(pulse_freq_hz, bandwidth_hz)

def _legacy_get_pulse_band_hz(pulse_freq_hz, bandwidth_hz=1000.0):
    """
    Returns a symmetric band around pulse frequency.
    For example, 5kHz with 1kHz bandwidth -> [4.5kHz, 5.5kHz].
    """
    center = float(pulse_freq_hz)
    half_bw = float(bandwidth_hz) / 2.0
    low = max(1.0, center - half_bw)
    high = center + half_bw
    return low, high


def _match_alignment_to_neural(detected_indices, correlation_vals, rising_edges, fs, window_ms=1200.0):
    return _shared_match_alignment_to_neural(
        detected_indices,
        correlation_vals,
        rising_edges,
        fs,
        window_ms=window_ms,
    )

def _legacy_match_alignment_to_neural(detected_indices, correlation_vals, rising_edges, fs, window_ms=1200.0):
    """
    Matches alignment pulses with detected neural pulses.
    Returns:
        tuple: (alignment_map, delay_ms_map)
            alignment_map: {alignment_index_in_rising_edges: neural_sample_index}
            delay_ms_map: {alignment_index_in_rising_edges: delay_ms}
    """
    window_samples = int((window_ms / 1000.0) * fs)
    alignment_map = {}
    delay_ms_map = {}

    for i, align_idx_val in enumerate(rising_edges):
        win_end = align_idx_val
        win_start = align_idx_val - window_samples

        mask = (detected_indices >= win_start) & (detected_indices <= win_end)
        candidates = detected_indices[mask]

        if len(candidates) > 0:
            cand_corrs = correlation_vals[candidates]
            best_local_idx = np.argmax(cand_corrs)
            best_neural_idx = candidates[best_local_idx]
            alignment_map[i] = best_neural_idx
            delay_ms_map[i] = (align_idx_val - best_neural_idx) * 1000.0 / fs

    return alignment_map, delay_ms_map


def _detect_pulse_sequence_from_envelope(envelope, fs, symmetry_ratio=0.4):
    return _shared_detect_pulse_sequence_from_envelope(
        envelope,
        fs,
        symmetry_ratio=symmetry_ratio,
    )

def _legacy_detect_pulse_sequence_from_envelope(envelope, fs, symmetry_ratio=0.4):
    """
    Generic detector for 1ms high -> 1ms low -> 1ms high pattern using an energy/envelope signal.
    """
    if envelope is None or len(envelope) == 0:
        return np.array([]), np.array([])

    n_samples_1ms = int(fs * 0.001)
    if n_samples_1ms < 1:
        return np.array([]), np.array([])

    template = np.concatenate([
        np.ones(n_samples_1ms),
        np.zeros(n_samples_1ms),
        np.ones(n_samples_1ms)
    ])
    template_sum = np.sum(template)
    if template_sum > 0:
        template = template / template_sum

    correlation = signal.correlate(envelope, template, mode='same')
    if len(correlation) == 0:
        return np.array([]), np.array([])

    mean_corr = np.mean(correlation)
    std_corr = np.std(correlation)
    thresholds = [mean_corr + 4 * std_corr, mean_corr + 3 * std_corr, mean_corr + 2 * std_corr]
    distance = n_samples_1ms * 3

    peaks = []
    for thr in thresholds:
        peaks, _ = signal.find_peaks(correlation, height=thr, distance=distance)
        if len(peaks) > 0:
            break

    if len(peaks) == 0:
        return np.array([]), correlation

    valid_peaks = []
    half_len = (3 * n_samples_1ms) // 2

    for p in peaks:
        start_idx = p - half_len
        if start_idx < 0 or start_idx + 3 * n_samples_1ms > len(envelope):
            continue

        left_segment = envelope[start_idx : start_idx + n_samples_1ms]
        right_segment = envelope[start_idx + 2 * n_samples_1ms : start_idx + 3 * n_samples_1ms]
        if len(left_segment) == 0 or len(right_segment) == 0:
            continue

        e_left = np.mean(left_segment)
        e_right = np.mean(right_segment)
        if e_left == 0 and e_right == 0:
            continue

        ratio = min(e_left, e_right) / (max(e_left, e_right) + 1e-9)
        if ratio > symmetry_ratio:
            valid_peaks.append(p)

    return np.array(valid_peaks), correlation


def detect_pulse_sequence(signal_data, fs, pulse_freq_hz=5000.0):
    return _shared_detect_pulse_sequence(signal_data, fs, pulse_freq_hz=pulse_freq_hz)

def _legacy_detect_pulse_sequence(signal_data, fs, pulse_freq_hz=5000.0):
    """
    Detects a sequence of: 1ms pulse -> 1ms quiet -> 1ms pulse.
    Returns the indices of the midpoints of the detected sequences.
    """
    if signal_data is None or len(signal_data) == 0:
        return np.array([]), np.array([])
        
    # 1. Compute Amplitude Envelope
    try:
        analytic_signal = signal.hilbert(signal_data)
        amplitude_envelope = np.abs(analytic_signal)
    except Exception:
        # Fallback if hilbert fails (e.g. data too short?)
        amplitude_envelope = np.abs(signal_data)

    return _detect_pulse_sequence_from_envelope(amplitude_envelope, fs, symmetry_ratio=0.4)


def detect_pulse_sequence_energy(signal_data, fs):
    return _shared_detect_pulse_sequence_energy(signal_data, fs)

def _legacy_detect_pulse_sequence_energy(signal_data, fs):
    """
    Full-band energy based detector (harmonics-friendly).
    Uses short-time energy (1ms moving average of squared signal), then
    detects 1ms high -> 1ms low -> 1ms high pattern.
    """
    if signal_data is None or len(signal_data) == 0:
        return np.array([]), np.array([])

    n_samples_1ms = int(fs * 0.001)
    if n_samples_1ms < 1:
        return np.array([]), np.array([])

    x = signal_data - np.median(signal_data)
    nyquist = 0.5 * fs

    # Remove very low-frequency baseline so energy is dominated by transient pulse content.
    if nyquist > 301.0:
        hp_hz = 300.0
        b_hp, a_hp = signal.butter(2, hp_hz, btype='high', fs=fs)
        x = signal.filtfilt(b_hp, a_hp, x)

    inst_power = x * x
    kernel = np.ones(n_samples_1ms, dtype=float) / max(1, n_samples_1ms)
    energy_envelope = np.convolve(inst_power, kernel, mode='same')

    return _detect_pulse_sequence_from_envelope(energy_envelope, fs, symmetry_ratio=0.45)

def detect_sync_success_rate(file_path, pulse_freq_hz=5000.0, method='band'):
    """
    Calculates the success rate of sync pulse detection for 'raw' EDF files.
    
    Returns:
        tuple: (success_rate (float), num_matched (int), total_pulses (int), avg_delay_ms (float|None), message (str))
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        
        # Check if it's a raw file (heuristic: has 'RawData' and 'Alignment')
        # Labels might vary slightly, check loosely
        raw_idx = -1
        align_idx = -1
        
        for i, label in enumerate(labels):
            if 'RawData' in label:
                raw_idx = i
            elif 'Alignment' in label:
                align_idx = i
        
        if raw_idx == -1 or align_idx == -1:
            f.close()
            return 0.0, 0, 0, None, "Not a valid raw file (missing RawData or Alignment)"
            
        fs = f.getSampleFrequency(raw_idx)
        raw_data = f.readSignal(raw_idx)
        align_data = f.readSignal(align_idx)
        f.close()
        
        method = str(method).lower()
        if method == 'energy':
            detected_indices, correlation_vals = detect_pulse_sequence_energy(raw_data, fs)
        else:
            # --- Preprocessing (bandpass around selected pulse frequency) ---
            nyquist = 0.5 * fs
            low, high = _get_pulse_band_hz(pulse_freq_hz)
            if high >= nyquist:
                high = nyquist - 1.0
            if low >= high:
                return 0.0, 0, 0, None, f"Invalid pulse frequency {pulse_freq_hz}Hz for fs={fs}Hz"

            b, a = signal.butter(4, [low, high], btype='band', fs=fs)
            data_filtered = signal.filtfilt(b, a, raw_data)
            detected_indices, correlation_vals = detect_pulse_sequence(
                data_filtered,
                fs,
                pulse_freq_hz=pulse_freq_hz
            )
        
        # --- Detect Alignment Pulses ---
        align_thr = 0.5 * np.max(align_data) if np.max(align_data) > 0 else 0.5
        align_binary = (align_data > align_thr).astype(int)
        rising_edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
        n_alignment = len(rising_edges)
        
        if n_alignment == 0:
            return 0.0, 0, 0, None, "No alignment pulses found"
        
        # --- Match Pulses ---
        alignment_map, delay_ms_map = _match_alignment_to_neural(
            detected_indices,
            correlation_vals,
            rising_edges,
            fs,
            window_ms=1200.0
        )
        
        n_matched = len(alignment_map)
        success_rate = n_matched / n_alignment if n_alignment > 0 else 0
        avg_delay_ms = float(np.mean(list(delay_ms_map.values()))) if delay_ms_map else None
        
        return success_rate, n_matched, n_alignment, avg_delay_ms, "Success"
        
    except Exception as e:
        return 0.0, 0, 0, None, f"Error: {str(e)}"

def get_pulse_detection_details(file_path, pulse_freq_hz=5000.0, method='band'):
    """
    Returns detailed data for pulse detection visualization.
    """
    try:
        f = pyedflib.EdfReader(file_path)
        labels = f.getSignalLabels()
        
        raw_idx = -1
        align_idx = -1
        
        for i, label in enumerate(labels):
            if 'RawData' in label:
                raw_idx = i
            elif 'Alignment' in label:
                align_idx = i
        
        if raw_idx == -1 or align_idx == -1:
            f.close()
            return None, "Not a valid raw file"
            
        fs = f.getSampleFrequency(raw_idx)
        raw_data = f.readSignal(raw_idx)
        align_data = f.readSignal(align_idx)
        f.close()
        
        method = str(method).lower()
        if method == 'energy':
            detected_indices, correlation_vals = detect_pulse_sequence_energy(raw_data, fs)
            # For plotting in current UI, show a smoothed energy envelope as "filtered_data".
            n_samples_1ms = max(1, int(fs * 0.001))
            x = raw_data - np.median(raw_data)
            inst_power = x * x
            kernel = np.ones(n_samples_1ms, dtype=float) / n_samples_1ms
            data_filtered = np.convolve(inst_power, kernel, mode='same')
        else:
            # Bandpass around selected pulse frequency
            nyquist = 0.5 * fs
            low, high = _get_pulse_band_hz(pulse_freq_hz)
            if high >= nyquist:
                high = nyquist - 1.0
            if low >= high:
                return None, f"Invalid pulse frequency {pulse_freq_hz}Hz for fs={fs}Hz"
            b, a = signal.butter(4, [low, high], btype='band', fs=fs)
            data_filtered = signal.filtfilt(b, a, raw_data)
            detected_indices, correlation_vals = detect_pulse_sequence(
                data_filtered,
                fs,
                pulse_freq_hz=pulse_freq_hz
            )
        
        # Detect Alignment
        align_thr = 0.5 * np.max(align_data) if np.max(align_data) > 0 else 0.5
        align_binary = (align_data > align_thr).astype(int)
        rising_edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
        
        # Match
        alignment_map, delay_ms_map = _match_alignment_to_neural(
            detected_indices,
            correlation_vals,
            rising_edges,
            fs,
            window_ms=1200.0
        )
        avg_delay_ms = float(np.mean(list(delay_ms_map.values()))) if delay_ms_map else None
                
        return {
            'fs': fs,
            'raw_data': raw_data, # Original raw data
            'filtered_data': data_filtered,
            'align_data': align_data,
            'neural_indices': detected_indices,
            'align_indices': rising_edges,
            'matches': alignment_map, # index in rising_edges -> value in detected_indices
            'delays_ms': delay_ms_map, # index in rising_edges -> delay(ms)
            'avg_delay_ms': avg_delay_ms,
            'method': method,
            'correlation': correlation_vals
        }, "Success"
        
    except Exception as e:
        return None, str(e)
