import ast
import copy
import os

try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover - exercised implicitly in this environment
    yaml = None


DEFAULT_CONFIG = {
    "general": {
        "timezone": "Asia/Shanghai",
        "animal_id": "Unknown",
        "output_dir": "",
        "light_on_hour": 6.0,
        "light_off_hour": 18.0,
    },
    "io": {
        "lfp_sample_rate_hz": 1000,
        "imu_target_rate_hz": 100,
        "store_raw_lfp": True,
        "compression": "gzip",
        "lfp_chunk_seconds": 10,
        "imu_chunk_seconds": 10,
    },
    "preprocess": {
        "apply_lfp_phase_compensation": True,
        "rereference_method": "none",
        "adjacent_bipolar_edge_mode": "repeat_last_pair",
        "median_reference": False,
        "notch_enable": False,
        "notch_freq_hz": 50.0,
        "short_gap_interpolate_max_s": 2.0,
    },
    "missing_data": {
        "lfp_missing_threshold": -1000,
        "sensor_missing_threshold": -10,
        "valid_fraction_threshold": 0.8,
    },
    "features": {
        "feature_window_s": 60.0,
        "feature_step_s": 60.0,
        "spectral_entropy_enable": True,
        "welch_nperseg": 2048,
        "welch_noverlap": 1024,
        "bands": {
            "delta": [0.5, 4.0],
            "theta": [4.0, 8.0],
            "sigma": [10.0, 15.0],
            "beta": [13.0, 30.0],
            "low_gamma": [30.0, 55.0],
            "high_gamma": [65.0, 120.0],
        },
    },
    "activity": {
        "activity_metric_method": "diff_mag_mean",
        "moving_activity_quantile": 0.6,
        "inactive_activity_quantile": 0.2,
    },
    "working": {
        "working_merge_gap_s": 5.0,
        "working_min_bout_s": 120.0,
        "working_tail_exclude_s": 30.0,
        "working_window_min_fraction": 0.5,
    },
    "slice_build": {
        "filename_prefilter_margin_hours": 36.0,
    },
    "states": {
        "representative_channel_mode": "auto",
        "representative_channel_index": None,
        "representative_channel_selection_step_s": 1.0,
        "classification_use_raw_signal": True,
        "classification_lowpass_enable": True,
        "classification_lowpass_order": 4,
        "classification_lowpass_cutoff_hz": 100.0,
        "spectrogram_target_fs_hz": 1250.0,
        "spectrogram_freq_min_hz": 1.0,
        "spectrogram_freq_max_hz": 100.0,
        "spectrogram_n_freq_bins": 50,
        "scoring_window_s": 10.0,
        "scoring_step_s": 10.0,
        "imu_proxy_method": "rms_centered_magnitude",
        "imu_smoothing_sigma_s": 10.0,
        "rem_highfreq_low_hz": 80.0,
        "rem_highfreq_high_hz": 250.0,
        "threshold_hist_bins": 200,
        "threshold_hist_bins_auto": True,
        "threshold_hist_bins_min": 40,
        "threshold_hist_bins_max": 320,
        "threshold_hist_smoothing_sigma_bins": 2.0,
        "threshold_min_peak_prominence_fraction": 0.05,
        "threshold_min_peak_distance_fraction": 0.15,
        "min_wake_episode_s": 420.0,
        "final_review_hour_buffer_s": 300.0,
        "unknown_if_missing_imu": True,
        "analysis_states": ["Wake", "NREM", "REM", "Working"],
        "occupancy_states": ["Wake", "MiniWake", "NREM", "REM", "Working"],
    },
    "rhythm": {
        "hour_bin_s": 3600.0,
        "state_reference_normalization_enable": True,
        "state_reference_normalization_method": "divide_by_daily_state_band_mean",
        "average_channel_groups": [list(range(16))],
        "average_group_names": ["All channels"],
    },
    "report": {
        "report_output_dir": "",
        "plot_dpi": 120,
        "embed_png": True,
    },
}

REPLACE_DICT_KEYS = {"bands"}


def _deep_merge(base, override):
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in REPLACE_DICT_KEYS and isinstance(value, dict):
            merged[key] = copy.deepcopy(value)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _parse_scalar(value):
    text = value.strip()
    if text == "":
        return ""
    lower = text.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    if lower in {"null", "none"}:
        return None
    try:
        if "." not in text and "e" not in lower:
            return int(text)
        return float(text)
    except ValueError:
        pass
    if text.startswith("[") and text.endswith("]"):
        literal = (
            text.replace("true", "True")
            .replace("false", "False")
            .replace("null", "None")
        )
        return ast.literal_eval(literal)
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        return text[1:-1]
    return text


def _simple_yaml_load(text):
    lines = text.splitlines()
    root = {}
    stack = [(-1, root)]

    def next_meaningful_line(start_idx):
        for idx in range(start_idx, len(lines)):
            candidate = lines[idx]
            if candidate.strip() and not candidate.lstrip().startswith("#"):
                return candidate
        return None

    for line_idx, raw_line in enumerate(lines):
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = raw_line.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()

        if stripped.startswith("- "):
            current = stack[-1][1]
            if isinstance(current, list):
                current.append(_parse_scalar(stripped[2:].strip()))
            continue

        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        value = value.strip()
        current = stack[-1][1]
        if value == "":
            next_line = next_meaningful_line(line_idx + 1)
            next_is_list = False
            if next_line is not None:
                next_indent = len(next_line) - len(next_line.lstrip(" "))
                next_is_list = next_indent > indent and next_line.strip().startswith("- ")
            current[key] = [] if next_is_list else {}
            stack.append((indent, current[key]))
        else:
            current[key] = _parse_scalar(value)
    return root


def _recursive_get(mapping, key):
    if not isinstance(mapping, dict):
        return None
    if key in mapping:
        return mapping[key]
    for value in mapping.values():
        if isinstance(value, dict):
            found = _recursive_get(value, key)
            if found is not None:
                return found
    return None


class Config:
    def __init__(self, config_file=None, config_data=None):
        self.config_file = config_file
        loaded = {}
        if config_data is not None:
            loaded = copy.deepcopy(config_data)
        else:
            if config_file is None:
                base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                config_file = os.path.join(base_dir, "config_default.yaml")
                self.config_file = config_file
            if config_file and os.path.exists(config_file):
                with open(config_file, "r", encoding="utf-8") as f:
                    text = f.read()
                if yaml is not None:
                    loaded = yaml.safe_load(text) or {}
                else:
                    loaded = _simple_yaml_load(text)
        self.config_data = _deep_merge(DEFAULT_CONFIG, loaded)

    def get(self, key, default=None):
        if not key:
            return self.config_data
        if "." in key:
            current = self.config_data
            for part in key.split("."):
                if not isinstance(current, dict) or part not in current:
                    return default
                current = current[part]
            return current
        value = _recursive_get(self.config_data, key)
        return default if value is None else value

    def section(self, key):
        value = self.get(key, {})
        return value if isinstance(value, dict) else {}

    def as_dict(self):
        return copy.deepcopy(self.config_data)

    @classmethod
    def from_dict(cls, config_data):
        return cls(config_data=config_data)


def resolve_config(config_like=None):
    if config_like is None:
        return def_config
    if isinstance(config_like, Config):
        return config_like
    if isinstance(config_like, dict):
        return Config.from_dict(config_like)
    return Config(config_like)


def_config = Config()
