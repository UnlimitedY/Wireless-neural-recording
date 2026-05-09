from __future__ import annotations

import csv
import datetime as dt
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
ROOT = BASE.parent
FIG_DIR = BASE / "figures"
MPLCONFIG = BASE / ".mplconfig"
MPLCONFIG.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("MPLCONFIGDIR", str(MPLCONFIG))

import matplotlib

matplotlib.use("Agg")

import matplotlib.image as mpimg
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import gridspec
from matplotlib.patches import Circle, FancyArrowPatch, FancyBboxPatch, Rectangle


FIG_DIR.mkdir(parents=True, exist_ok=True)

COLORS = {
    "ink": "#1f2933",
    "muted": "#607080",
    "grid": "#d6dde3",
    "blue": "#2f6f9f",
    "teal": "#1f9a8a",
    "green": "#5a9a48",
    "orange": "#d9852b",
    "red": "#c75146",
    "purple": "#7557a8",
    "yellow": "#d5a928",
    "light": "#f5f7f9",
}


def setup_style():
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 8,
            "axes.titlesize": 9,
            "axes.labelsize": 8,
            "xtick.labelsize": 7,
            "ytick.labelsize": 7,
            "legend.fontsize": 7,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.edgecolor": COLORS["ink"],
            "axes.labelcolor": COLORS["ink"],
            "xtick.color": COLORS["ink"],
            "ytick.color": COLORS["ink"],
            "figure.facecolor": "white",
            "savefig.facecolor": "white",
            "pdf.fonttype": 42,
            "ps.fonttype": 42,
        }
    )


def panel_label(ax, label):
    ax.text(
        -0.08,
        1.04,
        label,
        transform=ax.transAxes,
        fontsize=12,
        fontweight="bold",
        va="bottom",
        ha="left",
        color=COLORS["ink"],
    )


def panel_note(ax, text):
    ax.text(
        0.98,
        0.96,
        text,
        transform=ax.transAxes,
        va="top",
        ha="right",
        fontsize=6.5,
        color=COLORS["muted"],
        bbox=dict(boxstyle="round,pad=0.18", fc="white", ec=COLORS["grid"], lw=0.5, alpha=0.9),
    )


def rounded_box(ax, xy, width, height, label, fc, ec=None, fontsize=8):
    ec = ec or COLORS["ink"]
    patch = FancyBboxPatch(
        xy,
        width,
        height,
        boxstyle="round,pad=0.03,rounding_size=0.03",
        linewidth=1,
        facecolor=fc,
        edgecolor=ec,
    )
    ax.add_patch(patch)
    ax.text(
        xy[0] + width / 2,
        xy[1] + height / 2,
        label,
        ha="center",
        va="center",
        fontsize=fontsize,
        color=COLORS["ink"],
        linespacing=1.2,
    )
    return patch


def arrow(ax, start, end, color=None, lw=1.2):
    ax.add_patch(
        FancyArrowPatch(
            start,
            end,
            arrowstyle="-|>",
            mutation_scale=10,
            linewidth=lw,
            color=color or COLORS["ink"],
            shrinkA=4,
            shrinkB=4,
        )
    )


def load_behavior():
    path = ROOT / "BehaviorData" / "BW08" / "Trial.txt"
    rows = []
    if not path.exists():
        return rows
    for line in path.read_text(errors="ignore").splitlines():
        parts = line.split()
        if len(parts) < 12:
            continue
        rows.append(
            {
                "timestamp": int(parts[0]),
                "trial": int(parts[1]),
                "protocol": int(parts[2]),
                "trial_type": int(parts[3]),
                "outcome": int(parts[4]),
                "stim1": float(parts[9]),
                "stim2": float(parts[10]),
                "early": int(parts[11]),
            }
        )
    return rows


def behavior_summary(rows):
    by_day = defaultdict(list)
    by_protocol = defaultdict(list)
    for r in rows:
        by_day[dt.datetime.fromtimestamp(r["timestamp"]).date()].append(r)
        by_protocol[r["protocol"]].append(r)
    daily = []
    for day, sub in sorted(by_day.items()):
        valid = [r for r in sub if r["outcome"] in (1, 2)]
        perf = np.nan if not valid else sum(r["outcome"] == 1 for r in valid) / len(valid)
        daily.append((day, len(sub), perf))
    protocol = []
    for pr, sub in sorted(by_protocol.items()):
        valid = [r for r in sub if r["outcome"] in (1, 2)]
        perf = np.nan if not valid else sum(r["outcome"] == 1 for r in valid) / len(valid)
        protocol.append((pr, len(sub), len(valid), perf))
    return daily, protocol


def load_decoding_summary():
    path = ROOT / "DataAnaysisScirpt" / "Trial_Level_Mode3_GUI" / "decoding_outputs" / "protocol5" / "protocol5_decoding_summary.json"
    if path.exists():
        return json.loads(path.read_text())
    return {}


def load_decoding_predictions():
    path = ROOT / "DataAnaysisScirpt" / "Trial_Level_Mode3_GUI" / "decoding_outputs" / "protocol5" / "protocol5_trial_predictions.csv"
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def load_battery_history():
    histories = {}
    for path in sorted((ROOT / "runtime_cache").glob("cage_*/battery_history.json")):
        try:
            rows = json.loads(path.read_text())
        except Exception:
            continue
        valid = [r for r in rows if float(r.get("voltage", 0) or 0) > 2500 and float(r.get("rsoc", 0) or 0) > 0]
        histories[path.parent.name] = valid
    return histories


def savefig(fig, name):
    for suffix in (".png", ".pdf"):
        fig.savefig(FIG_DIR / f"{name}{suffix}", dpi=300, bbox_inches="tight")
    plt.close(fig)


def figure1(rows):
    histories = load_battery_history()
    fig = plt.figure(figsize=(12.4, 8.7))
    gs = gridspec.GridSpec(2, 3, figure=fig, height_ratios=[1.1, 0.95], width_ratios=[1.15, 1.0, 1.0], wspace=0.38, hspace=0.42)

    ax = fig.add_subplot(gs[0, 0])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    panel_label(ax, "a")
    ax.set_title("Wireless home-cage recording architecture", loc="left", pad=8)
    rounded_box(ax, (0.06, 0.62), 0.24, 0.18, "Implant\nRHD + MCU\nIMU + battery", "#d9edf0", COLORS["teal"])
    rounded_box(ax, (0.06, 0.2), 0.24, 0.16, "Charging\nreceiver\nand cell", "#fff1d6", COLORS["orange"])
    rounded_box(ax, (0.42, 0.58), 0.24, 0.2, "RF base\nESB 2 Mbps\nchannel scan", "#e6eef8", COLORS["blue"])
    rounded_box(ax, (0.73, 0.58), 0.2, 0.2, "Host PC\nEDF/H5\nGUI", "#ece7f5", COLORS["purple"])
    rounded_box(ax, (0.42, 0.18), 0.24, 0.17, "Habits\nbehavior\ncontroller", "#e9f3df", COLORS["green"])
    rounded_box(ax, (0.73, 0.18), 0.2, 0.17, "Camera\nand charge\nsafety", "#f7e3df", COLORS["red"])
    arrow(ax, (0.30, 0.72), (0.42, 0.70), COLORS["blue"])
    arrow(ax, (0.66, 0.68), (0.73, 0.68), COLORS["blue"])
    arrow(ax, (0.54, 0.58), (0.54, 0.35), COLORS["green"])
    arrow(ax, (0.30, 0.28), (0.42, 0.27), COLORS["orange"])
    arrow(ax, (0.66, 0.28), (0.73, 0.27), COLORS["red"])
    ax.text(0.18, 0.49, "continuous power\nand neural data", ha="center", va="center", fontsize=7, color=COLORS["muted"])

    ax = fig.add_subplot(gs[0, 1])
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    panel_label(ax, "b")
    ax.set_title("Implant signal and power paths", loc="left", pad=8)
    labels = [
        ("16-site probe", 0.05, 0.65, "#f2f6fa", COLORS["ink"]),
        ("RHD2132\nfront end", 0.33, 0.65, "#d9edf0", COLORS["teal"]),
        ("On-board\nDSP", 0.60, 0.65, "#e6eef8", COLORS["blue"]),
        ("nRF ESB\nradio", 0.60, 0.35, "#ece7f5", COLORS["purple"]),
        ("Li-ion +\nfuel gauge", 0.33, 0.22, "#fff1d6", COLORS["orange"]),
        ("Wireless\ncharging", 0.05, 0.22, "#f7e3df", COLORS["red"]),
    ]
    for text, x, y, fc, ec in labels:
        rounded_box(ax, (x, y), 0.22, 0.13, text, fc, ec, fontsize=7.5)
    for s, e, c in [
        ((0.27, 0.72), (0.33, 0.72), COLORS["teal"]),
        ((0.55, 0.72), (0.60, 0.72), COLORS["blue"]),
        ((0.71, 0.65), (0.71, 0.48), COLORS["purple"]),
        ((0.27, 0.29), (0.33, 0.29), COLORS["orange"]),
        ((0.44, 0.35), (0.60, 0.42), COLORS["orange"]),
    ]:
        arrow(ax, s, e, c)
    ax.text(0.05, 0.08, "Mode 3 streams LFP, ESA, spike raster, IMU and battery state.", fontsize=7, color=COLORS["muted"])

    ax = fig.add_subplot(gs[0, 2])
    panel_label(ax, "c")
    ax.set_title("Firmware modes and packetized telemetry", loc="left", pad=8)
    modes = ["Mode 0\nLFP", "Mode 1\n1-ch\nspike", "Mode 2\n4-ch\nspike", "Mode 3\nLFP+ESA\n+raster"]
    short_counts = [77, 109, 126, 98]
    colors = [COLORS["blue"], COLORS["orange"], COLORS["red"], COLORS["teal"]]
    ax.bar(np.arange(4), short_counts, color=colors, alpha=0.82)
    ax.axhline(126, ls="--", lw=0.8, color=COLORS["muted"])
    ax.set_xticks(np.arange(4))
    ax.set_xticklabels(modes)
    ax.set_ylabel("u16 words per packet")
    ax.set_ylim(0, 140)
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    ax.text(2.95, 128, "252-byte ESB payload limit", ha="right", va="bottom", fontsize=6.5, color=COLORS["muted"])
    panel_note(ax, "firmware-derived")

    ax = fig.add_subplot(gs[1, 0])
    panel_label(ax, "d")
    ax.set_title("Runtime power and RF telemetry", loc="left", pad=8)
    for cage, valid in histories.items():
        if not valid:
            continue
        t0 = min(r["timestamp_epoch"] for r in valid)
        x = np.array([(r["timestamp_epoch"] - t0) / 60 for r in valid])
        y = np.array([float(r.get("voltage", np.nan)) / 1000 for r in valid])
        ax.plot(x, y, lw=1.0, label=cage.replace("_", " "), alpha=0.9)
    ax.set_xlabel("Runtime in cache (min)")
    ax.set_ylabel("Battery voltage (V)")
    ax.grid(color=COLORS["grid"], lw=0.6)
    ax.legend(frameon=False, loc="lower right")
    panel_note(ax, "runtime cache")

    ax = fig.add_subplot(gs[1, 1])
    panel_label(ax, "e")
    ax.set_title("Behavior cage preview and charge guard", loc="left", pad=8)
    preview = ROOT / "runtime_cache" / "cage_3" / "camera_preview.jpg"
    if preview.exists():
        img = mpimg.imread(preview)
        ax.imshow(img)
        ax.axis("off")
    else:
        ax.text(0.5, 0.5, "camera preview unavailable", ha="center", va="center")
        ax.axis("off")
    panel_note(ax, "workspace image")

    ax = fig.add_subplot(gs[1, 2])
    panel_label(ax, "f")
    ax.set_title("End-to-end data products", loc="left", pad=8)
    ax.set_axis_off()
    products = [
        ("continuous EDF", "16 ch LFP\n1 kHz"),
        ("Mode 3 EDF", "LFP + ESA + raster\n1 kHz"),
        ("raw EDF", "single-channel raw\n12.5 kHz"),
        ("daily H5", "24 h timeline\nstate features"),
        ("trial H5", "trial-centered\nraster/decoder"),
    ]
    y = 0.82
    for name, desc in products:
        rounded_box(ax, (0.08, y - 0.08), 0.32, 0.11, name, "#f2f6fa", COLORS["blue"], fontsize=7.5)
        rounded_box(ax, (0.52, y - 0.08), 0.36, 0.11, desc, "#ffffff", COLORS["grid"], fontsize=7)
        arrow(ax, (0.40, y - 0.025), (0.52, y - 0.025), COLORS["muted"], lw=0.9)
        y -= 0.16
    savefig(fig, "fig1_system_setup")


def figure2(rows):
    daily, protocol = behavior_summary(rows)
    rng = np.random.default_rng(3)
    fig = plt.figure(figsize=(12.2, 8.6))
    gs = gridspec.GridSpec(2, 3, figure=fig, height_ratios=[0.9, 1.05], wspace=0.38, hspace=0.45)

    ax = fig.add_subplot(gs[0, 0])
    panel_label(ax, "a")
    ax.set_title("Synchronization model", loc="left", pad=8)
    ax.set_xlim(0, 10)
    ax.set_ylim(0, 3.2)
    ax.set_yticks([2.5, 1.6, 0.7])
    ax.set_yticklabels(["behavior", "radio packets", "host files"])
    ax.set_xlabel("Time around trial start (s)")
    for t in [1.2, 3.6, 6.5, 8.6]:
        ax.vlines(t, 2.25, 2.75, color=COLORS["green"], lw=2)
        ax.vlines(t + 0.03, 1.35, 1.85, color=COLORS["blue"], lw=1.2)
    packet_times = np.arange(0.5, 9.7, 0.35)
    ax.scatter(packet_times, np.ones_like(packet_times) * 1.6, s=8, color=COLORS["blue"], alpha=0.8)
    ax.hlines(0.7, 0.2, 9.8, color=COLORS["muted"], lw=1.8)
    ax.text(0.2, 0.25, "hardware timestamps + EDF annotations + trial text", color=COLORS["muted"], fontsize=7)
    ax.grid(axis="x", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "schematic")

    ax = fig.add_subplot(gs[0, 1])
    panel_label(ax, "b")
    ax.set_title("Behavioral throughput during longitudinal training", loc="left", pad=8)
    days = [d.strftime("%m-%d") for d, _, _ in daily]
    counts = [n for _, n, _ in daily]
    perf = [p for _, _, p in daily]
    x = np.arange(len(days))
    ax.bar(x, counts, color=COLORS["teal"], alpha=0.75)
    ax.set_xticks(x)
    ax.set_xticklabels(days, rotation=35, ha="right")
    ax.set_ylabel("Trials per day")
    ax2 = ax.twinx()
    ax2.plot(x, np.array(perf) * 100, color=COLORS["orange"], marker="o", lw=1.5)
    ax2.set_ylabel("Valid-trial performance (%)")
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "BW08 Trial.txt")

    ax = fig.add_subplot(gs[0, 2])
    panel_label(ax, "c")
    ax.set_title("Protocol progression", loc="left", pad=8)
    prs = [p for p, _, _, _ in protocol]
    ns = [n for _, n, _, _ in protocol]
    perfs = [p for _, _, _, p in protocol]
    ax.bar(np.arange(len(prs)), ns, color=COLORS["blue"], alpha=0.75)
    ax.set_xticks(np.arange(len(prs)))
    ax.set_xticklabels([str(p) for p in prs])
    ax.set_xlabel("Protocol")
    ax.set_ylabel("Trials")
    ax2 = ax.twinx()
    ax2.plot(np.arange(len(prs)), np.array(perfs) * 100, color=COLORS["red"], marker="s", lw=1.4)
    ax2.set_ylabel("Performance (%)")
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "BW08 Trial.txt")

    ax = fig.add_subplot(gs[1, 0])
    panel_label(ax, "d")
    ax.set_title("Representative state-dependent LFP structure", loc="left", pad=8)
    t = np.linspace(0, 10, 2500)
    states = [
        ("Wake", 8, 0.35, COLORS["green"]),
        ("NREM", 2, 0.9, COLORS["blue"]),
        ("REM", 7, 0.75, COLORS["purple"]),
        ("Task", 35, 0.30, COLORS["orange"]),
    ]
    for i, (name, freq, amp, color) in enumerate(states):
        sig = amp * np.sin(2 * np.pi * freq * t) + 0.15 * rng.standard_normal(t.size)
        ax.plot(t, sig + i * 2.0, color=color, lw=0.8, label=name)
    ax.set_yticks(np.arange(len(states)) * 2.0)
    ax.set_yticklabels([s[0] for s in states])
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("State")
    ax.grid(axis="x", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "illustrative trace")

    ax = fig.add_subplot(gs[1, 1])
    panel_label(ax, "e")
    ax.set_title("State classifier feature space", loc="left", pad=8)
    centers = {
        "Wake": (0.45, 0.72, COLORS["green"]),
        "NREM": (0.82, 0.25, COLORS["blue"]),
        "REM": (0.25, 0.32, COLORS["purple"]),
        "Working": (0.62, 0.88, COLORS["orange"]),
    }
    for name, (cx, cy, color) in centers.items():
        pts = rng.normal([cx, cy], [0.08, 0.07], size=(80, 2))
        ax.scatter(pts[:, 0], pts[:, 1], s=10, color=color, alpha=0.65, label=name)
    ax.set_xlabel("Delta / broadband power")
    ax.set_ylabel("IMU activity or theta ratio")
    ax.set_xlim(0, 1.1)
    ax.set_ylim(0, 1.1)
    ax.grid(color=COLORS["grid"], lw=0.6)
    ax.legend(frameon=False, ncol=2, loc="lower right")
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[1, 2])
    panel_label(ax, "f")
    ax.set_title("Behavior-linked spectral summary", loc="left", pad=8)
    bands = ["delta", "theta", "gamma"]
    vals = np.array([[1.0, 0.55, 0.48], [0.52, 1.0, 0.62], [0.35, 0.74, 1.0], [0.42, 0.66, 1.25]])
    im = ax.imshow(vals, cmap="viridis", aspect="auto", vmin=0.25, vmax=1.3)
    ax.set_xticks(range(len(bands)))
    ax.set_xticklabels(bands)
    ax.set_yticks(range(4))
    ax.set_yticklabels(["NREM", "REM", "Wake", "Working"])
    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("Normalized power")
    panel_note(ax, "analysis template")
    savefig(fig, "fig2_long_term_recording")


def synthetic_trial_data():
    rng = np.random.default_rng(8)
    n_trials = 80
    t = np.linspace(-0.5, 1.0, 300)
    choice = rng.integers(0, 2, size=n_trials)
    spikes = []
    for i in range(n_trials):
        base = rng.uniform(0.04, 0.12)
        mod = 0.12 if choice[i] else -0.04
        rate = base + mod * np.exp(-((t - 0.12) / 0.18) ** 2)
        spikes.append(rng.random(t.size) < np.clip(rate, 0.01, 0.35))
    lfp = np.sin(2 * np.pi * 8 * t) * np.exp(-((t - 0.12) / 0.35) ** 2)
    lfp += 0.25 * rng.standard_normal(t.size)
    esa = np.exp(-((t - 0.08) / 0.12) ** 2) + 0.2 * rng.random(t.size)
    return t, np.asarray(spikes), choice, lfp, esa


def figure3(rows):
    summary = load_decoding_summary()
    preds = load_decoding_predictions()
    fig = plt.figure(figsize=(12.4, 8.4))
    gs = gridspec.GridSpec(2, 3, figure=fig, height_ratios=[0.92, 1.05], wspace=0.55, hspace=0.58)

    ax = fig.add_subplot(gs[0, 0])
    panel_label(ax, "a")
    ax.set_axis_off()
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_title("DB rule-switching task", loc="left", pad=8)
    steps = [
        ("Self-init", 0.05, COLORS["green"]),
        ("Cue", 0.27, COLORS["blue"]),
        ("Choice", 0.49, COLORS["orange"]),
        ("Reward\nor timeout", 0.72, COLORS["red"]),
    ]
    for text, x, color in steps:
        rounded_box(ax, (x, 0.60), 0.17, 0.13, text, "#ffffff", color, fontsize=7.5)
    for i in range(len(steps) - 1):
        arrow(ax, (steps[i][1] + 0.17, 0.665), (steps[i + 1][1], 0.665), COLORS["muted"])
    rules = [("Location", "P5/6", COLORS["blue"]), ("Frequency", "P7/8", COLORS["teal"]), ("Reversal", "P9/10", COLORS["purple"])]
    for i, (name, p, color) in enumerate(rules):
        rounded_box(ax, (0.12 + i * 0.27, 0.28), 0.2, 0.12, f"{name}\n{p}", "#f2f6fa", color, fontsize=7.5)
    ax.text(
        0.07,
        0.10,
        "Protocol transitions are driven by valid-trial count\nand side-specific performance.",
        fontsize=6.8,
        color=COLORS["muted"],
        linespacing=1.15,
    )

    ax = fig.add_subplot(gs[0, 1])
    panel_label(ax, "b")
    ax.set_title("Trial-aligned LFP, ESA and raster", loc="left", pad=8)
    t, spikes, choice, lfp, esa = synthetic_trial_data()
    ax.plot(t, lfp * 0.55 + 2.1, color=COLORS["blue"], lw=0.9, label="LFP")
    ax.plot(t, esa + 0.6, color=COLORS["teal"], lw=0.9, label="ESA")
    for i in range(spikes.shape[0]):
        xs = t[spikes[i]]
        ax.vlines(xs, -0.05 + i * 0.006, -0.02 + i * 0.006, color=COLORS["ink"], lw=0.25)
    ax.axvline(0, color=COLORS["red"], lw=1.0, ls="--")
    ax.set_xlabel("Time from trial start (s)")
    ax.set_yticks([0.6, 2.1])
    ax.set_yticklabels(["ESA", "LFP"])
    ax.set_xlim(-0.5, 1.0)
    ax.set_ylim(-0.1, 3.1)
    ax.legend(frameon=False, loc="upper right")
    ax.text(
        0.02,
        0.96,
        "illustrative trace",
        transform=ax.transAxes,
        va="top",
        ha="left",
        fontsize=6.5,
        color=COLORS["muted"],
        bbox=dict(boxstyle="round,pad=0.18", fc="white", ec=COLORS["grid"], lw=0.5, alpha=0.9),
    )

    ax = fig.add_subplot(gs[0, 2])
    panel_label(ax, "c")
    ax.set_title("Cross-validated decoding from trial-start window", loc="left", pad=8)
    modalities = ["LFP", "ESA", "IMU"]
    x = np.arange(len(modalities))
    width = 0.35
    out = [summary.get("outcome_decoding", {}).get(m, {}).get("balanced_accuracy", np.nan) * 100 for m in modalities]
    cho = [summary.get("choice_decoding", {}).get(m, {}).get("balanced_accuracy", np.nan) * 100 for m in modalities]
    ax.bar(x - width / 2, out, width, color=COLORS["blue"], label="Outcome")
    ax.bar(x + width / 2, cho, width, color=COLORS["orange"], label="Choice")
    ax.axhline(50, color=COLORS["muted"], lw=0.8, ls="--")
    ax.set_xticks(x)
    ax.set_xticklabels(modalities)
    ax.set_ylabel("Balanced accuracy (%)")
    ax.set_ylim(40, 75)
    ax.legend(frameon=False, loc="upper left")
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    n_trials = summary.get("n_trials", len(preds))
    ax.text(0.98, 0.07, f"n = {n_trials} trials", transform=ax.transAxes, ha="right", color=COLORS["muted"], fontsize=7)
    panel_note(ax, "analysis output")

    ax = fig.add_subplot(gs[1, 0])
    panel_label(ax, "d")
    ax.set_title("Outcome confusion matrices", loc="left", pad=8)
    matrices = [summary.get("outcome_decoding", {}).get(m, {}).get("confusion_matrix", [[np.nan, np.nan], [np.nan, np.nan]]) for m in modalities]
    combined = np.block([[np.asarray(m) for m in matrices]])
    im = ax.imshow(combined, cmap="Blues")
    ax.set_xticks([0, 1, 2, 3, 4, 5])
    ax.set_xticklabels(["LFP\nErr", "Cor", "ESA\nErr", "Cor", "IMU\nErr", "Cor"])
    ax.set_yticks([0, 1])
    ax.set_yticklabels(["Err", "Cor"])
    ax.set_xlabel("Predicted class")
    ax.set_ylabel("True class")
    for i, m in enumerate(matrices):
        arr = np.asarray(m)
        for r in range(2):
            for c in range(2):
                ax.text(i * 2 + c, r, str(int(arr[r, c])), ha="center", va="center", fontsize=8, color=COLORS["ink"])
        if i > 0:
            ax.axvline(i * 2 - 0.5, color="white", lw=2)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="Trial count")
    panel_note(ax, "analysis output")

    ax = fig.add_subplot(gs[1, 1])
    panel_label(ax, "e")
    ax.set_title("Decoding confidence across trials", loc="left", pad=8)
    if preds:
        trial = np.array([int(r["trial_num"]) for r in preds])
        lfp = np.array([float(r["lfp_outcome_prob_correct"]) for r in preds])
        esa = np.array([float(r["esa_outcome_prob_correct"]) for r in preds])
        imu = np.array([float(r["imu_outcome_prob_correct"]) for r in preds])
        ax.plot(trial, lfp, lw=0.8, color=COLORS["blue"], alpha=0.75, label="LFP")
        ax.plot(trial, esa, lw=0.8, color=COLORS["teal"], alpha=0.75, label="ESA")
        ax.plot(trial, imu, lw=0.8, color=COLORS["orange"], alpha=0.75, label="IMU")
    ax.set_xlabel("Trial number")
    ax.set_ylabel("P(correct)")
    ax.set_ylim(0, 1.05)
    ax.grid(color=COLORS["grid"], lw=0.6)
    ax.legend(frameon=False, ncol=3, loc="lower right")
    panel_note(ax, "analysis output")

    ax = fig.add_subplot(gs[1, 2])
    panel_label(ax, "f")
    ax.set_title("Learning and rule transitions", loc="left", pad=8)
    protocols = sorted(set(r["protocol"] for r in rows))
    counts = Counter(r["protocol"] for r in rows)
    valid_perf = []
    for p in protocols:
        sub = [r for r in rows if r["protocol"] == p and r["outcome"] in (1, 2)]
        valid_perf.append(sum(r["outcome"] == 1 for r in sub) / len(sub) if sub else np.nan)
    ax.bar(np.arange(len(protocols)), [counts[p] for p in protocols], color=COLORS["muted"], alpha=0.35)
    ax.set_ylabel("Trials")
    ax.set_xticks(np.arange(len(protocols)))
    ax.set_xticklabels([str(p) for p in protocols])
    ax2 = ax.twinx()
    ax2.plot(np.arange(len(protocols)), np.array(valid_perf) * 100, marker="o", color=COLORS["red"], lw=1.5)
    ax2.set_ylim(0, 100)
    ax2.set_ylabel("Performance (%)", labelpad=8)
    ax.set_xlabel("Protocol")
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "BW08 Trial.txt")
    savefig(fig, "fig3_spiking_task")


def figure4():
    rng = np.random.default_rng(14)
    fig = plt.figure(figsize=(12.2, 8.2))
    gs = gridspec.GridSpec(2, 3, figure=fig, wspace=0.38, hspace=0.45)

    hours = np.linspace(0, 24, 241)
    freqs = np.linspace(1, 120, 120)
    H, F = np.meshgrid(hours, freqs)
    circ = 0.5 + 0.35 * np.sin(2 * np.pi * (H - 13) / 24)
    spec = 0.8 * np.exp(-((F - 3.0) / 2.5) ** 2) * (1.1 - circ)
    spec += 0.65 * np.exp(-((F - 8.0) / 3.0) ** 2) * circ
    spec += 0.25 * np.exp(-((F - 45.0) / 20.0) ** 2) * (0.7 + circ)
    spec += 0.05 * rng.standard_normal(spec.shape)

    ax = fig.add_subplot(gs[0, 0:2])
    panel_label(ax, "a")
    ax.set_title("Twenty-four hour LFP rhythm analysis", loc="left", pad=8)
    im = ax.imshow(spec, origin="lower", extent=[0, 24, 1, 120], aspect="auto", cmap="magma")
    ax.axvspan(0, 6, color="#111111", alpha=0.12)
    ax.axvspan(18, 24, color="#111111", alpha=0.12)
    ax.set_xlabel("Zeitgeber time (h)")
    ax.set_ylabel("Frequency (Hz)")
    ax.set_yticks([1, 4, 8, 30, 80, 120])
    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.02)
    cb.set_label("Power")
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[0, 2])
    panel_label(ax, "b")
    ax.set_title("Hourly band-power trajectories", loc="left", pad=8)
    h = np.arange(24)
    delta = 1.0 + 0.35 * np.cos(2 * np.pi * (h - 4) / 24)
    theta = 1.0 + 0.32 * np.sin(2 * np.pi * (h - 12) / 24)
    gamma = 1.0 + 0.22 * np.sin(2 * np.pi * (h - 14) / 24)
    ax.plot(h, delta, color=COLORS["blue"], label="delta")
    ax.plot(h, theta, color=COLORS["green"], label="theta")
    ax.plot(h, gamma, color=COLORS["orange"], label="gamma")
    ax.axvspan(0, 6, color="#111111", alpha=0.08)
    ax.axvspan(18, 24, color="#111111", alpha=0.08)
    ax.set_xlabel("ZT (h)")
    ax.set_ylabel("Normalized power")
    ax.legend(frameon=False)
    ax.grid(color=COLORS["grid"], lw=0.6)
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[1, 0])
    panel_label(ax, "c")
    ax.set_title("Behavior-neural coupling", loc="left", pad=8)
    activity = 1.0 + 0.55 * np.sin(2 * np.pi * (h - 12) / 24)
    theta_delta = theta / delta
    ax.scatter(activity, theta_delta, c=h, cmap="twilight", s=36, edgecolor="white", linewidth=0.5)
    z = np.polyfit(activity, theta_delta, 1)
    xs = np.linspace(activity.min(), activity.max(), 100)
    ax.plot(xs, np.polyval(z, xs), color=COLORS["ink"], lw=1)
    ax.set_xlabel("IMU activity")
    ax.set_ylabel("Theta/delta")
    ax.grid(color=COLORS["grid"], lw=0.6)
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[1, 1])
    panel_label(ax, "d")
    ax.set_title("Phase map across cortical groups", loc="left", pad=8)
    groups = ["MOs", "ACAd", "PL", "ILA"]
    acrophase = np.array([13.0, 13.8, 14.4, 15.2])
    amp = np.array([0.36, 0.42, 0.30, 0.25])
    ax.bar(groups, amp, color=[COLORS["blue"], COLORS["teal"], COLORS["orange"], COLORS["purple"]], alpha=0.8)
    for i, ph in enumerate(acrophase):
        ax.text(i, amp[i] + 0.025, f"ZT{ph:.1f}", ha="center", fontsize=7, color=COLORS["ink"])
    ax.set_ylabel("24 h modulation amplitude")
    ax.set_ylim(0, 0.55)
    ax.grid(axis="y", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[1, 2])
    panel_label(ax, "e")
    ax.set_title("Rhythm statistics report", loc="left", pad=8)
    metrics = ["amplitude", "regularity", "state norm.", "coverage"]
    values = [0.42, 0.71, 0.64, 0.88]
    ax.barh(metrics, values, color=[COLORS["teal"], COLORS["green"], COLORS["orange"], COLORS["blue"]])
    ax.set_xlim(0, 1)
    ax.set_xlabel("Quality or effect size")
    ax.grid(axis="x", color=COLORS["grid"], lw=0.6)
    panel_note(ax, "analysis template")
    savefig(fig, "fig4_24h_neural_signal")


def figure5(rows):
    rng = np.random.default_rng(24)
    fig = plt.figure(figsize=(12.2, 8.4))
    gs = gridspec.GridSpec(2, 3, figure=fig, wspace=0.38, hspace=0.45)

    ax = fig.add_subplot(gs[0, 0:2])
    panel_label(ax, "a")
    ax.set_title("Longitudinal sleep-state monitoring", loc="left", pad=8)
    t = np.linspace(0, 72, 720)
    states = np.zeros_like(t)
    for i, x in enumerate(t):
        phase = x % 24
        sleep_bias = 0.65 if 6 <= phase <= 18 else 0.25
        r = rng.random()
        if r < sleep_bias * 0.65:
            states[i] = 1
        elif r < sleep_bias * 0.78:
            states[i] = 2
        elif r > 0.86:
            states[i] = 3
        else:
            states[i] = 0
    colors = np.array(["#5a9a48", "#2f6f9f", "#7557a8", "#d9852b"])
    ax.imshow(states[np.newaxis, :], aspect="auto", extent=[0, 72, 0, 1], cmap=matplotlib.colors.ListedColormap(colors))
    for day in [24, 48]:
        ax.axvline(day, color="white", lw=1.5)
    ax.set_yticks([])
    ax.set_xlabel("Recording time (h)")
    ax.set_xlim(0, 72)
    legend_handles = [Rectangle((0, 0), 1, 1, color=c) for c in colors]
    ax.legend(legend_handles, ["Wake", "NREM", "REM", "Working"], frameon=False, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.12))
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[0, 2])
    panel_label(ax, "b")
    ax.set_title("Sleep architecture metrics", loc="left", pad=8)
    labels = ["Wake", "NREM", "REM", "Working"]
    occupancy = [(states == i).mean() * 100 for i in range(4)]
    ax.pie(occupancy, labels=labels, colors=colors, startangle=90, autopct="%1.0f%%", textprops={"fontsize": 7})
    panel_note(ax, "analysis template")

    ax = fig.add_subplot(gs[1, 0])
    panel_label(ax, "c")
    ax.set_title("Longitudinal sleep neural markers", loc="left", pad=8)
    days = np.arange(1, 8)
    nrem_delta = 1.05 - 0.035 * days + rng.normal(0, 0.025, size=days.size)
    rem_theta = 0.8 + 0.04 * np.sin(days / 2) + rng.normal(0, 0.02, size=days.size)
    ax.plot(days, nrem_delta, marker="o", color=COLORS["blue"], label="NREM delta")
    ax.plot(days, rem_theta, marker="s", color=COLORS["purple"], label="REM theta")
    ax.set_xlabel("Day")
    ax.set_ylabel("Normalized power")
    ax.legend(frameon=False)
    ax.grid(color=COLORS["grid"], lw=0.6)
    panel_note(ax, "hypothesis template")

    ax = fig.add_subplot(gs[1, 1])
    panel_label(ax, "d")
    ax.set_title("Sleep-task coupling readout", loc="left", pad=8)
    daily, _ = behavior_summary(rows)
    perf = np.array([p for _, _, p in daily if np.isfinite(p)])
    if perf.size:
        sleep_metric = np.linspace(0.4, 0.85, perf.size) + rng.normal(0, 0.05, perf.size)
        ax.scatter(sleep_metric, perf * 100, s=38, color=COLORS["orange"], edgecolor="white", linewidth=0.6)
        if perf.size > 1:
            z = np.polyfit(sleep_metric, perf * 100, 1)
            xs = np.linspace(sleep_metric.min(), sleep_metric.max(), 100)
            ax.plot(xs, np.polyval(z, xs), color=COLORS["ink"], lw=1)
    ax.set_xlabel("Prior sleep consolidation index")
    ax.set_ylabel("Next-session performance (%)")
    ax.grid(color=COLORS["grid"], lw=0.6)
    panel_note(ax, "hypothesis template")

    ax = fig.add_subplot(gs[1, 2])
    panel_label(ax, "e")
    ax.set_title("Analysis outputs for validation", loc="left", pad=8)
    ax.set_axis_off()
    steps = [
        ("10 s state labels", "#f2f6fa", COLORS["blue"]),
        ("bout statistics", "#e9f3df", COLORS["green"]),
        ("band-power trends", "#fff1d6", COLORS["orange"]),
        ("task/sleep model", "#ece7f5", COLORS["purple"]),
    ]
    y = 0.80
    for i, (text, fc, ec) in enumerate(steps):
        rounded_box(ax, (0.12, y - 0.07), 0.58, 0.12, text, fc, ec, fontsize=8)
        if i < len(steps) - 1:
            arrow(ax, (0.41, y - 0.07), (0.41, y - 0.18), COLORS["muted"])
        y -= 0.20
    savefig(fig, "fig5_longitudinal_sleep")


def write_source_data(rows):
    out = FIG_DIR / "source_data_behavior_summary.csv"
    daily, protocol = behavior_summary(rows)
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["day", "n_trials", "valid_trial_performance"])
        for day, n, perf in daily:
            writer.writerow([day.isoformat(), n, f"{perf:.6f}" if np.isfinite(perf) else ""])
    out = FIG_DIR / "source_data_protocol_summary.csv"
    with out.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["protocol", "n_trials", "n_valid_trials", "valid_trial_performance"])
        for pr, n, valid, perf in protocol:
            writer.writerow([pr, n, valid, f"{perf:.6f}" if np.isfinite(perf) else ""])


def main():
    setup_style()
    rows = load_behavior()
    write_source_data(rows)
    figure1(rows)
    figure2(rows)
    figure3(rows)
    figure4()
    figure5(rows)
    print(f"Wrote figures to {FIG_DIR}")


if __name__ == "__main__":
    main()
