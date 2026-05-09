from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy import signal

from data_processing import DataProcessor


def infer_fs_from_time_ms(time_ms):
    dt_ms = np.median(np.diff(time_ms))
    return 1000.0 / dt_ms


def circular_mean_deg(phase_rad):
    return np.degrees(np.angle(np.mean(np.exp(1j * phase_rad))))


def compare_band_phase(sig_before, sig_after, fs, band):
    sos = signal.butter(4, band, btype="bandpass", fs=fs, output="sos")
    before_bp = signal.sosfiltfilt(sos, sig_before)
    after_bp = signal.sosfiltfilt(sos, sig_after)
    phase_before = np.angle(signal.hilbert(before_bp))
    phase_after = np.angle(signal.hilbert(after_bp))
    phase_delta = np.angle(np.exp(1j * (phase_after - phase_before)))
    return {
        "phase_delta_deg": circular_mean_deg(phase_delta),
        "corrcoef": float(np.corrcoef(before_bp, after_bp)[0, 1]),
        "before_bp": before_bp,
        "after_bp": after_bp,
    }


def main():
    base = Path(__file__).resolve().parent
    neural_dir = base / "ExampleData" / "NeuralFile"
    behavior_dir = base / "ExampleData" / "BehaviorFile"
    out_fig = base / "phase_compensation_comparison.png"

    dp_before = DataProcessor(apply_lfp_phase_compensation=False)
    dp_after = DataProcessor(apply_lfp_phase_compensation=True)

    trials_before, stats_before, logs_before = dp_before.batch_process_directories(
        str(neural_dir), str(behavior_dir)
    )
    trials_after, stats_after, logs_after = dp_after.batch_process_directories(
        str(neural_dir), str(behavior_dir)
    )

    if not trials_before or not trials_after:
        raise RuntimeError(
            f"No trials loaded. before_logs={logs_before} after_logs={logs_after}"
        )

    first_key = sorted(set(trials_before.keys()) & set(trials_after.keys()))[0]
    trial_before = trials_before[first_key]
    trial_after = trials_after[first_key]

    lfp_t_ms, lfp_before = trial_before["lfp"]
    _, lfp_after = trial_after["lfp"]
    fs_lfp = infer_fs_from_time_ms(lfp_t_ms)

    channel = 0
    sig_before = lfp_before[channel]
    sig_after = lfp_after[channel]

    preview_mask = (lfp_t_ms >= -250) & (lfp_t_ms <= 1500)
    preview_t = lfp_t_ms[preview_mask]

    freqs = np.fft.rfftfreq(len(sig_before), d=1.0 / fs_lfp)
    spec_before = np.fft.rfft(sig_before)
    spec_after = np.fft.rfft(sig_after)
    mag_mask = np.abs(spec_before) > np.percentile(np.abs(spec_before), 40)
    freq_mask = (freqs >= 1) & (freqs <= 150) & mag_mask
    empirical_phase_delta_deg = np.degrees(
        np.unwrap(np.angle(spec_after[freq_mask] / (spec_before[freq_mask] + 1e-12)))
    )
    empirical_freqs = freqs[freq_mask]

    band_defs = {
        "theta_4_8": (4, 8),
        "beta_13_30": (13, 30),
        "gamma_30_80": (30, 80),
    }
    band_results = {
        name: compare_band_phase(sig_before, sig_after, fs_lfp, band)
        for name, band in band_defs.items()
    }

    fig, axes = plt.subplots(4, 1, figsize=(12, 15))

    axes[0].plot(preview_t, sig_before[preview_mask], label="Before compensation", alpha=0.8)
    axes[0].plot(preview_t, sig_after[preview_mask], label="After compensation", alpha=0.8)
    axes[0].axvline(0, color="k", linestyle="--", linewidth=1)
    axes[0].set_title(f"{first_key} | LFP ch{channel} waveform")
    axes[0].set_xlabel("Time from trial start (ms)")
    axes[0].set_ylabel("uV")
    axes[0].legend(loc="upper right")

    axes[1].plot(preview_t, (sig_after - sig_before)[preview_mask], color="tab:red")
    axes[1].axvline(0, color="k", linestyle="--", linewidth=1)
    axes[1].set_title("Waveform difference (after - before)")
    axes[1].set_xlabel("Time from trial start (ms)")
    axes[1].set_ylabel("uV")

    axes[2].plot(empirical_freqs, empirical_phase_delta_deg, color="tab:green")
    axes[2].set_title("Empirical phase delta spectrum (after / before)")
    axes[2].set_xlabel("Frequency (Hz)")
    axes[2].set_ylabel("Phase delta (deg)")

    summary_lines = [
        f"Trial: {first_key}",
        f"Stats before: {stats_before}",
        f"Stats after:  {stats_after}",
        (
            "Firmware phase compensation metadata: "
            f"{dp_after.lfp_phase_compensation_info}"
        ),
    ]
    for name, result in band_results.items():
        summary_lines.append(
            f"{name}: mean phase delta={result['phase_delta_deg']:.2f} deg, "
            f"band-limited corr={result['corrcoef']:.4f}"
        )

    axes[3].axis("off")
    axes[3].text(
        0.01,
        0.98,
        "\n".join(summary_lines),
        va="top",
        ha="left",
        family="monospace",
        fontsize=10,
    )

    fig.subplots_adjust(hspace=0.5, top=0.96, bottom=0.05)
    fig.savefig(out_fig, dpi=150)

    print(f"Saved comparison figure to: {out_fig}")
    print(f"Selected trial: {first_key}")
    for name, result in band_results.items():
        print(
            f"{name}: phase delta={result['phase_delta_deg']:.2f} deg, "
            f"band corr={result['corrcoef']:.4f}"
        )


if __name__ == "__main__":
    main()
