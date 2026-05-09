import pyedflib
import numpy as np
import matplotlib.pyplot as plt
from scipy import signal, interpolate
import os
import sys

import re

class TrialData:
    def __init__(self, line):
        parts = line.strip().split()
        # Header: Time TrialNum ProtocolIndex TrialType TrialOutcome SamplePeriod DelayPeriod is_earlylick Rule currStimu[0] currStimu[1] BaselineTrialFlag nVisited ...
        # Indices: 0    1        2             3         4            5            6           7            8    9            10           11                12
        
        self.time = parts[0]
        self.trial_num = int(parts[1])
        self.protocol_index = int(parts[2])
        self.trial_type = int(parts[3])
        self.trial_outcome = int(parts[4])
        self.sample_period = int(parts[5])
        self.delay_period = int(parts[6])
        self.is_earlylick = int(parts[7])
        self.rule = int(parts[8])
        self.curr_stimu = [float(parts[9]), float(parts[10])]
        self.baseline_flag = int(parts[11])
        self.n_visited = int(parts[12])
        
        self.states = {} # Map state ID to timestamp(s)
        self.state_sequence = [] # List of (state, timestamp)
        
        # Parse states
        idx = 13
        for _ in range(self.n_visited):
            if idx + 1 >= len(parts):
                break
            state = int(parts[idx])
            timestamp = int(parts[idx+1])
            idx += 2
            
            self.state_sequence.append((state, timestamp))
            if state not in self.states:
                self.states[state] = []
            self.states[state].append(timestamp)

def parse_trial_file(file_path):
    trials = {}
    if not os.path.exists(file_path):
        print(f"Error: Trial file not found at {file_path}")
        return trials
        
    with open(file_path, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                trial = TrialData(line)
                trials[trial.trial_num] = trial
            except Exception as e:
                print(f"Error parsing line: {line[:50]}... {e}")
    return trials

class TeventData:
    def __init__(self, line):
        parts = line.strip().split()
        # Header: TrialNum nEvent EventID[0] TimeStamp[0] ...
        self.trial_num = int(parts[0])
        self.n_event = int(parts[1])
        self.events = [] # List of (event_id, timestamp)
        
        idx = 2
        for _ in range(self.n_event):
            if idx + 1 >= len(parts):
                break
            event_id = int(parts[idx])
            timestamp = int(parts[idx+1])
            idx += 2
            self.events.append((event_id, timestamp))

def parse_tevent_file(file_path):
    tevents = {}
    if not os.path.exists(file_path):
        print(f"Error: Tevent file not found at {file_path}")
        return tevents
        
    with open(file_path, 'r') as f:
        for line in f:
            if not line.strip():
                continue
            try:
                tevent = TeventData(line)
                tevents[tevent.trial_num] = tevent
            except Exception as e:
                print(f"Error parsing Tevent line: {line[:50]}... {e}")
    return tevents

def get_end_timestamp(file_path):
    """
    Reads the EDF header to find the 'remark' with the last value's timestamp.
    Returns the timestamp in ms (assuming the value in remark is in ms or convertable).
    """
    try:
        f = pyedflib.EdfReader(file_path)
        header = f.getHeader()
        f.close()
        
        # User says: "all edf files have a remark, noting the data's last value timestamp"
        # We check 'recording_additional' and 'patientcode'
        candidates = [header.get('recording_additional', ''), header.get('patientcode', '')]
        
        for remark in candidates:
            if not remark:
                continue
                
            # Look for explicit "timestamp" label
            match = re.search(r'timestamp\s*[:=]?\s*(\d+)', remark, re.IGNORECASE)
            if match:
                return float(match.group(1))
            
            # Look for any large integer (timestamp-like)
            # Assuming it's at the end or standalone
            matches = re.findall(r'(\d+)', remark)
            if matches:
                # Heuristic: return the last one found
                return float(matches[-1])
                
        return 0
    except Exception as e:
        print(f"Error reading timestamp from {file_path}: {e}")
        return 0

def detect_pulse_sequence(signal_data, fs, method='envelope'):
    """
    Detects a sequence of: 1ms 5kHz pulse -> 1ms quiet -> 1ms 5kHz pulse.
    Returns the indices of the midpoints of the detected sequences.
    """
    n_samples_1ms = int(fs * 0.001)
    
    if method == 'envelope':
        # 1. Compute Amplitude Envelope
        analytic_signal = signal.hilbert(signal_data)
        amplitude_envelope = np.abs(analytic_signal)
        
        # 2. Define Template (Envelope)
        # 1ms High, 1ms Low, 1ms High
        # We use a kernel that rewards High regions and penalizes Low regions (optional)
        # Simple matched filter: 1, 1... 0, 0... 1, 1...
        # To make it sharper, we can use -0.5 for the quiet period to penalize noise there?
        # Let's try simple 1, 0, 1 first.
        
        template = np.concatenate([
            np.ones(n_samples_1ms),
            np.zeros(n_samples_1ms),
            np.ones(n_samples_1ms)
        ])
        
        # Normalize
        template = template / np.sum(template)
        
        # 3. Correlation
        # mode='same' centers the result
        correlation = signal.correlate(amplitude_envelope, template, mode='same')
        
        # 4. Find Peaks
        # Adaptive threshold
        mean_corr = np.mean(correlation)
        std_corr = np.std(correlation)
        # Threshold: Experimental. Start with mean + 4*std.
        threshold = mean_corr + 4 * std_corr
        
        # Min distance between peaks should be at least the length of the pattern (3ms)
        distance = n_samples_1ms * 3
        
        peaks, _ = signal.find_peaks(correlation, height=threshold, distance=distance)
        
        # --- Symmetry Verification ---
        # Pattern: [1ms Pulse] [1ms Quiet] [1ms Pulse]
        # Total Length: 3ms. Center (peak) is in the middle of "Quiet".
        # But actually, the template center is at index 1.5ms.
        # Template: [1ms ones, 1ms zeros, 1ms ones]. Length L = 3 * n_samples_1ms.
        # If correlation peak is at index `p`, this corresponds to the center of alignment between signal and template.
        # So `p` should align with the center of the template.
        # Center of template is roughly index 1.5 * n_samples_1ms.
        # Left Pulse in template: indices [0 : n_samples_1ms]
        # Right Pulse in template: indices [2*n_samples_1ms : 3*n_samples_1ms]
        
        valid_peaks = []
        valid_corrs = []
        
        half_len = (3 * n_samples_1ms) // 2
        
        for p in peaks:
            # Determine start index of the pattern in the signal
            # The peak `p` in correlation (mode='same') corresponds to the center of the window
            start_idx = p - half_len
            
            # Check bounds
            if start_idx < 0 or start_idx + 3 * n_samples_1ms > len(amplitude_envelope):
                continue
                
            # Extract envelope segments
            # Left pulse (first 1ms)
            left_segment = amplitude_envelope[start_idx : start_idx + n_samples_1ms]
            # Right pulse (last 1ms)
            right_segment = amplitude_envelope[start_idx + 2*n_samples_1ms : start_idx + 3*n_samples_1ms]
            
            # Calculate Energy (Mean Amplitude)
            e_left = np.mean(left_segment)
            e_right = np.mean(right_segment)
            
            # Symmetry Check
            # Prevent division by zero
            if e_left == 0 and e_right == 0:
                continue # Both zero, probably fine but correlation would be low anyway
            
            # Metric: Ratio of min/max. If perfectly symmetric, ratio = 1.
            # If one is noise (very large) and other is signal (moderate), ratio is small.
            # If one is signal and other is noise, ratio is small.
            # Threshold: 0.5 (one pulse must be at least 50% strength of the other)
            ratio = min(e_left, e_right) / (max(e_left, e_right) + 1e-9)
            
            if ratio > 0.5:
                valid_peaks.append(p)
                valid_corrs.append(correlation[p])
        
        return np.array(valid_peaks), np.array(correlation)

    return [], None

def apply_lowpass(data, cutoff, fs, order=4):
    nyquist = 0.5 * fs
    normal_cutoff = cutoff / nyquist
    b, a = signal.butter(order, normal_cutoff, btype='low', analog=False)
    y = signal.filtfilt(b, a, data)
    return y

def apply_highpass(data, cutoff, fs, order=4):
    nyquist = 0.5 * fs
    normal_cutoff = cutoff / nyquist
    b, a = signal.butter(order, normal_cutoff, btype='high', analog=False)
    y = signal.filtfilt(b, a, data)
    return y

def load_and_analyze_edf(raw_file_path, lfp_file_path=None, sensor_file_path=None):
    print(f"Processing Raw File: {raw_file_path}")
    if lfp_file_path:
        print(f"Processing LFP File: {lfp_file_path}")
    if sensor_file_path:
        print(f"Processing Sensor File: {sensor_file_path}")
    
    # --- Load Raw File ---
    try:
        f_raw = pyedflib.EdfReader(raw_file_path)
    except Exception as e:
        print(f"Error opening Raw EDF file: {e}")
        return

    # Raw Info
    fs_raw = f_raw.getSampleFrequency(0)
    n_samples_raw = f_raw.getNSamples()[0]
    print(f"Raw Sampling Rate: {fs_raw} Hz, Samples: {n_samples_raw}")
    
    raw_data = f_raw.readSignal(0)
    # Channel 1 is Alignment/Index (as per previous logic)
    # Verify signal labels if possible, but assuming index 1 is alignment
    idx_align = 2 if f_raw.signals_in_file > 2 else 1 # Simple heuristic or fixed index
    # User previously used index 2 for map? 
    # Previous code: idx_chan_map = 2. Let's stick to that if valid, else 1.
    if idx_align >= f_raw.signals_in_file:
        idx_align = 1
    
    alignment_data = f_raw.readSignal(idx_align)
    f_raw.close()
    
            # --- Load LFP File (if provided) ---
    lfp_data_all = None
    fs_lfp = 0
    lfp_time_offset = 0 # ms
    
    if lfp_file_path and os.path.exists(lfp_file_path):
        try:
            f_lfp = pyedflib.EdfReader(lfp_file_path)
            fs_lfp = f_lfp.getSampleFrequency(0)
            n_samples_lfp = f_lfp.getNSamples()[0]
            n_channels_lfp = f_lfp.signals_in_file
            print(f"LFP File: {n_channels_lfp} channels, {fs_lfp} Hz, {n_samples_lfp} samples")
            
            # Read all channels (Expect 32)
            # Pre-allocate array
            lfp_data_all = np.zeros((n_channels_lfp, n_samples_lfp))
            
            for ch in range(n_channels_lfp):
                lfp_data_all[ch, :] = f_lfp.readSignal(ch)
                
            f_lfp.close()
            
            # --- Cleaning: Set values < -1000 to 0 ---
            print("Cleaning LFP/ESA data (setting < -1000 to 0)...")
            lfp_data_all[lfp_data_all < -1000] = 0
            
            # --- LFP Processing (First 16 channels) ---
            # Indices 0-15 are LFP, 16-31 are ESA
            if n_channels_lfp >= 16:
                print("Processing LFP channels (0-15)...")
                # 1. Common Median Reference (CMR)
                # Calculate median across channels for each time point
                # lfp_data_all[0:16, :] shape is (16, N)
                # median along axis 0 (channels)
                median_ref = np.median(lfp_data_all[0:16, :], axis=0)
                
                # Subtract median
                lfp_data_all[0:16, :] -= median_ref
                
                # 2. Low-pass Filter (150Hz)
                print("Applying Low-pass filter (150Hz) to LFP channels...")
                # Design filter once
                nyq = 0.5 * fs_lfp
                b_lp, a_lp = signal.butter(4, 150.0 / nyq, btype='low')
                
                for ch in range(16):
                    lfp_data_all[ch, :] = signal.filtfilt(b_lp, a_lp, lfp_data_all[ch, :])
            else:
                print("Warning: Less than 16 channels in LFP file. Skipping LFP-specific processing.")

            # --- Alignment Logic ---
            # Get End Timestamps
            ts_end_raw = get_end_timestamp(raw_file_path)
            ts_end_lfp = get_end_timestamp(lfp_file_path)
            
            print(f"End Timestamp Raw: {ts_end_raw}")
            print(f"End Timestamp LFP: {ts_end_lfp}")
            
            if ts_end_raw > 0 and ts_end_lfp > 0:
                # Duration in ms
                dur_raw = (n_samples_raw / fs_raw) * 1000.0
                dur_lfp = (n_samples_lfp / fs_lfp) * 1000.0
                
                # Start Times (Absolute)
                start_raw = ts_end_raw - dur_raw
                start_lfp = ts_end_lfp - dur_lfp
                
                # We want to align LFP to Raw.
                # t_raw = t_lfp + (Start_lfp - Start_raw)
                # offset = Start_lfp - Start_raw
                lfp_time_offset = start_lfp - start_raw
                print(f"Calculated LFP Time Offset: {lfp_time_offset:.2f} ms")
            else:
                print("Warning: Could not find valid timestamps for alignment. Assuming 0 offset.")
                
        except Exception as e:
            print(f"Error loading LFP file: {e}")

    # --- Load Sensor File (if provided) ---
    sensor_data_all = None
    fs_sensor = 0
    sensor_time_offset = 0
    sensor_labels = []
    
    if sensor_file_path and os.path.exists(sensor_file_path):
        try:
            f_sensor = pyedflib.EdfReader(sensor_file_path)
            fs_sensor = f_sensor.getSampleFrequency(0)
            n_samples_sensor = f_sensor.getNSamples()[0]
            n_channels_sensor = f_sensor.signals_in_file
            sensor_labels = f_sensor.getSignalLabels()
            print(f"Sensor File: {n_channels_sensor} channels, {fs_sensor} Hz, {n_samples_sensor} samples")
            print(f"Sensor Labels: {sensor_labels}")
            
            # Identify Accelerometer Channels
            # Look for ACClX, ACClY, ACClZ
            # Also updateFlag
            acc_indices = []
            acc_names = ['ACClX', 'ACClY', 'ACClZ']
            
            # Map names to indices
            # User provided: ACClX, Y, Z. Case might vary.
            for name in acc_names:
                for idx, label in enumerate(sensor_labels):
                    if name.lower() in label.lower():
                        acc_indices.append(idx)
                        break
            
            if len(acc_indices) == 3:
                # Read 3 channels
                sensor_data_all = np.zeros((3, n_samples_sensor))
                for i, idx in enumerate(acc_indices):
                    sensor_data_all[i, :] = f_sensor.readSignal(idx)
                    
                # Cleaning: Set values < -5 to 0
                print("Cleaning Sensor data (setting < -5 to 0)...")
                sensor_data_all[sensor_data_all < -5] = 0
                
                f_sensor.close()
                
                # --- Alignment Logic for Sensor ---
                ts_end_sensor = get_end_timestamp(sensor_file_path)
                print(f"End Timestamp Sensor: {ts_end_sensor}")
                
                if ts_end_raw > 0 and ts_end_sensor > 0:
                     dur_sensor = (n_samples_sensor / fs_sensor) * 1000.0
                     start_sensor = ts_end_sensor - dur_sensor
                     
                     sensor_time_offset = start_sensor - start_raw
                     print(f"Calculated Sensor Time Offset: {sensor_time_offset:.2f} ms")
                else:
                     print("Warning: Could not find valid timestamps for sensor alignment. Assuming 0 offset.")
                     
            else:
                print(f"Warning: Could not find all accelerometer channels {acc_names} in {sensor_labels}")
                f_sensor.close()
                
        except Exception as e:
            print(f"Error loading Sensor file: {e}")

    # --- Data Cleaning / Interpolation (Raw) ---
    print("Analyzing data for missing points (outliers)...")
    
    # Strategy: "points way above average noise level" -> likely artifacts/saturation
    # 1. Calculate statistics
    mean_val = np.mean(raw_data)
    std_val = np.std(raw_data)
    
    print(f"Data Mean: {mean_val:.4f}, Std: {std_val:.4f}")
    
    # 2. Define Threshold
    # User specified "way above average noise level".
    # Typically, neural spikes are within +/- 5 std.
    # We will set a high threshold (e.g., 10 * std) to catch "data loss" artifacts.
    threshold_multiplier = 10.0
    threshold = 5000
    
    # Find bad indices (Magnitude is too large)
    # Using absolute deviation from mean
    is_bad = np.abs(raw_data) > threshold
    
    n_bad = np.sum(is_bad)
    print(f"Found {n_bad} points identified as outliers (> {threshold_multiplier} * std).")
    
    data_clean = raw_data.copy()
    alignment_clean = alignment_data.copy()
    
    
    if n_bad > 0:
        print("Interpolating outliers...")
        # Create x coordinates
        x = np.arange(len(raw_data))
        
        # Valid indices
        valid_mask = ~is_bad
        
        # We need at least some valid data
        if np.sum(valid_mask) > 0:
            # Linear interpolation
            data_clean = np.interp(x, x[valid_mask], raw_data[valid_mask])
            alignment_clean = np.interp(x, x[valid_mask], alignment_data[valid_mask])
        else:
            print("Warning: No valid data found to interpolate!")
            
    # --- Bandpass Filter (4kHz - 6kHz) ---
    print("Applying Bandpass Filter (4kHz - 6kHz)...")
    try:
        # Check Nyquist
        nyquist = 0.5 * fs_raw
        low = 4500.0
        high = 5500.0
        
        if high >= nyquist:
            print(f"Warning: High cutoff ({high} Hz) is >= Nyquist ({nyquist} Hz). Clamping to {nyquist-1} Hz.")
            high = nyquist - 1.0
            
        if low >= high:
             print("Error: Low cutoff is >= High cutoff after adjustment. Skipping filter.")
        else:
            b, a = signal.butter(4, [low, high], btype='band', fs=fs_raw)
            # Use filtfilt for zero phase distortion
            data_filtered = signal.filtfilt(b, a, data_clean)
            print("Filter applied successfully.")
    except Exception as e:
        print(f"Filter error: {e}")
        data_filtered = data_clean # Fallback

    # --- Full File Analysis ---
    print("Starting full file analysis...")
    
    # Locate and Parse Trial.txt
    # Assuming Trial.txt is in the same directory as the EDF file
    edf_dir = os.path.dirname(raw_file_path)
    trial_file = os.path.join(edf_dir, "Trial.txt")
    
    trial_data_map = {}
    if os.path.exists(trial_file):
        print(f"Found Trial.txt at {trial_file}")
        trial_data_map = parse_trial_file(trial_file)
        print(f"Parsed {len(trial_data_map)} trials.")
    else:
        print(f"Warning: Trial.txt not found at {trial_file}. Cannot perform behavioral alignment.")
        
    # Locate and Parse Tevent.txt
    tevent_file = os.path.join(edf_dir, "Tevent.txt")
    tevent_data_map = {}
    if os.path.exists(tevent_file):
        print(f"Found Tevent.txt at {tevent_file}")
        tevent_data_map = parse_tevent_file(tevent_file)
        print(f"Parsed {len(tevent_data_map)} tevent entries.")
    else:
        print(f"Warning: Tevent.txt not found at {tevent_file}.")
    
    # Create Output Directory for Aligned Plots
    output_dir = os.path.join(edf_dir, "Aligned_Trials")
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"Created output directory: {output_dir}")

    # Spectrogram parameters
    nfft = 1024
    noverlap = 512
    
    # We process the entire file at once
    chunk_filtered = data_filtered
    chunk_align = alignment_clean
    
    # --- Detect Pulse Sequence ---
    detected_indices, correlation_vals = detect_pulse_sequence(chunk_filtered, fs_raw)
    
    # Count pulses in alignment channel (rising edges)
    # Assume logic level: threshold at 0.5 (max is usually 1 or 5V, but data seems normalized or digital)
    # We use a robust method: find transitions from low to high
    align_thr = 0.5 * np.max(chunk_align) if np.max(chunk_align) > 0 else 0.5
    align_binary = (chunk_align > align_thr).astype(int)
    # Find rising edges: change from 0 to 1
    rising_edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
    n_alignment = len(rising_edges)
    
    print(f"Alignment Channel: Found {n_alignment} pulses.")
    
    # Filter detected sequences to match n_alignment
    # NEW STRATEGY: For each Alignment Pulse, look back 1200ms for best Neural Pulse.
    
    # 1. Get all potential candidates first (already done above in detected_indices)
    # We will iterate through alignment pulses and pick the best candidate.
    
    valid_matches_indices = [] # Indices in 'detected_indices'
    matched_align_indices = [] # Indices in 'rising_edges'
    
    # Window settings
    window_ms = 1200.0
    window_samples = int((window_ms / 1000.0) * fs_raw)
    
    # Store results for plotting: list of (time, type, label)
    # type: 'valid' or 'error'
    # For alignment, we need to map: Alignment Pulse Index -> Neural Pulse Time
    alignment_map = {} # { alignment_index_in_rising_edges : neural_pulse_index }
    
    print(f"Matching: Looking for pulses in {window_ms}ms window before each alignment pulse...")
    
    for i, align_idx in enumerate(rising_edges):
        # Define window [start, end]
        # Look BEFORE alignment
        win_end = align_idx
        win_start = align_idx - window_samples
        
        # Find candidates within this window
        mask = (detected_indices >= win_start) & (detected_indices <= win_end)
        candidates = detected_indices[mask]
        
        if len(candidates) > 0:
            # Candidates found. Pick the best one.
            cand_corrs = correlation_vals[candidates]
            best_local_idx = np.argmax(cand_corrs)
            best_neural_idx = candidates[best_local_idx]
            
            alignment_map[i] = best_neural_idx
            
    print(f"Matching Complete. Found {len(alignment_map)} valid matches.")
    
    # --- Per-Trial Alignment and Plotting ---
    # Iterate through Alignment Pulses (assuming they correspond to Trials 1, 2, 3...)
    # User says: "read value... represents Trialnum". We assume sequential 1-based index.
    
    for i, align_idx in enumerate(rising_edges):
        # Read the value of the Alignment Channel at this pulse
        # We sample slightly after the rising edge to ensure we read the high level
        # Assuming the pulse width is sufficient.
        # Let's read at index `align_idx` (which is the first point > threshold) or align_idx + 1
        
        # Read a small window and take the mode or max?
        # User says "every pulse is a value... 100, 101...".
        # So it's an analog value encoding the Trial ID? Or a digital bus?
        # If it's a single channel `alignment_data`, it must be an analog level representing the ID.
        # Let's read the value at `align_idx + 5` samples (assuming stable high).
        
        sample_idx = min(align_idx + 10, len(chunk_align) - 1)
        pulse_value = chunk_align[sample_idx]
        
        # Round to nearest integer if it's an ID
        # Assuming the value is directly the Trial ID (e.g. 100.0)
        trial_id_read = int(np.round(pulse_value))
        
        # Use this ID as the TrialNum
        trial_num = trial_id_read
        
        print(f"Alignment Pulse {i}: Index {align_idx}, Value {pulse_value:.2f} -> Trial ID {trial_num}")
        
        # Check if we have behavioral data
        if trial_num not in trial_data_map:
            print(f"Skipping Trial {trial_num}: No data in Trial.txt")
            continue
            
        trial_data = trial_data_map[trial_num]
        
        # Explicit Check: Does the Trial object's internal number match our read ID?
        if trial_data.trial_num != trial_num:
             print(f"Warning: Trial ID Mismatch! Expected {trial_num}, found {trial_data.trial_num} in map.")
        
        # Check Tevent data alignment
        if trial_num in tevent_data_map:
             tevent_data = tevent_data_map[trial_num]
             if tevent_data.trial_num != trial_num:
                  print(f"Warning: Tevent ID Mismatch! Expected {trial_num}, found {tevent_data.trial_num}.")
        else:
             print(f"Notice: No Tevent data for Trial {trial_num}")

        # Check if we have a valid neural alignment pulse
        if i not in alignment_map:
            print(f"Skipping Trial {trial_num}: No matching neural pulse found (Error).")
            continue
            
        neural_pulse_idx = alignment_map[i]
        
        # Check if State 3 exists
        if 3 not in trial_data.states:
            print(f"Skipping Trial {trial_num}: State 3 not visited.")
            continue
            
        # Get State 3 timestamp (first visit?)
        # User says "stateVisited中3的时候". Usually first visit is key.
        state3_ts = trial_data.states[3][0] # ms
        
        # Calculate Alignment
        # Logic: Neural Pulse Time <-> (State3_Timestamp - 5ms)
        # We want to plot X-axis as "Trial Time (ms)".
        # So Neural Pulse should be at X = State3_Timestamp - 5.
        
        # Define plotting window around the event
        # Let's show -1000ms to +5000ms relative to State 3? 
        # Or relative to Trial Start (0)?
        # The user says "align RawData and several key states".
        # Let's plot from -2000ms to end of trial (max timestamp + padding).
        
        # Find max timestamp in trial to determine duration
        max_ts = 0
        if trial_data.state_sequence:
            max_ts = trial_data.state_sequence[-1][1]
        
        plot_start_ms = -2000
        plot_end_ms = max_ts + 2000
        
        # Convert ms to samples (relative to neural pulse)
        # Neural Pulse is at `neural_pulse_idx` (samples).
        # This corresponds to time `T_ref = state3_ts - 5` (ms).
        # We want data from `T_start` to `T_end` (ms).
        # Delta_T_start = T_start - T_ref
        # Sample_Start = neural_pulse_idx + (Delta_T_start / 1000 * fs)
        
        t_ref_ms = state3_ts - 5
        
        delta_start_ms = plot_start_ms - t_ref_ms
        delta_end_ms = plot_end_ms - t_ref_ms
        
        sample_start = int(neural_pulse_idx + (delta_start_ms / 1000.0 * fs_raw))
        sample_end = int(neural_pulse_idx + (delta_end_ms / 1000.0 * fs_raw))
        
        # Check bounds
        if sample_start < 0: sample_start = 0
        if sample_end > len(chunk_filtered): sample_end = len(chunk_filtered)
        
        if sample_end <= sample_start:
            continue
            
        # Extract Data
        segment_data = data_clean[sample_start:sample_end]
        
        # Apply Filters for Visualization
        segment_low = apply_lowpass(segment_data, 150.0, fs_raw)
        segment_high = apply_highpass(segment_data, 600.0, fs_raw)
        
        # Time Vector for Plot (Trial Time in ms)
        t_start_actual = t_ref_ms + (sample_start - neural_pulse_idx) * 1000.0 / fs_raw
        t_vector = t_start_actual + np.arange(len(segment_data)) * 1000.0 / fs_raw
        
        # --- LFP Extraction ---
        segment_lfp_chunk = None
        t_vector_lfp = None
        
        if lfp_data_all is not None:
             # Start LFP
            raw_sample_start = neural_pulse_idx + (plot_start_ms - t_ref_ms) * fs_raw / 1000.0
            raw_time_start = raw_sample_start * 1000.0 / fs_raw
            lfp_time_start = raw_time_start - lfp_time_offset
            lfp_sample_start = int(lfp_time_start * fs_lfp / 1000.0)
            
            # End LFP
            raw_sample_end = neural_pulse_idx + (plot_end_ms - t_ref_ms) * fs_raw / 1000.0
            raw_time_end = raw_sample_end * 1000.0 / fs_raw
            lfp_time_end = raw_time_end - lfp_time_offset
            lfp_sample_end = int(lfp_time_end * fs_lfp / 1000.0)
            
            # Check bounds
            if lfp_sample_start < 0: lfp_sample_start = 0
            if lfp_sample_end > lfp_data_all.shape[1]: lfp_sample_end = lfp_data_all.shape[1]
            
            if lfp_sample_end > lfp_sample_start:
                # Extract all channels
                segment_lfp_chunk = lfp_data_all[:, lfp_sample_start:lfp_sample_end]
                
                lfp_t_start = (lfp_sample_start * 1000.0 / fs_lfp) + lfp_time_offset
                raw_time_pulse = neural_pulse_idx * 1000.0 / fs_raw
                lfp_trial_t_start = t_ref_ms + (lfp_t_start - raw_time_pulse)
                
                t_vector_lfp = lfp_trial_t_start + np.arange(segment_lfp_chunk.shape[1]) * 1000.0 / fs_lfp
                
        # --- Sensor Extraction ---
        segment_sensor_chunk = None
        t_vector_sensor = None
        
        if sensor_data_all is not None:
            raw_sample_start = neural_pulse_idx + (plot_start_ms - t_ref_ms) * fs_raw / 1000.0
            raw_time_start = raw_sample_start * 1000.0 / fs_raw
            sensor_time_start = raw_time_start - sensor_time_offset
            sensor_sample_start = int(sensor_time_start * fs_sensor / 1000.0)
            
            raw_sample_end = neural_pulse_idx + (plot_end_ms - t_ref_ms) * fs_raw / 1000.0
            raw_time_end = raw_sample_end * 1000.0 / fs_raw
            sensor_time_end = raw_time_end - sensor_time_offset
            sensor_sample_end = int(sensor_time_end * fs_sensor / 1000.0)
            
            if sensor_sample_start < 0: sensor_sample_start = 0
            if sensor_sample_end > sensor_data_all.shape[1]: sensor_sample_end = sensor_data_all.shape[1]
            
            if sensor_sample_end > sensor_sample_start:
                segment_sensor_chunk = sensor_data_all[:, sensor_sample_start:sensor_sample_end]
                
                sensor_t_start = (sensor_sample_start * 1000.0 / fs_sensor) + sensor_time_offset
                raw_time_pulse = neural_pulse_idx * 1000.0 / fs_raw
                sensor_trial_t_start = t_ref_ms + (sensor_t_start - raw_time_pulse)
                
                t_vector_sensor = sensor_trial_t_start + np.arange(segment_sensor_chunk.shape[1]) * 1000.0 / fs_sensor

        # Plot: 6 Subplots (Neural, Spikes, LFP Avg Spec, ESA Avg Spec, Sensor, Events)
        fig, axes = plt.subplots(6, 1, figsize=(12, 18), sharex=True, gridspec_kw={'height_ratios': [1, 1, 1, 1, 1, 0.5]})
        ax1, ax2, ax3, ax4, ax5, ax6 = axes
        
        # Subplot 1: Spectrogram for Low-Pass (<150Hz)
        # Downsample to 1.25kHz first
        target_fs = 1250.0
        q = int(fs_raw / target_fs)
        
        # Use decimate for downsampling (includes anti-aliasing filter)
        # segment_data is the raw data segment
        segment_downsampled = signal.decimate(segment_low, q)
        
        # Calculate Spectrogram on downsampled data
        # Use parameters consistent with LFP (fs=1250)
        # nperseg=256 (205ms), noverlap=240 (step=16 samples=12.8ms)
        f_spec, t_spec, Sxx = signal.spectrogram(segment_downsampled, target_fs, nperseg=256, noverlap=240)
        
        t_spec_shifted = t_spec*1000 + t_vector[0]
        
        # Calculate vmin/vmax for better contrast
        # Use percentiles to avoid extreme outliers
        # Only consider data in the 0-150Hz range for scaling
        freq_mask = (f_spec >= 0) & (f_spec <= 150)
        Sxx_db_masked = 10 * np.log10(Sxx[freq_mask, :] + 1e-10)
        vmin = np.percentile(Sxx_db_masked, 5)
        vmax = np.percentile(Sxx_db_masked, 99)
        
        # Use pcolormesh for precise alignment with other subplots
        # sns.heatmap uses categorical/index axes which break sharex alignment with lineplots
        print(vmin, vmax)
        pcm = ax1.pcolormesh(t_spec_shifted, f_spec, 10 * np.log10(Sxx + 1e-10), shading='gouraud', cmap='RdBu_r', vmin=vmin, vmax=vmax)
        ax1.set_ylim(0, 150) # Limit to 150Hz
        ax1.set_ylabel('Freq (Hz)')
        ax1.set_title('Neural Spectrogram (0-150Hz)')
        
        # Subplot 2: High-Pass (>600Hz)
        ax2.plot(t_vector, segment_high, label='High-Pass (>600Hz)', color='black', linewidth=0.5)
        ax2.set_ylabel('Amp (uV)')
        ax2.legend(loc='upper right')
        ax2.grid(True, alpha=0.3)
        ax2.set_title('Spikes (>600Hz)')
        
        # Subplot 3: LFP Average Spectrogram (0-15)
        if segment_lfp_chunk is not None and segment_lfp_chunk.shape[0] >= 16:
             # Calculate Spectrogram for each channel and average
             Sxx_accum = None
             f_lfp_spec = None
             t_lfp_spec = None
             
             # Increase overlap for smoother time resolution
             # nperseg=256 (205ms), noverlap=240 (step=16 samples=12.8ms)
             for ch in range(16):
                 f_lfp_spec, t_lfp_spec, Sxx_ch = signal.spectrogram(segment_lfp_chunk[ch, :], fs_lfp, nperseg=256, noverlap=240)
                 if Sxx_accum is None:
                     Sxx_accum = Sxx_ch
                 else:
                     Sxx_accum += Sxx_ch
             
             Sxx_avg = Sxx_accum / 16.0
             t_lfp_shifted = t_lfp_spec*1000 + t_vector_lfp[0]
             
             # Calculate vmin/vmax for LFP
             freq_mask_lfp = (f_lfp_spec >= 0) & (f_lfp_spec <= 150)
             Sxx_db_masked_lfp = 10 * np.log10(Sxx_avg[freq_mask_lfp, :] + 1e-10)
             vmin_lfp = np.percentile(Sxx_db_masked_lfp, 5)
             vmax_lfp = np.percentile(Sxx_db_masked_lfp, 99)
             
             ax3.pcolormesh(t_lfp_shifted, f_lfp_spec, 10 * np.log10(Sxx_avg + 1e-10), shading='gouraud', cmap='RdBu_r', vmin=vmin_lfp, vmax=vmax_lfp)
             ax3.set_ylim(0, 150) # Limit to 150Hz or Nyquist (625Hz)? User filtered LFP to 150Hz.
             ax3.set_ylabel('Freq (Hz)')
             ax3.set_title('LFP Avg Spectrogram (0-15)')
        else:
             ax3.text(0.5, 0.5, 'No LFP Data', ha='center', va='center')
             
        # Subplot 4: ESA Average Spectrogram (16-31)
        if segment_lfp_chunk is not None and segment_lfp_chunk.shape[0] >= 32:
             Sxx_accum_esa = None
             # ESA Channels 16-31
             for ch in range(16, 32):
                 f_esa_spec, t_esa_spec, Sxx_ch = signal.spectrogram(segment_lfp_chunk[ch, :], fs_lfp, nperseg=256, noverlap=128)
                 if Sxx_accum_esa is None:
                     Sxx_accum_esa = Sxx_ch
                 else:
                     Sxx_accum_esa += Sxx_ch
             
             Sxx_avg_esa = Sxx_accum_esa / 16.0
             t_esa_shifted = t_esa_spec*1000 + t_vector_lfp[0]
             
             # Calculate vmin/vmax for ESA
             freq_mask_esa = (f_esa_spec >= 0) & (f_esa_spec <= 12)
             Sxx_db_masked_esa = 10 * np.log10(Sxx_avg_esa[freq_mask_esa, :] + 1e-10)
             vmin_esa = np.percentile(Sxx_db_masked_esa, 5)
             vmax_esa = np.percentile(Sxx_db_masked_esa, 99)
             
             # Use pcolormesh for precise alignment
             ax4.pcolormesh(t_esa_shifted, f_esa_spec, 10 * np.log10(Sxx_avg_esa + 1e-10), shading='gouraud', cmap='RdBu_r', vmin=vmin_esa, vmax=vmax_esa)
             ax4.set_ylim(0, 12)
             ax4.set_ylabel('Freq (Hz)')
             ax4.set_title('ESA Avg Spectrogram (16-31)')
        else:
             ax4.text(0.5, 0.5, 'No ESA Data', ha='center', va='center')
        
        # Subplot 5: Sensor Data (ACCl X, Y, Z)
        if segment_sensor_chunk is not None:
             ax5.plot(t_vector_sensor, segment_sensor_chunk[0, :], label='ACCl X', color='red', linewidth=0.8, alpha=0.8)
             ax5.plot(t_vector_sensor, segment_sensor_chunk[1, :], label='ACCl Y', color='green', linewidth=0.8, alpha=0.8)
             ax5.plot(t_vector_sensor, segment_sensor_chunk[2, :], label='ACCl Z', color='blue', linewidth=0.8, alpha=0.8)
             ax5.set_ylabel('Acc (g)')
             ax5.legend(loc='upper right')
             ax5.grid(True, alpha=0.3)
             ax5.set_title('Accelerometer (Sensor)')
        else:
             ax5.text(0.5, 0.5, 'No Sensor Data', ha='center', va='center')
        
        # Subplot 6: Behavioral Events & States
        ax6.set_ylim(0, 1)
        ax6.set_yticks([])
        ax6.set_ylabel('Events')
        
        # Mark Key States: 0, 3, 13, 14, 6, 2
        key_states = [0, 3, 13, 14, 6, 2]
        colors = ['gray', 'green', 'blue', 'cyan', 'magenta', 'red']
        
        # Mark Neural Pulse Alignment Point (on all plots)
        for ax in axes:
            ax.axvline(x=t_ref_ms, color='orange', linestyle='--', label='Neural Pulse')

        # Mark States
        for s_idx, state_id in enumerate(key_states):
            if state_id in trial_data.states:
                for ts in trial_data.states[state_id]:
                    if plot_start_ms <= ts <= plot_end_ms:
                        for ax in [ax1, ax2, ax3, ax4, ax5]:
                             ax.axvline(x=ts, color=colors[s_idx % len(colors)], linestyle=':', linewidth=1.0, alpha=0.5)
                        
                        ax6.axvline(x=ts, color=colors[s_idx % len(colors)], linestyle=':', linewidth=1.5, alpha=0.8)
                        ax6.text(ts, 0.8, str(state_id), color=colors[s_idx % len(colors)], rotation=90, verticalalignment='top')

        # Mark Events from Tevent.txt
        if trial_num in tevent_data_map:
            tevent = tevent_data_map[trial_num]
            event_colors = {0: 'black', 1: 'black', 8: 'magenta', 10: 'cyan'}
            event_labels = {0: 'S', 1: 'E', 8: 'LickL', 10: 'LickR'}
            event_markers = {0: '>', 1: '<', 8: 'o', 10: 'o'} 
            
            for ev_id, ev_ts in tevent.events:
                # Include events 0 and 1 as requested
                if ev_id in event_colors:
                    if plot_start_ms <= ev_ts <= plot_end_ms:
                        if ev_id in [0, 1]:
                            ax6.axvline(x=ev_ts, color=event_colors[ev_id], linestyle='-', linewidth=2.0, alpha=0.7)
                            ax6.text(ev_ts, 0.5, event_labels[ev_id], color=event_colors[ev_id], rotation=90, verticalalignment='center', fontweight='bold')
                        else:
                            y_pos = 0.4 if ev_id == 8 else 0.6
                            ax6.scatter([ev_ts], [y_pos], color=event_colors[ev_id], marker=event_markers[ev_id], s=40, label=event_labels[ev_id])
                            
        # Title
        title_str = (f"Trial {trial_num} (Real) | Type: {trial_data.trial_type} | Outcome: {trial_data.trial_outcome} | "
                     f"Stimu: {trial_data.curr_stimu} | EarlyLick: {trial_data.is_earlylick}")
        
        ax1.set_title(title_str)
        ax6.set_xlabel('Time from Trial Start (ms)')
        
        plt.tight_layout()
        
        # Save
        save_path = os.path.join(output_dir, f"Trial_{trial_num}.png")
        plt.savefig(save_path)
        plt.close()
        
    print(f"Alignment and Plotting Complete. Saved to {output_dir}")

if __name__ == "__main__":
    # Default file path from environment
    default_raw = r"F:\FormalData\Script_Data\Mode3_DemoData\2026-02-05-07-12-31mode3_raw.edf"
    default_lfp = r"F:\FormalData\Script_Data\Mode3_DemoData\2026-02-05-07-12-31LFP&ESA.edf"
    default_sensor = r"F:\FormalData\Script_Data\Mode3_DemoData\2026-02-05-07-12-31sensor.edf"
    
    # Check if user provided args
    # Usage: python analyze.py [raw_file] [lfp_file] [sensor_file]
    if len(sys.argv) > 1:
        raw_file = sys.argv[1]
    else:
        raw_file = default_raw
        
    if len(sys.argv) > 2:
        lfp_file = sys.argv[2]
    else:
        lfp_file = default_lfp
        
    if len(sys.argv) > 3:
        sensor_file = sys.argv[3]
    else:
        sensor_file = default_sensor
        
    if os.path.exists(raw_file):
        load_and_analyze_edf(raw_file, lfp_file, sensor_file)
    else:
        print(f"File not found: {raw_file}")
