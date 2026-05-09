import csv
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

from Common_Analysis.behavior_analysis import (
    BIAS_STATE_LABELS,
    PERF_STATE_LABELS,
    RULE_LABELS,
    classify_bias_state,
    classify_perf_state,
    resolve_sd_card_dir_from_source_root,
)
from Common_Analysis.behavior_rt import (
    RT_CLASS_LABELS,
    compute_hourly_rt_features,
    parse_rt_trials,
)

from .config import resolve_config
from .connectivity_features import ensure_connectivity_features, load_connectivity_sidecar
from .slice_statistics import (
    _assign_fdr_q_values,
    _sanitize_filename_component,
    _to_float,
    build_slice_statistics_table,
    slice_statistics_id_from_h5_paths,
)

try:
    from scipy import stats as _scipy_stats
except Exception:
    _scipy_stats = None


PAIRED_CONTEXT_VERSION = 1
DEFAULT_CONTEXT_WINDOW_S = 3600.0
DEFAULT_N_PERMUTATIONS = 1000
MIN_GROUP_N = 5


def expected_paired_trial_context_summary_path(h5_paths, output_dir):
    slice_id = slice_statistics_id_from_h5_paths(h5_paths)
    return os.path.join(
        os.path.abspath(str(output_dir or os.getcwd())),
        f"{_sanitize_filename_component(slice_id)}_paired_summary.json",
    )


def run_paired_trial_context_analysis(
    h5_paths,
    output_dir,
    config=None,
    reference_scope="daily",
    context_window_s=DEFAULT_CONTEXT_WINDOW_S,
    n_permutations=DEFAULT_N_PERMUTATIONS,
    random_seed=0,
    force_connectivity=False,
    make_figures=True,
    progress_callback=None,
):
    output_dir = os.path.abspath(str(output_dir or os.getcwd()))
    os.makedirs(output_dir, exist_ok=True)
    paths = [os.path.abspath(str(path)) for path in h5_paths or [] if path]
    if not paths:
        raise ValueError("No H5 paths were provided for paired trial context analysis.")

    connectivity = {}
    connectivity_warnings = []
    for idx, path in enumerate(paths, start=1):
        _emit(progress_callback, "connectivity_file", idx - 1, len(paths), {"h5_path": path})
        try:
            ensure_connectivity_features(
                path,
                output_dir,
                config=config,
                force=force_connectivity,
                progress_callback=progress_callback,
            )
            connectivity[path] = load_connectivity_sidecar(path, output_dir)
        except Exception as exc:
            connectivity[path] = None
            connectivity_warnings.append(f"Connectivity skipped for {path}: {exc}")
        _emit(progress_callback, "connectivity_file", idx, len(paths), {"h5_path": path})

    table = build_paired_trial_context_table(
        paths,
        output_dir,
        config=config,
        reference_scope=reference_scope,
        context_window_s=context_window_s,
        connectivity=connectivity,
    )
    table.setdefault("warnings", []).extend(connectivity_warnings)
    stats = compute_paired_trial_context_statistics(
        table,
        n_permutations=n_permutations,
        random_seed=random_seed,
    )
    result = {
        "version": PAIRED_CONTEXT_VERSION,
        "table": table,
        "stats": stats,
        "reference_scope": str(reference_scope or "daily"),
        "context_window_s": float(context_window_s),
        "n_permutations": int(n_permutations),
        "random_seed": int(random_seed),
    }
    slice_id = table.get("slice_id") or slice_statistics_id_from_h5_paths(paths)
    return write_paired_trial_context_outputs(result, output_dir, slice_id, make_figures=make_figures)


def build_paired_trial_context_table(
    h5_paths,
    output_dir,
    config=None,
    reference_scope="daily",
    context_window_s=DEFAULT_CONTEXT_WINDOW_S,
    connectivity=None,
):
    cfg = resolve_config(config)
    hourly_table = build_slice_statistics_table(h5_paths, config=cfg, reference_scope=reference_scope)
    rows = list(hourly_table.get("rows", []))
    if not rows:
        raise ValueError("No hourly rows available for paired context analysis.")
    source_root = _resolve_source_root_from_h5(h5_paths)
    sd_card_dir = resolve_sd_card_dir_from_source_root(source_root)
    slice_start_ms = min(_to_float(row.get("epoch_ms")) for row in rows if np.isfinite(_to_float(row.get("epoch_ms")))) - 1800.0 * 1000.0
    slice_end_ms = max(_to_float(row.get("epoch_ms")) for row in rows if np.isfinite(_to_float(row.get("epoch_ms")))) + 1800.0 * 1000.0
    rt_records, rt_fit, rt_warnings = parse_rt_trials(
        sd_card_dir,
        slice_start_ms=slice_start_ms,
        slice_end_ms=slice_end_ms,
        config=cfg.get("behavior.rt", {}),
    )
    trial_behavior = _load_trial_behavior_from_h5s(h5_paths)
    rt_records = _attach_trial_behavior_to_rt_records(rt_records, trial_behavior)
    hourly_rt = compute_hourly_rt_features(rt_records, slice_start_ms, slice_end_ms)
    hourly_rows = _prepare_hourly_rows(rows, h5_paths, output_dir, connectivity, config=cfg)
    context_columns = _context_columns(hourly_rows)
    trial_rows = []
    for trial in rt_records:
        sample_ms = trial.get("sample_start_epoch_ms")
        choice_ms = trial.get("choice_epoch_ms")
        if sample_ms is None or choice_ms is None or not np.isfinite(float(sample_ms)) or not np.isfinite(float(choice_ms)):
            continue
        pre = _aggregate_window(hourly_rows, float(sample_ms) - float(context_window_s) * 1000.0, float(sample_ms), context_columns)
        post = _aggregate_window(hourly_rows, float(choice_ms), float(choice_ms) + float(context_window_s) * 1000.0, context_columns)
        row = _trial_base_row(trial)
        row["pre_valid_coverage_s"] = float(pre.pop("_coverage_s", 0.0))
        row["post_valid_coverage_s"] = float(post.pop("_coverage_s", 0.0))
        for key, value in pre.items():
            row[f"pre_{key}"] = value
        for key, value in post.items():
            row[f"post_{key}"] = value
        for key in context_columns:
            pre_val = _to_float(row.get(f"pre_{key}"))
            post_val = _to_float(row.get(f"post_{key}"))
            row[f"delta_{key}"] = float(post_val - pre_val) if np.isfinite(pre_val) and np.isfinite(post_val) else np.nan
        trial_rows.append(row)

    return {
        "slice_id": hourly_table.get("slice_id") or slice_statistics_id_from_h5_paths(h5_paths),
        "h5_paths": list(hourly_table.get("h5_paths", h5_paths)),
        "source_root": source_root,
        "sd_card_dir": sd_card_dir,
        "reference_scope": str(reference_scope or "daily"),
        "context_window_s": float(context_window_s),
        "hourly_rows": hourly_rows,
        "trial_rows": trial_rows,
        "context_columns": context_columns,
        "rt_records": rt_records,
        "rt_fit": rt_fit,
        "hourly_rt": hourly_rt,
        "warnings": list(hourly_table.get("warnings", [])) + rt_warnings,
    }


def compute_paired_trial_context_statistics(table, n_permutations=DEFAULT_N_PERMUTATIONS, random_seed=0):
    rows = list(table.get("trial_rows", []))
    rng = np.random.default_rng(int(random_seed))
    context_columns = list(table.get("context_columns", []))
    group_specs = _trial_group_specs()
    pre_rows = []
    post_rows = []
    delta_rows = []
    for feature in context_columns:
        for spec in group_specs:
            pre_rows.append(_group_test(rows, f"pre_{feature}", spec, rng, int(n_permutations), "pre_context_behavior"))
            post_rows.append(_group_test(rows, f"post_{feature}", spec, rng, int(n_permutations), "post_context_behavior"))
        delta_rows.append(_paired_delta_test(rows, f"delta_{feature}", rng, int(n_permutations)))
    _assign_fdr_q_values(pre_rows)
    _assign_fdr_q_values(post_rows)
    _assign_fdr_q_values(delta_rows)
    return {
        "pre_context_behavior_tests": _sort_rows(pre_rows),
        "post_context_behavior_tests": _sort_rows(post_rows),
        "pre_post_delta_tests": _sort_rows(delta_rows),
        "n_permutations": int(n_permutations),
        "method": "Trial-level paired context analysis with behavior-group permutation tests and paired sign-flip delta tests.",
    }


def write_paired_trial_context_outputs(result, output_dir, slice_id, make_figures=True):
    output_dir = os.path.abspath(str(output_dir or os.getcwd()))
    os.makedirs(output_dir, exist_ok=True)
    safe_id = _sanitize_filename_component(slice_id or "slice")
    table = result["table"]
    stats = result["stats"]
    rt_csv = os.path.join(output_dir, f"{safe_id}_rt_behavior_features.csv")
    rt_json = os.path.join(output_dir, f"{safe_id}_rt_behavior_features.json")
    context_csv = os.path.join(output_dir, f"{safe_id}_paired_trial_context_table.csv")
    pre_csv = os.path.join(output_dir, f"{safe_id}_paired_pre_context_behavior_tests.csv")
    post_csv = os.path.join(output_dir, f"{safe_id}_paired_post_context_behavior_tests.csv")
    delta_csv = os.path.join(output_dir, f"{safe_id}_paired_pre_post_delta_tests.csv")
    summary_json = os.path.join(output_dir, f"{safe_id}_paired_summary.json")

    _write_csv(table.get("rt_records", []), rt_csv)
    _write_csv(table.get("trial_rows", []), context_csv)
    _write_csv(stats.get("pre_context_behavior_tests", []), pre_csv)
    _write_csv(stats.get("post_context_behavior_tests", []), post_csv)
    _write_csv(stats.get("pre_post_delta_tests", []), delta_csv)
    with open(rt_json, "w", encoding="utf-8") as handle:
        json.dump({
            "version": PAIRED_CONTEXT_VERSION,
            "rt_fit": table.get("rt_fit", {}),
            "hourly_rt": _json_ready(table.get("hourly_rt", {})),
            "rt_class_labels": RT_CLASS_LABELS,
        }, handle, ensure_ascii=False, indent=2)

    figure_paths = make_paired_trial_context_figures(result, output_dir, safe_id) if make_figures else []
    summary = {
        "version": PAIRED_CONTEXT_VERSION,
        "slice_id": safe_id,
        "reference_scope": result.get("reference_scope", table.get("reference_scope", "daily")),
        "context_window_s": float(result.get("context_window_s", table.get("context_window_s", DEFAULT_CONTEXT_WINDOW_S))),
        "n_permutations": int(result.get("n_permutations", stats.get("n_permutations", 0))),
        "n_h5_files": int(len(table.get("h5_paths", []))),
        "n_trials": int(len(table.get("trial_rows", []))),
        "n_rt_trials": int(len(table.get("rt_records", []))),
        "h5_paths": list(table.get("h5_paths", [])),
        "rt_features_csv": rt_csv,
        "rt_features_json": rt_json,
        "paired_context_csv": context_csv,
        "pre_context_tests_csv": pre_csv,
        "post_context_tests_csv": post_csv,
        "pre_post_delta_tests_csv": delta_csv,
        "summary_json": summary_json,
        "figure_paths": figure_paths,
        "top_pre_context_effects": stats.get("pre_context_behavior_tests", [])[:40],
        "top_post_context_effects": stats.get("post_context_behavior_tests", [])[:40],
        "top_pre_post_delta_effects": stats.get("pre_post_delta_tests", [])[:40],
        "rt_fit": table.get("rt_fit", {}),
        "warnings": list(table.get("warnings", []))[:100],
        "method": stats.get("method", ""),
    }
    with open(summary_json, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)
    return summary


def make_paired_trial_context_figures(result, output_dir, slice_id):
    paths = []
    effect_path = os.path.join(output_dir, f"{slice_id}_paired_top_effects.png")
    state_path = os.path.join(output_dir, f"{slice_id}_paired_state_pre_post.png")
    distribution_path = os.path.join(output_dir, f"{slice_id}_paired_selected_distributions.png")
    _plot_top_effects(result["stats"], effect_path)
    _plot_state_pre_post(result["table"].get("trial_rows", []), state_path)
    _plot_selected_distributions(result["table"].get("trial_rows", []), result["stats"], distribution_path)
    paths.extend([effect_path, state_path, distribution_path])
    return paths


def _prepare_hourly_rows(rows, h5_paths, output_dir, connectivity, config=None):
    cfg = resolve_config(config)
    include_pair_targets = bool(cfg.get("connectivity.include_channel_pair_targets", False))
    channel_groups, group_names = _connectivity_channel_groups(cfg)
    prepared = []
    for row in rows:
        next_row = dict(row)
        center = _to_float(next_row.get("epoch_ms"))
        next_row["hour_start_epoch_ms"] = center - 1800.0 * 1000.0
        next_row["hour_end_epoch_ms"] = center + 1800.0 * 1000.0
        prepared.append(next_row)

    for h5_path in h5_paths:
        conn = connectivity.get(os.path.abspath(h5_path)) if connectivity else None
        if conn is None:
            conn = load_connectivity_sidecar(h5_path, output_dir)
        if conn is None:
            continue
        meta = conn["metadata"]
        band_names = list(meta.get("band_names", []))
        pac_pairs = list(meta.get("pac_band_pairs", []))
        centers = np.asarray(conn["hour_center_epoch_ms"], dtype=np.float64)
        plv = np.asarray(conn["plv"], dtype=np.float64)
        pac = np.asarray(conn["pac"], dtype=np.float64)
        basename = os.path.basename(h5_path)
        target_rows = [row for row in prepared if str(row.get("source_h5", "")) == basename]
        target_rows = sorted(target_rows, key=lambda row: _to_float(row.get("epoch_ms")))
        for hour_idx, row in enumerate(target_rows):
            if hour_idx >= centers.size:
                break
            for band_idx, band in enumerate(band_names):
                if plv.ndim != 4 or band_idx >= plv.shape[1]:
                    continue
                matrix = plv[hour_idx, band_idx]
                row[f"plv_{_token(band)}_mean"] = _matrix_mean(matrix, off_diagonal=True)
                for group_a, name_a in zip(channel_groups, group_names):
                    for group_b, name_b in zip(channel_groups, group_names):
                        row[f"plv_{_token(band)}_{_token(name_a)}_to_{_token(name_b)}"] = _group_pair_mean(
                            matrix,
                            group_a,
                            group_b,
                            off_diagonal=True,
                        )
                if not include_pair_targets:
                    continue
                for ch_a in range(plv.shape[2]):
                    for ch_b in range(plv.shape[3]):
                        row[f"plv_{_token(band)}_ch{ch_a:02d}_ch{ch_b:02d}"] = float(plv[hour_idx, band_idx, ch_a, ch_b])
            for pair_idx, pair in enumerate(pac_pairs):
                phase = _token(pair.get("phase_band", "phase"))
                amp = _token(pair.get("amplitude_band", "amp"))
                if pac.ndim != 4 or pair_idx >= pac.shape[1]:
                    continue
                matrix = pac[hour_idx, pair_idx]
                prefix = f"pac_{phase}_to_{amp}"
                row[f"{prefix}_mean"] = _matrix_mean(matrix, off_diagonal=False)
                for group_a, name_a in zip(channel_groups, group_names):
                    for group_b, name_b in zip(channel_groups, group_names):
                        row[f"{prefix}_{_token(name_a)}_phase_to_{_token(name_b)}_amp"] = _group_pair_mean(
                            matrix,
                            group_a,
                            group_b,
                            off_diagonal=False,
                        )
                if not include_pair_targets:
                    continue
                for ch_a in range(pac.shape[2]):
                    for ch_b in range(pac.shape[3]):
                        row[f"pac_{phase}_phase_{amp}_amp_ch{ch_a:02d}_ch{ch_b:02d}"] = float(pac[hour_idx, pair_idx, ch_a, ch_b])
    return prepared


def _connectivity_channel_groups(cfg):
    groups = cfg.get("rhythm.average_channel_groups", [])
    names = cfg.get("rhythm.average_group_names", [])
    normalized = []
    for group in groups or []:
        try:
            values = [int(idx) for idx in group]
        except Exception:
            values = []
        if values:
            normalized.append(values)
    if not normalized:
        normalized = [list(range(16))]
    normalized_names = [str(name) for name in names[: len(normalized)]]
    while len(normalized_names) < len(normalized):
        normalized_names.append(f"group_{len(normalized_names) + 1}")
    return normalized, normalized_names


def _matrix_mean(matrix, off_diagonal=False):
    arr = np.asarray(matrix, dtype=np.float64)
    if arr.ndim != 2:
        return np.nan
    mask = np.ones(arr.shape, dtype=bool)
    if off_diagonal and arr.shape[0] == arr.shape[1]:
        np.fill_diagonal(mask, False)
    values = arr[mask]
    return float(np.nanmean(values)) if np.isfinite(values).any() else np.nan


def _group_pair_mean(matrix, group_a, group_b, off_diagonal=False):
    arr = np.asarray(matrix, dtype=np.float64)
    if arr.ndim != 2:
        return np.nan
    a = [int(idx) for idx in group_a if 0 <= int(idx) < arr.shape[0]]
    b = [int(idx) for idx in group_b if 0 <= int(idx) < arr.shape[1]]
    if not a or not b:
        return np.nan
    sub = arr[np.ix_(a, b)]
    mask = np.ones(sub.shape, dtype=bool)
    if off_diagonal and a == b and sub.shape[0] == sub.shape[1]:
        np.fill_diagonal(mask, False)
    values = sub[mask]
    return float(np.nanmean(values)) if np.isfinite(values).any() else np.nan


def _context_columns(hourly_rows):
    if not hourly_rows:
        return []
    columns = []
    for key, value in hourly_rows[0].items():
        if key in {"hour_index", "source_h5_index", "time_s", "epoch_ms", "elapsed_day", "day_index", "zt_hour", "circadian_sin", "circadian_cos", "is_dark", "hour_valid"}:
            continue
        if not isinstance(value, (int, float, np.integer, np.floating)):
            continue
        if key.startswith(("lfp_", "state_", "occ_", "plv_", "pac_")):
            columns.append(key)
    return columns


def _aggregate_window(hourly_rows, start_ms, end_ms, columns):
    out = {key: np.nan for key in columns}
    if end_ms <= start_ms:
        out["_coverage_s"] = 0.0
        return out
    weights = []
    selected = []
    for row in hourly_rows:
        left = _to_float(row.get("hour_start_epoch_ms"))
        right = _to_float(row.get("hour_end_epoch_ms"))
        if not np.isfinite(left) or not np.isfinite(right):
            continue
        overlap = max(0.0, min(float(end_ms), right) - max(float(start_ms), left))
        if overlap > 0:
            weights.append(overlap)
            selected.append(row)
    coverage_s = float(np.sum(weights) / 1000.0) if weights else 0.0
    out["_coverage_s"] = coverage_s
    if not selected:
        return out
    weights = np.asarray(weights, dtype=np.float64)
    for key in columns:
        vals = np.asarray([_to_float(row.get(key)) for row in selected], dtype=np.float64)
        valid = np.isfinite(vals)
        if np.any(valid):
            out[key] = float(np.sum(vals[valid] * weights[valid]) / np.sum(weights[valid]))
    return out


def _trial_base_row(trial):
    return {
        "source_file": trial.get("source_file", ""),
        "trial_num": int(trial.get("trial_num", -1)),
        "sample_start_epoch_ms": _to_float(trial.get("sample_start_epoch_ms")),
        "choice_epoch_ms": _to_float(trial.get("choice_epoch_ms")),
        "rt_ms": _to_float(trial.get("rt_ms")),
        "rt_class": int(trial.get("rt_class", -1)),
        "rt_class_label": trial.get("rt_class_label", RT_CLASS_LABELS.get(int(trial.get("rt_class", -1)), "unknown")),
        "trial_type": int(trial.get("trial_type", -1)),
        "trial_outcome": int(trial.get("trial_outcome", -1)),
        "correct_flag": int(trial.get("correct_flag", -1)),
        "rule": int(trial.get("rule", -1)),
        "rule_label": RULE_LABELS.get(int(trial.get("rule", -1)), "unknown"),
        "trial_smoothed_perf_pct": _to_float(trial.get("trial_smoothed_perf_pct")),
        "perf_state": int(trial.get("perf_state", -1)),
        "perf_state_label": PERF_STATE_LABELS.get(int(trial.get("perf_state", -1)), "unknown"),
        "trial_smoothed_bias_score": _to_float(trial.get("trial_smoothed_bias_score")),
        "bias_state": int(trial.get("bias_state", -1)),
        "bias_state_label": BIAS_STATE_LABELS.get(int(trial.get("bias_state", -1)), "unknown"),
    }


def _load_trial_behavior_from_h5s(h5_paths):
    behavior = {}
    for path in h5_paths:
        try:
            with h5py.File(path, "r") as h5f:
                if "behavior" not in h5f:
                    continue
                grp = h5f["behavior"]
                epoch = np.asarray(grp.get("trial_epoch_ms", []), dtype=np.int64)
                perf = np.asarray(grp.get("trial_smoothed_perf_pct", np.full(epoch.shape, np.nan)), dtype=np.float64)
                bias = np.asarray(grp.get("trial_smoothed_bias_score", np.full(epoch.shape, np.nan)), dtype=np.float64)
                for idx, epoch_ms in enumerate(epoch):
                    behavior[int(epoch_ms)] = {
                        "trial_smoothed_perf_pct": float(perf[idx]) if idx < perf.size else np.nan,
                        "trial_smoothed_bias_score": float(bias[idx]) if idx < bias.size else np.nan,
                    }
        except Exception:
            continue
    return behavior


def _attach_trial_behavior_to_rt_records(records, behavior):
    epochs = np.asarray(sorted(behavior.keys()), dtype=np.int64)
    out = []
    for item in records:
        next_item = dict(item)
        sample_ms = item.get("sample_start_epoch_ms")
        matched = None
        if sample_ms is not None and epochs.size:
            idx = int(np.argmin(np.abs(epochs.astype(np.float64) - float(sample_ms))))
            if abs(float(epochs[idx]) - float(sample_ms)) <= 60000.0:
                matched = behavior.get(int(epochs[idx]))
        if matched:
            next_item.update(matched)
        perf = _to_float(next_item.get("trial_smoothed_perf_pct"))
        bias = _to_float(next_item.get("trial_smoothed_bias_score"))
        next_item["perf_state"] = classify_perf_state(perf)
        next_item["bias_state"] = classify_bias_state(bias)
        out.append(next_item)
    return out


def _resolve_source_root_from_h5(h5_paths):
    for path in h5_paths:
        try:
            with h5py.File(path, "r") as h5f:
                source_root = str(h5f["metadata"].attrs.get("slice_source_root", ""))
                if source_root:
                    return source_root
        except Exception:
            continue
    raise ValueError("Could not resolve slice_source_root from H5 metadata.")


def _trial_group_specs():
    return [
        {"column": "correct_flag", "label": "Outcome", "labels": {0: "error", 1: "correct"}, "valid": {0, 1}},
        {"column": "rt_class", "label": "RT class", "labels": RT_CLASS_LABELS, "valid": {0, 1, 2}},
        {"column": "perf_state", "label": "Performance state", "labels": PERF_STATE_LABELS, "valid": {0, 1, 2}},
        {"column": "bias_state", "label": "Bias state", "labels": BIAS_STATE_LABELS, "valid": {0, 1, 2}},
    ]


def _group_test(rows, feature, spec, rng, n_permutations, family):
    values = np.asarray([_to_float(row.get(feature)) for row in rows], dtype=np.float64)
    groups = np.asarray([_to_float(row.get(spec["column"])) for row in rows], dtype=np.float64)
    valid = np.isfinite(values) & np.isfinite(groups) & np.isin(groups.astype(np.int64), list(spec["valid"]))
    values = values[valid]
    groups = groups[valid].astype(np.int64)
    group_values = {code: values[groups == int(code)] for code in sorted(spec["valid"])}
    group_values = {code: vals[np.isfinite(vals)] for code, vals in group_values.items() if vals.size >= MIN_GROUP_N}
    if len(group_values) < 2:
        return _empty_group_row(feature, spec, family, int(values.size))
    obs = _eta2(values, groups)
    perm = []
    for _ in range(int(n_permutations)):
        shuffled = rng.permutation(groups)
        perm.append(_eta2(values, shuffled))
    p = (1.0 + np.sum(np.asarray(perm) >= obs)) / (float(n_permutations) + 1.0) if n_permutations > 0 else np.nan
    top = _top_pairwise_contrast(group_values)
    return {
        "feature": feature,
        "analysis_family": family,
        "behavior_variable": spec["column"],
        "behavior_label": spec["label"],
        "n_obs": int(values.size),
        "n_groups": int(len(group_values)),
        "effect_size": float(top["effect_size"]),
        "eta2": float(obs),
        "permutation_p": float(p) if np.isfinite(p) else np.nan,
        "fdr_q": np.nan,
        "top_contrast": top["contrast"],
        "top_contrast_label": top["label"],
        "group_summary_json": json.dumps(_group_summary(group_values), ensure_ascii=False),
    }


def _paired_delta_test(rows, feature, rng, n_permutations):
    values = np.asarray([_to_float(row.get(feature)) for row in rows], dtype=np.float64)
    values = values[np.isfinite(values)]
    if values.size < MIN_GROUP_N:
        return {
            "feature": feature,
            "analysis_family": "pre_post_paired_delta",
            "n_obs": int(values.size),
            "effect_size": np.nan,
            "median_delta": np.nan,
            "mean_delta": np.nan,
            "permutation_p": np.nan,
            "fdr_q": np.nan,
        }
    observed = abs(float(np.nanmean(values)))
    null = []
    for _ in range(int(n_permutations)):
        signs = rng.choice(np.asarray([-1.0, 1.0]), size=values.size, replace=True)
        null.append(abs(float(np.nanmean(values * signs))))
    p = (1.0 + np.sum(np.asarray(null) >= observed)) / (float(n_permutations) + 1.0) if n_permutations > 0 else np.nan
    iqr = float(np.nanpercentile(values, 75) - np.nanpercentile(values, 25))
    effect = float(np.nanmedian(values) / max(iqr, 1e-12))
    return {
        "feature": feature,
        "analysis_family": "pre_post_paired_delta",
        "n_obs": int(values.size),
        "effect_size": effect,
        "median_delta": float(np.nanmedian(values)),
        "mean_delta": float(np.nanmean(values)),
        "permutation_p": float(p) if np.isfinite(p) else np.nan,
        "fdr_q": np.nan,
    }


def _eta2(values, groups):
    grand = float(np.nanmean(values))
    ss_total = float(np.nansum((values - grand) ** 2))
    if ss_total <= 1e-12:
        return 0.0
    ss_between = 0.0
    for code in np.unique(groups):
        selected = values[groups == code]
        if selected.size:
            ss_between += float(selected.size) * (float(np.nanmean(selected)) - grand) ** 2
    return ss_between / ss_total


def _top_pairwise_contrast(group_values):
    best = {"effect_size": 0.0, "contrast": "", "label": ""}
    items = sorted(group_values.items())
    for idx, (code_a, vals_a) in enumerate(items):
        for code_b, vals_b in items[idx + 1:]:
            pooled = np.concatenate([vals_a, vals_b])
            iqr = float(np.nanpercentile(pooled, 75) - np.nanpercentile(pooled, 25))
            effect = float((np.nanmedian(vals_b) - np.nanmedian(vals_a)) / max(iqr, 1e-12))
            if abs(effect) > abs(best["effect_size"]):
                best = {
                    "effect_size": effect,
                    "contrast": f"{code_b}-vs-{code_a}",
                    "label": f"{code_b} vs {code_a}",
                }
    return best


def _group_summary(group_values):
    return {
        str(code): {
            "n": int(vals.size),
            "mean": float(np.nanmean(vals)) if vals.size else np.nan,
            "median": float(np.nanmedian(vals)) if vals.size else np.nan,
        }
        for code, vals in group_values.items()
    }


def _empty_group_row(feature, spec, family, n_obs):
    return {
        "feature": feature,
        "analysis_family": family,
        "behavior_variable": spec["column"],
        "behavior_label": spec["label"],
        "n_obs": int(n_obs),
        "n_groups": 0,
        "effect_size": np.nan,
        "eta2": np.nan,
        "permutation_p": np.nan,
        "fdr_q": np.nan,
        "top_contrast": "",
        "top_contrast_label": "",
        "group_summary_json": "{}",
    }


def _sort_rows(rows):
    return sorted(
        rows,
        key=lambda row: (
            np.inf if not np.isfinite(_to_float(row.get("fdr_q"))) else _to_float(row.get("fdr_q")),
            np.inf if not np.isfinite(_to_float(row.get("permutation_p"))) else _to_float(row.get("permutation_p")),
            -abs(_to_float(row.get("effect_size"))) if np.isfinite(_to_float(row.get("effect_size"))) else np.inf,
            str(row.get("feature", "")),
        ),
    )


def _write_csv(rows, path):
    rows = list(rows or [])
    with open(path, "w", encoding="utf-8", newline="") as handle:
        if not rows:
            handle.write("")
            return path
        columns = list(rows[0].keys())
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    return path


def _json_ready(value):
    if isinstance(value, dict):
        return {key: _json_ready(val) for key, val in value.items()}
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (np.integer, np.floating)):
        return value.item()
    return value


def _plot_top_effects(stats, path):
    rows = (stats.get("pre_context_behavior_tests", [])[:10] + stats.get("post_context_behavior_tests", [])[:10])[:20]
    fig, ax = plt.subplots(figsize=(12, 6))
    if not rows:
        ax.text(0.5, 0.5, "No paired effects", ha="center", va="center")
    else:
        y = np.arange(len(rows))
        effects = [abs(_to_float(row.get("effect_size"))) for row in rows]
        labels = [f"{row.get('analysis_family', '')}\n{str(row.get('feature', ''))[:36]}" for row in rows]
        ax.barh(y, effects, color="#4477aa")
        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=7)
        ax.invert_yaxis()
        ax.set_xlabel("|effect size|")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _plot_state_pre_post(rows, path):
    state_cols = ["occ_wake", "occ_nrem", "occ_rem", "occ_working"]
    fig, ax = plt.subplots(figsize=(8, 5))
    x = np.arange(len(state_cols))
    pre = [np.nanmean([_to_float(row.get(f"pre_{col}")) for row in rows]) for col in state_cols]
    post = [np.nanmean([_to_float(row.get(f"post_{col}")) for row in rows]) for col in state_cols]
    ax.bar(x - 0.18, pre, width=0.36, label="pre", color="#4477aa")
    ax.bar(x + 0.18, post, width=0.36, label="post", color="#cc6677")
    ax.set_xticks(x)
    ax.set_xticklabels([col.replace("occ_", "") for col in state_cols])
    ax.set_ylabel("occupancy fraction")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _plot_selected_distributions(rows, stats, path):
    top = (stats.get("pre_context_behavior_tests", []) or stats.get("post_context_behavior_tests", []) or [{}])[0]
    feature = top.get("feature", "")
    variable = top.get("behavior_variable", "")
    fig, ax = plt.subplots(figsize=(8, 5))
    if not feature or not variable:
        ax.text(0.5, 0.5, "No selected effect", ha="center", va="center")
    else:
        values = np.asarray([_to_float(row.get(feature)) for row in rows], dtype=np.float64)
        groups = np.asarray([_to_float(row.get(variable)) for row in rows], dtype=np.float64)
        codes = [int(code) for code in sorted(set(groups[np.isfinite(groups)].astype(int)))]
        data = [values[(groups == code) & np.isfinite(values)] for code in codes]
        ax.boxplot(data, labels=[str(code) for code in codes], showfliers=False)
        ax.set_title(f"{feature} by {variable}")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def _token(value):
    text = str(value or "").strip().lower()
    out = []
    for ch in text:
        out.append(ch if ch.isalnum() else "_")
    return "_".join("".join(out).split("_")).strip("_") or "x"


def _emit(callback, stage, done, total, payload=None):
    if callback is not None:
        callback(stage, int(done), int(total), payload or {})
