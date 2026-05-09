import os
import re
import pyedflib
import numpy as np
from scipy import signal

LFP_FIRMWARE_ORIGINAL_FS = 12500.0
LFP_FIRMWARE_TARGET_FS = 1000.0
LFP_FIRMWARE_BIQUAD_A1 = 1.7874325179564847
LFP_FIRMWARE_BIQUAD_A2 = -0.8079495914209132
LFP_FIRMWARE_GAIN = 0.005129268366107147
LFP_FIRMWARE_B = LFP_FIRMWARE_GAIN * np.array([1.0, 2.0, 1.0], dtype=np.float64)
LFP_FIRMWARE_A = np.array(
    [1.0, -LFP_FIRMWARE_BIQUAD_A1, -LFP_FIRMWARE_BIQUAD_A2],
    dtype=np.float64,
)
LFP_PHASE_COMPENSATION_BAND_HZ = (4.0, 150.0)


def get_lfp_firmware_phase_response(freqs_hz):
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    omega = 2.0 * np.pi * freqs_hz / LFP_FIRMWARE_ORIGINAL_FS
    _, response = signal.freqz(LFP_FIRMWARE_B, LFP_FIRMWARE_A, worN=omega)
    return response


def get_lfp_firmware_group_delay(freqs_hz):
    freqs_hz = np.asarray(freqs_hz, dtype=np.float64)
    omega = 2.0 * np.pi * freqs_hz / LFP_FIRMWARE_ORIGINAL_FS
    _, gd_samples = signal.group_delay((LFP_FIRMWARE_B, LFP_FIRMWARE_A), w=omega)
    return (gd_samples / LFP_FIRMWARE_ORIGINAL_FS) * 1000.0


def get_default_lfp_phase_compensation_info():
    reference_freqs = np.linspace(
        LFP_PHASE_COMPENSATION_BAND_HZ[0],
        LFP_PHASE_COMPENSATION_BAND_HZ[1],
        256,
        dtype=np.float64,
    )
    group_delay_ms = get_lfp_firmware_group_delay(reference_freqs)
    return {
        "applied": True,
        "method": (
            "FFT phase-only compensation of firmware 2nd-order IIR low-pass "
            "response (12.5kHz acquisition, 300Hz cutoff) before trial slicing"
        ),
        "reference_band_hz": LFP_PHASE_COMPENSATION_BAND_HZ,
        "mean_group_delay_ms": float(np.mean(group_delay_ms)),
        "max_group_delay_ms": float(np.max(group_delay_ms)),
    }


def phase_compensate_lfp_matrix(lfp_data, fs_lfp, pad_seconds=2.0):
    lfp_data = np.asarray(lfp_data, dtype=np.float64)
    if lfp_data.ndim != 2:
        raise ValueError("lfp_data must be a 2D array with shape [channels, samples]")
    if lfp_data.shape[1] < 4:
        return np.array(lfp_data, copy=True)

    pad_samples = int(max(8, min(lfp_data.shape[1] - 1, round(fs_lfp * pad_seconds))))
    fft_len = lfp_data.shape[1] + 2 * pad_samples
    freqs = np.fft.rfftfreq(fft_len, d=1.0 / fs_lfp)
    phase_response = np.angle(get_lfp_firmware_phase_response(freqs))
    phase_correction = np.exp(-1j * phase_response)

    compensated = np.empty_like(lfp_data, dtype=np.float64)
    for ch in range(lfp_data.shape[0]):
        padded = np.pad(lfp_data[ch], (pad_samples, pad_samples), mode="reflect")
        spectrum = np.fft.rfft(padded)
        corrected = np.fft.irfft(spectrum * phase_correction, n=fft_len)
        compensated[ch] = corrected[pad_samples:pad_samples + lfp_data.shape[1]]

    return compensated.astype(lfp_data.dtype, copy=False)


def get_hardware_end_ms(file_path):
    try:
        f = pyedflib.EdfReader(file_path)
        header = f.getHeader()
        f.close()
        candidates = [header.get('recording_additional', ''), header.get('patientcode', '')]
        for remark in candidates:
            if not remark: continue
            match = re.search(r'timestamp\s*[:=]?\s*(\d+)', remark, re.IGNORECASE)
            if match: return float(match.group(1))
            matches = re.findall(r'(\d+)', remark)
            if matches: return float(matches[-1])
        return 0
    except Exception:
        return 0


def read_edf_file(file_path, apply_phase_compensation=False, is_lfp=False, missing_threshold=-1000):
    """
    Reads an arbitrary EDF file, cleans it by replacing threshold values with NaN,
    optionally applies phase compensation, and returns data and metadata.
    """
    hw_end_ms = get_hardware_end_ms(file_path)
    try:
        f = pyedflib.EdfReader(file_path)
    except Exception as e:
        return None, None
        
    n_channels = f.signals_in_file
    if n_channels == 0:
        f.close()
        return None, None
        
    labels = f.getSignalLabels()
    fs = f.getSampleFrequency(0)
    n_samples = f.getNSamples()[0]
    
    duration_s = n_samples / fs
    hw_start_ms = hw_end_ms - (duration_s * 1000.0) if hw_end_ms > 0 else 0
    
    data_list = []
    for ch in range(n_channels):
        d = f.readSignal(ch)
        data_list.append(d)
        
    f.close()
    data_matrix = np.array(data_list)
    
    # Option B chosen by user: Replace dropped network data (<= threshold) with NaN rather than interpolate
    # We will do this uniformly.
    missing_mask = (data_matrix <= missing_threshold)
    data_matrix = data_matrix.astype(np.float64) # Ensure float so we can put NaN
    data_matrix[missing_mask] = np.nan
    
    # If this is LFP and we are requesting phase compensation
    if is_lfp and apply_phase_compensation and data_matrix.size > 0:
        # Before FFT phase compensation, NaN values must be temporarily replaced 
        # (e.g. interpolate) structurally just for the FFT, but we will put NaN back afterwards.
        temp_data = np.copy(data_matrix)
        for ch in range(temp_data.shape[0]):
            bad = np.isnan(temp_data[ch])
            if np.any(bad) and np.any(~bad):
                valid = ~bad
                x = np.arange(len(temp_data[ch]))
                temp_data[ch][bad] = np.interp(x[bad], x[valid], temp_data[ch][valid])
            elif np.all(bad):
                temp_data[ch][:] = 0
                
        data_matrix = phase_compensate_lfp_matrix(temp_data, fs)
        # Restore NaNs
        data_matrix[missing_mask] = np.nan
        
    metadata = {
        'labels': labels,
        'fs': fs,
        'n_samples': n_samples,
        'hw_start_ms': hw_start_ms,
        'hw_end_ms': hw_end_ms,
        'duration_s': duration_s,
        'source_file': file_path
    }
    
    return data_matrix, metadata
