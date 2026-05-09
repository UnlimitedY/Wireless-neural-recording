import csv
import datetime as _dt
import json
import os
import sys
import tempfile

os.environ.setdefault("MPLCONFIGDIR", os.path.join(tempfile.gettempdir(), "matplotlib"))
os.environ.setdefault("MPLBACKEND", "Agg")

import h5py
import numpy as np
import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from Common_Analysis.behavior_analysis import BIAS_STATE_LABELS, PERF_STATE_LABELS, RULE_LABELS

from .config import resolve_config
from .tz_compat import ZoneInfo


ANALYSIS_VERSION = 1


def run_longitudinal_dynamics_analysis(
    h5_path,
    output_dir=None,
    config=None,
    write_h5=True,
    make_figures=True,
):
    cfg = resolve_config(config)
    table = build_longitudinal_hourly_table(h5_path, cfg)
    effects = fit_longitudinal_effects(table)

    output_dir = output_dir or cfg.get("report.report_output_dir", "") or os.path.dirname(os.path.abspath(h5_path))
    os.makedirs(output_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(h5_path))[0]
    hourly_csv = os.path.join(output_dir, f"{base}_longitudinal_hourly_table.csv")
    effect_csv = os.path.join(output_dir, f"{base}_longitudinal_effect_summary.csv")
    summary_json = os.path.join(output_dir, f"{base}_longitudinal_summary.json")

    write_hourly_table_csv(table, hourly_csv)
    write_effect_table_csv(effects, effect_csv)

    figure_paths = []
    if make_figures:
        figure_paths = make_longitudinal_figures(table, effects, output_dir, base)

    summary = {
        "version": ANALYSIS_VERSION,
        "h5_path": os.path.abspath(h5_path),
        "hourly_csv": hourly_csv,
        "effect_csv": effect_csv,
        "summary_json": summary_json,
        "figure_paths": figure_paths,
        "n_hours": int(len(table["rows"])),
        "n_targets": int(len(effects["rows"])),
        "top_effects": effects["rows"][:10],
        "research_question": (
            "PFC LFP long-term dynamics as a function of circadian phase, "
            "sleep/wake/cognitive state, task rule, learning stage, and choice bias."
        ),
    }
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    if write_h5:
        write_longitudinal_group(h5_path, table, effects, summary)
    return summary


def build_longitudinal_hourly_table(h5_path, config=None):
    cfg = resolve_config(config)
    with h5py.File(h5_path, "r") as h5f:
        if "hourly_time_s" not in h5f["time"]:
            raise ValueError("H5 has no /time/hourly_time_s. Run Summarize Rhythm first.")
        if "hourly_feature_table" not in h5f["rhythm"]:
            raise ValueError("H5 has no /rhythm/hourly_feature_table. Run Summarize Rhythm first.")

        hourly_time_s = np.asarray(h5f["time/hourly_time_s"][:], dtype=np.float64)
        n_hours = int(hourly_time_s.size)
        hour_valid_mask = (
            np.asarray(h5f["rhythm/hour_valid_mask"][:], dtype=bool)
            if "hour_valid_mask" in h5f["rhythm"]
            else np.ones((n_hours,), dtype=bool)
        )
        if hour_valid_mask.shape[0] != n_hours:
            hour_valid_mask = np.ones((n_hours,), dtype=bool)
        origin_ms = float(
            h5f["time"].attrs.get(
                "time_origin_epoch_ms",
                h5f["metadata"].attrs.get("time_origin_epoch_ms", 0.0),
            )
        )
        timezone_name = str(h5f["metadata"].attrs.get("timezone", cfg.get("general.timezone", "Asia/Shanghai")))
        tz = ZoneInfo(timezone_name)
        band_names = _read_string_dataset(h5f["rhythm/band_names"]) if "band_names" in h5f["rhythm"] else []
        state_names = (
            _read_string_dataset(h5f["rhythm/state_names_occupancy"])
            if "state_names_occupancy" in h5f["rhythm"]
            else []
        )

        hourly_feature_source = "hourly_feature_table_normalized"
        if hourly_feature_source in h5f["rhythm"]:
            hourly_feature = np.asarray(h5f[f"rhythm/{hourly_feature_source}"][:], dtype=np.float64)
            lfp_suffix = "norm"
        else:
            hourly_feature = np.asarray(h5f["rhythm/hourly_feature_table"][:], dtype=np.float64)
            lfp_suffix = "power"

        n_channels = int(hourly_feature.shape[1]) if hourly_feature.ndim == 3 else 1
        group_defs, group_names = _normalize_channel_groups(
            cfg.get("rhythm.average_channel_groups", [list(range(n_channels))]),
            cfg.get("rhythm.average_group_names", ["All channels"]),
            n_channels,
        )
        group_band_values = _group_band_values(hourly_feature, group_defs)

        state_occ = (
            np.asarray(h5f["rhythm/state_occupancy_table"][:], dtype=np.float64)
            if "state_occupancy_table" in h5f["rhythm"]
            else np.zeros((n_hours, 0), dtype=np.float64)
        )
        state_stratified = (
            np.asarray(h5f["rhythm/state_stratified_feature_table_normalized"][:], dtype=np.float64)
            if "state_stratified_feature_table_normalized" in h5f["rhythm"]
            else None
        )
        state_group_band_values = (
            _state_group_band_values(state_stratified, group_defs)
            if state_stratified is not None
            else None
        )

        behavior = h5f["behavior"] if "behavior" in h5f else None
        behavior_arrays = _read_behavior_arrays(behavior, n_hours)

    rows = []
    numeric_columns = []
    for hour_idx, time_s in enumerate(hourly_time_s):
        if not bool(hour_valid_mask[hour_idx]):
            continue
        epoch_ms = origin_ms + float(time_s) * 1000.0
        local_dt = _dt.datetime.fromtimestamp(epoch_ms / 1000.0, tz=_dt.timezone.utc).astimezone(tz)
        zt_hour = local_dt.hour + local_dt.minute / 60.0 + local_dt.second / 3600.0
        row = {
            "hour_index": hour_idx,
            "lfp_hour_valid": 1,
            "time_s": float(time_s),
            "epoch_ms": float(epoch_ms),
            "iso_time": local_dt.isoformat(),
            "elapsed_day": float(time_s) / 86400.0,
            "day_index": int(np.floor(float(time_s) / 86400.0)),
            "zt_hour": float(zt_hour),
            "circadian_sin": float(np.sin(2.0 * np.pi * zt_hour / 24.0)),
            "circadian_cos": float(np.cos(2.0 * np.pi * zt_hour / 24.0)),
            "is_dark": int(_is_dark_hour(zt_hour, cfg)),
        }
        row.update(_behavior_row(behavior_arrays, hour_idx))

        for state_idx, state_name in enumerate(state_names):
            if state_idx < state_occ.shape[1]:
                row[f"occ_{_token(state_name)}"] = _safe_float(state_occ[hour_idx, state_idx])

        for group_idx, group_name in enumerate(group_names):
            for band_idx, band_name in enumerate(band_names):
                row[f"lfp_{_token(group_name)}_{_token(band_name)}_{lfp_suffix}"] = _safe_float(
                    group_band_values[hour_idx, group_idx, band_idx]
                )

        if state_group_band_values is not None:
            max_states = min(len(state_names), state_group_band_values.shape[1])
            for state_idx in range(max_states):
                state_name = state_names[state_idx]
                for group_idx, group_name in enumerate(group_names):
                    for band_idx, band_name in enumerate(band_names):
                        row[
                            f"state_{_token(state_name)}_{_token(group_name)}_{_token(band_name)}_norm"
                        ] = _safe_float(state_group_band_values[hour_idx, state_idx, group_idx, band_idx])
        rows.append(row)

    if rows:
        numeric_columns = [
            key for key, value in rows[0].items()
            if key != "iso_time" and isinstance(value, (int, float, np.integer, np.floating))
        ]

    return {
        "rows": rows,
        "numeric_columns": numeric_columns,
        "band_names": band_names,
        "state_names": state_names,
        "group_names": group_names,
        "lfp_suffix": lfp_suffix,
        "h5_path": os.path.abspath(h5_path),
    }


def fit_longitudinal_effects(table, min_observations=48):
    rows = table["rows"]
    if not rows:
        return {"rows": [], "columns": _effect_columns()}

    target_columns = [
        col for col in table["numeric_columns"]
        if col.startswith("lfp_") or col.startswith("state_")
    ]
    state_occ_columns = [col for col in table["numeric_columns"] if col.startswith("occ_")]
    if state_occ_columns:
        state_occ_columns = state_occ_columns[:-1]

    base_columns = ["elapsed_day", "circadian_sin", "circadian_cos"] + state_occ_columns
    behavior_columns = [
        "rule_frequency",
        "rule_reversal_frequency",
        "perf_intermediate",
        "perf_learned",
        "bias_left",
        "bias_right",
    ]

    design = _design_matrix_rows(rows, base_columns, behavior_columns)
    effect_rows = []
    for target in target_columns:
        y = np.asarray([_to_float(row.get(target)) for row in rows], dtype=np.float64)
        finite = np.isfinite(y) & np.all(np.isfinite(design["base"]), axis=1) & np.all(np.isfinite(design["full"]), axis=1)
        if int(np.sum(finite)) < int(min_observations):
            continue
        y_fit = y[finite]
        base_fit = design["base"][finite]
        full_fit = design["full"][finite]
        base_model = _fit_ols(y_fit, base_fit)
        full_model = _fit_ols(y_fit, full_fit)
        behavior_betas = full_model["beta"][-len(behavior_columns):] if behavior_columns else np.zeros((0,))
        if behavior_betas.size:
            top_idx = int(np.nanargmax(np.abs(behavior_betas)))
            top_term = behavior_columns[top_idx]
            top_beta = float(behavior_betas[top_idx])
        else:
            top_term = ""
            top_beta = np.nan
        effect_rows.append(
            {
                "target": target,
                "n_obs": int(np.sum(finite)),
                "r2_base": float(base_model["r2"]),
                "r2_full": float(full_model["r2"]),
                "delta_r2_behavior": float(max(0.0, full_model["r2"] - base_model["r2"])),
                "top_behavior_term": top_term,
                "top_behavior_beta": top_beta,
                "base_terms": json.dumps(base_columns, ensure_ascii=False),
                "behavior_terms": json.dumps(behavior_columns, ensure_ascii=False),
            }
        )
    effect_rows.sort(key=lambda row: row["delta_r2_behavior"], reverse=True)
    return {"rows": effect_rows, "columns": _effect_columns()}


def write_hourly_table_csv(table, csv_path):
    rows = table["rows"]
    if not rows:
        with open(csv_path, "w", encoding="utf-8", newline="") as handle:
            handle.write("")
        return csv_path
    columns = list(rows[0].keys())
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return csv_path


def write_effect_table_csv(effects, csv_path):
    columns = effects.get("columns") or _effect_columns()
    with open(csv_path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in effects.get("rows", []):
            writer.writerow({column: row.get(column, "") for column in columns})
    return csv_path


def write_longitudinal_group(h5_path, table, effects, summary):
    with h5py.File(h5_path, "a") as h5f:
        if "longitudinal" in h5f:
            del h5f["longitudinal"]
        group = h5f.create_group("longitudinal")
        group.attrs["analysis_version"] = ANALYSIS_VERSION
        group.attrs["summary_json"] = json.dumps(summary, ensure_ascii=False)
        group.attrs["hourly_columns_json"] = json.dumps(table["numeric_columns"], ensure_ascii=False)
        group.attrs["effect_columns_json"] = json.dumps(effects.get("columns", []), ensure_ascii=False)

        matrix = _numeric_matrix(table["rows"], table["numeric_columns"])
        group.create_dataset("hourly_numeric_matrix", data=matrix, compression="gzip")
        _write_string_dataset(group, "hourly_columns", table["numeric_columns"])
        _write_string_dataset(group, "band_names", table.get("band_names", []))
        _write_string_dataset(group, "state_names", table.get("state_names", []))
        _write_string_dataset(group, "group_names", table.get("group_names", []))

        effect_numeric_cols = ["n_obs", "r2_base", "r2_full", "delta_r2_behavior", "top_behavior_beta"]
        effect_matrix = _numeric_matrix(effects.get("rows", []), effect_numeric_cols)
        group.create_dataset("effect_numeric_matrix", data=effect_matrix, compression="gzip")
        _write_string_dataset(group, "effect_numeric_columns", effect_numeric_cols)
        _write_string_dataset(group, "effect_targets", [row.get("target", "") for row in effects.get("rows", [])])
        _write_string_dataset(group, "effect_top_behavior_terms", [row.get("top_behavior_term", "") for row in effects.get("rows", [])])


def make_longitudinal_figures(table, effects, output_dir, base_name):
    paths = []
    behavior_path = os.path.join(output_dir, f"{base_name}_longitudinal_behavior_timeline.png")
    effect_path = os.path.join(output_dir, f"{base_name}_longitudinal_behavior_effects.png")
    heatmap_path = os.path.join(output_dir, f"{base_name}_longitudinal_circadian_heatmap.png")

    _plot_behavior_timeline(table, behavior_path)
    paths.append(behavior_path)
    _plot_effect_summary(effects, effect_path)
    paths.append(effect_path)
    _plot_circadian_heatmap(table, effects, heatmap_path)
    paths.append(heatmap_path)
    return paths


def _read_behavior_arrays(behavior, n_hours):
    defaults = {
        "hourly_rule": np.full(n_hours, -1, dtype=np.int16),
        "hourly_perf_pct": np.full(n_hours, np.nan, dtype=np.float32),
        "hourly_perf_state": np.full(n_hours, -1, dtype=np.int8),
        "hourly_bias_score": np.full(n_hours, np.nan, dtype=np.float32),
        "hourly_bias_state": np.full(n_hours, -1, dtype=np.int8),
        "hourly_trial_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_valid_perf_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_choice_count": np.zeros(n_hours, dtype=np.int32),
        "hourly_fill_source": np.zeros(n_hours, dtype=np.int8),
    }
    if behavior is None:
        return defaults
    for key in list(defaults.keys()):
        if key in behavior:
            values = np.asarray(behavior[key][:])
            defaults[key] = _fit_length(values, n_hours, defaults[key])
    return defaults


def _behavior_row(arrays, hour_idx):
    rule = int(arrays["hourly_rule"][hour_idx])
    perf_state = int(arrays["hourly_perf_state"][hour_idx])
    bias_state = int(arrays["hourly_bias_state"][hour_idx])
    row = {
        "rule_code": rule,
        "rule_label": RULE_LABELS.get(rule, "unknown"),
        "perf_pct": _safe_float(arrays["hourly_perf_pct"][hour_idx]),
        "perf_state_code": perf_state,
        "perf_state_label": PERF_STATE_LABELS.get(perf_state, "unknown"),
        "bias_score": _safe_float(arrays["hourly_bias_score"][hour_idx]),
        "bias_state_code": bias_state,
        "bias_state_label": BIAS_STATE_LABELS.get(bias_state, "unknown"),
        "trial_count": int(arrays["hourly_trial_count"][hour_idx]),
        "valid_perf_count": int(arrays["hourly_valid_perf_count"][hour_idx]),
        "choice_count": int(arrays["hourly_choice_count"][hour_idx]),
        "behavior_fill_source": int(arrays["hourly_fill_source"][hour_idx]),
        "rule_frequency": int(rule == 1),
        "rule_reversal_frequency": int(rule == 2),
        "perf_intermediate": int(perf_state == 1),
        "perf_learned": int(perf_state == 2),
        "bias_left": int(bias_state == 0),
        "bias_right": int(bias_state == 2),
    }
    return row


def _group_band_values(hourly_feature, group_defs):
    feature = np.asarray(hourly_feature, dtype=np.float64)
    if feature.ndim == 2:
        feature = feature[:, np.newaxis, :]
    n_hours, _, n_bands = feature.shape
    out = np.full((n_hours, len(group_defs), n_bands), np.nan, dtype=np.float64)
    for group_idx, group in enumerate(group_defs):
        group_values = feature[:, group, :]
        out[:, group_idx, :] = _safe_nanmean(group_values, axis=1)
    return out


def _state_group_band_values(state_feature, group_defs):
    feature = np.asarray(state_feature, dtype=np.float64)
    if feature.ndim != 4:
        return None
    n_hours, n_states, _, n_bands = feature.shape
    out = np.full((n_hours, n_states, len(group_defs), n_bands), np.nan, dtype=np.float64)
    for group_idx, group in enumerate(group_defs):
        group_values = feature[:, :, group, :]
        out[:, :, group_idx, :] = _safe_nanmean(group_values, axis=2)
    return out


def _design_matrix_rows(rows, base_columns, behavior_columns):
    base_raw = []
    full_raw = []
    for row in rows:
        base = [1.0] + [_to_float(row.get(column)) for column in base_columns]
        behavior = [_to_float(row.get(column)) for column in behavior_columns]
        base_raw.append(base)
        full_raw.append(base + behavior)
    base = _standardize_design(np.asarray(base_raw, dtype=np.float64))
    full = _standardize_design(np.asarray(full_raw, dtype=np.float64))
    return {"base": base, "full": full}


def _standardize_design(matrix):
    out = np.array(matrix, dtype=np.float64, copy=True)
    for col_idx in range(1, out.shape[1]):
        col = out[:, col_idx]
        finite = np.isfinite(col)
        if not np.any(finite):
            continue
        mean = float(np.nanmean(col[finite]))
        std = float(np.nanstd(col[finite]))
        if std <= 1e-12:
            out[finite, col_idx] = col[finite] - mean
        else:
            out[finite, col_idx] = (col[finite] - mean) / std
    return out


def _fit_ols(y, x):
    beta, _, _, _ = np.linalg.lstsq(x, y, rcond=None)
    pred = x @ beta
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - float(np.mean(y))) ** 2))
    r2 = 0.0 if ss_tot <= 1e-12 else 1.0 - ss_res / ss_tot
    return {"beta": beta, "r2": float(max(-np.inf, min(1.0, r2)))}


def _plot_behavior_timeline(table, path):
    rows = table["rows"]
    fig, axes = plt.subplots(3, 1, figsize=(14, 5), dpi=140, sharex=True)
    if rows:
        x = np.asarray([row["elapsed_day"] for row in rows], dtype=np.float64)
        axes[0].step(x, [row["rule_code"] for row in rows], where="mid", color="#4c78a8")
        axes[1].step(x, [row["perf_state_code"] for row in rows], where="mid", color="#4daf7c")
        axes[1].plot(x, [row["perf_pct"] for row in rows], color="#1b7837", alpha=0.35)
        axes[2].step(x, [row["bias_state_code"] for row in rows], where="mid", color="#762a83")
        axes[2].plot(x, [row["bias_score"] for row in rows], color="#762a83", alpha=0.35)
    axes[0].set_ylabel("Rule")
    axes[1].set_ylabel("Perf")
    axes[2].set_ylabel("Bias")
    axes[2].set_xlabel("Elapsed day")
    axes[0].set_title("Behavior State Timeline")
    for ax in axes:
        ax.grid(True, alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_effect_summary(effects, path):
    rows = effects.get("rows", [])[:20]
    fig, ax = plt.subplots(figsize=(12, max(4, 0.35 * max(1, len(rows)))), dpi=140)
    if rows:
        labels = [row["target"] for row in rows][::-1]
        values = [row["delta_r2_behavior"] for row in rows][::-1]
        ax.barh(np.arange(len(rows)), values, color="#4477aa")
        ax.set_yticks(np.arange(len(rows)))
        ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Delta R2 after adding rule / learning stage / bias")
    ax.set_title("Behavior-Linked LFP Targets After Circadian And State Covariates")
    ax.grid(True, axis="x", alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _plot_circadian_heatmap(table, effects, path):
    rows = table["rows"]
    target = _select_heatmap_target(table, effects)
    fig, ax = plt.subplots(figsize=(12, 5), dpi=140)
    if rows and target:
        max_day = int(max(row["day_index"] for row in rows))
        grid = np.full((max_day + 1, 24), np.nan, dtype=np.float64)
        count = np.zeros((max_day + 1, 24), dtype=np.int32)
        for row in rows:
            day = int(row["day_index"])
            hour = int(np.floor(float(row["zt_hour"]))) % 24
            value = _to_float(row.get(target))
            if np.isfinite(value):
                if np.isnan(grid[day, hour]):
                    grid[day, hour] = 0.0
                grid[day, hour] += value
                count[day, hour] += 1
        grid = np.divide(grid, count, out=np.full_like(grid, np.nan), where=count > 0)
        im = ax.imshow(grid, aspect="auto", interpolation="nearest", cmap="viridis")
        fig.colorbar(im, ax=ax, shrink=0.8)
        ax.set_title(f"Circadian Heatmap | {target}")
    else:
        ax.set_title("Circadian Heatmap | no LFP target available")
    ax.set_xlabel("ZT hour")
    ax.set_ylabel("Elapsed day")
    ax.set_xticks(np.arange(0, 24, 2))
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def _select_heatmap_target(table, effects):
    if effects.get("rows"):
        return effects["rows"][0]["target"]
    preferred = [
        column for column in table.get("numeric_columns", [])
        if column.startswith("lfp_") and ("theta" in column or "beta" in column)
    ]
    if preferred:
        return preferred[0]
    candidates = [column for column in table.get("numeric_columns", []) if column.startswith("lfp_")]
    return candidates[0] if candidates else ""


def _normalize_channel_groups(groups, names, n_channels):
    normalized_groups = []
    normalized_names = []
    for idx, group in enumerate(groups or []):
        if not isinstance(group, (list, tuple)):
            continue
        valid = []
        for value in group:
            try:
                ch = int(value)
            except Exception:
                continue
            if 0 <= ch < n_channels and ch not in valid:
                valid.append(ch)
        if valid:
            normalized_groups.append(valid)
            if idx < len(names or []) and str(names[idx]).strip():
                normalized_names.append(str(names[idx]).strip())
            else:
                normalized_names.append(f"group_{idx}")
    if not normalized_groups:
        normalized_groups = [list(range(n_channels))]
        normalized_names = ["All channels"]
    return normalized_groups, normalized_names


def _is_dark_hour(zt_hour, cfg):
    light_on = float(cfg.get("general.light_on_hour", 6.0))
    light_off = float(cfg.get("general.light_off_hour", 18.0))
    if light_on < light_off:
        return zt_hour < light_on or zt_hour >= light_off
    return light_off <= zt_hour < light_on


def _read_string_dataset(dataset):
    return [str(value) for value in dataset[:].astype(str)]


def _write_string_dataset(group, name, values):
    dtype = h5py.string_dtype(encoding="utf-8")
    group.create_dataset(name, data=np.asarray([str(value) for value in values], dtype=dtype))


def _numeric_matrix(rows, columns):
    matrix = np.full((len(rows), len(columns)), np.nan, dtype=np.float64)
    for row_idx, row in enumerate(rows):
        for col_idx, column in enumerate(columns):
            matrix[row_idx, col_idx] = _to_float(row.get(column))
    return matrix


def _effect_columns():
    return [
        "target",
        "n_obs",
        "r2_base",
        "r2_full",
        "delta_r2_behavior",
        "top_behavior_term",
        "top_behavior_beta",
        "base_terms",
        "behavior_terms",
    ]


def _safe_nanmean(values, axis):
    values = np.asarray(values, dtype=np.float64)
    valid_count = np.sum(np.isfinite(values), axis=axis)
    summed = np.nansum(values, axis=axis)
    return np.divide(
        summed,
        valid_count,
        out=np.full(summed.shape, np.nan, dtype=np.float64),
        where=valid_count > 0,
    )


def _fit_length(values, n, default):
    values = np.asarray(values)
    if values.shape[0] == n:
        return values
    out = np.array(default, copy=True)
    take = min(n, values.shape[0])
    if take > 0:
        out[:take] = values[:take]
    return out


def _token(text):
    value = str(text or "unknown").strip().lower()
    keep = []
    for char in value:
        keep.append(char if char.isalnum() else "_")
    token = "_".join(part for part in "".join(keep).split("_") if part)
    return token or "unknown"


def _safe_float(value):
    value = _to_float(value)
    return float(value) if np.isfinite(value) else np.nan


def _to_float(value):
    try:
        if value is None:
            return np.nan
        return float(value)
    except Exception:
        return np.nan
