import numpy as np
from scipy import signal


def get_pulse_band_hz(pulse_freq_hz, bandwidth_hz=1000.0):
    center = float(pulse_freq_hz)
    half_bw = float(bandwidth_hz) / 2.0
    low = max(1.0, center - half_bw)
    high = center + half_bw
    return low, high


def match_alignment_to_neural(detected_indices, correlation_vals, rising_edges, fs, window_ms=1200.0):
    """
    Match behavior alignment pulses to preceding neural sync pulses.
    """
    detected_indices = np.asarray(detected_indices)
    rising_edges = np.asarray(rising_edges)
    window_samples = int((window_ms / 1000.0) * fs)
    alignment_map = {}
    delay_ms_map = {}

    for align_pos, align_sample in enumerate(rising_edges):
        win_start = align_sample - window_samples
        mask = (detected_indices >= win_start) & (detected_indices <= align_sample)
        candidates = detected_indices[mask]
        if len(candidates) == 0:
            continue

        cand_corrs = correlation_vals[candidates]
        best_local_idx = int(np.argmax(cand_corrs))
        best_neural_idx = int(candidates[best_local_idx])
        alignment_map[align_pos] = best_neural_idx
        delay_ms_map[align_pos] = (align_sample - best_neural_idx) * 1000.0 / fs

    return alignment_map, delay_ms_map


def detect_best_pulse_in_window(envelope, offset_idx, fs, threshold_ratio=0.4):
    """
    Detect the best 1ms high, 1ms low, 1ms high pulse inside one search window.
    """
    if len(envelope) == 0:
        return None, None

    n_samples_1ms = int(fs * 0.001)
    if n_samples_1ms < 1:
        return None, None

    template = np.concatenate([np.ones(n_samples_1ms), np.zeros(n_samples_1ms), np.ones(n_samples_1ms)])
    template = template / np.sum(template)
    correlation = signal.correlate(envelope, template, mode="same")
    threshold = np.mean(correlation) + 2 * np.std(correlation)
    distance = n_samples_1ms * 3
    peaks, _ = signal.find_peaks(correlation, height=threshold, distance=distance)

    best_peak = None
    max_corr = -1
    half_len = (3 * n_samples_1ms) // 2
    for peak in peaks:
        start_idx = peak - half_len
        if start_idx < 0 or start_idx + 3 * n_samples_1ms > len(envelope):
            continue

        left_segment = envelope[start_idx : start_idx + n_samples_1ms]
        right_segment = envelope[start_idx + 2 * n_samples_1ms : start_idx + 3 * n_samples_1ms]
        e_left = np.mean(left_segment)
        e_right = np.mean(right_segment)
        if e_left == 0 and e_right == 0:
            continue

        ratio = min(e_left, e_right) / (max(e_left, e_right) + 1e-9)
        if ratio > threshold_ratio and correlation[peak] > max_corr:
            max_corr = correlation[peak]
            best_peak = int(peak)

    return best_peak, correlation


def detect_pulse_sequence_from_envelope(envelope, fs, symmetry_ratio=0.4):
    """
    Detect all 1ms high, 1ms low, 1ms high pulse candidates in an envelope.
    """
    if envelope is None or len(envelope) == 0:
        return np.array([]), np.array([])

    n_samples_1ms = int(fs * 0.001)
    if n_samples_1ms < 1:
        return np.array([]), np.array([])

    template = np.concatenate([
        np.ones(n_samples_1ms),
        np.zeros(n_samples_1ms),
        np.ones(n_samples_1ms),
    ])
    template_sum = np.sum(template)
    if template_sum > 0:
        template = template / template_sum

    correlation = signal.correlate(envelope, template, mode="same")
    if len(correlation) == 0:
        return np.array([]), np.array([])

    mean_corr = np.mean(correlation)
    std_corr = np.std(correlation)
    thresholds = [mean_corr + 4 * std_corr, mean_corr + 3 * std_corr, mean_corr + 2 * std_corr]
    distance = n_samples_1ms * 3

    peaks = []
    for threshold in thresholds:
        peaks, _ = signal.find_peaks(correlation, height=threshold, distance=distance)
        if len(peaks) > 0:
            break

    if len(peaks) == 0:
        return np.array([]), correlation

    valid_peaks = []
    half_len = (3 * n_samples_1ms) // 2
    for peak in peaks:
        start_idx = peak - half_len
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
            valid_peaks.append(int(peak))

    return np.array(valid_peaks), correlation


def detect_pulse_sequence(signal_data, fs, pulse_freq_hz=5000.0):
    """
    Detect pulse sequence candidates from a filtered raw neural trace.
    """
    if signal_data is None or len(signal_data) == 0:
        return np.array([]), np.array([])

    try:
        amplitude_envelope = np.abs(signal.hilbert(signal_data))
    except Exception:
        amplitude_envelope = np.abs(signal_data)

    return detect_pulse_sequence_from_envelope(amplitude_envelope, fs, symmetry_ratio=0.4)


def detect_pulse_sequence_energy(signal_data, fs):
    """
    Harmonics-friendly detector based on short-time full-band energy.
    """
    if signal_data is None or len(signal_data) == 0:
        return np.array([]), np.array([])

    n_samples_1ms = int(fs * 0.001)
    if n_samples_1ms < 1:
        return np.array([]), np.array([])

    x = signal_data - np.median(signal_data)
    nyquist = 0.5 * fs
    if nyquist > 301.0:
        b_hp, a_hp = signal.butter(2, 300.0, btype="high", fs=fs)
        x = signal.filtfilt(b_hp, a_hp, x)

    inst_power = x * x
    kernel = np.ones(n_samples_1ms, dtype=float) / max(1, n_samples_1ms)
    energy_envelope = np.convolve(inst_power, kernel, mode="same")
    return detect_pulse_sequence_from_envelope(energy_envelope, fs, symmetry_ratio=0.45)
