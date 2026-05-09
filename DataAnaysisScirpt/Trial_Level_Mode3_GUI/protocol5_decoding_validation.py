from __future__ import annotations

import json
import math
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mplconfig_"))

import h5py
import matplotlib
import numpy as np
from scipy import signal
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, balanced_accuracy_score, confusion_matrix, roc_auc_score
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore", category=RuntimeWarning, module=r"sklearn\..*")


CONFIG = {
    "h5_path": Path(__file__).resolve().parents[3] / "Batch_Aligned_Trials.h5",
    "trial_txt_path": Path(__file__).resolve().parents[2]
    / "BehaviorData"
    / "BW08"
    / "Trial.txt",
    "protocol": 5,
    "window_ms": (-250.0, 250.0),
    "cv_splits": 5,
    "random_state": 42,
    "lfp": {
        "channels": list(range(16)),
        "median_reference": True,
        "bands": [
            ("theta", (4.0, 8.0), 1.0),
            ("beta", (13.0, 30.0), 1.0),
            ("low_gamma", (30.0, 55.0), 1.0),
            ("high_gamma", (65.0, 120.0), 1.0),
        ],
    },
    "esa": {
        "channels": list(range(16)),
        "bin_ms": 25.0,
    },
    "imu": {
        "channels": list(range(6)),
        "bin_ms": 25.0,
        "lowpass_hz": 40.0,
        "add_magnitude_features": True,
    },
    "output_dir": Path(__file__).resolve().parent / "decoding_outputs" / "protocol5",
}


@dataclass
class TrialMeta:
    trial_num: int
    protocol: int
    trial_type: int
    outcome: int
    choice: int | None


def parse_trial_file(path: Path) -> dict[int, TrialMeta]:
    trials: dict[int, TrialMeta] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) < 25:
            continue
        trial_num = int(parts[1])
        protocol = int(parts[2])
        trial_type = int(parts[3])
        outcome = int(parts[4])
        if outcome == 1:
            choice = trial_type
        elif outcome == 2:
            choice = 2 if trial_type == 1 else 1 if trial_type == 2 else None
        else:
            choice = None
        trials[trial_num] = TrialMeta(
            trial_num=trial_num,
            protocol=protocol,
            trial_type=trial_type,
            outcome=outcome,
            choice=choice,
        )
    return trials


def slice_window(
    time_ms: np.ndarray,
    values: np.ndarray,
    window_ms: tuple[float, float],
) -> tuple[np.ndarray, np.ndarray]:
    mask = (time_ms >= window_ms[0]) & (time_ms < window_ms[1])
    if values.ndim == 1:
        return time_ms[mask], values[mask]
    return time_ms[mask], values[:, mask]


def infer_fs(time_ms: np.ndarray) -> float:
    if time_ms.size < 2:
        raise ValueError("Need at least 2 time samples to infer sampling rate.")
    return 1000.0 / float(np.median(np.diff(time_ms)))


def build_time_bins(window_ms: tuple[float, float], bin_ms: float) -> np.ndarray:
    t0, t1 = window_ms
    n_bins = int(round((t1 - t0) / bin_ms))
    return np.linspace(t0, t1, n_bins + 1)


def sanitize_features(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    x = np.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6)
    return np.clip(x, -1e6, 1e6)


def binned_event_counts(
    time_ms: np.ndarray,
    values: np.ndarray,
    bins_ms: np.ndarray,
) -> np.ndarray:
    features = []
    bin_width_sec = (bins_ms[1] - bins_ms[0]) / 1000.0
    for ch in range(values.shape[0]):
        counts = []
        for start, stop in zip(bins_ms[:-1], bins_ms[1:]):
            mask = (time_ms >= start) & (time_ms < stop)
            counts.append(float(np.sum(values[ch, mask])))
        rates = np.asarray(counts, dtype=float) / bin_width_sec
        features.extend(np.sqrt(rates))
    return sanitize_features(np.asarray(features, dtype=float))


def safe_binary_auc(y_true: np.ndarray, y_score: np.ndarray) -> float:
    try:
        return float(roc_auc_score(y_true, y_score))
    except ValueError:
        return float("nan")


def binned_signal_stats(
    time_ms: np.ndarray,
    values: np.ndarray,
    bins_ms: np.ndarray,
) -> np.ndarray:
    mean_features = []
    std_features = []
    for ch in range(values.shape[0]):
        channel_means = []
        channel_stds = []
        for start, stop in zip(bins_ms[:-1], bins_ms[1:]):
            mask = (time_ms >= start) & (time_ms < stop)
            window_vals = values[ch, mask]
            if window_vals.size == 0:
                channel_means.append(0.0)
                channel_stds.append(0.0)
            else:
                channel_means.append(float(np.mean(window_vals)))
                channel_stds.append(float(np.std(window_vals)))
        mean_features.extend(channel_means)
        std_features.extend(channel_stds)
    return sanitize_features(np.asarray(mean_features + std_features, dtype=float))


def extract_lfp_features(
    lfp_t: np.ndarray,
    lfp_v: np.ndarray,
    window_ms: tuple[float, float],
    channels: Iterable[int],
    median_reference: bool,
    bands: list[tuple[str, tuple[float, float], float]],
) -> np.ndarray:
    time_ms, full_window = slice_window(lfp_t, lfp_v, window_ms)
    channel_ids = list(channels)
    signal_window = full_window[channel_ids].astype(float)
    if median_reference:
        signal_window = signal_window - np.median(full_window, axis=0, keepdims=True)
    signal_window = signal.detrend(signal_window, axis=-1, type="constant")
    fs = infer_fs(time_ms)

    features = []
    for ch in range(signal_window.shape[0]):
        freqs, pxx = signal.welch(
            signal_window[ch],
            fs=fs,
            nperseg=min(signal_window.shape[1], max(64, int(fs * 0.25))),
        )
        for _, (f_low, f_high), weight in bands:
            mask = (freqs >= f_low) & (freqs < f_high)
            band_power = np.trapezoid(pxx[mask], freqs[mask]) if np.any(mask) else 0.0
            features.append(np.log10(band_power + 1e-12) * weight)
        features.extend(
            [
                float(np.mean(signal_window[ch])),
                float(np.std(signal_window[ch])),
            ]
        )
    return sanitize_features(np.asarray(features, dtype=float))


def extract_esa_features(
    esa_t: np.ndarray,
    esa_v: np.ndarray,
    window_ms: tuple[float, float],
    channels: Iterable[int],
    bin_ms: float,
) -> np.ndarray:
    time_ms, esa_window = slice_window(esa_t, esa_v, window_ms)
    esa_window = esa_window[list(channels)].astype(float)
    bins_ms = build_time_bins(window_ms, bin_ms)
    return binned_signal_stats(time_ms, esa_window, bins_ms)


def extract_imu_features(
    sen_t: np.ndarray,
    sen_v: np.ndarray,
    window_ms: tuple[float, float],
    channels: Iterable[int],
    bin_ms: float,
    lowpass_hz: float,
    add_magnitude_features: bool,
) -> np.ndarray:
    time_ms, imu_window = slice_window(sen_t, sen_v[list(channels)], window_ms)
    imu_window = imu_window.astype(float)

    pre_mask = time_ms < 0
    if np.any(pre_mask):
        imu_window = imu_window - np.median(imu_window[:, pre_mask], axis=1, keepdims=True)

    fs = infer_fs(time_ms)
    if imu_window.shape[1] > 8:
        cutoff = min(lowpass_hz, fs * 0.4)
        b, a = signal.butter(2, cutoff, btype="low", fs=fs)
        imu_window = signal.filtfilt(b, a, imu_window, axis=-1)

    if add_magnitude_features and imu_window.shape[0] >= 6:
        accel_mag = np.linalg.norm(imu_window[:3], axis=0, keepdims=True)
        gyro_mag = np.linalg.norm(imu_window[3:6], axis=0, keepdims=True)
        imu_window = np.vstack([imu_window, accel_mag, gyro_mag])

    bins_ms = build_time_bins(window_ms, bin_ms)
    return binned_signal_stats(time_ms, imu_window, bins_ms)


def load_protocol_trials(config: dict) -> list[dict]:
    trial_meta = parse_trial_file(config["trial_txt_path"])
    trials = []
    with h5py.File(config["h5_path"], "r") as h5_file:
        for key in sorted(h5_file.keys()):
            match = re.search(r"Trial_(\d+)$", key)
            if not match:
                continue
            trial_num = int(match.group(1))
            meta = trial_meta.get(trial_num)
            if meta is None:
                continue
            if meta.protocol != config["protocol"]:
                continue
            if meta.outcome not in (1, 2) or meta.choice is None:
                continue

            group = h5_file[key]
            trials.append(
                {
                    "trial_key": key,
                    "trial_num": trial_num,
                    "protocol": meta.protocol,
                    "outcome": meta.outcome,
                    "choice": meta.choice,
                    "lfp_t": np.array(group["lfp_t"]),
                    "lfp_v": np.array(group["lfp_v"]),
                    "esa_t": np.array(group["esa_t"]) if "esa_t" in group else np.array(group["lfp_t"]),
                    "esa_v": np.array(group["esa_v"]) if "esa_v" in group else None,
                    "raster_v": np.array(group["raster_v"]) if "raster_v" in group else None,
                    "sen_t": np.array(group["sen_t"]),
                    "sen_v": np.array(group["sen_v"]),
                }
            )
    trials.sort(key=lambda item: item["trial_num"])
    return trials


def make_design_matrices(trials: list[dict], config: dict) -> dict[str, np.ndarray]:
    x_lfp = []
    x_esa = []
    x_imu = []
    y_outcome = []
    y_choice = []

    for trial in trials:
        if trial["esa_v"] is None:
            raise RuntimeError(
                "H5 trial groups do not contain 'esa_v'. Re-export the H5 pack with the 16-channel ESA stream."
            )
        x_lfp.append(
            extract_lfp_features(
                trial["lfp_t"],
                trial["lfp_v"],
                config["window_ms"],
                config["lfp"]["channels"],
                config["lfp"]["median_reference"],
                config["lfp"]["bands"],
            )
        )
        x_esa.append(
            extract_esa_features(
                trial["esa_t"],
                trial["esa_v"],
                config["window_ms"],
                config["esa"]["channels"],
                config["esa"]["bin_ms"],
            )
        )
        x_imu.append(
            extract_imu_features(
                trial["sen_t"],
                trial["sen_v"],
                config["window_ms"],
                config["imu"]["channels"],
                config["imu"]["bin_ms"],
                config["imu"]["lowpass_hz"],
                config["imu"]["add_magnitude_features"],
            )
        )
        y_outcome.append(1 if trial["outcome"] == 1 else 0)
        y_choice.append(1 if trial["choice"] == 2 else 0)

    return {
        "LFP": np.vstack(x_lfp),
        "ESA": np.vstack(x_esa),
        "IMU": np.vstack(x_imu),
        "y_outcome": np.asarray(y_outcome, dtype=int),
        "y_choice": np.asarray(y_choice, dtype=int),
    }


def run_cv_decode(x: np.ndarray, y: np.ndarray, n_splits: int, random_state: int) -> dict:
    cv = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=random_state)
    clf = make_pipeline(
        StandardScaler(),
        LogisticRegression(
            max_iter=5000,
            solver="lbfgs",
            class_weight="balanced",
            random_state=random_state,
        ),
    )
    pred = cross_val_predict(clf, x, y, cv=cv, method="predict")
    prob = cross_val_predict(clf, x, y, cv=cv, method="predict_proba")[:, 1]
    return {
        "pred": pred,
        "prob": prob,
        "accuracy": float(accuracy_score(y, pred)),
        "balanced_accuracy": float(balanced_accuracy_score(y, pred)),
        "auc": safe_binary_auc(y, prob),
        "confusion_matrix": confusion_matrix(y, pred).tolist(),
    }


def plot_prediction_panel(
    axes: np.ndarray,
    trial_nums: np.ndarray,
    labels_true: np.ndarray,
    decode_results: dict[str, dict],
    target_name: str,
    class_labels: tuple[str, str],
) -> None:
    color_map = {0: "#1f77b4", 1: "#d62728"}
    for ax, (modality, result) in zip(axes, decode_results.items()):
        pred = result["pred"]
        mismatch = pred != labels_true
        ax.scatter(
            trial_nums,
            np.ones_like(trial_nums),
            c=[color_map[int(v)] for v in labels_true],
            s=26,
            alpha=0.9,
            label="Ground truth",
        )
        ax.scatter(
            trial_nums,
            np.zeros_like(trial_nums),
            c=[color_map[int(v)] for v in pred],
            s=26,
            alpha=0.9,
            marker="s",
            label="Prediction",
        )
        if np.any(mismatch):
            ax.vlines(
                trial_nums[mismatch],
                0.1,
                0.9,
                color="0.75",
                linewidth=0.8,
                alpha=0.8,
            )
        ax.set_title(
            f"{modality} | acc={result['accuracy']:.3f}, bal={result['balanced_accuracy']:.3f}, auc={result['auc']:.3f}"
        )
        ax.set_yticks([0, 1])
        ax.set_yticklabels(["Prediction", "Ground truth"])
        ax.set_xlim(trial_nums.min() - 1, trial_nums.max() + 1)
        ax.set_xlabel("Trial number")
        ax.grid(True, axis="x", alpha=0.2)

    legend_handles = [
        plt.Line2D([0], [0], marker="o", linestyle="", color=color_map[0], label=class_labels[0]),
        plt.Line2D([0], [0], marker="o", linestyle="", color=color_map[1], label=class_labels[1]),
        plt.Line2D([0], [0], marker="o", linestyle="", color="black", label="Ground truth"),
        plt.Line2D([0], [0], marker="s", linestyle="", color="black", label="Prediction"),
    ]
    axes[0].legend(handles=legend_handles, loc="upper right", frameon=True)
    axes[0].set_ylabel(target_name)


def save_prediction_figure(
    trial_nums: np.ndarray,
    outcome_labels: np.ndarray,
    choice_labels: np.ndarray,
    outcome_results: dict[str, dict],
    choice_results: dict[str, dict],
    output_path: Path,
) -> None:
    fig, axes = plt.subplots(2, 3, figsize=(18, 8), sharex=False)
    plot_prediction_panel(
        axes[0],
        trial_nums,
        outcome_labels,
        outcome_results,
        target_name="Outcome",
        class_labels=("Error", "Correct"),
    )
    plot_prediction_panel(
        axes[1],
        trial_nums,
        choice_labels,
        choice_results,
        target_name="Choice",
        class_labels=("Left", "Right"),
    )
    fig.suptitle(
        "Protocol 5 decoding validation from trial-start ±250 ms window",
        fontsize=14,
        y=1.02,
    )
    plt.tight_layout()
    fig.savefig(output_path, dpi=180, bbox_inches="tight")
    plt.close(fig)


def save_prediction_table(
    trials: list[dict],
    outcome_results: dict[str, dict],
    choice_results: dict[str, dict],
    output_path: Path,
) -> None:
    header = [
        "trial_num",
        "trial_key",
        "ground_truth_outcome",
        "ground_truth_choice",
        "lfp_outcome_pred",
        "esa_outcome_pred",
        "imu_outcome_pred",
        "lfp_choice_pred",
        "esa_choice_pred",
        "imu_choice_pred",
        "lfp_outcome_prob_correct",
        "esa_outcome_prob_correct",
        "imu_outcome_prob_correct",
        "lfp_choice_prob_right",
        "esa_choice_prob_right",
        "imu_choice_prob_right",
    ]

    lines = [",".join(header)]
    for idx, trial in enumerate(trials):
        outcome_gt = "Correct" if trial["outcome"] == 1 else "Error"
        choice_gt = "Right" if trial["choice"] == 2 else "Left"
        row = [
            str(trial["trial_num"]),
            trial["trial_key"],
            outcome_gt,
            choice_gt,
            "Correct" if outcome_results["LFP"]["pred"][idx] == 1 else "Error",
            "Correct" if outcome_results["ESA"]["pred"][idx] == 1 else "Error",
            "Correct" if outcome_results["IMU"]["pred"][idx] == 1 else "Error",
            "Right" if choice_results["LFP"]["pred"][idx] == 1 else "Left",
            "Right" if choice_results["ESA"]["pred"][idx] == 1 else "Left",
            "Right" if choice_results["IMU"]["pred"][idx] == 1 else "Left",
            f"{outcome_results['LFP']['prob'][idx]:.6f}",
            f"{outcome_results['ESA']['prob'][idx]:.6f}",
            f"{outcome_results['IMU']['prob'][idx]:.6f}",
            f"{choice_results['LFP']['prob'][idx]:.6f}",
            f"{choice_results['ESA']['prob'][idx]:.6f}",
            f"{choice_results['IMU']['prob'][idx]:.6f}",
        ]
        lines.append(",".join(row))
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    output_dir = CONFIG["output_dir"]
    output_dir.mkdir(parents=True, exist_ok=True)

    trials = load_protocol_trials(CONFIG)
    if not trials:
        raise RuntimeError("No valid protocol 5 trials were found in the H5 pack.")

    design = make_design_matrices(trials, CONFIG)
    trial_nums = np.asarray([trial["trial_num"] for trial in trials], dtype=int)

    outcome_results = {
        modality: run_cv_decode(
            design[modality],
            design["y_outcome"],
            n_splits=CONFIG["cv_splits"],
            random_state=CONFIG["random_state"],
        )
        for modality in ("LFP", "ESA", "IMU")
    }
    choice_results = {
        modality: run_cv_decode(
            design[modality],
            design["y_choice"],
            n_splits=CONFIG["cv_splits"],
            random_state=CONFIG["random_state"],
        )
        for modality in ("LFP", "ESA", "IMU")
    }

    summary = {
        "n_trials": len(trials),
        "window_ms": CONFIG["window_ms"],
        "note": (
            "Current GUI-exported H5 contains lfp_v, esa_v, raster_v and sen_v. "
            "ESA decoding here uses the exported 16-channel 1 kHz analog ESA stream as the ESA source."
        ),
        "lfp_config": {
            "channels": CONFIG["lfp"]["channels"],
            "median_reference": CONFIG["lfp"]["median_reference"],
            "bands": CONFIG["lfp"]["bands"],
        },
        "esa_config": CONFIG["esa"],
        "imu_config": CONFIG["imu"],
        "outcome_decoding": {
            modality: {
                "accuracy": result["accuracy"],
                "balanced_accuracy": result["balanced_accuracy"],
                "auc": result["auc"],
                "confusion_matrix": result["confusion_matrix"],
            }
            for modality, result in outcome_results.items()
        },
        "choice_decoding": {
            modality: {
                "accuracy": result["accuracy"],
                "balanced_accuracy": result["balanced_accuracy"],
                "auc": result["auc"],
                "confusion_matrix": result["confusion_matrix"],
            }
            for modality, result in choice_results.items()
        },
    }

    summary_path = output_dir / "protocol5_decoding_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    prediction_csv = output_dir / "protocol5_trial_predictions.csv"
    save_prediction_table(trials, outcome_results, choice_results, prediction_csv)

    prediction_fig = output_dir / "protocol5_decoding_predictions.png"
    save_prediction_figure(
        trial_nums,
        design["y_outcome"],
        design["y_choice"],
        outcome_results,
        choice_results,
        prediction_fig,
    )

    print(f"Loaded {len(trials)} valid protocol 5 trials from {CONFIG['h5_path']}")
    print(
        "LFP config:",
        {
            "channels": CONFIG["lfp"]["channels"],
            "median_reference": CONFIG["lfp"]["median_reference"],
            "bands": CONFIG["lfp"]["bands"],
        },
    )
    for task_name, results in [("Outcome", outcome_results), ("Choice", choice_results)]:
        print(f"{task_name} decoding:")
        for modality, result in results.items():
            print(
                f"  {modality}: acc={result['accuracy']:.4f}, "
                f"bal_acc={result['balanced_accuracy']:.4f}, "
                f"auc={result['auc']:.4f}, "
                f"cm={result['confusion_matrix']}"
            )
    print(f"Saved figure to {prediction_fig}")
    print(f"Saved per-trial predictions to {prediction_csv}")
    print(f"Saved summary to {summary_path}")


if __name__ == "__main__":
    main()
