import base64
import io
import json
import os
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")

import h5py
import numpy as np
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

from .config import resolve_config
from .state_classification import STATE_CODES


def _fig_to_html(fig, embed_png):
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight")
    plt.close(fig)
    if embed_png:
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f'<img src="data:image/png;base64,{encoded}" style="max-width:100%;">'
    return buffer.getvalue()


def _state_color_map():
    return {
        "Wake": "#2f80ed",
        "MiniWake": "#7f7f7f",
        "NREM": "#f2c94c",
        "REM": "#eb5757",
        "Working": "#9b51e0",
    }


def _night_intervals(light_on_hour, light_off_hour, x_min=0.0, x_max=24.0):
    intervals = []
    if light_on_hour > x_min:
        intervals.append((x_min, min(light_on_hour, x_max)))
    if light_off_hour < x_max:
        intervals.append((max(light_off_hour, x_min), x_max))
    return [(start, end) for start, end in intervals if end > start]


def _add_night_shading(ax, light_on_hour, light_off_hour, x_min=0.0, x_max=24.0):
    for start, end in _night_intervals(light_on_hour, light_off_hour, x_min=x_min, x_max=x_max):
        ax.axvspan(start, end, color="black", alpha=0.3, zorder=-10, linewidth=0)


def _hour_axis_extent(hourly_time_s):
    x_hours = np.asarray(hourly_time_s, dtype=np.float64) / 3600.0
    if x_hours.size == 0:
        return 0.0, 24.0
    if x_hours.size == 1:
        half_width = 0.5
    else:
        diffs = np.diff(x_hours)
        half_width = float(np.nanmedian(diffs) / 2.0) if np.isfinite(diffs).any() else 0.5
        if half_width <= 0:
            half_width = 0.5
    return float(x_hours[0] - half_width), float(x_hours[-1] + half_width)


def _make_masks_figure(coverage, missing, mode3, working, fs, dpi, light_on_hour, light_off_hour):
    fig, ax = plt.subplots(figsize=(12, 2.8), dpi=dpi)
    ds = max(1, int(fs * 60))

    def downsample(arr):
        trim = arr[: (arr.size // ds) * ds]
        if trim.size == 0:
            return np.zeros(0, dtype=np.float64)
        return trim.reshape(-1, ds).mean(axis=1)

    time_h = np.arange(len(downsample(coverage))) * ds / fs / 3600.0
    ax.plot(time_h, downsample(coverage), label="Coverage", color="#2ca02c")
    ax.plot(time_h, downsample(missing), label="Missing", color="#d62728")
    ax.plot(time_h, downsample(mode3), label="Mode3", color="#1f77b4")
    ax.plot(time_h, downsample(working), label="Working", color="#9467bd")
    ax.set_xlim(0, 24)
    _add_night_shading(ax, light_on_hour, light_off_hour, x_min=0.0, x_max=24.0)
    ax.set_xlabel("ZT / hour")
    ax.set_ylabel("Fraction per minute")
    ax.set_title("24 h Coverage / Missing / Working Overview")
    ax.legend(loc="upper right", ncol=4, fontsize=8)
    return fig


def _make_state_timeline(state_time_s, state_label, state_names, dpi, light_on_hour, light_off_hour, state_valid_mask=None):
    colors = _state_color_map()
    fig, ax = plt.subplots(figsize=(12, 1.8), dpi=dpi)
    state_values = np.asarray([state_names[int(v)] for v in state_label], dtype=object)
    valid_mask = (
        np.asarray(state_valid_mask, dtype=bool)
        if state_valid_mask is not None
        else np.ones_like(state_label, dtype=bool)
    )
    for name in state_names:
        mask = (state_values == name) & valid_mask
        ax.scatter(
            state_time_s[mask] / 3600.0,
            np.zeros(np.sum(mask)),
            s=16,
            color=colors[name],
            label=name,
            marker="s",
        )
    ax.set_xlim(0, 24)
    _add_night_shading(ax, light_on_hour, light_off_hour, x_min=0.0, x_max=24.0)
    ax.set_yticks([])
    ax.set_xlabel("ZT / hour")
    ax.set_title("State Timeline")
    ax.legend(loc="upper right", ncol=3, fontsize=8)
    return fig


def _make_hourly_heatmap(hourly_feature, band_names, hourly_time_s, dpi, light_on_hour, light_off_hour, normalized=False):
    fig, ax = plt.subplots(figsize=(12, 3.5), dpi=dpi)
    feature = np.asarray(hourly_feature, dtype=np.float64)
    if feature.ndim == 3:
        valid_count = np.sum(np.isfinite(feature), axis=1)
        feature = np.divide(
            np.nansum(feature, axis=1),
            valid_count,
            out=np.full((feature.shape[0], feature.shape[2]), np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
    x0, x1 = _hour_axis_extent(hourly_time_s)
    if normalized:
        data = feature.T
        im = ax.imshow(
            data,
            aspect="auto",
            interpolation="nearest",
            cmap="coolwarm",
            vmin=0.5,
            vmax=1.5,
            extent=(x0, x1, len(band_names) - 0.5, -0.5),
        )
        title = "Hourly Band Power Heatmap (ratio to daily band mean)"
    else:
        data = 10.0 * np.log10(np.maximum(feature.T, 1e-12))
        im = ax.imshow(
            data,
            aspect="auto",
            interpolation="nearest",
            cmap="viridis",
            extent=(x0, x1, len(band_names) - 0.5, -0.5),
        )
        title = "Hourly Band Power Heatmap (dB)"
    _add_night_shading(ax, light_on_hour, light_off_hour, x_min=max(0.0, x0), x_max=min(24.0, x1))
    ax.set_yticks(np.arange(len(band_names)))
    ax.set_yticklabels(band_names)
    x_hours = np.asarray(hourly_time_s, dtype=np.float64) / 3600.0
    ax.set_xticks(x_hours)
    ax.set_xticklabels([f"{int(v):02d}" for v in x_hours], rotation=0)
    ax.set_xlabel("Hour")
    ax.set_xlim(x0, x1)
    ax.set_title(title)
    fig.colorbar(im, ax=ax, shrink=0.8)
    return fig


def _make_state_occupancy_plot(state_occupancy, hourly_time_s, state_names, dpi, light_on_hour, light_off_hour):
    colors = _state_color_map()
    fig, ax = plt.subplots(figsize=(12, 3.0), dpi=dpi)
    bottom = np.zeros(state_occupancy.shape[0], dtype=np.float64)
    x = hourly_time_s / 3600.0
    for idx, state_name in enumerate(state_names):
        if idx >= state_occupancy.shape[1]:
            continue
        values = state_occupancy[:, idx]
        ax.bar(x, values, bottom=bottom, width=0.8, color=colors[state_name], label=state_name)
        bottom += values
    x_max = max(24.0, np.max(x) + 0.5 if x.size else 24.0)
    ax.set_xlim(-0.5, x_max)
    _add_night_shading(ax, light_on_hour, light_off_hour, x_min=0.0, x_max=min(24.0, x_max))
    ax.set_xlabel("ZT / hour")
    ax.set_ylabel("Occupancy")
    ax.set_title("Hourly State Occupancy")
    ax.legend(loc="upper right", ncol=3, fontsize=8)
    return fig


def _make_state_band_summary(state_stratified, band_names, state_names, dpi):
    colors = _state_color_map()
    state_data = np.asarray(state_stratified, dtype=np.float64)
    if state_data.ndim == 4:
        valid_count = np.sum(np.isfinite(state_data), axis=(0, 2))
        band_summary = np.divide(
            np.nansum(state_data, axis=(0, 2)),
            valid_count,
            out=np.full((len(state_names), len(band_names)), np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
    elif np.isfinite(state_data).any():
        valid_count = np.sum(np.isfinite(state_data), axis=0)
        band_summary = np.divide(
            np.nansum(state_data, axis=0),
            valid_count,
            out=np.full((len(state_names), len(band_names)), np.nan, dtype=np.float64),
            where=valid_count > 0,
        )
    else:
        band_summary = np.full((len(state_names), len(band_names)), np.nan, dtype=np.float64)
    fig, ax = plt.subplots(figsize=(12, 4.0), dpi=dpi)
    x = np.arange(len(band_names))
    width = 0.12
    for offset, state_name in enumerate(state_names):
        idx = offset
        ax.bar(
            x + (offset - len(state_names) / 2) * width,
            10.0 * np.log10(np.maximum(band_summary[idx], 1e-12)),
            width=width,
            color=colors[state_name],
            label=state_name,
        )
    ax.set_xticks(x)
    ax.set_xticklabels(band_names, rotation=30, ha="right")
    ax.set_ylabel("Mean Band Power (dB)")
    ax.set_title("State-Stratified Band Power Summary")
    ax.legend(loc="upper right", ncol=3, fontsize=8)
    return fig


def _make_normalized_state_heatmap(normalized_state_stratified, band_names, state_names, hourly_time_s, dpi, light_on_hour, light_off_hour):
    normalized_data = np.asarray(normalized_state_stratified, dtype=np.float64)
    if normalized_data.ndim == 4:
        valid_count = np.sum(np.isfinite(normalized_data), axis=2)
        normalized_data = np.divide(
            np.nansum(normalized_data, axis=2),
            valid_count,
            out=np.full(
                (normalized_data.shape[0], normalized_data.shape[1], normalized_data.shape[3]),
                np.nan,
                dtype=np.float64,
            ),
            where=valid_count > 0,
        )
    row_labels = []
    rows = []
    for state_idx, state_name in enumerate(state_names):
        for band_idx, band_name in enumerate(band_names):
            row_labels.append(f"{state_name} | {band_name}")
            rows.append(normalized_data[:, state_idx, band_idx])
        if state_idx < len(state_names) - 1:
            row_labels.append("")
            rows.append(np.full(normalized_data.shape[0], np.nan, dtype=np.float64))
    data = np.asarray(rows, dtype=np.float64)
    fig_height = max(4.0, 0.24 * max(1, len(row_labels)))
    fig, ax = plt.subplots(figsize=(12, fig_height), dpi=dpi)
    valid = data[np.isfinite(data)]
    if valid.size:
        vmin = min(0.5, float(np.nanpercentile(valid, 5)))
        vmax = max(1.5, float(np.nanpercentile(valid, 95)))
        if vmax <= vmin:
            vmax = vmin + 1.0
    else:
        vmin, vmax = 0.5, 1.5
    x0, x1 = _hour_axis_extent(hourly_time_s)
    im = ax.imshow(
        data,
        aspect="auto",
        interpolation="nearest",
        cmap="coolwarm",
        vmin=vmin,
        vmax=vmax,
        extent=(x0, x1, len(row_labels) - 0.5, -0.5),
    )
    _add_night_shading(ax, light_on_hour, light_off_hour, x_min=max(0.0, x0), x_max=min(24.0, x1))
    ax.set_yticks(np.arange(len(row_labels)))
    ax.set_yticklabels(row_labels, fontsize=7)
    for row_idx, label in enumerate(row_labels):
        if label == "":
            ax.axhline(row_idx - 0.5, color="black", linewidth=0.8, linestyle="--")
    x_hours = np.asarray(hourly_time_s, dtype=np.float64) / 3600.0
    ax.set_xticks(x_hours)
    ax.set_xticklabels([f"{int(v):02d}" for v in x_hours], rotation=0)
    ax.set_xlabel("Hour")
    ax.set_xlim(x0, x1)
    ax.set_title("State-Normalized Hourly Power (ratio to daily state-band mean)")
    fig.colorbar(im, ax=ax, shrink=0.85)
    return fig


def generate_html_report(h5_path, output_html=None, config=None):
    cfg = resolve_config(config)
    embed_png = bool(cfg.get("report.embed_png", True))
    dpi = int(cfg.get("report.plot_dpi", 120))
    light_on_hour = float(cfg.get("general.light_on_hour", 6.0))
    light_off_hour = float(cfg.get("general.light_off_hour", 18.0))
    report_dir = cfg.get("report.report_output_dir", "") or os.path.dirname(h5_path)
    os.makedirs(report_dir, exist_ok=True)
    if output_html is None:
        basename = os.path.splitext(os.path.basename(h5_path))[0]
        output_html = os.path.join(report_dir, f"{basename}_report.html")

    with h5py.File(h5_path, "r") as h5f:
        metadata = {
            key: value
            for key, value in h5f["metadata"].attrs.items()
            if key != "config_json"
        }
        coverage = np.asarray(h5f["lfp/file_coverage_mask"][:], dtype=np.float64)
        missing = np.asarray(h5f["lfp/missing_mask"][:], dtype=np.float64)
        mode3 = np.asarray(h5f["task/mode3_active_mask"][:], dtype=np.float64)
        working = np.asarray(h5f["task/working_mask"][:], dtype=np.float64)
        state_time_s = np.asarray(h5f["time/state_time_s"][:], dtype=np.float64)
        state_label = np.asarray(h5f["states/state_label"][:], dtype=np.int16)
        state_names_all = [str(v) for v in h5f["states/state_names"][:].astype(str)]
        state_valid_mask = (
            np.asarray(h5f["states/state_valid_mask"][:], dtype=bool)
            if "state_valid_mask" in h5f["states"]
            else np.ones_like(state_label, dtype=bool)
        )
        hourly_feature = np.asarray(h5f["rhythm/hourly_feature_table"][:], dtype=np.float64)
        hourly_feature_norm = (
            np.asarray(h5f["rhythm/hourly_feature_table_normalized"][:], dtype=np.float64)
            if "hourly_feature_table_normalized" in h5f["rhythm"]
            else None
        )
        hourly_time_s = np.asarray(h5f["time/hourly_time_s"][:], dtype=np.float64)
        state_occupancy = np.asarray(h5f["rhythm/state_occupancy_table"][:], dtype=np.float64)
        state_stratified = np.asarray(h5f["rhythm/state_stratified_feature_table"][:], dtype=np.float64)
        state_stratified_norm = (
            np.asarray(h5f["rhythm/state_stratified_feature_table_normalized"][:], dtype=np.float64)
            if "state_stratified_feature_table_normalized" in h5f["rhythm"]
            else None
        )
        band_names = [str(v) for v in h5f["rhythm/band_names"][:].astype(str)]
        state_names = [str(v) for v in h5f["rhythm/state_names"][:].astype(str)]
        state_names_occupancy = (
            [str(v) for v in h5f["rhythm/state_names_occupancy"][:].astype(str)]
            if "state_names_occupancy" in h5f["rhythm"]
            else state_names_all
        )
        fs = float(h5f["metadata"].attrs["lfp_sample_rate_hz"])
        hourly_norm_method = str(
            h5f["rhythm"].attrs.get("hourly_reference_normalization_method", "not_available")
        )
        state_norm_method = str(
            h5f["rhythm"].attrs.get("state_reference_normalization_method", "not_available")
        )
        representative_channel = int(
            h5f["states/representative_channel_index"][()]
        ) if "representative_channel_index" in h5f["states"] else -1
        nrem_lfhf_threshold = float(h5f["states/nrem_lfhf_threshold"][()]) if "nrem_lfhf_threshold" in h5f["states"] else np.nan
        rem_highfreq_threshold = float(h5f["states/rem_highfreq_threshold"][()]) if "rem_highfreq_threshold" in h5f["states"] else np.nan
        sleep_imu_threshold = float(h5f["states/sleep_imu_threshold"][()]) if "sleep_imu_threshold" in h5f["states"] else np.nan
        imu_smoothing_sigma_s = float(h5f["states/imu_smoothing_sigma_s"][()]) if "imu_smoothing_sigma_s" in h5f["states"] else np.nan
        threshold_method = (
            str(h5f["states/threshold_method_json"][()].decode("utf-8"))
            if "threshold_method_json" in h5f["states"] and isinstance(h5f["states/threshold_method_json"][()], bytes)
            else str(h5f["states/threshold_method_json"][()])
            if "threshold_method_json" in h5f["states"]
            else "{}"
        )
        review_complete = bool(
            h5f["states/review"].attrs.get("review_complete", False)
        ) if "review" in h5f["states"] else False

    summary_items = {
        "Coverage %": float(np.mean(coverage) * 100.0),
        "Missing %": float(np.mean(missing) * 100.0),
        "Mode3 %": float(np.mean(mode3) * 100.0),
        "Working %": float(np.mean(working) * 100.0),
        "MiniWake %": float(np.mean(state_label[state_valid_mask] == STATE_CODES["MiniWake"]) * 100.0) if np.any(state_valid_mask) else 0.0,
        "State-valid %": float(np.mean(state_valid_mask) * 100.0),
        "Representative ch": representative_channel,
        "Sleep-like IMU threshold": sleep_imu_threshold,
        "NREM LF/HF score threshold": nrem_lfhf_threshold,
        "REM 80-250 Hz score threshold": rem_highfreq_threshold,
        "Shared smoothing sigma (s)": imu_smoothing_sigma_s,
        "Threshold methods": threshold_method,
        "Review complete": review_complete,
        "Hourly norm method": hourly_norm_method,
        "State norm method": state_norm_method,
    }

    figures = [
        _fig_to_html(
            _make_masks_figure(coverage, missing, mode3, working, fs, dpi, light_on_hour, light_off_hour),
            embed_png,
        ),
        _fig_to_html(
            _make_state_timeline(
                state_time_s,
                state_label,
                state_names_all,
                dpi,
                light_on_hour,
                light_off_hour,
                state_valid_mask,
            ),
            embed_png,
        ),
        _fig_to_html(
            _make_hourly_heatmap(
                hourly_feature_norm if hourly_feature_norm is not None else hourly_feature,
                band_names,
                hourly_time_s,
                dpi,
                light_on_hour,
                light_off_hour,
                normalized=hourly_feature_norm is not None,
            ),
            embed_png,
        ),
        _fig_to_html(
            _make_state_occupancy_plot(
                state_occupancy,
                hourly_time_s,
                state_names_occupancy,
                dpi,
                light_on_hour,
                light_off_hour,
            ),
            embed_png,
        ),
        _fig_to_html(_make_state_band_summary(state_stratified, band_names, state_names, dpi), embed_png),
    ]
    if state_stratified_norm is not None:
        figures.append(
            _fig_to_html(
                _make_normalized_state_heatmap(
                    state_stratified_norm,
                    band_names,
                    state_names,
                    hourly_time_s,
                    dpi,
                    light_on_hour,
                    light_off_hour,
                ),
                embed_png,
            )
        )

    metadata_rows = "".join(
        f"<tr><th>{key}</th><td>{value}</td></tr>"
        for key, value in metadata.items()
    )
    summary_rows = "".join(
        f"<tr><th>{key}</th><td>{value}</td></tr>"
        if isinstance(value, bool)
        else f"<tr><th>{key}</th><td>{value:.2f}</td></tr>"
        if isinstance(value, (int, float))
        else f"<tr><th>{key}</th><td>{value}</td></tr>"
        for key, value in summary_items.items()
    )
    state_name_map = json.dumps(STATE_CODES, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="utf-8">
  <title>Continuous LFP Rhythm Daily Report</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; color: #222; }}
    h1, h2 {{ margin-bottom: 0.2em; }}
    .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    th, td {{ border: 1px solid #d9d9d9; padding: 6px 8px; text-align: left; font-size: 14px; }}
    th {{ background: #f4f6f8; }}
    .panel {{ margin: 18px 0; }}
    .figure {{ margin: 18px 0; }}
    code {{ background: #f6f8fa; padding: 2px 4px; }}
  </style>
</head>
<body>
  <h1>Continuous 24/7 LFP Daily Report</h1>
  <p>State code map: <code>{state_name_map}</code></p>

  <div class="grid">
    <div>
      <h2>Metadata</h2>
      <table>{metadata_rows}</table>
    </div>
    <div>
      <h2>QC Summary</h2>
      <table>{summary_rows}</table>
    </div>
  </div>

  <div class="panel">
    <h2>24 h Overview</h2>
    <div class="figure">{figures[0]}</div>
  </div>
  <div class="panel">
    <h2>State Timeline</h2>
    <div class="figure">{figures[1]}</div>
  </div>
  <div class="panel">
    <h2>Hourly Rhythm</h2>
    <div class="figure">{figures[2]}</div>
    <div class="figure">{figures[3]}</div>
  </div>
  <div class="panel">
    <h2>State-Stratified Summary</h2>
    <div class="figure">{figures[4]}</div>
  </div>
  {f'<div class="panel"><h2>State-Normalized Hourly Power</h2><div class="figure">{figures[5]}</div></div>' if len(figures) > 5 else ''}
</body>
</html>
"""

    with open(output_html, "w", encoding="utf-8") as f:
        f.write(html)
    return output_html
