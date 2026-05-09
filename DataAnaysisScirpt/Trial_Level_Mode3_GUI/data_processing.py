import os
import re
import sys
import json
from collections import defaultdict

import numpy as np
import pyedflib
from scipy import signal

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from Common_Analysis.edf_io import (
    get_default_lfp_phase_compensation_info,
    get_hardware_end_ms,
    phase_compensate_lfp_matrix,
)
from Common_Analysis.channel_map import (
    CHANNEL_ORDER_SEMANTICS,
    MODE3_SHALLOW_TO_DEEP_CHANNELS,
    reorder_lfp_to_unified_probe_order,
)
from Common_Analysis.trial_parsing import parse_tevent_file, parse_trial_file


ALIGNMENT_ROI_WINDOW_MS = (-300.0, -100.0)
ALIGNMENT_PULSE_FREQ_HZ = 4000.0
ALIGNMENT_BANDPASS_HZ = (3500.0, 4500.0)
ALIGNMENT_CHIP_PATTERN = "111001011"
ALIGNMENT_CHIP_MS = 1.0
ALIGNMENT_RESIDUAL_WARNING_MS = 5.0
MISSING_THRESHOLD = -1000.0
SIDECAR_FILENAME = "mode3_alignment_annotations.json"


def get_end_timestamp(file_path):
    return get_hardware_end_ms(file_path)


def _finite_fill_1d(data, fill_value=0.0):
    arr = np.asarray(data, dtype=np.float64).copy()
    bad = ~np.isfinite(arr)
    if not np.any(bad):
        return arr
    valid = ~bad
    if np.any(valid):
        x = np.arange(arr.size)
        arr[bad] = np.interp(x[bad], x[valid], arr[valid])
    else:
        arr[:] = fill_value
    return arr


def _continuous_with_nan(data, threshold=MISSING_THRESHOLD):
    arr = np.asarray(data, dtype=np.float64)
    out = arr.copy()
    out[out <= threshold] = np.nan
    return out


def _bandpass_for_display(data, fs, band_hz=ALIGNMENT_BANDPASS_HZ):
    arr = _finite_fill_1d(data)
    nyquist = 0.5 * float(fs)
    low, high = float(band_hz[0]), float(band_hz[1])
    if low <= 0 or high >= nyquist or low >= high or arr.size < 8:
        return arr
    sos = signal.butter(4, [low, high], btype="bandpass", fs=float(fs), output="sos")
    filtered = signal.sosfiltfilt(sos, arr)
    nan_mask = ~np.isfinite(data)
    filtered[nan_mask] = np.nan
    return filtered


def _raw_fingerprint(path):
    stat = os.stat(path)
    return {
        "path": os.path.abspath(path),
        "size": int(stat.st_size),
        "mtime_ns": int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9))),
    }


def _fingerprint_matches(path, fingerprint):
    if not fingerprint:
        return False
    current = _raw_fingerprint(path)
    return (
        os.path.abspath(path) == fingerprint.get("path")
        and current["size"] == int(fingerprint.get("size", -1))
        and current["mtime_ns"] == int(fingerprint.get("mtime_ns", -1))
    )


def find_raw_channels(reader):
    labels = [label.strip().lower() for label in reader.getSignalLabels()]
    raw_idx = 0
    align_idx = 2 if reader.signals_in_file > 2 else -1
    for idx, label in enumerate(labels):
        if "rawdata" in label:
            raw_idx = idx
        if "alignment" in label:
            align_idx = idx
    if align_idx < 0:
        raise ValueError("Raw EDF is missing an Alignment channel.")
    return raw_idx, align_idx


def alignment_roi_sample_bounds(edge_sample, n_samples, fs_raw, roi_window_ms=ALIGNMENT_ROI_WINDOW_MS):
    roi_start = int(max(0, round(edge_sample + roi_window_ms[0] * fs_raw / 1000.0)))
    roi_end = int(min(n_samples, round(edge_sample + roi_window_ms[1] * fs_raw / 1000.0)))
    return roi_start, roi_end


def build_alignment_candidates(raw_path, trials=None, roi_window_ms=ALIGNMENT_ROI_WINDOW_MS):
    reader = pyedflib.EdfReader(raw_path)
    raw_idx, align_idx = find_raw_channels(reader)
    fs_raw = float(reader.getSampleFrequency(raw_idx))
    raw_data = _continuous_with_nan(reader.readSignal(raw_idx))
    raw_align = np.asarray(reader.readSignal(align_idx), dtype=np.float64)
    labels = reader.getSignalLabels()
    reader.close()

    align_binary = (raw_align > 0.5).astype(np.int8)
    rising_edges = np.where(np.diff(align_binary, prepend=0) == 1)[0]
    candidates = []
    for edge_sample in rising_edges:
        trial_sample_idx = min(int(edge_sample + round(0.001 * fs_raw)), len(raw_align) - 1)
        trial_id = int(np.round(raw_align[trial_sample_idx]))
        trial = trials.get(trial_id) if trials else None
        roi_start, roi_end = alignment_roi_sample_bounds(edge_sample, len(raw_data), fs_raw, roi_window_ms)
        if roi_end <= roi_start:
            continue
        candidates.append(
            {
                "trial_id": trial_id,
                "alignment_sample": int(edge_sample),
                "roi_start_sample": roi_start,
                "roi_end_sample": roi_end,
                "trial_start_timestamp_ms": (
                    getattr(trial, "trial_start_timestamp_ms", None) if trial is not None else None
                ),
                "has_trial": trial is not None,
                "has_timestamp": (
                    trial is not None and getattr(trial, "trial_start_timestamp_ms", None) is not None
                ),
            }
        )

    raw_filtered = _bandpass_for_display(raw_data, fs_raw)
    return {
        "raw_path": os.path.abspath(raw_path),
        "fs_raw": fs_raw,
        "raw_data": raw_data,
        "raw_filtered": raw_filtered,
        "raw_align": raw_align,
        "labels": labels,
        "candidates": candidates,
        "roi_window_ms": list(roi_window_ms),
        "pulse_metadata": {
            "frequency_hz": ALIGNMENT_PULSE_FREQ_HZ,
            "bandpass_hz": list(ALIGNMENT_BANDPASS_HZ),
            "chip_pattern": ALIGNMENT_CHIP_PATTERN,
            "chip_ms": ALIGNMENT_CHIP_MS,
        },
        "fingerprint": _raw_fingerprint(raw_path),
    }


def fit_alignment_anchors(anchors, fs_raw):
    valid = []
    for anchor in anchors or []:
        timestamp = anchor.get("trial_start_timestamp_ms")
        sample = anchor.get("pulse_sample")
        if timestamp is None or sample is None:
            continue
        valid.append(
            {
                **anchor,
                "trial_start_timestamp_ms": float(timestamp),
                "pulse_sample": float(sample),
            }
        )
    if not valid:
        raise ValueError("At least one manual alignment anchor is required.")

    timestamps = np.asarray([item["trial_start_timestamp_ms"] for item in valid], dtype=np.float64)
    samples = np.asarray([item["pulse_sample"] for item in valid], dtype=np.float64)
    if len(valid) == 1:
        slope = float(fs_raw) / 1000.0
        offset = float(samples[0] - slope * timestamps[0])
        method = "single_anchor_fixed_slope"
    else:
        slope, offset = np.polyfit(timestamps, samples, 1)
        slope = float(slope)
        offset = float(offset)
        method = "linear_fit"

    predicted = slope * timestamps + offset
    residual_samples = predicted - samples
    samples_per_ms = slope if abs(slope) > 1e-9 else (float(fs_raw) / 1000.0)
    residual_ms = residual_samples / samples_per_ms
    return {
        "method": method,
        "slope_samples_per_ms": slope,
        "offset_samples": offset,
        "n_anchors": len(valid),
        "rms_residual_ms": float(np.sqrt(np.mean(residual_ms ** 2))) if len(valid) else 0.0,
        "max_abs_residual_ms": float(np.max(np.abs(residual_ms))) if len(valid) else 0.0,
        "anchors": valid,
        "warning_threshold_ms": ALIGNMENT_RESIDUAL_WARNING_MS,
    }


def predict_trial_start_sample(fit, trial_start_timestamp_ms):
    return (
        float(fit["slope_samples_per_ms"]) * float(trial_start_timestamp_ms)
        + float(fit["offset_samples"])
    )


def load_alignment_sidecar(output_dir):
    path = os.path.join(output_dir, SIDECAR_FILENAME)
    if not os.path.exists(path):
        return {"version": 1, "raw_files": {}}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("version", 1)
    data.setdefault("raw_files", {})
    return data


def save_alignment_sidecar(output_dir, sidecar):
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, SIDECAR_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(sidecar, handle, indent=2, ensure_ascii=False)
    return path


def get_cached_alignment_entry(sidecar, raw_path):
    raw_key = os.path.abspath(raw_path)
    entry = sidecar.get("raw_files", {}).get(raw_key)
    if not entry or not _fingerprint_matches(raw_path, entry.get("fingerprint")):
        return None
    if not entry.get("fit") or not entry.get("anchors"):
        return None
    return entry


def make_alignment_sidecar_entry(review, anchors, fit):
    serializable_anchors = []
    for anchor in anchors:
        serializable_anchors.append(
            {
                "trial_id": int(anchor["trial_id"]),
                "trial_start_timestamp_ms": float(anchor["trial_start_timestamp_ms"]),
                "alignment_sample": int(anchor["alignment_sample"]),
                "pulse_sample": float(anchor["pulse_sample"]),
            }
        )
    return {
        "fingerprint": review["fingerprint"],
        "anchors": serializable_anchors,
        "fit": fit,
        "roi_window_ms": review["roi_window_ms"],
        "pulse_pattern": review["pulse_metadata"],
        "fs_raw": float(review["fs_raw"]),
    }

class DataProcessor:
    def __init__(self, apply_lfp_phase_compensation=True):
        self.trials = {}
        self.tevents = {}
        self.trial_dict = {}
        self.apply_lfp_phase_compensation = apply_lfp_phase_compensation
        self.lfp_phase_compensation_info = get_default_lfp_phase_compensation_info()
        self.lfp_phase_compensation_info["applied"] = bool(apply_lfp_phase_compensation)

    def prepare_alignment_review(self, raw_file, trial_file=None, trials=None):
        if trials is None:
            trials = parse_trial_file(trial_file) if trial_file else self.trials
        return build_alignment_candidates(raw_file, trials=trials)

    def process_files(self, file1, file2, file3, file4, file5, suppress_parse=False, alignment_fit=None):
        # file1: LFP_ESA, file2: Raw, file3: Sensor, file4: Trial.txt, file5: Tevent.txt
        if not suppress_parse:
            self.trials = parse_trial_file(file4)
            self.tevents = parse_tevent_file(file5)

        if alignment_fit is None:
            raise ValueError("Manual alignment fit is required for Trial-Level Mode3 processing.")

        missing_trials_in_chunk = []

        f_raw = pyedflib.EdfReader(file2)
        raw_idx, align_idx = find_raw_channels(f_raw)
        fs_raw = float(f_raw.getSampleFrequency(raw_idx))
        n_samples_raw = int(f_raw.getNSamples()[raw_idx])
        raw_data = _continuous_with_nan(f_raw.readSignal(raw_idx))
        raw_align = np.asarray(f_raw.readSignal(align_idx), dtype=np.float64)
        f_raw.close()
        raw_filtered = _bandpass_for_display(raw_data, fs_raw)

        ts_raw = get_end_timestamp(file2)
        ts_lfp = get_end_timestamp(file1)
        ts_sen = get_end_timestamp(file3)

        dur_raw = (n_samples_raw / fs_raw) * 1000.0
        start_raw = ts_raw - dur_raw

        f_lfp = pyedflib.EdfReader(file1)
        fs_lfp = float(f_lfp.getSampleFrequency(0))
        lfp_samples = int(f_lfp.getNSamples()[0])
        lfp_offset_ms = (ts_lfp - (lfp_samples / fs_lfp) * 1000.0) - start_raw

        lfp_data = []
        esa_data = []
        raster_data = []
        for ch in range(f_lfp.signals_in_file):
            label = f_lfp.getLabel(ch)
            channel_data = f_lfp.readSignal(ch)
            if ch < 16:
                lfp_data.append(_continuous_with_nan(channel_data))
            elif 16 <= ch < 32:
                esa_data.append(_continuous_with_nan(channel_data))
            elif label.startswith("Raster"):
                discrete = np.asarray(channel_data, dtype=np.float64)
                discrete[discrete <= MISSING_THRESHOLD] = 0.0
                raster_data.append(discrete)

        lfp_data = np.array(lfp_data)
        esa_data = np.array(esa_data) if len(esa_data) > 0 else None
        raster_data = np.array(raster_data) if len(raster_data) > 0 else None

        if lfp_data.size > 0:
            lfp_data = reorder_lfp_to_unified_probe_order(lfp_data, "mode3_lfp")
        if esa_data is not None and esa_data.size > 0:
            esa_data = reorder_lfp_to_unified_probe_order(esa_data, "mode3_lfp")
        if raster_data is not None and raster_data.size > 0:
            raster_data = reorder_lfp_to_unified_probe_order(raster_data, "mode3_lfp", fill_value=0.0)

        if self.apply_lfp_phase_compensation and lfp_data.size > 0:
            missing_mask = ~np.isfinite(lfp_data)
            lfp_temp = np.empty_like(lfp_data, dtype=np.float64)
            for ch in range(lfp_data.shape[0]):
                lfp_temp[ch] = _finite_fill_1d(lfp_data[ch])
            lfp_data = phase_compensate_lfp_matrix(lfp_temp, fs_lfp)
            lfp_data[missing_mask] = np.nan

        f_lfp.close()

        f_sen = pyedflib.EdfReader(file3)
        fs_sen = float(f_sen.getSampleFrequency(0))
        sen_samples = int(f_sen.getNSamples()[0])
        sen_offset_ms = (ts_sen - (sen_samples / fs_sen) * 1000.0) - start_raw
        sen_data = []
        for ch in range(f_sen.signals_in_file):
            sen_data.append(_continuous_with_nan(f_sen.readSignal(ch)))
        sen_data = np.array(sen_data)
        sen_labels = f_sen.getSignalLabels()
        f_sen.close()

        self.trial_dict = {}
        for trial_id, trial in sorted(self.trials.items()):
            trial_start_timestamp_ms = getattr(trial, "trial_start_timestamp_ms", None)
            if trial_start_timestamp_ms is None:
                missing_trials_in_chunk.append(f"Trial {trial_id} has no TrialStartTimestampMs; skipped.")
                continue
            sync_sample = predict_trial_start_sample(alignment_fit, trial_start_timestamp_ms)
            if sync_sample < 0 or sync_sample >= n_samples_raw:
                continue
            if getattr(trial, "start_ts", 0) == 0:
                continue

            self.trial_dict[trial_id] = {
                "trial_obj": trial,
                "tevent_obj": self.tevents.get(trial_id, None),
                "sync_sample_raw": float(sync_sample),
                "trial_start_timestamp_ms": float(trial_start_timestamp_ms),
                "raw": raw_data,
                "raw_filt": raw_filtered,
                "raw_align": raw_align,
                "fs_raw": fs_raw,
                "lfp": lfp_data,
                "esa": esa_data,
                "raster": raster_data,
                "fs_lfp": fs_lfp,
                "lfp_offset_ms": lfp_offset_ms,
                "lfp_phase_compensation": dict(self.lfp_phase_compensation_info),
                "sen": sen_data,
                "sen_labels": sen_labels,
                "fs_sen": fs_sen,
                "sen_offset_ms": sen_offset_ms,
                "alignment_fit": dict(alignment_fit),
            }

        self.last_missing_log = missing_trials_in_chunk
        return self.trial_dict

    def get_trial_data(self, trial_id, pre_ms=4000, post_ms=None):
        # Given a trial ID, extract numpy arrays of time & data dynamically based on the trial interval 
        if trial_id not in self.trial_dict: return None
        td = self.trial_dict[trial_id]
        
        trial = td['trial_obj']
        duration = trial.end_ts - trial.start_ts if trial.end_ts > trial.start_ts else 5000 # default 5s if missing
        
        # Extrapolate +-4s boundaries by default.
        if post_ms is None:
            post_ms = duration + 4000
        
        sync_sample = float(td['sync_sample_raw'])
        fs_raw = td['fs_raw']
        
        # TrialStart in raw absolute time (Centered exactly on the Neural Sync Pulse)
        raw_time_pulse = sync_sample * 1000.0 / fs_raw
        trial_start_abs = raw_time_pulse
        
        # Raw slicing
        raw_start_ideal = int(round(sync_sample + (-pre_ms)/1000.0 * fs_raw))
        raw_end_ideal = int(round(sync_sample + (post_ms)/1000.0 * fs_raw))
        raw_start = max(0, raw_start_ideal); raw_end = min(len(td['raw_filt']), raw_end_ideal)
        
        raw_v = td['raw_filt'][raw_start:raw_end] 
        raw_align_v = td['raw_align'][raw_start:raw_end]
        raw_t = (np.arange(raw_start, raw_end) - sync_sample) * 1000.0 / fs_raw
        
        # LFP & Raster Slicing
        lfp_target_start = raw_time_pulse + (-pre_ms) - td['lfp_offset_ms']
        lfp_target_end = raw_time_pulse + (post_ms) - td['lfp_offset_ms']
        
        lfp_start_ideal = int(lfp_target_start * td['fs_lfp'] / 1000.0)
        lfp_end_ideal = int(lfp_target_end * td['fs_lfp'] / 1000.0)
        lfp_start = max(0, lfp_start_ideal); lfp_end = min(td['lfp'].shape[1], lfp_end_ideal)
        
        lfp_v = td['lfp'][:, lfp_start:lfp_end]
        lfp_t = (np.arange(lfp_start, lfp_end) * 1000.0 / td['fs_lfp'] + td['lfp_offset_ms']) - trial_start_abs
        
        esa_v = None
        if td['esa'] is not None:
            esa_end_e = min(td['esa'].shape[1], lfp_end)
            esa_v = td['esa'][:, lfp_start:esa_end_e]
        
        raster_v = None
        if td['raster'] is not None:
            lfp_end_r = min(td['raster'].shape[1], lfp_end)
            raster_v = td['raster'][:, lfp_start:lfp_end_r]
            
        # Sen Slicing
        sen_target_start = raw_time_pulse + (-pre_ms) - td['sen_offset_ms']
        sen_target_end = raw_time_pulse + (post_ms) - td['sen_offset_ms']
        
        sen_start_ideal = int(sen_target_start * td['fs_sen'] / 1000.0)
        sen_end_ideal = int(sen_target_end * td['fs_sen'] / 1000.0)
        sen_start = max(0, sen_start_ideal); sen_end = min(td['sen'].shape[1], sen_end_ideal)
        
        sen_v = td['sen'][:, sen_start:sen_end]
        sen_t = (np.arange(sen_start, sen_end) * 1000.0 / td['fs_sen'] + td['sen_offset_ms']) - trial_start_abs
        
        return {
             'raw': (raw_t, raw_v, raw_align_v),
             'lfp': (lfp_t, lfp_v),
             'esa': (lfp_t[:esa_v.shape[1]] if esa_v is not None else lfp_t, esa_v),
             'raster': (lfp_t[:raster_v.shape[1]] if raster_v is not None else lfp_t, raster_v),
             'sen': (sen_t, sen_v, td['sen_labels']),
             'trial_start_ts': trial.start_ts,
             'trial_start_timestamp_ms': td.get('trial_start_timestamp_ms'),
             'alignment_fit': dict(td.get('alignment_fit', {})),
             'tevent': td['tevent_obj'],
             'states': trial.states_history,
             'lfp_phase_compensation': dict(td.get('lfp_phase_compensation', {})),
        }

    def get_extracted_data(self, trial_id):
        if hasattr(self, 'extracted_cache') and trial_id in self.extracted_cache:
            return self.extracted_cache[trial_id]
        if isinstance(trial_id, int) or str(trial_id).isdigit():
            return self.get_trial_data(int(trial_id))
        return None

    def load_from_h5(self, filepath):
        import h5py
        import ast
        import json
        import types
        self.extracted_cache = {}
        self.h5_metadata = {}
        with h5py.File(filepath, 'r') as f:
            self.h5_metadata = {key: f.attrs[key] for key in f.attrs.keys()}
            for key in f.keys():
                grp = f[key]
                tdata = {}

                # Reload raw arrays
                tdata['raw'] = (
                    np.array(grp['raw_t']),
                    np.array(grp['raw_v']),
                    np.array(grp['raw_align_v'])
                )

                # Reload LFP
                tdata['lfp'] = (np.array(grp['lfp_t']), np.array(grp['lfp_v']))

                # Reload ESA
                if 'esa_v' in grp:
                    esa_t = np.array(grp['esa_t']) if 'esa_t' in grp else np.array(grp['lfp_t'])
                    tdata['esa'] = (esa_t, np.array(grp['esa_v']))
                else:
                    tdata['esa'] = (np.array(grp['lfp_t']), None)

                # Reload raster
                if 'raster_v' in grp:
                    tdata['raster'] = (np.array(grp['lfp_t']), np.array(grp['raster_v']))
                else:
                    tdata['raster'] = (np.array(grp['lfp_t']), None)

                # Reload sensor
                labels_attr = grp.attrs.get('sen_labels', "[]")
                try:
                    sen_labels = json.loads(labels_attr)
                except Exception:
                    sen_labels = ast.literal_eval(labels_attr)
                tdata['sen'] = (
                    np.array(grp['sen_t']),
                    np.array(grp['sen_v']),
                    sen_labels
                )

                # Reload metadata
                tdata['trial_start_ts'] = float(grp.attrs['trial_start_ts'])
                if 'trial_start_timestamp_ms' in grp.attrs:
                    tdata['trial_start_timestamp_ms'] = float(grp.attrs['trial_start_timestamp_ms'])
                else:
                    tdata['trial_start_timestamp_ms'] = None
                tdata['alignment_fit'] = json.loads(grp.attrs.get('alignment_fit_json', '{}'))
                tdata['channel_order'] = {
                    'semantics': grp.attrs.get('channel_order_semantics', ''),
                    'mode3_source_channels_shallow_to_deep': json.loads(
                        grp.attrs.get('mode3_source_channels_shallow_to_deep', '[]')
                    ),
                }
                tdata['states'] = json.loads(grp.attrs['states'])
                tdata['lfp_phase_compensation'] = {
                    'applied': bool(grp.attrs.get('lfp_phase_compensation_applied', False)),
                    'method': grp.attrs.get('lfp_phase_compensation_method', ''),
                    'mean_group_delay_ms': float(grp.attrs.get('lfp_phase_compensation_mean_group_delay_ms', 0.0)),
                    'max_group_delay_ms': float(grp.attrs.get('lfp_phase_compensation_max_group_delay_ms', 0.0)),
                }

                # Reload behavioral events mock object
                tevent_obj = types.SimpleNamespace()
                tevent_obj.events = json.loads(grp.attrs.get('tevent_list', '[]'))
                tdata['tevent'] = tevent_obj

                self.extracted_cache[key] = tdata

        return sorted(list(self.extracted_cache.keys()))

    def _scan_mode3_groups(self, neural_dir):
        prefix_pattern = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2})")
        groups = {}
        for dirpath, _, filenames in os.walk(neural_dir):
            for fname in filenames:
                match = prefix_pattern.search(fname)
                if not match:
                    continue
                prefix = match.group(1)
                groups.setdefault(prefix, []).append(os.path.join(dirpath, fname))

        valid_groups = {}
        target_suffixes = ["LFP&ESA.edf", "mode3_raw.edf", "sensor.edf"]
        missing_log = []
        for prefix, files in groups.items():
            mapped = {}
            for fpath in files:
                fname = os.path.basename(fpath)
                for suffix in target_suffixes:
                    if fname.endswith(suffix):
                        mapped[suffix] = fpath
            if len(mapped) == 3:
                valid_groups[prefix] = mapped
            else:
                missing_log.append(
                    f"Warning: Group {prefix} is missing components. Found suffixes: {list(mapped.keys())}"
                )
        return groups, valid_groups, missing_log

    def batch_process_directories(
        self,
        neural_dir,
        trial_file,
        tevent_file=None,
        output_dir=None,
        annotation_provider=None,
    ):
        if tevent_file is None and os.path.isdir(trial_file):
            behavior_dir = trial_file
            trial_file = os.path.join(behavior_dir, "Trial.txt")
            tevent_file = os.path.join(behavior_dir, "Tevent.txt")

        missing_log = []
        if not trial_file or not os.path.exists(trial_file):
            missing_log.append(f"CRITICAL: Trial.txt not found: {trial_file}")
        if not tevent_file or not os.path.exists(tevent_file):
            missing_log.append(f"CRITICAL: Tevent.txt not found: {tevent_file}")
            
        if len(missing_log) > 0:
            return None, {}, missing_log

        groups, valid_groups, scan_logs = self._scan_mode3_groups(neural_dir)
        missing_log.extend(scan_logs)
        all_extracted_trials_by_day = defaultdict(dict)
        total_trials_aligned = 0
        processed_groups = 0

        self.trials = parse_trial_file(trial_file)
        self.tevents = parse_tevent_file(tevent_file)

        sidecar = load_alignment_sidecar(output_dir) if output_dir else {"version": 1, "raw_files": {}}
        for prefix, paths in sorted(valid_groups.items()):
            raw_path = paths["mode3_raw.edf"]
            entry = get_cached_alignment_entry(sidecar, raw_path)
            if entry is None:
                review = self.prepare_alignment_review(raw_path, trials=self.trials)
                if annotation_provider is None:
                    missing_log.append(f"[{prefix}] Manual alignment is missing; skipped.")
                    continue
                entry = annotation_provider(review)
                if entry is None:
                    missing_log.append(f"[{prefix}] Manual alignment cancelled or empty; skipped.")
                    continue
                sidecar.setdefault("raw_files", {})[os.path.abspath(raw_path)] = entry
                if output_dir:
                    save_alignment_sidecar(output_dir, sidecar)

            try:
                dict_segment = self.process_files(
                    paths["LFP&ESA.edf"], 
                    raw_path,
                    paths["sensor.edf"],
                    trial_file, 
                    tevent_file,
                    suppress_parse=True,
                    alignment_fit=entry["fit"],
                )

                if hasattr(self, 'last_missing_log') and self.last_missing_log:
                    missing_log.extend([f"[{prefix}] {msg}" for msg in self.last_missing_log])
                processed_groups += 1
                    
            except Exception as e:
                missing_log.append(f"[{prefix}] Error processing group: {str(e)}")
                continue
                
            for t_id in dict_segment.keys():
                extracted = self.get_trial_data(t_id)
                if extracted is not None:
                    composite_key = f"{prefix}_Trial_{t_id}"
                    date_key = prefix[:10]
                    all_extracted_trials_by_day[date_key][composite_key] = extracted
                    total_trials_aligned += 1
                else:
                    missing_log.append(f"[{prefix}] Warning: Extracted trial {t_id} failed during data slicing")

        output_files = {}
        if output_dir:
            for date_key, trials in sorted(all_extracted_trials_by_day.items()):
                if not trials:
                    continue
                out_path = os.path.join(output_dir, f"{date_key}_Batch_Aligned_Trials.h5")
                save_trials_to_h5(trials, out_path)
                output_files[date_key] = out_path

        stats = {
            "total_groups_found": len(groups),
            "valid_groups_found": len(valid_groups),
            "valid_groups_processed": processed_groups,
            "total_trials_aligned": total_trials_aligned,
            "output_files": output_files,
        }

        if output_dir:
            return output_files, stats, missing_log
        merged = {}
        for trials in all_extracted_trials_by_day.values():
            merged.update(trials)
        return merged, stats, missing_log

def save_trials_to_h5(trials_dict, filepath):
    import h5py

    pulse_metadata = {
        "frequency_hz": ALIGNMENT_PULSE_FREQ_HZ,
        "bandpass_hz": list(ALIGNMENT_BANDPASS_HZ),
        "chip_pattern": ALIGNMENT_CHIP_PATTERN,
        "chip_ms": ALIGNMENT_CHIP_MS,
    }

    with h5py.File(filepath, 'w') as f:
        f.attrs['Prompt_Description'] = (
            "Mode 3 Continuous Neural/Behavior Aligned Data. "
            "Structure: Root -> [Trial_ID] -> Group components. "
            "Components include 'raw' [time, voltage, sync_index], "
            "'lfp' [time, 16_ch_matrix_data], 'esa' [time, 16_ch_1kHz_analog_esa_data], "
            "'raster' [time, 16_ch_binary], "
            "and 'sen' [time, sensor_matrix]. "
            "States and Tevents are saved as JSON attributes."
        )
        f.attrs['alignment_version'] = 'manual_anchor_timestamp_linear_fit_v1'
        f.attrs['alignment_pulse_metadata_json'] = json.dumps(pulse_metadata)
        f.attrs['alignment_sidecar_filename'] = SIDECAR_FILENAME
        f.attrs['continuous_missing_value_policy'] = (
            f"continuous values <= {MISSING_THRESHOLD:g} are stored as NaN; "
            "alignment and raster channels remain discrete"
        )
        f.attrs['channel_order_semantics'] = CHANNEL_ORDER_SEMANTICS
        f.attrs['mode3_source_channels_shallow_to_deep'] = json.dumps(MODE3_SHALLOW_TO_DEEP_CHANNELS)

        if trials_dict:
            first_trial = next(iter(trials_dict.values()))
            lfp_phase_meta = first_trial.get('lfp_phase_compensation', {})
            f.attrs['lfp_phase_compensation_applied'] = bool(lfp_phase_meta.get('applied', False))
            f.attrs['lfp_phase_compensation_method'] = str(lfp_phase_meta.get('method', ''))
            f.attrs['lfp_phase_compensation_mean_group_delay_ms'] = float(
                lfp_phase_meta.get('mean_group_delay_ms', 0.0)
            )
            f.attrs['lfp_phase_compensation_max_group_delay_ms'] = float(
                lfp_phase_meta.get('max_group_delay_ms', 0.0)
            )

        for tr_key, tdata in trials_dict.items():
            grp = f.create_group(str(tr_key))

            raw_t, raw_v, raw_align_v = tdata['raw']
            grp.create_dataset('raw_t', data=raw_t, compression="gzip")
            grp.create_dataset('raw_v', data=raw_v, compression="gzip")
            grp.create_dataset('raw_align_v', data=raw_align_v, compression="gzip")

            lfp_t, lfp_v = tdata['lfp']
            grp.create_dataset('lfp_t', data=lfp_t, compression="gzip")
            grp.create_dataset('lfp_v', data=lfp_v, compression="gzip")

            esa_t_e, esa_v = tdata['esa']
            if esa_v is not None:
                grp.create_dataset('esa_t', data=esa_t_e, compression="gzip")
                grp.create_dataset('esa_v', data=esa_v, compression="gzip")

            _, raster_v = tdata['raster']
            if raster_v is not None:
                grp.create_dataset('raster_v', data=raster_v, compression="gzip")

            sen_t, sen_v, sen_labels = tdata['sen']
            grp.create_dataset('sen_t', data=sen_t, compression="gzip")
            grp.create_dataset('sen_v', data=sen_v, compression="gzip")
            grp.attrs['sen_labels'] = json.dumps(list(sen_labels))

            grp.attrs['trial_start_ts'] = tdata['trial_start_ts']
            trial_start_timestamp_ms = tdata.get('trial_start_timestamp_ms')
            if trial_start_timestamp_ms is not None:
                grp.attrs['trial_start_timestamp_ms'] = float(trial_start_timestamp_ms)
            grp.attrs['alignment_fit_json'] = json.dumps(tdata.get('alignment_fit', {}))
            grp.attrs['channel_order_semantics'] = CHANNEL_ORDER_SEMANTICS
            grp.attrs['mode3_source_channels_shallow_to_deep'] = json.dumps(MODE3_SHALLOW_TO_DEEP_CHANNELS)

            lfp_phase_meta = tdata.get('lfp_phase_compensation', {})
            grp.attrs['lfp_phase_compensation_applied'] = bool(lfp_phase_meta.get('applied', False))
            grp.attrs['lfp_phase_compensation_method'] = str(lfp_phase_meta.get('method', ''))
            grp.attrs['lfp_phase_compensation_mean_group_delay_ms'] = float(
                lfp_phase_meta.get('mean_group_delay_ms', 0.0)
            )
            grp.attrs['lfp_phase_compensation_max_group_delay_ms'] = float(
                lfp_phase_meta.get('max_group_delay_ms', 0.0)
            )

            grp.attrs['states'] = json.dumps(tdata['states'])
            tevent = tdata['tevent']
            if tevent is not None:
                grp.attrs['tevent_list'] = json.dumps(tevent.events)
            else:
                grp.attrs['tevent_list'] = '[]'
