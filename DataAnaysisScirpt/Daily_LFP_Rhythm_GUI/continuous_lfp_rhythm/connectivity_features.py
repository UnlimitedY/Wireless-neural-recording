import json
import math
import os
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")

import h5py
import numpy as np
from scipy import signal

from .config import resolve_config


CONNECTIVITY_VERSION = 1
DEFAULT_TARGET_FS_HZ = 250.0
DEFAULT_PHASE_BINS = 18


def expected_connectivity_sidecar_paths(h5_path, output_dir):
    output_dir = os.path.abspath(str(output_dir or os.getcwd()))
    base = os.path.splitext(os.path.basename(str(h5_path)))[0]
    return {
        "npz": os.path.join(output_dir, f"{base}_connectivity_features.npz"),
        "json": os.path.join(output_dir, f"{base}_connectivity_features.json"),
    }


def ensure_connectivity_features(h5_path, output_dir, config=None, force=False, progress_callback=None):
    paths = expected_connectivity_sidecar_paths(h5_path, output_dir)
    if not force and os.path.exists(paths["npz"]) and os.path.exists(paths["json"]):
        try:
            with open(paths["json"], "r", encoding="utf-8") as handle:
                meta = json.load(handle)
            if int(meta.get("version", 0)) == CONNECTIVITY_VERSION:
                return {"paths": paths, "metadata": meta, "reused": True}
        except Exception:
            pass
    result = compute_hourly_connectivity_features(
        h5_path,
        config=config,
        progress_callback=progress_callback,
    )
    write_connectivity_sidecar(result, paths["npz"], paths["json"])
    return {"paths": paths, "metadata": result["metadata"], "reused": False}


def compute_hourly_connectivity_features(h5_path, config=None, progress_callback=None):
    cfg = resolve_config(config)
    bands = _select_connectivity_bands(cfg.get("features.bands", {}))
    target_fs = float(cfg.get("connectivity.target_fs_hz", DEFAULT_TARGET_FS_HZ))
    valid_fraction_threshold = float(cfg.get("missing_data.valid_fraction_threshold", 0.8))
    phase_bins = int(cfg.get("connectivity.pac_phase_bins", DEFAULT_PHASE_BINS))

    with h5py.File(h5_path, "r") as h5f:
        if "lfp" not in h5f or "raw_lfp" not in h5f["lfp"]:
            raise ValueError("H5 does not contain /lfp/raw_lfp; cannot compute PLV/PAC.")
        raw = h5f["lfp/raw_lfp"]
        n_channels = int(raw.shape[0])
        lfp_fs = float(h5f["metadata"].attrs.get("lfp_sample_rate_hz", 1000.0))
        total_samples = int(raw.shape[1])
        origin_epoch_ms = float(h5f["time"].attrs.get("time_origin_epoch_ms", h5f["metadata"].attrs.get("time_origin_epoch_ms", 0.0)))
        if "rhythm" in h5f and "hour_edges_s" in h5f["rhythm"]:
            hour_edges_s = np.asarray(h5f["rhythm/hour_edges_s"][:], dtype=np.float64)
        else:
            total_s = float(h5f["time"].attrs.get("lfp_time_s_extent", [0.0, total_samples / lfp_fs])[1])
            hour_edges_s = np.arange(0.0, math.ceil(total_s / 3600.0) * 3600.0 + 1.0, 3600.0, dtype=np.float64)
            hour_edges_s[-1] = min(hour_edges_s[-1], total_s)
        if hour_edges_s.size < 2:
            hour_edges_s = np.asarray([0.0, total_samples / lfp_fs], dtype=np.float64)

        band_names = list(bands.keys())
        band_ranges = np.asarray([bands[name] for name in band_names], dtype=np.float64)
        pac_pairs = _low_to_high_band_pairs(band_names, bands)
        n_hours = int(hour_edges_s.size - 1)
        plv = np.full((n_hours, len(band_names), n_channels, n_channels), np.nan, dtype=np.float32)
        pac = np.full((n_hours, len(pac_pairs), n_channels, n_channels), np.nan, dtype=np.float32)
        valid_fraction = np.full(n_hours, np.nan, dtype=np.float32)
        hour_center_epoch_ms = origin_epoch_ms + ((hour_edges_s[:-1] + hour_edges_s[1:]) / 2.0) * 1000.0
        hour_start_epoch_ms = origin_epoch_ms + hour_edges_s[:-1] * 1000.0
        hour_end_epoch_ms = origin_epoch_ms + hour_edges_s[1:] * 1000.0

        missing_mask_ds = h5f["lfp/missing_mask"] if "missing_mask" in h5f["lfp"] else None
        coverage_ds = h5f["lfp/file_coverage_mask"] if "file_coverage_mask" in h5f["lfp"] else None

        for hour_idx in range(n_hours):
            start_s = float(hour_edges_s[hour_idx])
            end_s = float(hour_edges_s[hour_idx + 1])
            start = max(0, int(round(start_s * lfp_fs)))
            end = min(total_samples, int(round(end_s * lfp_fs)))
            if end <= start:
                continue
            raw_hour = np.asarray(raw[:, start:end], dtype=np.float64)
            missing = (
                np.asarray(missing_mask_ds[start:end], dtype=bool)
                if missing_mask_ds is not None
                else np.any(~np.isfinite(raw_hour), axis=0)
            )
            coverage = (
                np.asarray(coverage_ds[start:end], dtype=bool)
                if coverage_ds is not None
                else np.ones(end - start, dtype=bool)
            )
            valid_sample = coverage & (~missing)
            valid_fraction[hour_idx] = float(np.mean(valid_sample)) if valid_sample.size else np.nan
            if valid_fraction[hour_idx] < valid_fraction_threshold:
                _emit(progress_callback, hour_idx + 1, n_hours, h5_path)
                continue
            prepared, fs_used = _prepare_hour_data(raw_hour, lfp_fs, target_fs)
            if prepared.shape[1] < max(64, int(fs_used * 5.0)):
                _emit(progress_callback, hour_idx + 1, n_hours, h5_path)
                continue
            analytic_by_band = []
            for band_name in band_names:
                band_data = _bandpass_filter(prepared, fs_used, bands[band_name])
                analytic_by_band.append(signal.hilbert(band_data, axis=-1))
            for band_idx, analytic in enumerate(analytic_by_band):
                plv[hour_idx, band_idx] = compute_plv_matrix(np.angle(analytic)).astype(np.float32)
            for pair_idx, pair in enumerate(pac_pairs):
                phase_analytic = analytic_by_band[pair["phase_band_index"]]
                amp_analytic = analytic_by_band[pair["amplitude_band_index"]]
                pac[hour_idx, pair_idx] = compute_pac_matrix(
                    np.angle(phase_analytic),
                    np.abs(amp_analytic),
                    n_bins=phase_bins,
                ).astype(np.float32)
            _emit(progress_callback, hour_idx + 1, n_hours, h5_path)

    metadata = {
        "version": CONNECTIVITY_VERSION,
        "source_h5": os.path.abspath(str(h5_path)),
        "band_names": band_names,
        "band_ranges_hz": band_ranges.tolist(),
        "pac_band_pairs": pac_pairs,
        "channel_order": "channel_0_to_15_probe_shallow_to_deep",
        "target_fs_hz": float(target_fs),
        "source_lfp_fs_hz": float(lfp_fs),
        "valid_fraction_threshold": float(valid_fraction_threshold),
        "pac_phase_bins": int(phase_bins),
        "method": "hourly PLV from Hilbert phase; PAC from Tort modulation index using low-frequency phase to high-frequency amplitude.",
    }
    return {
        "metadata": metadata,
        "hour_start_epoch_ms": hour_start_epoch_ms.astype(np.float64),
        "hour_center_epoch_ms": hour_center_epoch_ms.astype(np.float64),
        "hour_end_epoch_ms": hour_end_epoch_ms.astype(np.float64),
        "valid_fraction": valid_fraction,
        "plv": plv,
        "pac": pac,
    }


def write_connectivity_sidecar(result, npz_path, json_path):
    os.makedirs(os.path.dirname(os.path.abspath(npz_path)), exist_ok=True)
    np.savez_compressed(
        npz_path,
        hour_start_epoch_ms=result["hour_start_epoch_ms"],
        hour_center_epoch_ms=result["hour_center_epoch_ms"],
        hour_end_epoch_ms=result["hour_end_epoch_ms"],
        valid_fraction=result["valid_fraction"],
        plv=result["plv"],
        pac=result["pac"],
    )
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(result["metadata"], handle, ensure_ascii=False, indent=2)


def load_connectivity_sidecar(h5_path, output_dir):
    paths = expected_connectivity_sidecar_paths(h5_path, output_dir)
    if not os.path.exists(paths["npz"]) or not os.path.exists(paths["json"]):
        return None
    with open(paths["json"], "r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    data = np.load(paths["npz"])
    return {
        "paths": paths,
        "metadata": metadata,
        "hour_start_epoch_ms": data["hour_start_epoch_ms"],
        "hour_center_epoch_ms": data["hour_center_epoch_ms"],
        "hour_end_epoch_ms": data["hour_end_epoch_ms"],
        "valid_fraction": data["valid_fraction"],
        "plv": data["plv"],
        "pac": data["pac"],
    }


def compute_plv_matrix(phase):
    phase = np.asarray(phase, dtype=np.float64)
    if phase.ndim != 2:
        raise ValueError("phase must be [channels, samples].")
    valid = np.all(np.isfinite(phase), axis=0)
    if not np.any(valid):
        return np.full((phase.shape[0], phase.shape[0]), np.nan, dtype=np.float64)
    wrapped = ((phase[:, valid] + np.pi) % (2.0 * np.pi)) - np.pi
    z = np.exp(1j * wrapped).astype(np.complex128, copy=False)
    n_channels = z.shape[0]
    mat = np.full((n_channels, n_channels), np.nan, dtype=np.float64)
    for ch_a in range(n_channels):
        for ch_b in range(n_channels):
            mat[ch_a, ch_b] = float(np.abs(np.mean(z[ch_a] * np.conjugate(z[ch_b]))))
    np.fill_diagonal(mat, 1.0)
    return mat


def compute_pac_matrix(phase, amplitude, n_bins=18):
    phase = np.asarray(phase, dtype=np.float64)
    amplitude = np.asarray(amplitude, dtype=np.float64)
    if phase.ndim != 2 or amplitude.ndim != 2:
        raise ValueError("phase and amplitude must be [channels, samples].")
    if phase.shape[1] != amplitude.shape[1]:
        raise ValueError("phase and amplitude sample axes must match.")
    n_phase_ch = phase.shape[0]
    n_amp_ch = amplitude.shape[0]
    out = np.full((n_phase_ch, n_amp_ch), np.nan, dtype=np.float64)
    bins = np.linspace(-np.pi, np.pi, int(n_bins) + 1)
    for p_idx in range(n_phase_ch):
        p = phase[p_idx]
        finite_phase = np.isfinite(p)
        for a_idx in range(n_amp_ch):
            amp = amplitude[a_idx]
            valid = finite_phase & np.isfinite(amp) & (amp >= 0)
            if np.count_nonzero(valid) < int(n_bins):
                continue
            means = np.zeros(int(n_bins), dtype=np.float64)
            for bin_idx in range(int(n_bins)):
                if bin_idx == int(n_bins) - 1:
                    mask = valid & (p >= bins[bin_idx]) & (p <= bins[bin_idx + 1])
                else:
                    mask = valid & (p >= bins[bin_idx]) & (p < bins[bin_idx + 1])
                if np.any(mask):
                    means[bin_idx] = float(np.nanmean(amp[mask]))
            total = float(np.sum(means))
            if total <= 0:
                continue
            prob = means / total
            uniform = 1.0 / float(n_bins)
            with np.errstate(divide="ignore", invalid="ignore"):
                kl = np.nansum(prob * np.log(np.maximum(prob, 1e-12) / uniform))
            out[p_idx, a_idx] = float(kl / np.log(float(n_bins)))
    return out


def _select_connectivity_bands(bands):
    if not isinstance(bands, dict) or not bands:
        return {"delta": [1.0, 4.0], "theta": [4.0, 12.0], "gamma": [30.0, 55.0]}
    preferred = ["delta", "theta", "gamma"]
    selected = {}
    for name in preferred:
        if name in bands:
            selected[name] = [float(bands[name][0]), float(bands[name][1])]
    if len(selected) >= 2:
        return selected
    for name, value in bands.items():
        if len(selected) >= 3:
            break
        if name not in selected:
            selected[str(name)] = [float(value[0]), float(value[1])]
    return selected


def _low_to_high_band_pairs(band_names, bands):
    centers = {name: (float(bands[name][0]) + float(bands[name][1])) / 2.0 for name in band_names}
    pairs = []
    for p_idx, p_name in enumerate(band_names):
        for a_idx, a_name in enumerate(band_names):
            if centers[p_name] < centers[a_name]:
                pairs.append({
                    "phase_band": p_name,
                    "amplitude_band": a_name,
                    "phase_band_index": int(p_idx),
                    "amplitude_band_index": int(a_idx),
                })
    return pairs


def _prepare_hour_data(raw_hour, fs, target_fs):
    data = np.asarray(raw_hour, dtype=np.float64)
    for ch_idx in range(data.shape[0]):
        channel = data[ch_idx]
        finite = np.isfinite(channel)
        if not np.any(finite):
            channel[:] = 0.0
            continue
        if not np.all(finite):
            x = np.arange(channel.size, dtype=np.float64)
            channel[~finite] = np.interp(x[~finite], x[finite], channel[finite])
        channel -= float(np.nanmedian(channel))
    fs = float(fs)
    target_fs = min(float(target_fs), fs)
    if fs > target_fs + 1e-6:
        up = int(round(target_fs))
        down = int(round(fs))
        gcd = math.gcd(up, down)
        up //= gcd
        down //= gcd
        data = signal.resample_poly(data, up, down, axis=-1)
        fs = target_fs
    return data, fs


def _bandpass_filter(data, fs, band):
    low, high = [float(v) for v in band]
    nyq = float(fs) / 2.0
    low = max(0.05, low)
    high = min(high, nyq * 0.95)
    if high <= low:
        raise ValueError(f"Invalid band {band} for fs={fs}")
    sos = signal.butter(4, [low / nyq, high / nyq], btype="bandpass", output="sos")
    return signal.sosfiltfilt(sos, data, axis=-1)


def _emit(callback, done, total, h5_path):
    if callback is not None:
        callback("connectivity_hour", int(done), int(total), {"h5_path": str(h5_path)})
