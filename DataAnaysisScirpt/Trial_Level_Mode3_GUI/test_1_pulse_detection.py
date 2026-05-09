import os
import sys
from pathlib import Path

import pyedflib
import numpy as np
import matplotlib.pyplot as plt
from scipy import signal
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from Common_Analysis.preprocess import clean_data_linear as _shared_clean_data_linear
from Common_Analysis.sync_detection import detect_best_pulse_in_window as _shared_detect_best_pulse_in_window

def clean_data_linear(data, threshold=-1000):
    return _shared_clean_data_linear(data, threshold=threshold)

def _legacy_clean_data_linear(data, threshold=-1000):
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

def detect_best_pulse_in_window(envelope, offset_idx, fs, threshold_ratio=0.5):
    return _shared_detect_best_pulse_in_window(
        envelope,
        offset_idx,
        fs,
        threshold_ratio=threshold_ratio,
    )

def _legacy_detect_best_pulse_in_window(envelope, offset_idx, fs, threshold_ratio=0.5):
    """
    Search for the best pulse sequence in the given envelope window.
    Returns: (best_idx, correlation_map)
    best_idx is relative to the start of the envelope array.
    """
    if len(envelope) == 0:
        return None, None
        
    n_samples_1ms = int(fs * 0.001)
    # Template: 1ms High, 1ms Low, 1ms High
    template = np.concatenate([np.ones(n_samples_1ms), np.zeros(n_samples_1ms), np.ones(n_samples_1ms)])
    template = template / np.sum(template)
    
    correlation = signal.correlate(envelope, template, mode='same')
    
    # Adaptive threshold: Only look at peaks above the mean
    mean_corr = np.mean(correlation)
    std_corr = np.std(correlation)
    threshold = mean_corr + 2 * std_corr
    distance = n_samples_1ms * 3
    
    peaks, _ = signal.find_peaks(correlation, height=threshold, distance=distance)
    
    best_peak = None
    max_corr = -1
    half_len = (3 * n_samples_1ms) // 2
    
    for p in peaks:
        start_idx = p - half_len
        # Bounds check
        if start_idx < 0 or start_idx + 3 * n_samples_1ms > len(envelope):
            continue
            
        left_segment = envelope[start_idx : start_idx + n_samples_1ms]
        right_segment = envelope[start_idx + 2*n_samples_1ms : start_idx + 3*n_samples_1ms]
        
        e_left = np.mean(left_segment)
        e_right = np.mean(right_segment)
        
        if e_left == 0 and e_right == 0:
            continue
            
        ratio = min(e_left, e_right) / (max(e_left, e_right) + 1e-9)
        
        # Check symmetry
        if ratio > threshold_ratio:
            if correlation[p] > max_corr:
                max_corr = correlation[p]
                best_peak = p
                
    return best_peak, correlation

def main():
    base_dir = Path(__file__).resolve().parent
    raw_file = base_dir / "ExampleData" / "NeuralFile" / "2026-04-03-04-46-19mode3_raw.edf"
    
    print(f"Loading files...")
    f_raw = pyedflib.EdfReader(str(raw_file))
    fs_raw = f_raw.getSampleFrequency(0)
    raw_data = f_raw.readSignal(0)
    raw_align = f_raw.readSignal(2) # Channel 2 is 'Alignment'
    f_raw.close()
    
    print(f"Raw data length: {len(raw_data)} samples. FS: {fs_raw} Hz")
    
    t0 = time.time()
    raw_clean = clean_data_linear(raw_data, threshold=-1000)
    print(f"Cleaning took {time.time()-t0:.2f}s")
    
    t0 = time.time()
    nyq = 0.5 * fs_raw
    b, a = signal.butter(4, [4500.0/nyq, 5500.0/nyq], btype='band')
    raw_filtered = signal.filtfilt(b, a, raw_clean)
    print(f"Filtering took {time.time()-t0:.2f}s")
    
    t0 = time.time()
    # 1. Locate alignment pulses (Trial Index) in Channel 1
    # Pulse values are the trial indices (e.g., 1, 2, 3...)
    # Set a small fixed threshold to catch all pulses regardless of amplitude.
    align_thr = 0.5 
    align_binary = (raw_align > align_thr).astype(int)
    rising_edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
    expected_pulses = len(rising_edges)
    print(f"Found {expected_pulses} alignment pulses from channel 1.")
    
    # Process pulse envelopes efficiently
    # Computing hilbert for entire sequence is fine or only for windows.
    # Since whole sequence is small (~1.5M points), fast enough:
    print("Computing Hilbert envelope...")
    analytic_signal = signal.hilbert(raw_filtered)
    envelope = np.abs(analytic_signal)
    
    found_pulses = []
    
    win_start_offset = int(1.0 * fs_raw) # 1s before
    win_end_offset = int(0.5 * fs_raw)   # 500ms before
    
    for i, align_idx in enumerate(rising_edges):
        # 2. Search between 1s and 500ms before the alignment pulse
        search_start = max(0, align_idx - win_start_offset)
        search_end = max(0, align_idx - win_end_offset)
        
        if search_end <= search_start:
            print(f"Invalid search window for pulse {i}")
            continue
            
        env_window = envelope[search_start:search_end]
        
        # 4. Find the max energy pulse satisfying the 1ms-1ms-1ms logic
        best_peak_rel, corr_window = detect_best_pulse_in_window(env_window, search_start, fs_raw, threshold_ratio=0.4)
        
        if best_peak_rel is not None:
            best_peak_abs = search_start + best_peak_rel
            found_pulses.append((align_idx, best_peak_abs, corr_window, search_start, search_end))
        else:
            print(f"Warning: No valid neural pulse found for alignment pulse at idx {align_idx}")
            
    print(f"Detection took {time.time()-t0:.2f}s.")
    
    # 5. Output totals
    print(f"--- Results ---")
    print(f"Theoretically expected pulses: {expected_pulses}")
    print(f"Successfully found pulses:     {len(found_pulses)}")
    
    # Save plots for the first 3 detected pulses to verify
    for p_i, (align_idx, peak_idx, corr_window, s_start, s_end) in enumerate(found_pulses[:3]):
        
        # Plot window: 10ms around the detected peak
        window = int(0.01 * fs_raw)
        plot_start = max(0, peak_idx - window)
        plot_end = min(len(raw_data), peak_idx + window)
        
        t = np.arange(plot_start, plot_end) / fs_raw * 1000
        
        plt.figure(figsize=(10, 8))
        plt.subplot(3, 1, 1)
        plt.plot(t, raw_clean[plot_start:plot_end], color='blue')
        plt.title(f'Raw Data (Cleaned) - Pulse {p_i+1} (Aligned to {align_idx})')
        plt.ylabel('Amplitude')
        
        plt.subplot(3, 1, 2)
        plt.plot(t, raw_filtered[plot_start:plot_end], color='gray', label='4.5-5.5kHz bandpass')
        plt.plot(t, envelope[plot_start:plot_end], color='red', alpha=0.7, label='Hilbert Envelope')
        plt.title('Filtered & Envelope')
        plt.ylabel('Amplitude')
        plt.legend()
        
        plt.subplot(3, 1, 3)
        # We need to map correlation window back to absolute time for plotting
        corr_plot_start = max(0, plot_start - s_start)
        corr_plot_end = min(len(corr_window), plot_end - s_start)
        t_corr = np.arange(plot_start, plot_start + (corr_plot_end - corr_plot_start)) / fs_raw * 1000
        
        plt.plot(t_corr, corr_window[corr_plot_start:corr_plot_end], color='green', label='Correlation')
        plt.axvline(peak_idx / fs_raw * 1000, color='red', linestyle='--', label=f'Peak')
        plt.title('Template Correlation')
        plt.xlabel('Time (ms)')
        plt.ylabel('Score')
        plt.legend()
        
        plt.tight_layout()
        out_file = base_dir / f"test_pulse_{p_i+1}.png"
        plt.savefig(str(out_file))
        print(f"Saved plot: {out_file}")
        plt.close()

if __name__ == '__main__':
    main()
